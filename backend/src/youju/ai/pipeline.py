"""每轮定时任务里的 AI 检查（D-023）：对抓到正文的文件做类型判断和截止抽取，和规则互相印证。

- 截止：规则候选里，AI 也找到同一天的标"AI 一致"；AI 找到、规则没找到的，作为 AI 候选送进核对台。
  两种都要人工核对通过后才给用户看（红线：关键字段未经核验不得发布）。
- 类型：先记录 AI 的判断，和规则不一致的在报告里列出，供人定。给用户看的仍是规则结果。
- 只发政府公开文件的标题和正文，不发任何用户数据。
"""

import fcntl
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from youju.ai.classify import classify_with_ai
from youju.ai.client import AIError, ChatClient
from youju.ai.deadlines import RESULT_VERSION, extract_deadlines_with_ai, stored_version
from youju.classify import comparable_kind, doc_kind
from youju.dedup import (
    canonical_ids,
    groups_for,
    reviewed_key,
    sync_reviewed_twins,
    visible_documents,
)
from youju.extract.deadlines import DeadlineCandidate, may_be_same, same_moment
from youju.extract.service import ai_found_in_group
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail, Source

# 调用出错的文件隔多久再试
RETRY_AFTER = timedelta(hours=24)
# 连续几份文件都出错就停下这一轮（多半是服务不可用或额度用完），由任务包装器发提醒
MAX_CONSECUTIVE_ERRORS = 3
# 截止日期按北京时间的今天算（和核对台"30 天内截止"一致）
BEIJING = ZoneInfo("Asia/Shanghai")


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
    # 用了 due_first 时：排在最前、先补跑的文件份数（有快到期的待核对候选、AI 还没抽过截止）
    due_soon_documents: int = 0
    # 调模型期间正文被换掉、这次的结果没记的份数（下一轮按新正文重查）
    body_changed: int = 0
    # 重跑旧版本时碰上定时任务那一轮在跑，停下了（round_running）
    round_busy: bool = False


TASKS = ("kind", "deadlines")
# 定时任务一轮的锁文件（deploy/round.sh、first-round.sh 用 flock 拿着）
ROUND_LOCK = "/tmp/youju-round.lock"


def round_running(path: str = ROUND_LOCK) -> bool:
    """定时任务那一轮正在跑（锁被拿着）。只试一下、马上放开，不挡下一轮。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


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


def _checkable(*conditions):
    """能交给 AI 检查的文件：正文抓到了、来源可用。只查主文件：同一通知的其他网址（已归并）
    内容相同，不重复花用量；类型待定页也只列主文件。"""
    return visible_documents(
        select(Document, DocumentDetail)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.status == "ok",
            DocumentDetail.body_text != "",
            Source.rights_level == 1,
            *conditions,
        )
    )


def _documents_due_soon(session: Session, limit: int, now: datetime, days: int):
    """有 days 天内（北京时间今天起，含今天和第 days 天）截止的待核对候选、AI 还没抽过截止的
    主文件，截止早的优先。候选在同一通知其他网址上的，查主文件（AI 结果按整个归并组比对）。"""
    today = now.astimezone(BEIJING).date()
    earliest_by_doc = session.execute(
        select(DocumentDeadline.document_id, func.min(DocumentDeadline.candidate_date))
        .where(
            DocumentDeadline.status == "pending",
            DocumentDeadline.candidate_date.between(today, today + timedelta(days=days)),
        )
        .group_by(DocumentDeadline.document_id)
    ).all()
    masters = canonical_ids(session, [doc_id for doc_id, _ in earliest_by_doc])
    earliest: dict[int, date] = {}
    for doc_id, due in earliest_by_doc:
        master = masters.get(doc_id, doc_id)
        earliest[master] = min(earliest.get(master, due), due)
    if not earliest:
        return []
    rows = session.execute(
        _checkable(~_done("deadlines", now), Document.id.in_(list(earliest)))
    ).all()
    rows.sort(key=lambda row: (earliest[row[0].id], -row[0].id))
    return rows[:limit]


def _documents_to_check(session: Session, limit: int, now: datetime, first: list | None = None):
    """两项检查里还有没做完的文件，新发现的优先；first（先补跑的文件）排在最前，
    合计不超过 limit。"""
    first = list(first or [])[:limit]
    if len(first) >= limit:
        return first
    stmt = _checkable(or_(*(~_done(task, now) for task in TASKS)))
    if first:
        stmt = stmt.where(Document.id.not_in([doc.id for doc, _ in first]))
    rest = session.execute(stmt.order_by(Document.id.desc()).limit(limit - len(first))).all()
    return first + rest


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


def _serialize(items: list[DeadlineCandidate]) -> dict:
    """存进 AI 检查记录的截止结果，带版本号。
    读的时候用 ai.deadlines.stored_items，新旧格式都认。"""
    return {
        "version": RESULT_VERSION,
        "items": [
            {
                "kind": c.kind,
                "date": c.due_date.isoformat(),
                "time": c.due_time.strftime("%H:%M") if c.due_time else None,
                "evidence": c.evidence,
                "confidence": c.confidence,
            }
            for c in items
        ],
    }


def _apply_deadlines(
    session: Session, doc: Document, items: list[DeadlineCandidate], report: AIRunReport
) -> None:
    master = canonical_ids(session, [doc.id]).get(doc.id, doc.id)
    members = groups_for(session, [master])[master]
    # 同一通知的所有网址（归并组）里的候选，用来判断"已经有了"
    group_rows = list(
        session.scalars(select(DocumentDeadline).where(DocumentDeadline.document_id.in_(members)))
    )
    # 规则候选：同一通知各网址的 AI 结果里有没有同一天（刚记下的这份也在里面）。
    # AI 后到、或只检查了同组别的网址，已有的规则候选也一起更新
    session.flush()
    found = ai_found_in_group(session, members) or []
    for row in group_rows:
        if row.origin != "rule":
            continue
        row.ai_agrees = any(
            same_moment(d, t, row.candidate_date, row.candidate_time) for d, t in found
        )
        if row.document_id == doc.id and row.status == "pending":
            if row.ai_agrees:
                report.deadlines_agree += 1
            else:
                report.deadlines_rule_only += 1
    # AI 找到、规则没找到的：作为 AI 候选送审。同一通知（整个归并组）里已有同一天的候选
    # （不论是否核对过、是规则还是 AI 找到的；一边没写时刻也算），不再送
    reviewed = {reviewed_key(row, row.status) for row in group_rows if row.status != "pending"}
    for c in items:
        if any(
            may_be_same(c.due_date, c.due_time, row.candidate_date, row.candidate_time)
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
    session: Session,
    client: ChatClient,
    *,
    limit: int = 300,
    now: datetime | None = None,
    due_first: int | None = None,
) -> AIRunReport:
    """对还没检查过的文件逐份做类型判断和截止抽取；每份做完就提交，中途停下不丢已做的。

    due_first（天数）：先补跑这么多天内截止的待核对候选所在的文件，核对台优先核的那些先有 AI 结果；
    合计仍不超过 limit。只写 AI 检查记录、规则候选上的"AI 是否一致"，以及新增"仅 AI 找到"的
    待核对候选；AI 的结果从不决定核对状态（通过与否只能由人在核对台点。归并组里同一截止已由人
    核对过的，照原有规则沿用人的核对结果，见 sync_reviewed_twins）。
    """
    now = now or datetime.now(UTC)
    report = AIRunReport()
    consecutive_errors = 0
    first = _documents_due_soon(session, limit, now, due_first) if due_first is not None else []
    report.due_soon_documents = len(first)
    for doc, detail in _documents_to_check(session, limit, now, first):
        report.documents += 1
        tasks = _pending_tasks(session, doc.id, now)
        # 这次交给模型的正文；结果先放着，写之前核对正文没被换掉（见下）
        body = detail.body_text
        records: list[tuple[str, dict]] = []
        ai_kind: str | None = None
        items: list[DeadlineCandidate] | None = None
        failed = 0
        if "kind" in tasks:
            try:
                kind = classify_with_ai(client, doc.title, body)
                if kind is None:
                    # 类别不在四类之内或字段不对：按出错记，隔 24 小时再试
                    records.append(
                        ("kind", {"error": "AI 返回的类型无效", "usage": client.last_usage})
                    )
                    failed += 1
                else:
                    result = {
                        "kind": kind.kind,
                        "confidence": kind.confidence,
                        "reason": kind.reason,
                    }
                    records.append(("kind", {"result": result, "usage": client.last_usage}))
                    ai_kind = kind.kind
            except AIError as exc:
                records.append(("kind", {"error": str(exc), "usage": client.last_usage}))
                failed += 1
            _count_usage(report, client.last_usage)
        if "deadlines" in tasks:
            try:
                items = extract_deadlines_with_ai(
                    client, doc.title, body, detail.publish_date or doc.publish_date
                )
                records.append(
                    ("deadlines", {"result": _serialize(items), "usage": client.last_usage})
                )
            except AIError as exc:
                records.append(("deadlines", {"error": str(exc), "usage": client.last_usage}))
                failed += 1
            _count_usage(report, client.last_usage)
        # 调模型期间正文可能被换掉（youju refetch-bodies 重取正文时删掉这份的检查记录、要求重查）：
        # 先锁住这份详情再核对正文（重取正文写入时也先锁它），变了就不记这次的结果，下一轮按新正文
        # 重查，免得旧正文的结果把"要重查"盖掉
        current = session.scalar(
            select(DocumentDetail.body_text)
            .where(DocumentDetail.document_id == doc.id)
            .with_for_update()
        )
        if current != body:
            session.rollback()
            report.body_changed += 1
        else:
            for task, values in records:
                _record(session, doc.id, task, client.model, now, **values)
            if ai_kind is not None:
                rule = doc_kind(doc.title)
                # AI 只分四类："资讯与指南"和 AI 的"其他"算一致
                if comparable_kind(rule) == ai_kind:
                    report.kind_agree += 1
                else:
                    report.kind_disagree.append((doc.id, doc.title, rule, ai_kind))
            if items is not None:
                _apply_deadlines(session, doc, items, report)
            session.commit()
        # 出错照样计数（模型出错和正文换没换无关），连续出错照样停下
        if failed:
            report.errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                report.stopped_early = True
                break
        else:
            consecutive_errors = 0
    return report


def old_deadline_checks(
    session: Session, now: datetime, days: int
) -> list[tuple[Document, DocumentDetail, AICheck]]:
    """发布日期在近 days 天内（北京时间今天往前数）的主文件里，截止检查成功过、但是旧版本做的，
    新的排前面。只读。"""
    since = now.astimezone(BEIJING).date() - timedelta(days=days)
    rows = session.execute(
        _checkable(func.coalesce(DocumentDetail.publish_date, Document.publish_date) >= since)
        .add_columns(AICheck)
        .join(AICheck, (AICheck.document_id == Document.id) & (AICheck.task == "deadlines"))
        .where(AICheck.error.is_(None))
        .order_by(Document.id.desc())
    ).all()
    return [
        (doc, detail, check)
        for doc, detail, check in rows
        if stored_version(check.result) < RESULT_VERSION
    ]


def rerun_old_deadlines(
    session: Session,
    client: ChatClient,
    *,
    days: int,
    limit: int,
    now: datetime | None = None,
    busy: Callable[[], bool] = lambda: False,
) -> AIRunReport:
    """重跑近 days 天内旧版本的截止检查（只做截止这一项，最多 limit 份），新结果覆盖旧结果，
    不删数据（D-099 期 0）：已有的候选和核对状态都不动，规则候选的"AI 是否一致"按新结果重算，
    新找到的照常作为 AI 候选送审。调用出错的保留旧结果，下次再跑。

    每份开始前问一下 busy（命令里是 round_running）：定时任务那一轮在跑就停下，免得和规则抽取
    抢同一批候选的锁；已做完的照常保留，下次接着跑。

    会改线上已有的 AI 检查记录，要先征得同意再跑（youju ai-recheck-deadlines --apply）。
    定时任务（run_ai_checks）不会因为版本旧就重跑。"""
    now = now or datetime.now(UTC)
    report = AIRunReport()
    consecutive_errors = 0
    for doc, detail, _ in old_deadline_checks(session, now, days)[:limit]:
        if busy():
            report.round_busy = True
            break
        report.documents += 1
        body = detail.body_text
        try:
            items = extract_deadlines_with_ai(
                client, doc.title, body, detail.publish_date or doc.publish_date
            )
        except AIError:
            _count_usage(report, client.last_usage)
            session.rollback()
            report.errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                report.stopped_early = True
                break
            continue
        _count_usage(report, client.last_usage)
        consecutive_errors = 0
        # 和定时任务一样：调模型期间正文被换掉的，这次的结果不记
        current = session.scalar(
            select(DocumentDetail.body_text)
            .where(DocumentDetail.document_id == doc.id)
            .with_for_update()
        )
        if current != body:
            session.rollback()
            report.body_changed += 1
            continue
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
        session.commit()
    return report
