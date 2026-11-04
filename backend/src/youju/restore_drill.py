"""备份恢复演练（D-118 免“连续稳定 14 天”的条件之一）：在同一个 MySQL 上建临时库，
恢复最新备份、核对，最后删掉临时库。

线上由创始人在 admin 容器终端里跑 `youju restore-drill`。整个过程不连正式库：建库、删库用不带
库名的连接，迁移和恢复用临时库自己的连接串（alembic 的 env.py 只在没给连接串时才读设置）。
admin 账号要有 `youju\\_drill\\_%` 这一类库的权限（backend/deploy/mysql_accounts.sql 第 ③ 节，
root 执行一次）。临时库里是整库数据的副本：终端断开、实例停止、按 Ctrl+C 都照常删掉；
万一没删掉，下一次演练开头会先删（独立审阅 #440）。
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import re
import secrets
import signal
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from alembic import command
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from youju.backup import (
    BEIJING,
    FORMAT,
    VERSION,
    BackupError,
    backed_up_tables,
    download_latest,
    restore,
)
from youju.db import create_mysql_engine
from youju.migrations import alembic_config

PREFIX = "youju_drill_"
NAME = re.compile(r"youju_drill_(\d{14})_[0-9a-f]{4}")
# 建库时间（库名里的北京时间）早于这么久的同类库，算上次没删掉的；
# 更新的可能是另一个正在跑的演练，不动它
STALE_AFTER = timedelta(hours=1)
GRANT_HINT = (
    "数据库账号没有建临时库的权限：请在云托管 MySQL 里用 root 执行 "
    "backend/deploy/mysql_accounts.sql 第 ③ 节那一句授权，再重跑"
)
# 这几种信号（终端断开、实例停止、Ctrl+C）都转成异常，照常走删库
SIGNALS = tuple(
    sig for sig in (getattr(signal, n, None) for n in ("SIGINT", "SIGTERM", "SIGHUP")) if sig
)


class _Stopped(BaseException):
    """演练被中断：不是 Exception 的子类，免得被当成普通错误吞掉。"""


def _database_name() -> str:
    return f"{PREFIX}{datetime.now(BEIJING):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"


def _say(message: str) -> None:
    # 终端断开后再写屏幕会出错：删库已经做完了，写不出来就算了
    with contextlib.suppress(OSError):
        print(message, flush=True)


def _code(exc: BaseException) -> int | None:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", None)
    return args[0] if args else None


def _read_header(source: Path) -> dict:
    try:
        with gzip.open(source, "rb") as src:
            header = json.loads(src.readline() or b"null")
    except (OSError, ValueError, EOFError):
        raise BackupError("备份文件读不出来，或说明行格式不对") from None
    if (
        not isinstance(header, dict)
        or header.get("format") != FORMAT
        or header.get("version") != VERSION
    ):
        raise BackupError("备份说明行格式不对")
    return header


def _drill_databases(server) -> list[str]:
    with server.connect() as conn:
        rows = conn.execute(text("SHOW DATABASES LIKE :pattern"), {"pattern": "youju\\_drill\\_%"})
        return [row[0] for row in rows if NAME.fullmatch(row[0])]


def _made_at(name: str) -> datetime:
    stamp = NAME.fullmatch(name).group(1)
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=BEIJING)


def _install_handlers() -> dict:
    def stop(signum, frame):
        raise _Stopped(signum)

    previous = {}
    for sig in SIGNALS:
        # 不在主线程时装不了，照常跑
        with contextlib.suppress(ValueError, OSError):
            previous[sig] = signal.signal(sig, stop)
    return previous


def _set_handlers(handlers: dict) -> None:
    for sig, handler in handlers.items():
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def run(database_url: str, cloud_env: str | None, source: Path | None) -> int:
    previous = _install_handlers()
    server = None
    target = None
    name = None
    created = False
    deleted = False
    failure = None
    lines: list[str] = []
    try:
        url = make_url(database_url)
        if url.get_backend_name() != "mysql" or not url.database:
            raise BackupError("只接受指定了当前库名的 MySQL 连接串")
        name = _database_name()
        if not NAME.fullmatch(name) or name.lower() == url.database.lower():
            raise BackupError("临时库名不安全或与当前库相同")
        if source is None and not cloud_env:
            raise BackupError("没有设置 YOUJU_CLOUD_ENV，无法下载备份")
        with tempfile.TemporaryDirectory() as directory:
            start = time.monotonic()
            if source is None:
                local = Path(directory) / "backup.jsonl.gz"
                with httpx.Client(trust_env=False) as client:
                    filename = Path(download_latest(client, cloud_env, local)).name
                source = local
            else:
                if not source.is_file():
                    raise BackupError(f"找不到备份文件 {source}")
                filename = source.name
            download_seconds = time.monotonic() - start
            # 建库之前先看说明行和迁移版本：不对就不建库
            header = _read_header(source)
            revision = header.get("revision")
            if not isinstance(revision, str) or not revision:
                raise BackupError("备份说明行没有迁移版本")
            script = ScriptDirectory.from_config(alembic_config(database_url))
            try:
                known = script.get_revision(revision) is not None
            except CommandError:
                known = False
            if not known:
                raise BackupError(f"备份的迁移版本 {revision} 不在现在的代码里")
            head = script.get_current_head()
            if revision != head:
                # 恢复按现在代码里的表核对：版本不同时会报“表不存在”“表结构不一致”，看起来像备份坏了
                raise BackupError(
                    f"备份的迁移版本是 {revision}，现在代码的最新版本是 {head}："
                    "部署了新迁移以后，要等下一份备份（每天凌晨自动备份）出来再演练"
                )
            created_at = header.get("created_at")
            if created_at is not None:
                # 给创始人看的：换成北京时间，不带微秒和时区写法
                try:
                    created_at = (
                        datetime.fromisoformat(created_at)
                        .astimezone(BEIJING)
                        .strftime("%Y-%m-%d %H:%M:%S（北京时间）")
                    )
                except (TypeError, ValueError):
                    raise BackupError("备份说明行的时间格式不对") from None
            # 建库、删库用不带库名的连接。注意 url.set(database=None) 不会去掉库名
            # （None 当作“不改”），要重新拼一个（受限账号测试查出：只有演练库权限的账号
            # 连当前库会报 1044）
            server_url = URL.create(
                url.drivername,
                username=url.username,
                password=url.password,
                host=url.host,
                port=url.port,
                query=url.query,
            ).render_as_string(hide_password=False)
            server = create_mysql_engine(server_url)
            now = datetime.now(BEIJING)
            for old in _drill_databases(server):
                if now - _made_at(old) > STALE_AFTER:
                    _say(f"发现上次没删掉的临时库 {old}，先删掉")
                    with server.begin() as conn:
                        conn.execute(text(f"DROP DATABASE `{old}`"))
                else:
                    _say(f"有一个 1 小时内建的临时库 {old}，可能另一个演练正在跑，没动它")
            try:
                with server.begin() as conn:
                    charset = "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
                    conn.execute(text(f"CREATE DATABASE `{name}` {charset}"))
            except DBAPIError as exc:
                if _code(exc) in (1044, 1227):
                    raise BackupError(GRANT_HINT) from None
                raise
            created = True
            _say(f"已建临时库 {name}（演练结束会删掉）")
            target_url = url.set(database=name).render_as_string(hide_password=False)
            cfg = alembic_config(target_url)
            cfg.config_file_name = None
            start = time.monotonic()
            migration_log = logging.getLogger("alembic")
            previous_level = migration_log.level
            try:
                migration_log.setLevel(logging.WARNING)
                command.upgrade(cfg, revision)
            finally:
                migration_log.setLevel(previous_level)
            target = create_mysql_engine(target_url)
            # 迁移会写入种子行（如 signing_keys）；恢复要求空库，只清临时库里的
            with target.begin() as conn:
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                try:
                    for table in backed_up_tables():
                        conn.execute(table.delete())
                finally:
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            migration_seconds = time.monotonic() - start
            start = time.monotonic()
            with source.open("rb") as src:
                counts = restore(target, src)
            restore_seconds = time.monotonic() - start
            lines.append(f"备份文件：{filename}")
            if created_at is not None:
                lines.append(f"备份时间：{created_at}")
            lines.append(f"迁移版本：{revision}；总条数：{sum(counts.values())}")
            lines.extend(f"  {table}: {count}" for table, count in counts.items())
            lines.append(
                f"下载 {download_seconds:.1f} 秒；迁移 {migration_seconds:.1f} 秒；"
                f"恢复 {restore_seconds:.1f} 秒"
            )
    except _Stopped:
        failure = "演练被中断（终端断开、实例停止或按了 Ctrl+C）"
    except Exception as exc:
        if isinstance(exc, BackupError):
            failure = str(exc).splitlines()[0]
        elif isinstance(exc, SQLAlchemyError):
            failure = f"数据库操作失败（{type(exc).__name__}，错误码 {_code(exc)}）"
        else:
            failure = f"演练操作失败（{type(exc).__name__}）"
    finally:
        # 删库期间不再响应中断：先删库，再写屏幕
        _set_handlers({sig: signal.SIG_IGN for sig in previous})
        try:
            if target is not None:
                target.dispose()
            if created:
                try:
                    with server.begin() as conn:
                        conn.execute(text(f"DROP DATABASE `{name}`"))
                    deleted = True
                except Exception:
                    failure = failure or "清理临时库失败"
            if server is not None:
                server.dispose()
        finally:
            _set_handlers(previous)
    for line in lines:
        _say(line)
    if created and deleted:
        _say(f"临时库 {name} 已删除")
    elif created:
        _say(f"临时库 {name} 没删掉，请手动删除（下一次演练开头也会先删）")
    if failure:
        _say(f"恢复演练失败：{failure}")
        return 1
    _say("恢复演练成功")
    return 0
