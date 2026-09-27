"""按标题、发文机关和发布日期归并展示；原始题录与衍生记录仍各自保留。"""

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


def reconcile_documents(session: Session, titles: set[str] | None = None) -> int:
    """更新自动归并；active=False 是人工撤销标记，后续重跑不会覆盖。"""
    stmt = (
        select(Document, DocumentDetail, Source)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .order_by(Document.id)
    )
    if titles is not None:
        normalized_titles = {re.sub(r"\s+", "", title) for title in titles}
        stmt = stmt.where(
            func.regexp_replace(Document.title, r"\s+", "", "g").in_(normalized_titles)
        )
    groups: dict[str, list[int]] = defaultdict(list)
    ids: set[int] = set()
    for doc, detail, source in session.execute(stmt):
        ids.add(doc.id)
        key = match_key(doc, detail, source)
        if key:
            groups[key].append(doc.id)
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
            row.active = False
            changed += 1
    return changed


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
