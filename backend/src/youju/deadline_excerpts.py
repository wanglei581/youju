"""公开截止候选的批量判定；调用方决定是否启用摘录口径。

D-061：机器摘录只在 8–30 天到期时给日期（7 天内只给人工核对过的，30 天以后写"截止未核对"），
日期必须写在原句里，原句必须在当前正文里；同一类型几个日期对不上、已过期的都不给日期。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date, time, timedelta
from functools import lru_cache
from hashlib import sha256
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.dedup import groups_for
from youju.models import DocumentDeadline, DocumentDetail

ORDER = {"企业申报截止": 0, "材料报送截止": 1, "推荐或审核截止": 2}
EXCERPT_FIRST_DAY = 8
EXCERPT_LAST_DAY = 30
DATE_IN_QUOTE = re.compile(
    r"(?<!\d)(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"
    r"|(?<!\d)(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2})(?!\d)"
)
_body_cache: OrderedDict[tuple[int, str], str] = OrderedDict()
_body_cache_bytes = 0
_body_cache_lock = Lock()
_BODY_CACHE_LIMIT = 8 * 1024 * 1024
_check_cache: OrderedDict[tuple[int, str, str], bool] = OrderedDict()
_CHECK_CACHE_LIMIT = 4096


def _normal(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _normal_body(doc_id: int, body: str) -> str:
    """正文指纹作键；正文一变即重算，缓存总量受限。"""
    global _body_cache_bytes
    key = (doc_id, sha256(body.encode()).hexdigest())
    with _body_cache_lock:
        cached = _body_cache.get(key)
        if cached is not None:
            _body_cache.move_to_end(key)
            return cached
    normalized = _normal(body)
    size = len(normalized.encode())
    if size <= _BODY_CACHE_LIMIT:
        with _body_cache_lock:
            old = _body_cache.pop(key, None)
            if old is not None:
                _body_cache_bytes -= len(old.encode())
            _body_cache[key] = normalized
            _body_cache_bytes += size
            while _body_cache_bytes > _BODY_CACHE_LIMIT:
                _, dropped = _body_cache.popitem(last=False)
                _body_cache_bytes -= len(dropped.encode())
    return normalized


@lru_cache(maxsize=16384)
def date_in_quote(day: date, quote: str) -> bool:
    """年份写明时必须相同；未写年份时只核对月日。"""
    for match in DATE_IN_QUOTE.finditer(_normal(quote)):
        year, month, dom = match.group(1, 2, 3) if match.group(2) else match.group(4, 5, 6)
        if (
            int(month) == day.month
            and int(dom) == day.day
            and (year is None or int(year) == day.year)
        ):
            return True
    return False


@dataclass(frozen=True)
class View:
    kind: str
    source: str
    day: date | None = None
    clock: time | None = None
    quote: str | None = None
    record_id: int | None = None

    def public(self, today: date) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "date": self.day.isoformat() if self.day else None,
            "time": self.clock.strftime("%H:%M") if self.clock else None,
            "remainingDays": (self.day - today).days if self.day else None,
            "quote": self.quote,
        }


def selected(views: list[View]) -> View | None:
    """已核对优先；同一来源内企业申报优先，否则最早截止。"""
    visible = [view for view in views if view.source in ("verified", "excerpt")]
    if not visible:
        return None
    verified = [view for view in visible if view.source == "verified"]
    pool = verified or visible
    enterprise = [view for view in pool if view.kind == "企业申报截止"]
    return min(
        enterprise or pool,
        key=lambda view: (view.day or date.max, view.clock or time.max, view.record_id or 0),
    )


def batch(
    session: Session,
    doc_ids: list[int],
    approved: dict[int, list[DocumentDeadline]],
    today: date,
    members: dict[int, list[int]] | None = None,
) -> tuple[dict[int, list[View]], dict[int, bool], Counter[str]]:
    """一次取归并组候选和当前正文；每个候选必须匹配它所属文件的正文。"""
    if not doc_ids:
        return {}, {}, Counter()
    members = members if members is not None else groups_for(session, doc_ids)
    all_ids = list({member for ids in members.values() for member in ids})
    rows = session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id.in_(all_ids),
            DocumentDeadline.status.in_(("pending", "changed")),
        )
    ).all()
    eligible = [
        row
        for row in rows
        if row.status == "pending"
        and today + timedelta(days=EXCERPT_FIRST_DAY)
        <= row.candidate_date
        <= today + timedelta(days=EXCERPT_LAST_DAY)
        and date_in_quote(row.candidate_date, row.evidence or "")
    ]
    eligible_ids = {row.document_id for row in eligible}
    body_hashes = (
        dict(
            session.execute(
                select(DocumentDetail.document_id, func.sha2(DocumentDetail.body_text, 256)).where(
                    DocumentDetail.document_id.in_(eligible_ids), DocumentDetail.status == "ok"
                )
            ).all()
        )
        if eligible_ids
        else {}
    )
    matches: dict[int, bool] = {}
    missing: list[tuple[DocumentDeadline, tuple[int, str, str]]] = []
    with _body_cache_lock:
        for row in eligible:
            key = (row.id, body_hashes.get(row.document_id) or "", row.evidence or "")
            if key in _check_cache:
                matches[row.id] = _check_cache[key]
                _check_cache.move_to_end(key)
            else:
                missing.append((row, key))
    missing_ids = {row.document_id for row, _ in missing if row.document_id in body_hashes}
    bodies = (
        dict(
            session.execute(
                select(DocumentDetail.document_id, DocumentDetail.body_text).where(
                    DocumentDetail.document_id.in_(missing_ids), DocumentDetail.status == "ok"
                )
            ).all()
        )
        if missing_ids
        else {}
    )
    normalized_bodies = {
        doc_id: _normal_body(doc_id, body or "") for doc_id, body in bodies.items()
    }
    for row, key in missing:
        matches[row.id] = bool(
            (quote := _normal(row.evidence or ""))
            and quote in normalized_bodies.get(row.document_id, "")
        )
        with _body_cache_lock:
            _check_cache[key] = matches[row.id]
            _check_cache.move_to_end(key)
            if len(_check_cache) > _CHECK_CACHE_LIMIT:
                _check_cache.popitem(last=False)
    by_member: dict[int, list[DocumentDeadline]] = {}
    for row in rows:
        by_member.setdefault(row.document_id, []).append(row)
    output: dict[int, list[View]] = {}
    soon: dict[int, bool] = {}
    reasons: Counter[str] = Counter()
    for doc_id, ids in members.items():
        verified: dict[str, View] = {}
        for row in approved.get(doc_id, []):
            if row.approved_date and row.kind not in verified:
                verified[row.kind] = View(
                    row.kind,
                    "verified",
                    row.approved_date,
                    row.approved_time,
                    row.evidence or None,
                    row.id,
                )
        changed = {
            (row.kind, row.approved_date)
            for member in ids
            for row in by_member.get(member, [])
            if row.status == "changed"
        }
        pending: dict[str, list[DocumentDeadline]] = {}
        for member in ids:
            for row in by_member.get(member, []):
                if row.status == "pending" and (row.kind, row.candidate_date) not in changed:
                    pending.setdefault(row.kind, []).append(row)
        soon[doc_id] = any(
            kind not in verified and today <= row.candidate_date <= today + timedelta(days=7)
            for kind, candidates in pending.items()
            for row in candidates
        )
        views = list(verified.values())
        for kind, candidates in pending.items():
            if kind in verified:
                continue
            dates = {row.candidate_date for row in candidates}
            reason = None
            if len(dates) != 1:
                reason = "冲突"
            else:
                day = next(iter(dates))
                if day < today:
                    reason = "过期"
                elif day < today + timedelta(days=EXCERPT_FIRST_DAY):
                    reason = "7 天内"
                elif day > today + timedelta(days=EXCERPT_LAST_DAY):
                    reason = "30 天以后"
                else:
                    valid = [row for row in candidates if date_in_quote(day, row.evidence or "")]
                    if not valid:
                        reason = "日期不在原句里"
                    else:
                        valid = [row for row in valid if matches.get(row.id, False)]
                        if not valid:
                            reason = "原句不在正文里"
                        else:
                            row = min(valid, key=lambda item: item.id)
                            views.append(
                                View(kind, "excerpt", day, row.candidate_time, row.evidence, row.id)
                            )
            if reason:
                reasons[reason] += 1
                views.append(View(kind, "unverified"))
        output[doc_id] = sorted(
            views, key=lambda view: (ORDER.get(view.kind, 9), view.day or date.max)
        )
    return output, soon, reasons
