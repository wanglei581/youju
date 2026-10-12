"""客户相关通知表（原名“在报项目跟进表”）的后台引擎。

读取客户画像，查询近 90 天发布且没有核对为已截止的申报通知，生成结果并导出 Excel。
客户画像不用于推断行业；关键词和支持方式都是用户明确填写的筛选条件。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from sqlalchemy.orm import Session

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
from youju.export_notes import add_excel_notes
from youju.fields import SUPPORT_LABELS
from youju.models import Document, DocumentDetail, Source
from youju.sources.onboard import PROVINCES as SOURCE_PROVINCES

_TZ = v1._TZ
_PROVINCES = frozenset((*SOURCE_PROVINCES, "国家部委"))
_PROVINCE_TO_SOURCE = {"国家部委": "国家"}
_SPLIT = re.compile(r"[;,；，、|]+")


@dataclass(frozen=True)
class CustomerProfile:
    """一行客户画像；invalid_province 为真时不会访问数据库查询项目。"""

    name: str
    province: str
    keywords: tuple[str, ...] = ()
    supports: tuple[str, ...] = ()
    invalid_province: bool = False

    def __post_init__(self) -> None:
        if self.province not in _PROVINCES:
            object.__setattr__(self, "invalid_province", True)

    @property
    def valid(self) -> bool:
        return bool(self.name and self.province and not self.invalid_province)


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "customer_name": self.customer_name,
            "title": self.title,
            "issuer": self.issuer,
            "province": self.province,
            "publish_date": self.publish_date.isoformat() if self.publish_date else None,
            "deadline_type": self.deadline_type,
            "deadline_date": self.deadline_date.isoformat() if self.deadline_date else None,
            "deadline_status": "已核对" if self.deadline_date else "截止未核对，请以原文为准",
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


def _profile(value: CustomerProfile | Mapping[str, object]) -> CustomerProfile:
    if isinstance(value, CustomerProfile):
        return value
    if not isinstance(value, Mapping):
        raise CompareInputError("客户画像必须是 CustomerProfile 或字典")
    name = _text(value.get("客户名称", value.get("name", "")))
    province = _text(value.get("省份", value.get("province", "")))
    keywords = _parts(value.get("关键词", value.get("keywords", "")))
    raw_supports = _parts(value.get("支持方式", value.get("supports", value.get("support", ""))))
    supports = tuple(item for item in raw_supports if item in SUPPORT_LABELS)
    return CustomerProfile(
        name=name,
        province=province,
        keywords=keywords,
        supports=supports,
        invalid_province=province not in _PROVINCES,
    )


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
    for row in rows[1:]:
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
            "keywords": _parts(keyword),
            "supports": _parts(support),
        }
        result.append(_profile(values))
    return result


def _source_province(profile: CustomerProfile) -> str:
    return _PROVINCE_TO_SOURCE.get(profile.province, profile.province)


def _keyword_match(title: str, keywords: tuple[str, ...]) -> bool:
    return not keywords or any(keyword in title for keyword in keywords)


def _province_match(profile: CustomerProfile, source: Source) -> bool:
    # 国家部委通知对所有客户可见；客户填“国家部委”时只看国家部委通知。
    if source.province == "国家":
        return True
    return profile.province != "国家部委" and source.province == _source_province(profile)


VERIFIED_TEXT = "已核对"
UNVERIFIED_TEXT = "未核对，请以原文为准"
UNVERIFIED_SUMMARY = "未核对"


def _deadline_text(value: date, clock: time | None) -> str:
    return f"{value.isoformat()} {clock:%H:%M}" if clock else value.isoformat()


def track_clients(
    session: Session,
    profiles: Iterable[CustomerProfile | Mapping[str, object]],
) -> TrackReport:
    """按客户画像批量查询在报项目，查询只做一次，适合 200 客户。"""

    customers = [_profile(item) for item in profiles]
    if len(customers) > MAX_INPUT_ROWS:
        raise CompareInputError("客户画像最多 500 行")
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
    details = v1._details(session, selected_ids)
    fields = v1._approved_fields(session, selected_ids)
    notices: list[
        tuple[Document, Source, DocumentDetail | None, Any, tuple[str, ...], int | None]
    ] = []
    for document, source in rows:
        deadline = v1._selected_deadline(approved.get(document.id, []))
        status = v1.application_status(approved.get(document.id, []), stamp)
        if status and status["code"] == "closed":
            continue
        remaining = (
            (deadline.approved_date - today).days if deadline and deadline.approved_date else None
        )
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
                    deadline_type=deadline.kind if deadline else "",
                    deadline_date=deadline.approved_date if deadline else None,
                    deadline_time=deadline.approved_time if deadline else None,
                    remaining_days=remaining,
                    support="、".join(supports),
                    url=document.url,
                    publish_date=document.publish_date,
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


def export_track_excel(report: TrackReport, output: str | Path) -> int:
    """导出“客户相关通知”和“汇总”两页，返回通知页的数据行数。"""

    book = Workbook()
    detail = book.active
    detail.title = "客户相关通知"
    note = (
        "第三方整理 · 非政府平台；近 90 天发布的申报通知；"
        "截止日期只写人工核对过的，没核对的请以原文为准；"
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
        "截止日期",
        "剩余天数",
        "支持方式",
        "原文链接",
    ]
    detail.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    detail.cell(1, 1, _safe_cell(note))
    for column, header in enumerate(headers, 1):
        detail.cell(2, column, header)
    row_count = 0
    summary_rows: list[tuple[object, ...]] = []
    for client in report.clients:
        count = len(client.items)
        verified = sum(item.deadline_date is not None for item in client.items)

        def within(days: int, items=client.items, verified=verified) -> object:
            # 一条都没核对时不写 0：0 会被看成“没有快截止的”，其实是不知道
            if not verified:
                return UNVERIFIED_SUMMARY if items else ""
            return sum(
                item.remaining_days is not None and item.remaining_days <= days for item in items
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
                (VERIFIED_TEXT if item.deadline_date else UNVERIFIED_TEXT) if item else "",
                item.deadline_type if item else "",
                _deadline_text(item.deadline_date, item.deadline_time)
                if item and item.deadline_date
                else "",
                item.remaining_days if item and item.remaining_days is not None else "",
                item.support if item else "",
                item.url if item else "",
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
        "已核对截止日期的",
        "7 天内截止",
        "14 天内截止",
        "30 天内截止",
        "截止未核对的",
    ]
    summary.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(summary_headers))
    summary.cell(
        1,
        1,
        _safe_cell(
            note + "；“N 天内截止”只数已核对截止日期的通知，“截止未核对的”也可能快截止，请看原文"
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
