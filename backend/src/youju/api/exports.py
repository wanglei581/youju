"""把公开题录与人工核对字段导出为小程序可直接打开的文件。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from docx import Document as WordDocument
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.classify import doc_kind, list_status, region_label
from youju.db import get_session
from youju.dedup import canonical_ids, groups_for
from youju.models import Document, DocumentDeadline, DocumentDetail, Source

router = APIRouter(prefix="/api/v1", tags=["小程序"])
SessionDep = Annotated[Session, Depends(get_session)]
TZ = ZoneInfo("Asia/Shanghai")
MAX_BYTES = 1000 * 1024
HEADERS = ("序号", "标题", "类型", "名单状态", "发文机关", "文号", "发布日期", "地区", "截止日期")
NOTICE = (
    "本文件由第三方整理，非政府平台；截止日期只列人工核对通过的记录，"
    "请以发文机关原文为准；名单状态仅按标题用词标注。"
)


class ExportIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    format: Literal["xlsx", "docx"]


@dataclass
class ExportItem:
    number: int
    doc_id: int
    title: str
    kind: str
    status: str
    issuer: str
    doc_number: str
    published: str
    region: str
    deadline: str
    urls: list[str]
    sites: str
    source_checked: str
    deadline_checked: str
    change_checked: str


def _date_time(value: datetime | None) -> str:
    return value.astimezone(TZ).strftime("%Y-%m-%d") if value else ""


def _deadline_text(d: DocumentDeadline) -> str:
    day = d.approved_date.isoformat()
    clock = f" {d.approved_time.strftime('%H:%M')}" if d.approved_time else ""
    return f"{d.kind} {day}{clock}"


def _is_web_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _items(session: Session, raw_ids: list[str]) -> list[ExportItem]:
    valid: list[tuple[str, int]] = []
    invalid: list[str] = []
    for raw in raw_ids:
        if not re.fullmatch(r"[1-9][0-9]*", raw) or len(raw) > 19 or int(raw) > 2**63 - 1:
            invalid.append(raw)
        else:
            valid.append((raw, int(raw)))
    parsed = [doc_id for _, doc_id in valid]
    found = (
        set(session.scalars(select(Document.id).where(Document.id.in_(parsed))))
        if parsed
        else set()
    )
    invalid.extend(raw for raw, doc_id in valid if doc_id not in found)
    if invalid:
        invalid_set = set(invalid)
        raise HTTPException(
            status_code=422,
            detail={
                "不存在的文件编号": list(dict.fromkeys(i for i in raw_ids if i in invalid_set))
            },
        )

    masters = canonical_ids(session, parsed)
    ordered = list(dict.fromkeys(masters.get(doc_id, doc_id) for doc_id in parsed))
    rows = session.execute(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id.in_(ordered))
    ).all()
    by_id = {doc.id: (doc, source) for doc, source in rows}
    groups = groups_for(session, ordered)
    member_master = {member: master for master, members in groups.items() for member in members}
    group_rows = session.execute(
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id.in_(member_master))
        .order_by(Document.id)
    ).all()
    members: dict[int, list[tuple[Document, Source]]] = {master: [] for master in ordered}
    for doc, source in group_rows:
        members[member_master[doc.id]].append((doc, source))

    # 正文不参与导出；只在数据库计算长度以沿用详情页的归并组优选规则。
    details: dict[int, list[tuple[int, str | None, str | None, int]]] = {}
    for doc_id, issuer, number, body_length in session.execute(
        select(
            DocumentDetail.document_id,
            DocumentDetail.issuer,
            DocumentDetail.doc_number,
            # 按字数算（MySQL 的 LENGTH 是字节数），和详情页用 Python len() 的口径一致
            func.char_length(DocumentDetail.body_text),
        ).where(DocumentDetail.document_id.in_(member_master))
    ):
        details.setdefault(member_master[doc_id], []).append(
            (doc_id, issuer, number, body_length or 0)
        )

    deadlines: dict[int, list[DocumentDeadline]] = {master: [] for master in ordered}
    for deadline in session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id.in_(member_master),
            DocumentDeadline.status.in_(("approved", "changed")),
        )
    ):
        deadlines[member_master[deadline.document_id]].append(deadline)
    target_ids = [
        d.changed_to_document_id
        for group in deadlines.values()
        for d in group
        if d.changed_to_document_id
    ]
    target_masters = canonical_ids(session, target_ids)
    titles = {
        doc_id: title
        for doc_id, title in session.execute(
            select(Document.id, Document.title).where(
                Document.id.in_([target_masters.get(i, i) for i in target_ids])
            )
        )
    }

    result = []
    for index, master in enumerate(ordered, 1):
        doc, source = by_id[master]
        best = max(
            details.get(master, []),
            key=lambda row: (bool(row[3]), row[3], row[0] == master),
            default=None,
        )
        group_deadlines = deadlines[master]
        changed = sorted(
            (d for d in group_deadlines if d.status == "changed" and d.approved_date),
            key=lambda d: (d.approved_date, d.id),
        )
        changed_keys = {(d.kind, d.approved_date, d.approved_time) for d in changed}
        approved = sorted(
            (
                d
                for d in group_deadlines
                if d.status == "approved"
                and d.approved_date
                and (d.kind, d.approved_date, d.approved_time) not in changed_keys
            ),
            key=lambda d: (d.approved_date, d.id),
        )
        notes: list[str] = []
        seen: set[tuple] = set()
        for d in approved:
            key = (d.kind, d.approved_date, d.approved_time)
            if key not in seen:
                notes.append(_deadline_text(d))
                seen.add(key)
        for d in changed:
            key = (d.kind, d.approved_date, d.approved_time)
            if key not in seen:
                target = target_masters.get(d.changed_to_document_id, d.changed_to_document_id)
                title = titles.get(target, "变更通知")
                notes.append(f"原定 {_deadline_text(d)} 已{d.change_reason}，见新通知《{title}》")
                seen.add(key)
        group = members[master]
        urls = list(dict.fromkeys(item.url for item, _ in group))
        sites = list(
            dict.fromkeys(
                f"{src.dept_full_name}（{urlsplit(item.url).hostname or src.url}）"
                for item, src in group
            )
        )
        source_dates = sorted({src.verified_on.isoformat() for _, src in group if src.verified_on})
        review_dates = sorted({_date_time(d.reviewed_at) for d in group_deadlines if d.reviewed_at})
        change_dates = sorted({_date_time(d.changed_at) for d in changed if d.changed_at})
        result.append(
            ExportItem(
                number=index,
                doc_id=master,
                title=doc.title,
                kind=doc_kind(doc.title),
                status=list_status(doc.title) or "",
                issuer=(best[1] if best and best[1] else source.dept_full_name),
                doc_number=(best[2] if best and best[2] else ""),
                published=doc.publish_date.isoformat() if doc.publish_date else "",
                region=region_label(source.province, source.level),
                deadline="；".join(notes) if notes else "见原文",
                urls=urls,
                sites="；".join(sites),
                source_checked="、".join(source_dates) or "未记录",
                deadline_checked="、".join(review_dates) or "未记录",
                change_checked="、".join(change_dates) or "未记录",
            )
        )
    return result


def _cell(sheet, row: int, column: int, value: str | int) -> None:
    cell = sheet.cell(row, column, value)
    if isinstance(value, str):
        cell.data_type = "s"  # 政府网站题录属于外部文本，不能解释成 Excel 公式。
    cell.alignment = Alignment(vertical="top", wrap_text=True)


def _xlsx(items: list[ExportItem], stamp: str) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "文件清单"
    max_urls = max(len(item.urls) for item in items)
    headers = [*HEADERS, "原文链接", *(f"原文链接 {n}" for n in range(2, max_urls + 1))]
    for column, title in enumerate(headers, 1):
        _cell(sheet, 1, column, title)
        sheet.cell(1, column).font = Font(bold=True)
    for row, item in enumerate(items, 2):
        values = (
            item.number,
            item.title,
            item.kind,
            item.status,
            item.issuer,
            item.doc_number,
            item.published,
            item.region,
            item.deadline,
            *item.urls,
        )
        for column, value in enumerate(values, 1):
            _cell(sheet, row, column, value)
        for column, url in enumerate(item.urls, len(HEADERS) + 1):
            if _is_web_url(url):
                sheet.cell(row, column).hyperlink = url
                sheet.cell(row, column).font = Font(color="0563C1", underline="single")
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in range(1, len(headers) + 1):
        sheet.column_dimensions[sheet.cell(1, column).column_letter].width = (
            60 if column in (2, 9) or column > len(HEADERS) else 18
        )

    note = book.create_sheet("数据说明")
    note.append(["数据说明", NOTICE])
    note.append(["导出时间（北京时间）", stamp])
    note.append(
        ["序号", "文件编号", "来源网站", "来源核对日期", "截止日期核对日期", "变更核对日期"]
    )
    for row, item in enumerate(items, 4):
        values = (
            item.number,
            str(item.doc_id),
            item.sites,
            item.source_checked,
            item.deadline_checked,
            item.change_checked,
        )
        for column, value in enumerate(values, 1):
            _cell(note, row, column, value)
    note.column_dimensions["B"].width = 65
    note.column_dimensions["C"].width = 80
    for letter in "ADEF":
        note.column_dimensions[letter].width = 22
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def _docx(items: list[ExportItem], stamp: str) -> bytes:
    book = WordDocument()
    book.add_heading("有据导出 · 数据说明", 0)
    book.add_paragraph(NOTICE)
    book.add_paragraph(f"导出时间（北京时间）：{stamp}")
    sources = book.add_table(rows=1, cols=6)
    for cell, title in zip(
        sources.rows[0].cells,
        ("序号", "文件编号", "来源网站", "来源核对日期", "截止日期核对日期", "变更核对日期"),
        strict=True,
    ):
        cell.text = title
    for item in items:
        values = (
            str(item.number),
            str(item.doc_id),
            item.sites,
            item.source_checked,
            item.deadline_checked,
            item.change_checked,
        )
        for cell, value in zip(sources.add_row().cells, values, strict=True):
            cell.text = value
    for item in items:
        book.add_heading(f"{item.number}. {item.title}", level=1)
        table = book.add_table(rows=0, cols=2)
        fields = (
            ("序号", str(item.number)),
            ("标题", item.title),
            ("类型", item.kind),
            ("名单状态", item.status),
            ("发文机关", item.issuer),
            ("文号", item.doc_number),
            ("发布日期", item.published),
            ("地区", item.region),
            ("截止日期", item.deadline),
            ("来源网站", item.sites),
            ("来源核对日期", item.source_checked),
            ("截止日期核对日期", item.deadline_checked),
            ("变更核对日期", item.change_checked),
        )
        for label, value in fields:
            cells = table.add_row().cells
            cells[0].text = label
            cells[1].text = value
        book.add_paragraph("原文链接：")
        for url in item.urls:
            book.add_paragraph(url)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


@router.post("/exports")
def export_documents(payload: ExportIn, session: SessionDep) -> Response:
    items = _items(session, payload.ids)
    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    data = _xlsx(items, stamp) if payload.format == "xlsx" else _docx(items, stamp)
    if len(data) >= MAX_BYTES:
        raise HTTPException(status_code=413, detail="导出文件超过 1000 KiB，请减少文件数量")
    filename = f"有据导出_{stamp[:10]}.{payload.format}"
    mime = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if payload.format == "xlsx"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": (
                f"attachment; filename=youju-export_{stamp[:10]}.{payload.format}; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
