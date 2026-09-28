"""每轮定时任务里的 AI 检查（D-023）：对抓到正文的文件做类型判断和截止抽取，和规则互相印证。

- 截止：规则候选里，AI 也找到同一天的标"AI 一致"；AI 找到、规则没找到的，作为 AI 候选送进核对台。
  两种都要人工核对通过后才给用户看（红线：关键字段未经核验不得发布）。
- 类型：先记录 AI 的判断，和规则不一致的在报告里列出，供人定。给用户看的仍是规则结果。
- 只发政府公开文件的标题和正文，不发任何用户数据。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from youju.ai.classify import classify_with_ai
from youju.ai.client import AIError, ChatClient
from youju.ai.deadlines import extract_deadlines_with_ai
from youju.classify import doc_kind
from youju.dedup import canonical_ids, groups_for, reviewed_key, sync_reviewed_twins
from youju.extract.deadlines import DeadlineCandidate, same_moment
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


TASKS = ("kind", "deadlines")


def _done(task: str, now: datetime):
    """这项检查已经做完：成功过，或出错还不到 24 小时（到时间再试）。"""
    return (
        select(AICheck.id)
        .where(
            AICheck.document_id == Document.id,
            AICheck.task == task,
            or_(AICheck.error.is_(None), AICheck.checked_at > now - RETRY_AFTER),
        )
        .exists()
    )


def _documents_to_check(session: Session, limit: int, now: datetime):
    """两项检查里还有没做完的文件，新发现的优先。"""
    stmt = (
        select(Document, DocumentDetail)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.status == "ok",
            DocumentDetail.body_text != "",
            Source.rights_level == 1,
            or_(*(~_done(task, now) for task in TASKS)),
        )
        .order_by(Document.id.desc())
        .limit(limit)
    )
    return session.execute(stmt).all()


def _pending_tasks(session: Session, document_id: int, now: datetime) -> list[str]:
    finished = {
        row.task
        for row in session.scalars(select(AICheck).where(AICheck.document_id == document_id))
        if row.error is None or row.checked_at > now - RETRY_AFTER
    }
    return [task for task in TASKS if task not in finished]


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
    master = canonical_ids(session, [doc.id]).get(doc.id, doc.id)
    members = groups_for(session, [master])[master]
    # 同一通知的所有网址（归并组）里的候选，用来判断"已经有了"
    group_rows = list(
        session.scalars(select(DocumentDeadline).where(DocumentDeadline.document_id.in_(members)))
    )
    rows = [row for row in group_rows if row.document_id == doc.id]
    # 规则候选：AI 是否也找到了同一天
    for row in rows:
        if row.origin != "rule":
            continue
        row.ai_agrees = any(
            same_moment(c.due_date, c.due_time, row.candidate_date, row.candidate_time)
            for c in items
        )
        if row.status == "pending":
            if row.ai_agrees:
                report.deadlines_agree += 1
            else:
                report.deadlines_rule_only += 1
    # AI 找到、规则没找到的：作为 AI 候选送审。同一通知（整个归并组）里已有同一天的候选
    # （不论是否核对过、是规则还是 AI 找到的），不再送
    reviewed = {reviewed_key(row, row.status) for row in group_rows if row.status != "pending"}
    for c in items:
        if any(
            same_moment(c.due_date, c.due_time, row.candidate_date, row.candidate_time)
            for row in group_rows
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
        group_rows.append(row)
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
        tasks = _pending_tasks(session, doc.id, now)
        failed = 0
        if "kind" in tasks:
            try:
                kind = classify_with_ai(client, doc.title, detail.body_text)
                if kind is None:
                    # 类别不在四类之内或字段不对：按出错记，隔 24 小时再试
                    _record(
                        session,
                        doc.id,
                        "kind",
                        client.model,
                        now,
                        error="AI 返回的类型无效",
                        usage=client.last_usage,
                    )
                    failed += 1
                else:
                    _record(
                        session,
                        doc.id,
                        "kind",
                        client.model,
                        now,
                        result={
                            "kind": kind.kind,
                            "confidence": kind.confidence,
                            "reason": kind.reason,
                        },
                        usage=client.last_usage,
                    )
                    rule = doc_kind(doc.title)
                    if rule == kind.kind:
                        report.kind_agree += 1
                    else:
                        report.kind_disagree.append((doc.id, doc.title, rule, kind.kind))
            except AIError as exc:
                _record(
                    session,
                    doc.id,
                    "kind",
                    client.model,
                    now,
                    error=str(exc),
                    usage=client.last_usage,
                )
                failed += 1
            _count_usage(report, client.last_usage)
        if "deadlines" in tasks:
            try:
                items = extract_deadlines_with_ai(
                    client, doc.title, detail.body_text, detail.publish_date or doc.publish_date
                )
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
                _record(
                    session,
                    doc.id,
                    "deadlines",
                    client.model,
                    now,
                    error=str(exc),
                    usage=client.last_usage,
                )
                failed += 1
            _count_usage(report, client.last_usage)
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
