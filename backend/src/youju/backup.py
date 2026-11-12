"""数据库备份（验收 A6）：每天把整库导出成一个压缩文件，存到云托管对象存储，保留 30 天；
出事时用 `youju restore` 恢复到迁移好的空库。

- 一致快照：所有表在同一个"可重复读"事务里读，导出的是同一时刻的数据。
- 格式：gzip 压缩的 JSON Lines。第一行是说明（格式版本、迁移版本、导出时间、各表条数），之后每行
  一条记录 {"t": 表名, "r": 字段}，最后一行是结尾（各表内容的 SHA-256）。没有结尾说明文件不完整。
  和数据库品牌、版本无关，解压就能看。
- 恢复：锁住全部表后再查"同一迁移版本、空库"，逐行校验字段，写完在同一个事务里核对条数、外键和
  各表内容的 SHA-256，全部对上才提交；任何一步不对就回滚，目标库保持原样。
- 存放：云托管对象存储 backups/youju-日期.jsonl.gz（北京时间的日期，同一天重跑就覆盖）。
  通过云托管"开放接口服务"上传、删除，不需要 access_token（官方建议容器内用 http 调用；
  文件本身按返回的 https 地址上传）；每次上传后删掉 31–40 天前的，某天没删掉的后面 9 天还会再删。
- 备份里有用户数据（openid、关注、纠错），和数据库同等对待：存储桶权限设为
  "所有用户不可读（只有管理员可读写）"，只由管理员在云托管控制台下载。
- 不备份访问日志暂存表（access_logs）：它每天归档到对象存储，归档另存 190 天
  （youju/access_log.py）；恢复也不动它。
- 校验：线上不能临时建库做恢复演练，改为每天 4:40 `youju backup-verify` 下载最新一份，只读核对
  格式、每行字段、各表条数、SHA-256 和迁移版本（版本不同就恢复不了），不写数据库。
- 2.0（D-129 ⑥，创始人 11/12"能接受"）：服务器（YOUJU_PUBLIC_REPLICA=true，公开数据以本机
  同步来的为准）只备用户数据：不备同步的表，两边都会新建行的表只备服务器自己的行；
  说明行写 "scope": "user"。
  服务器坏了：新服务器迁移好、本机全推一遍公开数据，再恢复这份（指向的公开行在本机那边已经删了的，
  照外键本来的规则处理，见 _settle_orphans）。本机（公开数据的主库，没有用户数据）留最近 3 天和最近
  4 个周日的（keep_policy）；云托管、服务器照旧 30 天。云托管的备份文件一个字节都不变。
"""

import gzip
import hashlib
import json
import logging
import tempfile
import time as clock
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import IO, Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Connection, Engine, Table, delete, exists, func, select, text, update
from sqlalchemy import types as sqltypes
from sqlalchemy.exc import SQLAlchemyError

from youju import storage
from youju.dbtypes import UTCDateTime
from youju.models import Base

FORMAT = "youju-backup"
VERSION = 1
KEEP_DAYS = 30
# 每次删掉这些天之前的备份（多删几天，漏跑或某天没删掉的也能清掉）
_PRUNE_DAYS = range(KEEP_DAYS + 1, KEEP_DAYS + 11)
_BATCH = 1000
# 上传遇到网络中断、服务繁忙时重试的等待秒数
_RETRY_WAITS = (5, 15)
BEIJING = ZoneInfo("Asia/Shanghai")
log = logging.getLogger("youju.backup")
# 云托管开放接口服务：容器内用 http 调用，免 access_token（需在"云调用-微信令牌权限配置"里登记接口）
OPEN_API = "http://api.weixin.qq.com"
# 开放接口返回这些错误码时是平台暂时的问题，可以重试
_BUSY_CODES = {-1, -1000, -501002}


# 不备份、不恢复的表：访问日志暂存表每天归档到对象存储，归档自己保存（见 youju/access_log.py）；
# 给 AI 用的附件文字（youju/attachment_text.py）随时能从原网站重新下载取出，原网站删了的本来就要
# 撤下，恢复旧文字反而不对：恢复后由附件定时任务重新取
NOT_BACKED_UP = frozenset(
    {"access_logs", "attachment_text_segments", "attachment_text_urls", "attachment_text_versions"}
)


# 备份范围：all 是上面几张表以外的全部表（云托管、2.0 本机，和以前一样）；
# user 只备用户数据（2.0 服务器）
SCOPE_ALL = "all"
SCOPE_USER = "user"
# 留几份：30 天（有用户数据的：隐私保护指引写的是备份保留 30 天）；本机最近 3 天加最近 4 个周日
KEEP_30_DAYS = "30d"
KEEP_3_DAYS_4_WEEKS = "3d4w"


def backup_scope(settings) -> str:
    """公开数据是本机同步来的（2.0 服务器）只备用户数据；其余照旧整库。"""
    return SCOPE_USER if getattr(settings, "public_replica", False) else SCOPE_ALL


def keep_policy(settings) -> str:
    """2.0 本机（自建、公开数据的主库，没有用户数据）留最近 3 天和最近 4 个周日的（D-129，11/12）；
    云托管、服务器照旧 30 天。两种都不超过 30 天（周日那份最多留 27 天）。"""
    if getattr(settings, "hosting", "cloudrun") == "self" and not getattr(
        settings, "public_replica", False
    ):
        return KEEP_3_DAYS_4_WEEKS
    return KEEP_30_DAYS


def _sync_tables():
    # youju/sync/tables.py 也从这里导入（_decoder），放在函数里导入，免得互相导入
    from youju.sync import tables as sync_tables

    return sync_tables.SYNC_TABLES, sync_tables.SERVER_OWNED


def backed_up_tables(scope: str = SCOPE_ALL) -> list[Table]:
    tables = [t for t in Base.metadata.sorted_tables if t.name not in NOT_BACKED_UP]
    if scope == SCOPE_ALL:
        return tables
    if scope != SCOPE_USER:
        raise BackupError(f"不认识的备份范围：{scope}")
    synced, owned = _sync_tables()
    return [t for t in tables if t.name not in synced or t.name in owned]


def own_rows(table: Table, scope: str):
    """user 范围里两边都会新建行的表只备服务器自己的行：返回筛选条件；其余返回 None（整表）。
    SERVER_OWNED 存的是"不是服务器自己的行"的条件（本机推来的那部分），取反就是服务器自己的行。"""
    if scope != SCOPE_USER:
        return None
    _, owned = _sync_tables()
    condition = owned.get(table.name)
    return None if condition is None else ~condition(table)


def _count(table: Table, where=None):
    stmt = select(func.count()).select_from(table)
    return stmt if where is None else stmt.where(where)


def scope_of(header: dict) -> str:
    """备份文件的范围（以前的文件没有这一项，是整库）。"""
    scope = header.get("scope", SCOPE_ALL)
    if scope not in (SCOPE_ALL, SCOPE_USER):
        raise BackupError("备份文件的说明行格式不对：不认识的备份范围")
    return scope


class BackupError(RuntimeError):
    """备份或恢复失败；信息里不含数据内容和上传凭证。retryable 表示稍后重试可能成功。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class BackupReport:
    counts: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    path: str = ""
    file_id: str = ""
    pruned: int = 0


def object_path(day: date) -> str:
    return f"backups/youju-{day:%Y%m%d}.jsonl.gz"


# 下载要用完整 file_id（cloud://环境.存储桶/路径），前缀只能从上传接口的返回里取。用这个专门的路径
# 申请上传链接、只取前缀，不向返回的上传地址发数据，也不拿真实备份的路径去申请。
PROBE_PATH = "backups/prefix-probe"


def _encode(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError(f"不支持的类型：{type(value).__name__}")


def _line(table: str, row: dict) -> bytes:
    """一条记录的写法；导出和恢复后核对用同一种写法算 SHA-256。"""
    item = {"t": table, "r": row}
    return (json.dumps(item, ensure_ascii=False, default=_encode) + "\n").encode()


def _rows(conn: Connection, table: Table, where=None) -> Iterator[dict]:
    stmt = select(table) if where is None else select(table).where(where)
    result = conn.execution_options(stream_results=True, yield_per=_BATCH).execute(
        stmt.order_by(*table.primary_key.columns)
    )
    for row in result.mappings():
        yield dict(row)


def _revision(conn: Connection) -> str | None:
    return conn.scalar(text("SELECT version_num FROM alembic_version"))


def dump(
    engine: Engine, out: IO[bytes], *, now: datetime | None = None, scope: str = SCOPE_ALL
) -> dict[str, int]:
    """把全部表（访问日志暂存表除外；scope=user 时只有用户数据）按主键顺序导出到 out
    （gzip JSON Lines），返回各表条数。"""
    tables = backed_up_tables(scope)
    only = {t.name: own_rows(t, scope) for t in tables}
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        # 先数条数：可重复读下，后面读到的和这里数的是同一时刻的数据
        counts = {t.name: conn.scalar(_count(t, only[t.name])) or 0 for t in tables}
        header = {
            "format": FORMAT,
            "version": VERSION,
            "revision": _revision(conn),
            "created_at": (now or datetime.now(UTC)).isoformat(),
            "tables": counts,
        }
        if scope != SCOPE_ALL:
            # 整库的说明行和以前一模一样；只备用户数据的写明范围、哪几张表只有服务器自己的行
            header["scope"] = scope
            header["own_rows_only"] = sorted(n for n, w in only.items() if w is not None)
        digests: dict[str, str] = {}
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write((json.dumps(header, ensure_ascii=False) + "\n").encode())
            for table in tables:
                digest = hashlib.sha256()
                for row in _rows(conn, table, only[table.name]):
                    line = _line(table.name, row)
                    digest.update(line)
                    gz.write(line)
                digests[table.name] = digest.hexdigest()
            gz.write((json.dumps({"end": True, "sha256": digests}) + "\n").encode())
    return counts


def _decoder(column) -> Callable[[Any], Any] | None:
    kind = column.type
    if isinstance(kind, (UTCDateTime, sqltypes.DateTime)):
        return datetime.fromisoformat
    if isinstance(kind, sqltypes.Date):
        return date.fromisoformat
    if isinstance(kind, sqltypes.Time):
        return time.fromisoformat
    return None


def _orphans(
    conn: Connection, tables: dict[str, Table], *, skip: frozenset[str] = frozenset()
) -> list[str]:
    """外键指向不存在的记录的：子表.字段。skip 里的父表不查（演练只备用户数据的备份时，
    临时库里的公开表是空的）。"""
    found = []
    for table in tables.values():
        for fk in table.foreign_keys:
            child, parent = fk.parent, fk.column
            parent_table = parent.table
            if parent_table.name in skip:
                continue
            if parent_table is table:
                # 自关联外键（以前 users 的引荐关系就是，迁移 0035 已删，现在没有）：
                # 父端须单独别名，并在恢复时一并声明表锁。
                parent_table = table.alias(f"{table.name}_{child.name}_parent")
                parent = parent_table.c[parent.name]
            joined = table.outerjoin(parent_table, child == parent)
            count = conn.scalar(
                select(func.count()).select_from(joined).where(child.is_not(None), parent.is_(None))
            )
            if count:
                found.append(f"{table.name}.{child.name}")
    return found


def _settle_orphans(conn: Connection, tables: dict[str, Table]) -> dict[str, tuple[str, int]]:
    """只备用户数据的备份恢复到新服务器（公开数据已经从本机推过来）以后：用户数据指着的公开行
    （通知、转发组、截止日期……）在本机那边已经删了的，照外键本来的规则处理——级联删除的这一行
    不要、置空的置空、不许删上级的报错停下。和线上本来会发生的一样：同步删掉公开行时，数据库就是
    这样处理指着它的用户数据。返回 {子表.字段: (CASCADE 或 SET NULL, 行数)}。"""
    done: dict[str, tuple[str, int]] = {}
    # 级联会一层层传下去（删掉的用户行又有别的用户行指着它），每轮从头再查，最多 10 轮
    for _ in range(10):
        changed = 0
        for table in tables.values():
            for fk in table.foreign_keys:
                child, parent = fk.parent, fk.column
                if parent.table is table:
                    continue  # 自关联的现在没有；有的话由 _orphans 查出来报错
                missing = child.is_not(None) & ~exists(
                    select(1).select_from(parent.table).where(parent == child)
                )
                n = conn.scalar(_count(table, missing)) or 0
                if not n:
                    continue
                key = f"{table.name}.{child.name}"
                action = (fk.ondelete or "").upper()
                if action == "CASCADE":
                    conn.execute(delete(table).where(missing))
                elif action == "SET NULL":
                    conn.execute(update(table).where(missing).values({child.name: None}))
                else:
                    raise BackupError(
                        f"{key} 有 {n} 行指着的记录已经不在了（这个外键不许删上级），没有恢复"
                    )
                old = done.get(key, (action, 0))[1]
                done[key] = (action, old + n)
                changed += n
        if not changed:
            return done
    raise BackupError("处理指着已删记录的行转了 10 轮还没完，没有恢复")


def _load(
    conn: Connection,
    gz: IO[bytes],
    header: dict,
    tables: dict[str, Table],
    *,
    only: dict | None = None,
    check_raw: bool = False,
) -> dict:
    """锁表后检查版本和空库、逐行写入，返回文件结尾（各表 SHA-256）。出错抛异常，由调用方回滚。"""
    if _revision(conn) != header.get("revision"):
        raise BackupError(
            f"目标库的迁移版本是 {_revision(conn)}，备份是 {header.get('revision')}："
            "先用同一版本的代码把目标库迁移好"
        )
    if set(header["tables"]) != set(tables):
        raise BackupError("备份文件的说明行格式不对：表名和这个版本的表对不上")
    only = only or {}
    # 只备服务器自己的行的表（截止日期），看的是"服务器自己的行"有没有，公开的行本来就有
    busy = [n for n, t in tables.items() if conn.scalar(_count(t, only.get(n)))]
    if busy:
        raise BackupError(f"目标库不是空的：{', '.join(busy)}")
    raw = {name: hashlib.sha256() for name in tables} if check_raw else None
    columns = {name: {c.name for c in t.columns} for name, t in tables.items()}
    decoders = {
        name: {c.name: d for c in t.columns if (d := _decoder(c)) is not None}
        for name, t in tables.items()
    }
    batch: list[dict] = []
    current: str | None = None
    trailer: dict | None = None
    for number, raw_line in enumerate(gz, start=2):
        item = json.loads(raw_line)
        if trailer is not None:
            raise BackupError("备份文件在结尾之后还有内容")
        if not isinstance(item, dict):
            raise BackupError(f"第 {number} 行不是一条记录")
        if item.get("end") is True:
            trailer = item
            continue
        name, row = item.get("t"), item.get("r")
        if name not in tables or not isinstance(row, dict) or set(row) != columns[name]:
            raise BackupError(f"第 {number} 行的表名或字段和这个版本的表结构不一致")
        if raw is not None:
            raw[name].update(raw_line)
        if name != current or len(batch) >= _BATCH:
            if batch:
                conn.execute(tables[current].insert(), batch)
                batch = []
            current = name
        for column, decode in decoders[name].items():
            if row[column] is not None:
                row[column] = decode(row[column])
        batch.append(row)
    if batch:
        conn.execute(tables[current].insert(), batch)
    if trailer is None or not isinstance(trailer.get("sha256"), dict):
        raise BackupError("备份文件不完整：没有结尾")
    if raw is not None:
        # 文件本身对不对：恢复后有的表会按外键规则调整（_settle_orphans），那几张表核对不了
        # 库里的内容，所以先核对文件里的每一行
        changed = [n for n, d in raw.items() if d.hexdigest() != trailer["sha256"].get(n)]
        if changed:
            raise BackupError(f"备份文件内容和结尾 SHA-256 不一致：{', '.join(changed)}")
    return trailer


def _verify(
    conn: Connection,
    header: dict,
    trailer: dict,
    tables: dict[str, Table],
    *,
    only: dict | None = None,
    skip_parents: frozenset[str] = frozenset(),
    settled: dict[str, tuple[str, int]] | None = None,
) -> dict:
    """写完、提交前核对：各表条数、外键、各表内容的 SHA-256 都要和备份一致。按外键规则调整过的表
    （settled）：条数要等于备份里的减去级联删掉的，内容不比（文件本身已在 _load 里核对过）。"""
    only = only or {}
    settled = settled or {}
    removed: dict[str, int] = {}
    for key, (action, n) in settled.items():
        if action == "CASCADE":
            removed[key.split(".")[0]] = removed.get(key.split(".")[0], 0) + n
    adjusted = {key.split(".")[0] for key in settled}
    counts = {n: conn.scalar(_count(t, only.get(n))) or 0 for n, t in tables.items()}
    expected = header.get("tables", {})
    wrong = [n for n in tables if counts[n] != expected.get(n, 0) - removed.get(n, 0)]
    if wrong:
        raise BackupError(f"写入后条数和备份说明对不上：{', '.join(wrong)}")
    orphans = _orphans(conn, tables, skip=skip_parents)
    if orphans:
        raise BackupError(f"写入后外键对不上：{', '.join(orphans)}")
    digests = trailer["sha256"]
    changed = []
    for name, table in tables.items():
        if name in adjusted:
            continue
        digest = hashlib.sha256()
        for row in _rows(conn, table, only.get(name)):
            digest.update(_line(name, row))
        if digest.hexdigest() != digests.get(name):
            changed.append(name)
    if changed:
        raise BackupError(f"写入后内容和备份不一致：{', '.join(changed)}")
    return counts


def restore(
    engine: Engine,
    src: IO[bytes],
    *,
    drill: bool = False,
    report: dict | None = None,
) -> dict[str, int]:
    """把备份恢复到 engine 指向的库：这个库要已迁移到和备份相同的版本、而且是空的（只备用户数据的：
    用户数据各表是空的，公开数据已经从本机推过来）。锁住要写的表后检查、写入、核对，全部对上才提交；
    否则回滚并报错。返回各表条数。

    只备用户数据的备份（scope=user）：
    - 恢复演练（drill=True，临时库里只有迁移建的空表）：指向公开表的外键不查，其余照常核对；
    - 真恢复：公开数据要先在（通知表不能是空的）；指着的公开行已经不在的，照外键规则处理
      （_settle_orphans），处理了什么写进 report["settled"]。"""
    try:
        with gzip.GzipFile(fileobj=src, mode="rb") as gz:
            header = json.loads(gz.readline() or b"null")
            if (
                not isinstance(header, dict)
                or header.get("format") != FORMAT
                or header.get("version") != VERSION
            ):
                raise BackupError("不是有据的备份文件，或格式版本不认识")
            # 各表条数：表名是字符串，条数是非负整数（布尔值不算）；表名和这个版本对不对应，
            # 核对过迁移版本后再查，版本不同时先说版本
            counts_ok = isinstance(header.get("tables"), dict) and all(
                isinstance(k, str) and type(v) is int and v >= 0
                for k, v in header["tables"].items()
            )
            if not counts_ok or not isinstance(header.get("revision"), (str, type(None))):
                raise BackupError("备份文件的说明行格式不对")
            scope = scope_of(header)
            tables = {t.name: t for t in backed_up_tables(scope)}
            only = {name: own_rows(t, scope) for name, t in tables.items()}
            # 备份以外、被外键指着的表（只备用户数据时：通知、转发组这些公开表）只读锁住
            outside = sorted(
                {
                    fk.column.table.name
                    for table in tables.values()
                    for fk in table.foreign_keys
                    if fk.column.table.name not in tables
                }
            )
            parent_locks = [
                f"`{table.name}` AS `{table.name}_{fk.parent.name}_parent` READ"
                for table in tables.values()
                for fk in table.foreign_keys
                if fk.column.table is table
            ]
            locks = ", ".join(
                [f"`{name}` WRITE" for name in tables]
                + [f"`{name}` READ" for name in outside]
                + parent_locks
                + ["`alembic_version` READ"]
            )
            user = scope == SCOPE_USER
            if user and not drill and "documents" not in outside:
                raise BackupError("这个版本的用户数据表没有指着通知表，恢复前查不了公开数据在不在")
            with engine.connect() as conn:
                # 锁住要写的表：检查空库到写完提交之间，别的程序（如定时任务、同步）写不进来
                conn.execute(text(f"LOCK TABLES {locks}"))
                try:
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                    if user and not drill:
                        documents = Base.metadata.tables["documents"]
                        if not conn.scalar(_count(documents)):
                            raise BackupError(
                                "这个库里还没有公开数据：先在本机 youju sync-push --full 推一遍，"
                                "再恢复用户数据（不然指着通知的用户数据会被当成已删的处理掉）"
                            )
                    trailer = _load(conn, gz, header, tables, only=only, check_raw=user)
                    settled = _settle_orphans(conn, tables) if user and not drill else {}
                    skip = frozenset(outside) | frozenset(n for n, w in only.items() if w)
                    counts = _verify(
                        conn,
                        header,
                        trailer,
                        tables,
                        only=only,
                        skip_parents=skip if user and drill else frozenset(),
                        settled=settled,
                    )
                    conn.commit()
                    if report is not None:
                        report["scope"] = scope
                        report["settled"] = settled
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    # 会话级设置一定改回来，免得这个连接回到连接池后一直不查外键
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
                    conn.execute(text("UNLOCK TABLES"))
                    conn.commit()
    except BackupError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, zlib.error) as exc:
        raise BackupError("备份文件损坏或不完整") from exc
    except SQLAlchemyError as exc:
        raise BackupError(f"写入目标库失败：{exc.__class__.__name__}") from exc
    return counts


def verify(engine: Engine, src: IO[bytes]) -> dict[str, int]:
    """只读校验完整备份及当前库的迁移版本，不读取或修改业务表。只备用户数据的备份按它自己的范围核对。"""
    try:
        with gzip.GzipFile(fileobj=src, mode="rb") as gz:
            header = json.loads(gz.readline() or b"null")
            if (
                not isinstance(header, dict)
                or header.get("format") != FORMAT
                or type(header.get("version")) is not int
                or header["version"] != VERSION
            ):
                raise BackupError("不是有据的备份文件，或格式版本不认识")
            tables = {t.name: t for t in backed_up_tables(scope_of(header))}
            counts = dict.fromkeys(tables, 0)
            digests = {name: hashlib.sha256() for name in tables}
            expected = header.get("tables")
            if (
                not isinstance(expected, dict)
                or set(expected) != set(tables)
                or any(type(n) is not int or n < 0 for n in expected.values())
                or not isinstance(header.get("revision"), (str, type(None)))
            ):
                raise BackupError("备份文件的说明行格式不对")
            with engine.connect() as conn:
                revision = _revision(conn)
            if revision != header["revision"]:
                raise BackupError(
                    f"当前库的迁移版本是 {revision}，备份是 {header['revision']}：无法恢复"
                )
            columns = {name: {c.name for c in table.columns} for name, table in tables.items()}
            trailer = None
            for number, raw in enumerate(gz, start=2):
                if trailer is not None:
                    raise BackupError("备份文件在结尾之后还有内容")
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise BackupError(f"第 {number} 行不是一条记录")
                if item.get("end") is True:
                    trailer = item
                    continue
                name, row = item.get("t"), item.get("r")
                if name not in tables or not isinstance(row, dict) or set(row) != columns[name]:
                    raise BackupError(f"第 {number} 行的表名或字段和这个版本的表结构不一致")
                counts[name] += 1
                digests[name].update(raw)
            if trailer is None:
                raise BackupError("备份文件不完整：没有结尾")
            checksums = trailer.get("sha256")
            if not isinstance(checksums, dict) or set(checksums) != set(tables):
                raise BackupError("备份文件的结尾校验信息不完整")
            wrong = [name for name in tables if counts[name] != expected[name]]
            if wrong:
                raise BackupError(f"实际条数和备份说明对不上：{', '.join(wrong)}")
            changed = [name for name in tables if digests[name].hexdigest() != checksums[name]]
            if changed:
                raise BackupError(f"文件内容和结尾 SHA-256 不一致：{', '.join(changed)}")
    except BackupError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, zlib.error) as exc:
        raise BackupError("备份文件损坏或不完整") from exc
    except SQLAlchemyError as exc:
        raise BackupError(f"读取当前库迁移版本失败：{exc.__class__.__name__}") from exc
    return counts


def _open_api(client: httpx.Client, api: str, payload: dict) -> dict:
    if storage.emulated():
        # 2.0 自建服务器：存储接口在本机目录或 S3 上照原格式做（youju/storage.py）
        try:
            data = storage.open_api(api, payload)
        except storage.StorageError as exc:
            raise BackupError(f"调用 {api} 失败：{exc}", retryable=True) from None
        if data.get("errcode") != 0:
            raise BackupError(f"调用 {api} 失败（errcode {data.get('errcode')}）")
        return data
    try:
        res = client.post(f"{OPEN_API}{api}", json=payload, timeout=20)
    except httpx.TransportError as exc:
        raise BackupError(f"调用 {api} 网络失败", retryable=True) from exc
    if res.status_code >= 500:
        raise BackupError(f"调用 {api} 失败（HTTP {res.status_code}）", retryable=True)
    try:
        res.raise_for_status()
        data = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BackupError(f"调用 {api} 失败：{exc.__class__.__name__}") from exc
    if not isinstance(data, dict) or data.get("errcode") != 0:
        code = data.get("errcode") if isinstance(data, dict) else "?"
        raise BackupError(f"调用 {api} 失败（errcode {code}）", retryable=code in _BUSY_CODES)
    return data


def upload(client: httpx.Client, env: str, path: str, local: Path, *, what: str = "备份") -> str:
    """上传到云托管对象存储，返回文件 ID（cloud://…）。what 是出错信息里怎么称呼这个文件。"""
    if storage.emulated():
        # 2.0 自建服务器：直接写本机目录或 S3（youju/storage.py）
        try:
            return storage.upload(env, path, local)
        except (storage.StorageError, OSError) as exc:
            raise BackupError(f"上传{what}失败：{exc.__class__.__name__}", retryable=True) from None
    link = _open_api(client, "/tcb/uploadfile", {"env": env, "path": path})
    needed = ("url", "authorization", "token", "cos_file_id", "file_id")
    missing = [k for k in needed if not link.get(k)]
    if missing:
        raise BackupError(f"上传链接缺少字段：{', '.join(missing)}")
    fields = {
        "key": path,
        "Signature": link["authorization"],
        "x-cos-security-token": link["token"],
        "x-cos-meta-fileid": link["cos_file_id"],
    }
    try:
        with local.open("rb") as fh:
            res = client.post(
                link["url"],
                data=fields,
                files={"file": (Path(path).name, fh, "application/gzip")},
                timeout=600,
            )
    except httpx.TransportError as exc:
        raise BackupError(f"上传{what}网络失败", retryable=True) from exc
    if not 200 <= res.status_code < 300:
        raise BackupError(
            f"上传{what}失败（HTTP {res.status_code}）", retryable=res.status_code >= 500
        )
    return link["file_id"]


def days_to_prune(today: date, keep: str = KEEP_30_DAYS) -> list[date]:
    """要删的备份是哪几天的（多数那天本来就没有备份文件，删不到不算错）。
    - 30 天：31–40 天前的（某天没删掉的，后面 9 天还会再删）；
    - 本机（3 天加 4 个周）：1–40 天前的，除了最近 3 天（今天、昨天、前天）和最近 4 个周日
      （今天是周日就算一个）。从 30 天换过来的那天，31 天以内多出来的也一并删掉。"""
    if keep == KEEP_30_DAYS:
        return [today - timedelta(days=d) for d in _PRUNE_DAYS]
    if keep != KEEP_3_DAYS_4_WEEKS:
        raise BackupError(f"不认识的备份保留方式：{keep}")
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    kept = {today - timedelta(days=d) for d in range(3)}
    kept |= {last_sunday - timedelta(weeks=w) for w in range(4)}
    days = (today - timedelta(days=d) for d in range(1, _PRUNE_DAYS.stop))
    return [day for day in days if day not in kept]


def prune(
    client: httpx.Client,
    env: str,
    file_id: str,
    path: str,
    today: date,
    keep: str = KEEP_30_DAYS,
) -> int:
    """删掉旧备份（哪几天的见 days_to_prune），返回删掉的个数。"""
    prefix = file_id[: -len(path)] if file_id.endswith(path) else None
    if not prefix:
        raise BackupError("文件 ID 的格式和预期不同，没有删除旧备份")
    old = [prefix + object_path(day) for day in days_to_prune(today, keep)]
    return delete_files(client, env, old, what="旧备份")


def delete_files(client: httpx.Client, env: str, file_ids: list[str], *, what: str) -> int:
    """从对象存储删掉这些文件，返回删掉的个数。没删掉的写进日志备查（多数是本来就没有这个文件），
    不当作失败。"""
    data = _open_api(client, "/tcb/batchdeletefile", {"env": env, "fileid_list": file_ids})
    items = data.get("delete_list", [])
    for item in items:
        if item.get("status") != 0:
            log.info("%s没删掉：%s（%s）", what, item.get("fileid"), item.get("errmsg"))
    return sum(1 for item in items if item.get("status") == 0)


def _retrying(action: Callable[[], Any], sleep: Callable[[float], None]) -> Any:
    """网络中断、服务繁忙时等 5 秒、15 秒各重试一次；其他错误直接抛出。"""
    for wait in (*_RETRY_WAITS, None):
        try:
            return action()
        except BackupError as exc:
            if not exc.retryable or wait is None:
                raise
            sleep(wait)
    raise AssertionError("不会走到这里")


def download_latest(
    client: httpx.Client,
    env: str,
    dest: Path,
    *,
    today: date | None = None,
    sleep: Callable[[float], None] = clock.sleep,
) -> str:
    """下载近 40 天最新的备份到 dest，返回对象存储路径。"""
    today = today or datetime.now(BEIJING).date()
    link = _retrying(
        lambda: _open_api(client, "/tcb/uploadfile", {"env": env, "path": PROBE_PATH}), sleep
    )
    file_id = link.get("file_id")
    if not isinstance(file_id, str) or not file_id.endswith(PROBE_PATH):
        raise BackupError("无法取得备份文件 ID 前缀")
    prefix = file_id[: -len(PROBE_PATH)]
    if not prefix.startswith("cloud://"):
        raise BackupError("备份文件 ID 的格式和预期不同")
    for start in range(0, 40, 10):
        paths = [object_path(today - timedelta(days=d)) for d in range(start, start + 10)]
        ids = [prefix + name for name in paths]
        data = _retrying(
            lambda ids=ids: _open_api(
                client,
                "/tcb/batchdownloadfile",
                {"env": env, "file_list": [{"fileid": name, "max_age": 600} for name in ids]},
            ),
            sleep,
        )
        items = data.get("file_list") or data.get("download_list") or []
        if not isinstance(items, list):
            raise BackupError("备份下载链接列表格式不对")
        by_id = {item.get("fileid"): item for item in items if isinstance(item, dict)}
        for name, candidate in zip(paths, ids, strict=True):
            item = by_id.get(candidate)
            if not item or item.get("status") not in (None, 0):
                continue
            url = item.get("download_url") or item.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise BackupError("没有拿到备份的下载链接")
            attempts = 0

            def fetch(candidate: str = candidate) -> None:
                nonlocal attempts, url
                if attempts:
                    fresh = _open_api(
                        client,
                        "/tcb/batchdownloadfile",
                        {"env": env, "file_list": [{"fileid": candidate, "max_age": 600}]},
                    )
                    fresh_items = fresh.get("file_list") or fresh.get("download_list") or []
                    fresh_item = next(
                        (
                            i
                            for i in fresh_items
                            if isinstance(i, dict) and i.get("fileid") == candidate
                        ),
                        None,
                    )
                    url = (fresh_item or {}).get("download_url") or (fresh_item or {}).get("url")
                    if (
                        not fresh_item
                        or fresh_item.get("status") not in (None, 0)
                        or not isinstance(url, str)
                        or not url.startswith("https://")
                    ):
                        raise BackupError("没有拿到备份的下载链接")
                attempts += 1
                try:
                    with client.stream("GET", url, timeout=600) as response:
                        if response.status_code != 200:
                            raise BackupError(
                                f"下载备份失败（HTTP {response.status_code}）",
                                retryable=response.status_code >= 500
                                or response.status_code == 403,
                            )
                        with dest.open("wb") as out:
                            for chunk in response.iter_bytes():
                                out.write(chunk)
                except httpx.TransportError as exc:
                    raise BackupError("下载备份网络失败", retryable=True) from exc

            _retrying(fetch, sleep)
            return name
    raise BackupError("近 40 天没有找到备份文件")


def run_backup(
    engine: Engine,
    env: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = clock.sleep,
    scope: str = SCOPE_ALL,
    keep: str = KEEP_30_DAYS,
) -> BackupReport:
    """导出整库（scope=user 时只有用户数据）、上传到对象存储、删掉旧的（keep：30 天，或本机的
    3 天加 4 个周日；上传和删除遇到网络中断、服务繁忙都重试两次）。失败抛 BackupError
    （由 job.sh 发提醒）。"""
    now = now or datetime.now(UTC)
    today = now.astimezone(BEIJING).date()
    report = BackupReport(path=object_path(today))
    own = client is None
    client = client or storage.http_client()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / Path(report.path).name
            with local.open("wb") as out:
                report.counts = dump(engine, out, now=now, scope=scope)
            report.bytes = local.stat().st_size
            report.file_id = _retrying(lambda: upload(client, env, report.path, local), sleep)
        try:
            report.pruned = _retrying(
                lambda: prune(client, env, report.file_id, report.path, today, keep), sleep
            )
        except BackupError as exc:
            # 今天的备份已经上传好了；旧的没删掉也要提醒（明天还会再删）。30 天的说法和以前一样
            which = " 30 天前的备份" if keep == KEEP_30_DAYS else "旧备份"
            raise BackupError(f"今天的备份已上传，但删除{which}失败：{exc}") from exc
    finally:
        if own:
            client.close()
    return report
