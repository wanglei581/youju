"""小程序用的公开接口（/api/v1）：首页、搜索筛选、文件详情、申报日历、最近名单。

返回格式与小程序 mock/data.js 一致，接口接上后界面不用改。截止日期只返回人工核对通过的（approved）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, func, not_, or_, select
from sqlalchemy.orm import Session

from youju.classify import doc_kind, kind_filter, list_status, region_label, status_filter
from youju.db import get_session
from youju.models import Document, DocumentDeadline, DocumentDetail, Source

router = APIRouter(prefix="/api/v1", tags=["小程序"])
SessionDep = Annotated[Session, Depends(get_session)]

_TZ = ZoneInfo("Asia/Shanghai")
_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 同一份文件有多个截止时，列表和日历优先显示企业申报截止
_DEADLINE_ORDER = {"企业申报截止": 0, "材料报送截止": 1, "推荐或审核截止": 2}
_TIME_DAYS = {"近 7 天": 7, "近 30 天": 30}
_TECH = ("科技",)
_INDUSTRY = ("工信", "经信", "工业和信息化")


def today() -> date:
    """业务日期按北京时间。"""
    return datetime.now(_TZ).date()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _approved(session: Session, doc_ids: list[int]) -> dict[int, list[DocumentDeadline]]:
    """每份文件核对通过的截止日期，按显示优先级排好。"""
    if not doc_ids:
        return {}
    rows = session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id.in_(doc_ids), DocumentDeadline.status == "approved"
        )
    ).all()
    out: dict[int, list[DocumentDeadline]] = {}
    for d in rows:
        out.setdefault(d.document_id, []).append(d)
    for items in out.values():
        items.sort(key=lambda d: (_DEADLINE_ORDER.get(d.kind, 9), d.approved_date or date.max))
    return out


def _details(session: Session, doc_ids: list[int]) -> dict[int, DocumentDetail]:
    if not doc_ids:
        return {}
    rows = session.scalars(select(DocumentDetail).where(DocumentDetail.document_id.in_(doc_ids)))
    return {d.document_id: d for d in rows}


def _hhmm(value: Any) -> str:
    return value.strftime("%H:%M") if value else ""


def _deadline_brief(d: DocumentDeadline | None) -> dict[str, Any] | None:
    if d is None or d.approved_date is None:
        return None
    return {"date": d.approved_date.isoformat(), "time": _hhmm(d.approved_time), "verified": True}


def _item(
    doc: Document, src: Source, detail: DocumentDetail | None, deadlines: list[DocumentDeadline]
) -> dict:
    """列表行（与小程序 doc-row 的数据格式一致）。"""
    return {
        "id": str(doc.id),
        "kind": doc_kind(doc.title),
        "status": list_status(doc.title),
        "region": region_label(src.province, src.level),
        "title": doc.title,
        "issuer": (detail.issuer if detail and detail.issuer else src.dept_full_name),
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "url": doc.url,
        "deadline": _deadline_brief(deadlines[0] if deadlines else None),
    }


def _items(session: Session, rows: list[tuple[Document, Source]]) -> list[dict]:
    ids = [doc.id for doc, _ in rows]
    details, approved = _details(session, ids), _approved(session, ids)
    return [_item(doc, src, details.get(doc.id), approved.get(doc.id, [])) for doc, src in rows]


def _base() -> Select:
    return select(Document, Source).join(Source, Document.source_id == Source.id)


def _ordered(stmt: Select) -> Select:
    return stmt.order_by(Document.publish_date.desc().nulls_last(), Document.id.desc())


@router.get("/home")
def home(session: SessionDep) -> dict:
    """首页：本周新增（按发布日期，周一起算）、分类计数、覆盖范围、最新 8 份。"""
    start = today() - timedelta(days=today().weekday())
    week = select(Document.title).where(Document.publish_date >= start)
    titles = [t for (t,) in session.execute(week).all()]
    kinds = [doc_kind(t) for t in titles]
    sources = (
        session.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True)))
        or 0
    )
    provinces = (
        session.scalar(
            select(func.count(func.distinct(Source.province))).where(
                Source.enabled.is_(True), Source.province != "国家"
            )
        )
        or 0
    )
    # 首页"最新"只放申报通知、名单、政策三类；统计数据发布等其他文件在搜索里能找到
    latest = session.execute(
        _ordered(_base().where(kind_filter(Document.title, ["申报通知", "名单", "政策"]))).limit(8)
    ).all()
    return {
        "stats": {
            "week_total": len(titles),
            "apply": kinds.count("申报通知"),
            "lists": kinds.count("名单"),
            "other": len(titles) - kinds.count("申报通知") - kinds.count("名单"),
            "provinces": provinces,
            "sources": sources,
        },
        "items": _items(session, [tuple(r) for r in latest]),
    }


def _dept_filter(groups: list[str]):
    tech = or_(*(Source.dept.contains(w) for w in _TECH))
    industry = or_(*(Source.dept.contains(w) for w in _INDUSTRY))
    parts = {"科技部门": tech, "工信部门": industry, "其他部门": and_(not_(tech), not_(industry))}
    return or_(*(parts[g] for g in groups if g in parts))


@router.get("/search")
def search(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100, description="标题或文号关键词")] = None,
    kind: Annotated[str | None, Query(description="页签：申报通知 / 名单 / 政策")] = None,
    regions: Annotated[
        str | None, Query(description="省份，逗号分隔；国家部委写'国家部委'")
    ] = None,
    types: Annotated[str | None, Query(description="申报通知,名单与公示,政策文件,其他")] = None,
    status: Annotated[str | None, Query(description="公示,正式公布")] = None,
    time: Annotated[str, Query(description="近 7 天 / 近 30 天 / 今年 / 不限")] = "近 30 天",
    depts: Annotated[str | None, Query(description="科技部门,工信部门,其他部门")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """搜索与筛选。total 是符合全部条件的份数，total_all 是不限发布时间的份数。"""
    stmt = _base().outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
    kw = (q or "").strip()
    if kw:
        like = f"%{_escape_like(kw)}%"
        stmt = stmt.where(
            or_(
                Document.title.ilike(like, escape="\\"),
                DocumentDetail.doc_number.ilike(like, escape="\\"),
            )
        )
    if kind in ("申报通知", "名单", "政策"):
        stmt = stmt.where(kind_filter(Document.title, [kind]))
    type_map = {"申报通知": "申报通知", "名单与公示": "名单", "政策文件": "政策", "其他": "其他"}
    picked_types = [type_map[t] for t in _split(types) if t in type_map]
    if picked_types:
        stmt = stmt.where(kind_filter(Document.title, picked_types))
    picked_regions = [r for r in _split(regions) if r != "不限地区"]
    if picked_regions:
        provinces = ["国家" if r == "国家部委" else r for r in picked_regions]
        stmt = stmt.where(Source.province.in_(provinces))
    if _split(status):
        stmt = stmt.where(status_filter(Document.title, _split(status)))
    if _split(depts):
        stmt = stmt.where(_dept_filter(_split(depts)))

    total_all = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if time in _TIME_DAYS:
        stmt = stmt.where(Document.publish_date >= today() - timedelta(days=_TIME_DAYS[time]))
    elif time == "今年":
        stmt = stmt.where(Document.publish_date >= date(today().year, 1, 1))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    return {
        "q": kw,
        "total": total,
        "total_all": total_all,
        "items": _items(session, [(doc, src) for doc, src, *_ in rows]),
    }


def _fmt_deadline_note(d: DocumentDeadline) -> str:
    day = d.approved_date
    time = f" {_hhmm(d.approved_time)}" if d.approved_time else ""
    return f"{d.kind} {day.month}月{day.day}日 {_WEEK[day.weekday()]}{time}"


@router.get("/documents/{doc_id}")
def document(doc_id: int, session: SessionDep) -> dict:
    """文件详情：要点、附件、原文出处；截止日期只给核对通过的。"""
    row = session.execute(_base().where(Document.id == doc_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份文件")
    doc, src = row
    detail = _details(session, [doc.id]).get(doc.id)
    approved = _approved(session, [doc.id]).get(doc.id, [])
    issuer = detail.issuer if detail and detail.issuer else src.dept_full_name
    points = [["发文机关", issuer]]
    if detail and detail.doc_number:
        points.append(["文号", detail.doc_number])
    points.append(["适用地区", "全国" if src.province == "国家" else src.province])

    deadline = None
    excerpt = None
    checked = None
    if approved:
        main = approved[0]
        deadline = {
            "label": main.kind,
            "date": main.approved_date.isoformat(),
            "time": _hhmm(main.approved_time),
            "verified": True,
            "notes": [_fmt_deadline_note(d) for d in approved[1:]],
        }
        excerpt = {"section": "截止时间原文", "text": main.evidence}
        checked = main.reviewed_at.astimezone(_TZ).date().isoformat() if main.reviewed_at else None

    kind = doc_kind(doc.title)
    return {
        "id": str(doc.id),
        "kind": kind,
        "tags": [kind],
        "region": region_label(src.province, src.level),
        "title": doc.title,
        "issuer": issuer,
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "url": doc.url,
        "site": f"{src.dept_full_name}网站",
        "host": urlsplit(doc.url).hostname or "",
        "deadline": deadline,
        "points": points,
        "excerpt": excerpt,
        "attachments": [
            {"ext": a.extension.upper(), "name": a.name, "url": a.url}
            for a in (detail.attachments if detail else [])
        ],
        "checked": checked,
    }


@router.get("/calendar")
def calendar(session: SessionDep, days: Annotated[int, Query(ge=1, le=366)] = 366) -> dict:
    """申报日历：核对通过、还没截止的事项（按截止日期排序）。"""
    start = today()
    rows = session.execute(
        select(DocumentDeadline, Document, Source)
        .join(Document, DocumentDeadline.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(
            DocumentDeadline.status == "approved",
            DocumentDeadline.approved_date >= start,
            DocumentDeadline.approved_date <= start + timedelta(days=days),
        )
        .order_by(DocumentDeadline.approved_date, DocumentDeadline.approved_time.nulls_last())
    ).all()
    return {
        "today": start.isoformat(),
        "items": [
            {
                "id": f"dl{d.id}",
                "docId": str(doc.id),
                "due": d.approved_date.isoformat(),
                "time": _hhmm(d.approved_time),
                "region": f"{'国家' if src.province == '国家' else src.province} · {src.dept}",
                "kind": doc_kind(doc.title),
                "title": doc.title,
                "what": d.kind,
                "hasDetail": True,
                "verified": True,
            }
            for d, doc, src in rows
        ],
    }


@router.get("/lists/recent")
def recent_lists(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """最近发布的名单与公示，以及本周份数。"""
    stmt = _base().where(kind_filter(Document.title, ["名单"]))
    start = today() - timedelta(days=today().weekday())
    week_total = (
        session.scalar(
            select(func.count()).select_from(stmt.where(Document.publish_date >= start).subquery())
        )
        or 0
    )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    return {
        "week_total": week_total,
        "total": total,
        "items": _items(session, [tuple(r) for r in rows]),
    }
