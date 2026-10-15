"""客户相关通知表（原名“在报项目跟进表”）的后台引擎。

读取客户画像，查询近 90 天发布且没有核对为已截止的申报通知，生成结果并导出 Excel。
客户画像不用于推断行业；关键词和支持方式都是用户明确填写的筛选条件。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from youju import deadline_excerpts
from youju.api import v1
from youju.compare import (
    MAX_INPUT_ROWS,
    CompareInputError,
    _beijing_minute,
    _file_rows,
    _header_key,
    _safe_cell,
    _text,
)
from youju.export_notes import DEADLINE_HEADER, add_excel_notes
from youju.fields import SUPPORT_LABELS
from youju.models import Document, DocumentDetail, Source
from youju.regions import (
    checked_area_name,
    city_key,
    covered_city_scopes,
    covered_district_scopes,
    district_parent_city,
    resolve_city,
    resolve_district,
)
from youju.sources.onboard import PROVINCES as SOURCE_PROVINCES

_TZ = v1._TZ
_PROVINCES = frozenset((*SOURCE_PROVINCES, "国家部委"))
_PROVINCE_TO_SOURCE = {"国家部委": "国家"}
_PROVINCE_ALIASES = {
    f"{p}省": p
    for p in SOURCE_PROVINCES
    if p not in {"国家", "北京", "天津", "上海", "重庆", "内蒙古", "广西", "西藏", "宁夏", "新疆"}
}
_PROVINCE_ALIASES.update({f"{p}市": p for p in ("北京", "天津", "上海", "重庆")})
_PROVINCE_ALIASES.update(
    {
        "内蒙古自治区": "内蒙古",
        "广西壮族自治区": "广西",
        "西藏自治区": "西藏",
        "宁夏回族自治区": "宁夏",
        "新疆维吾尔自治区": "新疆",
    }
)


_SPLIT = re.compile(r"[;,；，、|]+")


@dataclass(frozen=True)
class CustomerProfile:
    """一行客户画像；invalid_province 为真时不会访问数据库查询项目。"""

    name: str
    province: str
    keywords: tuple[str, ...] = ()
    supports: tuple[str, ...] = ()
    invalid_province: bool = False
    city: str = ""
    invalid_city: bool = False
    district: str = ""
    invalid_district: bool = False

    def __post_init__(self) -> None:
        province = _PROVINCE_ALIASES.get(self.province.strip(), self.province.strip())
        object.__setattr__(self, "province", province)
        try:
            district = checked_area_name(self.district, "区县")
        except ValueError as exc:
            raise CompareInputError(str(exc)) from None
        object.__setattr__(self, "district", district)
        if district and (not self.city or province in ("国家", "国家部委")):
            raise CompareInputError("填写区县时必须提供所属省份和城市")
        if isinstance(self.city, str):
            city = self.city.strip()
            if province in ("北京", "天津", "上海", "重庆") and city_key(city) == province:
                city = province if district else ""
            object.__setattr__(
                self, "city", district_parent_city(province, city) if district else city
            )
        if district and not self.city:
            raise CompareInputError("填写区县时必须提供所属省份和城市")
        if self.city and (
            not isinstance(self.city, str)
            or len(self.city) > 32
            or any(c in self.city for c in "/\\,，;；、|\t\n\r")
            or self.province in ("国家", "国家部委")
        ):
            raise CompareInputError("城市须填写所属省份内的城市简称，国家部委范围请留空")
        if self.province not in _PROVINCES:
            object.__setattr__(self, "invalid_province", True)

    @property
    def valid(self) -> bool:
        return bool(
            self.name
            and self.province
            and not self.invalid_province
            and not self.invalid_city
            and not self.invalid_district
        )


@dataclass(frozen=True)
class TrackItem:
    document_id: int
    customer_name: str
    title: str
    issuer: str
    province: str
    deadline_type: str
    deadline_date: date | None
    deadline_time: time | None
    remaining_days: int | None
    support: str
    url: str
    publish_date: date | None = None
    deadline_source: str = "unverified"
    deadline_quote: str = ""
    city: str = ""
    level: str = ""
    district: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "customer_name": self.customer_name,
            "title": self.title,
            "issuer": self.issuer,
            "province": self.province,
            "city": self.city,
            "district": self.district,
            "level": self.level,
            "publish_date": self.publish_date.isoformat() if self.publish_date else None,
            "deadline_type": self.deadline_type,
            "deadline_date": self.deadline_date.isoformat() if self.deadline_date else None,
            "deadline_status": (
                VERIFIED_TEXT
                if self.deadline_source == "verified"
                else EXCERPT_TEXT
                if self.deadline_source == "excerpt"
                else UNVERIFIED_TEXT
            ),
            "deadline_quote": self.deadline_quote,
            "deadline_time": self.deadline_time.strftime("%H:%M") if self.deadline_time else None,
            "remaining_days": self.remaining_days,
            "support": self.support,
            "url": self.url,
        }


@dataclass
class CustomerTrack:
    profile: CustomerProfile
    items: list[TrackItem] = field(default_factory=list)

    @property
    def customer_name(self) -> str:
        return self.profile.name

    @property
    def matches(self) -> list[TrackItem]:
        return self.items


@dataclass
class TrackReport:
    clients: list[CustomerTrack]
    generated_at: datetime

    @property
    def items(self) -> list[TrackItem]:
        return [item for client in self.clients for item in client.items]

    @property
    def rows(self) -> list[CustomerTrack]:
        return self.clients

    @property
    def results(self) -> list[CustomerTrack]:
        return self.clients

    @property
    def detail_row_count(self) -> int:
        return sum(max(1, len(client.items)) for client in self.clients)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "clients": [
                {
                    "customer_name": client.customer_name,
                    "province": client.profile.province,
                    "city": client.profile.city,
                    "district": client.profile.district,
                    "invalid_district": client.profile.invalid_district,
                    "invalid_city": client.profile.invalid_city,
                    "keywords": list(client.profile.keywords),
                    "supports": list(client.profile.supports),
                    "invalid_province": client.profile.invalid_province,
                    "items": [item.as_dict() for item in client.items],
                }
                for client in self.clients
            ],
        }


def now() -> datetime:
    """业务时间按北京时间；测试可替换本模块函数。"""

    return v1.now().astimezone(_TZ)


def _parts(value: object) -> tuple[str, ...]:
    if isinstance(value, (tuple, list, set)):
        return tuple(part for part in (_text(item).strip() for item in value) if part)
    return tuple(part for part in (_text(v).strip() for v in _SPLIT.split(_text(value))) if part)


def parse_customer_profile(value: CustomerProfile | Mapping[str, object]) -> CustomerProfile:
    """文件、JSON 预览和后台任务共用单行解析，非法城市不得留到扣额度后才发现。"""
    if isinstance(value, CustomerProfile):
        return value
    if not isinstance(value, Mapping):
        raise CompareInputError("客户画像必须是 CustomerProfile 或字典")
    name = _text(value.get("客户名称", value.get("name", "")))
    province = _text(value.get("省份", value.get("province", "")))
    raw_city = value.get("城市", value.get("city", ""))
    if raw_city is not None and not isinstance(raw_city, str):
        raise CompareInputError("城市须填写文字")
    city = _text(raw_city)
    try:
        district = checked_area_name(value.get("区县", value.get("district", "")), "区县")
    except ValueError as exc:
        raise CompareInputError(str(exc)) from None
    keywords = _parts(value.get("关键词", value.get("keywords", "")))
    raw_supports = _parts(value.get("支持方式", value.get("supports", value.get("support", ""))))
    supports = tuple(item for item in raw_supports if item in SUPPORT_LABELS)
    return CustomerProfile(
        name=name,
        province=province,
        city=city,
        district=district,
        keywords=keywords,
        supports=supports,
    )


def validate_customer_rows(rows: Iterable[Mapping[str, object]]) -> None:
    """在提交前逐行验证；错误只给位置与原因，不回显客户内容。"""
    for row_number, row in enumerate(rows, 1):
        try:
            parse_customer_profile(row)
        except CompareInputError as exc:
            raise CompareInputError(f"第 {row_number} 行：{exc}") from None


def read_customer_profiles(
    path: str | Path, *, content: bytes | None = None
) -> list[CustomerProfile]:
    """读取客户画像 xlsx/csv；省份不在固定清单时保留该行但标记为不查询。"""

    rows = _file_rows(Path(path), content=content)
    if not rows:
        return []
    header = rows[0]
    columns = {_header_key(value): index for index, value in enumerate(header)}
    required = {"客户名称", "省份"}
    if not required.issubset(columns):
        raise CompareInputError("客户画像必须包含“客户名称”和“省份”表头")
    if len(rows) - 1 > MAX_INPUT_ROWS:
        raise CompareInputError("客户画像最多 500 行")
    result: list[CustomerProfile] = []
    for row_number, row in enumerate(rows[1:], 2):
        name_index, province_index = columns["客户名称"], columns["省份"]
        name = _text(row[name_index] if name_index < len(row) else "")
        province = _text(row[province_index] if province_index < len(row) else "")
        if not name and not province:
            continue
        keyword = (
            row[columns["关键词"]] if "关键词" in columns and columns["关键词"] < len(row) else ""
        )
        support = (
            row[columns["支持方式"]]
            if "支持方式" in columns and columns["支持方式"] < len(row)
            else ""
        )
        values = {
            "name": name,
            "province": province,
            "city": row[columns["城市"]]
            if "城市" in columns and columns["城市"] < len(row)
            else "",
            "district": row[columns["区县"]]
            if "区县" in columns and columns["区县"] < len(row)
            else "",
            "keywords": _parts(keyword),
            "supports": _parts(support),
        }
        try:
            result.append(parse_customer_profile(values))
        except CompareInputError as exc:
            raise CompareInputError(f"第 {row_number} 行：{exc}") from None
    return result


def _source_province(profile: CustomerProfile) -> str:
    return _PROVINCE_TO_SOURCE.get(profile.province, profile.province)


def _keyword_match(title: str, keywords: tuple[str, ...]) -> bool:
    return not keywords or any(keyword in title for keyword in keywords)


def _province_match(profile: CustomerProfile, source: Source) -> bool:
    # 国家部委通知对所有客户可见；客户填“国家部委”时只看国家部委通知。
    if source.province == "国家":
        return True
    if profile.province == "国家部委" or source.province != _source_province(profile):
        return False
    # 填城市时仍保留本省省级通知；市级只匹配该市，缺失城市的市级来源不猜测。
    return (
        not profile.city
        or source.level == "省级"
        or (source.level == "市级" and city_key(source.city or "") == city_key(profile.city))
        or (
            profile.district
            and source.level == "区县级"
            and source.city == profile.city
            and source.district == profile.district
        )
    )


VERIFIED_TEXT = "已人工核对"
EXCERPT_TEXT = "机器摘录，未经人工核对"
UNVERIFIED_TEXT = "未核对，请以原文为准"
UNVERIFIED_SUMMARY = "未核对"


def _deadline_text(value: date, clock: time | None) -> str:
    return f"{value.isoformat()} {clock:%H:%M}" if clock else value.isoformat()


def track_clients(
    session: Session,
    profiles: Iterable[CustomerProfile | Mapping[str, object]],
) -> TrackReport:
    """按客户画像批量查询在报项目，查询只做一次，适合 200 客户。"""

    customers = [parse_customer_profile(item) for item in profiles]
    if len(customers) > MAX_INPUT_ROWS:
        raise CompareInputError("客户画像最多 500 行")
    if any(client.city and not client.invalid_province for client in customers):
        covered = covered_city_scopes(session)
        district_scopes = covered_district_scopes(session)
        normalized = []
        for client in customers:
            if client.district:
                resolved = resolve_district(
                    client.province, client.city, client.district, district_scopes
                )
                normalized.append(
                    replace(client, city=resolved[0], district=resolved[1])
                    if resolved
                    else replace(client, invalid_district=True)
                )
                continue
            if not client.city or (client.province, client.city) in covered:
                normalized.append(client)
                continue
            city = resolve_city(client.province, client.city, covered)
            normalized.append(
                replace(client, city=city)
                if city is not None
                else replace(client, invalid_city=True)
            )
        customers = normalized
    customers.sort(key=lambda client: client.name)
    stamp = now()
    today = stamp.date()
    valid = [client for client in customers if client.valid]
    if not valid:
        return TrackReport([CustomerTrack(client) for client in customers], stamp)

    statement = v1._base().where(
        v1._kind_is(["申报通知"]),
        Document.publish_date >= today - timedelta(days=90),
        Document.publish_date <= today,
        Document.id.not_in(v1._application_ids(session, {"closed"})),
    )
    rows = session.execute(statement).all()
    selected_ids = [document.id for document, _ in rows]
    approved = v1._approved(session, selected_ids)
    views, _, _ = deadline_excerpts.batch(session, selected_ids, approved, today)
    details = v1._details(session, selected_ids)
    fields = v1._approved_fields(session, selected_ids)
    notices: list[
        tuple[Document, Source, DocumentDetail | None, Any, tuple[str, ...], int | None]
    ] = []
    for document, source in rows:
        deadline = deadline_excerpts.selected(views.get(document.id, []))
        status = v1.application_status(approved.get(document.id, []), stamp)
        if status and status["code"] == "closed":
            continue
        remaining = (deadline.day - today).days if deadline and deadline.day else None
        supports = tuple(fields.get(document.id, {}).get("support", ()))
        detail = details.get(document.id)
        notices.append((document, source, detail, deadline, supports, remaining))

    result: list[CustomerTrack] = []
    for client in customers:
        if not client.valid:
            result.append(CustomerTrack(client))
            continue
        items: list[TrackItem] = []
        for document, source, detail, deadline, supports, remaining in notices:
            if not _province_match(client, source) or not _keyword_match(
                document.title, client.keywords
            ):
                continue
            if client.supports and not set(client.supports).intersection(supports):
                continue
            items.append(
                TrackItem(
                    document_id=document.id,
                    customer_name=client.name,
                    title=document.title,
                    issuer=(detail.issuer if detail and detail.issuer else source.dept_full_name),
                    province="国家部委" if source.province == "国家" else source.province,
                    city=source.city or "",
                    district=source.district or "",
                    level=source.level,
                    deadline_type=deadline.kind if deadline else "",
                    deadline_date=deadline.day if deadline else None,
                    deadline_time=deadline.clock if deadline else None,
                    remaining_days=remaining,
                    support="、".join(supports),
                    url=document.url,
                    publish_date=document.publish_date,
                    deadline_source=deadline.source if deadline else "unverified",
                    deadline_quote=deadline.quote or "" if deadline else "",
                )
            )
        # 核对过截止日期的排前面、截止早的先；没核对的排后面、新发布的先
        items.sort(
            key=lambda item: (
                item.deadline_date or date.max,
                item.deadline_time is None,
                item.deadline_time or time.min,
                -(item.publish_date.toordinal() if item.publish_date else 0),
                item.document_id,
            )
        )
        result.append(CustomerTrack(client, items))
    return TrackReport(result, stamp)


def _scope_note(profile: CustomerProfile) -> str:
    if profile.invalid_province:
        return "省份不支持，未查询"
    if profile.invalid_district:
        return "区县未收录或不属于所填省市，未查询；不会改为全市查询"
    if profile.district:
        return "含国家部委、本省省级及本市市级通知；区县级仅所填区县"
    if profile.invalid_city:
        return "城市未收录或不属于所填省份，未查询"
    return (
        "含国家部委及本省省级通知；市级仅所填城市" if profile.city else "未指定城市，按省份范围查询"
    )


def export_track_excel(report: TrackReport, output: str | Path) -> int:
    """导出“客户相关通知”和“汇总”两页，返回通知页的数据行数。"""

    book = Workbook()
    detail = book.active
    detail.title = "客户相关通知"
    note = (
        "第三方整理 · 非政府平台；近 90 天发布的申报通知；"
        f"“{DEADLINE_HEADER}”列中标“机器摘录”的未经人工核对；"
        f"数据截至 {_beijing_minute(report.generated_at)}"
    )
    # 截止日期一列只写日期（原文写了具体时间的带上时间）。没核对的留空，在“截止核对情况”里写明：
    # 免得把说明文字当成日期，也免得把“没核对”看成“没有截止”（10/12 用户视角评审）
    headers = [
        "客户名称",
        "通知标题",
        "发文机关",
        "省份",
        "发布日期",
        "截止核对情况",
        "截止类型",
        DEADLINE_HEADER,
        "截止原文句子",
        "剩余天数",
        "支持方式",
        "原文链接",
        "城市",
        "层级",
        "客户省份",
        "客户城市",
        "查询说明",
        "区县",
        "客户区县",
    ]
    detail.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    detail.cell(1, 1, _safe_cell(note))
    for column, header in enumerate(headers, 1):
        detail.cell(2, column, header)
    deadline_column = get_column_letter(headers.index(DEADLINE_HEADER) + 1)
    detail.column_dimensions[deadline_column].width = 28
    row_count = 0
    summary_rows: list[tuple[object, ...]] = []
    for client in report.clients:
        count = len(client.items)
        verified = sum(item.deadline_source == "verified" for item in client.items)

        dated = sum(item.deadline_source in ("verified", "excerpt") for item in client.items)

        def within(days: int, items=client.items, verified=verified, dated=dated) -> object:
            if not items:
                return ""
            # 能看到日期的一条都没有时不写 0：0 会被看成“没有快截止的”，其实是不知道。
            # 7 天内只认人工核对过的（D-061），14、30 天另含机器摘录的
            if not (verified if days <= 7 else dated):
                return UNVERIFIED_SUMMARY
            return sum(
                item.remaining_days is not None
                and item.remaining_days <= days
                and (days > 7 or item.deadline_source == "verified")
                for item in items
            )

        summary_rows.append(
            (
                client.customer_name,
                count,
                verified,
                within(7),
                within(14),
                within(30),
                count - verified,
                client.profile.province,
                client.profile.city,
                _scope_note(client.profile),
                client.profile.district,
            )
        )
        items = client.items or [None]
        for item in items:
            values = [
                client.customer_name,
                item.title if item else "暂无",
                item.issuer if item else "",
                item.province if item else (client.profile.province or ""),
                item.publish_date.isoformat() if item and item.publish_date else "",
                (
                    VERIFIED_TEXT
                    if item.deadline_source == "verified"
                    else EXCERPT_TEXT
                    if item.deadline_source == "excerpt"
                    else UNVERIFIED_TEXT
                )
                if item
                else "",
                item.deadline_type if item else "",
                _deadline_text(item.deadline_date, item.deadline_time)
                if item and item.deadline_date
                else "",
                item.deadline_quote if item else "",
                item.remaining_days if item and item.remaining_days is not None else "",
                item.support if item else "",
                item.url if item else "",
                item.city if item else "",
                item.level if item else "",
                client.profile.province,
                client.profile.city,
                _scope_note(client.profile),
                item.district if item else "",
                client.profile.district,
            ]
            if item is None and client.profile.invalid_province:
                values[3] = f"{client.profile.province}（不支持，未查询）"
            for column, value in enumerate(values, 1):
                detail.cell(row_count + 3, column, _safe_cell(value))
            row_count += 1

    summary = book.create_sheet("汇总")
    summary_headers = [
        "客户名称",
        "通知数",
        "人工核对过截止日期的",
        "7 天内截止",
        "14 天内截止",
        "30 天内截止",
        "截止未核对的",
        "客户省份",
        "客户城市",
        "查询说明",
        "客户区县",
    ]
    summary.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(summary_headers))
    summary.cell(
        1,
        1,
        _safe_cell(
            note + "；“7 天内截止”只数人工核对过的，“14 天内”“30 天内”另含机器摘录的；"
            "“截止未核对的”也可能快截止，请看原文"
        ),
    )
    for column, header in enumerate(summary_headers, 1):
        summary.cell(2, column, header)
    for row_number, values in enumerate(summary_rows, 3):
        for column, value in enumerate(values, 1):
            summary.cell(row_number, column, _safe_cell(value))
    add_excel_notes(book.create_sheet("数据说明"))
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    book.save(output_path)
    return row_count


# 与名单比对引擎保持相同的调用习惯，便于批处理脚本复用。
read_customer_file = read_customer_profiles
load_customer_file = read_customer_profiles
read_client_profiles = read_customer_profiles
track = track_clients
generate_track = track_clients
export_excel = export_track_excel
write_excel = export_track_excel
export_client_track_excel = export_track_excel

__all__ = [
    "CustomerProfile",
    "CustomerTrack",
    "TrackItem",
    "TrackReport",
    "read_customer_profiles",
    "track_clients",
    "export_track_excel",
    "read_customer_file",
    "load_customer_file",
    "read_client_profiles",
    "track",
    "generate_track",
    "export_excel",
    "write_excel",
    "export_client_track_excel",
]
