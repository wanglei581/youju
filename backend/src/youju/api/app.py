"""公开只读 API 与内部截止日期核对接口。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from youju.api.v1 import router as v1_router
from youju.config import get_settings
from youju.db import get_session
from youju.lists.rules import candidate_key, is_person_query, normalize_name
from youju.models import Document, DocumentDeadline, DocumentDetail, ListEntry, Source

app = FastAPI(title="有据 API", version="0.1.0")
app.include_router(v1_router)

SessionDep = Annotated[Session, Depends(get_session)]


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    level: str
    province: str
    dept: str
    dept_full_name: str
    column_name: str
    url: str
    fetch_mode: str
    enabled: bool
    health: str
    consecutive_failures: int
    last_check_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None


class DocumentOut(BaseModel):
    id: int
    title: str
    url: str
    publish_date: date | None
    first_seen_at: datetime
    is_baseline: bool
    source_id: int
    province: str
    dept: str
    column_name: str


class AttachmentOut(BaseModel):
    name: str
    url: str
    extension: str


class DetailOut(BaseModel):
    issuer: str | None
    doc_number: str | None
    publish_date: date | None
    fetched_at: datetime
    http_status: int | None
    status: str
    failure_reason: str | None
    attachments: list[AttachmentOut]


class DocumentWithDetailOut(DocumentOut):
    detail: DetailOut | None
    deadlines: list[ApprovedDeadlineOut]
    deadline_text: str


class ApprovedDeadlineOut(BaseModel):
    kind: str
    date: date
    time: str | None


class PendingDeadlineOut(BaseModel):
    id: int
    document_id: int
    title: str
    url: str
    kind: str
    candidate_date: date
    candidate_time: str | None
    evidence: str
    confidence: str


class ReviewIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)
    new_date: date | None = Field(default=None, alias="date")
    new_time: str | None = Field(default=None, alias="time")


class DocumentPage(BaseModel):
    total: int
    items: list[DocumentOut]


class Stats(BaseModel):
    sources_total: int
    sources_enabled: int
    sources_by_health: dict[str, int]
    documents_total: int
    documents_new_7d: int
    documents_by_province: dict[str, int]


class ListMatchOut(BaseModel):
    name: str
    credit_code: str | None
    category: str
    batch: str | None
    status: str
    publish_date: date | None
    issuer: str | None
    original_url: str
    attachment_url: str | None
    sequence: str | None
    region: str | None


class ListSearchOut(BaseModel):
    exact_matches: list[ListMatchOut]
    similar_candidates: list[ListMatchOut]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _document_out(doc: Document, source: Source) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        url=doc.url,
        publish_date=doc.publish_date,
        first_seen_at=doc.first_seen_at,
        is_baseline=doc.is_baseline,
        source_id=source.id,
        province=source.province,
        dept=source.dept,
        column_name=source.column_name,
    )


def _list_match(entry: ListEntry, doc: Document, detail: DocumentDetail | None) -> ListMatchOut:
    return ListMatchOut(
        name=entry.name,
        credit_code=entry.credit_code,
        category=entry.category,
        batch=entry.batch,
        status=entry.status,
        publish_date=(detail.publish_date if detail else None) or doc.publish_date,
        issuer=detail.issuer if detail else None,
        original_url=doc.url,
        attachment_url=(
            entry.source_url
            if entry.source_url not in {doc.url, detail.final_url if detail else None}
            else None
        ),
        sequence=entry.sequence,
        region=entry.region,
    )


def _unique_list_rows(
    rows: list[tuple[ListEntry, Document, DocumentDetail | None]],
) -> list[ListMatchOut]:
    found: dict[tuple[int, str], ListMatchOut] = {}
    for entry, doc, detail in rows:
        key = (doc.id, entry.normalized_name)
        match = _list_match(entry, doc, detail)
        if key not in found or (match.attachment_url and not found[key].attachment_url):
            found[key] = match
    return list(found.values())


@app.get("/api/lists/search", response_model=ListSearchOut)
def search_lists(
    session: SessionDep,
    q: Annotated[
        str, Query(min_length=2, max_length=100, description="企业全称或统一社会信用代码")
    ],
) -> ListSearchOut:
    query = normalize_name(q)
    if is_person_query(query):
        raise HTTPException(status_code=422, detail="只查企业和机构")
    if not query:
        raise HTTPException(status_code=422, detail="请输入企业或机构全称")
    exact_rows = session.execute(
        select(ListEntry, Document, DocumentDetail)
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(
            Source.rights_level == 1,
            (ListEntry.normalized_name == query) | (ListEntry.credit_code == query.upper()),
        )
        .order_by(Document.publish_date.desc().nulls_last(), ListEntry.id)
        .limit(200)
    ).all()
    similar_rows = []
    if len(query) >= 4 and not query.isascii():
        prefix = candidate_key(query)[:4]
        if len(prefix) >= 2:
            rows = session.execute(
                select(ListEntry, Document, DocumentDetail)
                .join(Document, Document.id == ListEntry.document_id)
                .join(Source, Source.id == Document.source_id)
                .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
                .where(
                    Source.rights_level == 1,
                    ListEntry.normalized_name.like(f"%{_escape_like(prefix)}%", escape="\\"),
                )
                .order_by(Document.publish_date.desc().nulls_last(), ListEntry.id)
                .limit(500)
            ).all()
            similar_rows = [
                (entry, doc, detail)
                for entry, doc, detail in rows
                if entry.normalized_name != query
                and candidate_key(entry.normalized_name) == candidate_key(query)
            ][:50]
    return ListSearchOut(
        exact_matches=_unique_list_rows(exact_rows),
        similar_candidates=_unique_list_rows(similar_rows),
    )


@app.get("/health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/api/sources", response_model=list[SourceOut])
def list_sources(
    session: SessionDep,
    health: str | None = None,
    province: str | None = None,
    enabled: bool | None = None,
) -> list[Source]:
    stmt = select(Source).order_by(Source.level.desc(), Source.province, Source.dept, Source.id)
    if health:
        stmt = stmt.where(Source.health == health)
    if province:
        stmt = stmt.where(Source.province == province)
    if enabled is not None:
        stmt = stmt.where(Source.enabled.is_(enabled))
    return list(session.scalars(stmt).all())


@app.get("/api/documents", response_model=DocumentPage)
def list_documents(
    session: SessionDep,
    q: Annotated[
        str | None, Query(min_length=2, max_length=100, description="标题关键词，至少 2 个字")
    ] = None,
    province: str | None = None,
    dept: str | None = None,
    since: Annotated[date | None, Query(description="发布日期不早于")] = None,
    new_only: Annotated[bool, Query(description="只看首次检查之后新发现的")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentPage:
    stmt = select(Document, Source).join(Source, Document.source_id == Source.id)
    if q:
        stmt = stmt.where(Document.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if province:
        stmt = stmt.where(Source.province == province)
    if dept:
        stmt = stmt.where(Source.dept == dept)
    if since:
        stmt = stmt.where(Document.publish_date >= since)
    if new_only:
        stmt = stmt.where(Document.is_baseline.is_(False))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.execute(
        stmt.order_by(Document.publish_date.desc().nulls_last(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return DocumentPage(total=total, items=[_document_out(doc, src) for doc, src in rows])


@app.get("/api/documents/{doc_id}", response_model=DocumentWithDetailOut)
def get_document(doc_id: int, session: SessionDep) -> DocumentWithDetailOut:
    row = session.execute(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id == doc_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份文件")
    doc, source = row
    detail = session.get(DocumentDetail, doc.id)
    approved = session.scalars(
        select(DocumentDeadline)
        .where(DocumentDeadline.document_id == doc.id, DocumentDeadline.status == "approved")
        .order_by(DocumentDeadline.approved_date, DocumentDeadline.id)
    ).all()
    deadlines = [
        ApprovedDeadlineOut(
            kind=d.kind,
            date=d.approved_date,
            time=d.approved_time.strftime("%H:%M") if d.approved_time else None,
        )
        for d in approved
    ]
    return DocumentWithDetailOut(
        **_document_out(doc, source).model_dump(),
        deadlines=deadlines,
        deadline_text="；".join(
            f"{d.kind}：{d.date.isoformat()}" + (f" {d.time}" if d.time else "") for d in deadlines
        )
        if deadlines
        else "截止时间见原文",
        detail=DetailOut(
            issuer=detail.issuer,
            doc_number=detail.doc_number,
            publish_date=detail.publish_date,
            fetched_at=detail.fetched_at,
            http_status=detail.http_status,
            status=detail.status,
            failure_reason=detail.failure_reason,
            attachments=[
                AttachmentOut(name=a.name, url=a.url, extension=a.extension)
                for a in detail.attachments
            ],
        )
        if detail
        else None,
    )


def _require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().admin_token
    if not expected or not x_admin_token or not compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="管理接口未授权")


AdminDep = Annotated[None, Depends(_require_admin)]


@app.get("/api/admin/deadlines", response_model=list[PendingDeadlineOut])
def pending_deadlines(
    session: SessionDep,
    _admin: AdminDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PendingDeadlineOut]:
    rows = session.execute(
        select(DocumentDeadline, Document)
        .join(Document)
        .where(DocumentDeadline.status == "pending")
        .order_by(Document.publish_date.desc().nulls_last(), DocumentDeadline.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        PendingDeadlineOut(
            id=d.id,
            document_id=doc.id,
            title=doc.title,
            url=doc.url,
            kind=d.kind,
            candidate_date=d.candidate_date,
            candidate_time=d.candidate_time.strftime("%H:%M") if d.candidate_time else None,
            evidence=d.evidence,
            confidence=d.confidence,
        )
        for d, doc in rows
    ]


def _review(deadline_id: int, body: ReviewIn, session: Session, status: str) -> dict[str, str]:
    row = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.id == deadline_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到候选")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="候选已核对")
    if status == "rejected" and (body.new_date is not None or body.new_time is not None):
        raise HTTPException(status_code=422, detail="驳回时不能修改日期")
    if body.new_time is not None:
        from datetime import time

        try:
            parsed_time = time.fromisoformat(body.new_time)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="时间格式应为 HH:MM") from exc
    else:
        parsed_time = row.candidate_time
    row.status = status
    row.reviewed_by = body.reviewed_by
    row.reviewed_at = datetime.now(UTC)
    if status == "approved":
        row.approved_date = body.new_date or row.candidate_date
        row.approved_time = parsed_time
    session.commit()
    return {"status": status}


@app.post("/api/admin/deadlines/{deadline_id}/approve")
def approve_deadline(
    deadline_id: int, body: ReviewIn, session: SessionDep, _admin: AdminDep
) -> dict[str, str]:
    return _review(deadline_id, body, session, "approved")


@app.post("/api/admin/deadlines/{deadline_id}/reject")
def reject_deadline(
    deadline_id: int, body: ReviewIn, session: SessionDep, _admin: AdminDep
) -> dict[str, str]:
    return _review(deadline_id, body, session, "rejected")


@app.get("/api/stats", response_model=Stats)
def stats(session: SessionDep) -> Stats:
    week_ago = datetime.now(UTC) - timedelta(days=7)
    by_health = dict(
        session.execute(select(Source.health, func.count()).group_by(Source.health)).all()
    )
    by_province = dict(
        session.execute(
            select(Source.province, func.count(Document.id))
            .join(Document, Document.source_id == Source.id)
            .group_by(Source.province)
        ).all()
    )
    return Stats(
        sources_total=session.scalar(select(func.count(Source.id))) or 0,
        sources_enabled=session.scalar(select(func.count(Source.id)).where(Source.enabled)) or 0,
        sources_by_health=by_health,
        documents_total=session.scalar(select(func.count(Document.id))) or 0,
        documents_new_7d=session.scalar(
            select(func.count(Document.id)).where(
                Document.is_baseline.is_(False), Document.first_seen_at >= week_ago
            )
        )
        or 0,
        documents_by_province=by_province,
    )
