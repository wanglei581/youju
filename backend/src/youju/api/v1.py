"""小程序用的公开接口（/api/v1）：首页、搜索筛选、文件详情、申报日历、报告、最近名单、查名单。

返回格式与小程序 mock/data.js 一致。截止日期只返回人工核对通过的（approved）；待核对的候选
只给"有没有"（deadline_state），不给日期；
截止日期等关键字段只给核对过的；详情页另给正文供阅读（D-030，见 body_for_reading），名单类挡掉
名单内容后给（D-092）。
"""

from __future__ import annotations

import logging
import re
import time as _time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import Select, and_, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from youju import attachment_text, deadline_excerpts
from youju import fields as field_rules
from youju.ai import eligibility
from youju.attachments import attachment_items, is_recent
from youju.categories import CATEGORIES, CATEGORY_KINDS, category_filter, shown_category
from youju.classify import (
    GUIDE,
    KINDS,
    TITLE_EVALUATIONS,
    applicable_region,
    effective_kind,
    kind_filter,
    list_status,
    region_label,
    status_filter,
)
from youju.conditions import public_conditions
from youju.crawler.detail import _name_like, redact_sensitive
from youju.crawler.urls import public_url
from youju.db import get_session
from youju.dedup import canonical_ids, group_ids, groups_for, visible_documents
from youju.fields import SUPPORT_LABELS
from youju.issuer import normalize_issuer
from youju.lists import body as list_body
from youju.lists import evidence as list_evidence
from youju.lists.lookup import (
    ListLookupResult,
    list_title_allowed,
    lookup_list_matches,
    lookup_name_candidates,
    public_condition,
)
from youju.lists.rules import candidate_key, is_person_query, normalize_name
from youju.models import (
    Document,
    DocumentAttachment,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    DocumentMerge,
    JobRun,
    ListEntry,
    ListEntryLocation,
    ListEvidenceSource,
    ListParse,
    RepostGroup,
    RepostGroupMember,
    Source,
)
from youju.regions import (
    checked_area_name,
    covered_city_scopes,
    covered_district_scopes,
    resolve_city,
    resolve_district,
)
from youju.report_revision import list_count_revision, publication_revision
from youju.repost import confirmed_members, grouped_documents, labels_for
from youju.takedown import available_condition, cache_revision, is_taken_down
from youju.title_tags import INDUSTRIES, INDUSTRY_RULES, SUPPORT_KINDS, SUPPORT_MODES, SUPPORT_RULES

router = APIRouter(prefix="/api/v1", tags=["小程序"])
log = logging.getLogger("youju.api.v1")


def _request_session(session: Annotated[Session, Depends(get_session)]) -> Session:
    # 每个请求先看规则列算完没有（60 秒查一次），后面的类型筛选据此决定带不带标题规则兜底
    note_rules_complete(session)
    return session


SessionDep = Annotated[Session, Depends(_request_session)]

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
TIMES = {"近 7 天": 7, "近 30 天": 30, "近 90 天": 90, "今年": None, "不限": None}
DEPTS = {"科技部门", "工信部门", "其他部门"}
LIST_BOARD_TYPES = ("高新技术企业", "专精特新", "科技型中小企业", "其他")
LIST_BOARD_TYPE_SET = set(LIST_BOARD_TYPES)
PROVINCES = {
    "国家部委", "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "不限地区",
}  # fmt: skip
# 对外层级名称与来源登记中的“国家级”区分。
LEVELS = {"国家部委", "省级", "市级", "区县级"}
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


def _approved(
    session: Session, doc_ids: list[int], members: dict[int, list[int]] | None = None
) -> dict[int, list[DocumentDeadline]]:
    """每份文件核对通过的截止日期，按显示优先级排好。"""
    if not doc_ids:
        return {}
    members = members if members is not None else groups_for(session, doc_ids)
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


def _detail_meta(
    session: Session, doc_ids: list[int], members: dict[int, list[int]] | None = None
) -> dict[int, tuple[str | None, str | None]]:
    """列表汇总只需发文机关、文号；不再次传输完整正文。"""
    if not doc_ids:
        return {}
    members = members if members is not None else groups_for(session, doc_ids)
    rows = {
        doc_id: (issuer, number, length or 0)
        for doc_id, issuer, number, length in session.execute(
            select(
                DocumentDetail.document_id,
                DocumentDetail.issuer,
                DocumentDetail.doc_number,
                func.char_length(DocumentDetail.body_text),
            ).where(DocumentDetail.document_id.in_([i for ids in members.values() for i in ids]))
        )
    }
    return {
        doc_id: (best[1], best[2])
        for doc_id, ids in members.items()
        if (
            best := max(
                ((member, *rows[member]) for member in ids if member in rows),
                key=lambda row: (bool(row[3]), row[3], row[0] == doc_id),
                default=None,
            )
        )
        is not None
    }


def _hhmm(value: Any) -> str:
    return value.strftime("%H:%M") if value else ""


def _deadline_brief(d: DocumentDeadline | None) -> dict[str, Any] | None:
    if d is None or d.approved_date is None:
        return None
    return {"date": d.approved_date.isoformat(), "time": _hhmm(d.approved_time), "verified": True}


def link_gone_date(gone: datetime | None) -> str | None:
    """原网页确认失效的日期（北京时间）；能打开或还没确认的为 None（youju/link_check.py）。"""
    if gone is None:
        return None
    if gone.tzinfo is None:
        gone = gone.replace(tzinfo=UTC)
    return gone.astimezone(_TZ).date().isoformat()


def _link_gone(doc: Document) -> str | None:
    return link_gone_date(doc.link_gone_at)


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
        "region": region_label(src.province, src.level, src.city, src.district),
        "province": src.province,
        "city": src.city,
        "district": src.district,
        "level": src.level,
        "title": doc.title,
        "issuer": normalize_issuer(
            detail.issuer if detail and detail.issuer else src.dept_full_name
        ),
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "url": public_url(doc.url),
        "link_gone": _link_gone(doc),
        "deadline": _deadline_brief(deadlines[0] if deadlines else None),
        "deadline_state": deadline_state(deadlines, pending),
    }
    return payload


def _with_ai(session: Session, *groups: list[dict], key: str = "id") -> None:
    """列表行加上 ai（D-075）：这份能不能做 AI 解读、不能的原因代码，从 ai_eligibility 一次查出。

    还没算过的给 None，小程序就不显示按钮。能不能解读是文件本身的属性，和 AI 开没开无关；
    这个人能不能用 AI 由小程序另问 api。"""
    rows = [item for group in groups for item in group]
    flags = eligibility.flags(session, [int(item[key]) for item in rows if item.get(key)])
    for item in rows:
        item["ai"] = flags.get(int(item[key])) if item.get(key) else None


def _shown_with_ai(session: Session, items: list[dict], key: str = "id") -> list[dict]:
    """截止报表、日历这一页要返回的行加上 ai，返回同一个列表。"""
    _with_ai(session, items, key=key)
    return items


def _items(
    session: Session,
    rows: list[tuple[Document, Source]],
    grouped: bool = False,
    excerpts: bool = False,
) -> list[dict]:
    """批量取详情、截止日期和待核对标记（各一次查询），不逐条查库。

    grouped（group=1，D-055）：主条目带上所在转发组的其他版本（group），旧版小程序不传、不给。"""
    ids = [doc.id for doc, _ in rows]
    details, approved = _details(session, ids), _approved(session, ids)
    # 已有核对通过截止的不必再查候选
    pending = _pending(session, [doc_id for doc_id in ids if not approved.get(doc_id)])
    items = [
        _item(doc, src, details.get(doc.id), approved.get(doc.id, []), doc.id in pending)
        for doc, src in rows
    ]
    if excerpts:
        views, soon, _ = deadline_excerpts.batch(session, ids, approved, now().date())
        for item in items:
            doc_id = int(item["id"])
            due = deadline_excerpts.selected(views.get(doc_id, []), now().date())
            item["due"] = due.public(now().date()) if due else None
            item["dueSoonUnverified"] = soon.get(doc_id, False)
    if grouped:
        versions = repost_versions(session, ids)
        for item in items:
            info = versions.get(int(item["id"]))
            if info and info["others"]:
                others = info["others"]
                item["group"] = {
                    "id": info["group_id"],
                    "label": info["label"],
                    "versions": len(others),
                    # 其他版本的地区（同省两个部门的只写一次），小程序最多写 3 个，多了写“等”
                    "regions": list(dict.fromkeys(o["region"] for o in others)),
                    "items": [
                        {k: o[k] for k in ("id", "label", "region", "issuer", "date", "url")}
                        for o in others
                    ],
                }
    _with_ai(session, items)
    return items


# ---- 转发组（D-055）：同一份部里通知的各地版本 ---------------------------------------------
# 只有在管理台确认过的组员（repost_group_members.confirmed_at 有值）才对外按组折叠；没确认的、
# 人工移出的都按单条显示。截止日期不随组合并：每个版本只显示它自己核对通过的截止，按版本标明。


_grouped = grouped_documents


def _province_name(province: str) -> str:
    return "国家部委" if province == _NATIONAL else province


def repost_versions(session: Session, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
    """这些文件所在的已确认转发组：{文件: {group_id, label 这一版的叫法, upstream, others}}。

    others 是同组其他对外可见、已确认的版本（不含自己），按部里原件、发布日期排；每个版本带自己的
    原文网址。没有其他可见版本的不算组（others 为空）。"""
    if not doc_ids:
        return {}
    group_of = dict(
        session.execute(
            select(RepostGroupMember.document_id, RepostGroupMember.group_id)
            .join(RepostGroup, RepostGroup.id == RepostGroupMember.group_id)
            .where(
                RepostGroup.needs_review.is_(False),
                RepostGroupMember.document_id.in_(doc_ids),
                RepostGroupMember.active.is_(True),
                RepostGroupMember.confirmed_at.is_not(None),
            )
        ).all()
    )
    if not group_of:
        return {}
    rows = session.execute(
        _base()
        .add_columns(RepostGroupMember.group_id)
        .join(RepostGroupMember, RepostGroupMember.document_id == Document.id)
        .join(RepostGroup, RepostGroup.id == RepostGroupMember.group_id)
        .where(
            RepostGroup.needs_review.is_(False),
            RepostGroupMember.group_id.in_(set(group_of.values())),
            RepostGroupMember.active.is_(True),
            RepostGroupMember.confirmed_at.is_not(None),
        )
    ).all()
    by_group: dict[int, list[tuple[Document, Source]]] = {}
    for doc, src, group_id in rows:
        by_group.setdefault(group_id, []).append((doc, src))
    groups = {
        g.id: g
        for g in session.scalars(select(RepostGroup).where(RepostGroup.id.in_(list(by_group))))
    }
    details = _details(session, [doc.id for doc, _, _ in rows])
    out: dict[int, dict[str, Any]] = {}
    for doc_id, group_id in group_of.items():
        members = sorted(
            by_group.get(group_id, []),
            key=lambda pair: (
                pair[1].province != _NATIONAL,
                pair[0].publish_date or date.max,
                pair[0].id,
            ),
        )
        if not any(doc.id == doc_id for doc, _ in members):
            continue  # 自己不对外可见（下架等）
        labels = labels_for({doc.id: src for doc, src in members})
        group = groups[group_id]
        out[doc_id] = {
            "group_id": group_id,
            "label": labels[doc_id],
            "upstream": (
                {"title": group.upstream_title, "number": group.upstream_number}
                if group.upstream_number
                else None
            ),
            "members": members,
            "labels": labels,
            "others": [
                {
                    "id": str(doc.id),
                    "label": labels[doc.id],
                    "region": _province_name(
                        f"{src.city}·{src.district}"
                        if src.level == "区县级" and src.district
                        else src.city
                        if src.level == "市级" and src.city
                        else src.province
                    ),
                    "issuer": normalize_issuer(
                        details[doc.id].issuer
                        if doc.id in details and details[doc.id].issuer
                        else src.dept_full_name
                    ),
                    "date": doc.publish_date.isoformat() if doc.publish_date else None,
                    "url": public_url(doc.url),
                }
                for doc, src in members
                if doc.id != doc_id
            ],
        }
    return out


# —— 规则列是不是都算完了 ——
# 类型、项目类别、支持方式、产业方向都在入库时按标题规则算好存成一列（rule_kind 等），规则改了由
# youju reclassify 重算（admin 每次启动时跑）。筛选只比较这一列；值为 NULL 的行（刚加列、还没重算）
# 才当场按标题规则判断。这段"当场判断"写成 SQL 很长："其他"一类 30 多万字符，折叠资讯与指南用的
# "不是资讯与指南"近 50 万字符，数据库光解析一条就要零点几秒，一次搜索有五六条（10/9 线上实测：
# 默认搜索 3–7 秒）。所以先看库里还有没有 NULL：没有就不带这段，结果完全一样；有才带上。
_RULE_COLUMNS = (
    Document.rule_kind,
    Document.rule_category,
    Document.rule_supports,
    Document.rule_industries,
)
_RULES_RECHECK = 60.0  # 确认算完后隔多少秒再查一次
_rules_complete = False
_rules_checked_at = 0.0


def note_rules_complete(session: Session) -> bool:
    """查一下规则列是不是都算完了（没有 NULL），记在进程里，供 _kind_is 等决定带不带标题规则兜底。

    请求一进来（SessionDep）和 search_statement 开头各调一次。确认算完后 60 秒内不再查；
    没算完时每次都查，重算一结束就生效。没调过这个函数的进程（如定时任务）照旧带兜底，只是慢一些。
    """
    global _rules_complete, _rules_checked_at
    stamp = _time.monotonic()
    if _rules_complete and stamp - _rules_checked_at < _RULES_RECHECK:
        return True
    pending = session.scalar(
        select(Document.id).where(or_(*(c.is_(None) for c in _RULE_COLUMNS))).limit(1)
    )
    _rules_complete, _rules_checked_at = pending is None, stamp
    return _rules_complete


def _kind_is(kinds: list[str]):
    """类型筛选：人工定过类型的按人工定的，没定过的按标题规则——规则结果入库时已算好
    存在 rule_kind，只比较这一列；还没算的（刚升级、还没跑 youju reclassify）当场按规则判断，
    结果一样。库里没有"还没算的"时不带当场判断那一段（见 note_rules_complete）。"""
    by_rule = Document.rule_kind.in_(kinds)
    if not _rules_complete:
        by_rule = or_(
            by_rule, and_(Document.rule_kind.is_(None), kind_filter(Document.title, kinds))
        )
    return or_(
        Document.kind_override.in_(kinds),
        and_(Document.kind_override.is_(None), by_rule),
    )


def _category_is(categories: list[str]):
    """项目类别筛选：只在申报通知和名单里找；类别入库时已算好存在 rule_category，
    还没算的（刚升级、还没跑 youju reclassify）当场按规则判断，结果一样。"""
    by_rule = Document.rule_category.in_(categories)
    if not _rules_complete:
        by_rule = or_(
            by_rule,
            and_(Document.rule_category.is_(None), category_filter(Document.title, categories)),
        )
    return and_(_kind_is(list(CATEGORY_KINDS)), by_rule)


def _supports_are(supports: list[str]):
    """支持方式筛选（按标题）：只在申报通知和名单里找；选了几项，有其中任一项就列出。"""
    return and_(
        _kind_is(list(SUPPORT_KINDS)),
        SUPPORT_RULES.filter(
            Document.rule_supports, Document.title, supports, complete=_rules_complete
        ),
    )


def _industries_are(industries: list[str]):
    """产业方向筛选（按标题）：所有类型；选了几项，有其中任一项就列出。"""
    return INDUSTRY_RULES.filter(
        Document.rule_industries, Document.title, industries, complete=_rules_complete
    )


def _folds_guides(kind: str, types: str | None) -> bool:
    """只在"全部"页签、没选类型时折叠资讯与指南；选了类型就按选的列（选了资讯与指南就照常列出）。"""
    return kind == "all" and not _pick(types, TYPES, "类型")


def fold_guides(
    session: Session,
    stmt: Select,
    kind: str,
    types: str | None,
    fold: int | None,
    group: bool = False,
) -> tuple[Select, int, int]:
    """按 fold 处理"资讯与指南"（搜索和关注的新文件共用）：返回（要列出的查询, 资讯与指南篇数,
    折叠了几篇）。fold 不传时不统计（旧版小程序）；0 照常列出、给出篇数；1 不列出、只给篇数。
    group 为真时按转发组算（返回的查询仍未折叠，由调用方折叠）：篇数是折叠资讯与指南后实际少掉的
    条数（组数）。一个转发组里同时有“资讯与指南”和“其他”的版本时，折叠后这一组仍以“其他”那版
    列出，不算折叠掉。"""
    if fold is None or not _folds_guides(kind, types):
        return stmt, 0, 0
    if group:
        guides = _count(session, _grouped(stmt)) - _count(
            session, _grouped(stmt.where(_kind_is(_NOT_GUIDE)))
        )
    else:
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
def home(
    request: Request,
    session: SessionDep,
    excerpts: Annotated[int | None, Query(ge=1, le=1)] = None,
    group: Annotated[
        int | None,
        Query(
            ge=1,
            le=1,
            description="1 按转发组折叠（D-055）：同一份部里通知的各地版本只列一条、计数按组；"
            "不传和以前一样",
        ),
    ] = None,
) -> dict:
    """首页：本周新增（按发布日期，北京时间周一起算）、分类计数、覆盖范围、最近一次成功检查的时间、
    最新 8 份。

    group=1（D-055）：本周新增按转发组算，本周有新版本的组计 1 份，week_reposts 是并进组里、
    没有另计的版本数；最新 8 份也按组折叠。"""
    start, end = _week(now().date())
    week = visible_documents(
        select(Document.id, Document.title, Document.kind_override)
        .join(Source, Document.source_id == Source.id)
        .where(public_condition())
    ).where(Document.publish_date >= start, Document.publish_date < end)
    week_rows = session.execute(week.order_by(Document.id)).all()
    week_reposts = 0
    if group == 1:
        members = confirmed_members()
        group_of = dict(
            session.execute(
                select(members.c.document_id, members.c.group_id).where(
                    members.c.document_id.in_([row.id for row in week_rows] or [-1])
                )
            ).all()
        )
        seen: set[int] = set()
        kept = []
        for row in week_rows:
            group_id = group_of.get(row.id)
            if group_id is not None and group_id in seen:
                week_reposts += 1
                continue
            if group_id is not None:
                seen.add(group_id)
            kept.append(row)
        week_rows = kept
    kinds = [effective_kind(row.title, row.kind_override) for row in week_rows]
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
    cities = (
        session.scalar(
            select(func.count()).select_from(
                select(Source.province, Source.city)
                .where(live, Source.level == "市级", Source.city.is_not(None), Source.city != "")
                .distinct()
                .subquery()
            )
        )
        or 0
    )
    # 最近一次成功检查：定时任务台账里最近一次成功结束的抓取（crawl）的结束时间；没有记录时为空。
    # 本周 0 份时小程序写"本周暂无新文件 · 最近检查 …"，只陈述检查时间，不推测原因。
    last_checked = session.scalar(
        select(func.max(JobRun.finished_at)).where(JobRun.job == "crawl", JobRun.status == "ok")
    )
    last_reviewed = session.scalar(
        select(func.max(DocumentDeadline.reviewed_at)).where(DocumentDeadline.status != "pending")
    )
    # 首页"最新"只放申报通知、名单、政策三类；统计数据发布等其他文件在搜索里能找到
    latest_stmt = _base().where(_kind_is(["申报通知", "名单", "政策"]))
    if group == 1:
        latest_stmt = _grouped(latest_stmt)
    latest = session.execute(_ordered(latest_stmt).limit(8)).all()
    stats = {
        "week_total": len(kinds),
        "apply": kinds.count("申报通知"),
        "lists": kinds.count("名单"),
        "other": len(kinds) - kinds.count("申报通知") - kinds.count("名单"),
        "provinces": provinces,
        "cities": cities,
        "sources": sources,
        # 关注条件的本周新增要登录后才能算（#30）；未登录为 null，小程序不显示那一栏
        "followed_new": None,
        "last_checked_at": (
            last_checked.astimezone(_TZ).isoformat(timespec="seconds") if last_checked else None
        ),
        # 截止核对是否断档（定稿方案第八节）：最近一次人工核对截止的时间，从没核对过为空；
        # 小程序据此在连续 3 个工作日没核对时写"今日未更新"
        "deadline_reviewed_at": (
            last_reviewed.astimezone(_TZ).isoformat(timespec="seconds") if last_reviewed else None
        ),
    }
    from youju.api import me

    try:
        user = me.known_user(request, session)
        if user is not None:
            user = me.current_user(user)
            stats.update(me.followed_week_stats(session, user))
    except HTTPException as exc:
        if exc.status_code not in (401, 403):
            raise
    if group == 1:
        stats["week_reposts"] = week_reposts
    payload = {
        "stats": stats,
        "items": _items(session, [tuple(r) for r in latest], group == 1, excerpts == 1),
    }
    if excerpts == 1:
        report = _report_payload_excerpts(session, 7, "", "", "", 1, 5)
        payload["dueSoon"] = report["items"]
        payload["dueSoonUnverifiedTotal"] = report["unverifiedSoon"]["total"]
    return payload


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
        elif enabled:
            not_covered.append({"name": name, "reason": "正在接入"})
        elif any(s.fetch_mode == "blocked" for s in mine):
            not_covered.append({"name": name, "reason": "网站不允许程序访问"})
        else:
            not_covered.append({"name": name, "reason": "暂未接入"})
    city_columns: dict[tuple[str, str], int] = {}
    for source in sources:
        if source.enabled and source.last_success_at and source.level == "市级" and source.city:
            key = (source.province, source.city)
            city_columns[key] = city_columns.get(key, 0) + 1
    cities = [
        {"province": province, "city": city, "sources": count}
        for (province, city), count in sorted(city_columns.items())
    ]
    district_columns: dict[tuple[str, str, str], int] = {}
    for source in sources:
        if (
            source.enabled
            and source.last_success_at
            and source.level == "区县级"
            and source.city
            and source.district
        ):
            key = (source.province, source.city, source.district)
            district_columns[key] = district_columns.get(key, 0) + 1
    districts = [
        {"province": p, "city": c, "district": d, "sources": count}
        for (p, c, d), count in sorted(district_columns.items())
    ]
    lasts = [r["lastUpdatedAt"] for r in regions]
    return {
        "sources": sum(r["sources"] for r in regions),
        "provinces": sum(1 for r in regions if r["name"] != "国家部委"),
        "national": any(r["name"] == "国家部委" for r in regions),
        "regions": regions,
        "cities": cities,
        "districts": districts,
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


# 搜索频率上限（S3）：同一来源地址每分钟最多 120 次，超了返回 429。只按来源地址算、不用 openid：
# 隐私保护指引一·2 写明 openid 在服务器内存里只统计导出和打开附件的次数，搜索不在里面；
# 来源地址是一·8 写明为保障运行安全处理的信息，查名单的限流也是按它算的。来源地址取平台写的
# X-Original-Forwarded-For（9/30 实测不能伪造），和访问日志一样截到 64 个字符。
# 依据：线上 10/12–10/18 同一来源一分钟最多搜了 24 次，平常 1–5 次；"我的客户"打开一次最多查
# 20 组（每家客户一组）。120 次够一个办公室几个人共用一个公网出口、各自连开几次"我的客户"；
# 脚本刷接口每秒能发几十次，限到每秒 2 次。计数只在进程内存里（不写库、不进日志），键数有上限
# （淘汰最久没搜的）。快照里取最新 8 份不经过这里。
SEARCH_PER_MINUTE = 120
_SEARCH_KEYS_MAX = 10000
_SEARCH_TIMES: OrderedDict[str, deque[float]] = OrderedDict()
_SEARCH_LOCK = Lock()


def _allow_search(address: str, now: float) -> bool:
    """这个来源地址这次搜索没超上限就记一次、返回 True；超了返回 False，不记，也不更新先后顺序
    （免得借此把别人的记录挤出去）。"""
    key = "ip:" + address
    with _SEARCH_LOCK:
        times = _SEARCH_TIMES.get(key) or deque()
        while times and times[0] <= now - 60:
            times.popleft()
        if len(times) >= SEARCH_PER_MINUTE:
            return False
        times.append(now)
        _SEARCH_TIMES[key] = times
        _SEARCH_TIMES.move_to_end(key)
        while len(_SEARCH_TIMES) > _SEARCH_KEYS_MAX:
            _SEARCH_TIMES.popitem(last=False)
        return True


def _limit_search(request: Request) -> None:
    from youju.api.app import client_ip

    if not _allow_search(client_ip(request)[:64], _time.monotonic()):
        raise HTTPException(status_code=429, detail="搜索太频繁，请稍后再试")


# 限流写在路由上（先于取数据库会话和校验参数）：快照等内部直接调 search() 的不计次
@router.get("/search", dependencies=[Depends(_limit_search)])
def search(
    session: SessionDep,
    excerpts: Annotated[int | None, Query(ge=1, le=1)] = None,
    q: Annotated[str | None, Query(max_length=50, description="标题或文号关键词")] = None,
    kind: Annotated[str, Query(description="页签：all / 申报通知 / 名单 / 政策")] = "all",
    regions: Annotated[str | None, Query(max_length=400, description="省份，逗号分隔")] = None,
    cities: Annotated[
        str | None,
        Query(max_length=2000, description="已收录城市，省/市逗号分隔，与地区取并集"),
    ] = None,
    districts: Annotated[
        str | None, Query(max_length=4000, description="省/市/区，逗号分隔")
    ] = None,
    types: Annotated[
        str | None, Query(max_length=400, description="申报通知,名单与公示,政策文件,其他")
    ] = None,
    status: Annotated[str | None, Query(max_length=400, description="公示,公布")] = None,
    time: Annotated[
        str, Query(description="近 7 天 / 近 30 天 / 近 90 天 / 今年 / 不限")
    ] = "近 30 天",
    depts: Annotated[
        str | None, Query(max_length=400, description="科技部门,工信部门,其他部门")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
    open: Annotated[int | None, Query(ge=0, le=1, description="1 只看未截止（含即将截止）")] = None,
    exclude_closed: Annotated[
        bool, Query(description="只排除人工核对且已截止的申报通知；未核对的保留；不传不改变结果")
    ] = False,
    levels: Annotated[
        str | None, Query(max_length=400, description="国家部委,省级,市级,区县级")
    ] = None,
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
    group: Annotated[
        int | None,
        Query(
            ge=1,
            le=1,
            description="1 按转发组折叠（D-055）：同一份部里通知的各地版本只列一条、计数按组；"
            "不传和以前一样",
        ),
    ] = None,
    ai: Annotated[
        int | None,
        Query(
            ge=1,
            le=1,
            description="1 只看能做 AI 解读的（D-075，按 ai_eligibility 预先算好的结果）；"
            "不传和以前一样",
        ),
    ] = None,
) -> dict:
    """搜索与筛选。total 是符合全部条件的份数，total_all 是不限发布时间的份数（都按当前页签）。

    group=1（D-055）：先按条件筛出各个版本，再把同一确认过的转发组折叠成一条主条目（见 _grouped），
    total、total_all、guides、folded 都按组算；reposts 是折叠掉、没有另计的版本数，主条目带 group
    （其他版本和各自的原文网址）。

    fold=1 时"资讯与指南"类文件不列出、不计入 total 和 total_all，只在 folded 里给出折叠了几篇
    （不悄悄藏：小程序写明"已折叠 X 篇"，点开就按 fold=0 列出来）；guides 是符合条件的
    资讯与指南篇数（折叠时等于 folded）。只在"全部"页签、没选类型时折叠。不传 fold 时
    不返回这两项。"""
    filters = {"levels": levels, "categories": categories}
    filters |= {
        "supports": supports,
        "industries": industries,
        "cities": cities,
        "districts": districts,
        "exclude_closed": exclude_closed,
    }
    effective_open = None if excerpts == 1 and open == 1 else open
    stmt = search_statement(
        session, q, kind, regions, types, status, time, depts, effective_open, **filters
    )
    all_time = search_statement(
        session, q, kind, regions, types, status, "不限", depts, effective_open, **filters
    )
    if excerpts == 1 and open == 1:
        active_ids = _open_excerpt_ids(session)
        stmt = stmt.where(Document.id.in_(active_ids))
        all_time = all_time.where(Document.id.in_(active_ids))
    if ai == 1:
        stmt = stmt.where(Document.id.in_(eligibility.eligible_ids()))
        all_time = all_time.where(Document.id.in_(eligibility.eligible_ids()))
    grouped = group == 1
    stmt, guides, folded = fold_guides(session, stmt, kind, types, fold, grouped)
    if fold == 1 and _folds_guides(kind, types):
        all_time = all_time.where(_kind_is(_NOT_GUIDE))
    reposts = 0
    if grouped:
        versions = _count(session, stmt)
        stmt, all_time = _grouped(stmt), _grouped(all_time)
    total = _count(session, stmt)
    if grouped:
        reposts = versions - total
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    open_now = _base().where(
        _kind_is(["申报通知"]), Document.id.in_(_application_ids(session, {"open", "closing"}))
    )
    body = {
        "q": (q or "").strip(),
        "total": total,
        "total_all": _count(session, all_time),
        # 截止日期已人工核对、还没截止的申报通知一共几份（不随筛选条件变）：
        # 小程序在"只看未截止"旁边如实写出来
        "verified_open_total": _count(session, _grouped(open_now) if grouped else open_now),
        "items": _items(session, [tuple(r) for r in rows], grouped, excerpts == 1),
    }
    if fold is not None:  # 旧版小程序不传 fold，返回格式和以前一样
        body |= {"folded": folded, "guides": guides}
    if grouped:  # 旧版小程序不传 group，返回格式和以前一样
        body["reposts"] = reposts
    return body


# 地区筛选的"国家部委"和层级筛选的"国家部委"共用这一个条件（来源省份填"国家"），两处不会不一致
_NATIONAL = "国家"


def _level_filter(levels: list[str]):
    """层级：国家部委 = 国家级来源；省级 = 省级来源（不含国家级、市级）。"""
    parts = {
        "国家部委": Source.province == _NATIONAL,
        "省级": and_(Source.province != _NATIONAL, Source.level == "省级"),
        "市级": and_(Source.province != _NATIONAL, Source.level == "市级"),
        "区县级": and_(Source.province != _NATIONAL, Source.level == "区县级"),
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
    cities: str | None = None,
    districts: str | None = None,
    exclude_closed: bool = False,
) -> Select:
    """公开搜索、关注和提醒共用筛选校验及 SQL。

    各组筛选之间取交集，同一组里多选取并集。地区和层级也是这样：地区选的省份（或国家部委）
    和层级选的国家部委 / 省级 / 市级同时满足才列出。"国家部委"在两组里是同一个条件（见 _NATIONAL），
    所以只在地区里选、只在层级里选、两边都选，结果相同；地区只选省份而层级只选国家部委，
    或地区只选国家部委而层级只选省级，两者没有交集，结果为空（小程序在空结果里提示调整）。
    项目类别只有申报通知和名单有：选了类别，政策、资讯与指南、其他就不再列出。支持方式（按标题）
    也一样；产业方向（按标题）所有类型都有。这三组都是同一组里多选取并集。
    """
    note_rules_complete(session)  # 关注、提醒等不经过 SessionDep 的调用也在这里查一次
    if kind not in TABS:
        raise HTTPException(status_code=422, detail=f"页签不支持：{kind}")
    if time not in TIMES:
        raise HTTPException(status_code=422, detail=f"发布时间不支持：{time}")
    picked_types = [TYPES[t] for t in _pick(types, TYPES, "类型")]
    picked_regions = [r for r in _pick(regions, PROVINCES, "地区") if r != "不限地区"]
    picked_status = _pick(status, STATUSES, "发布阶段")
    picked_depts = _pick(depts, DEPTS, "发文部门")
    picked_levels = _pick(levels, LEVELS, "层级")
    picked_categories = _pick(categories, CATEGORY_SET, "项目类别")
    picked_supports = _pick(supports, SUPPORT_SET, "支持方式")
    picked_industries = _pick(industries, INDUSTRY_SET, "产业方向")
    picked_cities = list(dict.fromkeys(c.strip() for c in (cities or "").split(",") if c.strip()))
    if picked_cities:
        # 只接受收录范围里的城市；自由文本客户名称及关键词不进入接口。
        covered = covered_city_scopes(session)
        resolved_cities = []
        for picked in picked_cities:
            province, separator, city = picked.partition("/")
            resolved = resolve_city(province, city, covered) if separator and city else None
            if resolved is None:
                raise HTTPException(status_code=422, detail="城市未收录，请重新选择地区")
            resolved_cities.append(f"{province}/{resolved}")
        picked_cities = list(dict.fromkeys(resolved_cities))

    picked_districts = []
    for picked in dict.fromkeys(c.strip() for c in (districts or "").split(",") if c.strip()):
        parts = picked.split("/")
        if len(parts) != 3 or not all(parts):
            raise HTTPException(status_code=422, detail="区县须为省/市/区完整路径")
        province, city, district = parts
        city, district = _validate_district_scope(session, province, city, district)
        picked_districts.append((province, city, district))
    if districts and districts.strip() and not picked_districts:
        raise HTTPException(status_code=422, detail="区县须为省/市/区完整路径")

    stmt = _base()
    kw = (q or "").strip()
    if kw:
        stmt = stmt.where(Document.id.in_(_keyword_ids(session, kw)))
    if kind != "all":
        stmt = stmt.where(_kind_is([kind]))
    if picked_types:
        stmt = stmt.where(_kind_is(picked_types))
    if picked_regions or picked_cities or picked_districts:
        area_parts = []
        if picked_regions:
            area_parts.append(
                Source.province.in_([_NATIONAL if r == "国家部委" else r for r in picked_regions])
            )
        area_parts.extend(
            and_(
                Source.province == c.split("/")[0],
                Source.city == c.split("/")[1],
                Source.level == "市级",
            )
            for c in picked_cities
        )
        area_parts.extend(
            and_(
                Source.province == p,
                Source.city == c,
                Source.district == d,
                Source.level == "区县级",
            )
            for p, c, d in picked_districts
        )
        stmt = stmt.where(or_(*area_parts))
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
        stmt = stmt.where(
            _kind_is(["申报通知"]), Document.id.in_(_application_ids(session, {"open", "closing"}))
        )
    if exclude_closed:
        # 只去掉可信的已截止状态；changed 会由 _approved 抑制，未核对、无日期的仍保留。
        closed_ids = _application_ids(session, {"closed"})
        if closed_ids:
            stmt = stmt.where(not_(and_(_kind_is(["申报通知"]), Document.id.in_(closed_ids))))

    d = now().date()
    if TIMES[time]:
        stmt = stmt.where(Document.publish_date >= d - timedelta(days=TIMES[time]))
    elif time == "今年":
        stmt = stmt.where(Document.publish_date >= date(d.year, 1, 1))
    return stmt


def _application_ids(session: Session, codes: set[str]) -> list[int]:
    """申报状态在 codes 里的文件编号（转发组取主版本）。

    状态只按人工核对通过的截止日期算（_approved），没核对的文件没有状态，不会出现在这里。
    只要 open、closing 时先按"今天起还没过的截止"缩小范围；要 closed 时得看全部核对通过的截止
    （现在数量很少，多了以后再考虑存一列状态）。
    """
    current = now()
    if "closed" not in codes:
        ids = _future_approved_canonical_ids(session, current.date())
    else:
        candidates = list(
            session.scalars(
                select(DocumentDeadline.document_id)
                .where(DocumentDeadline.status == "approved")
                .distinct()
            )
        )
        mapping = canonical_ids(session, candidates)
        ids = sorted({mapping.get(i, i) for i in candidates})
    approved = _approved(session, ids)
    return [
        i
        for i in ids
        if (state := application_status(approved.get(i, []), current)) and state["code"] in codes
    ]


def _excerpt_candidate_ids(session: Session, today: date | None = None) -> list[int]:
    """只扫描可能仍有效的候选：没过期的已核对截止，和 30 天内的待核对候选（30 天以后的
    机器摘录不给日期，D-061）；归并组在批量判定时处理。"""
    today = today or now().date()
    raw = list(
        session.scalars(
            select(DocumentDeadline.document_id)
            .where(
                or_(
                    and_(
                        DocumentDeadline.status == "approved",
                        DocumentDeadline.approved_date >= today,
                    ),
                    and_(
                        DocumentDeadline.status == "pending",
                        DocumentDeadline.candidate_date.between(
                            today, today + timedelta(days=deadline_excerpts.EXCERPT_LAST_DAY)
                        ),
                    ),
                )
            )
            .distinct()
        )
    )
    mapping = canonical_ids(session, raw)
    return sorted({mapping.get(doc_id, doc_id) for doc_id in raw})


# 首页、截止页和"只看未截止"共用的整批判定：{"universe": (缓存键, 取的时刻, 结果)}
_EXCERPT_CACHE: dict[str, tuple[tuple, float, Any]] = {}


def _excerpt_universe(
    session: Session,
) -> tuple[list[int], dict[int, list[deadline_excerpts.View]], dict[int, bool]]:
    """还可能有截止的全部通知（未过期的已核对或待核对候选）逐类型判定一次。

    按（北京时间日期、截止数据版本）缓存 300 秒：核对、撤回、新候选让版本变化，立刻重算；
    正文改动和雷达缓存一样由 300 秒兜底（版本不读正文）。列表和详情不用这份缓存，按当页现算。"""
    today = now().date()
    key = (today, publication_revision(session, "deadlines"))
    stamp = _time.monotonic()
    cached = _EXCERPT_CACHE.get("universe")
    if cached and cached[0] == key and stamp - cached[1] < REPORT_CACHE_TTL:
        return cached[2]
    ids = _excerpt_candidate_ids(session, today)
    members = groups_for(session, ids)
    views, soon, _ = deadline_excerpts.batch(
        session, ids, _approved(session, ids, members), today, members
    )
    result = (ids, views, soon)
    _EXCERPT_CACHE["universe"] = (key, stamp, result)
    return result


def _open_excerpt_ids(session: Session) -> list[int]:
    ids, views, soon = _excerpt_universe(session)
    kinds = {
        doc_id: effective_kind(title, override)
        for doc_id, title, override in session.execute(
            select(Document.id, Document.title, Document.kind_override).where(Document.id.in_(ids))
        )
    }
    current = now()
    return [
        doc_id
        for doc_id in ids
        if kinds.get(doc_id) == "申报通知"
        and (
            (
                (due := deadline_excerpts.selected(views.get(doc_id, []), current.date()))
                is not None
                and due.day is not None
                and (due.source == "excerpt" or _view_open(due, current))
            )
            or soon.get(doc_id, False)
        )
    ]


def _view_open(view: deadline_excerpts.View, stamp: datetime) -> bool:
    if view.day is None:
        return False
    stamp = stamp.astimezone(_TZ)
    return view.day > stamp.date() or (
        view.day == stamp.date()
        and (view.clock is None or view.clock >= stamp.time().replace(tzinfo=None))
    )


def _validate_city_scope(session: Session, province: str, city: str) -> str:
    if not city:
        return ""
    resolved = resolve_city(province, city, covered_city_scopes(session))
    if resolved is not None:
        return resolved
    raise HTTPException(status_code=422, detail="城市未收录，请重新选择地区；不会改为全省查询")


def _validate_district_scope(
    session: Session, province: str, city: str, district: str
) -> tuple[str, str]:
    try:
        city = checked_area_name(city, "城市")
        district = checked_area_name(district, "区县")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not district:
        return _validate_city_scope(session, province, city), ""
    resolved = (
        resolve_district(province, city, district, covered_district_scopes(session))
        if province and city
        else None
    )
    if resolved is None:
        raise HTTPException(
            status_code=422, detail="区县暂未收录或不属于所选省市，不会改为全市查询"
        )
    return resolved


def _city_matches(source: Source, province: str, city: str, district: str = "") -> bool:
    if district:
        return (
            source.level == "区县级"
            and source.province == province
            and source.city == city
            and source.district == district
        )
    shown = "国家部委" if source.province == "国家" else source.province
    return (not province or province == shown) and (
        not city or (source.level == "市级" and city == source.city)
    )


def _report_payload(
    session: Session,
    days: int,
    province: str,
    dept: str,
    support: str,
    page: int,
    size: int,
    city: str = "",
    district: str = "",
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
        if not _city_matches(src, province, city, district):
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
                "issuer": normalize_issuer(
                    detail.issuer if detail and detail.issuer else src.dept_full_name
                ),
                "province": "国家部委" if src.province == "国家" else src.province,
                "city": src.city,
                "district": src.district,
                "level": src.level,
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
        "items": _shown_with_ai(session, entries[start : start + size]),
    }


def _report_payload_excerpts(
    session: Session,
    days: int,
    province: str,
    dept: str,
    support: str,
    page: int,
    size: int,
    city: str = "",
    district: str = "",
) -> dict[str, Any]:
    current = now()
    today = current.date()
    universe, views, soon = _excerpt_universe(session)
    stmt = _base().where(Document.id.in_(universe))
    if support:
        stmt = stmt.where(_support_condition(support))
    rows = session.execute(stmt).all()
    ids = [doc.id for doc, _ in rows]
    details = _detail_meta(session, ids)
    entries = []
    unverified = []
    for doc, src in rows:
        if effective_kind(doc.title, doc.kind_override) != "申报通知":
            continue
        shown_province = "国家部委" if src.province == "国家" else src.province
        if not _city_matches(src, province, city, district):
            continue
        if dept and _dept_bucket(src.dept) != dept:
            continue
        detail = details.get(doc.id)
        issuer = normalize_issuer(detail[0] if detail and detail[0] else src.dept_full_name)
        due = deadline_excerpts.selected(views.get(doc.id, []), today)
        verified_soon = (
            due is not None
            and due.source == "verified"
            and due.day is not None
            and (due.day - today).days <= 7
            and _view_open(due, current)
        )
        if soon.get(doc.id, False) and not verified_soon:
            soon_kind = next(
                (view.kind for view in views.get(doc.id, []) if view.source == "unverified"),
                "截止未核对",
            )
            unverified.append(
                {
                    "id": str(doc.id),
                    "title": doc.title,
                    "issuer": issuer,
                    "province": shown_province,
                    "city": src.city,
                    "district": src.district,
                    "level": src.level,
                    "kind": soon_kind,
                }
            )
        if due is None or due.day is None or not _view_open(due, current):
            continue
        remaining = (due.day - today).days
        if remaining > 30:
            continue
        entries.append(
            {
                "id": str(doc.id),
                "title": doc.title,
                "issuer": issuer,
                "province": shown_province,
                "city": src.city,
                "district": src.district,
                "level": src.level,
                "dept": src.dept_full_name,
                "label": due.kind,
                "deadline_type": due.kind,
                "date": due.day.isoformat(),
                "time": due.clock.strftime("%H:%M") if due.clock else None,
                "remaining_days": remaining,
                "file_number": detail[1] if detail else None,
                "doc_number": detail[1] if detail else None,
                "source": due.source,
                "quote": due.quote,
                "due": due.public(today),
                "dueSoonUnverified": soon.get(doc.id, False),
            }
        )
    entries.sort(
        key=lambda item: (item["date"], item["time"] is None, item["time"] or "", item["id"])
    )
    summary_verified = {
        str(limit): sum(
            item["source"] == "verified" and item["remaining_days"] <= limit for item in entries
        )
        for limit in (7, 14, 30)
    }
    summary = {
        str(limit): sum(item["remaining_days"] <= limit for item in entries)
        for limit in (7, 14, 30)
    }
    shown = [item for item in entries if item["remaining_days"] <= days]
    by_province: dict[str, int] = {}
    by_dept: dict[str, int] = {}
    for item in shown:
        by_province[item["province"]] = by_province.get(item["province"], 0) + 1
        bucket = _dept_bucket(item["dept"])
        by_dept[bucket] = by_dept.get(bucket, 0) + 1
    start = (page - 1) * size
    return {
        "days": days,
        "summary": summary,
        "summaryVerified": summary_verified,
        "unverifiedSoon": {
            "total": len(unverified),
            "items": _shown_with_ai(session, unverified[:50]),
        },
        "by_province": by_province,
        "by_dept": by_dept,
        "province_counts": by_province,
        "dept_counts": by_dept,
        "total": len(shown),
        "page": page,
        "size": size,
        "items": _shown_with_ai(session, shown[start : start + size]),
    }


def _report_response(
    session: Session,
    days: int,
    province: str,
    dept: str,
    support: str,
    page: int,
    size: int,
    city: str = "",
    district: str = "",
) -> dict[str, Any]:
    """截止雷达接口的返回：雷达数据，加上空状态说明用的已核对条数（不随筛选变化，只给数字）。"""
    payload = _report_payload(session, days, province, dept, support, page, size, city, district)
    payload["verified_total"] = verified_total(session)
    return payload


@router.get("/reports/deadlines")
def deadline_report(
    session: SessionDep,
    excerpts: Annotated[int | None, Query(ge=1, le=1)] = None,
    days: Annotated[int, Query(description="未来 7、14 或 30 天")] = 7,
    province: Annotated[str, Query(max_length=32)] = "",
    city: Annotated[str, Query(max_length=32)] = "",
    district: Annotated[str, Query(max_length=32)] = "",
    dept: Annotated[str, Query(max_length=128)] = "",
    support: Annotated[str, Query(max_length=16)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if days not in (7, 14, 30):
        raise HTTPException(status_code=422, detail="days 只支持 7、14、30")
    province = province.strip()
    city = city.strip()
    city, district = _validate_district_scope(session, province, city, district)
    dept = dept.strip()
    if province and province not in (set(PROVINCES) - {"不限地区"}):
        raise HTTPException(status_code=422, detail="省份不支持")
    if dept and dept not in DEPTS:
        raise HTTPException(status_code=422, detail="部门不支持")
    if support and support not in SUPPORT_LABELS:
        raise HTTPException(status_code=422, detail="支持方式不支持")
    if excerpts == 1:
        payload = _report_payload_excerpts(
            session, days, province, dept, support, page, size, city, district
        )
        payload["verified_total"] = verified_total(session)
        return payload
    if (
        city
        or support
        or session.scalar(
            select(DocumentDeadline.id)
            .where(
                DocumentDeadline.status == "approved",
                DocumentDeadline.approved_date == now().date(),
                DocumentDeadline.approved_time.is_not(None),
            )
            .limit(1)
        )
    ):
        # 当天带时刻的截止随时间变化；支持方式依赖正文指纹，缓存命中会绕过即时 stale 检查。
        return _report_response(session, days, province, dept, support, page, size, city, district)
    # 管理服务和公开服务可能是不同进程；用数据库历史版本让缓存即时失效。
    revision = publication_revision(session, "deadlines")
    key = (days, province, dept, support, page, size, now().astimezone(_TZ).date(), revision)
    cached = _REPORT_CACHE.get(key)
    current = _time.monotonic()
    if cached and current - cached[0] < REPORT_CACHE_TTL:
        return cached[1]
    payload = _report_response(session, days, province, dept, support, page, size, city, district)
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


# 名单看板每份名单的企业数（S2，10/19）：以前每次都把全部名单条目（线上约 27.8 万行）读进程序
# 去重，名单没变也每 5 分钟整批重算（快照在 admin 上缓存失效时要 3.9–4.7 秒）。现在在数据库里
# 按归并组去重计数，结果按它用到的全部数据的版本缓存（publication_revision 的名单部分加
# list_count_revision），版本不变就不重算；另设 1 小时兜底，万一有版本没覆盖到的改动（如直接改库
# 改企业名），最晚 1 小时后也会算进去。值为 (版本, 写入时刻, {归并组主文件编号: 企业数})。
LIST_COUNTS_TTL = 3600
_LIST_COUNTS: dict[str, tuple[tuple, float, dict[int, int]]] = {}
_LIST_COUNTS_LOCK = Lock()


def _list_entry_counts(session: Session, lists_revision: tuple) -> dict[int, int]:
    """{归并组主文件编号: 去重后的企业名称数}，只含至少读到一个名称的组。

    口径和改前在程序里逐条去重时完全一样：只算和查名单同一公开来源条件（只取一级来源，或与一级
    来源归并的同一通知）、人工没改成别的类型的文件的条目；主文件和它当前生效的副本算一组
    （副本只能挂在一个主文件下：document_merges 以副本编号为主键）；同一组里同一个规范化名称只算
    一次，空名称不算。名称列的排序规则是 utf8mb4_0900_bin（迁移 0001），数据库去重和程序里比较
    字符串一样：区分大小写、不忽略尾部空格。"""
    # 先取版本再汇总：汇总期间有新写入时，存下的版本只会比数据旧，下次就重算，不会反过来
    revision = (lists_revision, list_count_revision(session))
    with _LIST_COUNTS_LOCK:  # 同一时间只算一份，并发的请求等它算完直接用
        cached = _LIST_COUNTS.get("all")
        stamp = _time.monotonic()
        if cached and cached[0] == revision and stamp - cached[1] < LIST_COUNTS_TTL:
            return cached[2]
        # 归并表起别名：公开来源条件的子查询里也用到 document_merges，两层不混
        merge = aliased(DocumentMerge)
        master = func.coalesce(merge.canonical_id, ListEntry.document_id)
        rows = session.execute(
            select(master, func.count(func.distinct(ListEntry.normalized_name)))
            .select_from(ListEntry)
            .join(Document, Document.id == ListEntry.document_id)
            .join(Source, Source.id == Document.source_id)
            .outerjoin(
                merge,
                and_(merge.duplicate_id == ListEntry.document_id, merge.active.is_(True)),
            )
            .where(
                public_condition(),
                or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
                func.char_length(ListEntry.normalized_name) > 0,
            )
            .group_by(master)
        ).all()
        counts = {int(group): int(names) for group, names in rows}
        _LIST_COUNTS["all"] = (revision, stamp, counts)
        return counts


def _list_board_payload(
    session: Session,
    days: int,
    year: int | None,
    province: str,
    list_type: str,
    lists_revision: tuple | None = None,
) -> dict[str, Any]:
    """生成筛选后的完整名单和统计，调用方负责分页。"""
    rows = session.execute(_base().where(_kind_is(["名单"]))).all()
    if not rows:
        return {"records": [], "stats": {"by_year": [], "by_province": [], "by_type": []}}

    masters = [doc.id for doc, _ in rows]
    details = _details(session, masters)
    if lists_revision is None:
        lists_revision = publication_revision(session, "lists")
    # 主文件都是可见文件（不是任何生效归并的副本），所以按主文件编号取到的就是它这一组的数
    counts = _list_entry_counts(session, lists_revision)

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
        count = counts.get(doc.id)  # 一个企业名称也没读到的名单为 None
        records.append(
            {
                "id": str(doc.id),
                "title": doc.title,
                "date": doc.publish_date.isoformat() if doc.publish_date else None,
                "wording": wording,
                "issuer": normalize_issuer(
                    detail.issuer if detail and detail.issuer else src.dept_full_name
                ),
                "province": item_province,
                "city": src.city,
                "district": src.district,
                "level": src.level,
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
            if record["count"] is None:
                continue
            value = record[field]
            if value is None:
                continue
            key = str(value)
            current = grouped.setdefault(key, [0, 0])
            current[0] += 1
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
        result = _list_board_payload(session, days, year_value, province, list_type, revision)
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


# 详情页、阅读页的正文（D-030、D-097）：从原网页或 PDF 自动提取的文字，未经人工逐字核对，只供阅读；
# 截止日期等关键字段仍只看"要点"里核对过的。入库时已隐去个人信息，给出前按现行规则再隐去一遍，
# 遮掉的地方写"（这里隐去了电话）"这类说法（D-097：以前写"（电话见原文）"，像在叫人去原网站），
# 去掉网页上几处固定的按钮文字（只按原样整串替换，不做猜测性的清理）。
# 给哪些：申报通知、政策给全文；名单类挡掉名单内容后给（D-092）；资讯与指南、其他两类，标题写着
# 个人事项的（PERSONAL_MATTER_WORDS）不给，其余给全文（D-097 ②）。
# 不显示的：像网页导航的（page_chrome_only）。以前不到 200 字的一律不显示（多半没抓全），真短的
# 通知（"现将《××办法》印发给你们，请遵照执行。"）也被挡掉了，D-097 改成现在这样。
# 详情最多给前 2 万字（离 callContainer 单次返回 1000 KiB 的上限很远），阅读页分页给全文。
# BODY_MIN_CHARS 只用来挑合并转载里用哪一份：自己的正文够这么长就用自己的，不够再看组里别的网址
# （AI 解读取正文也是这个挑法），不再用来挡显示。
BODY_MIN_CHARS = 200
BODY_MAX_CHARS = 20_000
# 入库脱敏留下的标记（crawler.detail.redact_sensitive、attachment_text.mask）→ 页面上的说法
MASK_MARKS = (
    ("[电话已脱敏]", "（这里隐去了电话）"),
    ("[手机号已脱敏]", "（这里隐去了电话）"),
    ("[姓名已脱敏]", "（这里隐去了联系人）"),
    ("[邮箱已脱敏]", "（这里隐去了邮箱）"),
    ("[身份证号已脱敏]", "（这里隐去了证件号）"),
    ("[银行账号已脱敏]", "（这里隐去了银行账号）"),
)
PHONE_MARK = "（这里隐去了电话）"
_PAGE_BUTTONS = ("【关闭窗口】", "【打印本页】", "【字体：大 中 小】", "扫一扫在手机打开当前页")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
BODY_KINDS = ("申报通知", "政策")
# 名单类通知的正文挡掉名单内容后也给读（D-092，youju/lists/body.py）
LIST_BODY_KINDS = (*BODY_KINDS, "名单")
# 资讯与指南、其他（D-097 ②）：标题不是个人事项的也给全文。
# BODY_KINDS 不变：AI 解读只做申报通知和政策（ai/interpret.py 用它）
OPEN_BODY_KINDS = (GUIDE, "其他")
# 个人事项（D-097 ②）：招聘、考试、职称、任免、表彰个人、救助、处分等，标题里有这些词的不给正文
# （正文多半列着个人姓名、成绩、职务）。按 10/27 线上公开快照里这两类全部 841 个标题逐条核过：
# 宁可多挡（"评审专家培训的通知"也挡），不漏；会误挡普通政府文件的词（"先进"挡了"先进制造业"、
# "个人"挡了"个人所得税"政策、"体检"挡了"政务新媒体检查"）不用，换成更具体的写法。
PERSONAL_MATTER_WORDS = (
    # 招聘、招募、录用、考试
    "招聘",
    "招募",
    "引才",
    "人才引进",
    "引进人才",
    "拟聘",
    "聘用",
    "录用",
    "招录",
    "选调",
    "公务员",
    "面试",
    "笔试",
    "考试",
    "成绩",
    "招生",
    "毕业生",
    "学生",
    "学员",
    "博士后",
    # 职称、资格
    "职称",
    "专业技术资格",
    "任职资格",
    "专业技术职务",
    "评审结果",
    "答辩",
    "资格证书",
    "合格证书",
    "业余无线电",
    *TITLE_EVALUATIONS,
    # 任免、人事
    "任免",
    "任职",
    "拟任",
    "任前",
    "干部",
    "人事",
    "职务",
    "考察对象",
    "负责人",
    "签字人",
    "矿长",
    "同志",
    "人员",
    "人选",
    "入选",
    "专家",
    "特派员",
    "外国人",
    "薪酬",
    # 表彰个人、奖励
    "表彰",
    "表扬",
    "选树",
    "劳模",
    "劳动模范",
    "奖章",
    "红旗手",
    "获奖",
    "授奖",
    "授予",
    "奖金",
    "初评通过",
    "技能大赛",
    "技能竞赛",
    # 救助、生活
    "救助",
    "低保",
    "抚恤",
    "慰问",
    "助学",
    "奖学金",
    "退休",
    "社保",
    "医保",
    "工伤",
    "户籍",
    "婚姻",
    # 处分、处罚、考核
    "处分",
    "处罚",
    "违纪",
    "问责",
    "通报批评",
    "免职",
    "撤职",
    "失信",
    "巡察",
    "考核结果",
    "考核记分",
)


def personal_matter(title: str | None) -> bool:
    """资讯与指南、其他类的标题写着个人事项（D-097 ②）：正文、附件都不给。
    字中间隔着空格的（"招 聘"）也算。"""
    plain = re.sub(r"\s+", "", title or "")
    return any(word in plain for word in PERSONAL_MATTER_WORDS)


def body_open(title: str | None, kind: str) -> bool:
    """这类文件的正文给不给读（不含名单类：名单类看 list_body_filter）。"""
    return kind in BODY_KINDS or (kind in OPEN_BODY_KINDS and not personal_matter(title))


# 一句话的标点：中文的句号、逗号、分号、问号、叹号；英文的只算紧跟在汉字或右括号、右引号后面的
# （"3,716.50""9:00"里的不算）。冒号不算：导航、发布信息里到处是"当前位置：""发布时间："
_SENTENCE = re.compile(rf"[，。；！？]|(?<=[{chr(0x4E00)}-{chr(0x9FFF)}）」』”])[,.;!?]")
# 像网页导航的正文最长多少字（10/27 线上公开快照：广东科技厅"首页 政务公开 科技资讯……"31 字、
# 四川"首页 > 政民互动 > 意见征集……"60 字、青海"当前位置: 首页 - 新闻资讯……上一篇 下一篇"95–215 字、
# 网站页脚 203–261 字；没有标点的长文多是表格（网站年度报表、名单），照常显示）
PAGE_CHROME_MAX_CHARS = 300


def page_chrome_only(text: str) -> bool:
    """像网页导航：通篇没有一句话（没有句号、逗号这类标点），而且不长。这样的正文不显示（D-097）；
    有一句话的短通知照常显示。"""
    return len(text) <= PAGE_CHROME_MAX_CHARS and _SENTENCE.search(text) is None


def display_marks(text: str) -> str:
    """把入库脱敏留下的"[电话已脱敏]"这类标记换成页面上的说法（"（这里隐去了电话）"）。"""
    for old, new in MASK_MARKS:
        text = text.replace(old, new)
    return text


def _clean_reading_body(text: str | None, *, redact=redact_sensitive) -> str:
    cleaned = display_marks(redact((text or "").replace("\r\n", "\n").replace("\r", "\n")))
    for button in _PAGE_BUTTONS:
        cleaned = cleaned.replace(button, "")
    return _BLANK_LINES.sub("\n\n", _mask_contact_names(cleaned)).strip()


# 联系人所在的行（"联系人""联 系 人""联系方式"），和这类标签写在上一行末尾时的下一行：像姓名的都遮
_CONTACT_LABEL = re.compile(r"联\s*系\s*人|联\s*络\s*人|经\s*办\s*人|联系方式")
_LABEL_AT_END = re.compile(
    r"(?:联\s*系\s*人|联\s*络\s*人|经\s*办\s*人|联系方式)(?:及电话)?\s*[:：]\s*$"
)
# 写着电话的行：有号码（或入库时遮过的号码）的，像姓名的都遮（"吾慧星、张竞博，电话：
# （这里隐去了电话）""刘老师，（这里隐去了电话）""联系电话：88888888（李四）"）；只有"电话""手机"
# 字样、没有号码的（表头"联系电话"、"联系电话：张三、李四"），只遮带称呼的和"电话：""手机："后面的
_PHONE_LINE = re.compile(rf"电话|手机|{PHONE_MARK}")
_PHONE = (
    rf"(?:{PHONE_MARK}|(?<!\d)0\d{{2,3}}[-－]?\d{{7,8}}(?!\d)|(?<!\d)1[3-9]\d{{9}}(?!\d)"
    r"|(?<!\d)\d{7,8}(?!\d))"
)
_PHONE_ANY = re.compile(_PHONE)
_PHONE_LABEL = re.compile(r"(?:电\s*话|手\s*机)(?:号码?)?\s*[:：]")
# "联系人："写在上一行末尾、下面一行一个"姓名 电话"的：这几行都遮
_CONTACT_ROW = re.compile(rf"^\s*[{chr(0x4E00)}-{chr(0x9FFF)}·]{{2,4}}\s*[，,、:：]?\s*{_PHONE}")
_TITLE_END = (
    "老师",
    "先生",
    "女士",
    "同志",
    "处长",
    "科长",
    "主任",
    "部长",
    "局长",
    "所长",
    "站长",
    "经理",
    "专员",
    "工程师",
)
# 网站页脚的编审署名（行里任何位置的"一审：""责编："）：换成"（这里隐去了署名）"，不冒充联系人
_EDITOR = re.compile(
    r"(?:[一二三]审|初审|复审|终审|责任编辑|责编|编辑|校对|审核人?|审稿|签发人?|撰稿人?|供稿)\s*[:：]"
)
_CONTACT_SPLIT = re.compile(r"[\s；;，,、：:（）()\[\]【】/／]+")
_HAN_CHAR = f"{chr(0x4E00)}-{chr(0x9FFF)}"
_UNIT_END_CHARS = "处科室局厅部办站所院会队组股区"
_UNIT_THEN_NAME = re.compile(rf"[{_UNIT_END_CHARS}]([{_HAN_CHAR}]{{2,3}})$")
# 字间带空格的姓名（"联系人：张 三""孟 双、王 璐 联系电话"）：前后不能再接着一个单字（"张 三 李 四"
# 分不清，不遮）
_SPACED_NAME = re.compile(
    rf"(?<![{_HAN_CHAR}])(?<!(?<![{_HAN_CHAR}])[{_HAN_CHAR}] )"
    rf"[{_HAN_CHAR}](?: [{_HAN_CHAR}]){{1,2}}"
    rf"(?![{_HAN_CHAR}])(?! [{_HAN_CHAR}](?![{_HAN_CHAR}]))"
)
# 表格模板里以冒号结尾的栏目（"推荐单位（盖章）："）：不当"标签的下一行"
_FORM_FIELD = re.compile(r"[:：]\s*$")
# 常见姓氏开头、但这些行里常见的普通词
_NOT_NAMES = frozenset(
    {
        "关于",
        "关闭",
        "查询",
        "时间",
        "方式",
        "周一",
        "周二",
        "周三",
        "周四",
        "周五",
        "周六",
        "周日",
        "周末",
        "工作日",
        "上午",
        "下午",
        "中午",
        "白天",
        "晚上",
        "全天",
        "节假日",
        # 单位名后面常跟的词（"指挥部成立""××室通过"）
        "成立",
        "通过",
        "通知",
        "工作",
        "专项",
        "会议",
        "负责",
        "办理",
        "受理",
        "咨询",
        # 表格模板里的词（"推荐单位（盖章）："）
        "盖章",
        "签字",
        "签章",
        "公章",
    }
)
_CONTACT_MARK = "（这里隐去了联系人）"
_EDITOR_MARK = "（这里隐去了署名）"


def _person(word: str) -> bool:
    return word not in _NOT_NAMES and _name_like(word)


def _masked(token: str, mark: str) -> str | None:
    """一段里有像姓名的就给遮好的这一段，没有给 None。整段像（"张三""刘老师"）；去掉称呼后像
    （"张三处长""王五主任"）；"张三和李四"拆开都像；"高新处张三"单位用字后面的 2–3 个字像。"""
    if _person(token):
        return mark
    title = next((t for t in _TITLE_END if token.endswith(t) and len(token) > len(t)), None)
    if title is not None:
        rest = token[: -len(title)]
        if _person(rest):
            return mark
        inner = _masked(rest, mark)
        if inner is not None:
            # "高新区张三处长"：单位留着，姓名连称呼一起遮
            return inner
    parts = [p for p in re.split(r"[和与及]", token) if p]
    if len(parts) >= 2 and all(_person(p) for p in parts):
        return re.sub(r"[^和与及]+", mark, token)
    match = _UNIT_THEN_NAME.search(token)
    if match and _person(match.group(1)):
        return token[: match.start(1)] + mark
    return None


def _mask_line(line: str, tokens: list[str], mark: str) -> str:
    line = _SPACED_NAME.sub(
        lambda m: mark if _person(m.group(0).replace(" ", "")) else m.group(0), line
    )
    for token in sorted(set(tokens), key=len, reverse=True):
        new = _masked(token, mark)
        if new is None:
            continue
        if new == mark:
            # 带括号的（"（王老师）"）连括号一起换，不出现两层括号
            line = line.replace(f"（{token}）", mark).replace(f"({token})", mark)
        line = re.sub(rf"(?<![{_HAN_CHAR}]){re.escape(token)}(?![{_HAN_CHAR}])", new, line)
    return line


def _mask_contact_names(text: str) -> str:
    """给出正文前，联系人所在的行、只写了电话的行、网站编审署名里像姓名的词（常见姓氏开头的
    2–3 个字，和入库脱敏认姓名同一套）再遮一遍。以前入库时只遮了"联系人："后面的第一个词：
    "联系人：（这里隐去了联系人）某某处 张三"的张三、"刘老师，电话……""一审：张三"都还在
    （D-092 让名单类通知的正文也能读，10/25 用本机数据副本实测发现；Grok、agy 审阅补了连写、
    拆行、空格隔开的写法）。"""
    out = []
    after_label = False
    for line in text.split("\n"):
        if not line.strip():
            # 空行不打断"标签写在上一行末尾"：下面第一行有字的照样遮
            out.append(line)
            continue
        raw = line
        contact = (after_label and not _FORM_FIELD.search(line)) or (
            _CONTACT_LABEL.search(line) is not None
        )
        editor = _EDITOR.search(line) is not None
        tokens = [w for w in _CONTACT_SPLIT.split(line) if w]
        if contact:
            line = _mask_line(line, tokens, _CONTACT_MARK)
        elif editor:
            line = _mask_line(line, tokens, _EDITOR_MARK)
        elif _PHONE_LINE.search(line) and _PHONE_ANY.search(line):
            line = _mask_line(line, tokens, _CONTACT_MARK)
        elif _PHONE_LINE.search(line):
            label = _PHONE_LABEL.search(line)
            near_label = [
                t
                for t in tokens
                if t.endswith(_TITLE_END) or (label is not None and line.find(t) >= label.end())
            ]
            line = _mask_line(line, near_label, _CONTACT_MARK)
        # 标签写在行尾的，下一行也遮；下面接着是一行一个"姓名 电话"的，一直遮到不是为止
        row = after_label and _CONTACT_ROW.match(raw) is not None
        after_label = _LABEL_AT_END.search(line) is not None or row
        out.append(line)
    return "\n".join(out)


@dataclass
class ReadingBody:
    """一份通知的正文给不给、给哪一份（详情页和阅读页同一个判断，D-097）。

    status：ok 给文字（text 是清理、脱敏后的全文；hidden：名单类挡掉了名单内容）；list 名单类通知的
    正文几乎全是名单（reason=list），或名单没能和通知文字分开（reason=mixed），只给查名单；
    personal 个人事项（资讯与指南、其他类的标题写着招聘、职称等）和失信、个人类名单，不给；
    none 没有能显示的正文，reason 写页面上怎么说：attachments 这份通知列着附件（"这份通知的内容在
    附件里"），site 没有附件（"这个网站的正文还取不到"），link_gone 原网页已失效（"原网页已打不开，
    正文暂不提供"）。
    problem：none 的具体原因（运维日报按网站统计）：empty 没取到正文；chrome 取到的像网页导航；
    short_list 名单类通知挡完名单剩的字太少（D-092 的下限）；link_gone 原网页已失效。"""

    status: str
    text: str = ""
    owner_id: int | None = None
    hidden: bool = False
    reason: str | None = None
    problem: str | None = None


def _no_body(problem: str, *, attachments: bool | None, owner_id: int | None = None) -> ReadingBody:
    reason = None if attachments is None else ("attachments" if attachments else "site")
    return ReadingBody("none", owner_id=owner_id, reason=reason, problem=problem)


def _judge_text(
    cleaned: str,
    kind: str,
    *,
    list_filter: Callable[[str], list_body.Filtered] | None,
    attachments: bool | None,
    owner_id: int | None = None,
) -> ReadingBody:
    """已清理、脱敏的正文能不能显示：名单类先挡掉名单内容（D-092），再看是不是像网页导航。"""
    hidden = False
    if list_filter is not None:
        result = list_filter(cleaned)
        if result.text is None and result.reason == "short":
            # 没认出名单、挡完剩的字又太少（D-092 的下限，防止短名单漏出来）
            return _no_body("short_list", attachments=attachments, owner_id=owner_id)
        if result.text is None:
            return ReadingBody("list", owner_id=owner_id, reason=result.reason)
        cleaned, hidden = result.text, result.hidden
    if not cleaned:
        return _no_body("empty", attachments=attachments, owner_id=owner_id)
    if page_chrome_only(cleaned):
        return _no_body("chrome", attachments=attachments, owner_id=owner_id)
    return ReadingBody("ok", text=cleaned, owner_id=owner_id, hidden=hidden)


def body_payload(body: ReadingBody) -> dict:
    """详情接口的 body：ok 给前 2 万字和全文字数；个人事项写 original（旧版小程序按"不展示"说）；
    没有能显示的正文给 none 和原因。"""
    if body.status == "ok":
        payload = {
            "status": "ok",
            "text": body.text[:BODY_MAX_CHARS],
            "total": len(body.text),
            "truncated": len(body.text) > BODY_MAX_CHARS,
        }
        if body.hidden:
            payload["listHidden"] = True
        return payload
    if body.status == "personal":
        return {"status": "original", "reason": "personal"}
    if body.status == "list":
        return {"status": "list"}
    return {"status": "none", "reason": body.reason} if body.reason else {"status": "none"}


def body_for_reading(
    text: str | None,
    kind: str,
    *,
    list_filter: Callable[[str], list_body.Filtered] | None = None,
    title: str | None = None,
) -> dict:
    """一段正文按详情页的规则给不给（不查库、不看附件，没正文时不写原因）。资讯与指南、其他类要
    给标题（title）才能判断是不是个人事项，没给按个人事项算。名单类：给了 list_filter
    （list_body_filter，D-092）的挡掉名单后给文字，没给的按 list 说。"""
    if kind == "名单" and list_filter is None:
        return {"status": "list"}
    if kind != "名单" and not body_open(title if title is not None else "招聘", kind):
        return body_payload(ReadingBody("personal"))
    return body_payload(
        _judge_text(_clean_reading_body(text), kind, list_filter=list_filter, attachments=None)
    )


def list_body_filter(session: Session, doc: Document) -> Callable[[str], list_body.Filtered] | None:
    """名单类通知正文的过滤函数（D-092）：这份通知能读（不是失信、个人类）时给，不能读给 None。
    用这份通知归并组解析出的条目名称认名单行，组里各版本的发文机关署名照常显示。"""
    if not list_body.readable(doc.title):
        return None
    return list_body.filter_for(session, group_ids(session, doc.id), min_chars=list_body.MIN_CHARS)


def body_withheld(doc: Document, kind: str) -> bool:
    """个人事项（资讯与指南、其他类）和失信、个人类名单：正文不给（详情页、阅读页都一样）。"""
    if kind == "名单":
        return not list_body.readable(doc.title)
    return not body_open(doc.title, kind)


def reading_body(
    session: Session,
    doc: Document,
    kind: str,
    *,
    attachments: bool,
    redact=redact_sensitive,
) -> ReadingBody:
    """详情页和阅读页给的正文（D-097：两处同一个判断）。attachments：这份通知列着附件（没有能显示
    的正文时，页面说"内容在附件里"还是"正文还取不到"）。原网页已失效的不给（D-097"维持现状不给，
    统计失效原因后另议"；以前详情页还显示之前收录的正文、阅读页不给，两处不一致），组里原网页已失效
    的其他网址的正文也不用。"""
    if body_withheld(doc, kind):
        # 失信、处罚类和个人类名单：和别的个人事项一样不展示，不引导去查名单
        return ReadingBody("personal")
    if doc.link_gone_at is not None:
        return ReadingBody("none", reason="link_gone", problem="link_gone")
    list_filter = list_body_filter(session, doc) if kind == "名单" else None
    if list_filter is not None:
        allowed: tuple[str, ...] = LIST_BODY_KINDS
    elif kind in BODY_KINDS:
        allowed = BODY_KINDS
    else:
        allowed = (*BODY_KINDS, *OPEN_BODY_KINDS)
    chosen = _reading_body_selection(
        session,
        doc.id,
        redact=redact,
        live_only=True,
        allowed_kinds=allowed,
        for_reading=True,
        list_filter=list_filter,
    )
    if chosen is None:
        return _no_body("empty", attachments=attachments)
    owner_id, _, cleaned = chosen
    return _judge_text(
        cleaned, kind, list_filter=list_filter, attachments=attachments, owner_id=owner_id
    )


def _reading_body_selection(
    session: Session,
    doc_id: int,
    *,
    redact=redact_sensitive,
    live_only: bool = False,
    allowed_kinds: tuple[str, ...] | None = BODY_KINDS,
    for_reading: bool = False,
    list_filter: Callable[[str], list_body.Filtered] | None = None,
) -> tuple[int, str, str] | None:
    """主文足够可读就优先用；否则依长度、编号依次尝试转载，不一次载入全部正文。

    返回实际来源编号、原始文字、清洗后文字。都不足时保留首个候选，让调用方继续说没有正文；
    live_only 由原生阅读启用，旧 AI 结果保留原站撤下后的既有处理规则。
    allowed_kinds=None 仅供管理报告统计历史采集范围，公开阅读保持默认类型限制。
    for_reading（详情页、阅读页，D-097）：像网页导航的、空的不算可读，跳过去看别的网址；资讯与指南、
    其他类标题写着个人事项的网址不用；都不可读时给第一个候选（调用方据此说"像网页导航"）。
    名单类（给了 list_filter）先挡掉名单再看：几乎全是名单、名单分不开的就用这一份（页面说去
    查名单）；挡完剩的字太少、剩下的像网页导航，才看别的网址。
    AI 解读不加 for_reading，挑法和以前一样。
    """
    stmt = (
        select(Document.id, Document.title, Document.kind_override)
        .join(DocumentDetail, Document.id == DocumentDetail.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.document_id.in_(group_ids(session, doc_id)),
            DocumentDetail.status == "ok",
            DocumentDetail.body_text.is_not(None),
            DocumentDetail.body_text != "",
            Source.rights_level == 1,
            available_condition(),
        )
        .order_by(
            (Document.id == doc_id).desc(),
            func.char_length(DocumentDetail.body_text).desc(),
            Document.id,
        )
    )
    if live_only:
        stmt = stmt.where(Document.link_gone_at.is_(None))
    first = fallback = None
    for candidate_id, title, override in session.execute(stmt).all():
        kind = effective_kind(title, override)
        if allowed_kinds is not None and kind not in allowed_kinds:
            continue
        if for_reading and kind in OPEN_BODY_KINDS and personal_matter(title):
            continue
        raw = session.scalar(
            select(DocumentDetail.body_text).where(DocumentDetail.document_id == candidate_id)
        )
        if not raw:
            continue
        cleaned = _clean_reading_body(raw, redact=redact)
        candidate = candidate_id, raw, cleaned
        usable = not for_reading or _usable_for_reading(cleaned, list_filter)
        if usable and len(cleaned) >= BODY_MIN_CHARS:
            return candidate
        if usable and fallback is None:
            fallback = candidate
        if first is None:
            first = candidate
    return fallback or first


def _usable_for_reading(
    cleaned: str, list_filter: Callable[[str], list_body.Filtered] | None
) -> bool:
    """这一份正文在详情页、阅读页算不算可读（挑合并转载里用哪一份时用）。"""
    if list_filter is None:
        return bool(cleaned) and not page_chrome_only(cleaned)
    result = list_filter(cleaned)
    if result.text is None:
        return result.reason in ("list", "mixed")
    return not page_chrome_only(result.text)


def _reading_body(
    session: Session, doc_id: int, *, allowed_kinds: tuple[str, ...] | None = BODY_KINDS
) -> str | None:
    selected = _reading_body_selection(session, doc_id, allowed_kinds=allowed_kinds)
    return selected[1] if selected else None


@router.get("/documents/{doc_id}")
def document(doc_id: int, session: SessionDep) -> dict:
    """文件详情：要点、附件、原文出处、正文（供阅读）；截止日期只给核对通过的。"""
    from youju.ai import interpret

    if is_taken_down(session, doc_id):
        raise HTTPException(status_code=404, detail="该文件已下架")
    doc_id = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
    row = session.execute(_base().where(Document.id == doc_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这份文件")
    doc, src = row
    # AI 入口用（不需要身份）：interpret、clause 是这份文件能不能做，reason 是不能做的原因代码
    # （D-075）。只说文件本身的属性、按原文实时判断，和 AI 开没开无关，admin、api 两个服务算出来
    # 一样。AI 开没开、这个人能不能用只问 api（/advisor/mode、能力接口）：详情多半由 admin 回，
    # 不写它自己的开关，免得两个服务设置不一样时入口一边显示、一边拒绝。
    ai_reason: str | None = None
    try:
        interpret.eligible_body(session, doc.id)
    except interpret.InterpretRefusal as exc:
        ai_reason = exc.code or "unavailable"
    ai_eligible = ai_reason is None
    detail = _details(session, [doc.id]).get(doc.id)
    attachment_rows = sorted(detail.attachments, key=lambda a: a.position) if detail else []
    approved = _approved(session, [doc.id]).get(doc.id, [])
    deadline_views, _, _ = deadline_excerpts.batch(
        session, [doc.id], {doc.id: approved}, now().date()
    )
    pending = not approved and doc.id in _pending(session, [doc.id])
    kind = effective_kind(doc.title, doc.kind_override)
    fields = _approved_fields(session, [doc.id]).get(doc.id, {}) if kind == "申报通知" else {}
    # 申报条件（D-052 第④项）：只给整份人工确认过、和现在这版正文对得上的清单
    apply_conditions = public_conditions(session, doc.id) if kind == "申报通知" else None
    status_value = application_status(approved) if kind == "申报通知" else None
    issuer = normalize_issuer(detail.issuer if detail and detail.issuer else src.dept_full_name)
    points = [["发文机关", issuer]]
    if detail and detail.doc_number:
        points.append(["文号", detail.doc_number])
    points.append(["适用地区", applicable_region(src.province, src.level, src.city, src.district)])
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
        "ai": {
            "interpret": ai_eligible,
            "clause": ai_eligible,
            "reason": ai_reason,
        },
        "id": str(doc.id),
        "kind": kind,
        "tags": [kind],
        # 项目类别：按标题归类，仅供检索参考，非官方分类；只有申报通知和名单有
        "category": shown_category(kind, doc.rule_category, doc.title),
        "region": region_label(src.province, src.level, src.city, src.district),
        "province": src.province,
        "city": src.city,
        "district": src.district,
        "level": src.level,
        "title": doc.title,
        "issuer": issuer,
        "date": doc.publish_date.isoformat() if doc.publish_date else None,
        "status": list_status(doc.title, kind),
        "app_status": status_value,
        "fields": fields,
        "url": public_url(doc.url),
        "link_gone": _link_gone(doc),
        "original_urls": [
            public_url(d.url)
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
        "deadlines": [view.public(now().date()) for view in deadline_views.get(doc.id, [])]
        if effective_kind(doc.title, doc.kind_override) == "申报通知"
        else [],
        "deadline_changes": deadline_changes,
        "points": points,
        "excerpt": excerpt,
        "attachments": _detail_attachments(session, doc, kind, attachment_rows),
        "checked": checked,
        # 正文（D-097）：和阅读页同一个判断（原网页已失效的不给）
        "body": body_payload(
            reading_body(
                session, doc, kind, attachments=bool(attachment_rows), redact=attachment_text.mask
            )
        ),
    }
    if fields:
        payload["fields"] = fields
    else:
        payload.pop("fields", None)
    if apply_conditions:
        payload["apply_conditions"] = apply_conditions
    # 转发组（D-055）：同一份部里通知的其他版本，各版本的截止各自列出（不互相套用）
    repost = repost_versions(session, [doc.id]).get(doc.id)
    if repost and repost["others"]:
        payload["repost"] = _repost_detail(session, doc.id, repost)
    return payload


def _detail_attachments(
    session: Session, doc: Document, kind: str, rows: list[DocumentAttachment]
) -> list[dict]:
    """详情页的附件：能不能在小程序里读（readable、state、stateText、scannedPages）和阅读页同一个
    判断（attachment_text.judge，D-097）。preview、reason 是附件副本能不能下载，旧版小程序还在用，
    照旧给，不能当成"能不能读"。"""
    items = attachment_items(
        session,
        rows,
        kind=kind,
        recent=is_recent(doc.publish_date, doc.first_seen_at, now(), kind=kind),
    )
    states = {r.attachment_id: r for r in attachment_text.judge(session, doc, rows, now())}
    for item in items:
        state = states[int(item["id"])]
        item.update(
            readable=state.readable,
            state=state.state,
            stateText=state.note,
            scannedPages=(state.scanned_pages or 0) if state.readable else 0,
        )
    return items


def _repost_detail(session: Session, doc_id: int, info: dict[str, Any]) -> dict[str, Any]:
    """详情页的“各版本”：本组每个对外可见、已确认的版本，各带自己的原文网址和自己核对通过的截止。

    截止按版本标（“部里”只给库里的部里原件，其余写“江西版”），不同版本的日期不互相套用；
    没核对的写截止未核对。顺序：部里原件、本版、其他版本按发布日期。"""
    members = info["members"]
    ids = [doc.id for doc, _ in members]
    approved = _approved(session, ids)
    pending = _pending(session, [i for i in ids if not approved.get(i)])
    details = _details(session, ids)
    ordered = sorted(
        members,
        key=lambda pair: (
            pair[1].province != _NATIONAL,
            pair[0].id != doc_id,
            pair[0].publish_date or date.max,
            pair[0].id,
        ),
    )
    versions = []
    for version, src in ordered:
        deadlines = approved.get(version.id, [])
        detail = details.get(version.id)
        versions.append(
            {
                "id": str(version.id),
                "label": info["labels"][version.id],
                "current": version.id == doc_id,
                "region": _province_name(
                    f"{src.city}·{src.district}"
                    if src.level == "区县级" and src.district
                    else src.city
                    if src.level == "市级" and src.city
                    else src.province
                ),
                "issuer": normalize_issuer(
                    detail.issuer if detail and detail.issuer else src.dept_full_name
                ),
                "date": version.publish_date.isoformat() if version.publish_date else None,
                "url": public_url(version.url),
                "deadlines": [
                    {
                        "label": d.kind,
                        "date": d.approved_date.isoformat(),
                        "time": _hhmm(d.approved_time),
                    }
                    for d in deadlines
                ],
                "deadline_state": deadline_state(deadlines, version.id in pending),
            }
        )
    return {
        "group_id": info["group_id"],
        "label": info["label"],
        # 照录正文里引用的部里通知标题和文号（没有就不给），不写推断的内容
        "upstream": info["upstream"],
        "versions": versions,
    }


@router.get("/calendar")
def calendar(
    session: SessionDep,
    excerpts: Annotated[int | None, Query(ge=1, le=1)] = None,
    days: Annotated[int, Query(ge=1, le=366)] = 366,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
    province: Annotated[str, Query(max_length=32)] = "",
    city: Annotated[str, Query(max_length=32)] = "",
    district: Annotated[str, Query(max_length=32)] = "",
) -> dict:
    """申报日历：核对通过的未截止事项及近 30 天已截止事项，分别分页和统计。

    每条的 id 是截止记录编号（一份文件可以有多个截止）；打开详情用 docId。
    """
    if province and province not in (set(PROVINCES) - {"不限地区"}):
        raise HTTPException(status_code=422, detail="省份不支持")
    city, district = _validate_district_scope(session, province, city, district)
    if excerpts == 1:
        return _calendar_payload_excerpts(session, days, limit, offset, province, city, district)
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
            selected_source = master.source if master_id != doc.id else src
            if _city_matches(selected_source, province, city, district):
                unique[key] = (dl, master, selected_source)
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
                "province": "国家部委" if src.province == "国家" else src.province,
                "city": src.city,
                "district": src.district,
                "level": src.level,
                "due": dl.approved_date.isoformat(),
                "time": _hhmm(dl.approved_time),
                "region": (
                    f"{applicable_region(src.province, src.level, src.city, src.district)}"
                    f" · {src.dept}"
                ),
                "kind": effective_kind(doc.title, doc.kind_override),
                "title": doc.title,
                "what": dl.kind,
                "hasDetail": True,
                "verified": True,
            }
            for dl, doc, src in rows
        ]

    return {
        "today": d.isoformat(),
        "total": len(upcoming),
        "items": _shown_with_ai(session, items(upcoming)[offset : offset + limit], key="docId"),
        "pastTotal": len(past),
        "past": _shown_with_ai(session, items(past)[offset : offset + limit], key="docId"),
        # 空状态说明用：已核对截止的文件份数（与截止雷达的 verified_total 同一口径，只给数字）
        "verifiedTotal": verified_total(session),
    }


def _calendar_payload_excerpts(
    session: Session,
    days: int,
    limit: int,
    offset: int,
    province: str = "",
    city: str = "",
    district: str = "",
) -> dict:
    current = now()
    today = current.date()
    raw_ids = list(
        session.scalars(
            select(DocumentDeadline.document_id)
            .where(
                or_(
                    and_(
                        DocumentDeadline.status == "approved",
                        DocumentDeadline.approved_date >= today - timedelta(days=30),
                        DocumentDeadline.approved_date <= today + timedelta(days=days),
                    ),
                    and_(
                        DocumentDeadline.status == "pending",
                        DocumentDeadline.candidate_date >= today,
                        DocumentDeadline.candidate_date <= today + timedelta(days=max(days, 7)),
                    ),
                )
            )
            .distinct()
        )
    )
    mapping = canonical_ids(session, raw_ids)
    ids = sorted({mapping.get(doc_id, doc_id) for doc_id in raw_ids})
    rows = session.execute(
        _base()
        .add_columns(DocumentDetail.issuer)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(Document.id.in_(ids))
    ).all()
    ids = [doc.id for doc, _, _ in rows]
    members = groups_for(session, ids)
    views, soon, _ = deadline_excerpts.batch(
        session, ids, _approved(session, ids, members), today, members
    )
    upcoming, past, unverified = [], [], []
    for doc, src, detail_issuer in rows:
        if not _city_matches(src, province, city, district):
            continue
        kind = effective_kind(doc.title, doc.kind_override)
        due = deadline_excerpts.selected(views.get(doc.id, []), today)
        if kind == "申报通知" and soon.get(doc.id, False):
            unverified.append(
                {
                    "id": str(doc.id),
                    "title": doc.title,
                    "issuer": normalize_issuer(detail_issuer or src.dept_full_name),
                    "province": "国家部委" if src.province == "国家" else src.province,
                    "city": src.city,
                    "district": src.district,
                    "level": src.level,
                    "kind": next(
                        (v.kind for v in views[doc.id] if v.source == "unverified"), "截止未核对"
                    ),
                }
            )
        for view in views.get(doc.id, []):
            if view.day is None or view.source == "unverified":
                continue
            if view.source == "excerpt" and kind != "申报通知":
                continue
            if view.day > today + timedelta(days=days):
                continue
            open_now = _view_open(view, current)
            if not open_now and (
                view.source != "verified" or view.day < today - timedelta(days=30)
            ):
                continue
            item = {
                "id": f"dl{view.record_id}",
                "docId": str(doc.id),
                "province": "国家部委" if src.province == "国家" else src.province,
                "city": src.city,
                "district": src.district,
                "level": src.level,
                "due": due.public(today) if due else None,
                "date": view.day.isoformat(),
                "time": _hhmm(view.clock),
                "region": (
                    f"{applicable_region(src.province, src.level, src.city, src.district)}"
                    f" · {src.dept}"
                ),
                "kind": kind,
                "title": doc.title,
                "what": view.kind,
                "hasDetail": True,
                "verified": view.source == "verified",
                "source": view.source,
                "quote": view.quote,
                "dueSoonUnverified": soon.get(doc.id, False),
            }
            (upcoming if open_now else past).append(item)

    def sort_key(item):
        return (item["date"], item["time"] == "", item["time"], item["id"])

    upcoming.sort(key=sort_key)
    past.sort(key=sort_key, reverse=True)
    return {
        "today": today.isoformat(),
        "total": len(upcoming),
        "items": _shown_with_ai(session, upcoming[offset : offset + limit], key="docId"),
        "pastTotal": len(past),
        # 已截止的也带 ai（能不能解读不随截止变）；按钮放不放由小程序定（D-075：已截止的不放）
        "past": _shown_with_ai(session, past[offset : offset + limit], key="docId"),
        "verifiedTotal": verified_total(session),
        "unverifiedSoon": {
            "total": len(unverified),
            "items": _shown_with_ai(session, unverified[:50]),
        },
    }


@router.get("/lists/recent")
def recent_lists(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
    group: Annotated[
        int | None,
        Query(
            ge=1,
            le=1,
            description="1 按转发组折叠（D-055）：同一份部里通知的各地版本只列一条、计数按组；"
            "不传和以前一样",
        ),
    ] = None,
) -> dict:
    """最近发布的名单与公示，以及本周份数。group=1 时按转发组折叠和计数（名单一般没有转发组）。"""
    grouped = group == 1
    wrap = _grouped if grouped else (lambda stmt: stmt)
    stmt = _base().where(_kind_is(["名单"]))
    start, end = _week(now().date())
    week_total = _count(
        session, wrap(stmt.where(Document.publish_date >= start, Document.publish_date < end))
    )
    stmt = wrap(stmt)
    total = _count(session, stmt)
    rows = session.execute(_ordered(stmt).limit(limit).offset(offset)).all()
    return {
        "week_total": week_total,
        "total": total,
        "items": _items(session, [tuple(r) for r in rows], grouped),
    }


# 包含查询成本高于精确查找：每个来源地址每分钟 30 次，单进程共 300 次；内存键数有上限。
_LOOKUP_TIMES: OrderedDict[str, deque[float]] = OrderedDict()
_LOOKUP_INSTANCE: deque[float] = deque()
_LOOKUP_LOCK = Lock()


def _limit_lookup(request: Request) -> None:
    from youju.api.app import client_ip

    stamp = _time.monotonic()
    caller = client_ip(request)
    with _LOOKUP_LOCK:
        times = _LOOKUP_TIMES.get(caller, deque())
        for queue in (times, _LOOKUP_INSTANCE):
            while queue and queue[0] <= stamp - 60:
                queue.popleft()
        if len(times) >= 30 or len(_LOOKUP_INSTANCE) >= 300:
            raise HTTPException(status_code=429, detail="查询太频繁，请稍后再试")
        times.append(stamp)
        _LOOKUP_INSTANCE.append(stamp)
        _LOOKUP_TIMES[caller] = times
        _LOOKUP_TIMES.move_to_end(caller)
        while len(_LOOKUP_TIMES) > 1000:
            _LOOKUP_TIMES.popitem(last=False)


@router.get("/lists/lookup")
def lookup_lists(
    request: Request,
    session: SessionDep,
    q: Annotated[
        str,
        Query(min_length=2, max_length=100, description="企业全称（也认统一社会信用代码）"),
    ],
) -> dict:
    """查名单：这家企业上过哪些名单。

    按企业全称或统一社会信用代码与名单原文逐条精确比对。按名称查到的条目带信用代码时，
    再用信用代码找回同一企业用别的名称（如更名前）上过的名单。同名但信用代码不同的，
    标 ambiguous，由小程序提示逐条核对。名称相近的另列（similar），不当作同一家。
    同一份名单只列一次；同一通知的不同网址按内容归并（youju.dedup）后也只列一次。只查企业和机构，
    输入像个人姓名的返回 422。coverage 是已收录的名单范围，查不到时据此说明。
    """
    _limit_lookup(request)
    query = normalize_name(q)
    # 像个人姓名的输入（2–4 个汉字、不含机构用词，企业简称如"海尔"也在内）不做精确比对，
    # 只列名称包含它的企业和机构全称供选；一个候选也没有就照旧拒绝。
    # 候选都过企业/机构校验，不会列出个人。
    person = bool(query) and is_person_query(query)
    if person or not query:
        lookup = ListLookupResult(query, (), False, False)
    else:
        lookup = lookup_list_matches(
            session, query, scan_limit=_LOOKUP_SCAN, result_limit=_LOOKUP_LIMIT
        )
    candidates = lookup_name_candidates(session, query) if query and not lookup.matches else []
    if not query or (person and not candidates):
        raise HTTPException(status_code=422, detail="只查企业和机构，请输入企业全称")
    masters = lookup.master_ids
    best = {match.master_id: match for match in lookup.matches}
    tokens = list_evidence.entry_tokens(session, [m.entry_id for m in lookup.matches])
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
            # 查"原名单里的那一行"用（D-088）：带签名，只有查到这家企业的人拿得到
            "entryToken": tokens.get(entry.entry_id),
        }
        items.append(item)
    shown_names = {entry.normalized_name for entry in best.values()}
    similar: list[dict] = []
    key = candidate_key(query)
    if not person and len(query) >= 4 and not query.isascii() and len(key) >= 2:
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
    return {
        "query": q.strip(),
        "items": items,
        "similar": similar,
        "candidates": candidates,
        "ambiguous": lookup.ambiguous,
        "truncated": lookup.truncated,
        "coverage": _lookup_coverage(session),
    }


# 查名单的比对范围（收录份数、最早日期、没读出和没读全的份数）是整库统计，不在每次查询时都重算：
# 名单数据版本（解析记录、人工定的类型、下架和归并，见 report_revision）没变时 60 秒内复用。
# 下架、重新解析后马上失效（管理服务和接口服务是不同进程，靠数据库里的版本）；来源权限这类少见的
# 改动最多晚 60 秒
LOOKUP_COVERAGE_TTL = 60
_LOOKUP_COVERAGE: dict[str, tuple[tuple, float, dict[str, Any]]] = {}


def _lookup_coverage(session: Session) -> dict[str, Any]:
    revision = publication_revision(session, "lists")
    cached = _LOOKUP_COVERAGE.get("all")
    current = _time.monotonic()
    if cached and cached[0] == revision and current - cached[1] < LOOKUP_COVERAGE_TTL:
        return dict(cached[2])
    lists_total, since = session.execute(
        select(func.count(func.distinct(ListEntry.document_id)), func.min(Document.publish_date))
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(Source.rights_level == 1, available_condition(), list_title_allowed())
    ).one()
    unread, partial = _unread_lists(session)
    coverage = {
        "lists": lists_total,
        "since": since.isoformat() if since else None,
        # "没查到"时如实说（D-091）：没读出企业名称的名单份数、只读了前面一部分的份数
        "unreadLists": unread,
        "partialLists": partial,
    }
    _LOOKUP_COVERAGE["all"] = (revision, current, coverage)
    return dict(coverage)


def _unread_lists(session: Session) -> tuple[int, int]:
    """公开的名单文件里，没读出企业名称的份数（扫描件、旧格式、表格没认出来、详情取不到）、
    只读了前面一部分的份数（PDF 超过页数上限）。"""
    has_entries = (
        select(ListEntry.id).where(ListEntry.document_id == ListParse.document_id).exists()
    )
    base = (
        select(func.count(func.distinct(ListParse.document_id)))
        .join(Document, Document.id == ListParse.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Source.rights_level == 1,
            available_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
            list_title_allowed(),
        )
    )
    unread = session.scalar(base.where(~has_entries)) or 0
    partial = (
        session.scalar(base.where(has_entries, ListParse.failure_reason.like("%仅解析前%"))) or 0
    )
    return unread, partial


_EVIDENCE_REASONS = {
    "scan": "原文附件是扫描图片，读不出文字",
    "empty": "原文附件里没有文字",
    "broken": "原文附件损坏或加了密",
    "format": "原文附件的格式暂不支持",
    "too_large": "原文附件太大",
    "webpage": "原网站给的不是文件",
    "gone": "原网站已经没有这个附件",
    "unavailable": "原网站暂时下载不了这个附件",
}


def _where_text(where: dict) -> str:
    """原文位置的说法：第 3 页第 1 个表第 12 行；工作表"Sheet1"第 120 行；第 2 个表第 5 行。"""
    parts = []
    if where.get("sheet"):
        parts.append(f"工作表“{where['sheet']}”")
    if where.get("page"):
        parts.append(f"第 {where['page']} 页")
    if where.get("table"):
        parts.append(f"第 {where['table']} 个表")
    if where.get("row"):
        parts.append(f"第 {where['row']} 行")
    return "".join(parts)


@router.get("/lists/record")
def list_record(
    request: Request,
    session: SessionDep,
    entry: Annotated[
        str, Query(min_length=3, max_length=40, description="查名单结果里的 entryToken")
    ],
) -> dict:
    """查名单结果里一条记录在原名单里的那一行（D-088）：只给这一行和列名、在原文的位置，个人信息
    列不显示，不提供整份名单，也不给原文链接。条目编号要用查名单时给的加密编号。"""
    _limit_lookup(request)
    entry_id = list_evidence.parse_entry_token(session, entry)
    row = session.get(ListEntry, entry_id) if entry_id is not None else None
    doc = None
    if row is not None:
        doc = session.execute(
            select(Document, Source)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.id == row.document_id,
                public_condition(),
                or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
                list_title_allowed(),
            )
        ).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="没有找到这条记录，请重新查询")
    document, source = doc
    kind = effective_kind(document.title, document.kind_override)
    location = session.get(ListEntryLocation, row.id)
    evidence_source = session.scalar(
        select(ListEvidenceSource).where(
            ListEvidenceSource.url_hash == list_evidence.url_key(row.source_url)
        )
    )
    parsed_from = session.scalar(
        select(ListParse.source_kind).where(
            ListParse.document_id == row.document_id, ListParse.source_url == row.source_url
        )
    )
    rows: list[dict] = []
    if parsed_from == "detail":
        # 名单在通知网页正文的表格里：原文那一行暂时只能给解析出的记录
        state = "in_body"
    elif evidence_source is None or (location is None and evidence_source.status != "failed"):
        state = "pending"
    elif evidence_source.status == "failed":
        state = "failed"
    elif location.state in ("ok", "multiple", "ambiguous"):
        state = "found"
        for stored in location.rows:
            # 存的时候已只留公开的列（list_evidence.shown_cell）；这里按列名再挡一次个人信息列
            # （名称列除外），文字再脱敏一遍。name：这一格是名称列（查到的那家单位）
            cells = [
                {
                    "h": cell.get("h") or "",
                    "t": redact_sensitive(str(cell.get("t", ""))),
                    "name": bool(cell.get("n")),
                }
                for cell in stored.get("cells", [])
                if cell.get("n") or not list_evidence.personal_header(cell.get("h"))
            ]
            rows.append(
                {
                    "where": _where_text(stored.get("where") or {}),
                    "cells": cells,
                    "hidden": int(stored.get("hidden") or 0),
                }
            )
    elif location.state == "no_name_column":
        # 原文表格里没认出哪一列是企业名称：不说"原文里没有这一行"
        state = "no_column"
    else:
        state = "notfound"
    partial = evidence_source is not None and evidence_source.status == "partial"
    return {
        "entry": {
            "name": redact_sensitive(row.name),
            "category": row.category,
            "batch": row.batch,
            "region": row.region,
        },
        "document": {
            "id": str(document.id),
            "title": redact_sensitive(document.title),
            "status": list_status(document.title, kind),
            "issuer": redact_sensitive(normalize_issuer(source.dept_full_name)),
            "date": document.publish_date.isoformat() if document.publish_date else None,
            "region": region_label(source.province, source.level, source.city, source.district),
            # 只给网站名称和域名（和站内阅读的出处一样），不给原文链接
            "site": f"{source.dept_full_name}网站",
            "host": urlsplit(document.url).hostname or "",
            "link_gone": link_gone_date(document.link_gone_at),
        },
        "evidence": {
            "state": state,
            "rows": rows,
            "more": location is not None and location.state == "ambiguous",
            "partial": partial,
            "pagesRead": evidence_source.pages_read if partial else None,
            "reason": _EVIDENCE_REASONS.get(evidence_source.reason or "")
            if state == "failed" and evidence_source is not None
            else None,
        },
    }


# 首屏快照（10/12 冷启动诊断）：接口服务空闲后缩到 0 个实例，实测空闲 25 分钟后 67 秒才取到数据。
# admin 服务常驻 1 个实例、不会冷启动：小程序打开时先从它取首页、截止页默认视图、
# 查名单要用的公开数据，接口服务醒了再换成实时的。admin 上只认经小程序云调用来的请求
# （带 X-WX-SOURCE，见 app._role_guard），公网直接访问是 404，不对外开放数据接口（D-050）。
# 只有公开数据、不按用户区分：调首页时用一个不带请求头的空请求，认不出用户
# （不带"你关注的条件"那一栏）。
# 按北京时间日期缓存 60 秒，60 秒内直接给、不查数据库。
# S2（10/19）：同一时间只算一份。以前缓存一到期，同时来的请求各算一遍（线上 10/17 18:03 同一秒
# 3 个请求各算了 3.9–4.7 秒）；现在当场算的和后台刷新的共用一把锁，后来的等这一份算完直接用。
# 到期后 5 分钟内先给旧的、同时安排后台刷新一份（响应发出后才算，不拖慢这次请求）；给旧的之前
# 先核对下架版本，算好以后有文件下架或恢复就不给旧的。没有可用的旧的（刚启动、跨天、有下架或恢复、
# 旧的超过 5 分钟）才当场算。admin 固定 1 个实例，进程内的锁就够；接口服务上各实例各算各的。
# 值为 ((日期, 下架版本), 开始算的时刻, 快照)。快照不走 SessionDep（它会先查一次规则列）：
# 没有可用的旧的时，占到名额、拿到锁、真要算了才查。
# 当场算的时候同时等的请求最多 8 个（含正在算的），每个最多等 10 秒，多了或等久了回 503：等着的请求
# 占着线程池（同步接口共用默认 40 个），不能让首屏把管理台和其他接口挤住；名额在查数据库之前就占，
# 数据库慢的时候多出来的请求也不去查。小程序取快照只试一次，取不到就直接等实时数据，不提示。
_SNAPSHOT_CACHE: dict[str, tuple[tuple, float, dict[str, Any]]] = {}
SNAPSHOT_TTL = 60
SNAPSHOT_STALE = 300
SNAPSHOT_WAITERS = 8
SNAPSHOT_WAIT = 10.0
_SNAPSHOT_BUILD = Lock()  # 当场算和后台刷新共用：同一时间只算一份
_SNAPSHOT_STATE = Lock()
_snapshot_waiters = 0  # 当场算的和等着它的请求数（受 _SNAPSHOT_STATE 保护）
# 已安排、还没做完的后台刷新是什么时候安排的（monotonic）；没有为 None。
# 安排了超过 SNAPSHOT_STALE 秒还没做完的当作丢了（如响应没发出去），可以重新安排
_snapshot_refresh_at: float | None = None


def _snapshot_revision(session: Session) -> tuple:
    """快照的下架版本：下架、恢复的历史和归并（cache_revision），加上现在下架着的文件的条数和
    编号校验和（下架时间列有索引，只读下架着的几行）——不经管理台直接改库下架、恢复也能发现。"""
    hidden = session.execute(
        select(func.count(), func.coalesce(func.sum(func.crc32(Document.id)), 0)).where(
            Document.taken_down_at.is_not(None)
        )
    ).one()
    return (cache_revision(session), tuple(hidden))


def _cached_snapshot(max_age: float) -> tuple[tuple, float, dict[str, Any]] | None:
    """今天算的、不超过 max_age 秒的那份，没有返回 None。只看日期和时间，不查数据库。"""
    cached = _SNAPSHOT_CACHE.get("first")
    if (
        cached
        and cached[0][0] == now().astimezone(_TZ).date()
        and _time.monotonic() - cached[1] < max_age
    ):
        return cached
    return None


def _build_snapshot(session: Session) -> dict[str, Any]:
    """算一份快照存进缓存。调用方持有 _SNAPSHOT_BUILD。"""
    current = now()
    stamp = _time.monotonic()
    # 先取下架版本再算：算的时候有新下架，存下的键只会比数据旧，下次请求就重算
    key = (current.astimezone(_TZ).date(), _snapshot_revision(session))
    note_rules_complete(session)
    anonymous = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/home",
            "headers": [],
            "query_string": b"",
        }
    )
    first = home(anonymous, session, excerpts=1, group=1)
    first["items"] = search(session, kind="申报通知", time="不限", limit=8, offset=0, group=1)[
        "items"
    ]
    payload = {
        "generatedAt": current.astimezone(_TZ).isoformat(timespec="seconds"),
        "home": first,
        "deadlines": deadline_report(session, excerpts=1, days=7, page=1, size=20),
        "lists": recent_lists(session, limit=20, offset=0, group=1),
        "scope": list_board_report(session, days=30, page=1, size=1)["stats"],
    }
    _SNAPSHOT_CACHE["first"] = (key, stamp, payload)
    return payload


def _claim_snapshot_refresh() -> bool:
    """还没有在做的后台刷新时安排一个，返回 True；已经安排了返回 False。"""
    global _snapshot_refresh_at
    stamp = _time.monotonic()
    with _SNAPSHOT_STATE:
        if _snapshot_refresh_at is not None and stamp - _snapshot_refresh_at < SNAPSHOT_STALE:
            return False
        _snapshot_refresh_at = stamp
        return True


def _snapshot_wait(delta: int) -> bool:
    """当场算或等着的请求数加减 1；加的时候已经满了返回 False（不加）。"""
    global _snapshot_waiters
    with _SNAPSHOT_STATE:
        if delta > 0 and _snapshot_waiters >= SNAPSHOT_WAITERS:
            return False
        _snapshot_waiters += delta
        return True


def _refresh_snapshot(bind) -> None:
    """后台刷新：响应发出后在线程池里跑，用自己的数据库会话（请求的会话那时已经关了）。"""
    global _snapshot_refresh_at
    try:
        with Session(bind=bind, expire_on_commit=False) as session, _SNAPSHOT_BUILD:
            # 等锁的时候可能已经有请求当场算好了
            if _cached_snapshot(SNAPSHOT_TTL) is None:
                _build_snapshot(session)
    except Exception:
        # 刷新失败：旧的照样给到 5 分钟，之后的请求当场算，出错照常报错
        log.exception("首屏快照后台刷新失败")
    finally:
        with _SNAPSHOT_STATE:
            _snapshot_refresh_at = None


@router.get("/snapshot")
def snapshot(
    session: Annotated[Session, Depends(get_session)], background: BackgroundTasks
) -> dict[str, Any]:
    """首屏快照：home（同 /home?group=1&excerpts=1，列表换成最新 8 份申报通知，不带机器摘录）、
    deadlines（同 /reports/deadlines?days=7&excerpts=1）、lists（同 /lists/recent?group=1）、
    scope（同 /reports/lists 的 stats）。"""
    fresh = _cached_snapshot(SNAPSHOT_TTL)
    if fresh is not None:
        return fresh[2]
    stale = _cached_snapshot(SNAPSHOT_STALE)
    if stale is not None and stale[0][1] == _snapshot_revision(session):
        if _claim_snapshot_refresh():
            background.add_task(_refresh_snapshot, session.get_bind())
        return stale[2]
    # 等锁前先结束只读事务、把连接还给连接池：同时等的请求多时不把连接池占满
    session.rollback()
    busy = HTTPException(status_code=503, detail="首屏数据正在生成，请稍后再试")
    if not _snapshot_wait(1):
        raise busy
    try:
        if not _SNAPSHOT_BUILD.acquire(timeout=SNAPSHOT_WAIT):
            raise busy
        try:
            # 等锁的时候别的请求或后台刷新可能已经算好了，直接用
            fresh = _cached_snapshot(SNAPSHOT_TTL)
            if fresh is not None:
                return fresh[2]
            return _build_snapshot(session)
        finally:
            _SNAPSHOT_BUILD.release()
    finally:
        _snapshot_wait(-1)
