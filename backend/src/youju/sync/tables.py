"""同步哪些表、行怎么编码（本机和服务器共用）。"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Table

from youju.backup import _decoder
from youju.models import Base
from youju.public_snapshot import PUBLIC_TABLES

# 公开快照的表，加上下架、恢复的操作记录：下架在本机核对台做（公开数据的主库在本机），
# 服务器靠 documents.taken_down_at 隐藏，各处缓存按操作记录的编号和时间失效
# （takedown.cache_revision），所以操作记录也要过去；记录里的备注（可能写到投诉人）不传。
# 首发前搬家带过来的已下架网址（takedown_blocks，D-127）也过去：本机重建、从服务器导回时不丢
SYNC_TABLES = frozenset(PUBLIC_TABLES | {"document_takedown_events", "takedown_blocks"})
# 传之前清空的字段：人工填写、可能写到投诉人的备注（可为空的写 NULL，不可为空的写空串）
CLEARED_COLUMNS: dict[str, tuple[str, ...]] = {
    "documents": ("takedown_note", "restore_note"),
    "document_takedown_events": ("note",),
}
# 存附件副本文件 ID 的字段：服务器存储里要有这些文件
BLOB_COLUMNS: dict[str, tuple[str, ...]] = {"attachment_copies": ("file_id",)}


def tables() -> list[Table]:
    """按外键依赖排好（父表在前）。"""
    return [t for t in Base.metadata.sorted_tables if t.name in SYNC_TABLES]


def table(name: str) -> Table:
    if name not in SYNC_TABLES:
        raise KeyError(name)
    return Base.metadata.tables[name]


def pk_columns(t: Table) -> list[str]:
    return [c.name for c in t.primary_key.columns]


def _encode(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError(f"不支持的类型：{type(value).__name__}")


def encode_row(t: Table, row: dict) -> dict:
    """本机读出的一行 → 可以 JSON 序列化的 dict（清掉备注）。"""
    row = dict(row)
    for name in CLEARED_COLUMNS.get(t.name, ()):
        if name in row and row[name] is not None:
            row[name] = None if t.c[name].nullable else ""
    return row


def decode_row(t: Table, row: Any) -> dict:
    """服务器收到的一行 → 写库用的 dict。字段必须和这个版本的表结构完全一致。"""
    if not isinstance(row, dict) or set(row) != {c.name for c in t.columns}:
        raise ValueError(f"{t.name} 的字段和这个版本的表结构不一致")
    out = dict(row)
    for column in t.columns:
        decode = _decoder(column)
        if decode is not None and out[column.name] is not None:
            out[column.name] = decode(out[column.name])
    for name in CLEARED_COLUMNS.get(t.name, ()):
        if out[name] is not None:
            out[name] = None if t.c[name].nullable else ""
    return out


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=_encode, separators=(",", ":")).encode()


# ---- 请求签名（本机和服务器用同一个 YOUJU_SYNC_SECRET） ----
# 签名算在"方法、路径加查询串、时间、包体的 SHA-256"上，包体的 SHA-256 另放一个请求头：
# 服务器先只看请求头验签，签名对了才读包体（不让没签名的请求塞几十 MB 进来占内存），
# 读完再核对包体和头里的 SHA-256 一致

HEADER_TIME = "X-Youju-Sync-Time"
HEADER_SIGNATURE = "X-Youju-Sync-Signature"
HEADER_BODY = "X-Youju-Sync-Body-Sha256"
WINDOW_SECONDS = 300


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature(secret: str, method: str, target: str, stamp: str, digest: str) -> str:
    """target 是路径加查询串（如 /api/sync/v1/pks?table=documents&after=0）；digest 是包体的
    SHA-256（十六进制）。"""
    message = "\n".join([method.upper(), target, stamp, digest])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def signature_ok(
    secret: str, method: str, target: str, stamp: str, digest: str, given: str, now: float
) -> bool:
    try:
        when = int(stamp)
    except ValueError:
        return False
    if abs(now - when) > WINDOW_SECONDS:
        return False
    if len(digest) != 64:
        return False
    expected = signature(secret, method, target, stamp, digest)
    return bool(given) and hmac.compare_digest(expected, given)
