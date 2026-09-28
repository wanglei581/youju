"""每轮定时任务里的 AI 检查（D-023）：对抓到正文的文件做类型判断和截止抽取，和规则互相印证。

- 截止：规则候选里，AI 也找到同一天的标"AI 一致"；AI 找到、规则没找到的，作为 AI 候选送进核对台。
  两种都要人工核对通过后才给用户看（红线：关键字段未经核验不得发布）。
- 类型：先记录 AI 的判断，和规则不一致的在报告里列出，供人定。给用户看的仍是规则结果。
- 只发政府公开文件的标题和正文，不发任何用户数据。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from youju.ai.classify import classify_with_ai
from youju.ai.client import AIError, ChatClient
from youju.ai.deadlines import extract_deadlines_with_ai
from youju.classify import doc_kind
from youju.dedup import canonical_ids, groups_for, reviewed_key, sync_reviewed_twins
from youju.extract.deadlines import DeadlineCandidate
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail, Source

# 调用出错的文件隔多久再试
RETRY_AFTER = timedelta(hours=24)
# 连续几份文件都出错就停下这一轮（多半是服务不可用或额度用完），由任务包装器发提醒
MAX_CONSECUTIVE_ERRORS = 3


@dataclass
class AIRunReport:
    documents: int = 0
    kind_agree: int = 0
    # （文件编号, 标题, 规则判断, AI 判断）
    kind_disagree: list[tuple[int, str, str, str]] = field(default_factory=list)
    deadlines_agree: int = 0
    deadlines_rule_only: int = 0
    deadlines_ai_only: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stopped_early: bool = False


def _same_moment(a_date: date, a_time: time | None, b_date: date, b_time: time | None) -> bool:
    """同一天，且两边都写了时刻时时刻也相同，算同一个截止。"""
    return a_date == b_date and (a_time is None or b_time is None or a_time == b_time)


def _documents_to_check(session: Session, limit: int, now: datetime):
    """还没做过截止检查的文件（出错的隔 24 小时再试），新发现的优先。"""
    done = select(AICheck.id).where(
        AICheck.document_id == Document.id,
        AICheck.task == "deadlines",
        or_(AICheck.error.is_(None), AICheck.checked_at > now - RETRY_AFTER),
    )
    stmt = (
        select(Document, DocumentDetail)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.status == "ok",
            DocumentDetail.body_text != "",
            Source.rights_level == 1,
            ~done.exists(),
        )
        .order_by(Document.id.desc())
        .limit(limit)
    )
    return session.execute(stmt).all()


def _record(
    session: Session,
    document_id: int,
    task: str,
    model: str,
    now: datetime,
    *,
    result: dict | list | None = None,
    error: str | None = None,
    usage: dict | None = None,
) -> None:
    row = session.scalar(
        select(AICheck).where(AICheck.document_id == document_id, AICheck.task == task)
    )
    if row is None:
        row = AICheck(document_id=document_id, task=task)
        session.add(row)
    row.model = model
    row.result = result
    row.error = error[:200] if error else None
    row.prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    row.completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    row.checked_at = now


def _count_usage(report: AIRunReport, usage: dict | None) -> None:
    report.prompt_tokens += int((usage or {}).get("prompt_tokens") or 0)
    report.completion_tokens += int((usage or {}).get("completion_tokens") or 0)


def _serialize(items: list[DeadlineCandidate]) -> list[dict]:
    return [
        {
            "kind": c.kind,
            "date": c.due_date.isoformat(),
            "time": c.due_time.strftime("%H:%M") if c.due_time else None,
            "evidence": c.evidence,
            "confidence": c.confidence,
        }
        for c in items
    ]


def _apply_deadlines(
    session: Session, doc: Document, items: list[DeadlineCandidate], report: AIRunReport
) -> None:
    rows = list(
        session.scalars(select(DocumentDeadline).where(DocumentDeadline.document_id == doc.id))
    )
    # 规则候选：AI 是否也找到了同一天
    for row in rows:
        if row.origin != "rule":
            continue
        row.ai_agrees = any(
            _same_moment(c.due_date, c.due_time, row.candidate_date, row.candidate_time)
            for c in items
        )
        if row.status == "pending":
            if row.ai_agrees:
                report.deadlines_agree += 1
            else:
                report.deadlines_rule_only += 1
    # AI 找到、规则没找到的：作为 AI 候选送审。同一文件已有同一天的（不论是否核对过）、
    # 或同一通知的其他网址已核对过同一截止的，不再送
    master = canonical_ids(session, [doc.id]).get(doc.id, doc.id)
    members = groups_for(session, [master])[master]
    reviewed = {
        reviewed_key(row, row.status)
        for row in session.scalars(
            select(DocumentDeadline).where(
                DocumentDeadline.document_id.in_(members), DocumentDeadline.status != "pending"
            )
        )
    }
    for c in items:
        if any(
            _same_moment(c.due_date, c.due_time, row.candidate_date, row.candidate_time)
            for row in rows
        ):
            continue
        probe = SimpleNamespace(
            kind=c.kind, candidate_date=c.due_date, candidate_time=c.due_time, evidence=c.evidence
        )
        if (
            reviewed_key(probe, "approved") in reviewed
            or reviewed_key(probe, "rejected") in reviewed
        ):
            continue
        # 指纹带 "ai|" 前缀，和规则候选的指纹分开
        fingerprint = hashlib.sha256(
            f"ai|{c.kind}|{c.due_date}|{c.due_time}|{c.evidence}".encode()
        ).hexdigest()
        row = DocumentDeadline(
            document_id=doc.id,
            fingerprint=fingerprint,
            kind=c.kind,
            candidate_date=c.due_date,
            candidate_time=c.due_time,
            evidence=c.evidence,
            confidence=c.confidence,
            status="pending",
            origin="ai",
        )
        session.add(row)
        rows.append(row)
        report.deadlines_ai_only += 1
    session.flush()
    sync_reviewed_twins(session, {master})


def run_ai_checks(
    session: Session, client: ChatClient, *, limit: int = 300, now: datetime | None = None
) -> AIRunReport:
    """对还没检查过的文件逐份做类型判断和截止抽取；每份做完就提交，中途停下不丢已做的。"""
    now = now or datetime.now(UTC)
    report = AIRunReport()
    consecutive_errors = 0
    for doc, detail in _documents_to_check(session, limit, now):
        report.documents += 1
        failed = 0
        try:
            kind = classify_with_ai(client, doc.title, detail.body_text)
            _count_usage(report, client.last_usage)
            _record(
                session,
                doc.id,
                "kind",
                client.model,
                now,
                result=None
                if kind is None
                else {"kind": kind.kind, "confidence": kind.confidence, "reason": kind.reason},
                usage=client.last_usage,
            )
            if kind is not None:
                rule = doc_kind(doc.title)
                if rule == kind.kind:
                    report.kind_agree += 1
                else:
                    report.kind_disagree.append((doc.id, doc.title, rule, kind.kind))
        except AIError as exc:
            _record(session, doc.id, "kind", client.model, now, error=str(exc))
            failed += 1
        try:
            items = extract_deadlines_with_ai(
                client, doc.title, detail.body_text, detail.publish_date or doc.publish_date
            )
            _count_usage(report, client.last_usage)
            _record(
                session,
                doc.id,
                "deadlines",
                client.model,
                now,
                result=_serialize(items),
                usage=client.last_usage,
            )
            _apply_deadlines(session, doc, items, report)
        except AIError as exc:
            _record(session, doc.id, "deadlines", client.model, now, error=str(exc))
            failed += 1
        session.commit()
        if failed:
            report.errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                report.stopped_early = True
                break
        else:
            consecutive_errors = 0
    return report
