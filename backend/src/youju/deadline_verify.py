"""D-130：生成待核对清单，校验证据后写入核对结果。"""

import hashlib
import re
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import select

from youju import deadline_excerpts as excerpts
from youju.models import Document, DocumentDeadline, DocumentDetail


def body_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _without_space(value):
    return re.sub(r"\s+", "", value)


def _body_quote(body, quote):
    positions = [i for i, char in enumerate(body) if not char.isspace()]
    normalized = "".join(body[i] for i in positions)
    needle = _without_space(quote)
    start = normalized.find(needle)
    if start < 0:
        return None
    return body[positions[start] : positions[start + len(needle) - 1] + 1]


# 原句里的时刻：抽取截止时认的写法（extract/deadlines.py 的 _TIME："17:00""17：00""17时"
# "17 时 30 分"）再加上"点"（AI 找到的截止常写"下午5点"）；前面可以带上午、下午、中午、晚上。
# 冒号后面要有两位分钟（带秒的只认整分，"17:00:00"）；"时""点"后面的分钟要带"分"，免得把后面的
# "12月"当成分钟；"5点半"不认。时刻前面可以是"截止时间："这类冒号，不能是数字或"数字加冒号"
# （"17:00:30"里的"00:30"不算一个时刻）
_CLOCK = re.compile(
    r"(?:(?P<period>上午|下午|中午|晚上)\s*)?(?<!\d)(?<!\d[:：])(?P<hour>[01]?\d|2[0-3])\s*"
    r"(?:[:：]\s*(?P<minute>[0-5]\d)(?:[:：](?P<second>[0-5]\d))?\s*分?"
    r"|[时点](?:\s*(?P<cnminute>[0-5]\d)\s*分)?)"
    r"(?![\d半])"
)


class _Clock:
    """把 _CLOCK 的结果交给抽取用的 _clock（它按 hour、minute 两组取值）。"""

    def __init__(self, found):
        self._groups = {
            "hour": found.group("hour"),
            "minute": found.group("minute") or found.group("cnminute"),
        }

    def group(self, name):
        return self._groups[name]


def _has_time(quote, clock):
    """原句里写着这个时刻（换算上午、下午和抽取截止时同一个 _clock）。"""
    from youju.extract.deadlines import _clock

    return any(
        found.group("second") in (None, "00")
        and _clock(_Clock(found), found.group("period")) == clock
        for found in _CLOCK.finditer(quote)
    )


def _reopened_same_deadline(session, row):
    """归并组里同一截止（类型、日期、时间相同；证据句可以不同）被质检退回、还待核对的各条。
    退回时整组同一截止都会退回，页面上也只显示一个，所以退回原因按这一截止整组看。"""
    from youju.dedup import canonical_ids, groups_for

    master = canonical_ids(session, [row.document_id]).get(row.document_id, row.document_id)
    members = groups_for(session, [master])[master]
    return list(
        session.scalars(
            select(DocumentDeadline)
            .where(
                DocumentDeadline.document_id.in_(members),
                DocumentDeadline.kind == row.kind,
                DocumentDeadline.candidate_date == row.candidate_date,
                DocumentDeadline.candidate_time.is_(None)
                if row.candidate_time is None
                else DocumentDeadline.candidate_time == row.candidate_time,
                DocumentDeadline.status == "pending",
                DocumentDeadline.audit_state == "reopened",
            )
            .order_by(DocumentDeadline.id)
            .execution_options(populate_existing=True)
        )
    )


def _reopen_note(row):
    return f"质检退回原因：{row.audit_note}" if row.audit_note else "质检退回（没写原因）"


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
        # batch 判"7 天内"时这一类只有一个日期、在今天到 7 天后之间；这里再按 --days 收窄
        if not today <= row.candidate_date <= today + timedelta(days=days):
            continue
        doc = session.get(Document, row.document_id)
        detail = session.get(DocumentDetail, row.document_id)
        if not detail or detail.status != "ok":
            continue
        body = detail.body_text or ""
        digest = body_hash(body)
        if digest == row.verification_unclear_hash:
            continue
        # 同一截止同组里有退回的：每条都带上退回原因，核哪一条都看得到
        reopened = _reopened_same_deadline(session, row)
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
                reopened=bool(reopened),
                audit_note="；".join(_reopen_note(r) for r in reopened) or None,
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
    if set(item) - {
        "id",
        "verdict",
        "quote",
        "date",
        "time",
        "note",
        "body_sha256",
        "document_id",
        "title",
        "url",
        "kind",
        "evidence",
        "origin",
        "ai_agrees",
        "sentences",
        "reopened",
        "audit_note",
    }:
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
    reopened = _reopened_same_deadline(session, row) if verdict != "unclear" else []
    if reopened:
        # 结果要来自带着当前退回原因的清单：reopened 为 true、audit_note 和现在整组的退回原因一样；
        # 出清单以后又有退回的，等下一份清单。还要在 note 里写怎么处理了退回原因，记进留痕
        if item.get("reopened") is not True:
            return "这一截止同组有质检退回，须按带退回原因的清单核对"
        if item.get("audit_note") != "；".join(_reopen_note(r) for r in reopened):
            return "退回原因和清单里的不一样（出清单以后又有退回），等下一份清单"
        if not item.get("note", "").strip():
            return "同组有质检退回，须在 note 里写怎么处理了退回原因"
    if row.kind not in excerpts.DEADLINE_KINDS and verdict != "unclear":
        return "其他类不能通过"
    stmt = select(DocumentDetail).where(DocumentDetail.document_id == row.document_id)
    # 读最新的正文，别用会话里上一条留下的旧值（--apply 每条提交，会话不过期）
    detail = session.scalar(
        (stmt.with_for_update() if write else stmt).execution_options(populate_existing=True)
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
    if not 12 <= len(_without_space(quote)) <= 300:
        return "quote 去空白后须为 12 至 300 字"
    quote = _body_quote(body, quote)
    if quote is None:
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
        if (day, clock) == (row.candidate_date, row.candidate_time):
            return "corrected 的日期时间和候选一样，应写 confirmed"
    if not excerpts.date_in_quote(day, quote):
        return "quote 里的日期不匹配"
    if clock is not None and not _has_time(quote, clock):
        return "原句里没有时间"
    # 同组副本也会公开；只有每份当前正文都包含这句证据才一起通过。
    twin_quotes = {row.id: quote}
    for twin in locked:
        if twin.id == row.id or twin.status != "pending":
            continue
        twin_detail = session.scalar(
            select(DocumentDetail)
            .where(DocumentDetail.document_id == twin.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        twin_quote = (
            _body_quote(twin_detail.body_text or "", quote)
            if twin_detail and twin_detail.status == "ok"
            else None
        )
        if twin_quote is None:
            return "同组副本正文不含 quote，未写入"
        twin_quotes[twin.id] = twin_quote
    if write:
        stamp = datetime.now(UTC)
        # _apply_review 会清空质检信息；同组每条各自保存自己的退回原因。
        # _apply_review 会清掉质检标记；退回过的各自留一笔（没写原因的也留）
        handled = item.get("note", "").strip()
        reopen_notes = {
            d.id: f"{_reopen_note(d)}；Claude 处理：{handled}" if handled else _reopen_note(d)
            for d in locked
            if d.status == "pending" and d.audit_state == "reopened"
        }
        _apply_review(row, locked, "approved", "Claude", stamp, day, clock)
        for item_row in locked:
            if item_row.reviewed_by == "Claude" and item_row.reviewed_at == stamp:
                item_row.verification_quote = twin_quotes[item_row.id]
                item_row.verification_corrected = verdict == "corrected"
                if item_row.id in reopen_notes:
                    item_row.verification_unclear_note = reopen_notes[item_row.id]
    return "通过并修改日期" if verdict == "corrected" else "通过"


def stats(session, since=None):
    """统计现存状态；同次通过的同组同候选及后来沿用的副本只计一次。"""
    from youju.dedup import canonical_ids

    stmt = (
        select(DocumentDeadline)
        .where(
            ((DocumentDeadline.status == "approved") & (DocumentDeadline.reviewed_by == "Claude"))
            | (
                (DocumentDeadline.status == "pending")
                & DocumentDeadline.verification_unclear_at.is_not(None)
            )
        )
        .order_by(DocumentDeadline.id)
    )
    rows = list(session.scalars(stmt))
    masters = canonical_ids(session, list({row.document_id for row in rows}))
    groups = {}
    for row in rows:
        unclear = row.status == "pending"
        stamp = row.verification_unclear_at if unclear else row.reviewed_at
        if since and (
            stamp is None
            or stamp
            < datetime.combine(since, time.min, timezone(timedelta(hours=8))).astimezone(UTC)
        ):
            continue
        verdict = (
            "unclear" if unclear else "corrected" if row.verification_corrected else "confirmed"
        )
        # 看不准只写一条；通过的副本沿用同一时间、类型、候选日期和时间。
        key = (
            (row.id,)
            if unclear or stamp is None
            else (
                masters.get(row.document_id, row.document_id),
                stamp,
                row.kind,
                row.candidate_date,
                row.candidate_time,
                verdict,
            )
        )
        groups.setdefault(key, []).append(row)
    result = {
        name: {"confirmed": 0, "corrected": 0, "unclear": 0}
        for name in ("total", "ai_agrees", "other")
    }
    for group in groups.values():
        row = group[0]
        verdict = (
            "unclear"
            if row.status == "pending"
            else "corrected"
            if row.verification_corrected
            else "confirmed"
        )
        bucket = "ai_agrees" if all(r.ai_agrees is True for r in group) else "other"
        result["total"][verdict] += 1
        result[bucket][verdict] += 1
    for counts in result.values():
        n = counts["confirmed"] + counts["corrected"]
        counts["accuracy"] = counts["confirmed"] / n if n else None
    return result
