"""多客户在报项目跟进表后台引擎。

这里只处理内部批量任务：读取客户画像、按截止雷达的公开口径查询，生成结果并导出 Excel。
客户画像不用于推断行业；关键词和支持方式都是用户明确填写的筛选条件。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
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
    deadline_date: date
    deadline_time: time | None
    remaining_days: int
    support: str
    url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "customer_name": self.customer_name,
            "title": self.title,
            "issuer": self.issuer,
            "province": self.province,
            "deadline_type": self.deadline_type,
            "deadline_date": self.deadline_date.isoformat(),
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


def read_customer_profiles(path: str | Path) -> list[CustomerProfile]:
    """读取客户画像 xlsx/csv；省份不在固定清单时保留该行但标记为不查询。"""

    rows = _file_rows(Path(path))
    if not rows:
        return []
    header = rows[0]
    columns = {_header_key(value): index for index, value in enumerate(header)}
    required = {"客户名称", "省份"}
    if not required.issubset(columns):
        raise CompareInputError("客户画像必须包含“客户名称”和“省份”表头")
    if len(rows) - 1 > MAX_INPUT_ROWS:
        raise CompareInputError("客户画像最多 200 行")
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


def _deadline_text(value: date, clock: time | None) -> str:
    return f"{value.isoformat()} {clock:%H:%M}" if clock else value.isoformat()


def track_clients(
    session: Session,
    profiles: Iterable[CustomerProfile | Mapping[str, object]],
) -> TrackReport:
    """按客户画像批量查询在报项目，查询只做一次，适合 200 客户。"""

    customers = [_profile(item) for item in profiles]
    if len(customers) > MAX_INPUT_ROWS:
        raise CompareInputError("客户画像最多 200 行")
    customers.sort(key=lambda client: client.name)
    stamp = now()
    today = stamp.date()
    valid = [client for client in customers if client.valid]
    if not valid:
        return TrackReport([CustomerTrack(client) for client in customers], stamp)

    candidate_ids = v1._future_approved_canonical_ids(session, today)
    statement = v1._base().where(Document.id.in_(candidate_ids), v1._kind_is(["申报通知"]))
    rows = session.execute(statement).all()
    selected_ids = [document.id for document, _ in rows]
    approved = v1._approved(session, selected_ids)
    details = v1._details(session, selected_ids)
    fields = v1._approved_fields(session, selected_ids)
    notices: list[tuple[Document, Source, DocumentDetail | None, Any, tuple[str, ...], int]] = []
    for document, source in rows:
        deadline = v1._selected_deadline(approved.get(document.id, []))
        if deadline is None or deadline.approved_date is None:
            continue
        status = v1.application_status([deadline], stamp)
        if not status or status["code"] == "closed":
            continue
        remaining = (deadline.approved_date - today).days
        # 跟进表列出全部未截止项目（不限 30 天）；30 天内的部分与截止雷达同口径，A2 按此核对。
        if remaining < 0:
            continue
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
                    deadline_type=deadline.kind,
                    deadline_date=deadline.approved_date,
                    deadline_time=deadline.approved_time,
                    remaining_days=remaining,
                    support="、".join(supports),
                    url=document.url,
                )
            )
        items.sort(
            key=lambda item: (
                item.deadline_date,
                item.deadline_time is None,
                item.deadline_time or time.min,
                item.document_id,
            )
        )
        result.append(CustomerTrack(client, items))
    return TrackReport(result, stamp)


def export_track_excel(report: TrackReport, output: str | Path) -> int:
    """导出“跟进表”和“汇总”，返回跟进表数据行数。"""

    book = Workbook()
    detail = book.active
    detail.title = "跟进表"
    note = (
        "第三方整理 · 非政府平台；只含人工核对过的截止日期，以原文为准；"
        f"数据截至 {_beijing_minute(report.generated_at)}"
    )
    headers = [
        "客户名称",
        "通知标题",
        "发文机关",
        "省份",
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
    summary_rows: list[tuple[str, int, int, int, int]] = []
    for client in report.clients:
        count = len(client.items)
        summary_rows.append(
            (
                client.customer_name,
                count,
                sum(item.remaining_days <= 7 for item in client.items),
                sum(item.remaining_days <= 14 for item in client.items),
                sum(item.remaining_days <= 30 for item in client.items),
            )
        )
        items = client.items or [None]
        for item in items:
            values = [
                client.customer_name,
                item.title if item else "暂无",
                item.issuer if item else "",
                item.province if item else (client.profile.province or ""),
                item.deadline_type if item else "",
                _deadline_text(item.deadline_date, item.deadline_time) if item else "",
                item.remaining_days if item else "",
                item.support if item else "",
                item.url if item else "",
            ]
            if item is None and client.profile.invalid_province:
                values[3] = f"{client.profile.province}（不支持，未查询）"
            for column, value in enumerate(values, 1):
                detail.cell(row_count + 3, column, _safe_cell(value))
            row_count += 1

    summary = book.create_sheet("汇总")
    summary_headers = ["客户名称", "项目数", "7天内截止数", "14天内截止数", "30天内截止数"]
    summary.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(summary_headers))
    summary.cell(1, 1, _safe_cell(note))
    for column, header in enumerate(summary_headers, 1):
        summary.cell(2, column, header)
    for row_number, values in enumerate(summary_rows, 3):
        for column, value in enumerate(values, 1):
            summary.cell(row_number, column, _safe_cell(value))
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
