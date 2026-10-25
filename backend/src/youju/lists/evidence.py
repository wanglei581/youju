"""查名单站内闭环（D-088、D-091）：名单条目在原文附件里的那一行。

设计：docs/设计/查名单站内闭环_设计_2026-10-23.md（第四节已定方案 C）。

- 范围（candidates）：查名单能查到的条目所用的那份名单附件——公开来源、没下架、名单类（人工定
  为名单或没定）、解析成功并有条目的附件。只取文字，不存副本（名单附件不给下载）；也不受申报
  通知、政策的保留期限制：查的就是往年的认定。
- 读取（refresh）：用和附件副本同一个礼貌抓取器下载（遵守 robots、每站间隔），按名单模式的上限取
  文字（attachment_text.LIST_LIMITS），整份文字只在内存里用，不落库。
- 定位（locate）：先认名称列，再只在名称列里比。名称列是这份附件里哪一列有多少格（去掉序号和
  空白后）正好是本附件某条条目的名称——按不同名称数，推荐单位、承担单位这类反复出现同一个机构
  的列不会被认成名称列。有信用代码的条目，这一行里还要有一格整格等于这个代码（这一行没有代码格
  的不卡）。不在备注等其他列里找名称或代码，不按序号推算位置。
- 只存命中行（每条最多 5 行），存之前去掉个人信息列：列名像个人信息的（联系人、负责人、法定代表
  人、姓名、电话……），和不是名称列、整列多半是人名的；号码取文字时已遮，存之前再遮一遍。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from youju import attachment_text as texts
from youju.lists.lookup import public_condition
from youju.lists.rules import is_person_query, normalize_name
from youju.models import (
    Document,
    DocumentAttachment,
    ListEntry,
    ListEntryLocation,
    ListEvidenceSource,
    ListParse,
    SigningKey,
    Source,
)

log = logging.getLogger("youju.lists.evidence")

# 定位规则的版本：改了 locate、个人信息列的认法、存的格式就加一，已读过的附件下一轮重读
LOCATOR_VERSION = 1
MAX_HITS = 5
# 名称列要占到最多那一列命中名称数的这个比例（一份附件里有几张表、名称列不止一个时都认）
NAME_COLUMN_SHARE = 0.2
# 临时问题（网络、服务器出错、robots.txt 暂时取不到）隔多久再试；读不出来的（扫描件、格式不支持、
# 原网站删了）隔多久再看一次（原网站可能换了文件）
RETRY_AFTER = timedelta(hours=24)
FAILED_RECHECK = timedelta(days=7)
# 读成功的隔多久再下载一次，看原网站的文件变没变
OK_RECHECK = timedelta(days=30)
_LOCK_NAME = "youju:list-evidence"

_SEQUENCE = re.compile(r"^[(（]?\d{1,4}[)）.、．:：]?")
_CODE = re.compile(r"^[0-9A-Z]{18}$")
# 这些列名下的格属于个人信息，不存、不显示（号码入库前已遮，姓名、地址等没遮）
PERSONAL_HEADERS = (
    "联系人",
    "负责人",
    "法定代表人",
    "法人代表",
    "法人",
    "姓名",
    "名字",
    "专家",
    "电话",
    "手机",
    "联系方式",
    "身份证",
    "证件",
    "邮箱",
    "电子邮件",
    "住址",
    "地址",
    "户籍",
    "出生",
    "性别",
    "年龄",
    "学历",
    "职务",
    "职称",
)
# 不是名称列、整列至少这个比例的格像人名（2–4 个汉字、不含机构用词）的，当个人信息列
PERSON_COLUMN_SHARE = 0.5
_PLACE_ENDS = ("省", "市", "区", "县", "旗", "镇", "乡", "州", "盟", "街道")


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
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def personal_header(label: str | None) -> bool:
    return any(word in (label or "") for word in PERSONAL_HEADERS)


# ---- 条目编号的签名（D-088：防止按编号把整份名单翻出来） ----


def _signing_key(session: Session) -> bytes:
    row = session.get(SigningKey, "list_entry")
    if row is None:
        raise RuntimeError("签名密钥还没准备好（迁移 0041 没跑？）")
    return bytes.fromhex(row.key_hex)


def _mac(key: bytes, entry_id: int) -> str:
    digest = hmac.new(key, f"list-entry:{entry_id}".encode(), hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def entry_tokens(session: Session, entry_ids: list[int]) -> dict[int, str]:
    """给查名单结果里的条目编号签名：只有查到这家企业的人拿得到某条条目的编号。"""
    if not entry_ids:
        return {}
    key = _signing_key(session)
    return {entry_id: f"{entry_id}.{_mac(key, entry_id)}" for entry_id in entry_ids}


def parse_entry_token(session: Session, token: str) -> int | None:
    """验签，取出条目编号；格式不对、签名对不上的给 None。"""
    match = re.fullmatch(r"([1-9][0-9]{0,11})\.([A-Za-z0-9_-]{16})", token or "")
    if not match:
        return None
    entry_id = int(match.group(1))
    if not hmac.compare_digest(match.group(2), _mac(_signing_key(session), entry_id)):
        return None
    return entry_id


# ---- 范围 ----


def candidates(session: Session) -> dict[str, str]:
    """要读的名单附件：原站链接 → 扩展名（题录里的；没有就看链接）。新发布的在前。"""
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


# ---- 定位 ----


@dataclass
class Found:
    state: str
    segments: list[int] = field(default_factory=list)


def _column_key(seg, cell) -> tuple:
    loc = seg.loc or {}
    return (loc.get("sheet"), cell.get("h") or "", cell.get("c"))


def _name_columns(rows, names: set[str]) -> set[tuple]:
    hits: dict[tuple, set[str]] = defaultdict(set)
    for seg in rows:
        for cell in seg.cells:
            if isinstance(cell, dict) and (value := cell_name(str(cell.get("t", "")))) in names:
                hits[_column_key(seg, cell)].add(value)
    if not hits:
        return set()
    top = max(len(v) for v in hits.values())
    # 只有一两个名称的小名单，最多的那一列就是；否则零星撞上一个名称的列（推荐单位等）不算
    floor = max(min(top, 2), math.ceil(top * NAME_COLUMN_SHARE))
    return {key for key, v in hits.items() if len(v) >= floor}


def locate(segments, entries) -> tuple[dict[int, Found], set[tuple]]:
    """segments：这份附件的段（kind、n、loc、cells）；entries：(条目编号, 规范名称, 信用代码)。

    返回每条条目找到的段号，和认出的名称列（存的时候名称列不当个人信息列）。"""
    rows = [s for s in segments if s.kind == "row" and s.cells]
    names = {name for _, name, _ in entries if name}
    name_columns = _name_columns(rows, names)
    if not name_columns:
        return {entry_id: Found("no_name_column") for entry_id, _, _ in entries}, set()
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
    return out, name_columns


def _looks_like_person(value: str) -> bool:
    """像人名：2–4 个汉字、不含机构用词，也不是地名（某某区）、批次（第一批）这类短词。"""
    return is_person_query(value) and not value.endswith(_PLACE_ENDS) and "批" not in value


def _person_columns(rows, name_columns: set[tuple]) -> set[tuple]:
    """不是名称列、没有列名、整列多半是人名的列（表头没认出来的联系人、负责人列）。

    有列名的列按列名认（personal_header），这里不再按内容猜，免得把"所在地区"这类短词的列藏掉。"""
    seen: dict[tuple, int] = defaultdict(int)
    people: dict[tuple, int] = defaultdict(int)
    for seg in rows:
        for cell in seg.cells:
            if not isinstance(cell, dict) or cell.get("h"):
                continue
            key = _column_key(seg, cell)
            value = normalize_name(str(cell.get("t", "")))
            if key in name_columns or not value:
                continue
            seen[key] += 1
            if _looks_like_person(value):
                people[key] += 1
    return {key for key, n in seen.items() if people[key] >= max(1, n * PERSON_COLUMN_SHARE)}


def _stored_row(seg, hidden_columns: set[tuple]) -> dict:
    """存进 list_entry_locations 的一行：原文位置、各格（列名、文字）。

    个人信息列去掉，号码再遮一遍。"""
    cells, hidden = [], 0
    for cell in seg.cells:
        if not isinstance(cell, dict):
            continue
        header = str(cell.get("h") or "")
        if personal_header(header) or _column_key(seg, cell) in hidden_columns:
            hidden += 1
            continue
        cells.append({"h": header, "t": texts.mask(str(cell.get("t", "")))})
    where = {k: v for k, v in (seg.loc or {}).items() if k in ("page", "table", "sheet", "row")}
    return {"where": where, "cells": cells, "hidden": hidden}


# ---- 读取和存 ----


@dataclass
class EvidenceReport:
    due: int = 0
    read: int = 0
    partial: int = 0
    failed: dict = field(default_factory=lambda: defaultdict(int))
    states: dict = field(default_factory=lambda: defaultdict(int))
    skipped_lock: bool = False
    errors: int = 0


def url_key(url: str) -> str:
    """链接的 SHA-256，list_evidence_sources.url_hash 用它（和附件文字同一个算法）。"""
    return texts.url_key(url)


def _due(session: Session, wanted: dict[str, str], now: datetime) -> list[str]:
    """这一轮要读的链接：没读过的；规则升级了的；条目变了的（重新解析过）；到了重试或复查时间的。"""
    rows = {r.url_hash: r for r in session.scalars(select(ListEvidenceSource))}
    counts = (
        dict(
            session.execute(
                select(ListEntry.source_url, func.count(ListEntry.id))
                .where(ListEntry.source_url.in_(list(wanted)))
                .group_by(ListEntry.source_url)
            ).all()
        )
        if wanted
        else {}
    )
    located = (
        dict(
            session.execute(
                select(ListEntry.source_url, func.count(ListEntryLocation.entry_id))
                .join(ListEntryLocation, ListEntryLocation.entry_id == ListEntry.id)
                .where(ListEntry.source_url.in_(list(wanted)))
                .group_by(ListEntry.source_url)
            ).all()
        )
        if wanted
        else {}
    )
    never, stale = [], []
    for url in wanted:
        row = rows.get(url_key(url))
        if row is None:
            never.append(url)
            continue
        outdated = row.extractor != texts.EXTRACTOR_VERSION or row.locator != LOCATOR_VERSION
        if row.status == "failed":
            if outdated or (row.retry_at is not None and row.retry_at <= now):
                stale.append(url)
            continue
        changed = counts.get(url, 0) != row.entries or located.get(url, 0) != counts.get(url, 0)
        recheck = row.checked_at + OK_RECHECK <= now
        if outdated or changed or recheck:
            stale.append(url)
    return never + stale


@contextmanager
def _exclusive(session: Session) -> Iterator[bool]:
    """拿名单读取任务锁（不等待）：拿到给 True，另一个同样的任务正在跑给 False。"""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine.dialect.name != "mysql":
        yield True
        return
    with engine.connect() as conn:
        got = conn.execute(text("SELECT GET_LOCK(:k, 0)"), {"k": _LOCK_NAME}).scalar() == 1
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": _LOCK_NAME})


def _source_row(session: Session, url: str, now: datetime) -> ListEvidenceSource:
    key = url_key(url)
    row = session.scalar(select(ListEvidenceSource).where(ListEvidenceSource.url_hash == key))
    if row is None:
        # 先按"还没读成"建一行，读完再改；中途出错整个事务回滚，不留半截记录
        row = ListEvidenceSource(
            url_hash=key, url=url, status="failed", extractor=0, locator=0, checked_at=now
        )
        session.add(row)
    return row


def _record_failure(row, reason: str, detail: str, now: datetime, *, transient: bool) -> None:
    row.status = "failed"
    row.reason = reason
    row.detail = (detail or "")[:300]
    row.extractor = texts.EXTRACTOR_VERSION
    row.locator = LOCATOR_VERSION
    row.checked_at = now
    row.retry_at = now + (RETRY_AFTER if transient else FAILED_RECHECK)


def _read_one(session: Session, url: str, fetcher, now: datetime, report: EvidenceReport) -> None:
    from youju.attachments import MAX_BYTES, fetch_failure

    row = _source_row(session, url, now)
    result = fetcher.fetch(url)
    if result.status != "ok":
        failure = fetch_failure(result)
        reason = {"gone": "gone", "too_large": "too_large"}.get(failure.code, "unavailable")
        _record_failure(row, reason, failure.detail, now, transient=failure.transient)
        report.failed[reason] += 1
        return
    if not _government_url(result.final_url or url):
        _record_failure(row, "unavailable", "跳转到了非政府网站", now, transient=False)
        report.failed["unavailable"] += 1
        return
    content = result.content or b""
    if len(content) > MAX_BYTES:
        _record_failure(row, "too_large", "附件超过 20MB", now, transient=False)
        report.failed["too_large"] += 1
        return
    extraction = texts.extract(content, limits=texts.LIST_LIMITS)
    row.content_sha256 = hashlib.sha256(content).hexdigest()
    row.file_type = extraction.file_type
    if not extraction.ok:
        _record_failure(
            row, extraction.reason or "broken", extraction.detail or "", now, transient=False
        )
        report.failed[extraction.reason or "broken"] += 1
        return
    entries = session.execute(
        select(ListEntry.id, ListEntry.normalized_name, ListEntry.credit_code).where(
            ListEntry.source_url == url
        )
    ).all()
    found, name_columns = locate(
        extraction.segments, [(e.id, e.normalized_name, e.credit_code) for e in entries]
    )
    by_n = {seg.n: seg for seg in extraction.segments}
    table_rows = [s for s in extraction.segments if s.kind == "row" and s.cells]
    hidden = _person_columns(table_rows, name_columns)
    ids = [e.id for e in entries]
    if ids:
        session.execute(delete(ListEntryLocation).where(ListEntryLocation.entry_id.in_(ids)))
    pages_total = extraction.pages
    pages_limit = texts.LIST_LIMITS.pdf_pages
    partial = (
        extraction.file_type == "pdf" and pages_total is not None and pages_total > pages_limit
    )
    row.status = "partial" if partial else "ok"
    row.reason, row.detail = None, None
    row.pages_total = pages_total
    row.pages_read = min(pages_total, pages_limit) if pages_total is not None else None
    row.extractor = texts.EXTRACTOR_VERSION
    row.locator = LOCATOR_VERSION
    row.entries = len(ids)
    row.located = sum(1 for f in found.values() if f.segments)
    row.checked_at = now
    row.retry_at = None
    session.flush()
    for entry_id, result_found in found.items():
        session.add(
            ListEntryLocation(
                entry_id=entry_id,
                source_id=row.id,
                state=result_found.state,
                rows=[_stored_row(by_n[n], hidden) for n in result_found.segments if n in by_n],
                computed_at=now,
            )
        )
        report.states[result_found.state] += 1
    report.read += 1
    if partial:
        report.partial += 1


def refresh(
    session: Session,
    *,
    limit: int = 30,
    fetcher=None,
    now: datetime | None = None,
    urls: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> EvidenceReport:
    """读这一轮该读的名单附件（最多 limit 个，每个一个事务），定位条目、存命中行。"""
    from youju.config import get_settings
    from youju.crawler.fetcher import PoliteFetcher

    report = EvidenceReport()
    now = now or datetime.now(UTC)
    with _exclusive(session) as got:
        if not got:
            report.skipped_lock = True
            return report
        wanted = candidates(session)
        if urls is not None:
            wanted = {url: wanted[url] for url in urls if url in wanted}
        due = _due(session, wanted, now)
        report.due = len(due)
        owns_fetcher = fetcher is None
        if fetcher is None:
            settings = get_settings()
            fetcher = PoliteFetcher(
                settings.user_agent,
                settings.request_timeout,
                settings.per_host_interval,
                url_allowed=_government_url,
                decode_text=False,
            )
        try:
            for url in due[:limit]:
                try:
                    _read_one(session, url, fetcher, now, report)
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - 一份出错不影响其他份
                    session.rollback()
                    report.errors += 1
                    log.warning("名单附件读取出错：%s", exc.__class__.__name__)
        finally:
            if owns_fetcher:
                fetcher.close()
    return report


def preview(session: Session, *, now: datetime | None = None) -> dict:
    """只看不读：一共多少份名单附件，这一轮该读多少份，各状态多少份。"""
    now = now or datetime.now(UTC)
    wanted = candidates(session)
    due = _due(session, wanted, now)
    by_status = dict(
        session.execute(
            select(ListEvidenceSource.status, func.count()).group_by(ListEvidenceSource.status)
        ).all()
    )
    return {"candidates": len(wanted), "due": len(due), "status": by_status}
