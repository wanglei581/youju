"""AI 质检员（D-023）：每天复查已经发布给用户的截止日期，存疑的标出来、提醒到群里，由人复核。

- 规则回查（不用 AI，每次全量）：截止早于发布日期；正文里根本没写这个日期。
- AI 抽查（配了 AI 时）：每次抽一部分文件，让 AI 对照原文判断；只在原文明确写着别的日期、
  或明确没有这项截止时才算存疑，免得误报太多。同一文件 30 天内不重复抽。
- 只标记、不自动撤下：撤下（退回待核对）由人在核对台决定。人确认无误的不再标。
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.ai.client import AIError, ChatClient
from youju.dedup import visible_documents
from youju.extract.deadlines import _DATE
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail

# 同一文件隔多久再让 AI 抽查
REAUDIT_AFTER = timedelta(days=30)
# 发给 AI 的原文：证据句前后各取这么多字
_CONTEXT = 300

_SYSTEM = """你是截止日期质检员。必须只返回 JSON 对象，字段 items 为数组。
下面给出一份政府通知的相关原文，和已经人工核对通过的若干截止（编号、类型、日期、时刻）。
对每条截止判断：原文是否写着这一类事项在这一天截止。只有原文明确写着别的日期、
或原文明确没有这项截止时，ok 才写 false；拿不准就写 true。
每项字段：id（给出的编号）、ok（true 或 false）、found_date（原文写的日期，YYYY-MM-DD，没有就空）、
reason（不超过 40 字）。"""


@dataclass
class AuditReport:
    checked_by_rules: int = 0
    checked_by_ai: int = 0
    # （截止编号, 标题, 原因）
    flagged: list[tuple[int, str, str]] = field(default_factory=list)
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _mentions(body: str, due: date) -> bool:
    """正文里有没有写这个日期（年份写了的也要对得上）。"""
    for match in _DATE.finditer(body):
        year = match.group("year")
        if (
            int(match.group("month")) == due.month
            and int(match.group("day")) == due.day
            and (year is None or int(year) == due.year)
        ):
            return True
    return False


def rule_note(deadline: DocumentDeadline, publish_date: date | None, body: str) -> str | None:
    """规则回查：返回存疑原因，没问题返回 None。"""
    due = deadline.approved_date
    if due is None:
        return None
    if publish_date is not None and due < publish_date - timedelta(days=1):
        return f"截止日期 {due} 早于发布日期 {publish_date}"
    if body and not _mentions(body, due):
        return f"正文里找不到 {due.month}月{due.day}日"
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


def _excerpt(body: str, evidence: str) -> str:
    """证据句所在位置前后的原文；找不到证据句就取正文开头。"""
    at = body.find(evidence[:20]) if evidence else -1
    if at < 0:
        return body[: _CONTEXT * 2]
    return body[max(0, at - _CONTEXT) : at + len(evidence) + _CONTEXT]


def _ai_check(
    client: ChatClient, doc: Document, body: str, rows: list[DocumentDeadline]
) -> dict[int, str]:
    """AI 判为存疑的截止：编号 → 原因。"""
    excerpts = []
    for row in rows:
        piece = _excerpt(body, row.evidence)
        if piece not in excerpts:
            excerpts.append(piece)
    listing = "\n".join(
        f"编号 {row.id}：{row.kind}，{row.approved_date}"
        + (f" {row.approved_time.strftime('%H:%M')}" if row.approved_time else "")
        for row in rows
    )
    user = f"标题：{doc.title}\n相关原文：\n" + "\n……\n".join(excerpts) + f"\n\n截止：\n{listing}"
    result = client.complete_json(_SYSTEM, user)
    items = result.get("items")
    wanted = {row.id for row in rows}
    notes: dict[int, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("ok") is not False:
            continue
        try:
            deadline_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if deadline_id not in wanted:
            continue
        found = str(item.get("found_date") or "").strip()
        reason = str(item.get("reason") or "").strip()[:40]
        notes[deadline_id] = (
            "AI：" + (reason or "和原文不一致") + (f"（原文写的是 {found}）" if found else "")
        )
    return notes


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

    # 2. AI 抽查：有已通过截止、30 天内没抽过的文件，随机抽 sample 份
    recent = (
        select(AICheck.id)
        .where(
            AICheck.document_id == Document.id,
            AICheck.task == "audit",
            AICheck.checked_at > now - REAUDIT_AFTER,
        )
        .exists()
    )
    doc_ids = list(
        session.scalars(
            _unaudited(
                select(Document.id)
                .join(DocumentDeadline, DocumentDeadline.document_id == Document.id)
                .where(~recent)
            )
            .group_by(Document.id)
            .order_by(func.rand())
            .limit(sample)
        )
    )
    for doc_id in doc_ids:
        doc = session.get(Document, doc_id)
        detail = session.get(DocumentDetail, doc_id)
        body = detail.body_text if detail else ""
        targets = list(
            session.scalars(
                _unaudited(
                    select(DocumentDeadline)
                    .join(Document, Document.id == DocumentDeadline.document_id)
                    .where(DocumentDeadline.document_id == doc_id)
                )
            )
        )
        if not targets or not body:
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
            notes = _ai_check(client, doc, body, targets)
            check.result = {str(k): v for k, v in notes.items()}
            check.error = None
        except AIError as exc:
            check.result = None
            check.error = str(exc)[:200]
            report.errors += 1
            notes = {}
        usage = client.last_usage
        check.prompt_tokens = usage["prompt_tokens"]
        check.completion_tokens = usage["completion_tokens"]
        report.prompt_tokens += usage["prompt_tokens"]
        report.completion_tokens += usage["completion_tokens"]
        report.checked_by_ai += 1
        for row in targets:
            if row.id in notes:
                _flag(row, notes[row.id], now)
                report.flagged.append((row.id, doc.title, notes[row.id]))
        session.commit()
    return report


def flagged_summary(report: AuditReport, limit: int = 3) -> str:
    """群提醒用的简短说明。"""
    by_title: dict[str, list[str]] = defaultdict(list)
    for _, title, note in report.flagged:
        by_title[title].append(note)
    lines = [f"《{title[:30]}》{notes[0]}" for title, notes in list(by_title.items())[:limit]]
    more = "等" if len(by_title) > limit else ""
    return (
        f"质检发现 {len(report.flagged)} 条已发布的截止日期可能有误{more}，"
        "请到核对台「质检存疑」复核：" + "；".join(lines)
    )
