"""公开只读 API 与内部截止日期核对接口。"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import threading
import time as clock
from collections import defaultdict, deque
from datetime import UTC, date, datetime, time, timedelta
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from youju import access_log
from youju import fields as field_rules
from youju.alerts import send_alert
from youju.api import v1 as v1_api
from youju.api.exports import router as exports_router
from youju.api.me import router as me_router
from youju.api.v1 import _approved, _details
from youju.api.v1 import router as v1_router
from youju.classify import doc_kind, effective_kind, list_status
from youju.config import get_settings
from youju.db import get_session
from youju.dedup import canonical_ids, group_ids, groups_for, visible_documents
from youju.fields import SUPPORT_LABELS
from youju.lists.rules import candidate_key, is_person_query, normalize_name
from youju.models import (
    URL_KEY_MAX,
    AICheck,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    Feedback,
    ListEntry,
    Source,
)
from youju.sources.onboard import MIN_ITEMS

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


error_log = logging.getLogger("youju.api")
# 同一实例两次接口出错提醒至少间隔的秒数。计数在进程内：start.sh 用 --workers 1 固定单进程，
# 一个进程就是一个实例；实例多时每个实例各自最多 10 分钟一条
_ERROR_ALERT_INTERVAL = 600
_error_lock = threading.Lock()
_error_state: dict = {"last_alert": None, "count": 0}


def _send_in_background(message: str) -> None:
    # 发提醒要等网络，放到线程里，不挡住请求
    threading.Thread(target=send_alert, args=(message,), daemon=True).start()


def _note_server_error(sample: str) -> None:
    """记一次接口出错；距上次提醒超过 10 分钟就发一条，带上这期间的次数。"""
    now = clock.monotonic()
    with _error_lock:
        _error_state["count"] += 1
        last = _error_state["last_alert"]
        if last is not None and now - last < _ERROR_ALERT_INTERVAL:
            return
        count = _error_state["count"]
        _error_state.update(last_alert=now, count=0)
    role = getattr(get_settings(), "role", "all")
    _send_in_background(f"接口出错（{role} 服务，上次提醒以来 {count} 次）：{sample}")


def _printable(text: str, limit: int) -> str:
    """不可见字符（如换行）换成 ?，再截断：路径来自请求，不能用来伪造日志行或挤掉后面的内容。"""
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception) -> JSONResponse:
    """没被处理的异常：返回 500，并提醒到群里（完整堆栈由 uvicorn 记录，这里只记一行）。
    提醒只写异常类型、请求方法和路径，不带查询参数、请求内容和异常详情（可能含用户输入）。
    记录和提醒本身出错也不影响返回固定的 500。"""
    try:
        where = f"{request.method} {_printable(request.url.path, 200)}"
        error_log.error("接口出错：%s（%s）", where, type(exc).__name__)
        _note_server_error(f"{type(exc).__name__}，{where}")
    except Exception:  # noqa: BLE001 - 提醒失败不能让用户拿不到 500
        # 连日志都写不了时也照样返回 500
        with contextlib.suppress(Exception):
            error_log.error("接口出错提醒失败")
    return JSONResponse(status_code=500, content={"detail": "服务出错了，请稍后再试"})


@app.middleware("http")
async def _role_guard(request: Request, call_next):
    """按部署角色开放路径（白名单）：api 服务只开放小程序接口（/api/v1/）和健康检查，
    admin 服务只开放核对台、管理接口和健康检查；其余一律 404
    （早期样机的 /api/documents 等不对外）。"""
    role = getattr(get_settings(), "role", "all")
    path = request.url.path
    admin_path = path == "/admin" or path.startswith(("/admin/", "/api/admin/"))
    mini_path = path.startswith("/api/v1/")
    blocked = (role == "api" and not (mini_path or path == "/health")) or (
        role == "admin" and not (admin_path or path == "/health")
    )
    if blocked:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


def _openid_or_none(request: Request) -> str | None:
    from youju.api.me import request_openid

    try:
        return request_openid(request)
    except HTTPException:
        return None


@app.middleware("http")
async def _access_log(request: Request, call_next):
    """网络日志（见 youju/access_log.py）：每次请求记一条，健康检查除外；放在最外层，
    被拦下的请求（404、401、429）和出错的请求也记。只记路径，不记查询参数和请求内容。
    响应发完后再写库，不拖慢响应；出错的请求（没有响应可挂）当场写完再交给出错处理。"""
    path = request.url.path
    if path == "/health":
        return await call_next(request)
    started = clock.monotonic()
    entry = {
        "service": getattr(get_settings(), "role", "all"),
        "method": request.method[:8],
        "path": _printable(path, 190),
        "ip": client_ip(request)[:64] or None,
        "openid": _openid_or_none(request),
    }
    try:
        response = await call_next(request)
    except Exception:
        entry.update(status=500, ms=int((clock.monotonic() - started) * 1000))
        entry["detail"] = getattr(request.state, "audit", None)
        await run_in_threadpool(access_log.write, entry)
        raise
    entry.update(status=response.status_code, ms=int((clock.monotonic() - started) * 1000))
    entry["detail"] = getattr(request.state, "audit", None)
    # call_next 返回的响应本身不带后台任务（接口自己的后台任务在里层已经跑过）
    response.background = BackgroundTask(access_log.write, entry)
    return response


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
    # rule：规则抽出；ai：规则没找到、AI 找到的。ai_agrees：规则候选里 AI 是否也找到了同一天
    origin: str = "rule"
    ai_agrees: bool | None = None
    # 质检退回的：当时的存疑原因（重新核对时要看）
    reopen_note: str | None = None


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
    # 纠错分诊：规则和 AI 复查对应文件已发布截止日期的结果（还没分诊时为空）
    triageNote: str | None = None
    triagedAt: datetime | None = None


class UnchangeIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)


_HHMM = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def parse_hhmm(value: str) -> time:
    """只收 HH:MM（00:00–23:59），带秒、时区等写法一律不收：存的、导出的和导入比对的保持一致。"""
    if not _HHMM.fullmatch(value):
        raise ValueError("时间格式应为 HH:MM")
    return time.fromisoformat(value)


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
        status=list_status(doc.title, effective_kind(doc.title, doc.kind_override)),
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
        .order_by(Document.publish_date.desc(), ListEntry.id)
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
                .order_by(Document.publish_date.desc(), ListEntry.id)
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
        stmt.order_by(Document.publish_date.desc(), Document.id.desc()).limit(limit).offset(offset)
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
    """核对台操作留痕（操作人、来源、时间）：打到日志，同时记进访问日志，按网络日志留存要求保存。"""
    record = {"event": "admin", "action": action, "ip": client_ip(request), **fields}
    audit_log.info(json.dumps(record, ensure_ascii=False, default=str))
    # 同时记进这次请求的访问日志（保存 190 天）；来源 IP 访问日志里已有
    request.state.audit = json.loads(
        json.dumps({"action": action, **fields}, ensure_ascii=False, default=str)
    )


AdminDep = Annotated[None, Depends(_require_admin)]


DueFilter = Literal["all", "30", "future"]
DueQuery = Annotated[
    DueFilter,
    Query(description="只看哪些：all 全部；30 未过期且 30 天内截止；future 未过期"),
]


def _due_condition(due: DueFilter):
    """按候选截止日期筛选（北京时间的今天算未过期）。同一通知的相同候选日期一样，不影响只列一条的规则。"""
    if due == "all":
        return None
    today = v1_api.now().date()
    if due == "30":
        return DocumentDeadline.candidate_date.between(today, today + timedelta(days=30))
    return DocumentDeadline.candidate_date >= today


@app.get("/api/admin/deadlines/count")
def pending_count(session: SessionDep, _admin: AdminDep, due: DueQuery = "all") -> dict[str, int]:
    """待核对条数，与列表同一规则：归并组里相同的候选只算一条。"""
    stmt = select(DocumentDeadline).where(DocumentDeadline.status == "pending")
    condition = _due_condition(due)
    if condition is not None:
        stmt = stmt.where(condition)
    pending = session.scalars(stmt).all()
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


@app.get("/admin/fields", response_class=HTMLResponse, include_in_schema=False)
def fields_console() -> str:
    return (Path(__file__).resolve().parent.parent / "admin" / "fields.html").read_text(
        encoding="utf-8"
    )


class FieldReviewIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)
    value: str | None = Field(default=None, max_length=300)
    approved_value: str | None = Field(default=None, max_length=300)


class FieldCandidateOut(BaseModel):
    id: int
    field: Literal["support", "audience"]
    value: str
    evidence: str
    evidence_offset: int
    source: Literal["rule", "ai", "both"]
    status: Literal["pending", "approved", "rejected", "stale"]
    approved_value: str | None


class FieldReviewOut(BaseModel):
    document_id: int
    title: str
    url: str
    publish_date: date | None
    deadline: date | None
    fields: list[FieldCandidateOut]


def _field_queue(session: Session) -> list[FieldReviewOut]:
    """待核队列：已核截止且未来 30 天优先，其次未来 30 天但截止未核；过期不列。"""
    today = v1_api.now().date()
    rows = session.execute(
        select(DocumentField, Document, Source)
        .join(Document, Document.id == DocumentField.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentField.status.in_(("pending", "stale")), v1_api._kind_is(["申报通知"]))
        .where(or_(DocumentField.field != "audience", field_rules.AUDIENCE_ENABLED))
    ).all()
    if not rows:
        return []
    doc_ids = list({doc.id for _, doc, _ in rows})
    approved = v1_api._approved(session, doc_ids)
    masters = canonical_ids(session, doc_ids)
    pending_dates = dict(
        session.execute(
            select(DocumentDeadline.document_id, func.min(DocumentDeadline.candidate_date))
            .where(
                DocumentDeadline.document_id.in_(doc_ids),
                DocumentDeadline.status == "pending",
                DocumentDeadline.candidate_date >= today,
                DocumentDeadline.candidate_date <= today + timedelta(days=30),
            )
            .group_by(DocumentDeadline.document_id)
        ).all()
    )
    grouped: dict[int, list[DocumentField]] = {}
    docs: dict[int, tuple[Document, Source]] = {}
    for field, doc, src in rows:
        grouped.setdefault(doc.id, []).append(field)
        docs[doc.id] = (doc, src)
    cards: list[tuple[int, date, FieldReviewOut]] = []
    for document_id, fields in grouped.items():
        deadlines = approved.get(masters.get(document_id, document_id), [])
        selected = v1_api._selected_deadline(deadlines)
        due = selected.approved_date if selected else None
        pending_unverified = pending_dates.get(document_id)
        verified_due = due if due and today <= due <= today + timedelta(days=30) else None
        queue_due = verified_due or pending_unverified
        if queue_due is None:
            continue
        doc, _ = docs[document_id]
        cards.append(
            (
                0 if verified_due else 1,
                queue_due,
                FieldReviewOut(
                    document_id=document_id,
                    title=doc.title,
                    url=doc.url,
                    publish_date=doc.publish_date,
                    deadline=queue_due,
                    fields=[
                        FieldCandidateOut(
                            id=item.id,
                            field=item.field,
                            value=item.value,
                            evidence=item.evidence,
                            evidence_offset=item.evidence_offset,
                            source=item.source,
                            status=item.status,
                            approved_value=item.approved_value,
                        )
                        for item in sorted(fields, key=lambda item: item.id)
                    ],
                ),
            )
        )
    cards.sort(key=lambda item: (item[0], item[1], item[2].document_id))
    return [item[2] for item in cards]


@app.get("/api/admin/fields/count")
def field_pending_count(session: SessionDep, _admin: AdminDep) -> dict[str, int]:
    return {"pending": sum(len(item.fields) for item in _field_queue(session))}


@app.get("/api/admin/fields", response_model=list[FieldReviewOut])
def pending_fields(
    session: SessionDep,
    _admin: AdminDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[FieldReviewOut]:
    items = _field_queue(session)
    if before_id is not None:
        items = [item for item in items if item.document_id < before_id]
    return items[:limit]


def _review_field(
    field_id: int, body: FieldReviewIn, session: Session, status: str
) -> DocumentField:
    item = session.get(DocumentField, field_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到字段候选")
    if item.status not in ("pending", "stale"):
        raise HTTPException(status_code=409, detail="字段候选已核对")
    if status == "approved":
        proposed = body.value if body.value is not None else body.approved_value
        value = item.value if proposed is None else proposed.strip()
        if not value or len(value) > 300:
            raise HTTPException(status_code=422, detail="通过后的值不能为空且不超过 300 字")
        if item.field == "support" and value not in SUPPORT_LABELS:
            raise HTTPException(status_code=422, detail="支持方式必须是固定标签")
        item.approved_value = value
    item.status = status
    item.reviewed_by = body.reviewed_by
    item.reviewed_at = datetime.now(UTC)
    session.commit()
    return item


@app.post("/api/admin/fields/{field_id}/approve")
def approve_field(
    field_id: int, body: FieldReviewIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    item = session.get(DocumentField, field_id)
    before = item.value if item else None
    result = _review_field(field_id, body, session, "approved")
    _audit(
        request,
        "field_approve",
        field_id=field_id,
        document_id=result.document_id,
        reviewer=body.reviewed_by,
        before=before,
        after=result.approved_value,
    )
    return {"status": result.status}


@app.post("/api/admin/fields/{field_id}/reject")
def reject_field(
    field_id: int, body: FieldReviewIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    result = _review_field(field_id, body, session, "rejected")
    _audit(
        request,
        "field_reject",
        field_id=field_id,
        document_id=result.document_id,
        reviewer=body.reviewed_by,
    )
    return {"status": result.status}


@app.get("/admin/feedback", response_class=HTMLResponse, include_in_schema=False)
def feedback_console() -> str:
    """纠错和申请覆盖核对台；口令仅由页面请求管理接口时发送。"""
    return (Path(__file__).resolve().parent.parent / "admin" / "feedback.html").read_text(
        encoding="utf-8"
    )


KINDS = ("申报通知", "名单", "政策", "其他")


class KindPendingOut(BaseModel):
    """AI 和标题规则判断不一致、等人定类型的文件。"""

    id: int
    title: str
    url: str
    publish_date: date | None
    source: str
    rule_kind: str
    ai_kind: str
    ai_reason: str
    ai_confidence: float
    # 人工已定的类型（只在"全部"里出现）
    decided: str | None
    decided_by: str | None


class KindDecisionIn(BaseModel):
    kind: Literal["申报通知", "名单", "政策", "其他"]
    decided_by: str = Field(min_length=1, max_length=80)


def _kind_disagreements(session: Session, include_decided: bool) -> list[KindPendingOut]:
    """AI 判断成功、且和标题规则不一致的文件（同一通知的其他网址不重复列），新的在前。"""
    rows = session.execute(
        visible_documents(
            select(Document, Source, AICheck)
            .join(Source, Source.id == Document.source_id)
            .join(
                AICheck,
                and_(
                    AICheck.document_id == Document.id,
                    AICheck.task == "kind",
                    AICheck.error.is_(None),
                ),
            )
        ).order_by(Document.id.desc())
    ).all()
    out: list[KindPendingOut] = []
    for doc, src, check in rows:
        result = check.result if isinstance(check.result, dict) else {}
        ai_kind = result.get("kind")
        rule_kind = doc_kind(doc.title)
        if ai_kind not in KINDS or ai_kind == rule_kind:
            continue
        if doc.kind_override is not None and not include_decided:
            continue
        out.append(
            KindPendingOut(
                id=doc.id,
                title=doc.title,
                url=doc.url,
                publish_date=doc.publish_date,
                source=src.dept_full_name,
                rule_kind=rule_kind,
                ai_kind=ai_kind,
                ai_reason=str(result.get("reason", "")),
                ai_confidence=float(result.get("confidence") or 0),
                decided=doc.kind_override,
                decided_by=doc.kind_decided_by,
            )
        )
    return out


@app.get("/api/admin/kinds/count")
def kinds_pending_count(session: SessionDep, _admin: AdminDep) -> dict[str, int]:
    return {"pending": len(_kind_disagreements(session, include_decided=False))}


@app.get("/api/admin/kinds", response_model=list[KindPendingOut])
def admin_kinds(
    session: SessionDep,
    _admin: AdminDep,
    status: Literal["pending", "all"] = "pending",
    before_id: Annotated[int | None, Query(ge=1, description="翻页游标：只取编号更小的")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[KindPendingOut]:
    items = _kind_disagreements(session, include_decided=status == "all")
    if before_id is not None:
        items = [item for item in items if item.id < before_id]
    return items[:limit]


@app.post("/api/admin/documents/{document_id}/kind")
def decide_kind(
    document_id: int,
    body: KindDecisionIn,
    session: SessionDep,
    _admin: AdminDep,
    request: Request,
) -> dict[str, str]:
    """人工定类型：给用户看的类型、筛选和名单解析都按这个。"""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    doc.kind_override = body.kind
    doc.kind_decided_by = body.decided_by
    doc.kind_decided_at = datetime.now(UTC)
    session.commit()
    _audit(request, "kind_decide", document_id=document_id, kind=body.kind, by=body.decided_by)
    return {"kind": body.kind}


@app.post("/api/admin/documents/{document_id}/kind/undo")
def undo_kind(
    document_id: int, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, str]:
    """撤回人工定的类型，回到按标题规则判断。"""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    doc.kind_override = None
    doc.kind_decided_by = None
    doc.kind_decided_at = None
    session.commit()
    _audit(request, "kind_undo", document_id=document_id)
    return {"kind": doc_kind(doc.title)}


@app.get("/admin/kinds", response_class=HTMLResponse, include_in_schema=False)
def kinds_console() -> str:
    """类型待定：AI 和标题规则判断不一致的文件由人定类型；口令仅由页面请求管理接口时发送。"""
    return (Path(__file__).resolve().parent.parent / "admin" / "kinds.html").read_text(
        encoding="utf-8"
    )


class AuditFlagOut(BaseModel):
    """质检存疑：已发布给用户的截止，被规则或 AI 标为可能有误，等人复核。"""

    id: int
    document_id: int
    title: str
    url: str
    source: str
    publish_date: date | None
    kind: str
    approved_date: date
    approved_time: str | None
    evidence: str
    note: str
    flagged_at: datetime | None


class AuditActionIn(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=80)


def _audit_flagged(session: Session):
    return (
        select(DocumentDeadline, Document, Source)
        .join(Document, Document.id == DocumentDeadline.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentDeadline.status == "approved", DocumentDeadline.audit_state == "flagged")
    )


@app.get("/api/admin/audit/count")
def audit_flagged_count(session: SessionDep, _admin: AdminDep) -> dict[str, int]:
    stmt = select(func.count()).select_from(_audit_flagged(session).subquery())
    return {"flagged": session.scalar(stmt) or 0}


@app.get("/api/admin/audit", response_model=list[AuditFlagOut])
def audit_flagged(
    session: SessionDep,
    _admin: AdminDep,
    before_id: Annotated[int | None, Query(ge=1, description="翻页游标：只取编号更小的")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[AuditFlagOut]:
    stmt = _audit_flagged(session)
    if before_id is not None:
        stmt = stmt.where(DocumentDeadline.id < before_id)
    rows = session.execute(stmt.order_by(DocumentDeadline.id.desc()).limit(limit)).all()
    return [
        AuditFlagOut(
            id=d.id,
            document_id=doc.id,
            title=doc.title,
            url=doc.url,
            source=src.dept_full_name,
            publish_date=doc.publish_date,
            kind=d.kind,
            approved_date=d.approved_date,
            approved_time=d.approved_time.strftime("%H:%M") if d.approved_time else None,
            evidence=d.evidence,
            note=d.audit_note or "",
            flagged_at=d.audit_at,
        )
        for d, doc, src in rows
    ]


@app.post("/api/admin/deadlines/{deadline_id}/audit-ok")
def audit_ok(
    deadline_id: int,
    body: AuditActionIn,
    session: SessionDep,
    _admin: AdminDep,
    request: Request,
) -> dict[str, str]:
    """人复核后确认无误：不再标存疑（存疑原因保留备查）。"""
    row = session.get(DocumentDeadline, deadline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到候选")
    if row.audit_state != "flagged":
        raise HTTPException(status_code=409, detail="这条不在质检存疑里")
    row.audit_state = "cleared"
    row.audit_by = body.reviewed_by
    row.audit_at = datetime.now(UTC)
    session.commit()
    _audit(request, "audit_ok", deadline_id=deadline_id, reviewer=body.reviewed_by)
    return {"audit": "cleared"}


@app.post("/api/admin/deadlines/{deadline_id}/reopen")
def reopen_deadline(
    deadline_id: int,
    body: AuditActionIn,
    session: SessionDep,
    _admin: AdminDep,
    request: Request,
) -> dict[str, str]:
    """退回待核对：用户端立即不再显示这个截止，候选回到核对台、标明是质检退回，必须由人重新核对。
    归并组里同一截止（类型、核定日期和时间相同）已通过的副本一起退回：用户端显示的是同一个截止，
    只退一条的话其他副本照样显示。已标变更的不能退回。"""
    row = session.get(DocumentDeadline, deadline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到候选")
    group = _lock_group(session, row)
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="只有已通过的截止能退回待核对")
    note = row.audit_note if row.audit_state == "flagged" and row.audit_note else "人工退回"
    now = datetime.now(UTC)
    reopened = 0
    for item in group:
        if item.status != "approved":
            continue
        item.status = "pending"
        item.reviewed_by = None
        item.reviewed_at = None
        item.approved_date = None
        item.approved_time = None
        item.audit_state = "reopened"
        item.audit_note = note
        item.audit_at = now
        item.audit_by = body.reviewed_by
        reopened += 1
    session.commit()
    _audit(request, "reopen", deadline_id=deadline_id, reviewer=body.reviewed_by, count=reopened)
    return {"status": "pending"}


@app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
def audit_console() -> str:
    """质检存疑：已发布的截止被规则或 AI 标为可能有误，人复核；口令仅由页面请求管理接口时发送。"""
    return (Path(__file__).resolve().parent.parent / "admin" / "audit.html").read_text(
        encoding="utf-8"
    )


# ---------- 来源接入（D-023 来源接入 Agent）：试跑 → 人确认 → 接入 ----------

# 同一网站两次试跑至少间隔这么久（秒）：试跑由人点按钮触发，也不能连续打扰对方网站。
# admin 服务固定 1 个实例，进程内记录即可；试跑中的重定向每一跳由抓取器按 3 秒限速
_TRIAL_INTERVAL = 10.0
_trial_hosts: dict[str, float] = {}
_trial_guard = threading.Lock()
# 试跑凭证有效期（秒）：接入时必须带上试跑给出的凭证，证明这个网址和规则确实试跑通过
_TRIAL_TTL = 1800
# 接入来源时等别人接入完成最多等多久（秒）
_CREATE_LOCK_WAIT = 10


class SourceTrialIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(min_length=8, max_length=URL_KEY_MAX)
    # 通用规则识别出来了，人仍想看看 AI 写的抓取规则
    ai_rule: bool = False


class TrialItemOut(BaseModel):
    title: str
    url: str
    publish_date: date | None


class SourceTrialOut(BaseModel):
    url: str
    final_url: str
    status: str
    error: str | None
    ai_enabled: bool
    generic_count: int
    generic: list[TrialItemOut]
    ai_config: dict[str, str] | None
    ai_count: int
    ai_items: list[TrialItemOut]
    ai_note: str | None
    proposal: dict[str, str]
    # 试跑通过的规则各给一张凭证（识别出至少 3 条带日期的条目才给），接入时带上
    generic_token: str | None = None
    ai_token: str | None = None


class SourceCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(min_length=8, max_length=URL_KEY_MAX)
    level: Literal["国家级", "省级", "市级"]
    province: str
    dept: str = Field(min_length=1, max_length=32)
    dept_full_name: str = Field(min_length=2, max_length=128)
    column_name: str = Field(min_length=2, max_length=64)
    # 空：用通用规则；否则是试跑时 AI 写、人确认过的 CSS 选择器
    parser_config: dict[str, str] = Field(default_factory=dict)
    trial_token: str = Field(min_length=10, max_length=200)
    reviewed_by: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _consistent(self) -> SourceCreateIn:
        from youju.sources.onboard import PROVINCES, valid_config

        if self.province not in PROVINCES:
            raise ValueError('省份要从列表里选（国家部委选"国家"）')
        if (self.level == "国家级") != (self.province == "国家"):
            raise ValueError('国家级来源的省份填"国家"，其他层级要选具体省份')
        if self.parser_config and valid_config(self.parser_config) is None:
            raise ValueError("抓取规则只能是合法的 CSS 选择器（item_selector 必填）")
        return self


def _source_url(url: str) -> str:
    """校验并规范化栏目网址（去掉 #片段、协议和域名小写）；格式不对或不是政府网站返回 422。"""
    from youju.crawler.detail_service import _government_url
    from youju.crawler.urls import normalize_url

    raw = url.strip()
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018  端口不合法时这里报错
        normalized = normalize_url(raw, raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="网址格式不对") from exc
    if (
        not normalized
        or parts.scheme.lower() not in ("http", "https")
        or not _government_url(normalized)
    ):
        raise HTTPException(status_code=422, detail="只接政府网站（域名以 .gov.cn 结尾）的栏目网址")
    return normalized


def _existing_source(session: Session, url: str) -> int | None:
    """已有来源里同一栏目的编号：按抓取去重用的规范键比，
    index.html 和目录、http 和 https 算同一个。"""
    from youju.crawler.urls import url_key

    key = url_key(url)
    for source_id, source_url in session.execute(select(Source.id, Source.url)):
        try:
            if url_key(source_url) == key:
                return source_id
        except ValueError:
            continue
    return None


@contextlib.contextmanager
def _source_create_lock(session: Session):
    """接入来源时查重和写入串行做（MySQL 命名锁，另开一个连接持有）：同一栏目的两种写法
    （index.html 和目录）同时接入时，后一个等前一个提交后再查重。"""
    with session.get_bind().connect() as conn:
        got = conn.execute(
            text("SELECT GET_LOCK('youju_source_create', :wait)"), {"wait": _CREATE_LOCK_WAIT}
        ).scalar()
        if got != 1:
            raise HTTPException(status_code=503, detail="有人正在接入来源，请稍后再试")
        try:
            yield
        finally:
            conn.execute(text("SELECT RELEASE_LOCK('youju_source_create')"))


def _trial_mac(url: str, config: dict[str, str], issued: int) -> str:
    secret = hmac.new(
        (get_settings().admin_token or "").encode(), b"youju-source-trial", hashlib.sha256
    ).digest()
    payload = json.dumps(
        {"url": url, "config": config, "issued": issued},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def _trial_token(url: str, config: dict[str, str]) -> str:
    issued = int(clock.time())
    return f"{issued}.{_trial_mac(url, config, issued)}"


def _trial_passed(token: str, url: str, config: dict[str, str]) -> bool:
    """凭证是试跑给这个网址、这套规则的，且没过期。"""
    issued_text, _, mac = token.partition(".")
    try:
        issued = int(issued_text)
    except ValueError:
        return False
    if not 0 <= clock.time() - issued <= _TRIAL_TTL:
        return False
    return compare_digest(mac, _trial_mac(url, config, issued))


def _trial_fetcher():
    from youju.sources.onboard import trial_fetcher

    return trial_fetcher()


def _items_out(items) -> list[TrialItemOut]:
    return [TrialItemOut(title=i.title, url=i.url, publish_date=i.publish_date) for i in items]


@app.post("/api/admin/sources/trial", response_model=SourceTrialOut)
def source_trial(
    body: SourceTrialIn, session: SessionDep, _admin: AdminDep, request: Request
) -> SourceTrialOut:
    """来源接入第一步：试跑一个栏目（抓一次列表页、识别条目、给出登记建议），不写数据库。"""
    from youju.ai import get_chat_client
    from youju.sources.onboard import trial

    url = _source_url(body.url)
    existing = _existing_source(session, url)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"这个栏目已经在来源里（编号 {existing}）")
    host = urlsplit(url).hostname or ""
    now = clock.monotonic()
    with _trial_guard:
        if len(_trial_hosts) > 1000:
            _trial_hosts.clear()
        last = _trial_hosts.get(host)
        if last is not None and now - last < _TRIAL_INTERVAL:
            raise HTTPException(status_code=429, detail="同一网站 10 秒内只试跑一次，请稍后再试")
        _trial_hosts[host] = now
    session.commit()  # 抓取可能要几十秒，期间不占着数据库事务
    client = get_chat_client()
    try:
        with _trial_fetcher() as fetcher:
            result = trial(url, fetcher, client, want_ai_rule=body.ai_rule)
    finally:
        if client is not None:
            try:
                if client.totals["calls"]:
                    from youju.ai.usage import record_usage

                    record_usage(session, client, "onboard")
            except Exception:
                session.rollback()
                audit_log.exception("AI 用量记账失败（onboard）")
            finally:
                client.close()
    _audit(
        request,
        "source_trial",
        url=url,
        status=result.status,
        generic=len(result.generic),
        ai=len(result.ai_items),
    )
    proposal = result.proposal
    ok = result.status == "ok"
    return SourceTrialOut(
        url=url,
        final_url=result.final_url,
        status=result.status,
        error=result.error,
        ai_enabled=client is not None,
        generic_count=len(result.generic),
        generic=_items_out(result.generic[:10]),
        ai_config=result.ai_config,
        ai_count=len(result.ai_items),
        ai_items=_items_out(result.ai_items[:10]),
        ai_note=result.ai_note,
        proposal={
            "level": proposal.level,
            "province": proposal.province,
            "dept": proposal.dept,
            "dept_full_name": proposal.dept_full_name,
            "column_name": proposal.column_name,
        },
        generic_token=_trial_token(url, {}) if ok and len(result.generic) >= MIN_ITEMS else None,
        ai_token=_trial_token(url, result.ai_config) if ok and result.ai_config else None,
    )


@app.post("/api/admin/sources", status_code=201)
def create_source(
    body: SourceCreateIn, session: SessionDep, _admin: AdminDep, request: Request
) -> dict[str, int]:
    """来源接入第二步：人看过试跑结果、核对过登记信息后接入。下一轮定时抓取开始检查，
    第一次抓到的条目算基线，不当作新文件提醒用户。"""
    from youju.sources.onboard import valid_config

    url = _source_url(body.url)
    config = valid_config(body.parser_config) if body.parser_config else {}
    if not _trial_passed(body.trial_token, url, config):
        raise HTTPException(
            status_code=422,
            detail="请先试跑这个网址，用试跑通过的规则接入（试跑结果 30 分钟内有效）",
        )
    with _source_create_lock(session):
        session.rollback()  # 拿到锁后重新开事务，读到别人刚提交的来源
        if _existing_source(session, url) is not None:
            raise HTTPException(status_code=409, detail="这个栏目已经在来源里")
        source_id = _insert_source(session, body, url, config)
    _audit(
        request,
        "source_create",
        source_id=source_id,
        url=url,
        reviewer=body.reviewed_by,
        parser_config=config,
    )
    return {"id": source_id}


def _insert_source(session: Session, body: SourceCreateIn, url: str, config: dict) -> int:
    source = Source(
        url=url,
        level=body.level,
        province=body.province,
        dept=body.dept.strip(),
        dept_full_name=body.dept_full_name.strip(),
        column_name=body.column_name.strip(),
        fetch_mode="static",
        enabled=True,
        parser_config=config,
        rights_level=1,
        verification_status=f"已验证（后台接入，{body.reviewed_by}）"[:64],
        verified_on=v1_api.now().date(),
        note="来源接入：试跑后由人确认接入" + ("，用 AI 写的抓取规则" if config else ""),
    )
    session.add(source)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="这个栏目已经在来源里") from exc
    return source.id


@app.get("/admin/sources", response_class=HTMLResponse, include_in_schema=False)
def sources_console() -> str:
    """来源接入：输入栏目网址试跑，看识别结果、核对登记信息后接入；口令仅由页面请求管理接口时发送。"""
    return (Path(__file__).resolve().parent.parent / "admin" / "sources.html").read_text(
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
                triageNote=row.triage_note,
                triagedAt=row.triaged_at,
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
    due: DueQuery = "all",
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
    condition = _due_condition(due)
    if condition is not None:
        base = base.where(condition)
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
            origin=d.origin,
            ai_agrees=d.ai_agrees,
            reopen_note=d.audit_note if d.audit_state == "reopened" else None,
        )
        for d, doc, src in rows
    ]


def _lock_candidates(session: Session, row: DocumentDeadline) -> list[DocumentDeadline]:
    """按编号顺序一次锁住这条候选和归并组里同一候选的副本（类型、日期、时间、证据句都相同），
    并刷新成最新状态；并发核对时不会互相等待成死锁，调用方锁住后再判断状态。"""
    master = canonical_ids(session, [row.document_id]).get(row.document_id, row.document_id)
    members = groups_for(session, [master])[master]
    return list(
        session.scalars(
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
    )


def _apply_review(
    row: DocumentDeadline,
    locked: list[DocumentDeadline],
    status: str,
    reviewed_by: str,
    reviewed_at: datetime,
    approved_date: date | None = None,
    approved_time: time | None = None,
) -> None:
    """把核对结果写到这条候选和归并组里还没核对的同一候选副本上（免得重复核对）；不提交。
    重新核对过了，质检退回的标记一并清掉：通过的以后照常质检。"""
    twins = [d for d in locked if d.id != row.id and d.status == "pending"]
    for item in {row, *twins}:
        item.status = status
        item.reviewed_by = reviewed_by
        item.reviewed_at = reviewed_at
        item.audit_state = None
        item.audit_note = None
        item.audit_at = None
        item.audit_by = None
        if status == "approved":
            item.approved_date = approved_date
            item.approved_time = approved_time


def _review(deadline_id: int, body: ReviewIn, session: Session, status: str) -> dict[str, str]:
    row = session.get(DocumentDeadline, deadline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到候选")
    locked = _lock_candidates(session, row)
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="候选已核对")
    if status == "rejected" and (body.new_date is not None or body.new_time is not None):
        raise HTTPException(status_code=422, detail="驳回时不能修改日期")
    # 区分"没传时间"（沿用候选时间）和"明确清空时间"（time 传 null：原文只写到日期）
    if body.new_time is not None:
        try:
            parsed_time = parse_hhmm(body.new_time)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="时间格式应为 HH:MM") from exc
    elif "new_time" in body.model_fields_set:
        parsed_time = None
    else:
        parsed_time = row.candidate_time
    _apply_review(
        row,
        locked,
        status,
        body.reviewed_by,
        datetime.now(UTC),
        body.new_date or row.candidate_date,
        parsed_time,
    )
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
    ordered = stmt.order_by(Document.publish_date.desc(), DocumentDeadline.id.desc())
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
    _check_change_target(session, row, body.new_document_id)
    _apply_change(group, body.reason, body.new_document_id, body.reviewed_by, datetime.now(UTC))
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


def _check_change_target(session: Session, row: DocumentDeadline, new_document_id: int) -> None:
    """变更通知要是另一份文件、与原通知同一地区、发布不早于原通知（防止关联到无关文件）。"""
    same = canonical_ids(session, [new_document_id, row.document_id])
    if same.get(new_document_id, new_document_id) == same.get(row.document_id, row.document_id):
        raise HTTPException(status_code=422, detail="新通知不能是原通知本身")
    new_doc = session.get(Document, new_document_id)
    if new_doc is None:
        raise HTTPException(status_code=404, detail="没有找到新通知")
    old_doc = session.get(Document, row.document_id)
    if new_doc.source.province != old_doc.source.province:
        raise HTTPException(status_code=422, detail="新通知与原通知不是同一地区，请核对")
    if new_doc.publish_date is None or old_doc.publish_date is None:
        raise HTTPException(status_code=422, detail="发布日期不详，无法核对先后，请先到原文核对")
    if new_doc.publish_date < old_doc.publish_date:
        raise HTTPException(status_code=422, detail="新通知的发布日期早于原通知，请核对")


def _apply_change(
    group: list[DocumentDeadline],
    reason: str,
    new_document_id: int,
    changed_by: str,
    changed_at: datetime,
) -> None:
    """归并组里相同的已通过副本（同一通知的其他网址）一并标为已变更，否则副本仍会被当作有效截止显示；
    不提交。"""
    for item in [d for d in group if d.status == "approved"]:
        item.status = "changed"
        item.change_reason = reason
        item.changed_to_document_id = new_document_id
        item.changed_by = changed_by
        item.changed_at = changed_at


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


class ReviewChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["延期", "调整", "撤销"]
    toUrlKey: str = Field(min_length=1, max_length=2000)
    changedBy: str = Field(min_length=1, max_length=80)
    changedAt: datetime


class ReviewTransferItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urlKey: str = Field(min_length=1, max_length=2000)
    fingerprint: str = Field(min_length=64, max_length=64)
    title: str | None = Field(default=None, max_length=500)  # 只给人看，导入时不用
    kind: str = Field(max_length=32)
    candidateDate: date
    candidateTime: str | None = None
    evidence: str = Field(max_length=500)
    status: Literal["approved", "rejected", "changed"]
    approvedDate: date | None = None
    approvedTime: str | None = None
    reviewedBy: str = Field(min_length=1, max_length=80)
    reviewedAt: datetime
    change: ReviewChangeItem | None = None

    @field_validator("candidateTime", "approvedTime")
    @classmethod
    def _hhmm_time(cls, value: str | None) -> str | None:
        if value is not None:
            parse_hhmm(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ReviewTransferItem:
        if self.status == "rejected" and (self.approvedDate or self.change):
            raise ValueError("驳回的记录不能带核定日期或变更")
        if self.status != "rejected" and self.approvedDate is None:
            raise ValueError("通过或已变更的记录要有核定日期")
        if (self.status == "changed") != (self.change is not None):
            raise ValueError("只有已变更的记录带变更信息")
        return self


class ReviewTransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    exportedAt: datetime | None = None
    items: list[ReviewTransferItem] = Field(max_length=5000)


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


@app.get("/api/admin/reviews/export")
def export_reviews(session: SessionDep, _admin: AdminDep, request: Request) -> JSONResponse:
    """导出已核对的截止（通过、驳回、已变更），用"文件网址键 + 候选指纹"标识，
    可以导入到另一个库（如国庆期间在开发库核对，上线后导入正式库）。"""
    rows = session.execute(
        select(DocumentDeadline, Document.url_key, Document.title)
        .join(Document, DocumentDeadline.document_id == Document.id)
        .where(DocumentDeadline.status.in_(("approved", "rejected", "changed")))
        .order_by(DocumentDeadline.id)
    ).all()
    target_ids = {d.changed_to_document_id for d, _, _ in rows if d.changed_to_document_id}
    targets = dict(
        session.execute(
            select(Document.id, Document.url_key).where(Document.id.in_(target_ids))
        ).all()
    )
    items = [
        {
            "urlKey": url_key,
            "fingerprint": d.fingerprint,
            "title": title,
            "kind": d.kind,
            "candidateDate": d.candidate_date.isoformat(),
            "candidateTime": _hhmm(d.candidate_time),
            "evidence": d.evidence,
            "status": d.status,
            "approvedDate": d.approved_date.isoformat() if d.approved_date else None,
            "approvedTime": _hhmm(d.approved_time),
            "reviewedBy": d.reviewed_by or "未记录",
            "reviewedAt": (d.reviewed_at or d.created_at).isoformat(),
            "change": {
                "reason": d.change_reason,
                "toUrlKey": targets[d.changed_to_document_id],
                "changedBy": d.changed_by or "未记录",
                "changedAt": (d.changed_at or d.reviewed_at or d.created_at).isoformat(),
            }
            if d.status == "changed"
            else None,
        }
        for d, url_key, title in rows
    ]
    _audit(request, "reviews_export", count=len(items))
    return JSONResponse(
        {"version": 1, "exportedAt": datetime.now(UTC).isoformat(), "items": items},
        headers={"Content-Disposition": 'attachment; filename="youju-reviews.json"'},
    )


def _same_result(
    row: DocumentDeadline, item: ReviewTransferItem, target_id: int | None, at: time | None
) -> bool:
    if row.status != item.status:
        return False
    if item.status == "rejected":
        return True
    same_date = row.approved_date == item.approvedDate and row.approved_time == at
    if item.status == "approved":
        return same_date
    return (
        same_date
        and row.changed_to_document_id == target_id
        and row.change_reason == item.change.reason
    )


@app.post("/api/admin/reviews/import")
def import_reviews(
    body: ReviewTransferIn,
    session: SessionDep,
    _admin: AdminDep,
    request: Request,
    dry_run: bool = True,
) -> dict:
    """导入核对结果（默认只预览，dry_run=false 才写入）。

    按文件网址键和候选指纹找到这边的候选；还没核对的按导出的结果核对，沿用核对台的规则
    （归并组里同一候选的副本一并处理、变更通知的校验）。已核对且结果相同的跳过，结果不同的
    记为冲突、不覆盖。已变更的截止，这边还没有变更通知时整条先不导入：只导入"通过"会让
    已经变更的截止重新显示出来；等抓到变更通知后再导入一次即可。
    """
    counts = dict.fromkeys(
        ("applied", "already", "conflict", "missingDocument", "missingCandidate", "missingTarget"),
        0,
    )
    problems: list[dict] = []

    def problem(kind: str, item: ReviewTransferItem, reason: str) -> None:
        counts[kind] += 1
        if len(problems) < 50:
            problems.append({"title": item.title or item.urlKey, "reason": reason})

    for item in body.items:
        doc_id = session.scalar(select(Document.id).where(Document.url_key == item.urlKey))
        if doc_id is None:
            problem("missingDocument", item, "这边还没有这份文件")
            continue
        row = session.scalar(
            select(DocumentDeadline).where(
                DocumentDeadline.document_id == doc_id,
                DocumentDeadline.fingerprint == item.fingerprint,
            )
        )
        if row is None:
            # 指纹由证据句算出，重抓时空白、断句稍有不同就会变：同一文件里类型、日期、时间都相同
            # 且只有一条时按它匹配；有多条分不清时不猜
            candidate_time = time.fromisoformat(item.candidateTime) if item.candidateTime else None
            same = session.scalars(
                select(DocumentDeadline).where(
                    DocumentDeadline.document_id == doc_id,
                    DocumentDeadline.kind == item.kind,
                    DocumentDeadline.candidate_date == item.candidateDate,
                    DocumentDeadline.candidate_time.is_(None)
                    if candidate_time is None
                    else DocumentDeadline.candidate_time == candidate_time,
                )
            ).all()
            if len(same) != 1:
                problem(
                    "missingCandidate",
                    item,
                    "这边还没有这条截止候选（可能还没补详情或抽截止）"
                    if not same
                    else "这边有几条相同类型和日期的候选，分不清，请在核对台核对",
                )
                continue
            row = same[0]
        target_id = None
        if item.status == "changed":
            target_id = session.scalar(
                select(Document.id).where(Document.url_key == item.change.toUrlKey)
            )
            if target_id is None:
                session.refresh(row)
                problem(
                    "missingTarget",
                    item,
                    "这边已通过，但导出记录显示已变更、这边还没有变更通知——请在核对台核对是否仍然有效"
                    if row.status == "approved"
                    else "这边还没有发布变更的新通知，先不导入",
                )
                continue
        locked = _lock_candidates(session, row)
        at = time.fromisoformat(item.approvedTime) if item.approvedTime else None
        if row.status != "pending":
            if _same_result(row, item, target_id, at):
                counts["already"] += 1
            else:
                problem("conflict", item, "这边已核对，结果不同，未覆盖")
            continue
        if any(d.status == "pending" and d.audit_state == "reopened" for d in locked):
            problem(
                "conflict", item, "这边质检退回了这条截止，要在核对台重新核对，不按导入结果通过"
            )
            continue
        if item.status == "changed":
            try:
                _check_change_target(session, row, target_id)
            except HTTPException as exc:
                problem("conflict", item, str(exc.detail))
                continue
        final = "rejected" if item.status == "rejected" else "approved"
        _apply_review(row, locked, final, item.reviewedBy, item.reviewedAt, item.approvedDate, at)
        if item.status == "changed":
            # 先写进事务：锁组查询会从数据库刷新记录，没写进去的核定日期会被刷掉
            session.flush()
            _apply_change(
                _lock_group(session, row),
                item.change.reason,
                target_id,
                item.change.changedBy,
                item.change.changedAt,
            )
        counts["applied"] += 1
    if dry_run:
        session.rollback()
    else:
        session.commit()
    _audit(request, "reviews_import", dry_run=dry_run, **counts)
    return {"dryRun": dry_run, **counts, "problems": problems}


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
        stmt.order_by(Document.publish_date.desc(), Document.id.desc()).limit(limit)
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
