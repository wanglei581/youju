"""文件级公开开关；当前归并组任何成员下架，整组立即不可公开。"""

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from youju.dedup import canonical_ids, group_ids
from youju.models import Document, DocumentMerge, DocumentTakedownEvent


def _hidden_groups():
    member = aliased(Document)
    merge = aliased(DocumentMerge)
    return (
        select(func.coalesce(merge.canonical_id, member.id))
        .select_from(member)
        .outerjoin(merge, (merge.duplicate_id == member.id) & merge.active.is_(True))
        .where(member.taken_down_at.is_not(None))
    )


def available_condition(document=Document):
    # 用归并组主编号比较，后续新加入的副本也不能绕过下架。
    current_merge = aliased(DocumentMerge)
    master = (
        select(current_merge.canonical_id)
        .where(current_merge.duplicate_id == document.id, current_merge.active.is_(True))
        .correlate(document)
        .scalar_subquery()
    )
    return ~func.coalesce(master, document.id).in_(_hidden_groups())


def cache_revision(session: Session) -> tuple:
    """跨进程失效；下架后新增、撤销或重分归并也会改变公开范围。"""
    latest = session.execute(
        select(DocumentTakedownEvent.id, DocumentTakedownEvent.operated_at)
        .order_by(DocumentTakedownEvent.id.desc())
        .limit(1)
    ).first()
    if latest is None:
        return ()
    mappings = session.execute(
        select(DocumentMerge.duplicate_id, DocumentMerge.canonical_id)
        .where(
            DocumentMerge.active.is_(True),
            or_(
                DocumentMerge.canonical_id.in_(_hidden_groups()),
                DocumentMerge.duplicate_id.in_(
                    select(Document.id).where(Document.taken_down_at.is_not(None))
                ),
            ),
        )
        .order_by(DocumentMerge.duplicate_id)
    ).all()
    return (*latest, tuple(tuple(row) for row in mappings))


def is_taken_down(session: Session, document_id: int) -> bool:
    return (
        session.scalar(
            select(Document.id).where(Document.id == document_id, ~available_condition())
        )
        is not None
    )


def change_state(
    session: Session,
    document_id: int,
    *,
    action: str,
    note: str,
    operator: str,
    reason: str | None = None,
) -> list[int] | None:
    master = canonical_ids(session, [document_id]).get(document_id, document_id)
    ids = group_ids(session, master)
    # 所有操作按编号顺序锁整组，重复请求也保留独立历史。
    docs = session.scalars(
        select(Document)
        .where(Document.id.in_(ids))
        .order_by(Document.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if not docs:
        return None
    stamp = datetime.now(UTC)
    for doc in docs:
        if action == "takedown":
            doc.taken_down_at, doc.takedown_reason = stamp, reason
            doc.takedown_note, doc.taken_down_by = note, operator
        else:
            doc.taken_down_at = None
            doc.restored_at, doc.restore_note, doc.restored_by = stamp, note, operator
        session.add(
            DocumentTakedownEvent(
                document_id=doc.id,
                action=action,
                reason=reason,
                note=note,
                operated_by=operator,
                operated_at=stamp,
            )
        )
    session.commit()
    return [doc.id for doc in docs]
