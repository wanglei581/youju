"""申报条件清单（D-052 第④项）：抽取入库、核对队列、对外读取。

- 抽取：每轮定时任务里，对还没抽过当前正文的申报通知调用后台模型（youju.ai.conditions），
  30 天内截止的先抽，每轮有份数上限；没配 AI 密钥时整个步骤跳过。
- 核对：核对台逐条通过、改写（原文引用不能改）或删除，整份确认后才对外（youju.api.conditions）。
- 对外：只读整份确认过、而且正文指纹和当前正文一致的清单里通过的条目；正文一变立即不再显示。
  对外只给条件、原文引用和核对日期，不写怎么抽出来的，也不写能否申报。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.ai.client import AIError, ChatClient
from youju.crawler.detail import redact_sensitive
from youju.dedup import canonical_ids, group_ids, groups_for, visible_documents
from youju.fields import body_fingerprint
from youju.models import (
    CONDITION_CATEGORIES,
    AICheck,
    ConditionItem,
    ConditionList,
    Document,
    DocumentDeadline,
    DocumentDetail,
    Source,
)
from youju.takedown import available_condition

# AI 检查记录（AICheck）里这项任务的名字；出错的隔 24 小时再试
TASK = "conditions"
RETRY_AFTER = timedelta(hours=24)
# 连续几份都出错就停下这一轮（多半是服务不可用或额度用完），由任务包装器发提醒
MAX_CONSECUTIVE_ERRORS = 3
# 优先抽取、优先核对的范围：今天（北京时间）起这么多天内截止
SOON_DAYS = 30
BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass
class ConditionRunReport:
    documents: int = 0
    # 有条目、送去核对的清单份数，和其中的条目数
    lists: int = 0
    items: int = 0
    # 抽过、但没有一条通过原文核对的份数（不进核对台）
    empty: int = 0
    dropped: int = 0
    dropped_reasons: Counter = field(default_factory=Counter)
    # 排在最前、30 天内截止的份数
    due_soon: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stopped_early: bool = False


def public_body(raw: str) -> str:
    """抽取和核对都用脱敏后的正文（对外展示的正文也是脱敏后的），引用里不会带出个人信息。"""
    return redact_sensitive(raw or "")


def _current_body():
    """清单对应的正文就是现在这版正文（在数据库里比指纹，不用等重新抽取）。"""
    return ConditionList.body_hash == func.sha2(DocumentDetail.body_text, 256)


def _notice():
    # 延迟引入，避免 API 路由装载时形成循环依赖
    from youju.api.v1 import _kind_is

    return _kind_is(["申报通知"])


def _public_lists():
    """可以对外、可以核对的清单：正文抓到了、是现在这版正文、一级公开来源、没下架、仍是申报通知。"""
    return (
        select(ConditionList)
        .join(Document, Document.id == ConditionList.document_id)
        .join(DocumentDetail, DocumentDetail.document_id == ConditionList.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.status == "ok",
            _current_body(),
            Source.rights_level == 1,
            available_condition(),
            _notice(),
        )
    )


def due_order(
    session: Session, masters: list[int], today: date, days: int = SOON_DAYS
) -> dict[int, tuple[int, int, int]]:
    """每份（主文件）的排序键，按升序就是先后：
    (0, 截止日, -编号) days 天内截止，截止早的先；(1, 0, -编号) 截止更晚或还不知道截止，新的先；
    (2, 0, -编号) 知道的截止都已过。截止看整个归并组里待核对或已通过的候选（通过的按核定日期）。"""
    groups = groups_for(session, sorted(set(masters)))
    master_of = {member: master for master, ids in groups.items() for member in ids}
    known: dict[int, list[date]] = defaultdict(list)
    if master_of:
        for doc_id, status, candidate, approved in session.execute(
            select(
                DocumentDeadline.document_id,
                DocumentDeadline.status,
                DocumentDeadline.candidate_date,
                DocumentDeadline.approved_date,
            ).where(
                DocumentDeadline.document_id.in_(list(master_of)),
                DocumentDeadline.status.in_(("pending", "approved")),
            )
        ):
            due = approved if status == "approved" and approved else candidate
            if due is not None:
                known[master_of[doc_id]].append(due)
    order: dict[int, tuple[int, int, int]] = {}
    for master in groups:
        future = [due for due in known.get(master, []) if due >= today]
        if future and min(future) <= today + timedelta(days=days):
            order[master] = (0, min(future).toordinal(), -master)
        elif future or not known.get(master):
            order[master] = (1, 0, -master)
        else:
            order[master] = (2, 0, -master)
    return order


def _candidates(session: Session, now: datetime) -> list[int]:
    """还没抽过现在这版正文的申报通知（主文件），出错不到 24 小时的不重试。"""
    done = (
        select(ConditionList.id)
        .where(ConditionList.document_id == Document.id, _current_body())
        .exists()
    )
    failed = (
        select(AICheck.id)
        .where(
            AICheck.document_id == Document.id,
            AICheck.task == TASK,
            AICheck.error.is_not(None),
            AICheck.checked_at > now - RETRY_AFTER,
        )
        .exists()
    )
    stmt = visible_documents(
        select(Document.id)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.status == "ok",
            DocumentDetail.body_text != "",
            Source.rights_level == 1,
            available_condition(),
            _notice(),
            ~done,
            ~failed,
        )
    )
    return list(session.scalars(stmt))


def save_list(
    session: Session,
    document_id: int,
    digest: str,
    result,
    *,
    model: str,
    now: datetime,
) -> ConditionList:
    """存一份新清单（待核对）；没有条目的记为 empty，免得每轮重抽。同一份通知还没确认的旧版清单
    （正文已经变了）标为被替代，不再进核对台；确认过的旧版留着作留痕（正文对不上，不会对外）。"""
    for old in session.scalars(
        select(ConditionList).where(
            ConditionList.document_id == document_id,
            ConditionList.body_hash != digest,
            ConditionList.status == "pending",
        )
    ):
        old.status = "superseded"
    from youju.ai.conditions import PROMPT_VERSION

    row = ConditionList(
        document_id=document_id,
        body_hash=digest,
        status="pending" if result.items else "empty",
        model=model[:64],
        prompt_version=PROMPT_VERSION,
        dropped=result.dropped,
        extracted_at=now,
    )
    row.items = [
        ConditionItem(
            position=position,
            category=item.category,
            text=item.text,
            original_text=item.text,
            quote=item.quote,
            quote_offset=item.offset,
            status="pending",
        )
        for position, item in enumerate(result.items, 1)
    ]
    session.add(row)
    session.flush()
    return row


def run_extraction(
    session: Session,
    client: ChatClient,
    *,
    limit: int = 60,
    due_days: int = SOON_DAYS,
    now: datetime | None = None,
) -> ConditionRunReport:
    """逐份抽取，每份做完就提交，中途停下不丢已做的。只写清单（待核对）和 AI 检查记录，
    从不改核对结果：对外与否只由人在核对台决定。"""
    from youju.ai.conditions import extract_conditions_with_ai
    from youju.ai.pipeline import _record

    now = now or datetime.now(UTC)
    report = ConditionRunReport()
    ids = _candidates(session, now)
    order = due_order(session, ids, now.astimezone(BEIJING).date(), due_days)
    picked = sorted(ids, key=lambda doc_id: order[doc_id])[: max(0, limit)]
    report.due_soon = sum(1 for doc_id in picked if order[doc_id][0] == 0)
    consecutive_errors = 0
    for doc_id in picked:
        doc = session.get(Document, doc_id)
        detail = session.get(DocumentDetail, doc_id)
        if doc is None or detail is None:
            continue
        report.documents += 1
        digest = body_fingerprint(detail.body_text)
        try:
            result = extract_conditions_with_ai(client, doc.title, public_body(detail.body_text))
        except AIError as exc:
            _record(
                session, doc_id, TASK, client.model, now, error=str(exc), usage=client.last_usage
            )
            session.commit()
            _count_usage(report, client.last_usage)
            report.errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                report.stopped_early = True
                break
            continue
        consecutive_errors = 0
        _count_usage(report, client.last_usage)
        exists = session.scalar(
            select(ConditionList.id).where(
                ConditionList.document_id == doc_id, ConditionList.body_hash == digest
            )
        )
        if exists is None:
            save_list(session, doc_id, digest, result, model=client.model, now=now)
        _record(
            session,
            doc_id,
            TASK,
            client.model,
            now,
            result={
                "body_hash": digest,
                "items": len(result.items),
                "dropped": result.dropped,
                "dropped_reasons": {k: v for k, v in result.reasons.items() if v},
            },
            usage=client.last_usage,
        )
        session.commit()
        if result.items:
            report.lists += 1
            report.items += len(result.items)
        else:
            report.empty += 1
        report.dropped += result.dropped
        report.dropped_reasons.update({k: v for k, v in result.reasons.items() if v})
    return report


def _count_usage(report: ConditionRunReport, usage: dict | None) -> None:
    report.prompt_tokens += int((usage or {}).get("prompt_tokens") or 0)
    report.completion_tokens += int((usage or {}).get("completion_tokens") or 0)


# ---- 核对台 ----


def is_current(session: Session, row: ConditionList) -> bool:
    detail = session.get(DocumentDetail, row.document_id)
    return (
        detail is not None
        and detail.status == "ok"
        and body_fingerprint(detail.body_text) == row.body_hash
    )


def pending_ids(session: Session) -> list[tuple[int, int]]:
    """待核对的清单：（清单编号, 文件编号）。只算现在这版正文的、可以对外的。"""
    stmt = _public_lists().where(ConditionList.status == "pending")
    return [(row.id, row.document_id) for row in session.scalars(stmt)]


def pending_count(session: Session) -> int:
    return len(pending_ids(session))


def review_queue(
    session: Session, *, limit: int, now: datetime | None = None
) -> list[tuple[ConditionList, tuple[int, int, int]]]:
    """待核对清单，按所在通知的截止排：30 天内截止的先（截止早的先），再是截止更晚或不知道的
    （新的先），知道的截止都已过的最后。"""
    now = now or datetime.now(UTC)
    rows = pending_ids(session)
    masters = canonical_ids(session, [doc_id for _, doc_id in rows])
    master_of = {list_id: masters.get(doc_id, doc_id) for list_id, doc_id in rows}
    order = due_order(session, list(set(master_of.values())), now.astimezone(BEIJING).date())
    picked = sorted(master_of, key=lambda list_id: (order[master_of[list_id]], list_id))[:limit]
    lists = {
        row.id: row
        for row in session.scalars(select(ConditionList).where(ConditionList.id.in_(picked)))
    }
    return [(lists[list_id], order[master_of[list_id]]) for list_id in picked if list_id in lists]


def confirmed_lists(session: Session, *, limit: int) -> list[ConditionList]:
    """最近整份确认的清单（可以撤回确认）。"""
    return list(
        session.scalars(
            select(ConditionList)
            .where(ConditionList.status == "confirmed")
            .order_by(ConditionList.confirmed_at.desc(), ConditionList.id.desc())
            .limit(limit)
        )
    )


def quote_context(body: str, item: ConditionItem, width: int = 80) -> tuple[str, str]:
    """引用前后各 width 字的正文，核对台里高亮引用、看上下文用。"""
    at = item.quote_offset
    if body[at : at + len(item.quote)] != item.quote:
        at = body.find(item.quote)
    if at < 0:
        return "", ""
    end = at + len(item.quote)
    return body[max(0, at - width) : at], body[end : end + width]


# ---- 对外 ----


def public_conditions(session: Session, doc_id: int) -> dict | None:
    """详情页的申报条件：整份确认过、是现在这版正文、来自一级公开来源的清单里通过的条目。
    归并组里优先这份文件自己的清单。没有就返回 None（详情页不显示这张卡片）。"""
    members = group_ids(session, doc_id)
    row = session.scalar(
        _public_lists()
        .where(ConditionList.document_id.in_(members), ConditionList.status == "confirmed")
        .order_by(
            (ConditionList.document_id == doc_id).desc(),
            ConditionList.confirmed_at.desc(),
            ConditionList.id.desc(),
        )
        .limit(1)
    )
    if row is None or row.confirmed_at is None:
        return None
    items = sorted(
        (item for item in row.items if item.status == "approved"),
        key=lambda item: (CONDITION_CATEGORIES.index(item.category), item.position),
    )
    if not items:
        return None
    return {
        "items": [
            {"category": item.category, "text": item.text, "quote": item.quote} for item in items
        ],
        "checked": row.confirmed_at.astimezone(BEIJING).date().isoformat(),
    }
