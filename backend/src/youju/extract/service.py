"""把规则结果写入核对队列，已处理记录不会被自动重置。"""

import hashlib
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.dedup import canonical_ids, groups_for, reviewed_key, sync_reviewed_twins
from youju.extract.deadlines import extract_deadlines
from youju.models import Document, DocumentDeadline, DocumentDetail, Source


def extract_existing(
    session: Session, *, since: date | None = None, document_ids: list[int] | None = None
) -> tuple[int, int]:
    stmt = (
        select(Document, DocumentDetail)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.status == "ok", DocumentDetail.body_text != "", Source.rights_level == 1
        )
        .order_by(Document.id)
    )
    if since is not None:
        stmt = stmt.where(Document.publish_date >= since)
    if document_ids is not None:
        if not document_ids:
            return 0, 0
        stmt = stmt.where(Document.id.in_(document_ids))
    docs = 0
    added = 0
    for doc, detail in session.execute(stmt):
        docs += 1
        existing = {
            row.fingerprint: row
            for row in session.scalars(
                select(DocumentDeadline).where(DocumentDeadline.document_id == doc.id)
            )
        }
        generated: set[str] = set()
        # 已核对过的截止（通过、驳回、已变更）：抽取规则或证据句截法变了，
        # 同一类型、同一日期时间的不再重复送审；不同类型的照常送审。
        # 看整个归并组（同一通知的其他网址），免得已标"已变更"的截止换个网址又被当作新候选送审
        master = canonical_ids(session, [doc.id]).get(doc.id, doc.id)
        members = groups_for(session, [master])[master]
        reviewed = {
            reviewed_key(row, row.status)
            for row in session.scalars(
                select(DocumentDeadline).where(
                    DocumentDeadline.document_id.in_(members),
                    DocumentDeadline.status != "pending",
                )
            )
        }
        for candidate in extract_deadlines(
            detail.body_text, detail.publish_date or doc.publish_date
        ):
            # 指纹含类型：同一句原文、同一日期里的不同事项（如企业申报和材料报送同日截止）各算一条
            fingerprint = hashlib.sha256(
                f"{candidate.kind}|{candidate.due_date}|{candidate.due_time}|{candidate.evidence}".encode()
            ).hexdigest()
            generated.add(fingerprint)
            if fingerprint in existing:
                row = existing[fingerprint]
                if row.status == "pending":
                    row.kind = candidate.kind
                    row.confidence = candidate.confidence
                continue
            probe = SimpleNamespace(
                kind=candidate.kind,
                candidate_date=candidate.due_date,
                candidate_time=candidate.due_time,
                evidence=candidate.evidence,
            )
            if reviewed_key(probe, "approved") in reviewed or (
                reviewed_key(probe, "rejected") in reviewed
            ):
                continue
            row = DocumentDeadline(
                document_id=doc.id,
                fingerprint=fingerprint,
                kind=candidate.kind,
                candidate_date=candidate.due_date,
                candidate_time=candidate.due_time,
                evidence=candidate.evidence,
                confidence=candidate.confidence,
                status="pending",
            )
            session.add(row)
            existing[fingerprint] = row
            added += 1
        for fingerprint, row in existing.items():
            if row.status == "pending" and fingerprint not in generated:
                session.delete(row)
        # 之前已抽出的待核对副本，若同组已有同一截止核对过，沿用核对结果
        session.flush()
        sync_reviewed_twins(session, {master})
    session.commit()
    return docs, added
