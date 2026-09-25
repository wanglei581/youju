"""只读 API：来源、文件题录、统计。现阶段供内部使用和小程序开发联调。"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from youju.db import get_session
from youju.models import Document, Source

app = FastAPI(title="有据 API", version="0.1.0")

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
    q: Annotated[str | None, Query(max_length=100, description="标题关键词")] = None,
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


@app.get("/api/documents/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, session: SessionDep) -> DocumentOut:
    row = session.execute(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id == doc_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份文件")
    return _document_out(*row)


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
