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
from sqlalchemy import Connection, Engine, Table, func, select, text
from sqlalchemy import types as sqltypes
from sqlalchemy.exc import SQLAlchemyError

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


# 不备份、不恢复的表：访问日志暂存表每天归档到对象存储，归档自己保存（见 youju/access_log.py）
NOT_BACKED_UP = frozenset({"access_logs"})


def backed_up_tables() -> list[Table]:
    return [t for t in Base.metadata.sorted_tables if t.name not in NOT_BACKED_UP]


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


def _rows(conn: Connection, table: Table) -> Iterator[dict]:
    result = conn.execution_options(stream_results=True, yield_per=_BATCH).execute(
        select(table).order_by(*table.primary_key.columns)
    )
    for row in result.mappings():
        yield dict(row)


def _revision(conn: Connection) -> str | None:
    return conn.scalar(text("SELECT version_num FROM alembic_version"))


def dump(engine: Engine, out: IO[bytes], *, now: datetime | None = None) -> dict[str, int]:
    """把全部表（访问日志暂存表除外）按主键顺序导出到 out（gzip JSON Lines），返回各表条数。"""
    tables = backed_up_tables()
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        # 先数条数：可重复读下，后面读到的和这里数的是同一时刻的数据
        counts = {t.name: conn.scalar(select(func.count()).select_from(t)) or 0 for t in tables}
        header = {
            "format": FORMAT,
            "version": VERSION,
            "revision": _revision(conn),
            "created_at": (now or datetime.now(UTC)).isoformat(),
            "tables": counts,
        }
        digests: dict[str, str] = {}
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write((json.dumps(header, ensure_ascii=False) + "\n").encode())
            for table in tables:
                digest = hashlib.sha256()
                for row in _rows(conn, table):
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


def _orphans(conn: Connection, tables: dict[str, Table]) -> list[str]:
    """外键指向不存在的记录的：子表.字段。"""
    found = []
    for table in tables.values():
        for fk in table.foreign_keys:
            child, parent = fk.parent, fk.column
            parent_table = parent.table
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


def _load(conn: Connection, gz: IO[bytes], header: dict, tables: dict[str, Table]) -> dict:
    """锁表后检查版本和空库、逐行写入，返回文件结尾（各表 SHA-256）。出错抛异常，由调用方回滚。"""
    if _revision(conn) != header.get("revision"):
        raise BackupError(
            f"目标库的迁移版本是 {_revision(conn)}，备份是 {header.get('revision')}："
            "先用同一版本的代码把目标库迁移好"
        )
    if set(header["tables"]) != set(tables):
        raise BackupError("备份文件的说明行格式不对：表名和这个版本的表对不上")
    busy = [n for n, t in tables.items() if conn.scalar(select(func.count()).select_from(t))]
    if busy:
        raise BackupError(f"目标库不是空的：{', '.join(busy)}")
    columns = {name: {c.name for c in t.columns} for name, t in tables.items()}
    decoders = {
        name: {c.name: d for c in t.columns if (d := _decoder(c)) is not None}
        for name, t in tables.items()
    }
    batch: list[dict] = []
    current: str | None = None
    trailer: dict | None = None
    for number, raw in enumerate(gz, start=2):
        item = json.loads(raw)
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
    return trailer


def _verify(conn: Connection, header: dict, trailer: dict, tables: dict[str, Table]) -> dict:
    """写完、提交前核对：各表条数、外键、各表内容的 SHA-256 都要和备份一致。"""
    counts = {n: conn.scalar(select(func.count()).select_from(t)) or 0 for n, t in tables.items()}
    expected = header.get("tables", {})
    wrong = [n for n in tables if counts[n] != expected.get(n, 0)]
    if wrong:
        raise BackupError(f"写入后条数和备份说明对不上：{', '.join(wrong)}")
    orphans = _orphans(conn, tables)
    if orphans:
        raise BackupError(f"写入后外键对不上：{', '.join(orphans)}")
    digests = trailer["sha256"]
    changed = []
    for name, table in tables.items():
        digest = hashlib.sha256()
        for row in _rows(conn, table):
            digest.update(_line(name, row))
        if digest.hexdigest() != digests.get(name):
            changed.append(name)
    if changed:
        raise BackupError(f"写入后内容和备份不一致：{', '.join(changed)}")
    return counts


def restore(engine: Engine, src: IO[bytes]) -> dict[str, int]:
    """把备份恢复到 engine 指向的库：这个库要已迁移到和备份相同的版本、而且是空的。
    锁住全部表后检查、写入、核对，全部对上才提交；否则回滚并报错。返回各表条数。"""
    tables = {t.name: t for t in backed_up_tables()}
    parent_locks = [
        f"`{table.name}` AS `{table.name}_{fk.parent.name}_parent` READ"
        for table in tables.values()
        for fk in table.foreign_keys
        if fk.column.table is table
    ]
    locks = ", ".join(
        [f"`{name}` WRITE" for name in tables] + parent_locks + ["`alembic_version` READ"]
    )
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
            with engine.connect() as conn:
                # 锁住全部表：检查空库到写完提交之间，别的程序（如定时任务）写不进来
                conn.execute(text(f"LOCK TABLES {locks}"))
                try:
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                    trailer = _load(conn, gz, header, tables)
                    counts = _verify(conn, header, trailer, tables)
                    conn.commit()
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
    """只读校验完整备份及当前库的迁移版本，不读取或修改业务表。"""
    tables = {t.name: t for t in backed_up_tables()}
    counts = dict.fromkeys(tables, 0)
    digests = {name: hashlib.sha256() for name in tables}
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


def prune(client: httpx.Client, env: str, file_id: str, path: str, today: date) -> int:
    """删掉 31–40 天前的备份，返回删掉的个数。清单里多数文件本来就没有（删不到不算错）；
    某天没删掉的，后面 9 天还在清单里会再删。"""
    prefix = file_id[: -len(path)] if file_id.endswith(path) else None
    if not prefix:
        raise BackupError("文件 ID 的格式和预期不同，没有删除旧备份")
    old = [prefix + object_path(today - timedelta(days=d)) for d in _PRUNE_DAYS]
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
) -> BackupReport:
    """导出整库、上传到对象存储、删掉 30 天前的（上传和删除遇到网络中断、服务繁忙都重试两次）。
    失败抛 BackupError（由 job.sh 发提醒）。"""
    now = now or datetime.now(UTC)
    today = now.astimezone(BEIJING).date()
    report = BackupReport(path=object_path(today))
    own = client is None
    client = client or httpx.Client(trust_env=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / Path(report.path).name
            with local.open("wb") as out:
                report.counts = dump(engine, out, now=now)
            report.bytes = local.stat().st_size
            report.file_id = _retrying(lambda: upload(client, env, report.path, local), sleep)
        try:
            report.pruned = _retrying(
                lambda: prune(client, env, report.file_id, report.path, today), sleep
            )
        except BackupError as exc:
            # 今天的备份已经上传好了；旧的没删掉也要提醒（后面 9 天还会再删）
            raise BackupError(f"今天的备份已上传，但删除 30 天前的备份失败：{exc}") from exc
    finally:
        if own:
            client.close()
    return report
