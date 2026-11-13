"""文件级公开开关；当前归并组任何成员下架，整组立即不可公开。

下架有两种：正式下架（文件的 taken_down_at，本机管理台做，随同步到服务器）和服务器上的紧急下架
（emergency_takedowns 里没撤销的行，电脑不在线时用，见 youju/emergency_takedown.py）。两种都整组
不对外；本机没有紧急下架的行，本机的结果和以前一样。"""

from datetime import UTC, datetime

from sqlalchemy import func, or_, select, true, union_all
from sqlalchemy.orm import Session, aliased

from youju.dedup import canonical_ids, group_ids
from youju.models import Document, DocumentMerge, DocumentTakedownEvent, EmergencyTakedown


def _emergency_ids():
    """服务器上紧急下架着（没撤销）的文件编号。"""
    return select(EmergencyTakedown.document_id).where(
        EmergencyTakedown.lifted_at.is_(None), EmergencyTakedown.document_id.is_not(None)
    )


def _hidden_groups():
    member = aliased(Document)
    merge = aliased(DocumentMerge)
    taken = (
        select(func.coalesce(merge.canonical_id, member.id))
        .select_from(member)
        .outerjoin(merge, (merge.duplicate_id == member.id) & merge.active.is_(True))
        .where(member.taken_down_at.is_not(None))
    )
    # 紧急下架另算一段再合起来：和正式下架写在一个 OR 里，下架时间列的索引就用不上了
    urgent = aliased(EmergencyTakedown)
    urgent_merge = aliased(DocumentMerge)
    emergency = (
        select(func.coalesce(urgent_merge.canonical_id, urgent.document_id))
        .select_from(urgent)
        .outerjoin(
            urgent_merge,
            (urgent_merge.duplicate_id == urgent.document_id) & urgent_merge.active.is_(True),
        )
        # 文件被删、编号置空的行不算：NOT IN 的结果里有 NULL，所有文件都会被当成下架
        .where(urgent.lifted_at.is_(None), urgent.document_id.is_not(None))
    )
    return union_all(taken, emergency)


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
    events = select(
        func.max(DocumentTakedownEvent.id).label("latest"),
        func.max(DocumentTakedownEvent.operated_at).label("stamp"),
    ).subquery()
    mappings = (
        select(
            func.count().label("count"),
            func.coalesce(
                func.sum(
                    func.crc32(
                        func.concat_ws("|", DocumentMerge.duplicate_id, DocumentMerge.canonical_id)
                    )
                ),
                0,
            ).label("checksum"),
        )
        .where(
            DocumentMerge.active.is_(True),
            or_(
                DocumentMerge.canonical_id.in_(_hidden_groups()),
                DocumentMerge.duplicate_id.in_(
                    select(Document.id).where(Document.taken_down_at.is_not(None))
                ),
                DocumentMerge.duplicate_id.in_(_emergency_ids()),
            ),
        )
        .subquery()
    )

    def urgent(column):
        # 紧急下架：新加（最大编号、条数）、撤销（已撤销的条数、最晚的撤销时间）都会变。
        # 写成标量子查询放在列里：再 JOIN 一个子查询会被当成笛卡尔积（SQLAlchemy 的 FROM 检查）
        return func.max(select(column).select_from(EmergencyTakedown).scalar_subquery())

    # 两个子查询都只产出一行；用一条聚合 SQL 保留原有下架和归并失效语义，
    # 避免历史下架后每次请求将整批映射加载到 Python。
    revision = session.execute(
        select(
            func.max(events.c.latest),
            func.max(events.c.stamp),
            func.max(mappings.c.count),
            func.max(mappings.c.checksum),
            urgent(func.max(EmergencyTakedown.id)),
            urgent(func.count()),
            urgent(func.count(EmergencyTakedown.lifted_at)),
            urgent(func.max(EmergencyTakedown.lifted_at)),
        )
        .select_from(events)
        .join(mappings, true())
    ).one()
    base = tuple(revision[:4]) if revision[0] is not None else ()
    # 从没有过紧急下架（本机一直是这样）：和以前一模一样
    return base if revision[4] is None else base + tuple(revision[4:])


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
