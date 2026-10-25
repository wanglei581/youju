"""查名单站内闭环（D-088、D-091）：名单条目在原文附件里的那一行。

设计：docs/设计/查名单站内闭环_设计_2026-10-23.md（第四节已定方案 C）。

- 范围（candidates）：查名单能查到的条目所用的那份名单文件——公开来源、没下架、名单类（人工定
  为名单或没定）、解析成功并有条目的附件（或通知网址本身就是名单文件的）。只取文字，不存副本（名单
  附件不给下载）；也不受申报通知、政策的保留期限制：查的就是往年的认定。
- 读取（refresh）：用和附件副本同一个礼貌抓取器下载（遵守 robots、每站间隔），按名单模式的上限取
  文字（attachment_text.LIST_LIMITS），整份文字只在内存里用，不落库。每轮有时间上限。
- 定位（locate）：先认名称列，再只在名称列里比。名称列按每张表（工作表、PDF 跨页接上的表）里的每一
  列（同一列名）分别认：这一列有字的格里至少一半正好是本附件某条条目的名称，或者是这张表里条目名称
  最多的一列（够整份文件最多那一列的两成）；同一张表里有几列都够格时，只写了同一个名称的列（推荐
  单位、承担单位里反复出现的同一个机构）不算。有信用代码的条目，这一行
  里还要有一格整格等于这个代码（这一行没有代码格的不卡）。不在备注等其他列里找名称或代码，不按序号
  推算位置。表头行也比（Word 里整张表都设了"标题行重复"的，名单就在表头行里）。
- 只存命中行（每条最多 5 行），只存这几类格：名称列；列名是名称、单位、代码、编号、地区、类别、
  批次、年度、领域这类的（列名里有联系人、负责人、姓名、电话这类个人信息用词的除外）；没有列名的
  只存序号和统一社会信用代码。别的格不存，只记"另有几列没显示"。号码取文字时已遮（统一社会信用
  代码不遮），存之前再遮一遍。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from youju import attachment_text as texts
from youju.lists.lookup import public_condition
from youju.lists.rules import normalize_credit_code, normalize_name
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

# 定位规则的版本：改了 locate、存哪些列、存的格式就加一，已读过的附件下一轮重读
LOCATOR_VERSION = 2
MAX_HITS = 5
# 名称列：这一列有字的格里，正好是条目名称的至少占这个比例；或者是这张表里条目名称最多的列，
# 名称数够整份文件里最多那一列的这个比例
NAME_COLUMN_SHARE = 0.5
MAIN_COLUMN_SHARE = 0.2
# 临时问题（网络、服务器出错、robots.txt 暂时取不到）隔多久再试；读不出来的（扫描件、格式不支持、
# 原网站删了）隔多久再看一次（原网站可能换了文件）
RETRY_AFTER = timedelta(hours=24)
FAILED_RECHECK = timedelta(days=7)
# 读成功的隔多久再下载一次，看原网站的文件变没变
OK_RECHECK = timedelta(days=30)
# 一轮最多跑多久（秒）：到点不再开始读下一份，剩下的下一轮接着读
RUN_SECONDS = 20 * 60
_LOCK_NAME = "youju:list-evidence"

_SEQUENCE = re.compile(r"^[(（]?\d{1,4}[)）.、．:：]?")
_SEQUENCE_CELL = re.compile(r"[(（]?\d{1,5}[)）.、．]?")
_CODE = re.compile(r"^[0-9A-Z]{18}$")
# 列名里有这些词的属于个人信息，不存、不显示（名称列除外：名称列里是查到的那家单位的名称）
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
    "申报人",
    "申请人",
    "完成人",
    "发明人",
    "设计人",
    "带头人",
    "经办人",
    "填报人",
    "推荐人",
    "主持人",
    "成员",
    "作者",
    "创始人",
    "董事长",
    "总经理",
    "导师",
)
# 只存、只显示列名里有这些词的列（先排除上面的个人信息列）：单位名称、编号、地区、类别、批次、
# 产品和技术、名次、金额这类公开的认定信息。备注、地址、没认出列名的和别的列都不存
SHOWN_HEADERS = (
    "名称",
    "单位",
    "企业",
    "机构",
    "公司",
    "部门",
    "代码",
    "编号",
    "证书",
    "序号",
    "地区",
    "区域",
    "区划",
    "所属",
    "所在",
    "注册地",
    "属地",
    "省份",
    "市",
    "区",
    "县",
    "类别",
    "类型",
    "级别",
    "等级",
    "等次",
    "批次",
    "年度",
    "年份",
    "日期",
    "时间",
    "有效期",
    "领域",
    "行业",
    "产业",
    "产品",
    "型号",
    "规格",
    "方向",
    "项目",
    "技术",
    "指标",
    "范围",
    "能级",
    "物项",
    "面积",
    "奖项",
    "名次",
    "排序",
    "排名",
    "金额",
    "资金",
    "经费",
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
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def personal_header(label: str | None) -> bool:
    return any(word in (label or "") for word in PERSONAL_HEADERS)


def shown_cell(header: str | None, value: str, *, is_name: bool = False) -> bool:
    """命中行里这一格存不存、显示不显示（D-088：个人信息不显示，只给必要的列）。"""
    if is_name:
        return True
    if header:
        return not personal_header(header) and any(word in header for word in SHOWN_HEADERS)
    value = (value or "").strip()
    return bool(_SEQUENCE_CELL.fullmatch(value)) or normalize_credit_code(value) is not None


# ---- 条目编号（D-088：签名的不透明编号，防止按编号把整份名单翻出来） ----
# 条目编号加密后给出（AES-SIV：同一条目每次得到同一个编号，改一个字就解不开），看不出是第几条

_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
_TOKEN_CONTEXT = [b"youju:list-entry:v1"]


def _token_cipher(session: Session) -> AESSIV:
    row = session.get(SigningKey, "list_entry")
    if row is None:
        raise RuntimeError("签名密钥还没准备好（迁移 0041 没跑？）")
    # 库里存的是总密钥，按用途派生出这里用的那把
    key = hmac.new(bytes.fromhex(row.key_hex), b"list-entry-token", hashlib.sha256).digest()
    return AESSIV(key)


def entry_tokens(session: Session, entry_ids: list[int]) -> dict[int, str]:
    """给查名单结果里的条目编号加密：只有查到这家企业的人拿得到某条条目的编号。"""
    if not entry_ids:
        return {}
    cipher = _token_cipher(session)
    return {
        entry_id: base64.urlsafe_b64encode(
            cipher.encrypt(entry_id.to_bytes(8, "big"), _TOKEN_CONTEXT)
        ).decode()
        for entry_id in entry_ids
    }


def parse_entry_token(session: Session, token: str) -> int | None:
    """解开编号；格式不对、被改过的给 None。"""
    if not _TOKEN.fullmatch(token or ""):
        return None
    try:
        plain = _token_cipher(session).decrypt(base64.urlsafe_b64decode(token), _TOKEN_CONTEXT)
    except (InvalidTag, ValueError, binascii.Error):
        return None
    entry_id = int.from_bytes(plain, "big")
    return entry_id if len(plain) == 8 and 0 < entry_id < 2**53 else None


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
            # attachment：通知的附件；document：通知网址本身就是名单文件（如直接发布的 PDF）
            ListParse.source_kind.in_(("attachment", "document")),
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


def _table_key(seg) -> tuple:
    loc = seg.loc or {}
    return (loc.get("sheet"), loc.get("slide"), loc.get("table"))


def _column_key(seg, cell) -> tuple:
    return (*_table_key(seg), cell.get("h") or "", cell.get("c"))


def _data_rows(segments) -> list:
    """参与比对的表格行：普通行和表头行。Word 里整张表（或前面几十行）都设了"标题行重复"的，名单
    就在表头行里；真正的表头行里没有条目名称，也没有列名，不和下面各行算同一列，比了也不碍事。"""
    return [s for s in segments if s.kind in ("row", "head") and s.cells]


def _name_columns(rows, names: set[str]) -> set[tuple]:
    filled: dict[tuple, int] = defaultdict(int)
    hit_cells: dict[tuple, int] = defaultdict(int)
    hit_names: dict[tuple, set[str]] = defaultdict(set)
    for seg in rows:
        for cell in seg.cells:
            if not isinstance(cell, dict):
                continue
            value = cell_name(str(cell.get("t", "")))
            if not value:
                continue
            key = _column_key(seg, cell)
            filled[key] += 1
            if value in names:
                hit_cells[key] += 1
                hit_names[key].add(value)
    # 每张表里条目名称最多的那一列（不同名称数）；整份文件里最多的
    tops: dict[tuple, int] = defaultdict(int)
    for key, found in hit_names.items():
        tops[key[:3]] = max(tops[key[:3]], len(found))
    biggest = max(tops.values(), default=0)
    qualified: dict[tuple, list[tuple]] = defaultdict(list)
    for key, n in hit_cells.items():
        distinct = len(hit_names[key])
        # 这一列有字的格里至少一半是条目名称；或者是这张表里名称最多的列、够整份文件最多那列的
        # 两成（名单里夹着解析时不收的政府部门等，名称列里条目占不到一半）
        mostly = n >= filled[key] * NAME_COLUMN_SHARE
        main = (
            distinct >= 2 and distinct == tops[key[:3]] and distinct >= biggest * MAIN_COLUMN_SHARE
        )
        if mostly or main:
            qualified[key[:3]].append(key)
    out: set[tuple] = set()
    for keys in qualified.values():
        # 同一张表里几列都够格：只写了同一个名称的列（推荐单位等）不算，除非只有这样的列
        varied = [key for key in keys if len(hit_names[key]) >= 2]
        out.update(varied or keys)
    return out


def locate(segments, entries) -> tuple[dict[int, Found], set[tuple]]:
    """segments：这份附件的段（kind、n、loc、cells）；entries：(条目编号, 规范名称, 信用代码)。

    返回每条条目找到的段号，和认出的名称列（存的时候名称列照样存）。"""
    rows = _data_rows(segments)
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


def _stored_row(seg, name_columns: set[tuple]) -> dict:
    """存进 list_entry_locations 的一行：原文位置、各格（列名、文字，名称列的格带 n）。

    只存 shown_cell 放行的格，别的只记个数；号码再遮一遍（统一社会信用代码不遮）。"""
    cells, hidden = [], 0
    for cell in seg.cells:
        if not isinstance(cell, dict):
            continue
        header = str(cell.get("h") or "")
        value = str(cell.get("t", ""))
        is_name = _column_key(seg, cell) in name_columns
        if not value.strip():
            continue
        if not shown_cell(header, value, is_name=is_name):
            hidden += 1
            continue
        item = {"h": header, "t": texts.mask_cell(value, header)}
        if is_name:
            item["n"] = 1
        cells.append(item)
    where = {k: v for k, v in (seg.loc or {}).items() if k in ("page", "table", "sheet", "row")}
    return {"where": where, "cells": cells, "hidden": hidden}


# ---- 读取和存 ----


@dataclass
class EvidenceReport:
    due: int = 0
    read: int = 0
    unchanged: int = 0
    partial: int = 0
    failed: dict = field(default_factory=lambda: defaultdict(int))
    states: dict = field(default_factory=lambda: defaultdict(int))
    skipped_lock: bool = False
    lost_lock: bool = False
    out_of_time: bool = False
    errors: int = 0


def url_key(url: str) -> str:
    """链接的 SHA-256，list_evidence_sources.url_hash 用它（和附件文字同一个算法）。"""
    return texts.url_key(url)


def _due(session: Session, wanted: dict[str, str], now: datetime) -> dict[str, str]:
    """这一轮要读的链接 → 为什么读：new 没读过；outdated 规则升级了；changed 条目变了（重新解析
    过）；retry 到了重试时间；recheck 到了复查时间（下载下来文件没变就不再重新定位）。"""
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
    never: dict[str, str] = {}
    stale: dict[str, str] = {}
    for url in wanted:
        row = rows.get(url_key(url))
        if row is None:
            never[url] = "new"
            continue
        outdated = row.extractor != texts.EXTRACTOR_VERSION or row.locator != LOCATOR_VERSION
        waiting = row.retry_at is not None and row.retry_at > now
        if row.status == "failed":
            if outdated:
                stale[url] = "outdated"
            elif not waiting and row.retry_at is not None:
                stale[url] = "retry"
            continue
        if waiting:
            # 读成功过、最近一次下载临时失败：先留着上次读到的，到了重试时间再读
            continue
        if outdated:
            stale[url] = "outdated"
        elif counts.get(url, 0) != row.entries or located.get(url, 0) != counts.get(url, 0):
            stale[url] = "changed"
        elif row.checked_at + OK_RECHECK <= now:
            stale[url] = "recheck"
    return never | stale


@contextmanager
def _exclusive(session: Session) -> Iterator[Callable[[], bool] | None]:
    """拿名单读取任务锁（不等待）：拿到给一个"锁还在不在"的检查函数，另一个同样的任务正在跑给 None。

    锁挂在单独的一个连接上；这个连接断了（云数据库断开空闲连接）MySQL 会自动放掉锁，所以每读一份前
    查一下锁还在不在，不在就停，免得两个任务同时读。"""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine.dialect.name != "mysql":
        yield lambda: True
        return
    with engine.connect() as conn:
        if conn.execute(text("SELECT GET_LOCK(:k, 0)"), {"k": _LOCK_NAME}).scalar() != 1:
            yield None
            return

        def held() -> bool:
            try:
                return (
                    conn.execute(
                        text("SELECT IS_USED_LOCK(:k) = CONNECTION_ID()"), {"k": _LOCK_NAME}
                    ).scalar()
                    == 1
                )
            except Exception:  # noqa: BLE001 - 连接断了：锁已经随连接放掉
                return False

        try:
            yield held
        finally:
            try:
                conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": _LOCK_NAME})
            except Exception:  # noqa: BLE001 - 连接已断，锁已随连接放掉
                log.warning("名单读取任务锁释放时连接已断开")


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
    row.reason = reason
    row.detail = (detail or "")[:300]
    if transient and row.status in ("ok", "partial"):
        # 读成功过：上次读到的那一行先留着给用户看，过一段时间再试
        row.retry_at = now + RETRY_AFTER
        return
    row.status = "failed"
    row.extractor = texts.EXTRACTOR_VERSION
    row.locator = LOCATOR_VERSION
    row.checked_at = now
    row.retry_at = now + (RETRY_AFTER if transient else FAILED_RECHECK)


def _note_error(session: Session, url: str, now: datetime, exc: Exception) -> None:
    """读的时候出了没料到的错（事务已回滚）：另开一个事务记成"暂时读不了"，过一段时间再试，
    免得每一轮都先卡在这一份上。"""
    try:
        row = _source_row(session, url, now)
        _record_failure(
            row, "unavailable", f"读取出错（{exc.__class__.__name__}）", now, transient=True
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 记不下来就算了，下一轮照样会再读
        session.rollback()


def _read_one(
    session: Session,
    url: str,
    fetcher,
    now: datetime,
    report: EvidenceReport,
    reason: str = "new",
) -> None:
    from youju.attachments import MAX_BYTES, fetch_failure

    row = _source_row(session, url, now)
    result = fetcher.fetch(url)
    if result.status != "ok":
        failure = fetch_failure(result)
        code = {"gone": "gone", "too_large": "too_large"}.get(failure.code, "unavailable")
        _record_failure(row, code, failure.detail, now, transient=failure.transient)
        report.failed[code] += 1
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
    digest = hashlib.sha256(content).hexdigest()
    if reason == "recheck" and row.status in ("ok", "partial") and row.content_sha256 == digest:
        # 到了复查时间、原网站的文件没变：不用重新取文字、定位
        row.checked_at = now
        row.retry_at = None
        report.unchanged += 1
        return
    extraction = texts.extract(content, limits=texts.LIST_LIMITS)
    row.content_sha256 = digest
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
                rows=[
                    _stored_row(by_n[n], name_columns) for n in result_found.segments if n in by_n
                ],
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
    seconds: float | None = RUN_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> EvidenceReport:
    """读这一轮该读的名单附件（最多 limit 份、seconds 秒，每份一个事务），定位条目、存命中行。"""
    from youju.config import get_settings
    from youju.crawler.fetcher import PoliteFetcher

    report = EvidenceReport()
    now = now or datetime.now(UTC)
    with _exclusive(session) as held:
        if held is None:
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
        started = clock()
        try:
            for url, reason in list(due.items())[:limit]:
                if seconds is not None and clock() - started > seconds:
                    report.out_of_time = True
                    break
                if not held():
                    report.lost_lock = True
                    log.warning("名单读取任务锁丢了（数据库连接断开），本轮停下")
                    break
                try:
                    _read_one(session, url, fetcher, now, report, reason)
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - 一份出错不影响其他份
                    session.rollback()
                    report.errors += 1
                    log.warning("名单附件读取出错：%s", exc.__class__.__name__)
                    _note_error(session, url, now, exc)
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
