"""本机这一端：记变更（触发器）、推到服务器、起步时导入服务器导出的公开数据。

见 youju/sync/__init__.py。
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import time as clock
import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx
from sqlalchemy import Engine, Table, func, select, text

from youju import storage
from youju.sync import tables as sync_tables
from youju.sync.tables import BLOB_COLUMNS, dumps, encode_row, pk_columns

log = logging.getLogger("youju.sync")
CHANGES = "sync_changes"
META = "sync_meta"
API = "/api/sync/v1"
# 每批最多多少条变更、每次请求最多多少字节（未压缩的 JSON）；超了就把这一批对半分
BATCH_CHANGES = 500
MAX_BYTES = 8 * 1024 * 1024
# 整表全推时每页多少行
PAGE_ROWS = 1000


class SyncError(RuntimeError):
    """同步出错；信息里不带密钥和数据内容。"""


# ---- 记变更 ----


def _trigger_bodies(t: Table) -> dict[str, tuple[str, str]]:
    cols = pk_columns(t)
    new = "JSON_ARRAY(" + ", ".join(f"NEW.`{c}`" for c in cols) + ")"
    old = "JSON_ARRAY(" + ", ".join(f"OLD.`{c}`" for c in cols) + ")"
    same = " AND ".join(f"OLD.`{c}` <=> NEW.`{c}`" for c in cols)
    log_new = f"INSERT INTO `{CHANGES}` (tbl, pk) VALUES ('{t.name}', {new})"
    log_old = f"INSERT INTO `{CHANGES}` (tbl, pk) VALUES ('{t.name}', {old})"
    return {
        "ai": ("INSERT", log_new),
        # 主键改了（很少见）：新旧两个主键都记，服务器删旧的、写新的
        "au": ("UPDATE", f"BEGIN {log_new}; IF NOT ({same}) THEN {log_old}; END IF; END"),
        "ad": ("DELETE", log_old),
    }


def trigger_names() -> list[str]:
    return [f"sync_{t.name}_{s}" for t in sync_tables.tables() for s in ("ai", "au", "ad")]


def setup(engine: Engine) -> str:
    """在本机库上建变更表和触发器（可以重复执行：触发器先删后建）。返回这条变更流的编号。

    MySQL 外键级联删除、置空不触发触发器：本机删父行时只记父行，服务器那边按同样的外键级联。
    建触发器要 TRIGGER 权限；开着二进制日志时还要 SUPER 或 log_bin_trust_function_creators=1
    （本机的 MySQL 容器用 --skip-log-bin 启动，见 deploy/selfhost/local/docker-compose.yml）。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS `{CHANGES}` ("
                " seq BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                " tbl VARCHAR(64) NOT NULL,"
                " pk VARCHAR(512) NOT NULL,"
                " changed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"
                ") ENGINE=InnoDB"
            )
        )
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS `{META}` ("
                " name VARCHAR(32) NOT NULL PRIMARY KEY, value VARCHAR(64) NOT NULL"
                ") ENGINE=InnoDB"
            )
        )
        stream = conn.scalar(text(f"SELECT value FROM `{META}` WHERE name = 'stream'"))
        if not stream:
            stream = uuid.uuid4().hex
            conn.execute(
                text(f"INSERT INTO `{META}` (name, value) VALUES ('stream', :v)"), {"v": stream}
            )
    with engine.begin() as conn:
        for t in sync_tables.tables():
            for suffix, (event, body) in _trigger_bodies(t).items():
                name = f"sync_{t.name}_{suffix}"
                conn.execute(text(f"DROP TRIGGER IF EXISTS `{name}`"))
                conn.execute(
                    text(f"CREATE TRIGGER `{name}` AFTER {event} ON `{t.name}` FOR EACH ROW {body}")
                )
    return stream


def missing_triggers(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        have = set(
            conn.scalars(
                text(
                    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS"
                    " WHERE TRIGGER_SCHEMA = DATABASE()"
                )
            )
        )
    return [name for name in trigger_names() if name not in have]


def stream_of(engine: Engine) -> str | None:
    with engine.connect() as conn:
        try:
            return conn.scalar(text(f"SELECT value FROM `{META}` WHERE name = 'stream'"))
        except Exception:  # noqa: BLE001 - 没建过变更表
            return None


def _revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


# ---- 和服务器说话 ----


class Client:
    """带签名的同步请求（YOUJU_SYNC_URL、YOUJU_SYNC_SECRET）。http 可以换成测试用的客户端。"""

    def __init__(self, url: str, secret: str, *, http: httpx.Client | None = None) -> None:
        if not url.startswith(("https://", "http://")):
            raise SyncError("YOUJU_SYNC_URL 要写成 https://域名")
        if len(secret) < 32:
            raise SyncError("YOUJU_SYNC_SECRET 至少 32 位")
        self.base = url.rstrip("/")
        self._secret = secret
        self._http = http or httpx.Client(trust_env=False, timeout=300)

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: bytes = b"",
        gzip_body: bool = False,
        content_type: str = "application/json",
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        query = httpx.QueryParams(params or {})
        target = API + path + (f"?{query}" if params else "")
        payload = gzip.compress(body, 6) if gzip_body else body
        stamp = str(int(clock.time()))
        headers = {
            sync_tables.HEADER_TIME: stamp,
            sync_tables.HEADER_SIGNATURE: sync_tables.signature(
                self._secret, method, target, stamp, payload
            ),
            "Content-Type": content_type,
        }
        if gzip_body:
            headers["Content-Encoding"] = "gzip"
        for wait in (5, 30, 120, None):
            try:
                response = self._http.request(
                    method, self.base + target, content=payload, headers=headers
                )
            except httpx.TransportError as exc:
                if wait is None:
                    raise SyncError(f"连不上服务器：{type(exc).__name__}") from None
                clock.sleep(wait)
                # 签名有时效：重试前重签
                stamp = str(int(clock.time()))
                headers[sync_tables.HEADER_TIME] = stamp
                headers[sync_tables.HEADER_SIGNATURE] = sync_tables.signature(
                    self._secret, method, target, stamp, payload
                )
                continue
            if response.status_code in ok:
                return response
            if response.status_code >= 500 and wait is not None:
                clock.sleep(wait)
                continue
            detail = ""
            with contextlib.suppress(ValueError, AttributeError):
                detail = str(response.json().get("detail", ""))[:200]
            raise SyncError(f"服务器回 {response.status_code}：{detail}")
        raise AssertionError("不会走到这里")

    def json(self, method: str, path: str, data: Any = None, **kwargs) -> Any:
        body = dumps(data) if data is not None else b""
        return self.call(method, path, body=body, gzip_body=bool(body), **kwargs).json()


# ---- 推 ----


@dataclass
class PushReport:
    batches: int = 0
    upserts: int = 0
    deletes: int = 0
    blobs: int = 0
    kept: dict[str, int] = field(default_factory=dict)
    applied_seq: int = 0


def _rows_by_pk(engine: Engine, t: Table, pks: list[Any]) -> dict[Any, dict]:
    (col,) = pk_columns(t)
    found: dict[Any, dict] = {}
    with engine.connect() as conn:
        for start in range(0, len(pks), 1000):
            part = pks[start : start + 1000]
            for row in conn.execute(select(t).where(t.c[col].in_(part))).mappings():
                found[row[col]] = dict(row)
    return found


def _payload(engine: Engine, changes: list[tuple[int, str, str]]) -> tuple[dict, int, int]:
    """一批变更 → {表: {upserts, deletes}}。读的是现在的行：读不到就是删了。"""
    wanted: dict[str, list[Any]] = defaultdict(list)
    for _seq, name, raw in changes:
        if name not in sync_tables.SYNC_TABLES:
            continue
        values = json.loads(raw)
        if len(values) != 1:
            raise SyncError(f"{name} 的主键不是一列，现在的同步不支持")
        if values[0] not in wanted[name]:
            wanted[name].append(values[0])
    out: dict[str, dict] = {}
    ups = dels = 0
    for t in sync_tables.tables():
        pks = wanted.get(t.name)
        if not pks:
            continue
        found = _rows_by_pk(engine, t, pks)
        upserts = [encode_row(t, found[pk]) for pk in pks if pk in found]
        deletes = [pk for pk in pks if pk not in found]
        out[t.name] = {"upserts": upserts, "deletes": deletes}
        ups, dels = ups + len(upserts), dels + len(deletes)
    return out, ups, dels


def _blob_paths(tables_payload: dict) -> list[str]:
    paths = []
    for name, columns in BLOB_COLUMNS.items():
        for row in tables_payload.get(name, {}).get("upserts", []):
            for column in columns:
                path = storage.path_of(row.get(column))
                if path and path not in paths:
                    paths.append(path)
    return paths


def _read_blob(store: storage.Backend, path: str) -> bytes:
    if isinstance(store, storage.DiskBackend):
        with store.open(path) as fh:
            return fh.read()
    with httpx.Client(trust_env=False, timeout=120) as http:
        response = http.get(store.url(path, 600))
    if response.status_code != 200:
        raise SyncError(f"读本机附件副本失败（HTTP {response.status_code}）")
    return response.content


def _send_blobs(client: Client, paths: list[str], store: storage.Backend | None = None) -> int:
    """服务器存储里没有的附件副本先传上去（本机存储里没有的跳过，服务器读不到就当"还在准备"）。"""
    if not paths:
        return 0
    missing = client.json("POST", "/blobs/missing", {"paths": paths}).get("missing", [])
    sent = 0
    store = store or storage.backend()
    for path in missing:
        if not isinstance(path, str) or path not in paths:
            continue
        if not store.exists(path):
            log.warning("本机存储里没有附件副本 %s，跳过", path)
            continue
        data = _read_blob(store, path)
        client.call(
            "PUT",
            "/blobs",
            params={"path": path, "sha256": hashlib.sha256(data).hexdigest()},
            body=data,
            content_type="application/octet-stream",
        )
        sent += 1
    return sent


def _changes(engine: Engine, after: int, limit: int) -> list[tuple[int, str, str]]:
    with engine.connect() as conn:
        return [
            (int(seq), tbl, pk)
            for seq, tbl, pk in conn.execute(
                text(f"SELECT seq, tbl, pk FROM `{CHANGES}` WHERE seq > :a ORDER BY seq LIMIT :n"),
                {"a": after, "n": limit},
            )
        ]


def _prune(engine: Engine, upto: int) -> None:
    """服务器已经写进去的变更从本机删掉（分批，不长时间锁表）。"""
    while True:
        with engine.begin() as conn:
            gone = conn.execute(
                text(f"DELETE FROM `{CHANGES}` WHERE seq <= :u ORDER BY seq LIMIT 10000"),
                {"u": upto},
            ).rowcount
        if gone < 10000:
            return


def check_ready(engine: Engine, client: Client) -> dict:
    """推之前的检查：触发器都在、两边的迁移版本一样。返回服务器的状态。"""
    missing = missing_triggers(engine)
    if missing:
        raise SyncError(
            f"本机库少了 {len(missing)} 个同步触发器（迁移重建过表？），先执行 youju sync-setup"
        )
    state = client.json("GET", "/state")
    mine = _revision(engine)
    if state.get("revision") != mine:
        raise SyncError(
            f"两边的迁移版本不一样（本机 {mine}，服务器 {state.get('revision')}）："
            "先把两边升到同一版本的代码"
        )
    return state


def push(
    engine: Engine,
    client: Client,
    *,
    batch: int = BATCH_CHANGES,
    max_bytes: int = MAX_BYTES,
    max_batches: int | None = None,
    store: storage.Backend | None = None,
) -> PushReport:
    """增量推：从服务器记的进度往后，一批一批推，直到没有新的变更（或推满 max_batches 批）。"""
    state = check_ready(engine, client)
    stream = stream_of(engine)
    if state.get("stream") != stream:
        raise SyncError(
            "服务器记的不是这条变更流（第一次同步、或本机库重建过）：先做一次全推 "
            "youju sync-push --full，或起步时用 youju sync-import 后 youju sync-push --mark-synced"
        )
    applied = int(state.get("applied_seq") or 0)
    report = PushReport(applied_seq=applied)
    size = batch
    while max_batches is None or report.batches < max_batches:
        changes = _changes(engine, applied, size)
        if not changes:
            break
        payload, ups, dels = _payload(engine, changes)
        body = dumps(payload)
        if len(body) > max_bytes and len(changes) > 1:
            size = max(1, len(changes) // 2)
            continue
        report.blobs += _send_blobs(client, _blob_paths(payload), store)
        result = client.json(
            "POST",
            "/apply",
            {"stream": stream, "from_seq": applied, "to_seq": changes[-1][0], "tables": payload},
        )
        applied = int(result["applied_seq"])
        for name, count in (result.get("kept") or {}).items():
            report.kept[name] = report.kept.get(name, 0) + int(count)
        report.batches += 1
        report.upserts += ups
        report.deletes += dels
        report.applied_seq = applied
        _prune(engine, applied)
        size = batch
    return report


def _pages(engine: Engine, t: Table, page: int) -> Iterator[list[dict]]:
    (col,) = pk_columns(t)
    last = None
    while True:
        stmt = select(t).order_by(t.c[col]).limit(page)
        if last is not None:
            stmt = stmt.where(t.c[col] > last)
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
        if not rows:
            return
        yield rows
        last = rows[-1][col]


def full_push(
    engine: Engine,
    client: Client,
    *,
    page: int = PAGE_ROWS,
    max_bytes: int = MAX_BYTES,
    prune: bool = True,
    only: set[str] | None = None,
    store: storage.Backend | None = None,
) -> PushReport:
    """整表全推：本机各表的全部行按主键分页推一遍（父表在前），再按主键清单删掉服务器上
    多出来的行（子表在前），最后把服务器的进度设成开始全推前本机的最大序号。

    第一次同步、本机库重建过、或者两边对不上要修的时候用。推的时候本机照常抓取：开始以后的
    变更都在变更表里，全推完接着增量推。"""
    check_ready(engine, client)
    stream = stream_of(engine)
    if not stream:
        raise SyncError("本机库还没建同步触发器：先执行 youju sync-setup")
    with engine.connect() as conn:
        start_seq = int(conn.scalar(text(f"SELECT COALESCE(MAX(seq), 0) FROM `{CHANGES}`")) or 0)
    report = PushReport(applied_seq=start_seq)
    chosen = [t for t in sync_tables.tables() if only is None or t.name in only]
    for t in chosen:
        for rows in _pages(engine, t, page):
            chunk: list[dict] = []
            size = 0
            for row in rows:
                encoded = encode_row(t, row)
                chunk.append(encoded)
                size += len(dumps(encoded))
                if size >= max_bytes:
                    report.blobs += _send_full(client, t, chunk, store)
                    report.upserts += len(chunk)
                    chunk, size = [], 0
            if chunk:
                report.blobs += _send_full(client, t, chunk, store)
                report.upserts += len(chunk)
            report.batches += 1
    if prune:
        for t in reversed(chosen):
            report.deletes += _prune_server(engine, client, t, page * 10)
    client.json("POST", "/reset", {"stream": stream, "applied_seq": start_seq})
    _prune(engine, start_seq)
    return report


def _send_full(
    client: Client, t: Table, rows: list[dict], store: storage.Backend | None = None
) -> int:
    payload = {t.name: {"upserts": rows, "deletes": []}}
    sent = _send_blobs(client, _blob_paths(payload), store)
    client.json("POST", "/apply", {"full": True, "tables": payload})
    return sent


def _prune_server(engine: Engine, client: Client, t: Table, page: int) -> int:
    (col,) = pk_columns(t)
    after: Any = None
    removed = 0
    while True:
        params: dict[str, Any] = {"table": t.name, "limit": page}
        if after is not None:
            params["after"] = after
        theirs = client.json("GET", "/pks", params=params).get("pks", [])
        if not theirs:
            return removed
        with engine.connect() as conn:
            mine = set(conn.scalars(select(t.c[col]).where(t.c[col].in_(theirs))))
        extra = [pk for pk in theirs if pk not in mine]
        if extra:
            body = {"full": True, "tables": {t.name: {"upserts": [], "deletes": extra}}}
            result = client.json("POST", "/apply", body)
            removed += len(extra) - int((result.get("kept") or {}).get(t.name, 0))
        after = theirs[-1]


def mark_synced(engine: Engine, client: Client) -> int:
    """起步时用：本机刚从服务器导出的数据导入（两边一样），直接把服务器的进度设成本机现在的最大序号。"""
    check_ready(engine, client)
    stream = stream_of(engine)
    if not stream:
        raise SyncError("本机库还没建同步触发器：先执行 youju sync-setup")
    with engine.connect() as conn:
        seq = int(conn.scalar(text(f"SELECT COALESCE(MAX(seq), 0) FROM `{CHANGES}`")) or 0)
    client.json("POST", "/reset", {"stream": stream, "applied_seq": seq})
    _prune(engine, seq)
    return seq


def status(engine: Engine, client: Client | None) -> dict:
    with engine.connect() as conn:
        try:
            pending, oldest = conn.execute(
                text(f"SELECT COUNT(*), MIN(changed_at) FROM `{CHANGES}`")
            ).one()
        except Exception:  # noqa: BLE001 - 没建过
            pending, oldest = None, None
        counts = {
            t.name: conn.scalar(select(func.count()).select_from(t)) or 0
            for t in sync_tables.tables()
        }
    out: dict[str, Any] = {
        "stream": stream_of(engine),
        "pending_changes": pending,
        "oldest_pending": oldest.isoformat() if oldest else None,
        "missing_triggers": len(missing_triggers(engine)),
        "local_counts": counts,
    }
    if client is not None:
        state = client.json("GET", "/state")
        theirs = client.json("GET", "/counts").get("counts", {})
        out["server"] = state
        out["count_diff"] = {
            name: counts[name] - int(theirs.get(name, 0))
            for name in counts
            if counts[name] != int(theirs.get(name, 0))
        }
    return out


# ---- 起步：服务器导出、本机导入 ----

EXPORT_FORMAT = "youju-sync-export"


def export(engine: Engine, out: IO[bytes]) -> dict[str, int]:
    """（服务器上执行）把同步的表整表导出（gzip JSON Lines），给本机起步导入。

    和公开快照不同：下架的文件照样导出（本机是主库，以后在本机恢复上架），备注清空；用户提交的
    线索生成的截止日期（origin='user'）不导出。说明行另记各表的自增起点，本机导入后照它设，
    以后本机新建的行不会和服务器上已有的（包括没导出的）撞编号。"""
    tables = sync_tables.tables()
    counts: dict[str, int] = {}
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
        next_ids = {
            name: int(value or 1)
            for name, value in conn.execute(
                text(
                    "SELECT TABLE_NAME, AUTO_INCREMENT FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE()"
                )
            )
            if name in sync_tables.SYNC_TABLES
        }
        with gzip.GzipFile(fileobj=out, mode="wb") as gz:
            gz.write(
                dumps(
                    {
                        "format": EXPORT_FORMAT,
                        "version": 1,
                        "revision": revision,
                        "tables": [t.name for t in tables],
                        "next_ids": next_ids,
                    }
                )
                + b"\n"
            )
            for t in tables:
                (col,) = pk_columns(t)
                stmt = select(t).order_by(t.c[col])
                if t.name == "document_deadlines":
                    stmt = stmt.where(t.c.origin != "user")
                count = 0
                result = conn.execution_options(stream_results=True, yield_per=1000).execute(stmt)
                for row in result.mappings():
                    gz.write(dumps({"t": t.name, "r": encode_row(t, dict(row))}) + b"\n")
                    count += 1
                counts[t.name] = count
            gz.write(dumps({"end": True, "counts": counts}) + b"\n")
    return counts


def import_export(engine: Engine, src: IO[bytes]) -> dict[str, int]:
    """（本机执行）导入 export() 的文件：同步的表都要是空的、迁移版本一致；写完设自增起点。"""
    tables = {t.name: t for t in sync_tables.tables()}
    counts: dict[str, int] = defaultdict(int)
    with gzip.GzipFile(fileobj=src, mode="rb") as gz, engine.begin() as conn:
        header = json.loads(gz.readline())
        if header.get("format") != EXPORT_FORMAT:
            raise SyncError("不是同步导出的文件")
        mine = conn.scalar(text("SELECT version_num FROM alembic_version"))
        if header.get("revision") != mine:
            raise SyncError(f"迁移版本不一致：本机 {mine}，文件 {header.get('revision')}")
        busy = [n for n, t in tables.items() if conn.scalar(select(func.count()).select_from(t))]
        if busy:
            raise SyncError(f"本机这些表不是空的：{', '.join(busy)}")
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        batch: list[dict] = []
        current: str | None = None
        trailer = None
        for raw in gz:
            item = json.loads(raw)
            if item.get("end") is True:
                trailer = item
                break
            name = item.get("t")
            if name not in tables:
                raise SyncError(f"文件里有不认识的表：{name}")
            if name != current or len(batch) >= 1000:
                if batch:
                    conn.execute(tables[current].insert(), batch)
                batch, current = [], name
            batch.append(sync_tables.decode_row(tables[name], item.get("r")))
            counts[name] += 1
        if batch:
            conn.execute(tables[current].insert(), batch)
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        expected = trailer.get("counts") if isinstance(trailer, dict) else None
        if not isinstance(expected, dict) or any(
            int(expected.get(n, 0)) != counts.get(n, 0) for n in tables
        ):
            # 抛出后整个事务回滚，本机的表保持空的
            raise SyncError("文件不完整或条数对不上")
        for name, value in (header.get("next_ids") or {}).items():
            if name in tables and type(value) is int and value > 1:
                conn.execute(text(f"ALTER TABLE `{name}` AUTO_INCREMENT = {int(value)}"))
    return dict(counts)


def export_to_file(engine: Engine, path: Path) -> dict[str, int]:
    with Path(path).open("wb") as out:
        return export(engine, out)


def import_file(engine: Engine, path: Path) -> dict[str, int]:
    with Path(path).open("rb") as src:
        return import_export(engine, src)


def client_from_settings(settings, *, http: httpx.Client | None = None) -> Client:
    secret = settings.sync_secret.get_secret_value().strip() if settings.sync_secret else ""
    if not settings.sync_url or not secret:
        raise SyncError("本机要配 YOUJU_SYNC_URL 和 YOUJU_SYNC_SECRET")
    return Client(settings.sync_url, secret, http=http)
