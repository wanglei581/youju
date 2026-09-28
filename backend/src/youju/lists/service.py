"""名单回填：先补详情，再用同一个礼貌抓取器读取政府附件。"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from youju.classify import effective_kind, list_status
from youju.config import get_settings
from youju.crawler.detail_service import _government_url, fetch_details
from youju.crawler.fetcher import MAX_RESPONSE_BYTES, PoliteFetcher
from youju.lists.parser import ParsedEntry, parse_attachment_result, parse_html_tables
from youju.lists.rules import ListClassification, classify_title, valid_org_name
from youju.models import (
    SOURCE_URL_MAX,
    Document,
    DocumentDetail,
    ListEntry,
    ListParse,
    Source,
)

SUPPORTED = {"pdf", "xlsx", "xls", "docx"}
UNSUPPORTED = {"doc", "wps", "et", "ofd", "zip", "rar"}
PERMANENT = {"ok", "empty", "unsupported"}
_PROTECTED = re.compile(r"验证码|人机验证|安全验证|访问过于频繁|异常访问")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ListSample:
    title: str
    name: str
    status: str | None
    url: str


@dataclass(frozen=True)
class ListReport:
    documents: int
    parsed_documents: int
    names: int
    categories: dict[str, int]
    failures: dict[str, int]
    samples: list[ListSample]
    attachments_fetched: int
    attachments_deferred: int


def _url_too_long(url: str) -> bool:
    """解析记录按网址唯一，数据库里网址有长度上限；超长的多半是异常链接，跳过并记日志。"""
    if len(url) <= SOURCE_URL_MAX:
        return False
    log.warning("名单来源网址过长，跳过：%s…", url[:120])
    return True


def _should_attempt(record: ListParse | None) -> bool:
    return record is None or (
        record.status not in PERMANENT
        and record.attempted_at < datetime.now(UTC) - timedelta(hours=6)
    )


def _save(
    session: Session,
    doc: Document,
    url: str,
    kind: str,
    classification: ListClassification,
    entries: list[ParsedEntry] | None,
    failure: str | None = None,
    partial_reason: str | None = None,
) -> None:
    if _url_too_long(url):
        return
    record = session.scalar(
        select(ListParse).where(ListParse.document_id == doc.id, ListParse.source_url == url)
    )
    session.query(ListEntry).filter_by(document_id=doc.id, source_url=url).delete()
    if record is None:
        record = ListParse(document_id=doc.id, source_url=url)
        session.add(record)
    record.source_kind = kind
    record.status = (
        "unsupported"
        if failure and failure.startswith(("暂不支持", "非政府网站", "文件超过20MB"))
        else "failed"
        if failure
        else "ok"
        if entries
        else "empty"
    )
    record.failure_reason = (
        failure or partial_reason or ("未识别到企业或机构名称表格" if not entries else None)
    )
    record.entry_count = len(entries or [])
    record.attempted_at = datetime.now(UTC)
    for entry in entries or []:
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=url,
                name=entry.name,
                normalized_name=entry.normalized_name,
                credit_code=entry.credit_code,
                sequence=entry.sequence,
                category=classification.category,
                batch=classification.batch,
                region=entry.region,
            )
        )
    session.commit()


def _parse_source(
    session: Session,
    doc: Document,
    url: str,
    extension: str,
    kind: str,
    classification: ListClassification,
    fetcher: PoliteFetcher,
) -> None:
    if _url_too_long(url):
        return
    if extension not in SUPPORTED and extension != "html":
        _save(session, doc, url, kind, classification, None, "暂不支持旧版或专有格式")
        return
    if not _government_url(url):
        _save(session, doc, url, kind, classification, None, "非政府网站")
        return
    response = fetcher.fetch(url)
    if response.status != "ok":
        reason = (
            "文件超过20MB"
            if response.status == "too_large"
            else response.error or "非政府网站"
            if response.status == "unsupported_host"
            else f"抓取失败：{response.status}"
        )
        _save(session, doc, url, kind, classification, None, reason)
        return
    if not _government_url(response.final_url or url):
        _save(session, doc, url, kind, classification, None, "非政府网站重定向")
        return
    if len(response.content) > MAX_RESPONSE_BYTES:
        _save(session, doc, url, kind, classification, None, "文件超过20MB")
        return
    try:
        partial_reason = None
        if _PROTECTED.search(response.text[:1000]):
            raise ValueError("暂不支持防护页或验证码")
        actual_extension = (
            "pdf" if extension == "html" and response.content.startswith(b"%PDF-") else extension
        )
        if actual_extension == "html":
            entries = parse_html_tables(response.text)
        else:
            result = parse_attachment_result(response.content, actual_extension)
            entries, partial_reason = result.entries, result.failure_reason
    except Exception as exc:  # noqa: BLE001 - 损坏的外部附件不能中断整批回填
        reason = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith("暂不支持")
            else f"解析失败：{exc.__class__.__name__}"
        )
        _save(session, doc, url, kind, classification, None, reason)
    else:
        _save(session, doc, url, kind, classification, entries, partial_reason=partial_reason)


def _prune_invalid_entries(session: Session, selected_ids: list[int]) -> None:
    if not selected_ids:
        return
    invalid_ids: list[int] = []
    affected: set[tuple[int, str]] = set()
    rows = session.execute(
        select(
            ListEntry.id,
            ListEntry.document_id,
            ListEntry.source_url,
            ListEntry.name,
            ListEntry.credit_code,
        )
        .where(ListEntry.document_id.in_(selected_ids))
        .execution_options(yield_per=1000)
    )
    for entry_id, document_id, source_url, name, code in rows:
        if not valid_org_name(name, code):
            invalid_ids.append(entry_id)
            affected.add((document_id, source_url))
    if not invalid_ids:
        return
    for offset in range(0, len(invalid_ids), 1000):
        session.execute(
            delete(ListEntry).where(ListEntry.id.in_(invalid_ids[offset : offset + 1000]))
        )
    for document_id, source_url in affected:
        count = session.scalar(
            select(func.count(ListEntry.id)).where(
                ListEntry.document_id == document_id, ListEntry.source_url == source_url
            )
        )
        record = session.scalar(
            select(ListParse).where(
                ListParse.document_id == document_id, ListParse.source_url == source_url
            )
        )
        if record is not None:
            record.entry_count = count or 0
            if not count and record.status == "ok":
                record.status = "empty"
                if record.failure_reason is None:
                    record.failure_reason = "未识别到企业或机构名称表格"
    session.commit()


def _report(
    session: Session, selected_ids: list[int], attachments_fetched: int, attachments_deferred: int
) -> ListReport:
    if not selected_ids:
        return ListReport(0, 0, 0, {}, {}, [], attachments_fetched, attachments_deferred)
    names = (
        session.scalar(
            select(func.count(ListEntry.id)).where(ListEntry.document_id.in_(selected_ids))
        )
        or 0
    )
    successes = set(
        session.scalars(
            select(ListEntry.document_id).where(ListEntry.document_id.in_(selected_ids)).distinct()
        )
    )
    categories = dict(
        session.execute(
            select(ListEntry.category, func.count(ListEntry.id))
            .where(ListEntry.document_id.in_(selected_ids))
            .group_by(ListEntry.category)
        ).all()
    )
    samples = session.execute(
        select(ListEntry, Document)
        .join(Document)
        .where(
            ListEntry.id.in_(
                select(func.min(ListEntry.id))
                .where(ListEntry.document_id.in_(selected_ids))
                .group_by(ListEntry.document_id)
            )
        )
        .order_by(Document.publish_date.desc(), ListEntry.id)
    ).all()
    failures = Counter(
        reason
        for doc_id, status, reason in session.execute(
            select(ListParse.document_id, ListParse.status, ListParse.failure_reason).where(
                ListParse.document_id.in_(selected_ids), ListParse.failure_reason.is_not(None)
            )
        )
        if reason and (status != "empty" or doc_id not in successes)
    )
    distinct_samples: list[ListSample] = []
    sampled_titles: set[str] = set()
    for entry, doc in samples:
        if doc.title not in sampled_titles:
            sampled_titles.add(doc.title)
            distinct_samples.append(
                ListSample(
                    doc.title,
                    entry.name,
                    list_status(doc.title, effective_kind(doc.title, doc.kind_override)),
                    doc.url,
                )
            )
        if len(distinct_samples) == 10:
            break
    return ListReport(
        documents=len(selected_ids),
        parsed_documents=len(successes),
        names=names,
        categories=categories,
        failures=dict(failures),
        samples=distinct_samples,
        attachments_fetched=attachments_fetched,
        attachments_deferred=attachments_deferred,
    )


def parse_lists(
    session: Session,
    *,
    since: date = date(2026, 7, 1),
    attachment_limit: int = 200,
    fetcher: PoliteFetcher | None = None,
) -> ListReport:
    if attachment_limit < 1 or attachment_limit > 200:
        raise ValueError("单次附件上限必须在1到200之间")
    documents = session.scalars(
        select(Document)
        .join(Source)
        .where(Source.rights_level == 1, Document.publish_date >= since)
        .order_by(Document.id)
    ).all()
    selected = [
        (doc, info)
        for doc in documents
        if (info := classify_title(doc.title, doc.kind_override)).is_list
    ]
    selected_ids = [doc.id for doc, _ in selected]
    previously_parsed = set(
        session.scalars(
            select(ListParse.document_id)
            .join(Document, Document.id == ListParse.document_id)
            .where(Document.publish_date >= since)
            .distinct()
        )
    )
    stale_ids = previously_parsed - set(selected_ids)
    if stale_ids:
        session.query(ListEntry).filter(ListEntry.document_id.in_(stale_ids)).delete(
            synchronize_session=False
        )
        session.query(ListParse).filter(ListParse.document_id.in_(stale_ids)).delete(
            synchronize_session=False
        )
        session.commit()
    _prune_invalid_entries(session, selected_ids)
    settings = get_settings()
    owned = fetcher is None
    if fetcher is None:
        fetcher = PoliteFetcher(
            settings.user_agent,
            settings.request_timeout,
            settings.per_host_interval,
            url_allowed=_government_url,
        )
    try:
        missing = [
            doc.id
            for doc, _ in selected
            if (doc.detail is None or doc.detail.status != "ok")
            and urlsplit(doc.url).path.rsplit(".", 1)[-1].lower() not in SUPPORTED | UNSUPPORTED
        ]
        if missing:
            fetch_details(session, limit=len(missing), document_ids=missing, fetcher=fetcher)
        attachments_fetched = 0
        attachments_deferred = 0
        for doc, info in selected:
            path = urlsplit(doc.url).path.lower()
            direct_extension = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
            if direct_extension in UNSUPPORTED:
                record = session.scalar(
                    select(ListParse).where(
                        ListParse.document_id == doc.id, ListParse.source_url == doc.url
                    )
                )
                if _should_attempt(record):
                    _save(session, doc, doc.url, "detail", info, None, "暂不支持旧版或专有格式")
                continue
            if direct_extension in SUPPORTED:
                record = session.scalar(
                    select(ListParse).where(
                        ListParse.document_id == doc.id, ListParse.source_url == doc.url
                    )
                )
                if _should_attempt(record):
                    if attachments_fetched >= attachment_limit:
                        attachments_deferred += 1
                    else:
                        attachments_fetched += 1
                        _parse_source(
                            session, doc, doc.url, direct_extension, "detail", info, fetcher
                        )
                continue
            detail = session.get(DocumentDetail, doc.id)
            if detail is None or detail.status != "ok":
                record = session.scalar(
                    select(ListParse).where(
                        ListParse.document_id == doc.id, ListParse.source_url == doc.url
                    )
                )
                if _should_attempt(record):
                    _save(session, doc, doc.url, "detail", info, None, "详情不可用")
                continue
            html_url = detail.final_url or doc.url
            source_url = html_url
            record = session.scalar(
                select(ListParse).where(
                    ListParse.document_id == doc.id, ListParse.source_url == source_url
                )
            )
            if _should_attempt(record):
                _parse_source(
                    session,
                    doc,
                    source_url,
                    "html",
                    "detail",
                    info,
                    fetcher,
                )
            for attachment in detail.attachments:
                record = session.scalar(
                    select(ListParse).where(
                        ListParse.document_id == doc.id, ListParse.source_url == attachment.url
                    )
                )
                if not _should_attempt(record):
                    continue
                downloadable = attachment.extension.lower() in SUPPORTED and _government_url(
                    attachment.url
                )
                if downloadable and attachments_fetched >= attachment_limit:
                    attachments_deferred += 1
                    continue
                if downloadable:
                    attachments_fetched += 1
                _parse_source(
                    session,
                    doc,
                    attachment.url,
                    attachment.extension.lower(),
                    "attachment",
                    info,
                    fetcher,
                )
    finally:
        if owned:
            fetcher.close()
    return _report(session, selected_ids, attachments_fetched, attachments_deferred)
