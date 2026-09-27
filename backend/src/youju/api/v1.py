"""小程序用的公开接口（/api/v1）：首页、搜索筛选、文件详情、申报日历、最近名单、查名单。

返回格式与小程序 mock/data.js 一致。截止日期只返回人工核对通过的（approved）；
公开接口不返回正文全文，只给核对过的字段和原文链接。
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
from youju.dedup import canonical_ids, group_ids, groups_for, visible_documents
from youju.lists.rules import candidate_key, is_person_query, normalize_name
from youju.models import Document, DocumentDeadline, DocumentDetail, ListEntry, Source

router = APIRouter(prefix="/api/v1", tags=["小程序"])
SessionDep = Annotated[Session, Depends(get_session)]

_TZ = ZoneInfo("Asia/Shanghai")
_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 同一份文件有多个截止时，列表和日历优先显示企业申报截止
_DEADLINE_ORDER = {"企业申报截止": 0, "材料报送截止": 1, "推荐或审核截止": 2}

# 筛选项的合法取值（与小程序筛选弹层一致）；其他值一律返回 422，不静默忽略
TABS = {"all", "申报通知", "名单", "政策"}
_LOOKUP_LIMIT = 100  # 查名单最多列出的名单份数
_LOOKUP_SCAN = 2000  # 查名单最多扫描的名单文件数（按发布日期从新到旧），防止极端情况下查询过慢
TYPES = {"申报通知": "申报通知", "名单与公示": "名单", "政策文件": "政策", "其他": "其他"}
STATUSES = {"公示", "公布"}
TIMES = {"近 7 天": 7, "近 30 天": 30, "今年": None, "不限": None}
DEPTS = {"科技部门", "工信部门", "其他部门"}
PROVINCES = {
    "国家部委", "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "不限地区",
}  # fmt: skip
_TECH = ("科技",)
_INDUSTRY = ("工信", "经信", "工业和信息化")


def now() -> datetime:
    """业务时间按北京时间（测试里替换这个函数）。"""
    return datetime.now(_TZ)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pick(value: str | None, allowed: set[str] | dict[str, Any], name: str) -> list[str]:
    """逗号分隔的筛选项：最多 40 项，每项必须是合法取值。"""
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    if len(items) > 40:
        raise HTTPException(status_code=422, detail=f"{name}最多 40 项")
    bad = [v for v in items if v not in allowed]
    if bad:
        raise HTTPException(status_code=422, detail=f"{name}不支持：{'、'.join(bad[:3])}")
    return items


def _week(d: date) -> tuple[date, date]:
    """本周：周一（含）到下周一（不含）。"""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=7)


def _approved(session: Session, doc_ids: list[int]) -> dict[int, list[DocumentDeadline]]:
    """每份文件核对通过的截止日期，按显示优先级排好。"""
    if not doc_ids:
        return {}
    members = groups_for(session, doc_ids)
    rows = session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id.in_([i for ids in members.values() for i in ids]),
            DocumentDeadline.status == "approved",
        )
    ).all()
    by_document: dict[int, list[DocumentDeadline]] = {}
    for d in rows:
        by_document.setdefault(d.document_id, []).append(d)
    out: dict[int, list[DocumentDeadline]] = {}
    for doc_id, ids in members.items():
        seen = set()
        for member in ids:
            for d in by_document.get(member, []):
                key = (d.kind, d.approved_date, d.approved_time)
                if key not in seen:
                    out.setdefault(doc_id, []).append(d)
                    seen.add(key)
    for items in out.values():
        items.sort(key=lambda d: (_DEADLINE_ORDER.get(d.kind, 9), d.approved_date or date.max))
    return out


def _details(session: Session, doc_ids: list[int]) -> dict[int, DocumentDetail]:
    if not doc_ids:
        return {}
    members = groups_for(session, doc_ids)
    rows = session.scalars(
        select(DocumentDetail).where(
            DocumentDetail.document_id.in_([i for ids in members.values() for i in ids])
        )
    ).all()
    by_id = {d.document_id: d for d in rows}
    return {
        doc_id: max(
            (by_id[i] for i in ids if i in by_id),
            key=lambda d: (bool(d.body_text), len(d.body_text), d.document_id == doc_id),
        )
        for doc_id, ids in members.items()
        if any(i in by_id for i in ids)
    }


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
    """批量取详情和截止日期（两次查询），不逐条查库。"""
    ids = [doc.id for doc, _ in rows]
    details, approved = _details(session, ids), _approved(session, ids)
    return [_item(doc, src, details.get(doc.id), approved.get(doc.id, [])) for doc, src in rows]


def _base() -> Select:
    return visible_documents(select(Document, Source).join(Source, Document.source_id == Source.id))


def _ordered(stmt: Select) -> Select:
    return stmt.order_by(Document.publish_date.desc().nulls_last(), Document.id.desc())


def _count(session: Session, stmt: Select) -> int:
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


@router.get("/home")
def home(session: SessionDep) -> dict:
    """首页：本周新增（按发布日期，北京时间周一起算）、分类计数、覆盖范围、最新 8 份。"""
    start, end = _week(now().date())
    week = visible_documents(select(Document.title)).where(
        Document.publish_date >= start, Document.publish_date < end
    )
    kinds = [doc_kind(t) for (t,) in session.execute(week).all()]
    enabled = Source.enabled.is_(True)
    sources = session.scalar(select(func.count()).select_from(Source).where(enabled)) or 0
    provinces = (
        session.scalar(
            select(func.count(func.distinct(Source.province))).where(
                enabled, Source.province != "国家"
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
            "week_total": len(kinds),
            "apply": kinds.count("申报通知"),
            "lists": kinds.count("名单"),
            "other": len(kinds) - kinds.count("申报通知") - kinds.count("名单"),
            "provinces": provinces,
            "sources": sources,
            # 关注条件的本周新增要登录后才能算（#30）；未登录为 null，小程序不显示那一栏
            "followed_new": None,
        },
        "items": _items(session, [tuple(r) for r in latest]),
    }


def _dept_filter(groups: list[str]):
    tech = or_(*(Source.dept.contains(w) for w in _TECH))
    industry = or_(*(Source.dept.contains(w) for w in _INDUSTRY))
    parts = {"科技部门": tech, "工信部门": industry, "其他部门": and_(not_(tech), not_(industry))}
    return or_(*(parts[g] for g in groups))


def _keyword_ids(session: Session, kw: str) -> Select:
    """关键词命中的文件：标题（走 pg_trgm 索引）或文号，分两次查询再合并，互不拖累。"""
    like = f"%{_escape_like(kw)}%"
    by_title = select(Document.id).where(Document.title.ilike(like, escape="\\"))
    by_number = select(DocumentDetail.document_id).where(
        DocumentDetail.doc_number.ilike(like, escape="\\")
    )
    return by_title.union(by_number)


@router.get("/search")
def search(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=50, description="标题或文号关键词")] = None,
    kind: Annotated[str, Query(description="页签：all / 申报通知 / 名单 / 政策")] = "all",
    regions: Annotated[str | None, Query(max_length=400, description="省份，逗号分隔")] = None,
    types: Annotated[
        str | None, Query(max_length=400, description="申报通知,名单与公示,政策文件,其他")
    ] = None,
    status: Annotated[str | None, Query(max_length=400, description="公示,公布")] = None,
    time: Annotated[str, Query(description="近 7 天 / 近 30 天 / 今年 / 不限")] = "近 30 天",
    depts: Annotated[
        str | None, Query(max_length=400, description="科技部门,工信部门,其他部门")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> dict:
    """搜索与筛选。total 是符合全部条件的份数，total_all 是不限发布时间的份数（都按当前页签）。"""
    if kind not in TABS:
        raise HTTPException(status_code=422, detail=f"页签不支持：{kind}")
    if time not in TIMES:
        raise HTTPException(status_code=422, detail=f"发布时间不支持：{time}")
    picked_types = [TYPES[t] for t in _pick(types, TYPES, "类型")]
    picked_regions = [r for r in _pick(regions, PROVINCES, "地区") if r != "不限地区"]
    picked_status = _pick(status, STATUSES, "名单状态")
    picked_depts = _pick(depts, DEPTS, "发文部门")

    stmt = _base()
    kw = (q or "").strip()
    if kw:
        stmt = stmt.where(Document.id.in_(_keyword_ids(session, kw)))
    if kind != "all":
        stmt = stmt.where(kind_filter(Document.title, [kind]))
    if picked_types:
        stmt = stmt.where(kind_filter(Document.title, picked_types))
    if picked_regions:
        stmt = stmt.where(
            Source.province.in_(["国家" if r == "国家部委" else r for r in picked_regions])
        )
    if picked_status:
        stmt = stmt.where(status_filter(Document.title, picked_status))
    if picked_depts:
        stmt = stmt.where(_dept_filter(picked_depts))

    total_all = _count(session, stmt)
    d = now().date()
    if TIMES[time]:
        stmt = stmt.where(Document.publish_date >= d - timedelta(days=TIMES[time]))
    elif time == "今年":
        stmt = stmt.where(Document.publish_date >= date(d.year, 1, 1))
    total = _count(session, stmt)
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    return {
        "q": kw,
        "total": total,
        "total_all": total_all,
        "items": _items(session, [tuple(r) for r in rows]),
    }


def _fmt_deadline_note(d: DocumentDeadline) -> str:
    day = d.approved_date
    time = f" {_hhmm(d.approved_time)}" if d.approved_time else ""
    return f"{d.kind} {day.month}月{day.day}日 {_WEEK[day.weekday()]}{time}"


@router.get("/documents/{doc_id}")
def document(doc_id: int, session: SessionDep) -> dict:
    """文件详情：要点、附件、原文出处；截止日期只给核对通过的；不返回正文全文。"""
    doc_id = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
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

    deadline = excerpt = checked = None
    if approved:
        main = approved[0]
        deadline = {
            "label": main.kind,
            "date": main.approved_date.isoformat(),
            "time": _hhmm(main.approved_time),
            "verified": True,
            "notes": [_fmt_deadline_note(d) for d in approved[1:]],
        }
        # 摘录只给核对过的那句原文证据
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
        "original_urls": [
            d.url
            for d in session.scalars(
                select(Document)
                .where(Document.id.in_(group_ids(session, doc.id)))
                .order_by(Document.id)
            )
        ],
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
def calendar(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=366)] = 366,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> dict:
    """申报日历：核对通过、还没截止的事项（当天已过具体时间的不算），按截止时间排序。

    每条的 id 是截止记录编号（一份文件可以有多个截止）；打开详情用 docId。
    """
    current = now()
    d, t = current.date(), current.time().replace(tzinfo=None)
    not_passed = or_(
        DocumentDeadline.approved_date > d,
        and_(
            DocumentDeadline.approved_date == d,
            or_(DocumentDeadline.approved_time.is_(None), DocumentDeadline.approved_time >= t),
        ),
    )
    stmt = (
        select(DocumentDeadline, Document, Source)
        .join(Document, DocumentDeadline.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(
            DocumentDeadline.status == "approved",
            not_passed,
            DocumentDeadline.approved_date <= d + timedelta(days=days),
        )
    )
    rows = session.execute(
        stmt.order_by(
            DocumentDeadline.approved_date,
            DocumentDeadline.approved_time.nulls_last(),
            DocumentDeadline.id,
        )
    ).all()
    mapping = canonical_ids(session, [doc.id for _, doc, _ in rows])
    unique = {}
    for dl, doc, src in rows:
        master_id = mapping.get(doc.id, doc.id)
        key = (master_id, dl.kind, dl.approved_date, dl.approved_time)
        if key not in unique:
            master = session.get(Document, master_id) if master_id != doc.id else doc
            unique[key] = (dl, master, master.source if master_id != doc.id else src)
    total = len(unique)
    rows = list(unique.values())[offset : offset + limit]
    return {
        "today": d.isoformat(),
        "total": total,
        "items": [
            {
                "id": f"dl{dl.id}",
                "docId": str(doc.id),
                "due": dl.approved_date.isoformat(),
                "time": _hhmm(dl.approved_time),
                "region": f"{'国家' if src.province == '国家' else src.province} · {src.dept}",
                "kind": doc_kind(doc.title),
                "title": doc.title,
                "what": dl.kind,
                "hasDetail": True,
                "verified": True,
            }
            for dl, doc, src in rows
        ],
    }


@router.get("/lists/recent")
def recent_lists(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> dict:
    """最近发布的名单与公示，以及本周份数。"""
    stmt = _base().where(kind_filter(Document.title, ["名单"]))
    start, end = _week(now().date())
    week_total = _count(
        session, stmt.where(Document.publish_date >= start, Document.publish_date < end)
    )
    total = _count(session, stmt)
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    return {
        "week_total": week_total,
        "total": total,
        "items": _items(session, [tuple(r) for r in rows]),
    }


@router.get("/lists/lookup")
def lookup_lists(
    session: SessionDep,
    q: Annotated[
        str, Query(min_length=2, max_length=100, description="企业全称或统一社会信用代码")
    ],
) -> dict:
    """查名单：这家企业上过哪些名单。

    按企业全称或统一社会信用代码与名单原文逐条精确比对。按名称查到的条目带信用代码时，
    再用信用代码找回同一企业用别的名称（如更名前）上过的名单。同名但信用代码不同的，
    标 ambiguous，由小程序提示逐条核对。名称相近的另列（similar），不当作同一家。
    同一份名单只列一次；同一通知的不同网址按内容归并（youju.dedup）后也只列一次。只查企业和机构，
    输入像个人姓名的返回 422。coverage 是已收录的名单范围，查不到时据此说明。
    """
    query = normalize_name(q)
    if not query or is_person_query(query):
        raise HTTPException(
            status_code=422, detail="只查企业和机构，请输入企业全称或统一社会信用代码"
        )
    code = query.upper()

    def public(stmt: Select) -> Select:
        return (
            stmt.join(Document, Document.id == ListEntry.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(Source.rights_level == 1)
        )

    direct = or_(ListEntry.normalized_name == query, ListEntry.credit_code == code)
    name_codes = set(
        session.scalars(
            public(select(ListEntry.credit_code).distinct()).where(
                ListEntry.normalized_name == query, ListEntry.credit_code.is_not(None)
            )
        )
    )
    codes = set(
        session.scalars(
            public(select(ListEntry.credit_code).distinct())
            .where(direct, ListEntry.credit_code.is_not(None))
            .limit(20)
        )
    )
    matched = or_(direct, ListEntry.credit_code.in_(codes)) if codes else direct
    # 名单条目所在文件先映射到归并后的主文件（同一通知的不同网址算一份），按主文件去重后再截取
    found = session.execute(
        public(select(ListEntry.document_id, Document.publish_date).distinct())
        .where(matched)
        .order_by(Document.publish_date.desc().nulls_last(), ListEntry.document_id.desc())
        .limit(_LOOKUP_SCAN)
    ).all()
    mapping = canonical_ids(session, [doc_id for doc_id, _ in found])
    newest: dict[int, date | None] = {}
    for doc_id, published in found:
        master = mapping.get(doc_id, doc_id)
        if master not in newest or (published and (newest[master] or date.min) < published):
            newest[master] = published
    masters = sorted(newest, key=lambda m: (newest[m] or date.min, m), reverse=True)
    truncated = len(masters) > _LOOKUP_LIMIT or len(found) >= _LOOKUP_SCAN
    masters = masters[:_LOOKUP_LIMIT]
    keep = set(masters)
    # 每份名单选一条展示：名称完全相同的优先，其次信用代码相同的
    best: dict[int, ListEntry] = {}
    members = [doc_id for doc_id, _ in found if mapping.get(doc_id, doc_id) in keep]
    for entry in session.scalars(
        select(ListEntry).where(ListEntry.document_id.in_(members), matched).order_by(ListEntry.id)
    ):
        master = mapping.get(entry.document_id, entry.document_id)
        current = best.get(master)
        if current is None or (entry.normalized_name == query and current.normalized_name != query):
            best[master] = entry
    rows = session.execute(_ordered(_base().where(Document.id.in_(masters)))).all()
    items: list[dict] = []
    for item in _items(session, [tuple(r) for r in rows]):
        entry = best[int(item["id"])]
        item["matched"] = {
            "name": entry.name,
            "category": entry.category,
            "batch": entry.batch,
            "sequence": entry.sequence,
            "creditCode": entry.credit_code,
            "region": entry.region,
            "via": "name" if entry.normalized_name == query else "creditCode",
        }
        items.append(item)
    shown_names = {entry.normalized_name for entry in best.values()}
    similar: list[dict] = []
    key = candidate_key(query)
    if len(query) >= 4 and not query.isascii() and len(key) >= 2:
        names = session.execute(
            select(
                ListEntry.normalized_name,
                func.min(ListEntry.name),
                func.count(func.distinct(ListEntry.document_id)),
            )
            .join(Document, Document.id == ListEntry.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Source.rights_level == 1,
                ListEntry.normalized_name.like(f"%{_escape_like(key[:4])}%", escape="\\"),
                ListEntry.normalized_name != query,
            )
            .group_by(ListEntry.normalized_name)
            .limit(500)
        ).all()
        similar = sorted(
            (
                {"name": name, "lists": n}
                for normalized, name, n in names
                # 信用代码相同、已列在结果里的别名（如更名前的名称）不再算"相近"
                if candidate_key(normalized) == key and normalized not in shown_names
            ),
            key=lambda x: -x["lists"],
        )[:10]
    lists_total, since = session.execute(
        select(func.count(func.distinct(ListEntry.document_id)), func.min(Document.publish_date))
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(Source.rights_level == 1)
    ).one()
    return {
        "query": q.strip(),
        "items": items,
        "similar": similar,
        "ambiguous": len(name_codes) > 1,
        "truncated": truncated,
        "coverage": {"lists": lists_total, "since": since.isoformat() if since else None},
    }
