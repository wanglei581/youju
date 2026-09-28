"""数据库备份（验收 A6）：每天把整库导出成一个压缩文件，存到云托管对象存储，保留 30 天；
出事时用 `youju restore` 恢复到迁移好的空库。

- 一致快照：所有表在同一个"可重复读"事务里读，导出的是同一时刻的数据。
- 格式：gzip 压缩的 JSON Lines。第一行是说明（格式版本、迁移版本、导出时间、各表条数），之后每行
  一条记录 {"t": 表名, "r": 字段}。和数据库品牌、版本无关，解压就能看。
- 存放：云托管对象存储 backups/youju-日期.jsonl.gz（北京时间的日期，同一天重跑就覆盖）。
  通过云托管"开放接口服务"上传、删除，不需要 access_token；每次上传后删掉 31–40 天前的。
- 备份里有用户数据（openid、关注、纠错），和数据库同等对待：存储桶权限设为
  "所有用户不可读（只有管理员可读写）"，只由管理员在云托管控制台下载。
"""

import gzip
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import IO, Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Engine, Table, func, select, text
from sqlalchemy import types as sqltypes

from youju.dbtypes import UTCDateTime
from youju.models import Base

FORMAT = "youju-backup"
VERSION = 1
KEEP_DAYS = 30
# 每次删掉这些天之前的备份（多删几天，漏跑过几天的也能清掉）
_PRUNE_DAYS = range(KEEP_DAYS + 1, KEEP_DAYS + 11)
_BATCH = 1000
BEIJING = ZoneInfo("Asia/Shanghai")
# 云托管开放接口服务：容器内用 http 调用，免 access_token（需在"云调用-微信令牌权限配置"里登记接口）
OPEN_API = "http://api.weixin.qq.com"


class BackupError(RuntimeError):
    """备份或恢复失败；信息里不含数据内容和上传凭证。"""


@dataclass
class BackupReport:
    counts: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    path: str = ""
    file_id: str = ""
    pruned: int = 0


def object_path(day: date) -> str:
    return f"backups/youju-{day:%Y%m%d}.jsonl.gz"


def _encode(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError(f"不支持的类型：{type(value).__name__}")


def _revision(conn) -> str | None:
    return conn.scalar(text("SELECT version_num FROM alembic_version"))


def dump(engine: Engine, out: IO[bytes], *, now: datetime | None = None) -> dict[str, int]:
    """把全部表按主键顺序导出到 out（gzip JSON Lines），返回各表条数。"""
    tables = Base.metadata.sorted_tables
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
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write((json.dumps(header, ensure_ascii=False) + "\n").encode())
            for table in tables:
                rows = conn.execution_options(stream_results=True, yield_per=_BATCH).execute(
                    select(table).order_by(*table.primary_key.columns)
                )
                for row in rows.mappings():
                    line = json.dumps(
                        {"t": table.name, "r": dict(row)}, ensure_ascii=False, default=_encode
                    )
                    gz.write((line + "\n").encode())
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


def _decoders(table: Table) -> dict[str, Callable[[Any], Any]]:
    return {c.name: d for c in table.columns if (d := _decoder(c)) is not None}


def restore(engine: Engine, src: IO[bytes]) -> dict[str, int]:
    """把备份恢复到 engine 指向的库：这个库要已迁移到和备份相同的版本、而且是空的。
    恢复完逐表核对条数，对不上就报错。返回各表条数。"""
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    with gzip.GzipFile(fileobj=src, mode="rb") as gz:
        header = json.loads(gz.readline())
        if header.get("format") != FORMAT or header.get("version") != VERSION:
            raise BackupError("不是有据的备份文件，或格式版本不认识")
        with engine.connect() as conn:
            if _revision(conn) != header.get("revision"):
                raise BackupError(
                    f"目标库的迁移版本是 {_revision(conn)}，备份是 {header.get('revision')}："
                    "先用同一版本的代码把目标库迁移好"
                )
            busy = [
                n for n, t in tables.items() if conn.scalar(select(func.count()).select_from(t))
            ]
            if busy:
                raise BackupError(f"目标库不是空的：{', '.join(busy)}")
        decoders = {name: _decoders(table) for name, table in tables.items()}
        with engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            batch: list[dict] = []
            current: str | None = None

            def flush() -> None:
                if batch:
                    conn.execute(tables[current].insert(), batch)
                    batch.clear()

            for raw in gz:
                item = json.loads(raw)
                name, row = item["t"], item["r"]
                if name not in tables:
                    raise BackupError(f"备份里有这个版本没有的表：{name}")
                if name != current or len(batch) >= _BATCH:
                    flush()
                    current = name
                for column, decode in decoders[name].items():
                    if row.get(column) is not None:
                        row[column] = decode(row[column])
                batch.append(row)
            flush()
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    with engine.connect() as conn:
        counts = {
            n: conn.scalar(select(func.count()).select_from(t)) or 0 for n, t in tables.items()
        }
    expected = header.get("tables", {})
    wrong = [n for n in tables if counts.get(n, 0) != expected.get(n, 0)]
    if wrong:
        raise BackupError(f"恢复后条数和备份说明对不上：{', '.join(wrong)}")
    return counts


def _open_api(client: httpx.Client, api: str, payload: dict) -> dict:
    try:
        res = client.post(f"{OPEN_API}{api}", json=payload, timeout=20)
        res.raise_for_status()
        data = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BackupError(f"调用 {api} 失败：{exc.__class__.__name__}") from exc
    if not isinstance(data, dict) or data.get("errcode") != 0:
        code = data.get("errcode") if isinstance(data, dict) else "?"
        raise BackupError(f"调用 {api} 失败（errcode {code}）")
    return data


def upload(client: httpx.Client, env: str, path: str, local: Path) -> str:
    """上传到云托管对象存储，返回文件 ID（cloud://…）。"""
    link = _open_api(client, "/tcb/uploadfile", {"env": env, "path": path})
    missing = [
        k for k in ("url", "authorization", "token", "cos_file_id", "file_id") if not link.get(k)
    ]
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
    except httpx.HTTPError as exc:
        raise BackupError(f"上传备份失败：{exc.__class__.__name__}") from exc
    if not 200 <= res.status_code < 300:
        raise BackupError(f"上传备份失败（HTTP {res.status_code}）")
    return link["file_id"]


def prune(client: httpx.Client, env: str, file_id: str, path: str, today: date) -> int:
    """删掉 31–40 天前的备份；本来就没有的不算错。返回删掉的个数。"""
    prefix = file_id[: -len(path)] if file_id.endswith(path) else None
    if not prefix:
        raise BackupError("文件 ID 的格式和预期不同，没有删除旧备份")
    old = [prefix + object_path(today - timedelta(days=d)) for d in _PRUNE_DAYS]
    data = _open_api(client, "/tcb/batchdeletefile", {"env": env, "fileid_list": old})
    return sum(1 for item in data.get("delete_list", []) if item.get("status") == 0)


def run_backup(
    engine: Engine,
    env: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> BackupReport:
    """导出整库、上传到对象存储、删掉 30 天前的。失败抛 BackupError（由 job.sh 发提醒）。"""
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
            report.file_id = upload(client, env, report.path, local)
        report.pruned = prune(client, env, report.file_id, report.path, today)
    finally:
        if own:
            client.close()
    return report
