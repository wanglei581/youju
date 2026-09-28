"""公开只读 API 与内部截止日期核对接口。"""

from __future__ import annotations

import json
import logging
import time as clock
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from youju.api.exports import router as exports_router
from youju.api.me import router as me_router
from youju.api.v1 import _approved, _details
from youju.api.v1 import router as v1_router
from youju.classify import list_status
from youju.config import get_settings
from youju.db import get_session
from youju.dedup import canonical_ids, group_ids, groups_for, visible_documents
from youju.lists.rules import candidate_key, is_person_query, normalize_name
from youju.models import Document, DocumentDeadline, DocumentDetail, Feedback, ListEntry, Source

_DOCS = get_settings().role == "all"  # 只在本机开发开放接口文档，线上两个服务都关闭
app = FastAPI(
    title="有据 API",
    version="0.1.0",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
app.include_router(v1_router)
app.include_router(exports_router)
app.include_router(me_router)

audit_log = logging.getLogger("youju.audit")
if not audit_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    audit_log.addHandler(_handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False


@app.middleware("http")
async def _role_guard(request: Request, call_next):
    """按部署角色开放路径：api 服务不开放核对台和管理接口，admin 服务不开放小程序接口（见 #32）。"""
    role = getattr(get_settings(), "role", "all")
    path = request.url.path
    admin_path = path == "/admin" or path.startswith(("/admin/", "/api/admin/"))
    blocked = (role == "api" and admin_path) or (
        role == "admin" and not (admin_path or path == "/health")
    )
    if blocked:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


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
    original_urls: list[str]


class ApprovedDeadlineOut(BaseModel):
    kind: str
    date: date
    time: str | None


class PendingDeadlineOut(BaseModel):
    id: int
    document_id: int
    title: str
    url: str
    publish_date: date | None
    source: str
    kind: str
    candidate_date: date
    candidate_time: str | None
    evidence: str
    confidence: str


class ReviewedDeadlineOut(BaseModel):
    """核对台里已通过或已标变更的截止。"""

    id: int
    document_id: int
    title: str
    url: str
    publish_date: date | None
    source: str
    kind: str
    approved_date: date
    approved_time: str | None
    status: str
    change_reason: str | None
    changed_to_title: str | None


CHANGE_REASONS = ("延期", "调整", "撤销")


class ChangeIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)
    reason: str = Field(description="延期 / 调整 / 撤销")
    new_document_id: int = Field(description="发布这项变更的通知（延期、调整、撤销通知）")


class AdminDocumentOut(BaseModel):
    id: int
    title: str
    url: str
    publish_date: date | None
    source: str


class FeedbackOut(BaseModel):
    id: int
    createdAt: datetime
    kind: Literal["申请覆盖", "纠错"]
    content: str
    checkResult: str
    docId: int | None
    docTitle: str | None
    docUrl: str | None
    handledAt: datetime | None


class UnchangeIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)


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
    status: str | None = Field(
        description="名单状态，按标题用词照录：公示 / 公布；标题没写的为空（D-018）"
    )
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


def _list_match(
    entry: ListEntry, doc: Document, detail: DocumentDetail | None, source_doc_url: str
) -> ListMatchOut:
    return ListMatchOut(
        name=entry.name,
        credit_code=entry.credit_code,
        category=entry.category,
        batch=entry.batch,
        status=list_status(doc.title),
        publish_date=(detail.publish_date if detail else None) or doc.publish_date,
        issuer=detail.issuer if detail else None,
        original_url=doc.url,
        attachment_url=(
            entry.source_url
            if entry.source_url
            not in {source_doc_url, doc.url, detail.final_url if detail else None}
            else None
        ),
        sequence=entry.sequence,
        region=entry.region,
    )


def _unique_list_rows(
    session: Session,
    rows: list[tuple[ListEntry, Document, DocumentDetail | None]],
) -> list[ListMatchOut]:
    found: dict[tuple[int, str], ListMatchOut] = {}
    mapping = canonical_ids(session, [doc.id for _, doc, _ in rows])
    masters = {master_id: session.get(Document, master_id) for master_id in set(mapping.values())}
    details = _details(session, list(masters))
    for entry, doc, detail in rows:
        source_doc_url = doc.url
        master_id = mapping.get(doc.id, doc.id)
        key = (master_id, entry.normalized_name)
        if master_id != doc.id:
            doc = masters[master_id]
            detail = details.get(master_id)
        match = _list_match(entry, doc, detail, source_doc_url)
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
        exact_matches=_unique_list_rows(session, exact_rows),
        similar_candidates=_unique_list_rows(session, similar_rows),
    )


CRON_HEARTBEAT = Path("/tmp/youju-cron-heartbeat")
_HEARTBEAT_MAX_AGE = 900


@app.get("/health")
def health(session: SessionDep) -> dict[str, str]:
    """健康检查：数据库连得上；admin 服务还要求定时任务心跳 15 分钟内更新过
    （cron 每 5 分钟写一次）。cron 停了就报不健康，由云托管重启容器，不会悄悄停掉每天的检查。"""
    session.execute(text("select 1"))
    if getattr(get_settings(), "role", "all") == "admin":
        try:
            age = clock.time() - CRON_HEARTBEAT.stat().st_mtime
        except OSError:
            age = None
        if age is None or age > _HEARTBEAT_MAX_AGE:
            raise HTTPException(status_code=503, detail="定时任务没有在运行")
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
    stmt = visible_documents(select(Document, Source).join(Source, Document.source_id == Source.id))
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
    doc_id = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
    row = session.execute(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id == doc_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份文件")
    doc, source = row
    detail = _details(session, [doc.id]).get(doc.id)
    approved = _approved(session, [doc.id]).get(doc.id, [])
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
        original_urls=[
            d.url
            for d in session.scalars(
                select(Document)
                .where(Document.id.in_(group_ids(session, doc.id)))
                .order_by(Document.id)
            )
        ],
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


# 管理口令输错限流（admin 服务固定 1 个实例，计数放在内存里）：同一来源 10 分钟内错 5 次锁定。
# 不设全局锁定——否则别人故意输错就能把管理员也锁在外面；
# 口令至少 32 位随机字符，换来源地址猜也猜不中。
# 计数表最多记 10000 个来源，超出时先清掉过期的，仍超出就整表清空，防止伪造地址把内存撑大
_FAIL_WINDOW = 600
_FAILS_PER_SOURCE = 5
_MAX_SOURCES = 10000
_admin_fails: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    """客户端地址：云托管把它放在 X-Original-Forwarded-For（官方文档：客户端 IPv4 或 IPv6 地址）。

    取最后一项：平台覆盖这个头时只有一项；若平台是在请求方自带的值后面追加，最后一项才是平台写的，
    取第一项就能被伪造。平台实际是哪种，首次部署时实测（部署文档"待验证"）。
    """
    forwarded = request.headers.get("x-original-forwarded-for") or ""
    return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "")


def _recent(entries: deque[float], now: float) -> deque[float]:
    while entries and now - entries[0] > _FAIL_WINDOW:
        entries.popleft()
    return entries


def _require_admin(request: Request, x_admin_token: Annotated[str | None, Header()] = None) -> None:
    now = clock.monotonic()
    ip = client_ip(request)
    if len(_admin_fails) > _MAX_SOURCES:
        for key in [k for k, v in _admin_fails.items() if not _recent(v, now)]:
            del _admin_fails[key]
        if len(_admin_fails) > _MAX_SOURCES:
            _admin_fails.clear()
    mine = _recent(_admin_fails[ip], now)
    if len(mine) >= _FAILS_PER_SOURCE:
        raise HTTPException(status_code=429, detail="口令输错次数太多，请 10 分钟后再试")
    expected = get_settings().admin_token
    if not expected or not x_admin_token or not compare_digest(x_admin_token, expected):
        mine.append(now)
        audit_log.warning(json.dumps({"event": "admin_auth_failed", "ip": ip}, ensure_ascii=False))
        raise HTTPException(status_code=401, detail="管理接口未授权")


def _audit(request: Request, action: str, **fields: object) -> None:
    """核对台操作留痕（操作人、来源、时间），打到日志，按网络日志留存要求保存（见部署说明）。"""
    record = {"event": "admin", "action": action, "ip": client_ip(request), **fields}
    audit_log.info(json.dumps(record, ensure_ascii=False, default=str))


AdminDep = Annotated[None, Depends(_require_admin)]


@app.get("/api/admin/deadlines/count")
def pending_count(session: SessionDep, _admin: AdminDep) -> dict[str, int]:
    """待核对条数，与列表同一规则：归并组里相同的候选只算一条。"""
    pending = session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.status == "pending")
    ).all()
    return {"pending": len(_pending_shown_ids(session, pending))}


def _pending_shown_ids(session: Session, deadlines: list[DocumentDeadline]) -> dict[int, int]:
    """这些候选里要显示的：{候选编号: 主文件编号}。同一归并组里类型、日期、时间、证据句都相同的
    待核对候选只显示编号最小的一条（看的是整组，不只这一批），翻页和计数都按这个规则。"""
    if not deadlines:
        return {}
    mapping = canonical_ids(session, [d.document_id for d in deadlines])
    masters = {mapping.get(d.document_id, d.document_id) for d in deadlines}
    members = groups_for(session, list(masters))
    master_of = {i: master for master, ids in members.items() for i in ids}
    best: dict[tuple, int] = {}
    for d in session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id.in_(list(master_of)),
            DocumentDeadline.status == "pending",
        )
    ):
        key = (master_of[d.document_id], d.kind, d.candidate_date, d.candidate_time, d.evidence)
        best[key] = min(best.get(key, d.id), d.id)
    shown = set(best.values())
    return {d.id: master_of[d.document_id] for d in deadlines if d.id in shown}


@app.get("/admin/review", response_class=HTMLResponse, include_in_schema=False)
def review_console() -> str:
    """截止日期核对台：静态页面，调用上面的管理接口（口令由使用者在页面里输入）。"""
    return (Path(__file__).resolve().parent.parent / "admin" / "review.html").read_text(
        encoding="utf-8"
    )


@app.get("/admin/feedback", response_class=HTMLResponse, include_in_schema=False)
def feedback_console() -> str:
    """纠错和申请覆盖核对台；口令仅由页面请求管理接口时发送。"""
    return (Path(__file__).resolve().parent.parent / "admin" / "feedback.html").read_text(
        encoding="utf-8"
    )


@app.get("/api/admin/feedback/count")
def feedback_pending_count(session: SessionDep, _admin: AdminDep) -> dict[str, int]:
    return {
        "pending": session.scalar(
            select(func.count(Feedback.id)).where(Feedback.handled_at.is_(None))
        )
        or 0
    }


@app.get("/api/admin/feedback", response_model=list[FeedbackOut])
def admin_feedback(
    session: SessionDep,
    _admin: AdminDep,
    status: Literal["pending", "all"] = "pending",
    before_id: Annotated[int | None, Query(ge=1, description="翻页游标：只取编号更小的")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[FeedbackOut]:
    stmt = select(Feedback)
    if status == "pending":
        stmt = stmt.where(Feedback.handled_at.is_(None))
    if before_id is not None:
        stmt = stmt.where(Feedback.id < before_id)
    rows = session.scalars(stmt.order_by(Feedback.id.desc()).limit(limit)).all()
    mapping = canonical_ids(session, [row.document_id for row in rows if row.document_id])
    doc_ids = {mapping.get(row.document_id, row.document_id) for row in rows if row.document_id}
    documents = {
        doc.id: doc
        for doc in session.scalars(select(Document).where(Document.id.in_(doc_ids))).all()
    }
    result = []
    for row in rows:
        doc = documents.get(mapping.get(row.document_id, row.document_id))
        result.append(
            FeedbackOut(
                id=row.id,
                createdAt=row.created_at,
                kind="申请覆盖" if row.content.startswith("申请覆盖：") else "纠错",
                content=row.content,
                checkResult=row.check_result,
                docId=doc.id if doc else None,
                docTitle=doc.title if doc else None,
                docUrl=doc.url if doc else None,
                handledAt=row.handled_at,
            )
        )
    return result


def _set_feedback_handled(feedback_id: int, session: Session, handled: bool) -> dict[str, bool]:
    row = session.scalar(select(Feedback).where(Feedback.id == feedback_id).with_for_update())
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到反馈")
    changed = (row.handled_at is None) == handled
    if changed:
        row.handled_at = datetime.now(UTC) if handled else None
        session.commit()
    return {"handled": handled, "changed": changed}


@app.post("/api/admin/feedback/{feedback_id}/handle")
def handle_feedback(
    feedback_id: int, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, bool]:
    result = _set_feedback_handled(feedback_id, session, True)
    _audit(request, "feedback_handle", feedback_id=feedback_id, changed=result["changed"])
    return result


@app.post("/api/admin/feedback/{feedback_id}/unhandle")
def unhandle_feedback(
    feedback_id: int, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, bool]:
    result = _set_feedback_handled(feedback_id, session, False)
    _audit(request, "feedback_unhandle", feedback_id=feedback_id, changed=result["changed"])
    return result


@app.get("/api/admin/deadlines", response_model=list[PendingDeadlineOut])
def pending_deadlines(
    session: SessionDep,
    _admin: AdminDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1, description="翻页游标：只取编号更小的")] = None,
) -> list[PendingDeadlineOut]:
    """待核对的候选，按编号从新到旧；翻页用上一页最小的编号作 before_id，不受别人同时核对的影响。

    归并后同一通知的不同网址可能各有一份相同的候选（类型、日期、时间、证据句都相同）：只列编号最小的
    那条（之后新抽出的副本编号更大，不会把已在翻页范围里的这条挤掉），标题和链接用主文件；
    本页凑不满就继续往下取，返回不足 limit 条说明已经取完。
    """
    base = (
        select(DocumentDeadline, Document, Source)
        .join(Document, DocumentDeadline.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(DocumentDeadline.status == "pending")
    )
    rows: list[tuple[DocumentDeadline, Document, Source]] = []
    cursor = before_id
    while len(rows) < limit:
        stmt = base if cursor is None else base.where(DocumentDeadline.id < cursor)
        batch = session.execute(stmt.order_by(DocumentDeadline.id.desc()).limit(limit)).all()
        if not batch:
            break
        cursor = batch[-1][0].id
        shown = _pending_shown_ids(session, [d for d, _, _ in batch])
        for deadline, doc, src in batch:
            if deadline.id in shown:
                master_id = shown[deadline.id]
                master = doc if master_id == doc.id else session.get(Document, master_id)
                rows.append((deadline, master, src if master_id == doc.id else master.source))
    rows = rows[:limit]
    return [
        PendingDeadlineOut(
            id=d.id,
            document_id=doc.id,
            title=doc.title,
            url=doc.url,
            publish_date=doc.publish_date,
            source=src.dept_full_name,
            kind=d.kind,
            candidate_date=d.candidate_date,
            candidate_time=d.candidate_time.strftime("%H:%M") if d.candidate_time else None,
            evidence=d.evidence,
            confidence=d.confidence,
        )
        for d, doc, src in rows
    ]


def _review(deadline_id: int, body: ReviewIn, session: Session, status: str) -> dict[str, str]:
    row = session.get(DocumentDeadline, deadline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到候选")
    # 归并组里同一候选（类型、日期、时间、证据句都相同）的其他副本一并按同样结果处理，免得重复核对。
    # 按编号顺序一次锁住本条和这些副本（并发核对时不会互相等待成死锁），锁住后再看状态
    master = canonical_ids(session, [row.document_id]).get(row.document_id, row.document_id)
    members = groups_for(session, [master])[master]
    locked = session.scalars(
        select(DocumentDeadline)
        .where(
            DocumentDeadline.document_id.in_(members),
            DocumentDeadline.kind == row.kind,
            DocumentDeadline.candidate_date == row.candidate_date,
            DocumentDeadline.candidate_time.is_(None)
            if row.candidate_time is None
            else DocumentDeadline.candidate_time == row.candidate_time,
            DocumentDeadline.evidence == row.evidence,
        )
        .order_by(DocumentDeadline.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="候选已核对")
    if status == "rejected" and (body.new_date is not None or body.new_time is not None):
        raise HTTPException(status_code=422, detail="驳回时不能修改日期")
    # 区分"没传时间"（沿用候选时间）和"明确清空时间"（time 传 null：原文只写到日期）
    if body.new_time is not None:
        from datetime import time

        try:
            parsed_time = time.fromisoformat(body.new_time)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="时间格式应为 HH:MM") from exc
    elif "new_time" in body.model_fields_set:
        parsed_time = None
    else:
        parsed_time = row.candidate_time
    twins = [d for d in locked if d.id != row.id and d.status == "pending"]
    now = datetime.now(UTC)
    for item in {row, *twins}:
        item.status = status
        item.reviewed_by = body.reviewed_by
        item.reviewed_at = now
        if status == "approved":
            item.approved_date = body.new_date or row.candidate_date
            item.approved_time = parsed_time
    session.commit()
    return {"status": status}


@app.post("/api/admin/deadlines/{deadline_id}/approve")
def approve_deadline(
    deadline_id: int, body: ReviewIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    result = _review(deadline_id, body, session, "approved")
    _audit(
        request,
        "approve",
        deadline_id=deadline_id,
        reviewer=body.reviewed_by,
        date=body.new_date,
        time=body.new_time,
    )
    return result


@app.post("/api/admin/deadlines/{deadline_id}/reject")
def reject_deadline(
    deadline_id: int, body: ReviewIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    result = _review(deadline_id, body, session, "rejected")
    _audit(request, "reject", deadline_id=deadline_id, reviewer=body.reviewed_by)
    return result


@app.get("/api/admin/deadlines/approved", response_model=list[ReviewedDeadlineOut])
def approved_deadlines(
    session: SessionDep,
    _admin: AdminDep,
    q: Annotated[str, Query(min_length=2, max_length=100, description="原通知标题里的词")],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[ReviewedDeadlineOut]:
    """找已通过（或已标变更）的截止，用于把原通知的截止标为已变更。标题里各词都要出现。"""
    words = [w for w in q.split() if w][:5]
    stmt = (
        select(DocumentDeadline, Document, Source)
        .join(Document, DocumentDeadline.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(DocumentDeadline.status.in_(("approved", "changed")))
    )
    for word in words:
        stmt = stmt.where(Document.title.like(f"%{_escape_like(word)}%", escape="\\"))
    # 分批取，按归并组去重，直到凑够 limit 个不同的截止（同一通知的副本不会占满名额）
    ordered = stmt.order_by(Document.publish_date.desc().nulls_last(), DocumentDeadline.id.desc())
    rows: list = []
    keys: set[tuple] = set()
    offset = 0
    while True:
        batch = session.execute(ordered.limit(limit * 5).offset(offset)).all()
        offset += len(batch)
        batch_map = canonical_ids(session, [doc.id for _, doc, _ in batch])
        for d, doc, src in batch:
            keys.add((batch_map.get(doc.id, doc.id), d.kind, d.approved_date, d.approved_time))
            rows.append((d, doc, src))
        if len(batch) < limit * 5 or len(keys) >= limit or offset >= 2000:
            break
    # 同组同时有"已通过"和"已变更"时先列"已通过"的：它仍作为有效截止显示，需要处理
    rows = sorted(rows, key=lambda r: r[0].status != "approved")
    new_ids = {d.changed_to_document_id for d, _, _ in rows if d.changed_to_document_id}
    titles = dict(
        session.execute(select(Document.id, Document.title).where(Document.id.in_(new_ids))).all()
    )
    # 同一通知的不同网址（归并组）里相同的截止只列一条，标题和链接用主文件
    mapping = canonical_ids(session, [doc.id for _, doc, _ in rows])
    out: dict[tuple, ReviewedDeadlineOut] = {}
    for d, doc, src in rows:
        if d.approved_date is None:
            continue
        master_id = mapping.get(doc.id, doc.id)
        key = (master_id, d.kind, d.approved_date, d.approved_time)
        if key in out:
            continue
        if master_id != doc.id:
            doc = session.get(Document, master_id)
            src = doc.source
        out[key] = ReviewedDeadlineOut(
            id=d.id,
            document_id=doc.id,
            title=doc.title,
            url=doc.url,
            publish_date=doc.publish_date,
            source=src.dept_full_name,
            kind=d.kind,
            approved_date=d.approved_date,
            approved_time=d.approved_time.strftime("%H:%M") if d.approved_time else None,
            status=d.status,
            change_reason=d.change_reason,
            changed_to_title=titles.get(d.changed_to_document_id),
        )
    items = sorted(
        out.values(),
        key=lambda o: (o.publish_date or date.min, o.id),
        reverse=True,
    )
    return items[:limit]


@app.post("/api/admin/deadlines/{deadline_id}/change")
def change_deadline(
    deadline_id: int, body: ChangeIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    """原通知已通过的截止标为已变更（延期、调整、撤销），可关联发布变更的新通知。"""
    if body.reason not in CHANGE_REASONS:
        raise HTTPException(status_code=422, detail="变更原因只能是延期、调整或撤销")
    row = session.get(DocumentDeadline, deadline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到截止记录")
    group = _lock_group(session, row)
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="只有已通过的截止才能标为已变更")
    same = canonical_ids(session, [body.new_document_id, row.document_id])
    if same.get(body.new_document_id, body.new_document_id) == same.get(
        row.document_id, row.document_id
    ):
        raise HTTPException(status_code=422, detail="新通知不能是原通知本身")
    new_doc = session.get(Document, body.new_document_id)
    if new_doc is None:
        raise HTTPException(status_code=404, detail="没有找到新通知")
    old_doc = session.get(Document, row.document_id)
    # 防止关联到无关文件：变更通知应与原通知同一地区，且发布不早于原通知
    if new_doc.source.province != old_doc.source.province:
        raise HTTPException(status_code=422, detail="新通知与原通知不是同一地区，请核对")
    if new_doc.publish_date is None or old_doc.publish_date is None:
        raise HTTPException(status_code=422, detail="发布日期不详，无法核对先后，请先到原文核对")
    if new_doc.publish_date < old_doc.publish_date:
        raise HTTPException(status_code=422, detail="新通知的发布日期早于原通知，请核对")
    # 归并组里相同的已通过副本（同一通知的其他网址）一并标记，否则副本仍会被当作有效截止显示
    now = datetime.now(UTC)
    for item in [d for d in group if d.status == "approved"]:
        item.status = "changed"
        item.change_reason = body.reason
        item.changed_to_document_id = body.new_document_id
        item.changed_by = body.reviewed_by
        item.changed_at = now
    session.commit()
    _audit(
        request,
        "change",
        deadline_id=deadline_id,
        reviewer=body.reviewed_by,
        reason=body.reason,
        new_document_id=body.new_document_id,
    )
    return {"status": "changed"}


def _lock_group(session: Session, row: DocumentDeadline) -> list[DocumentDeadline]:
    """按编号顺序一次锁住这条截止和归并组里类型、核定日期时间都相同的副本（含自己），
    并刷新成最新状态；并发操作时不会互相等待成死锁，调用方锁住后再判断状态。"""
    master = canonical_ids(session, [row.document_id]).get(row.document_id, row.document_id)
    members = groups_for(session, [master])[master]
    same_time = (
        DocumentDeadline.approved_time.is_(None)
        if row.approved_time is None
        else DocumentDeadline.approved_time == row.approved_time
    )
    group = (
        and_(
            DocumentDeadline.document_id.in_(members),
            DocumentDeadline.kind == row.kind,
            DocumentDeadline.approved_date == row.approved_date,
            same_time,
        )
        if row.approved_date is not None
        else DocumentDeadline.id == row.id
    )
    return list(
        session.scalars(
            select(DocumentDeadline)
            .where(or_(DocumentDeadline.id == row.id, group))
            .order_by(DocumentDeadline.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


@app.post("/api/admin/deadlines/{deadline_id}/unchange")
def unchange_deadline(
    deadline_id: int, body: UnchangeIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    """标错了可撤回：恢复为已通过。"""
    row = session.get(DocumentDeadline, deadline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到截止记录")
    group = _lock_group(session, row)
    if row.status != "changed":
        raise HTTPException(status_code=409, detail="这条截止没有标为已变更")
    for item in [d for d in group if d.status == "changed"]:
        item.status = "approved"
        item.change_reason = None
        item.changed_to_document_id = None
        item.changed_by = None
        item.changed_at = None
    session.commit()
    _audit(request, "unchange", deadline_id=deadline_id, reviewer=body.reviewed_by)
    return {"status": "approved"}


@app.get("/api/admin/documents", response_model=list[AdminDocumentOut])
def admin_documents(
    session: SessionDep,
    _admin: AdminDep,
    q: Annotated[str, Query(min_length=2, max_length=100, description="新通知标题里的词")],
    limit: Annotated[int, Query(ge=1, le=30)] = 15,
) -> list[AdminDocumentOut]:
    """找发布变更的新通知（延期、调整、撤销通知），标题里各词都要出现。"""
    stmt = select(Document, Source).join(Source, Document.source_id == Source.id)
    for word in [w for w in q.split() if w][:5]:
        stmt = stmt.where(Document.title.like(f"%{_escape_like(word)}%", escape="\\"))
    rows = session.execute(
        stmt.order_by(Document.publish_date.desc().nulls_last(), Document.id.desc()).limit(limit)
    ).all()
    return [
        AdminDocumentOut(
            id=doc.id,
            title=doc.title,
            url=doc.url,
            publish_date=doc.publish_date,
            source=src.dept_full_name,
        )
        for doc, src in rows
    ]


@app.get("/api/stats", response_model=Stats)
def stats(session: SessionDep) -> Stats:
    week_ago = datetime.now(UTC) - timedelta(days=7)
    by_health = dict(
        session.execute(select(Source.health, func.count()).group_by(Source.health)).all()
    )
    by_province = dict(
        session.execute(
            visible_documents(
                select(Source.province, func.count(Document.id)).join(
                    Document, Document.source_id == Source.id
                )
            ).group_by(Source.province)
        ).all()
    )
    return Stats(
        sources_total=session.scalar(select(func.count(Source.id))) or 0,
        sources_enabled=session.scalar(select(func.count(Source.id)).where(Source.enabled)) or 0,
        sources_by_health=by_health,
        documents_total=session.scalar(visible_documents(select(func.count(Document.id)))) or 0,
        documents_new_7d=session.scalar(
            visible_documents(select(func.count(Document.id))).where(
                Document.is_baseline.is_(False), Document.first_seen_at >= week_ago
            )
        )
        or 0,
        documents_by_province=by_province,
    )
