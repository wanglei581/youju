"""Strict database revision and query-bound authenticated lookup cursors."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.lists.pagination import LOOKUP_VERSION, LookupInputError
from youju.models import LookupRevision


class LookupChanged(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    revision: str
    key: bytes


def snapshot(session: Session) -> Snapshot:
    row = session.execute(select(LookupRevision.revision, LookupRevision.cursor_key)).one_or_none()
    if row is None:
        raise RuntimeError("名单分页修订表尚未准备完成")
    key = bytes.fromhex(row.cursor_key)
    epoch = hashlib.sha256(key).hexdigest()[:16]
    return Snapshot(f"{LOOKUP_VERSION}-{epoch}-{row.revision}", key)


def encode_cursor(state: Snapshot, query: str, identity: str | None, after) -> str:
    payload = json.dumps(
        [LOOKUP_VERSION, state.revision, query, identity, after],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(state.key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature + payload).rstrip(b"=").decode()


def decode_cursor(
    state: Snapshot, query: str, identity: str | None, cursor: str | None, revision: str | None
):
    if cursor and revision is None:
        raise LookupInputError("续页必须携带修订号，请重新查询")
    if revision is not None and revision != state.revision:
        raise LookupChanged("名单数据已更新，请重新查询")
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        signature, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(
            signature, hmac.new(state.key, payload, hashlib.sha256).digest()
        ):
            raise ValueError
        version, saved_revision, saved_query, saved_identity, after = json.loads(payload)
        if [version, saved_revision, saved_query, saved_identity] != [
            LOOKUP_VERSION,
            revision,
            query,
            identity,
        ]:
            raise ValueError
        return after
    except (ValueError, TypeError, UnicodeError) as exc:
        raise LookupInputError("翻页位置与查询不符，请重新查询") from exc
