"""私有截止线索与人工核对的衔接；奖励失败不回滚已核对的日期。"""

import hashlib
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from youju import rewards
from youju.dedup import canonical_ids, group_ids
from youju.models import AppKey, DeadlineClue, Document, DocumentDeadline, PassReward, User

log = logging.getLogger("youju.deadline_clues")
STATUSES = {"pending": "待核对", "accepted": "已采纳", "rejected": "未采纳"}


def lock_queue(session: Session) -> None:
    # 线索队列的写入先抢同一个跨进程行锁，互相串行。用户提交线索的接口已删（影响评估第六节（二）
    # 第 13 条），现在只有核对台处理以前留下的线索时写。
    stmt = insert(AppKey).values(name="deadline_clue_queue", value="1")
    session.execute(stmt.on_duplicate_key_update(name=stmt.inserted.name))


def out(row: DeadlineClue) -> dict:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "kind": row.kind,
        "date": row.date.isoformat(),
        "time": row.time.strftime("%H:%M") if row.time else None,
        "quote": row.quote,
        "status": STATUSES[row.status],
        "submitted_at": row.submitted_at.isoformat(),
        "processed_at": row.processed_at.isoformat() if row.processed_at else None,
        "deadline_id": row.deadline_id,
    }


def document_group(session: Session, document_id: int) -> list[int]:
    master = canonical_ids(session, [document_id]).get(document_id, document_id)
    return group_ids(session, master)


def lock_documents(session: Session, document_id: int) -> list[int]:
    ids = sorted(document_group(session, document_id))
    list(
        session.scalars(
            select(Document.id).where(Document.id.in_(ids)).order_by(Document.id).with_for_update()
        )
    )
    return ids


def settle(session: Session, deadline: DocumentDeadline, stamp: datetime) -> None:
    # 驳回候选只是否定该日期，保留用户线索供重新建候选或人工不采纳。
    if deadline.status not in ("approved", "changed"):
        return
    ids = document_group(session, deadline.document_id)
    rows = session.scalars(
        select(DeadlineClue)
        .where(
            DeadlineClue.document_id.in_(ids),
            DeadlineClue.kind == deadline.kind,
            DeadlineClue.status == "pending",
        )
        .order_by(DeadlineClue.id)
        .with_for_update()
    )
    for clue in rows:
        clue.status = "accepted" if clue.date == deadline.approved_date else "rejected"
        clue.processed_at, clue.deadline_id = stamp, deadline.id


def pending_candidate(session: Session, ids: list[int], kind: str) -> bool:
    return (
        session.scalar(
            select(DocumentDeadline.id)
            .where(
                DocumentDeadline.document_id.in_(ids),
                DocumentDeadline.kind == kind,
                DocumentDeadline.status == "pending",
            )
            .limit(1)
        )
        is not None
    )


def lock_review_queue(session: Session, *, settings=None) -> None:
    from youju.config import get_settings

    if rewards.enabled(settings or get_settings()):
        lock_queue(session)


def reward_ref(kind: str) -> str:
    return "deadline_" + hashlib.sha256(kind.encode()).hexdigest()[:20]


def _award_accepted(
    session: Session, settings, stamp: datetime, document_id: int | None = None
) -> dict[str, int]:
    result = {"granted": 0, "failed": 0}
    if not rewards.enabled(settings):
        return result
    stmt = (
        select(DeadlineClue)
        .where(DeadlineClue.status == "accepted")
        .order_by(DeadlineClue.submitted_at, DeadlineClue.id)
    )
    if document_id is not None:
        stmt = stmt.where(DeadlineClue.document_id.in_(document_group(session, document_id)))
    rows = session.scalars(stmt).all()
    masters = canonical_ids(session, [r.document_id for r in rows])
    # 包含已注销者，不能把本该给最早提交者的奖励改给后来者。
    winners = {}
    for row in rows:
        key = (masters.get(row.document_id, row.document_id), row.kind)
        winners.setdefault(key, row.user_id)
    session.rollback()
    for (doc_id, kind), user_id in winners.items():
        if user_id is None:
            continue
        try:
            if session.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
                session.rollback()
                continue
            ref = reward_ref(kind)
            if (
                session.scalar(
                    select(PassReward.id).where(
                        PassReward.ref_type == ref,
                        PassReward.ref_id.in_(document_group(session, doc_id)),
                    )
                )
                is None
            ):
                rewards.grant(session, user_id, "deadline_clue", 1, ref, doc_id, stamp)
                result["granted"] += 1
            session.commit()
        except Exception:
            session.rollback()
            result["failed"] += 1
            log.warning("截止线索奖励未发出，请运行 rewards-reconcile")
    return result


def award_accepted(
    session: Session, settings, stamp: datetime, document_id: int | None = None
) -> dict[str, int]:
    # 查询凭据出错同样不能把已经提交的核对结果变成失败响应。
    try:
        return _award_accepted(session, settings, stamp, document_id)
    except Exception:
        session.rollback()
        log.warning("截止线索奖励未发出，请运行 rewards-reconcile")
        return {"granted": 0, "failed": 1}
