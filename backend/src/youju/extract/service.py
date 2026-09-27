"""把规则结果写入核对队列，已处理记录不会被自动重置。"""

import hashlib
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

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
        for candidate in extract_deadlines(
            detail.body_text, detail.publish_date or doc.publish_date
        ):
            fingerprint = hashlib.sha256(
                f"{candidate.due_date}|{candidate.due_time}|{candidate.evidence}".encode()
            ).hexdigest()
            generated.add(fingerprint)
            if fingerprint in existing:
                row = existing[fingerprint]
                if row.status == "pending":
                    row.kind = candidate.kind
                    row.confidence = candidate.confidence
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
    session.commit()
    return docs, added
