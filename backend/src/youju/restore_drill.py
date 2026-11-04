"""在独立临时库中演练备份恢复。"""

import gzip
import json
import logging
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, text
from sqlalchemy.exc import SQLAlchemyError

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


def _database_name() -> str:
    return f"youju_drill_{datetime.now(BEIJING):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"


def run(database_url: str, cloud_env: str | None, source: Path | None) -> int:
    server = None
    target = None
    created = False
    deleted = False
    failure = None
    name = _database_name()
    try:
        url = make_url(database_url)
        if url.get_backend_name() != "mysql" or not url.database:
            raise BackupError("只接受指定了当前库名的 MySQL 连接串")
        if not re.fullmatch(r"youju_drill_\d{14}_[0-9a-f]{4}", name) or (
            name.lower() == url.database.lower()
        ):
            raise BackupError("临时库名不安全或与当前库相同")
        if source is None and not cloud_env:
            raise BackupError("没有设置 YOUJU_CLOUD_ENV，无法下载备份")
        with tempfile.TemporaryDirectory() as directory:
            start = time.monotonic()
            if source is None:
                source = Path(directory) / "backup.jsonl.gz"
                with httpx.Client(trust_env=False) as client:
                    filename = Path(download_latest(client, cloud_env, source)).name
            else:
                filename = source.name
            download_seconds = time.monotonic() - start
            server_url = url.set(database=None).render_as_string(hide_password=False)
            server = create_mysql_engine(server_url)
            with server.begin() as conn:
                conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
                created = True
            with gzip.open(source, "rb") as src:
                header = json.loads(src.readline())
            if (
                not isinstance(header, dict)
                or header.get("format") != FORMAT
                or (header.get("version") != VERSION)
            ):
                raise BackupError("备份说明行格式不对")
            revision = header.get("revision")
            if not isinstance(revision, str) or not revision:
                raise BackupError("备份说明行没有迁移版本")
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
            target_url = url.set(database=name).render_as_string(hide_password=False)
            cfg = alembic_config(target_url)
            cfg.config_file_name = None
            script_revision = ScriptDirectory.from_config(cfg).get_revision(revision)
            if script_revision is None or script_revision.revision != revision:
                raise BackupError("备份的迁移版本不在当前代码中")
            start = time.monotonic()
            migration_log = logging.getLogger("alembic")
            previous_level = migration_log.level
            try:
                migration_log.setLevel(logging.WARNING)
                command.upgrade(cfg, revision)
            finally:
                migration_log.setLevel(previous_level)
            target = create_mysql_engine(target_url)
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
            print(f"备份文件：{filename}")
            if created_at is not None:
                print(f"备份时间：{created_at}")
            print(f"迁移版本：{revision}；总条数：{sum(counts.values())}")
            for table, count in counts.items():
                print(f"  {table}: {count}")
            print(
                f"下载 {download_seconds:.1f} 秒；迁移 {migration_seconds:.1f} 秒；"
                f"恢复 {restore_seconds:.1f} 秒"
            )
    except Exception as exc:
        if isinstance(exc, BackupError):
            failure = str(exc).splitlines()[0]
        elif isinstance(exc, SQLAlchemyError):
            original = getattr(exc, "orig", None)
            code = original.args[0] if original and original.args else None
            failure = f"数据库操作失败（{type(exc).__name__}，错误码 {code}）"
        else:
            failure = f"演练操作失败（{type(exc).__name__}）"
    finally:
        if target is not None:
            target.dispose()
        if created:
            try:
                with server.begin() as conn:
                    conn.execute(text(f"DROP DATABASE `{name}`"))
                deleted = True
            except Exception:
                print(f"临时库 {name} 没删掉，请手动删除")
                failure = failure or "清理临时库失败"
        if server is not None:
            server.dispose()
    if deleted:
        print(f"临时库 {name} 已删除")
    if failure:
        print(f"恢复演练失败：{failure}")
        return 1
    print("恢复演练成功")
    return 0
