"""查名单站内闭环：名单条目在原文附件里的那一行（docs/设计/查名单站内闭环_设计_2026-10-23.md）。

- 范围（text_candidates）：查询能查到的条目所用的那份名单附件——公开来源、没下架、人工类型是名单
  或没定、解析成功并有条目的附件。只取文字，不存副本（名单附件不给下载）；也不受申报通知、政策
  的保留期限制：查的就是往年的认定。
- 定位（locate）：先认名称列，再只在名称列里比。名称列是这份附件里哪一列有多少格（去掉序号和
  空白后）正好是本附件某条条目的名称——按不同名称数，推荐单位、承担单位这类反复出现同一个机构
  的列不会被认成名称列。有信用代码的条目，这一行里还要有一格整格等于这个代码（这一行没有代码格
  的不卡）。不在备注等其他列里找名称或代码，不按序号推算位置。
- 刷新（refresh_locations）：附件文字换了版本、取文字规则升级、条目重解析后重算；接口只查表。
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from youju import attachment_text as texts
from youju.lists.lookup import public_condition
from youju.lists.rules import normalize_name
from youju.models import (
    AttachmentTextSegment,
    AttachmentTextUrl,
    AttachmentTextVersion,
    Document,
    DocumentAttachment,
    ListEntry,
    ListEntryLocation,
    ListParse,
    Source,
)

MAX_HITS = 5
# 名称列要占到最多那一列命中名称数的这个比例（一份附件里有几张表、名称列不止一个时都认）
NAME_COLUMN_SHARE = 0.2
_SEQUENCE = re.compile(r"^[(（]?\d{1,4}[)）.、．:：]?")
_CODE = re.compile(r"^[0-9A-Z]{18}$")
# 这些列名下的格属于个人信息，接口里不显示（号码入库前已遮，姓名等没遮）
PERSONAL_HEADERS = (
    "联系人",
    "负责人",
    "法定代表人",
    "法人代表",
    "法人",
    "姓名",
    "专家",
    "电话",
    "手机",
    "联系方式",
    "身份证",
    "证件",
    "邮箱",
    "住址",
    "户籍",
)


def cell_name(value: str) -> str:
    """格子里的名称，和条目的 normalized_name 同一套规则，再去掉开头的序号（"1." "（2）"）。"""
    return _SEQUENCE.sub("", normalize_name(value or ""))


def _code(value: str) -> str | None:
    code = re.sub(r"\s+", "", value or "").upper()
    return code if _CODE.match(code) else None


def _government_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "gov.cn" or host.endswith(".gov.cn")


def _extension(url: str) -> str:
    path = urlsplit(url).path
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def text_candidates(session: Session) -> dict[str, str]:
    """要取文字的名单附件：原站链接 → 扩展名（题录里的；没有就看链接）。"""
    from youju.attachments import DOWNLOAD_TYPES

    rows = session.execute(
        select(ListParse.source_url, ListParse.document_id)
        .join(Document, Document.id == ListParse.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ListParse.status == "ok",
            ListParse.source_kind == "attachment",
            ListParse.entry_count > 0,
            public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
        )
        .order_by(Document.publish_date.desc(), Document.id.desc())
    ).all()
    if not rows:
        return {}
    named = {
        (doc_id, url): (ext or "").lower()
        for doc_id, url, ext in session.execute(
            select(
                DocumentAttachment.document_id,
                DocumentAttachment.url,
                DocumentAttachment.extension,
            ).where(DocumentAttachment.document_id.in_({doc_id for _, doc_id in rows}))
        )
    }
    found: dict[str, str] = {}
    for url, doc_id in rows:
        ext = named.get((doc_id, url)) or _extension(url)
        if url not in found and ext in DOWNLOAD_TYPES and _government_url(url):
            found[url] = ext
    return found


@dataclass
class Found:
    state: str
    segments: list[int] = field(default_factory=list)


def _column_key(seg, cell) -> tuple:
    loc = seg.loc or {}
    return (loc.get("sheet"), cell.get("h") or "", cell.get("c"))


def locate(segments, entries) -> dict[int, Found]:
    """segments：这份附件的段（kind、n、cells）；entries：(条目编号, 规范名称, 信用代码)。"""
    rows = [s for s in segments if s.kind == "row" and s.cells]
    names = {name for _, name, _ in entries if name}
    hits: dict[tuple, set[str]] = defaultdict(set)
    for seg in rows:
        for cell in seg.cells:
            if isinstance(cell, dict) and (value := cell_name(str(cell.get("t", "")))) in names:
                hits[_column_key(seg, cell)].add(value)
    if not hits:
        return {entry_id: Found("no_name_column") for entry_id, _, _ in entries}
    top = max(len(v) for v in hits.values())
    # 只有一两个名称的小名单，最多的那一列就是；否则零星撞上一个名称的列（推荐单位等）不算
    floor = max(min(top, 2), math.ceil(top * NAME_COLUMN_SHARE))
    name_columns = {key for key, v in hits.items() if len(v) >= floor}
    by_name: dict[str, list] = defaultdict(list)
    for seg in rows:
        codes = {
            c
            for cell in seg.cells
            if isinstance(cell, dict) and (c := _code(str(cell.get("t", ""))))
        }
        for cell in seg.cells:
            if not isinstance(cell, dict) or _column_key(seg, cell) not in name_columns:
                continue
            value = cell_name(str(cell.get("t", "")))
            if value in names:
                by_name[value].append((seg.n, codes))
    out: dict[int, Found] = {}
    for entry_id, name, code in entries:
        matched = sorted(
            {n for n, codes in by_name.get(name, []) if not code or not codes or code in codes}
        )
        if not matched:
            out[entry_id] = Found("notfound")
        elif len(matched) > MAX_HITS:
            out[entry_id] = Found("ambiguous", matched[:MAX_HITS])
        else:
            out[entry_id] = Found("ok" if len(matched) == 1 else "multiple", matched)
    return out


@dataclass
class LocateReport:
    sources: int = 0
    located: int = 0
    states: dict = field(default_factory=lambda: defaultdict(int))
    skipped: int = 0


def refresh_locations(session: Session, now: datetime, *, urls=None) -> LocateReport:
    """给文字已就绪、位置没算或过时的名单附件重算条目位置并提交（每份附件一个事务）。"""
    report = LocateReport()
    wanted = urls if urls is not None else text_candidates(session)
    for url in wanted:
        text_url = session.scalar(
            select(AttachmentTextUrl).where(AttachmentTextUrl.url_hash == texts.url_key(url))
        )
        version = (
            session.get(AttachmentTextVersion, text_url.version_id)
            if text_url is not None and text_url.version_id
            else None
        )
        if (
            version is None
            or version.status != "ok"
            or version.extractor != texts.EXTRACTOR_VERSION
        ):
            report.skipped += 1
            continue
        entries = session.execute(
            select(ListEntry.id, ListEntry.normalized_name, ListEntry.credit_code).where(
                ListEntry.source_url == url
            )
        ).all()
        if not entries:
            continue
        ids = [e.id for e in entries]
        current = {
            row.entry_id: row
            for row in session.scalars(
                select(ListEntryLocation).where(ListEntryLocation.entry_id.in_(ids))
            )
        }
        if len(current) == len(ids) and all(
            r.version_id == version.id and r.extractor == version.extractor
            for r in current.values()
        ):
            continue
        segments = session.scalars(
            select(AttachmentTextSegment)
            .where(
                AttachmentTextSegment.version_id == version.id,
                AttachmentTextSegment.kind == "row",
            )
            .order_by(AttachmentTextSegment.n)
        ).all()
        found = locate(segments, [(e.id, e.normalized_name, e.credit_code) for e in entries])
        session.execute(delete(ListEntryLocation).where(ListEntryLocation.entry_id.in_(ids)))
        for entry_id, result in found.items():
            session.add(
                ListEntryLocation(
                    entry_id=entry_id,
                    version_id=version.id,
                    extractor=version.extractor,
                    state=result.state,
                    segments=result.segments,
                    computed_at=now,
                )
            )
            report.states[result.state] += 1
        session.commit()
        report.sources += 1
        report.located += len(found)
    return report


def personal_header(label: str) -> bool:
    return any(word in (label or "") for word in PERSONAL_HEADERS)
