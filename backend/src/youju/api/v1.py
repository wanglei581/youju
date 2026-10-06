"""小程序用的公开接口（/api/v1）：首页、搜索筛选、文件详情、申报日历、报告、最近名单、查名单。

返回格式与小程序 mock/data.js 一致。截止日期只返回人工核对通过的（approved）；待核对的候选
只给"有没有"（deadline_state），不给日期；
截止日期等关键字段只给核对过的；详情页另给正文供阅读（D-030，见 body_for_reading），名单类不给。
"""

from __future__ import annotations

import re
import time as _time
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from youju import fields as field_rules
from youju.attachments import attachment_items, is_recent
from youju.categories import CATEGORIES, CATEGORY_KINDS, category_filter, shown_category
from youju.classify import (
    GUIDE,
    KINDS,
    effective_kind,
    kind_filter,
    list_status,
    region_label,
    status_filter,
)
from youju.crawler.detail import redact_sensitive
from youju.db import get_session
from youju.dedup import canonical_ids, group_ids, groups_for, visible_documents
from youju.fields import SUPPORT_LABELS
from youju.lists.lookup import lookup_list_matches, public_condition
from youju.lists.rules import candidate_key, is_person_query, normalize_name
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    DocumentMerge,
    JobRun,
    ListEntry,
    Source,
)
from youju.report_revision import publication_revision
from youju.takedown import available_condition, is_taken_down
from youju.title_tags import INDUSTRIES, INDUSTRY_RULES, SUPPORT_KINDS, SUPPORT_MODES, SUPPORT_RULES

router = APIRouter(prefix="/api/v1", tags=["小程序"])
SessionDep = Annotated[Session, Depends(get_session)]

_TZ = ZoneInfo("Asia/Shanghai")
_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 同一份文件有多个截止时，列表和日历优先显示企业申报截止
_DEADLINE_ORDER = {"企业申报截止": 0, "材料报送截止": 1, "推荐或审核截止": 2}

# 截止雷达按进程缓存，避免首页/报告页短时间内重复扫描全部公开文件。
# 测试可直接 clear()；值为 (写入时刻, 响应对象)。
_REPORT_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
# 兼容测试和运维脚本使用更直观的名称。
_deadline_report_cache = _REPORT_CACHE
REPORT_CACHE_TTL = 300
REPORT_CACHE_MAX = 200

# 筛选项的合法取值（与小程序筛选弹层一致）；其他值一律返回 422，不静默忽略
TABS = {"all", "申报通知", "名单", "政策"}
_LOOKUP_LIMIT = 100  # 查名单最多列出的名单份数
_LOOKUP_SCAN = 2000  # 查名单最多扫描的名单文件数（按发布日期从新到旧），防止极端情况下查询过慢
TYPES = {
    "申报通知": "申报通知",
    "名单与公示": "名单",
    "政策文件": "政策",
    GUIDE: GUIDE,
    "其他": "其他",
}
# 项目类别（按标题归类，仅供检索参考，非官方分类；只有申报通知和名单有）
CATEGORY_SET = set(CATEGORIES)
# 支持方式、产业方向（按标题归类，仅供检索参考，非官方分类；只用于筛选，不返回、不展示）。
# 支持方式只有申报通知和名单有；产业方向所有类型都有，一份文件可以有多个或没有
SUPPORT_SET = set(SUPPORT_MODES)
INDUSTRY_SET = set(INDUSTRIES)
# "资讯与指南"以外的类型：折叠资讯与指南时列这些（写成正面的类型列表，不用"不是资讯与指南"，
# 免得规则列为空时 NOT 出空值把文件漏掉）
_NOT_GUIDE = [k for k in KINDS if k != GUIDE]
STATUSES = {"公示", "公布"}
TIMES = {"近 7 天": 7, "近 30 天": 30, "今年": None, "不限": None}
DEPTS = {"科技部门", "工信部门", "其他部门"}
LIST_BOARD_TYPES = ("高新技术企业", "专精特新", "科技型中小企业", "其他")
LIST_BOARD_TYPE_SET = set(LIST_BOARD_TYPES)
PROVINCES = {
    "国家部委", "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "不限地区",
}  # fmt: skip
# 层级筛选；地市还没接入，先不提供"市级"
LEVELS = {"国家部委", "省级"}
# 列表和详情的截止状态：verified 有核对通过的截止（deadline 给出日期）；unverified 没有核对通过的、
# 但有待核对的候选（页面写"截止未核对"，不给任何日期）；none 没有候选（页面写"截止时间见原文"）
DEADLINE_STATES = ("verified", "unverified", "none")
# 31 个省级地区（不含港澳台），按国家行政区划顺序；用于"已收录 / 还没收录"（验收 A8）
_PROVINCE_ORDER = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
)  # fmt: skip
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


def deadline_open(deadline, stamp: datetime) -> bool:
    """截止还没过（北京时间）：日期在今天之后，或就是今天且时刻未过（没写时刻算到当天结束）。"""
    stamp = stamp.astimezone(_TZ)
    today = stamp.date()
    local_time = stamp.time().replace(tzinfo=None)
    return deadline.approved_date is not None and (
        deadline.approved_date > today
        or (
            deadline.approved_date == today
            and (deadline.approved_time is None or deadline.approved_time >= local_time)
        )
    )


def _approved(session: Session, doc_ids: list[int]) -> dict[int, list[DocumentDeadline]]:
    """每份文件核对通过的截止日期，按显示优先级排好。"""
    if not doc_ids:
        return {}
    members = groups_for(session, doc_ids)
    rows = session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id.in_([i for ids in members.values() for i in ids]),
            DocumentDeadline.status.in_(("approved", "changed")),
        )
    ).all()
    by_document: dict[int, list[DocumentDeadline]] = {}
    for d in rows:
        by_document.setdefault(d.document_id, []).append(d)
    out: dict[int, list[DocumentDeadline]] = {}
    for doc_id, ids in members.items():
        # 同组里同一截止已标"已变更"的，其他仍为"已通过"的副本也不再当作有效截止（与导出一致）
        seen = {
            (d.kind, d.approved_date, d.approved_time)
            for member in ids
            for d in by_document.get(member, [])
            if d.status == "changed"
        }
        for member in ids:
            for d in by_document.get(member, []):
                key = (d.kind, d.approved_date, d.approved_time)
                if d.status == "approved" and key not in seen:
                    out.setdefault(doc_id, []).append(d)
                    seen.add(key)
    for items in out.values():
        items.sort(key=lambda d: (_DEADLINE_ORDER.get(d.kind, 9), d.approved_date or date.max))
    return out


def _pending(session: Session, doc_ids: list[int]) -> set[int]:
    """哪些文件（看整个归并组）有待核对的截止候选。只查有没有，不读候选日期。"""
    if not doc_ids:
        return set()
    members = groups_for(session, doc_ids)
    hit = set(
        session.scalars(
            select(DocumentDeadline.document_id)
            .where(
                DocumentDeadline.document_id.in_([i for ids in members.values() for i in ids]),
                DocumentDeadline.status == "pending",
            )
            .distinct()
        )
    )
    return {doc_id for doc_id, ids in members.items() if hit.intersection(ids)}


def deadline_state(approved: list[DocumentDeadline], pending: bool) -> str:
    """三种截止状态（见 DEADLINE_STATES）。approved 只接收 _approved 的结果：
    有核对通过的就是 verified，和 deadline 字段有日期一一对应。"""
    if approved:
        return "verified"
    return "unverified" if pending else "none"


def verified_total(session: Session) -> int:
    """截止雷达、申报日历空状态里的"已核对 X 条"：有核对通过截止的公开文件份数。

    与列表的 deadline_state == "verified" 同一口径：同一通知的不同网址算一份，已变更的不算，
    已下架和不公开的不算。只给数字，不给日期。
    """
    ids = list(
        session.scalars(
            select(DocumentDeadline.document_id)
            .where(DocumentDeadline.status == "approved")
            .distinct()
        )
    )
    if not ids:
        return 0
    mapping = canonical_ids(session, ids)
    masters = sorted({mapping.get(doc_id, doc_id) for doc_id in ids})
    visible = [doc.id for doc, _ in session.execute(_base().where(Document.id.in_(masters)))]
    approved = _approved(session, visible)
    return sum(1 for doc_id in visible if approved.get(doc_id))


def _public_fields() -> Select:
    """读取时核对当前正文指纹；不等重抽批处理，旧字段就视作 stale。

    归并组可以展示主文件，但字段本身必须来自一级来源的那份正文。
    """
    field_doc = aliased(Document)
    field_source = aliased(Source)
    return (
        select(DocumentField)
        .join(DocumentDetail, DocumentDetail.document_id == DocumentField.document_id)
        .join(field_doc, field_doc.id == DocumentField.document_id)
        .join(field_source, field_source.id == field_doc.source_id)
        .where(
            DocumentField.status == "approved",
            DocumentDetail.status == "ok",
            DocumentField.body_hash == func.sha2(DocumentDetail.body_text, 256),
            field_source.rights_level == 1,
            available_condition(field_doc),
            or_(DocumentField.field != "audience", field_rules.AUDIENCE_ENABLED),
        )
    )


def _support_condition(support: str):
    """在 SQL 中匹配主文件或已归并副本的有效支持方式。"""
    return (
        _public_fields()
        .outerjoin(
            DocumentMerge,
            and_(
                DocumentMerge.duplicate_id == DocumentField.document_id,
                DocumentMerge.active.is_(True),
            ),
        )
        .where(
            func.coalesce(DocumentMerge.canonical_id, DocumentField.document_id) == Document.id,
            DocumentField.field == "support",
            func.coalesce(func.nullif(DocumentField.approved_value, ""), DocumentField.value)
            == support,
        )
        .correlate(Document)
        .exists()
    )


def _approved_fields(session: Session, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
    """批量取当前正文的已通过、公开来源字段。"""
    if not doc_ids:
        return {}
    members = groups_for(session, doc_ids)
    all_ids = [item for ids in members.values() for item in ids]
    rows = session.scalars(
        _public_fields().where(DocumentField.document_id.in_(all_ids)).order_by(DocumentField.id)
    ).all()
    owner: dict[int, int] = {member: master for master, ids in members.items() for member in ids}
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = result.setdefault(owner.get(row.document_id, row.document_id), {})
        value = row.approved_value or row.value
        if row.field == "support":
            supports = item.setdefault("support", [])
            if value not in supports:
                supports.append(value)
            if row.evidence and value not in item.setdefault("support_evidence", {}):
                item["support_evidence"][value] = row.evidence
        elif row.field == "audience" and "audience" not in item:
            item["audience"] = {"text": value, "evidence": row.evidence}
    for item in result.values():
        if "support" in item:
            item["support"].sort(key=lambda value: SUPPORT_LABELS.index(value))
    return {doc_id: result.get(doc_id, {}) for doc_id in doc_ids if result.get(doc_id)}


def _selected_deadline(deadlines: list[DocumentDeadline]) -> DocumentDeadline | None:
    """按方案选择一份文件用于申报状态/截止雷达的主截止。"""
    if not deadlines:
        return None
    enterprise = [d for d in deadlines if d.kind == "企业申报截止"]
    pool = enterprise or deadlines
    return min(
        pool,
        key=lambda d: (d.approved_date or date.max, d.approved_time or datetime.max.time(), d.id),
    )


def application_status(
    deadlines: list[DocumentDeadline], stamp: datetime | None = None
) -> dict[str, Any] | None:
    """计算申报状态；只接收 _approved 返回的 approved 截止。"""
    selected = _selected_deadline(deadlines)
    if selected is None or selected.approved_date is None:
        return None
    stamp = (stamp or now()).astimezone(_TZ)
    today = stamp.date()
    due = selected.approved_date
    due_time = selected.approved_time
    closed = due < today or (
        due == today and due_time is not None and stamp.time().replace(tzinfo=None) > due_time
    )
    code = "closed" if closed else "closing" if (due - today).days <= 7 else "open"
    return {
        "code": code,
        "label": selected.kind,
        "date": due.isoformat(),
        "time": due_time.strftime("%H:%M") if due_time else None,
    }


def upcoming_deadline(
    deadlines: list[DocumentDeadline], stamp: datetime, days: int
) -> DocumentDeadline | None:
    """截止雷达口径：主截止（见 _selected_deadline）没过、且在今天起 days 天内时返回它。

    只接收 _approved 返回的已核对截止；截止雷达和周报的临期提醒共用这一口径。
    """
    selected = _selected_deadline(deadlines)
    if selected is None or selected.approved_date is None:
        return None
    status_value = application_status([selected], stamp)
    if not status_value or status_value["code"] == "closed":
        return None
    remaining = (selected.approved_date - stamp.astimezone(_TZ).date()).days
    return selected if 0 <= remaining <= days else None


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
    doc: Document,
    src: Source,
    detail: DocumentDetail | None,
    deadlines: list[DocumentDeadline],
    pending: bool = False,
) -> dict:
    """列表行（与小程序 doc-row 的数据格式一致）。"""
    kind = effective_kind(doc.title, doc.kind_override)
    app_status = application_status(deadlines) if kind == "申报通知" else None
    payload = {
        "id": str(doc.id),
        "kind": kind,
        # 项目类别：按标题归类，仅供检索参考；只有申报通知和名单有，其他为 null
        "category": shown_category(kind, doc.rule_category, doc.title),
        # status 一直是名单状态；申报状态只放在新增的 app_status，保持导出和旧客户端兼容。
        "status": list_status(doc.title, kind),
        "app_status": app_status,
        "region": region_label(src.province, src.level),
        "title": doc.title,
        "issuer": (detail.issuer if detail and detail.issuer else src.dept_full_name),
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "url": doc.url,
        "deadline": _deadline_brief(deadlines[0] if deadlines else None),
        "deadline_state": deadline_state(deadlines, pending),
    }
    return payload


def _items(session: Session, rows: list[tuple[Document, Source]]) -> list[dict]:
    """批量取详情、截止日期和待核对标记（各一次查询），不逐条查库。"""
    ids = [doc.id for doc, _ in rows]
    details, approved = _details(session, ids), _approved(session, ids)
    # 已有核对通过截止的不必再查候选
    pending = _pending(session, [doc_id for doc_id in ids if not approved.get(doc_id)])
    return [
        _item(doc, src, details.get(doc.id), approved.get(doc.id, []), doc.id in pending)
        for doc, src in rows
    ]


def _kind_is(kinds: list[str]):
    """类型筛选：人工定过类型的按人工定的，没定过的按标题规则——规则结果入库时已算好
    存在 rule_kind，只比较这一列；还没算的（刚升级、还没跑 youju reclassify）当场按规则判断，
    结果一样。"""
    by_rule = or_(
        Document.rule_kind.in_(kinds),
        and_(Document.rule_kind.is_(None), kind_filter(Document.title, kinds)),
    )
    return or_(
        Document.kind_override.in_(kinds),
        and_(Document.kind_override.is_(None), by_rule),
    )


def _category_is(categories: list[str]):
    """项目类别筛选：只在申报通知和名单里找；类别入库时已算好存在 rule_category，
    还没算的（刚升级、还没跑 youju reclassify）当场按规则判断，结果一样。"""
    by_rule = or_(
        Document.rule_category.in_(categories),
        and_(Document.rule_category.is_(None), category_filter(Document.title, categories)),
    )
    return and_(_kind_is(list(CATEGORY_KINDS)), by_rule)


def _supports_are(supports: list[str]):
    """支持方式筛选（按标题）：只在申报通知和名单里找；选了几项，有其中任一项就列出。"""
    return and_(
        _kind_is(list(SUPPORT_KINDS)),
        SUPPORT_RULES.filter(Document.rule_supports, Document.title, supports),
    )


def _industries_are(industries: list[str]):
    """产业方向筛选（按标题）：所有类型；选了几项，有其中任一项就列出。"""
    return INDUSTRY_RULES.filter(Document.rule_industries, Document.title, industries)


def _folds_guides(kind: str, types: str | None) -> bool:
    """只在"全部"页签、没选类型时折叠资讯与指南；选了类型就按选的列（选了资讯与指南就照常列出）。"""
    return kind == "all" and not _pick(types, TYPES, "类型")


def fold_guides(
    session: Session, stmt: Select, kind: str, types: str | None, fold: int | None
) -> tuple[Select, int, int]:
    """按 fold 处理"资讯与指南"（搜索和关注的新文件共用）：返回（要列出的查询, 资讯与指南篇数,
    折叠了几篇）。fold 不传时不统计（旧版小程序）；0 照常列出、给出篇数；1 不列出、只给篇数。"""
    if fold is None or not _folds_guides(kind, types):
        return stmt, 0, 0
    guides = _count(session, stmt.where(_kind_is([GUIDE])))
    if fold != 1:
        return stmt, guides, 0
    return stmt.where(_kind_is(_NOT_GUIDE)), guides, guides


def _base() -> Select:
    """公开给小程序的文件：同一通知只列主文件，只列可以公开的（见 public_condition）。"""
    return visible_documents(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(public_condition())
    )


def _ordered(stmt: Select) -> Select:
    # MySQL 降序时空值自然排在最后（没有发布日期的文件排在后面）
    return stmt.order_by(Document.publish_date.desc(), Document.id.desc())


def _count(session: Session, stmt: Select) -> int:
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


@router.get("/home")
def home(session: SessionDep) -> dict:
    """首页：本周新增（按发布日期，北京时间周一起算）、分类计数、覆盖范围、最近一次成功检查的时间、
    最新 8 份。"""
    start, end = _week(now().date())
    week = visible_documents(
        select(Document.title, Document.kind_override)
        .join(Source, Document.source_id == Source.id)
        .where(public_condition())
    ).where(Document.publish_date >= start, Document.publish_date < end)
    kinds = [effective_kind(t, override) for t, override in session.execute(week).all()]
    # 与收录范围（/coverage）同一口径：启用且至少成功检查过一次的栏目
    live = and_(Source.enabled.is_(True), Source.last_success_at.is_not(None))
    sources = session.scalar(select(func.count()).select_from(Source).where(live)) or 0
    provinces = (
        session.scalar(
            select(func.count(func.distinct(Source.province))).where(
                live, Source.province != "国家"
            )
        )
        or 0
    )
    # 最近一次成功检查：定时任务台账里最近一次成功结束的抓取（crawl）的结束时间；没有记录时为空。
    # 本周 0 份时小程序写"本周暂无新文件 · 最近检查 …"，只陈述检查时间，不推测原因。
    last_checked = session.scalar(
        select(func.max(JobRun.finished_at)).where(JobRun.job == "crawl", JobRun.status == "ok")
    )
    # 首页"最新"只放申报通知、名单、政策三类；统计数据发布等其他文件在搜索里能找到
    latest = session.execute(
        _ordered(_base().where(_kind_is(["申报通知", "名单", "政策"]))).limit(8)
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
            "last_checked_at": (
                last_checked.astimezone(_TZ).isoformat(timespec="seconds") if last_checked else None
            ),
        },
        "items": _items(session, [tuple(r) for r in latest]),
    }


@router.get("/coverage")
def coverage(session: SessionDep) -> dict:
    """收录范围（验收 A8）。

    已收录：有启用、且至少成功检查过一次的栏目。栏目数只算成功检查过的；每个地区列出启用的
    栏目和各自最近一次成功检查的时间（还没成功的时间为空，页面写"正在接入"）。
    还没收录的地区（国家部委和 31 个省级地区）附原因：网站不允许程序访问（抓取模式 blocked）、
    正在接入（需浏览器渲染，或已启用但还没成功）、暂未接入。
    """
    sources = session.scalars(
        select(Source).order_by(
            Source.province, Source.dept_full_name, Source.column_name, Source.id
        )
    ).all()

    def iso(value: datetime | None) -> str | None:
        return value.astimezone(_TZ).isoformat() if value else None

    regions, not_covered = [], []
    for province in ("国家", *_PROVINCE_ORDER):
        name = "国家部委" if province == "国家" else province
        mine = [s for s in sources if s.province == province]
        enabled = [s for s in mine if s.enabled]
        lasts = [s.last_success_at for s in enabled if s.last_success_at]
        if lasts:
            regions.append(
                {
                    "name": name,
                    "sources": len(lasts),
                    "lastUpdatedAt": iso(max(lasts)),
                    "columns": [
                        {
                            "name": f"{s.dept_full_name} · {s.column_name}",
                            "lastUpdatedAt": iso(s.last_success_at),
                        }
                        for s in enabled
                    ],
                }
            )
        elif enabled or any(s.fetch_mode == "browser" for s in mine):
            not_covered.append({"name": name, "reason": "正在接入"})
        elif any(s.fetch_mode == "blocked" for s in mine):
            not_covered.append({"name": name, "reason": "网站不允许程序访问"})
        else:
            not_covered.append({"name": name, "reason": "暂未接入"})
    lasts = [r["lastUpdatedAt"] for r in regions]
    return {
        "sources": sum(r["sources"] for r in regions),
        "provinces": sum(1 for r in regions if r["name"] != "国家部委"),
        "national": any(r["name"] == "国家部委" for r in regions),
        "regions": regions,
        "notCovered": not_covered,
        "updatedAt": max(lasts) if lasts else None,
    }


def _dept_filter(groups: list[str]):
    tech = or_(*(Source.dept.contains(w) for w in _TECH))
    industry = or_(*(Source.dept.contains(w) for w in _INDUSTRY))
    parts = {"科技部门": tech, "工信部门": industry, "其他部门": and_(not_(tech), not_(industry))}
    return or_(*(parts[g] for g in groups))


def _dept_bucket(value: str) -> str:
    """把来源部门归到报告筛选允许的三个稳定分组。"""
    if "科技" in value:
        return "科技部门"
    if any(word in value for word in _INDUSTRY):
        return "工信部门"
    return "其他部门"


def _list_board_type(title: str) -> str:
    """按标题把名单归到看板的四类；顺序固定，避免标题同时出现多个类别时漂移。"""
    if "高新技术企业" in title:
        return "高新技术企业"
    if "专精特新" in title:
        return "专精特新"
    if "科技型中小企业" in title:
        return "科技型中小企业"
    return "其他"


def _list_board_wording(title: str) -> str | None:
    """只照录标题里的用词；不根据“拟”等词推断是否最终入选。"""
    if "公示" in title:
        return "公示"
    if "公布" in title:
        return "公布"
    return None


def _future_approved_canonical_ids(session: Session, today: date) -> list[int]:
    """取今天起有已核对截止的归并主文件编号。"""
    ids = list(
        session.scalars(
            select(DocumentDeadline.document_id).where(
                DocumentDeadline.status == "approved",
                DocumentDeadline.approved_date >= today,
            )
        )
    )
    mapping = canonical_ids(session, ids)
    return sorted({mapping.get(doc_id, doc_id) for doc_id in ids})


def _keyword_ids(session: Session, kw: str) -> Select:
    """关键词命中的文件：标题或文号，分两次查询再合并，互不拖累。"""
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
    open: Annotated[int | None, Query(ge=0, le=1, description="1 只看未截止（含即将截止）")] = None,
    levels: Annotated[str | None, Query(max_length=400, description="国家部委,省级")] = None,
    categories: Annotated[
        str | None,
        Query(max_length=400, description="项目类别，逗号分隔（只在申报通知和名单里找）"),
    ] = None,
    fold: Annotated[
        int | None,
        Query(ge=0, le=1, description="1 折叠资讯与指南类文件；0 不折叠但给出篇数；不传则不统计"),
    ] = None,
    supports: Annotated[
        str | None,
        Query(max_length=400, description="支持方式（按标题），逗号分隔（只在申报通知和名单里找）"),
    ] = None,
    industries: Annotated[
        str | None, Query(max_length=400, description="产业方向（按标题），逗号分隔")
    ] = None,
) -> dict:
    """搜索与筛选。total 是符合全部条件的份数，total_all 是不限发布时间的份数（都按当前页签）。

    fold=1 时"资讯与指南"类文件不列出、不计入 total 和 total_all，只在 folded 里给出折叠了几篇
    （不悄悄藏：小程序写明"已折叠 X 篇"，点开就按 fold=0 列出来）；guides 是符合条件的
    资讯与指南篇数（折叠时等于 folded）。只在"全部"页签、没选类型时折叠。不传 fold 时
    不返回这两项。"""
    filters = {"levels": levels, "categories": categories}
    filters |= {"supports": supports, "industries": industries}
    stmt = search_statement(session, q, kind, regions, types, status, time, depts, open, **filters)
    all_time = search_statement(
        session, q, kind, regions, types, status, "不限", depts, open, **filters
    )
    stmt, guides, folded = fold_guides(session, stmt, kind, types, fold)
    if fold == 1 and _folds_guides(kind, types):
        all_time = all_time.where(_kind_is(_NOT_GUIDE))
    total = _count(session, stmt)
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    body = {
        "q": (q or "").strip(),
        "total": total,
        "total_all": _count(session, all_time),
        "items": _items(session, [tuple(r) for r in rows]),
    }
    if fold is not None:  # 旧版小程序不传 fold，返回格式和以前一样
        body |= {"folded": folded, "guides": guides}
    return body


# 地区筛选的"国家部委"和层级筛选的"国家部委"共用这一个条件（来源省份填"国家"），两处不会不一致
_NATIONAL = "国家"


def _level_filter(levels: list[str]):
    """层级：国家部委 = 国家级来源；省级 = 省级来源（不含国家级，地市接入后也不含市级）。"""
    parts = {
        "国家部委": Source.province == _NATIONAL,
        "省级": and_(Source.province != _NATIONAL, Source.level == "省级"),
    }
    return or_(*(parts[level] for level in levels))


def search_statement(
    session: Session,
    q: str | None,
    kind: str,
    regions: str | None,
    types: str | None,
    status: str | None,
    time: str,
    depts: str | None,
    open: int | None = None,
    levels: str | None = None,
    categories: str | None = None,
    supports: str | None = None,
    industries: str | None = None,
) -> Select:
    """公开搜索、关注和提醒共用筛选校验及 SQL。

    各组筛选之间取交集，同一组里多选取并集。地区和层级也是这样：地区选的省份（或国家部委）
    和层级选的国家部委 / 省级同时满足才列出。"国家部委"在两组里是同一个条件（见 _NATIONAL），
    所以只在地区里选、只在层级里选、两边都选，结果相同；地区只选省份而层级只选国家部委，
    或地区只选国家部委而层级只选省级，两者没有交集，结果为空（小程序在空结果里提示调整）。
    项目类别只有申报通知和名单有：选了类别，政策、资讯与指南、其他就不再列出。支持方式（按标题）
    也一样；产业方向（按标题）所有类型都有。这三组都是同一组里多选取并集。
    """
    if kind not in TABS:
        raise HTTPException(status_code=422, detail=f"页签不支持：{kind}")
    if time not in TIMES:
        raise HTTPException(status_code=422, detail=f"发布时间不支持：{time}")
    picked_types = [TYPES[t] for t in _pick(types, TYPES, "类型")]
    picked_regions = [r for r in _pick(regions, PROVINCES, "地区") if r != "不限地区"]
    picked_status = _pick(status, STATUSES, "名单状态")
    picked_depts = _pick(depts, DEPTS, "发文部门")
    picked_levels = _pick(levels, LEVELS, "层级")
    picked_categories = _pick(categories, CATEGORY_SET, "项目类别")
    picked_supports = _pick(supports, SUPPORT_SET, "支持方式")
    picked_industries = _pick(industries, INDUSTRY_SET, "产业方向")

    stmt = _base()
    kw = (q or "").strip()
    if kw:
        stmt = stmt.where(Document.id.in_(_keyword_ids(session, kw)))
    if kind != "all":
        stmt = stmt.where(_kind_is([kind]))
    if picked_types:
        stmt = stmt.where(_kind_is(picked_types))
    if picked_regions:
        stmt = stmt.where(
            Source.province.in_([_NATIONAL if r == "国家部委" else r for r in picked_regions])
        )
    if picked_levels:
        stmt = stmt.where(_level_filter(picked_levels))
    if picked_status:
        stmt = stmt.where(status_filter(Document.title, picked_status, _kind_is(["名单"])))
    if picked_depts:
        stmt = stmt.where(_dept_filter(picked_depts))
    if picked_categories:
        stmt = stmt.where(_category_is(picked_categories))
    if picked_supports:
        stmt = stmt.where(_supports_are(picked_supports))
    if picked_industries:
        stmt = stmt.where(_industries_are(picked_industries))

    if open is not None:
        if open != 1:
            raise HTTPException(status_code=422, detail="open 只支持 1")
        # 先从今天起、人工核对通过的截止取文件编号，再与公开的申报通知相交。
        # 这样不会为 open=1 把全部公开文件加载到进程内；_approved 仍负责归并组和 changed 口径。
        candidate_ids = _future_approved_canonical_ids(session, now().date())
        stmt = stmt.where(_kind_is(["申报通知"]))
        approved = _approved(session, candidate_ids)
        open_ids = []
        current = now()
        for document_id in candidate_ids:
            status_value = application_status(approved.get(document_id, []), current)
            if status_value and status_value["code"] in ("open", "closing"):
                open_ids.append(document_id)
        stmt = stmt.where(Document.id.in_(open_ids or [-1]))

    d = now().date()
    if TIMES[time]:
        stmt = stmt.where(Document.publish_date >= d - timedelta(days=TIMES[time]))
    elif time == "今年":
        stmt = stmt.where(Document.publish_date >= date(d.year, 1, 1))
    return stmt


def _report_payload(
    session: Session, days: int, province: str, dept: str, support: str, page: int, size: int
) -> dict[str, Any]:
    """截止雷达数据；同一份文件只取方案规定的主截止。"""
    current = now().astimezone(_TZ)
    today = current.date()
    # 先从未来且已核对通过的截止取主文件编号，再限定公开的申报通知；
    # 已变更的副本由 _approved 按归并组抑制，和列表/导出的口径一致。
    candidate_ids = _future_approved_canonical_ids(session, today)
    stmt = _base().where(Document.id.in_(candidate_ids), _kind_is(["申报通知"]))
    if support:
        stmt = stmt.where(_support_condition(support))
    rows = session.execute(stmt).all()
    selected_ids = [doc.id for doc, _ in rows]
    approved = _approved(session, selected_ids)
    details = _details(session, selected_ids)
    entries: list[dict[str, Any]] = []
    for doc, src in rows:
        if effective_kind(doc.title, doc.kind_override) != "申报通知":
            continue
        if province and province not in (
            src.province,
            "国家部委" if src.province == "国家" else src.province,
        ):
            continue
        if dept and _dept_bucket(src.dept) != dept:
            continue
        selected = upcoming_deadline(approved.get(doc.id, []), current, 30)
        if selected is None:
            continue
        remaining = (selected.approved_date - today).days
        detail = details.get(doc.id)
        entries.append(
            {
                "id": str(doc.id),
                "title": doc.title,
                "issuer": detail.issuer if detail and detail.issuer else src.dept_full_name,
                "province": "国家部委" if src.province == "国家" else src.province,
                "dept": src.dept_full_name,
                "label": selected.kind,
                "deadline_type": selected.kind,
                "date": selected.approved_date.isoformat(),
                "time": selected.approved_time.strftime("%H:%M")
                if selected.approved_time
                else None,
                "remaining_days": remaining,
                "file_number": detail.doc_number if detail else None,
                "doc_number": detail.doc_number if detail else None,
            }
        )
    entries.sort(
        key=lambda item: (item["date"], item["time"] is None, item["time"] or "", item["id"])
    )
    summary = {
        str(limit): sum(1 for item in entries if item["remaining_days"] <= limit)
        for limit in (7, 14, 30)
    }
    entries = [item for item in entries if item["remaining_days"] <= days]
    by_province: dict[str, int] = {}
    by_dept: dict[str, int] = {}
    for item in entries:
        by_province[item["province"]] = by_province.get(item["province"], 0) + 1
        by_dept[_dept_bucket(item["dept"])] = by_dept.get(_dept_bucket(item["dept"]), 0) + 1
    start = (page - 1) * size
    return {
        "days": days,
        "summary": summary,
        "by_province": by_province,
        "by_dept": by_dept,
        "province_counts": by_province,
        "dept_counts": by_dept,
        "total": len(entries),
        "page": page,
        "size": size,
        "items": entries[start : start + size],
    }


def _report_response(
    session: Session, days: int, province: str, dept: str, support: str, page: int, size: int
) -> dict[str, Any]:
    """截止雷达接口的返回：雷达数据，加上空状态说明用的已核对条数（不随筛选变化，只给数字）。"""
    payload = _report_payload(session, days, province, dept, support, page, size)
    payload["verified_total"] = verified_total(session)
    return payload


@router.get("/reports/deadlines")
def deadline_report(
    session: SessionDep,
    days: Annotated[int, Query(description="未来 7、14 或 30 天")] = 7,
    province: Annotated[str, Query(max_length=32)] = "",
    dept: Annotated[str, Query(max_length=128)] = "",
    support: Annotated[str, Query(max_length=16)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if days not in (7, 14, 30):
        raise HTTPException(status_code=422, detail="days 只支持 7、14、30")
    province = province.strip()
    dept = dept.strip()
    if province and province not in (set(PROVINCES) - {"不限地区"}):
        raise HTTPException(status_code=422, detail="省份不支持")
    if dept and dept not in DEPTS:
        raise HTTPException(status_code=422, detail="部门不支持")
    if support and support not in SUPPORT_LABELS:
        raise HTTPException(status_code=422, detail="支持方式不支持")
    if support or session.scalar(
        select(DocumentDeadline.id)
        .where(
            DocumentDeadline.status == "approved",
            DocumentDeadline.approved_date == now().date(),
            DocumentDeadline.approved_time.is_not(None),
        )
        .limit(1)
    ):
        # 当天带时刻的截止随时间变化；支持方式依赖正文指纹，缓存命中会绕过即时 stale 检查。
        return _report_response(session, days, province, dept, support, page, size)
    # 管理服务和公开服务可能是不同进程；用数据库历史版本让缓存即时失效。
    revision = publication_revision(session, "deadlines")
    key = (days, province, dept, support, page, size, now().astimezone(_TZ).date(), revision)
    cached = _REPORT_CACHE.get(key)
    current = _time.monotonic()
    if cached and current - cached[0] < REPORT_CACHE_TTL:
        return cached[1]
    payload = _report_response(session, days, province, dept, support, page, size)
    if len(_REPORT_CACHE) >= REPORT_CACHE_MAX:
        _REPORT_CACHE.pop(next(iter(_REPORT_CACHE)))
    _REPORT_CACHE[key] = (current, payload)
    return payload


def _list_board_stats_item(field: str, key: Any, lists: int, entries: int) -> dict[str, Any]:
    """统计项保留分组字段、名单份数和已解析的企业条数。"""
    return {
        field: key,
        "lists": lists,
        "entries": entries,
    }


def _list_board_payload(
    session: Session,
    days: int,
    year: int | None,
    province: str,
    list_type: str,
) -> dict[str, Any]:
    """生成筛选后的完整名单和统计，调用方负责分页。"""
    rows = session.execute(_base().where(_kind_is(["名单"]))).all()
    if not rows:
        return {"records": [], "stats": {"by_year": [], "by_province": [], "by_type": []}}

    masters = [doc.id for doc, _ in rows]
    members_by_master = groups_for(session, masters)
    owner = {member: master for master, members in members_by_master.items() for member in members}
    member_ids = list(owner)
    details = _details(session, masters)
    # 和查名单完全同一公开来源条件：只取一级来源，或与一级来源归并的同一通知。
    entry_rows = session.execute(
        select(ListEntry.document_id, ListEntry.normalized_name)
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ListEntry.document_id.in_(member_ids),
            public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
        )
    ).all()
    names_by_master: dict[int, set[str]] = {master: set() for master in masters}
    parsed_masters: set[int] = set()
    for document_id, normalized_name in entry_rows:
        master = owner.get(document_id)
        if master is not None:
            parsed_masters.add(master)
            if normalized_name:
                names_by_master[master].add(normalized_name)

    records: list[dict[str, Any]] = []
    for doc, src in rows:
        detail = details.get(doc.id)
        item_type = _list_board_type(doc.title)
        item_province = "国家部委" if src.province == "国家" else src.province
        item_year = doc.publish_date.year if doc.publish_date else None
        if year is not None and item_year != year:
            continue
        if province and item_province != province:
            continue
        if list_type and item_type != list_type:
            continue
        wording = _list_board_wording(doc.title)
        count = len(names_by_master[doc.id]) if doc.id in parsed_masters else None
        records.append(
            {
                "id": str(doc.id),
                "title": doc.title,
                "date": doc.publish_date.isoformat() if doc.publish_date else None,
                "wording": wording,
                "issuer": detail.issuer if detail and detail.issuer else src.dept_full_name,
                "province": item_province,
                "year": item_year,
                "type": item_type,
                "count": count,
            }
        )
    records.sort(
        key=lambda item: (item["date"] is None, item["date"] or "", item["id"]), reverse=True
    )

    def stat_rows(field: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[int]] = {}
        for record in records:
            value = record[field]
            if value is None:
                continue
            key = str(value)
            current = grouped.setdefault(key, [0, 0])
            current[0] += 1
            if record["count"] is not None:
                current[1] += record["count"]
        result = [
            _list_board_stats_item(
                field, int(key) if field == "year" else key, values[0], values[1]
            )
            for key, values in grouped.items()
        ]
        if field == "type":
            result.sort(key=lambda item: LIST_BOARD_TYPES.index(item["type"]))
        elif field == "year":
            result.sort(key=lambda item: int(item["year"]), reverse=True)
        else:
            result.sort(key=lambda item: item["province"])
        return result

    today = now().astimezone(_TZ).date()
    start_date = today - timedelta(days=days - 1)
    recent = [
        item
        for item in records
        if item["date"] is not None and start_date.isoformat() <= item["date"] <= today.isoformat()
    ]
    stats = {
        "by_year": stat_rows("year"),
        "by_province": stat_rows("province"),
        "by_type": stat_rows("type"),
    }
    return {"records": recent, "stats": stats}


@router.get("/reports/lists")
def list_board_report(
    session: SessionDep,
    days: Annotated[int, Query(description="最近 30 或 90 天")] = 30,
    year: Annotated[str, Query(max_length=4)] = "",
    province: Annotated[str, Query(max_length=32)] = "",
    type: Annotated[str, Query(max_length=32)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """名单看板：公示和公布都照录标题，不推断名单是否最终入选。"""
    if days not in (30, 90):
        raise HTTPException(status_code=422, detail="days 只支持 30、90")
    year_value: int | None = None
    year_text = year.strip()
    if year_text:
        if not year_text.isdigit() or not 1900 <= int(year_text) <= 2100:
            raise HTTPException(status_code=422, detail="年度不支持")
        year_value = int(year_text)
    province = province.strip()
    list_type = type.strip()
    if province and province not in (set(PROVINCES) - {"不限地区"}):
        raise HTTPException(status_code=422, detail="省份不支持")
    if list_type and list_type not in LIST_BOARD_TYPE_SET:
        raise HTTPException(status_code=422, detail="名单类型不支持")
    revision = publication_revision(session, "lists")
    key = (days, year_value, province, list_type, now().astimezone(_TZ).date(), revision)
    cached = _REPORT_CACHE.get(key)
    current = _time.monotonic()
    if cached and current - cached[0] < REPORT_CACHE_TTL:
        result = cached[1]
    else:
        result = _list_board_payload(session, days, year_value, province, list_type)
        if len(_REPORT_CACHE) >= REPORT_CACHE_MAX:
            _REPORT_CACHE.pop(next(iter(_REPORT_CACHE)))
        _REPORT_CACHE[key] = (current, result)
    records = result["records"]
    start = (page - 1) * size
    return {
        "days": days,
        "year": year_value,
        "province": province,
        "type": list_type,
        "recent": {
            "total": len(records),
            "page": page,
            "size": size,
            "items": records[start : start + size],
        },
        "stats": result["stats"],
    }


def _fmt_deadline_note(d: DocumentDeadline) -> str:
    day = d.approved_date
    time = f" {_hhmm(d.approved_time)}" if d.approved_time else ""
    return f"{d.kind} {day.month}月{day.day}日 {_WEEK[day.weekday()]}{time}"


# 详情页的正文（D-030）：从原网页或 PDF 自动提取的文字，未经人工逐字核对，只供阅读；截止日期等
# 关键字段仍只看"要点"里核对过的。只给申报通知和政策：名单、其他（招聘、职称评审等公示）里
# 可能有个人姓名。入库时已隐去个人信息，给出前按现行规则再隐去一遍，再换成"见原文"的说法，
# 去掉网页上几处固定的按钮文字（只按原样整串替换，不做猜测性的清理）。最多给前 2 万字（离
# callContainer 单次返回 1000 KiB 的上限很远）；不到 200 字的多半没抓全，不给。
BODY_MIN_CHARS = 200
BODY_MAX_CHARS = 20_000
_BODY_REPLACE = (
    ("[电话已脱敏]", "（电话见原文）"),
    ("[手机号已脱敏]", "（电话见原文）"),
    ("[姓名已脱敏]", "（联系人见原文）"),
    ("[邮箱已脱敏]", "（邮箱见原文）"),
    ("[身份证号已脱敏]", "（已隐去）"),
    ("[银行账号已脱敏]", "（已隐去）"),
    ("【关闭窗口】", ""),
    ("【打印本页】", ""),
    ("【字体：大 中 小】", ""),
    ("扫一扫在手机打开当前页", ""),
)
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
BODY_KINDS = ("申报通知", "政策")


def body_for_reading(text: str | None, kind: str) -> dict:
    """详情页正文：ok 给文字；名单类给 list，其他类给 original（都可能有个人姓名），
    没抓到完整正文给 none——这三种页面上提示复制原文链接查看。"""
    if kind == "名单":
        return {"status": "list"}
    if kind not in BODY_KINDS:
        return {"status": "original"}
    cleaned = redact_sensitive((text or "").replace("\r\n", "\n").replace("\r", "\n"))
    for old, new in _BODY_REPLACE:
        cleaned = cleaned.replace(old, new)
    cleaned = _BLANK_LINES.sub("\n\n", cleaned).strip()
    if len(cleaned) < BODY_MIN_CHARS:
        return {"status": "none"}
    return {
        "status": "ok",
        "text": cleaned[:BODY_MAX_CHARS],
        "total": len(cleaned),
        "truncated": len(cleaned) > BODY_MAX_CHARS,
    }


def _reading_body(session: Session, doc_id: int) -> str | None:
    """详情页用哪份正文：只从一级来源的文件里取；这份文件自己有就用自己的，没有再用同一通知
    其他网址里最长的那份。"""
    rows = session.execute(
        select(DocumentDetail.document_id, DocumentDetail.body_text)
        .join(Document, Document.id == DocumentDetail.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.document_id.in_(group_ids(session, doc_id)),
            DocumentDetail.status == "ok",
            Source.rights_level == 1,
            available_condition(),
        )
    ).all()
    bodies = {row.document_id: row.body_text for row in rows if row.body_text}
    return bodies.get(doc_id) or max(bodies.values(), key=len, default=None)


@router.get("/documents/{doc_id}")
def document(doc_id: int, session: SessionDep) -> dict:
    """文件详情：要点、附件、原文出处、正文（供阅读）；截止日期只给核对通过的。"""
    if is_taken_down(session, doc_id):
        raise HTTPException(status_code=404, detail="该文件已下架")
    doc_id = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
    row = session.execute(_base().where(Document.id == doc_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份文件")
    doc, src = row
    detail = _details(session, [doc.id]).get(doc.id)
    approved = _approved(session, [doc.id]).get(doc.id, [])
    pending = not approved and doc.id in _pending(session, [doc.id])
    kind = effective_kind(doc.title, doc.kind_override)
    fields = _approved_fields(session, [doc.id]).get(doc.id, {}) if kind == "申报通知" else {}
    status_value = application_status(approved) if kind == "申报通知" else None
    issuer = detail.issuer if detail and detail.issuer else src.dept_full_name
    points = [["发文机关", issuer]]
    if detail and detail.doc_number:
        points.append(["文号", detail.doc_number])
    points.append(["适用地区", "全国" if src.province == "国家" else src.province])
    if status_value:
        status_label = {"open": "征集中", "closing": "即将截止", "closed": "已截止"}[
            status_value["code"]
        ]
        when = (
            f"{status_value['date'][5:7].lstrip('0')}月{status_value['date'][8:10].lstrip('0')}日"
        )
        points.append(["申报状态", f"{status_label} · 按{status_value['label']} {when}"])

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

    # 已变更的截止（延期、调整、撤销）：不再当作有效截止，写明原因并链接到新通知
    # 看整个归并组（同一通知的不同网址）；相同的变更只列一条，新通知也换成它的主文件
    changed = (
        session.execute(
            select(DocumentDeadline)
            .where(
                DocumentDeadline.document_id.in_(group_ids(session, doc.id)),
                DocumentDeadline.status == "changed",
            )
            .order_by(DocumentDeadline.approved_date, DocumentDeadline.id)
        )
        .scalars()
        .all()
    )
    new_masters = canonical_ids(session, [d.changed_to_document_id for d in changed])
    deadline_changes: list[dict] = []
    seen_changes: set[tuple] = set()
    for d in changed:
        key = (d.kind, d.approved_date, d.approved_time)
        if d.approved_date is None or key in seen_changes:
            continue
        seen_changes.add(key)
        new_id = new_masters.get(d.changed_to_document_id, d.changed_to_document_id)
        if new_id and is_taken_down(session, new_id):
            new_id = None
        new_doc = session.get(Document, new_id) if new_id else None
        deadline_changes.append(
            {
                "label": d.kind,
                "date": d.approved_date.isoformat(),
                "time": _hhmm(d.approved_time),
                "reason": d.change_reason,
                "newDocId": str(new_id) if new_id else None,
                "newTitle": new_doc.title if new_doc else None,
            }
        )

    payload = {
        "id": str(doc.id),
        "kind": kind,
        "tags": [kind],
        # 项目类别：按标题归类，仅供检索参考，非官方分类；只有申报通知和名单有
        "category": shown_category(kind, doc.rule_category, doc.title),
        "region": region_label(src.province, src.level),
        "title": doc.title,
        "issuer": issuer,
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "status": list_status(doc.title, kind),
        "app_status": status_value,
        "fields": fields,
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
        "deadline_state": deadline_state(approved, pending),
        "deadline_changes": deadline_changes,
        "points": points,
        "excerpt": excerpt,
        # 附件：存有副本的可以在小程序里打开（preview），其他的给原因（youju/attachments.py）
        "attachments": attachment_items(
            session,
            detail.attachments if detail else [],
            kind=kind,
            recent=is_recent(doc.publish_date, doc.first_seen_at, now()),
        ),
        "checked": checked,
        "body": body_for_reading(_reading_body(session, doc.id), kind),
    }
    if fields:
        payload["fields"] = fields
    else:
        payload.pop("fields", None)
    return payload


@router.get("/calendar")
def calendar(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=366)] = 366,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> dict:
    """申报日历：核对通过的未截止事项及近 30 天已截止事项，分别分页和统计。

    每条的 id 是截止记录编号（一份文件可以有多个截止）；打开详情用 docId。
    """
    current = now()
    d = current.date()
    stmt = (
        select(DocumentDeadline, Document, Source)
        .join(Document, DocumentDeadline.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(
            DocumentDeadline.status == "approved",
            public_condition(),
            DocumentDeadline.approved_date >= d - timedelta(days=30),
            DocumentDeadline.approved_date <= d + timedelta(days=days),
        )
    )
    rows = session.execute(
        stmt.order_by(
            DocumentDeadline.approved_date,
            # 没写具体时刻的排在当天最后（MySQL 升序默认把空值排在最前）
            DocumentDeadline.approved_time.is_(None),
            DocumentDeadline.approved_time,
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
    # 与详情一样抑制归并组中已变更的截止，不能让旧副本进入日历。
    approved = _approved(session, [key[0] for key in unique])
    valid = {
        (master, dl.kind, dl.approved_date, dl.approved_time)
        for master, deadlines in approved.items()
        for dl in deadlines
    }
    upcoming, past = [], []
    for key, row in unique.items():
        if key not in valid:
            continue
        (upcoming if deadline_open(row[0], current) else past).append(row)
    past.reverse()

    def items(rows):
        return [
            {
                "id": f"dl{dl.id}",
                "docId": str(doc.id),
                "due": dl.approved_date.isoformat(),
                "time": _hhmm(dl.approved_time),
                "region": f"{'国家' if src.province == '国家' else src.province} · {src.dept}",
                "kind": effective_kind(doc.title, doc.kind_override),
                "title": doc.title,
                "what": dl.kind,
                "hasDetail": True,
                "verified": True,
            }
            for dl, doc, src in rows[offset : offset + limit]
        ]

    return {
        "today": d.isoformat(),
        "total": len(upcoming),
        "items": items(upcoming),
        "pastTotal": len(past),
        "past": items(past),
        # 空状态说明用：已核对截止的文件份数（与截止雷达的 verified_total 同一口径，只给数字）
        "verifiedTotal": verified_total(session),
    }


@router.get("/lists/recent")
def recent_lists(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> dict:
    """最近发布的名单与公示，以及本周份数。"""
    stmt = _base().where(_kind_is(["名单"]))
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
    lookup = lookup_list_matches(
        session, query, scan_limit=_LOOKUP_SCAN, result_limit=_LOOKUP_LIMIT
    )
    masters = lookup.master_ids
    best = {match.master_id: match for match in lookup.matches}
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
                available_condition(),
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
        .where(Source.rights_level == 1, available_condition())
    ).one()
    return {
        "query": q.strip(),
        "items": items,
        "similar": similar,
        "ambiguous": lookup.ambiguous,
        "truncated": lookup.truncated,
        "coverage": {"lists": lists_total, "since": since.isoformat() if since else None},
    }
