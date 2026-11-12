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
# 写入的先后：记下写过的最新一批的请求时间（毫秒，签名里的），比它旧的不写（server.apply）
ORDER = "sync_order"
# 写进来时两边都没有文件的附件副本（待补）：本机每次推之前来取，手上有了就补传；文件到了就划掉
PENDING = "sync_pending_blobs"
# 一次请求最多收多少字节（压缩后），解压后最多多少：防止塞爆内存
# 本机每批按未压缩 8MB 切（local.MAX_BYTES），附件副本最大 20MB：这两个上限都留了余量，
# 又不至于让几个请求就把 worker（限 640MB 内存）撑爆
MAX_BODY = 32 * 1024 * 1024
MAX_PLAIN = 64 * 1024 * 1024
MAX_BLOB = 25 * 1024 * 1024
BLOB_PREFIX = "attachments/"
SERVER_OWNED = sync_tables.SERVER_OWNED
_FLOORS_READY = False


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
    """先只看请求头验签（方法、路径加查询串、时间、头里写的包体 SHA-256），签名对了才读包体
    （限大小），读完核对包体的 SHA-256 和头里的一致，再按需解压。"""
    secret = _secret()
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    digest = request.headers.get(sync_tables.HEADER_BODY, "")
    if not sync_tables.signature_ok(
        secret,
        request.method,
        target,
        request.headers.get(sync_tables.HEADER_TIME, ""),
        digest,
        request.headers.get(sync_tables.HEADER_SIGNATURE, ""),
        clock.time(),
    ):
        raise HTTPException(status_code=401, detail="签名不对或已过期")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise HTTPException(status_code=413, detail="包体太大")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise HTTPException(status_code=413, detail="包体太大")
    body = bytes(raw)
    del raw
    if sync_tables.body_digest(body) != digest:
        raise HTTPException(status_code=401, detail="包体和签名对不上")
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


def ensure_floors(conn: Connection) -> None:
    """服务器自己会新建行的同步表（SERVER_ID_FLOOR）：自增起点抬到下限以上，服务器自己的行（如用户
    线索生成的截止日期）从那里往上分，和本机推来的编号不会撞。每个进程查一次。"""
    global _FLOORS_READY
    if _FLOORS_READY:
        return
    conn.execute(text("SET SESSION information_schema_stats_expiry = 0"))
    for name, floor in sync_tables.SERVER_ID_FLOOR.items():
        row = conn.execute(
            text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": name},
        ).first()
        # 没写过行、刚清空的表这里是 NULL（本机 MySQL 8 实测），也要抬
        if row is not None and (row[0] is None or int(row[0]) < floor):
            conn.execute(text(f"ALTER TABLE `{name}` AUTO_INCREMENT = {int(floor)}"))
    _FLOORS_READY = True


def ensure_floors_on_start() -> None:
    """2.0 服务器（配了同步密钥、收本机同步的）启动时就把编号下限抬好：管理台把用户线索送核对时
    （生成服务器自己的截止日期）不会先拿到小编号。没配同步的（云托管）什么都不做。出错只记日志，
    收第一批同步时（ensure_state）还会再试。"""
    settings = get_settings()
    secret = settings.sync_secret.get_secret_value().strip() if settings.sync_secret else ""
    if not secret:
        return
    try:
        with get_engine().connect() as conn:
            ensure_floors(conn)
    except Exception as exc:  # noqa: BLE001 - 启动不能因为它失败；只记类型
        log.warning(
            "启动时抬同步表的编号下限没成功（收第一批同步时会再试）：%s", type(exc).__name__
        )


def ensure_state(engine: Engine) -> None:
    """进度表（不是迁移：只是同步的记账，服务器第一次收同步时建）。"""
    with engine.connect() as conn:
        ensure_floors(conn)
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
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS `{ORDER}` ("
                " name VARCHAR(16) NOT NULL PRIMARY KEY,"
                " last_stamp BIGINT NOT NULL,"
                " updated_at DATETIME(6) NOT NULL"
                ") ENGINE=InnoDB"
            )
        )
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS `{PENDING}` ("
                " path VARCHAR(255) NOT NULL PRIMARY KEY,"
                " noted_at DATETIME(6) NOT NULL"
                ") ENGINE=InnoDB"
            )
        )


def max_ids(conn: Connection) -> dict[str, int]:
    """各同步表现在最大的主键（服务器自己的行不算）。本机推之前用它核对本机的自增编号没有倒退
    （本机从备份恢复、重建过时，新建的行会和服务器上已有的撞编号，REPLACE 会盖掉别的行）。"""
    out = {}
    for t in sync_tables.tables():
        (col,) = pk_columns(t)
        if not t.c[col].autoincrement or t.c[col].foreign_keys:
            continue  # 主键是父表编号的（如 document_details）跟着父表走，不单独核对
        stmt = select(func.max(t.c[col]))
        if t.name in SERVER_OWNED:
            stmt = stmt.where(SERVER_OWNED[t.name](t))
        value = conn.scalar(stmt)
        if value is not None:
            out[t.name] = int(value)
    return out


def read_state(engine: Engine, *, with_ids: bool = False) -> dict:
    ensure_state(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT stream, applied_seq, updated_at FROM `{STATE}` WHERE name = 'public'")
        ).first()
        revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
        ids = max_ids(conn) if with_ids else None
    out = {
        "stream": row.stream if row else None,
        "applied_seq": int(row.applied_seq) if row else 0,
        "updated_at": row.updated_at.isoformat() if row else None,
        "revision": revision,
    }
    if ids is not None:
        out["max_ids"] = ids
    return out


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


class Stale(Exception):
    def __init__(self, last: int) -> None:
        super().__init__("这一批比服务器已经写过的旧")
        self.last = last


class OwnedClash(Exception):
    def __init__(self, table: str, ids: list[Any]) -> None:
        super().__init__("本机推来的行和服务器自己的行编号撞了")
        self.table = table
        self.ids = ids


def _owned_clash(conn: Connection, parts: dict) -> None:
    """要写的行里有和服务器自己的行（SERVER_OWNED 以外的那部分，如用户线索生成的截止日期）同编号的：
    REPLACE 会把服务器自己的行盖掉，不写、报出来（编号下限之后不该出现，出现就是哪里不对）。"""
    for name, own in SERVER_OWNED.items():
        if name not in parts:
            continue
        t, upserts, _ = parts[name]
        (col,) = pk_columns(t)
        ids = [row[col] for row in upserts if row.get(col) is not None]
        for start in range(0, len(ids), 1000):
            clash = list(
                conn.scalars(
                    select(t.c[col]).where(t.c[col].in_(ids[start : start + 1000]), ~own(t))
                )
            )
            if clash:
                raise OwnedClash(name, clash)


class MissingBlobs(Exception):
    def __init__(self, paths: list[str]) -> None:
        super().__init__("附件副本文件不在服务器上")
        self.paths = paths


def _take_turn(conn: Connection, stamp: int) -> None:
    """在 sync_order 那一行上排队（SELECT … FOR UPDATE）：所有写入（apply、reset）和附件副本的
    清理都先拿这把锁。stamp 是请求签名里的毫秒时间，要比写过的最新一次新，否则抛 Stale；
    清理不是请求，传 0 只排队、不比时间。"""
    conn.execute(
        text(
            f"INSERT IGNORE INTO `{ORDER}` (name, last_stamp, updated_at) VALUES ('public', 0, :u)"
        ),
        {"u": datetime.now(UTC).replace(tzinfo=None)},
    )
    last = conn.execute(
        text(f"SELECT last_stamp FROM `{ORDER}` WHERE name = 'public' FOR UPDATE")
    ).scalar_one()
    if stamp and stamp <= last:
        raise Stale(int(last))


def _record_turn(conn: Connection, stamp: int) -> None:
    conn.execute(
        text(f"UPDATE `{ORDER}` SET last_stamp = :s, updated_at = :u WHERE name = 'public'"),
        {"s": stamp, "u": datetime.now(UTC).replace(tzinfo=None)},
    )


def _missing_blobs(parts: dict, local_has: set[str]) -> tuple[list[str], list[str]]:
    """这一批要写的附件副本行指向的文件里，本机有（local_has，本机随这一批告诉的）、服务器自己的
    存储（disk）里却不在的：推之前查过在、随后被清理删了。清理和写入在同一把锁下，所以核对时在的，
    写完之前不会被清理删掉；这些不在的不写，本机下一轮先补传再写。两边都没有的（本机也没有文件，
    比如云托管时期抓的、还没导进来的）照写，记进待补（PENDING）：不能让整批卡住，后面的下架也推不过
    来；以后本机有了文件，推之前来取待补、补传（不用等这一行再变），导进服务器的也会划掉。
    返回（不写的，待补的）。s3 由本机管，不核对。"""
    if "attachment_copies" not in parts or get_settings().storage_backend != "disk":
        return [], []
    _, upserts, _ = parts["attachment_copies"]
    store = storage.backend()
    paths = {p for row in upserts if (p := storage.path_of(row.get("file_id") or ""))}
    absent = sorted(p for p in paths if not store.exists(p))
    nowhere = [p for p in absent if p not in local_has]
    if nowhere:
        log.warning(
            "这一批有 %d 个附件副本两边都没有文件，照写、记待补（第一个：%s）",
            len(nowhere),
            nowhere[0],
        )
    return [p for p in absent if p in local_has], nowhere


def apply(engine: Engine, payload: dict, *, stamp: int) -> dict:
    """写一批（一个事务）。

    - 先后：stamp 是这个请求签名里的时间（毫秒）。每一批（增量、全推都算）都要比服务器写过的
      最新一批新，不然不写（抛 Stale，409）：签名 5 分钟内有效，挡住重放；本机重试会重签新时间，
      卡在半路的旧请求晚到时已经比重试那次旧了，不会用旧的样子盖掉新的。所有写入在这一行上排队。

    - 增量（full 不是 true）：stream 要和服务器记的一致（本机换了变更流、或服务器换过，都要先
      重新起步），否则抛 Conflict（409）。不按序号排先后：本机每批推的是这些行"现在"的样子，
      早一批晚一批、重推一遍结果都一样；服务器只记下推到过的最大序号给人看；
    - 全推（full=true）：不看 stream（第一次全推完由 /reset 设）。
    先删后写：删按子表先、外键检查照开（用户数据里指向它的照外键级联或置空；被挡住删不了的那一行
    留着，按表计数写进回执 kept；服务器自己的行不删）；写用 REPLACE、关外键检查
    （见 youju/sync/__init__.py）。"""
    full = payload.get("full") is True
    parts = _decoded(payload.get("tables"))
    local_has = payload.get("blobs_local") or []
    if not isinstance(local_has, list) or not all(isinstance(p, str) for p in local_has):
        raise ValueError("blobs_local 要是路径的列表")
    ensure_state(engine)
    kept: dict[str, int] = {}
    with engine.connect() as conn, conn.begin():
        _take_turn(conn, stamp)
        missing, nowhere = _missing_blobs(parts, set(local_has))
        if missing:
            raise MissingBlobs(missing)
        _owned_clash(conn, parts)
        noted = datetime.now(UTC).replace(tzinfo=None)
        for path in nowhere:
            conn.execute(
                text(f"INSERT IGNORE INTO `{PENDING}` (path, noted_at) VALUES (:p, :u)"),
                {"p": path, "u": noted},
            )
        if not full:
            row = conn.execute(
                text(f"SELECT stream, applied_seq FROM `{STATE}` WHERE name = 'public' FOR UPDATE")
            ).first()
            if row is None or row.stream != payload.get("stream"):
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
        _record_turn(conn, stamp)
        seq = payload.get("max_seq")
        if not full and type(seq) is int:
            conn.execute(
                text(
                    f"UPDATE `{STATE}` SET applied_seq = GREATEST(applied_seq, :s),"
                    " updated_at = :u WHERE name = 'public'"
                ),
                {"s": seq, "u": datetime.now(UTC).replace(tzinfo=None)},
            )
    if old_paths:
        _collect_garbage(engine, old_paths)
    state = read_state(engine)
    return {"applied_seq": state["applied_seq"], "kept": kept}


def _delete(conn: Connection, t: Table, pks: list[Any]) -> int:
    """删这些主键的行；被用户数据（RESTRICT 外键）挡住的逐行留下，返回留下的行数。
    服务器自己的行（SERVER_OWNED，如用户线索生成的截止日期）一律不删。"""
    (col,) = pk_columns(t)
    own = SERVER_OWNED.get(t.name)

    def stmt(condition):
        query = t.delete().where(condition)
        return query.where(own(t)) if own else query

    try:
        with conn.begin_nested():
            for start in range(0, len(pks), 1000):
                conn.execute(stmt(t.c[col].in_(pks[start : start + 1000])))
        return 0
    except IntegrityError:
        pass
    lost = 0
    for pk in pks:
        try:
            with conn.begin_nested():
                conn.execute(stmt(t.c[col] == pk))
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
    # 和写入排同一把锁：数引用到删文件之间，不会有新的一批指向它（写入时也在锁下核对文件在不在）
    with engine.connect() as conn, conn.begin():
        _take_turn(conn, 0)
        for path in paths:
            used = conn.scalar(
                select(func.count()).select_from(t).where(t.c.file_id.like(f"cloud://%/{path}"))
            )
            if not used:
                # 没人引用了：待补里有的也划掉，以后不会再把它补传上来
                conn.execute(text(f"DELETE FROM `{PENDING}` WHERE path = :p"), {"p": path})
                try:
                    store.delete(path)
                except (storage.StorageError, OSError):
                    log.warning("删除没人用的附件副本失败：%s", path)


# ---- 接口 ----


@router.get("/state")
async def state(request: Request) -> dict:
    await _body(request)
    return read_state(get_engine(), with_ids=True)


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
    if t.name in SERVER_OWNED:
        # 只在服务器上有的行（用户线索生成的截止日期）：不列出来，全推时就不会被当成多余的删掉
        stmt = stmt.where(SERVER_OWNED[t.name](t))
    if after is not None:
        stmt = stmt.where(t.c[col] > (int(after) if after.lstrip("-").isdigit() else after))
    with get_engine().connect() as conn:
        return {"pks": list(conn.scalars(stmt))}


@router.post("/apply")
async def apply_batch(request: Request) -> dict:
    payload = _json(await _body(request))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="包体格式不对")
    # 签名已经核对过（_body），时间是 13 位的毫秒数
    stamp = sync_tables.stamp_ms(request.headers.get(sync_tables.HEADER_TIME, ""))
    try:
        return await run_in_threadpool(lambda: apply(get_engine(), payload, stamp=stamp))
    except Stale as exc:
        detail = {
            "message": "这一批比服务器已经写过的旧（重放、或卡在半路晚到的请求），没有写；"
            "本机时钟往回调过的话，等时钟走过上次推的时间再推",
            "last_stamp": exc.last,
        }
        raise HTTPException(status_code=409, detail=detail) from None
    except OwnedClash as exc:
        detail = {
            "message": f"本机推来的 {exc.table} 和服务器自己的行编号撞了，"
            "没有写（不盖掉服务器的行）；按手册查两边的编号",
            "ids": exc.ids[:20],
        }
        raise HTTPException(status_code=409, detail=detail) from None
    except MissingBlobs as exc:
        detail = {
            "message": "这一批指向的附件副本文件不在服务器上（推之前查过在、随后被清理删了），"
            "没有写；下一轮推会先补传再写",
            "paths": exc.paths[:20],
        }
        raise HTTPException(status_code=409, detail=detail) from None
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
    # 和写入排同一把锁、按签名里的时间排先后：重放的旧 reset 不会把变更流改回去
    stamp = sync_tables.stamp_ms(request.headers.get(sync_tables.HEADER_TIME, ""))
    try:
        with engine.begin() as conn:
            _take_turn(conn, stamp)
            conn.execute(
                text(
                    f"INSERT INTO `{STATE}` (name, stream, applied_seq, updated_at)"
                    " VALUES ('public', :st, :s, :u)"
                    " ON DUPLICATE KEY UPDATE stream = :st, applied_seq = :s, updated_at = :u"
                ),
                {"st": stream, "s": seq, "u": datetime.now(UTC).replace(tzinfo=None)},
            )
            _record_turn(conn, stamp)
    except Stale as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "这个请求比服务器已经写过的旧（重放），没有改",
                "last_stamp": exc.last,
            },
        ) from None
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


@router.post("/blobs/pending")
async def pending_blobs(request: Request) -> dict:
    """待补的附件副本（写进来时两边都没有文件的），按路径分页：after 之后的 limit 个里，已经到了的
    （本机补传的、首发前从云托管导进来的）先划掉，剩下的给本机，本机手上有的就补传。还有下一页时
    next 是这一页最后一个路径（本机接着取，所以前面一直缺的不会挡住后面的）。"""
    payload = _json(await _body(request))
    payload = payload if isinstance(payload, dict) else {}
    limit, after = payload.get("limit", 500), payload.get("after") or ""
    if type(limit) is not int or not 1 <= limit <= 2000 or not isinstance(after, str):
        raise HTTPException(status_code=422, detail="limit、after 不对")
    engine = get_engine()
    ensure_state(engine)
    with engine.begin() as conn:
        # 和写入、清理排同一把锁：查文件在不在到划掉之间，不会被清理删掉文件、又被新的一批记进待补
        _take_turn(conn, 0)
        paths = list(
            conn.scalars(
                text(f"SELECT path FROM `{PENDING}` WHERE path > :a ORDER BY path LIMIT :n"),
                {"a": after, "n": limit},
            )
        )
        if not paths:
            return {"pending": [], "next": None}
        store = storage.backend()
        arrived = [p for p in paths if store.exists(p)]
        for path in arrived:
            conn.execute(text(f"DELETE FROM `{PENDING}` WHERE path = :p"), {"p": path})
    return {
        "pending": [p for p in paths if p not in arrived],
        "next": paths[-1] if len(paths) == limit else None,
    }


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
    engine = get_engine()
    ensure_state(engine)
    with engine.begin() as conn:
        # 同一把锁下确认文件确实在，再划掉待补（写文件不在锁下：大文件不占着锁）
        _take_turn(conn, 0)
        if storage.backend().exists(path):
            conn.execute(text(f"DELETE FROM `{PENDING}` WHERE path = :p"), {"p": path})
    return {"stored": path}
