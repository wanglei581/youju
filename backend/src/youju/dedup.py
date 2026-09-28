"""按标题、发文机关和发布日期归并展示；原始题录与衍生记录仍各自保留。"""

import hashlib
import re
from collections import defaultdict

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from youju.models import Document, DocumentDetail, DocumentMerge, Source


def match_key(doc: Document, detail: DocumentDetail | None, source: Source) -> str | None:
    agency = (detail.issuer if detail and detail.issuer else source.dept_full_name).strip()
    published = detail.publish_date if detail and detail.publish_date else doc.publish_date
    title = re.sub(r"\s+", "", doc.title)
    if not title or not agency or published is None:
        return None
    # 列表页截断的标题（以"..."或"…"结尾）不同文件可能截成一样，不参与归并
    if title.endswith(("...", "…", "..")):
        return None
    return f"{title}|{re.sub(r'\s+', '', agency)}|{published.isoformat()}"


def _signature(detail: DocumentDetail | None) -> tuple[str | None, str | None]:
    """文号和正文摘要（去空白后整段正文的哈希）；取不到的为 None（不参与比较）。
    正文有任何不同就不归并：宁可少合并、多显示一条，也不把不同的文件藏起来。"""
    if detail is None:
        return None, None
    number = re.sub(r"\s+", "", detail.doc_number) if detail.doc_number else None
    body = re.sub(r"\s+", "", detail.body_text or "")
    # 有正文就比较（解析时不足 30 字的正文已清空）；没有正文的不参与比较
    digest = hashlib.sha256(body.encode()).hexdigest() if body else None
    return number or None, digest


def _split_by_content(
    members: list[tuple[int, tuple[str | None, str | None]]],
) -> list[list[int]]:
    """标题、机关、日期都相同，但文号或正文开头不同的，分成不同的组，不归并。"""
    buckets: list[tuple[list[str | None], list[int]]] = []
    for doc_id, (number, head) in members:
        for sig, ids in buckets:
            if (number is None or sig[0] is None or number == sig[0]) and (
                head is None or sig[1] is None or head == sig[1]
            ):
                sig[0] = sig[0] or number
                sig[1] = sig[1] or head
                ids.append(doc_id)
                break
        else:
            buckets.append(([number, head], [doc_id]))
    return [ids for _, ids in buckets]


def reconcile_documents(session: Session, titles: set[str] | None = None) -> int:
    """更新自动归并。active=False 只用作人工撤销标记，后续重跑不会覆盖；
    自动判断不再归并的直接删掉映射，以后内容一致了还能重新归并。"""
    stmt = (
        select(Document, DocumentDetail, Source)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .order_by(Document.id)
    )
    if titles is not None:
        normalized_titles = {re.sub(r"\s+", "", title) for title in titles}
        stmt = stmt.where(
            # MySQL 的 REGEXP_REPLACE 默认替换全部匹配
            func.regexp_replace(Document.title, r"\s+", "").in_(normalized_titles)
        )
    by_key: dict[str, list[tuple[int, tuple[str | None, str | None]]]] = defaultdict(list)
    ids: set[int] = set()
    for doc, detail, source in session.execute(stmt):
        ids.add(doc.id)
        key = match_key(doc, detail, source)
        if key:
            by_key[key].append((doc.id, _signature(detail)))
    groups: dict[str, list[int]] = {}
    for key, members in by_key.items():
        for n, group in enumerate(_split_by_content(members)):
            groups[key if n == 0 else f"{key}|{n}"] = group
    if not ids:
        return 0
    mappings = {
        row.duplicate_id: row
        for row in session.scalars(select(DocumentMerge).where(DocumentMerge.duplicate_id.in_(ids)))
    }
    desired = {
        duplicate: (members[0], key) for key, members in groups.items() for duplicate in members[1:]
    }
    changed = 0
    for duplicate, (canonical, key) in desired.items():
        row = mappings.get(duplicate)
        if row is None:
            session.add(
                DocumentMerge(
                    duplicate_id=duplicate, canonical_id=canonical, active=True, match_key=key
                )
            )
            changed += 1
        elif row.active and (row.canonical_id != canonical or row.match_key != key):
            row.canonical_id, row.match_key = canonical, key
            changed += 1
    for duplicate, row in mappings.items():
        if row.active and duplicate not in desired:
            session.delete(row)
            changed += 1
    session.flush()
    sync_reviewed_twins(session, {canonical for canonical, _ in desired.values()})
    return changed


# 核对结果随组同步时要复制的字段
_REVIEW_FIELDS = (
    "status",
    "reviewed_by",
    "reviewed_at",
    "approved_date",
    "approved_time",
    "change_reason",
    "changed_to_document_id",
    "changed_by",
    "changed_at",
)


def reviewed_key(row, status: str) -> tuple:
    """判断"同一截止已核对过"用的键：通过、已变更的看（类型, 日期, 时间）——日期已确认是这类截止；
    驳回的还要证据句相同——免得把真正的截止错当成已驳回的那句（如"有效期至10月18日"）。"""
    base = (row.kind, row.candidate_date, row.candidate_time)
    return base if status in ("approved", "changed") else (*base, row.evidence)


def sync_reviewed_twins(session: Session, masters: set[int]) -> int:
    """归并组里还在待核对、但同组已有同一截止核对过的（见 reviewed_key），沿用那条的核对结果
    （通过、驳回、已变更），免得同一截止换个网址又送审，或已变更的又被通过。"""
    from youju.models import DocumentDeadline

    synced = 0
    for members in groups_for(session, sorted(masters)).values():
        if len(members) < 2:
            continue
        rows = session.scalars(
            select(DocumentDeadline)
            .where(DocumentDeadline.document_id.in_(members))
            .order_by(DocumentDeadline.id)
            .with_for_update()
            .execution_options(populate_existing=True)  # 读最新状态，别用会话里的旧值
        ).all()
        # 先找证据句也相同的已核对记录（任何结果），找不到再按（类型, 日期, 时间）找已通过、已变更的
        exact: dict[tuple, object] = {}
        confirmed: dict[tuple, object] = {}
        for d in rows:
            if d.status != "pending":
                exact.setdefault(reviewed_key(d, "rejected"), d)
                if d.status in ("approved", "changed"):
                    confirmed.setdefault(reviewed_key(d, "approved"), d)
        for d in rows:
            if d.status != "pending":
                continue
            source = exact.get(reviewed_key(d, "rejected")) or confirmed.get(
                reviewed_key(d, "approved")
            )
            if source is not None:
                for field in _REVIEW_FIELDS:
                    setattr(d, field, getattr(source, field))
                synced += 1
    return synced


def visible_documents(stmt: Select) -> Select:
    return stmt.where(
        ~Document.id.in_(select(DocumentMerge.duplicate_id).where(DocumentMerge.active.is_(True)))
    )


def canonical_ids(session: Session, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    return {
        row.duplicate_id: row.canonical_id
        for row in session.scalars(
            select(DocumentMerge).where(
                DocumentMerge.active.is_(True), DocumentMerge.duplicate_id.in_(ids)
            )
        )
    }


def group_ids(session: Session, canonical_id: int) -> list[int]:
    return groups_for(session, [canonical_id])[canonical_id]


def groups_for(session: Session, canonical_ids: list[int]) -> dict[int, list[int]]:
    groups = {doc_id: [doc_id] for doc_id in canonical_ids}
    if groups:
        rows = session.execute(
            select(DocumentMerge.canonical_id, DocumentMerge.duplicate_id)
            .where(
                DocumentMerge.canonical_id.in_(groups),
                DocumentMerge.active.is_(True),
            )
            .order_by(DocumentMerge.duplicate_id)
        )
        for canonical_id, duplicate_id in rows:
            groups[canonical_id].append(duplicate_id)
    return groups
