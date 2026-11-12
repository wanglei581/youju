"""D-130：生成待核对清单，校验证据后写入核对结果。"""

import hashlib
import re
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select

from youju import deadline_excerpts as excerpts
from youju.models import Document, DocumentDeadline, DocumentDetail


def body_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _without_space(value):
    return re.sub(r"\s+", "", value)


def queue(session, today, days=7):
    from youju.api import v1
    from youju.classify import effective_kind

    if not 0 <= days <= 7:
        raise ValueError("days 必须在 0 至 7 之间")
    ids = [
        d.id
        for d, _ in session.execute(v1._base())
        if effective_kind(d.title, d.kind_override) == "申报通知"
    ]
    blocked = []
    excerpts.batch(session, ids, v1._approved(session, ids), today, blocked_soon=blocked)
    result = []
    for row in {r.id: r for r in blocked}.values():
        if row.candidate_date > today + timedelta(days=days):
            continue
        doc = session.get(Document, row.document_id)
        detail = session.get(DocumentDetail, row.document_id)
        if not detail or detail.status != "ok":
            continue
        body = detail.body_text or ""
        digest = body_hash(body)
        if digest == row.verification_unclear_hash:
            continue
        sentences = [
            s.strip()[:300]
            for s in re.split(r"(?<=[。！？；;\n])", body)
            if excerpts.date_in_quote(row.candidate_date, s)
        ][:5]
        result.append(
            dict(
                id=row.id,
                document_id=doc.id,
                title=doc.title,
                url=doc.url,
                kind=row.kind,
                date=row.candidate_date.isoformat(),
                time=row.candidate_time.isoformat(timespec="minutes")
                if row.candidate_time
                else None,
                evidence=row.evidence,
                origin=row.origin,
                ai_agrees=row.ai_agrees,
                sentences=sentences,
                body_sha256=digest,
            )
        )
    return sorted(result, key=lambda r: (r["date"], r["time"] or "", r["id"]))


def apply_one(session, item, write=False):
    """逐条处理；写入调用方负责提交。"""
    from youju.api.app import _apply_review, _lock_candidates

    if not isinstance(item, dict) or type(item.get("id")) is not int:
        return "格式错误：id 必须是整数"
    if set(item) - {"id", "verdict", "quote", "date", "time", "note", "body_sha256"}:
        return "格式错误：含不支持的字段"
    verdict = item.get("verdict")
    if verdict not in ("confirmed", "corrected", "unclear"):
        return "verdict 无效"
    if not isinstance(item.get("note", ""), str):
        return "note 必须是字符串"
    row = session.get(DocumentDeadline, item["id"])
    if row is None:
        return "截止不存在"
    locked = _lock_candidates(session, row)
    if row.status != "pending":
        return "状态已变，不再待核对"
    if row.kind not in excerpts.DEADLINE_KINDS and verdict != "unclear":
        return "其他类不能通过"
    detail = session.scalar(
        select(DocumentDetail)
        .where(DocumentDetail.document_id == row.document_id)
        .with_for_update()
        if write
        else select(DocumentDetail).where(DocumentDetail.document_id == row.document_id)
    )
    if detail is None or detail.status != "ok":
        return "当前正文不可用"
    body = detail.body_text or ""
    if item.get("body_sha256") != body_hash(body):
        return "正文指纹不一致"
    if verdict == "unclear":
        if not item.get("note", "").strip():
            return "看不准须说明原因"
        if write:
            row.verification_unclear_hash = body_hash(body)
            row.verification_unclear_note = item["note"].strip()
            row.verification_unclear_at = datetime.now(UTC)
        return "记录看不准"
    quote = item.get("quote")
    if not isinstance(quote, str) or not _without_space(quote):
        return "缺少 quote"
    if _without_space(quote) not in _without_space(body):
        return "quote 不在当前正文"
    day, clock = row.candidate_date, row.candidate_time
    if verdict == "corrected":
        try:
            if "time" not in item:
                raise ValueError
            day = date.fromisoformat(item["date"])
            clock = time.fromisoformat(item["time"]) if item["time"] is not None else None
            if clock and (clock.tzinfo or clock.second or clock.microsecond):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            return "corrected 须提供有效 date 和 time（可为 null）"
    if not excerpts.date_in_quote(day, quote):
        return "quote 里的日期不匹配"
    # 同组副本也会公开；只有每份当前正文都包含这句证据才一起通过。
    for twin in locked:
        if twin.id == row.id or twin.status != "pending":
            continue
        twin_detail = session.scalar(
            select(DocumentDetail)
            .where(DocumentDetail.document_id == twin.document_id)
            .with_for_update()
        )
        if (
            not twin_detail
            or twin_detail.status != "ok"
            or _without_space(quote) not in _without_space(twin_detail.body_text or "")
        ):
            return "同组副本正文不含 quote，未写入"
    if write:
        stamp = datetime.now(UTC)
        _apply_review(row, locked, "approved", "Claude", stamp, day, clock)
        for twin in locked:
            if twin.reviewed_by == "Claude" and twin.reviewed_at == stamp:
                twin.verification_quote = quote
                twin.verification_corrected = verdict == "corrected"
    return "通过并修改日期" if verdict == "corrected" else "通过"
