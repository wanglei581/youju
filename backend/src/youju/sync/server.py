"""服务器这一端：收本机推来的公开数据。配了 YOUJU_SYNC_SECRET 才开放（否则一律 404）。

每个请求都要带本机用同一个密钥算的签名（tables.signature：方法、路径加查询串、时间、包体的
SHA-256），时间差 5 分钟以上的不认。见 youju/sync/__init__.py。
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time as clock
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import Connection, Engine, Table, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import Insert
from starlette.concurrency import run_in_threadpool

from youju import storage
from youju.config import get_settings
from youju.db import get_engine
from youju.sync import tables as sync_tables
from youju.sync.tables import pk_columns

log = logging.getLogger("youju.sync")
router = APIRouter(prefix="/api/sync/v1", include_in_schema=False)
STATE = "sync_state"
# 一次请求最多收多少字节（压缩后），解压后最多多少：防止塞爆内存
MAX_BODY = 64 * 1024 * 1024
MAX_PLAIN = 512 * 1024 * 1024
MAX_BLOB = 25 * 1024 * 1024
BLOB_PREFIX = "attachments/"


class Replace(Insert):
    """REPLACE INTO：主键或唯一键撞上的旧行先删再写（见 apply 的说明）。
    不进编译缓存：免得和同一张表的 INSERT 共用缓存里的 SQL。"""

    inherit_cache = False


@compiles(Replace, "mysql")
def _compile_replace(element, compiler, **kw):
    sql = compiler.visit_insert(element, **kw)
    return "REPLACE" + sql[len("INSERT") :]


def _secret() -> str:
    settings = get_settings()
    return settings.sync_secret.get_secret_value().strip() if settings.sync_secret else ""


async def _body(request: Request) -> bytes:
    """读包体（限大小）、验签、按需解压。签名算在原样收到的包体上。"""
    secret = _secret()
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise HTTPException(status_code=413, detail="包体太大")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise HTTPException(status_code=413, detail="包体太大")
    body = bytes(raw)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not sync_tables.signature_ok(
        secret,
        request.method,
        target,
        request.headers.get(sync_tables.HEADER_TIME, ""),
        body,
        request.headers.get(sync_tables.HEADER_SIGNATURE, ""),
        clock.time(),
    ):
        raise HTTPException(status_code=401, detail="签名不对或已过期")
    if request.headers.get("content-encoding") == "gzip":
        try:
            body = _gunzip(body)
        except (OSError, EOFError, zlib.error, ValueError):
            raise HTTPException(status_code=400, detail="包体解压失败") from None
    return body


def _gunzip(data: bytes) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = decompressor.decompress(data, MAX_PLAIN)
    if decompressor.unconsumed_tail or not decompressor.eof:
        raise ValueError("解压后太大或不完整")
    return out


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="包体不是 JSON") from None


def ensure_state(engine: Engine) -> None:
    """进度表（不是迁移：只是同步的记账，服务器第一次收同步时建）。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS `{STATE}` ("
                " name VARCHAR(16) NOT NULL PRIMARY KEY,"
                " stream VARCHAR(64) NOT NULL,"
                " applied_seq BIGINT NOT NULL,"
                " updated_at DATETIME(6) NOT NULL"
                ") ENGINE=InnoDB"
            )
        )


def read_state(engine: Engine) -> dict:
    ensure_state(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT stream, applied_seq, updated_at FROM `{STATE}` WHERE name = 'public'")
        ).first()
        revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
    return {
        "stream": row.stream if row else None,
        "applied_seq": int(row.applied_seq) if row else 0,
        "updated_at": row.updated_at.isoformat() if row else None,
        "revision": revision,
    }


class Conflict(Exception):
    def __init__(self, state: dict) -> None:
        super().__init__("进度对不上")
        self.state = state


def _decoded(payload: Any) -> dict[str, tuple[Table, list[dict], list[Any]]]:
    """校验并解码一批：只认同步的表、字段要和这个版本的表结构一致、主键只有一列。"""
    if not isinstance(payload, dict):
        raise ValueError("tables 格式不对")
    out = {}
    for name, part in payload.items():
        t = sync_tables.table(name)
        if len(pk_columns(t)) != 1:
            raise ValueError(f"{name} 的主键不是一列")
        if not isinstance(part, dict):
            raise ValueError(f"{name} 格式不对")
        upserts, deletes = part.get("upserts", []), part.get("deletes", [])
        if not isinstance(upserts, list) or not isinstance(deletes, list):
            raise ValueError(f"{name} 格式不对")
        if any(isinstance(pk, (dict, list)) or pk is None for pk in deletes):
            raise ValueError(f"{name} 的删除主键格式不对")
        out[name] = (t, [sync_tables.decode_row(t, row) for row in upserts], deletes)
    return out


def _old_blob_paths(conn: Connection, parts: dict) -> set[str]:
    if "attachment_copies" not in parts:
        return set()
    t, upserts, deletes = parts["attachment_copies"]
    ids = [row["id"] for row in upserts] + list(deletes)
    paths: set[str] = set()
    for start in range(0, len(ids), 1000):
        rows = conn.execute(select(t.c.file_id).where(t.c.id.in_(ids[start : start + 1000])))
        paths.update(p for (fid,) in rows if (p := storage.path_of(fid)))
    return paths


def apply(engine: Engine, payload: dict) -> dict:
    """写一批（一个事务）。

    - 增量（full 不是 true）：stream、from_seq 要和服务器记的进度一致，否则抛 Conflict（409），
      写完把进度改成 to_seq；
    - 全推（full=true）：不看、不改进度（最后由 /reset 设）。
    先删后写：删按子表先、外键检查照开（用户数据里指向它的照外键级联或置空；被挡住删不了的那一行
    留着，按表计数写进回执 kept）；写用 REPLACE、关外键检查（见 youju/sync/__init__.py）。"""
    full = payload.get("full") is True
    parts = _decoded(payload.get("tables"))
    ensure_state(engine)
    kept: dict[str, int] = {}
    with engine.connect() as conn, conn.begin():
        if not full:
            row = conn.execute(
                text(f"SELECT stream, applied_seq FROM `{STATE}` WHERE name = 'public' FOR UPDATE")
            ).first()
            from_seq, to_seq = payload.get("from_seq"), payload.get("to_seq")
            if type(from_seq) is not int or type(to_seq) is not int or to_seq <= from_seq:
                raise ValueError("from_seq、to_seq 不对")
            if row is None or row.stream != payload.get("stream") or row.applied_seq != from_seq:
                current = {
                    "stream": row.stream if row else None,
                    "applied_seq": row.applied_seq if row else 0,
                }
                raise Conflict(current)
        old_paths = _old_blob_paths(conn, parts)
        ordered = [t for t in sync_tables.tables() if t.name in parts]
        for t in reversed(ordered):
            _, _, deletes = parts[t.name]
            if deletes:
                lost = _delete(conn, t, deletes)
                if lost:
                    kept[t.name] = lost
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for t in ordered:
                _, upserts, _ = parts[t.name]
                for start in range(0, len(upserts), 500):
                    conn.execute(Replace(t), upserts[start : start + 500])
        finally:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        if not full:
            conn.execute(
                text(
                    f"UPDATE `{STATE}` SET applied_seq = :s, updated_at = :u WHERE name = 'public'"
                ),
                {"s": payload["to_seq"], "u": datetime.now(UTC).replace(tzinfo=None)},
            )
    if old_paths:
        _collect_garbage(engine, old_paths)
    state = read_state(engine)
    return {"applied_seq": state["applied_seq"], "kept": kept}


def _delete(conn: Connection, t: Table, pks: list[Any]) -> int:
    """删这些主键的行；被用户数据（RESTRICT 外键）挡住的逐行留下，返回留下的行数。"""
    (col,) = pk_columns(t)
    try:
        with conn.begin_nested():
            for start in range(0, len(pks), 1000):
                conn.execute(t.delete().where(t.c[col].in_(pks[start : start + 1000])))
        return 0
    except IntegrityError:
        pass
    lost = 0
    for pk in pks:
        try:
            with conn.begin_nested():
                conn.execute(t.delete().where(t.c[col] == pk))
        except IntegrityError:
            lost += 1
            log.warning("同步删除 %s #%s 被用户数据挡住，留着", t.name, pk)
    return lost


def _collect_garbage(engine: Engine, paths: set[str]) -> None:
    """换下来、删掉的附件副本文件：服务器自己的存储（disk）里没人再指向就删掉。
    s3 可能和本机共用一个存储桶，由本机管，不在这里删。"""
    settings = get_settings()
    if settings.storage_backend != "disk":
        return
    t = sync_tables.table("attachment_copies")
    store = storage.backend(settings)
    with engine.connect() as conn:
        for path in paths:
            used = conn.scalar(
                select(func.count()).select_from(t).where(t.c.file_id.like(f"cloud://%/{path}"))
            )
            if not used:
                try:
                    store.delete(path)
                except (storage.StorageError, OSError):
                    log.warning("删除没人用的附件副本失败：%s", path)


# ---- 接口 ----


@router.get("/state")
async def state(request: Request) -> dict:
    await _body(request)
    return read_state(get_engine())


@router.get("/counts")
async def counts(request: Request) -> dict:
    await _body(request)
    with get_engine().connect() as conn:
        return {
            "counts": {
                t.name: conn.scalar(select(func.count()).select_from(t)) or 0
                for t in sync_tables.tables()
            }
        }


@router.get("/pks")
async def pks(request: Request, table: str, limit: int = 10000, after: str | None = None) -> dict:
    await _body(request)
    try:
        t = sync_tables.table(table)
    except KeyError:
        raise HTTPException(status_code=422, detail="不是同步的表") from None
    (col,) = pk_columns(t)
    stmt = select(t.c[col]).order_by(t.c[col]).limit(max(1, min(limit, 50000)))
    if after is not None:
        stmt = stmt.where(t.c[col] > (int(after) if after.lstrip("-").isdigit() else after))
    with get_engine().connect() as conn:
        return {"pks": list(conn.scalars(stmt))}


@router.post("/apply")
async def apply_batch(request: Request) -> dict:
    payload = _json(await _body(request))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="包体格式不对")
    try:
        return await run_in_threadpool(apply, get_engine(), payload)
    except Conflict as exc:
        detail = {"message": "进度对不上", **exc.state}
        raise HTTPException(status_code=409, detail=detail) from None
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"这一批的格式不对：{exc}"[:300]) from None


@router.post("/reset")
async def reset(request: Request) -> dict:
    payload = _json(await _body(request))
    stream, seq = payload.get("stream"), payload.get("applied_seq")
    if not isinstance(stream, str) or not 8 <= len(stream) <= 64 or type(seq) is not int:
        raise HTTPException(status_code=422, detail="stream、applied_seq 不对")
    engine = get_engine()
    ensure_state(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO `{STATE}` (name, stream, applied_seq, updated_at)"
                " VALUES ('public', :st, :s, :u)"
                " ON DUPLICATE KEY UPDATE stream = :st, applied_seq = :s, updated_at = :u"
            ),
            {"st": stream, "s": seq, "u": datetime.now(UTC).replace(tzinfo=None)},
        )
    return read_state(engine)


def _blob_path(path: Any) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith(BLOB_PREFIX)
        or not storage.valid_path(path)
        or path.count("/") != 1
    ):
        raise HTTPException(status_code=422, detail="附件副本路径不对")
    return path


@router.post("/blobs/missing")
async def missing_blobs(request: Request) -> dict:
    payload = _json(await _body(request))
    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not isinstance(paths, list) or len(paths) > 2000:
        raise HTTPException(status_code=422, detail="paths 不对")
    store = storage.backend()
    return {"missing": [p for p in map(_blob_path, paths) if not store.exists(p)]}


@router.put("/blobs")
async def put_blob(request: Request, path: str, sha256: str) -> dict:
    """收一个附件副本文件。附件副本按内容的 SHA-256 命名（attachments/<SHA-256>.<格式>），
    名字、参数和内容三者对得上才存。"""
    body = await _body(request)
    path = _blob_path(path)
    digest = hashlib.sha256(body).hexdigest()
    name = path[len(BLOB_PREFIX) :].split(".", 1)[0]
    if len(body) > MAX_BLOB or digest != sha256 or name != digest:
        raise HTTPException(status_code=422, detail="附件副本内容和名字对不上")
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "blob"
        local.write_bytes(body)
        storage.backend().put_file(path, local)
    return {"stored": path}
