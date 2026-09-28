"""纠错分诊（D-023）：用户对某份文件纠错后，用规则和 AI 复查这份文件已发布的截止日期，
结果写在纠错旁边，处理的人一眼能看到发布的和原文对不对得上；查出存疑的照常进"质检存疑"。

- 用户写的纠错内容绝不发给 AI，这里也不读它：AI 只看这份政府文件的原文片段和已发布的截止，
  判断和质检员同一套（要照抄原文句子、写着它给出的日期才算存疑）。
- 只复查、不改数据：存疑的只标记，撤下由人在核对台决定；人确认过无误的不再标，只在分诊结果里写出来。
- 每条纠错只分诊一次（不看 30 天内抽查过没有：有人纠错就再查一遍）；AI 出错的下一轮再试，
  连续出错就停。
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.ai.audit import MAX_CONSECUTIVE_ERRORS, _ai_check, _flag, _plan, rule_note
from youju.ai.client import AIError, ChatClient
from youju.classify import effective_kind
from youju.dedup import canonical_ids
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail, Feedback

_NOTE_MAX = 300
# 一份文件的截止多、原文长，一次放不下时分批请 AI 复查，最多这么多批
_MAX_BATCHES = 3


@dataclass
class TriageReport:
    triaged: int = 0
    # 新标为存疑、进了"质检存疑"的截止条数
    flagged: int = 0
    errors: int = 0
    stopped_early: bool = False


def _kind_note(session: Session, doc: Document) -> str | None:
    """AI 判的类型和用户看到的不一样、又还没人定过时提一句。"""
    if doc.kind_override is not None:
        return None
    check = session.scalar(
        select(AICheck).where(
            AICheck.document_id == doc.id, AICheck.task == "kind", AICheck.error.is_(None)
        )
    )
    ai_kind = check.result.get("kind") if check and isinstance(check.result, dict) else None
    shown = effective_kind(doc.title, None)
    if ai_kind and ai_kind != shown:
        return f'类型：页面显示为{shown}，AI 认为是{ai_kind}（到"类型待定"处理）'
    return None


def _state_hint(row: DocumentDeadline) -> str:
    if row.audit_state == "cleared":
        return "（之前人确认过无误，未再标）"
    if row.audit_state == "flagged":
        return '（已在"质检存疑"里）'
    return ""


def triage_one(
    session: Session, client: ChatClient | None, feedback: Feedback, now: datetime
) -> tuple[str, int]:
    """复查一条纠错对应的文件，返回（分诊结果, 新标存疑条数）；AI 出错时抛 AIError，不提交。"""
    master = canonical_ids(session, [feedback.document_id]).get(
        feedback.document_id, feedback.document_id
    )
    doc = session.get(Document, master)
    if doc is None:
        return "对应文件已删除，没法复查", 0
    detail = session.get(DocumentDetail, master)
    body = detail.body_text if detail else ""
    publish = (detail.publish_date if detail else None) or doc.publish_date
    rows = list(
        session.scalars(
            select(DocumentDeadline)
            .where(DocumentDeadline.document_id == master, DocumentDeadline.status == "approved")
            .order_by(DocumentDeadline.id)
        )
    )
    pending = (
        session.scalar(
            select(func.count(DocumentDeadline.id)).where(
                DocumentDeadline.document_id == master, DocumentDeadline.status == "pending"
            )
        )
        or 0
    )
    parts: list[str] = []
    flagged = 0
    if not rows:
        parts.append("这份文件没有已发布的截止日期")
    else:
        # 已在"质检存疑"里的照实写出来；其余的规则回查，规则没发现问题的再请 AI 复查
        problems: dict[int, tuple[DocumentDeadline, str]] = {
            row.id: (row, row.audit_note or "已标存疑")
            for row in rows
            if row.audit_state == "flagged"
        }
        for row in rows:
            note = None if row.id in problems else rule_note(row, publish, body)
            if note:
                problems[row.id] = (row, note)
        remaining = [r for r in rows if r.id not in problems]
        batches = 0
        while client is not None and body and remaining and batches < _MAX_BATCHES:
            targets, excerpt = _plan(body, remaining)
            if not targets:
                break  # 原文里定位不到的，留给人看
            notes, _ = _ai_check(client, doc, excerpt, targets)
            batches += 1
            for row in targets:
                if row.id in notes:
                    problems[row.id] = (row, notes[row.id])
                elif row.audit_state is None:
                    row.audit_at = now  # 这次复查过、没问题，30 天内抽查不再抽
            done = {row.id for row in targets}
            remaining = [r for r in remaining if r.id not in done]
        for row, note in problems.values():
            if row.audit_state is None:
                _flag(row, note, now)
                flagged += 1
        checked_by = "规则和 AI " if client is not None else "规则"
        if problems:
            shown = "；".join(
                f"{row.approved_date} {note}{_state_hint(row)}"
                for row, note in list(problems.values())[:2]
            )
            parts.append(
                f"已发布的截止 {len(rows)} 条，{checked_by}复查 {len(problems)} 条存疑：{shown}"
            )
        elif client is not None and remaining:
            parts.append(f"已发布的截止 {len(rows)} 条，复查过的都对得上原文")
        else:
            parts.append(f"已发布的截止 {len(rows)} 条，{checked_by}复查都对得上原文")
        if client is None:
            parts.append("没配 AI，只做了规则回查")
        elif remaining:
            parts.append(
                f"另有 {len(remaining)} 条 AI 没复查到（原文里定位不到或篇幅太长），请人工核对"
            )
    if pending:
        parts.append(f"还有 {pending} 条截止候选待核对")
    kind = _kind_note(session, doc)
    if kind:
        parts.append(kind)
    return "；".join(parts)[:_NOTE_MAX], flagged


def triage_feedback(
    session: Session, client: ChatClient | None, *, limit: int = 50, now: datetime | None = None
) -> TriageReport:
    """分诊还没处理、还没分诊过、有对应文件的纠错。"""
    now = now or datetime.now(UTC)
    report = TriageReport()
    items = session.scalars(
        select(Feedback)
        .where(
            Feedback.handled_at.is_(None),
            Feedback.triaged_at.is_(None),
            Feedback.document_id.is_not(None),
        )
        .order_by(Feedback.id)
        .limit(limit)
    ).all()
    consecutive_errors = 0
    for feedback in items:
        try:
            note, flagged = triage_one(session, client, feedback, now)
        except AIError:
            session.rollback()  # 这条下一轮连同规则回查一起重做
            report.errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                report.stopped_early = True
                break
            continue
        consecutive_errors = 0
        feedback.triage_note = note
        feedback.triaged_at = now
        report.triaged += 1
        report.flagged += flagged
        session.commit()
    return report
