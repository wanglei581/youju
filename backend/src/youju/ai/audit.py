"""AI 质检员（D-023）：每天复查已经发布给用户的截止日期，存疑的标出来、提醒到群里，由人复核。

- 规则回查（不用 AI，每次全量）：截止早于发布日期；正文缺失、没法回查；正文里根本没写这个日期。
- AI 抽查（配了 AI 时）：每次抽一部分文件，让 AI 对照原文判断。AI 说有误时要照抄原文里的那句话：
  这句话确实在原文里、也写着 AI 说的日期，才算存疑；对不上的只记下、不标，免得 AI 编出来的理由误报。
  同一条截止 30 天内不重复抽；AI 出错的文件隔一天再试，连续出错就停。
- 只标记、不自动撤下：撤下（退回待核对）由人在核对台决定。人确认无误的不再标。
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from youju.ai.client import AIError, ChatClient
from youju.ai.deadlines import _compact, _date_span
from youju.dedup import visible_documents
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail

# 同一条截止隔多久再让 AI 抽查；AI 出错的文件隔多久再试
REAUDIT_AFTER = timedelta(days=30)
RETRY_AFTER = timedelta(days=1)
# 连续这么多份文件调用 AI 出错就停（多半是服务不可用或额度用完）
MAX_CONSECUTIVE_ERRORS = 3
# 发给 AI 的原文：证据句前后各取这么多字；每份文件的片段合起来不超过这么多字，一次最多查几条截止
_CONTEXT = 300
_MAX_EXCERPT = 3000
_MAX_ROWS = 8
# AI 照抄的原文句子至少这么长（去掉空白后），太短的对得上也说明不了什么
_MIN_QUOTE = 8

_SYSTEM = """你是截止日期质检员。必须只返回 JSON 对象，字段 items 为数组。
下面给出一份政府通知的相关原文，和已经人工核对通过的若干截止（编号、类型、日期、时刻）。
对每条截止判断：原文是否写着这一类事项在这一天（这个时刻）截止。只有原文明确写着别的日期或时刻、
或原文写明这一天截止的是别的事项时，ok 才写 false；拿不准就写 true。
每项字段：id（给出的编号）、ok（true 或 false）、
quote（ok 为 false 时必填：从原文照抄说明这项截止的那句话，不超过 80 字，不要改写）、
found_date（quote 里写的日期，YYYY-MM-DD，没有就空）、reason（不超过 40 字）。"""


@dataclass
class AuditReport:
    checked_by_rules: int = 0
    # AI 抽查了几份文件
    checked_by_ai: int = 0
    # （截止编号, 标题, 原因）
    flagged: list[tuple[int, str, str]] = field(default_factory=list)
    # AI 说有误、但照抄的原文对不上的条数（不标存疑）
    unverified: int = 0
    errors: int = 0
    stopped_early: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


def rule_note(deadline: DocumentDeadline, publish_date: date | None, body: str) -> str | None:
    """规则回查：返回存疑原因，没问题返回 None。"""
    due = deadline.approved_date
    if due is None:
        return None
    if publish_date is not None and due < publish_date:
        return f"截止日期 {due} 早于发布日期 {publish_date}"
    if not body:
        return "正文缺失（可能是重抓失败），没法回查，请打开原文核对"
    if _date_span(body, due) is None:
        return f"正文里找不到 {due.year}年{due.month}月{due.day}日"
    return None


def _flag(row: DocumentDeadline, note: str, now: datetime) -> None:
    row.audit_state = "flagged"
    row.audit_note = note[:200]
    row.audit_at = now


def _unaudited(stmt):
    """已通过、还没标过的截止；只看主文件（同一通知的其他网址内容相同，免得重复标）。"""
    return visible_documents(
        stmt.where(DocumentDeadline.status == "approved", DocumentDeadline.audit_state.is_(None))
    )


def _ai_due(stmt, now: datetime):
    """该让 AI 抽查的截止：没标过，且从没抽过或上次抽查已满 30 天（audit_at 记最近一次抽查）。"""
    return _unaudited(stmt).where(
        or_(DocumentDeadline.audit_at.is_(None), DocumentDeadline.audit_at < now - REAUDIT_AFTER)
    )


def _anchor(body: str, row: DocumentDeadline) -> tuple[int, int] | None:
    """这条截止在正文里的位置：先找证据句，找不到再找日期。"""
    if row.evidence:
        at = body.find(row.evidence[:20])
        if at >= 0:
            return at, at + min(len(row.evidence), _CONTEXT)
    return _date_span(body, row.approved_date) if row.approved_date else None


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _plan(body: str, rows: list[DocumentDeadline]) -> tuple[list[DocumentDeadline], str]:
    """这次发给 AI 的截止和原文片段：按顺序加入，片段合并后总长不超过上限；放不下的留到下次。"""
    chosen: list[DocumentDeadline] = []
    windows: list[tuple[int, int]] = []
    for row in rows:
        anchor = _anchor(body, row)
        if anchor is None:
            continue
        window = (max(0, anchor[0] - _CONTEXT), min(len(body), anchor[1] + _CONTEXT))
        merged = _merge([*windows, window])
        if sum(end - start for start, end in merged) > _MAX_EXCERPT:
            continue
        chosen.append(row)
        windows = merged
        if len(chosen) >= _MAX_ROWS:
            break
    return chosen, "\n……\n".join(body[start:end] for start, end in windows)


def _verified_note(item: dict, excerpt: str) -> str | None:
    """AI 说有误时，照抄的句子要在发给它的原文里、并写着它说的日期，才采信；返回存疑原因。"""
    quote = str(item.get("quote") or "").strip()
    compact = _compact(quote)
    if len(compact) < _MIN_QUOTE or compact not in _compact(excerpt):
        return None
    found_text = str(item.get("found_date") or "").strip()
    if found_text:
        try:
            found = date.fromisoformat(found_text)
        except ValueError:
            return None
        if _date_span(quote, found) is None:
            return None
    reason = str(item.get("reason") or "").strip()[:40] or "和原文不一致"
    note = "AI：" + reason + (f"（原文写的是 {found_text}）" if found_text else "")
    return f"{note}；原文：「{quote[:80]}」"


def _ai_check(
    client: ChatClient, doc: Document, excerpt: str, rows: list[DocumentDeadline]
) -> tuple[dict[int, str], list[int]]:
    """AI 判为存疑且对得上原文的截止（编号 → 原因），和对不上原文、没采信的编号。"""
    listing = "\n".join(
        f"编号 {row.id}：{row.kind}，{row.approved_date}"
        + (f" {row.approved_time.strftime('%H:%M')}" if row.approved_time else "")
        for row in rows
    )
    user = f"标题：{doc.title}\n相关原文：\n{excerpt}\n\n截止：\n{listing}"
    result = client.complete_json(_SYSTEM, user, max_tokens=200 + 150 * len(rows))
    items = result.get("items")
    wanted = {row.id for row in rows}
    notes: dict[int, str] = {}
    unverified: list[int] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("ok") is not False:
            continue
        try:
            deadline_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if deadline_id not in wanted or deadline_id in notes or deadline_id in unverified:
            continue
        note = _verified_note(item, excerpt)
        if note is None:
            unverified.append(deadline_id)
        else:
            notes[deadline_id] = note
    return notes, unverified


def run_audit(
    session: Session,
    client: ChatClient | None,
    *,
    sample: int = 20,
    now: datetime | None = None,
) -> AuditReport:
    now = now or datetime.now(UTC)
    report = AuditReport()

    # 1. 规则回查：全部已通过、还没标过的截止
    rows = session.execute(
        _unaudited(
            select(DocumentDeadline, Document, DocumentDetail)
            .join(Document, Document.id == DocumentDeadline.document_id)
            .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        )
    ).all()
    for row, doc, detail in rows:
        report.checked_by_rules += 1
        body = detail.body_text if detail else ""
        publish = (detail.publish_date if detail else None) or doc.publish_date
        note = rule_note(row, publish, body)
        if note:
            _flag(row, note, now)
            report.flagged.append((row.id, doc.title, note))
    session.commit()

    if client is None:
        return report

    # 2. AI 抽查：有该抽查的截止、最近一天没出过错的文件，随机抽 sample 份
    failed_recently = (
        select(AICheck.id)
        .where(
            AICheck.document_id == Document.id,
            AICheck.task == "audit",
            AICheck.error.is_not(None),
            AICheck.checked_at > now - RETRY_AFTER,
        )
        .exists()
    )
    doc_ids = list(
        session.scalars(
            _ai_due(
                select(Document.id).join(
                    DocumentDeadline, DocumentDeadline.document_id == Document.id
                ),
                now,
            )
            .where(~failed_recently)
            .group_by(Document.id)
            .order_by(func.rand())
            .limit(sample)
        )
    )
    consecutive_errors = 0
    for doc_id in doc_ids:
        doc = session.get(Document, doc_id)
        detail = session.get(DocumentDetail, doc_id)
        body = detail.body_text if detail else ""
        due_rows = list(
            session.scalars(
                _ai_due(
                    select(DocumentDeadline)
                    .join(Document, Document.id == DocumentDeadline.document_id)
                    .where(DocumentDeadline.document_id == doc_id),
                    now,
                ).order_by(
                    DocumentDeadline.audit_at.is_(None).desc(),
                    DocumentDeadline.audit_at,
                    DocumentDeadline.id,
                )
            )
        )
        targets, excerpt = _plan(body, due_rows) if body else ([], "")
        if not targets:
            continue
        check = session.scalar(
            select(AICheck).where(AICheck.document_id == doc_id, AICheck.task == "audit")
        )
        if check is None:
            check = AICheck(document_id=doc_id, task="audit")
            session.add(check)
        check.model = client.model
        check.checked_at = now
        try:
            notes, unverified = _ai_check(client, doc, excerpt, targets)
        except AIError as exc:
            notes, unverified = None, []
            check.result = None
            check.error = str(exc)[:200]
            report.errors += 1
            consecutive_errors += 1
        else:
            consecutive_errors = 0
            check.result = {
                "checked": [row.id for row in targets],
                "flagged": {str(k): v for k, v in notes.items()},
                "unverified": unverified,
            }
            check.error = None
            report.checked_by_ai += 1
            report.unverified += len(unverified)
            for row in targets:
                if row.id in notes:
                    _flag(row, notes[row.id], now)
                    report.flagged.append((row.id, doc.title, notes[row.id]))
                else:
                    row.audit_at = now  # 这次抽查过了，30 天后再抽
        usage = client.last_usage
        check.prompt_tokens = usage.get("prompt_tokens", 0)
        check.completion_tokens = usage.get("completion_tokens", 0)
        report.prompt_tokens += check.prompt_tokens
        report.completion_tokens += check.completion_tokens
        session.commit()
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            report.stopped_early = True
            break
    return report


def alert_text(report: AuditReport, limit: int = 3) -> str | None:
    """群提醒用的简短说明；没有存疑、也没出错时不提醒。"""
    parts = []
    if report.flagged:
        by_title: dict[str, list[str]] = defaultdict(list)
        for _, title, note in report.flagged:
            by_title[title].append(note)
        lines = [f"《{title[:30]}》{notes[0]}" for title, notes in list(by_title.items())[:limit]]
        more = "等" if len(by_title) > limit else ""
        parts.append(
            f"质检发现 {len(report.flagged)} 条已发布的截止日期可能有误{more}，"
            "请到核对台「质检存疑」复核：" + "；".join(lines)
        )
    if report.errors:
        parts.append(
            f"质检的 AI 抽查出错 {report.errors} 份文件"
            + (
                "，连续出错已提前停止（可能是服务不可用或额度用完）"
                if report.stopped_early
                else "，明天自动重试"
            )
        )
    return "\n".join(parts) or None
