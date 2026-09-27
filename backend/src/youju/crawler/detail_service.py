"""按网站抓取详情，复用列表抓取器的 robots、限速与标识。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from youju.config import get_settings
from youju.crawler.detail import DetailResult, parse_detail
from youju.crawler.fetcher import FetchResult, PoliteFetcher
from youju.models import Document, DocumentAttachment, DocumentDetail, Source


@dataclass(frozen=True)
class DetailFetchSummary:
    document_id: int
    status: str
    failure_reason: str | None = None


class DetailFetcher:
    def fetch(self, url: str) -> FetchResult:  # pragma: no cover - 接口定义
        raise NotImplementedError


_RETRY_DELAYS = (timedelta(hours=6), timedelta(hours=24))
_FILE_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".wps", ".et", ".zip", ".rar", ".ofd")


@dataclass(frozen=True)
class _FetchOutcome:
    document_id: int
    fetched_at: datetime
    fetch: FetchResult | None
    parsed: DetailResult | None
    status: str
    failure_reason: str | None


def _retryable(status: str, http_status: int | None) -> bool:
    # robots.txt 暂不可用时禁止本次抓取，但可在退避后重新检查。
    return status in {"network_error", "robots_error"} or (
        status == "http_error" and http_status is not None and http_status >= 500
    )


def _government_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "gov.cn" or host.endswith(".gov.cn")


def _save_result(
    session: Session,
    outcome: _FetchOutcome,
) -> DetailFetchSummary:
    document_id = outcome.document_id
    doc = session.get(Document, document_id)
    if doc is None:
        return DetailFetchSummary(document_id, "missing", "题录已删除")
    detail = session.get(DocumentDetail, document_id)
    if detail is None:
        detail = DocumentDetail(document_id=document_id)
        session.add(detail)
    attempts = detail.attempt_count + 1 if detail.attempt_count is not None else 1
    detail.attempt_count = attempts
    detail.next_retry_at = (
        outcome.fetched_at + _RETRY_DELAYS[attempts - 1]
        if attempts <= len(_RETRY_DELAYS)
        and _retryable(outcome.status, outcome.fetch.http_status if outcome.fetch else None)
        else None
    )
    detail.body_text = outcome.parsed.body_text if outcome.parsed else ""
    detail.issuer = outcome.parsed.issuer if outcome.parsed else None
    detail.doc_number = outcome.parsed.doc_number if outcome.parsed else None
    detail.publish_date = outcome.parsed.publish_date if outcome.parsed else None
    detail.fetched_at = outcome.fetched_at
    detail.http_status = outcome.fetch.http_status if outcome.fetch else None
    detail.status = outcome.status
    detail.failure_reason = outcome.failure_reason
    detail.final_url = outcome.fetch.final_url if outcome.fetch else None
    if outcome.parsed and outcome.status == "ok":
        detail.attachments = [
            DocumentAttachment(position=i, name=a.name, url=a.url, extension=a.extension)
            for i, a in enumerate(outcome.parsed.attachments)
        ]
        doc.status = "fetched"
    session.commit()
    return DetailFetchSummary(document_id, outcome.status, outcome.failure_reason)


def _fetch_one(document_id: int, url: str, fetcher: DetailFetcher) -> _FetchOutcome:
    now = datetime.now(UTC)
    if not _government_url(url):
        return _FetchOutcome(document_id, now, None, None, "unsupported_host", "非政府网站")
    if urlsplit(url).path.lower().endswith(_FILE_SUFFIXES):
        return _FetchOutcome(
            document_id, now, None, None, "unsupported_format", "链接不是 HTML 详情页"
        )
    fetched = fetcher.fetch(url)
    now = datetime.now(UTC)
    if fetched.status != "ok":
        return _FetchOutcome(
            document_id, now, fetched, None, fetched.status, fetched.error or fetched.status
        )
    if not _government_url(fetched.final_url or url):
        return _FetchOutcome(document_id, now, fetched, None, "unsupported_host", "非政府网站")
    content_type = (fetched.content_type or "").split(";", 1)[0].strip().lower()
    if (
        content_type and content_type not in {"text/html", "application/xhtml+xml"}
    ) or fetched.text.lstrip().startswith("%PDF-"):
        return _FetchOutcome(
            document_id, now, fetched, None, "unsupported_format", "响应不是 HTML 详情页"
        )
    parsed = parse_detail(fetched.text, fetched.final_url or url)
    status = (
        "protected"
        if parsed.failure_reason == "防护页或验证码"
        else "parse_empty"
        if parsed.failure_reason
        else "ok"
    )
    return _FetchOutcome(document_id, now, fetched, parsed, status, parsed.failure_reason)


def _fetch_selected(
    session: Session, selected: list[tuple[int, str]], fetcher: DetailFetcher, max_workers: int
) -> list[DetailFetchSummary]:
    if not selected:
        return []
    by_host: dict[str, list[tuple[int, str]]] = {}
    for document_id, url in selected:
        by_host.setdefault((urlsplit(url).hostname or "").lower(), []).append((document_id, url))

    def run_host(documents: list[tuple[int, str]]) -> list[_FetchOutcome]:
        return [_fetch_one(document_id, url, fetcher) for document_id, url in documents]

    # 网络并行只跨网站；数据库写入仍在主线程，避免共享 Session。
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        outcomes = {
            outcome.document_id: outcome
            for group in pool.map(run_host, by_host.values())
            for outcome in group
        }
    return [_save_result(session, outcomes[document_id]) for document_id, _ in selected]


def fetch_details(
    session: Session,
    *,
    limit: int,
    source_id: int | None = None,
    fetcher: DetailFetcher | None = None,
    max_workers: int | None = None,
) -> list[DetailFetchSummary]:
    """抓尚无详情或已到重试时间的公开政府文件；临时失败最多尝试三次。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    now = datetime.now(UTC)
    stmt = (
        select(Document.id, Document.url)
        .join(Source, Document.source_id == Source.id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(
            Source.rights_level == 1,
            or_(
                DocumentDetail.document_id.is_(None),
                and_(
                    DocumentDetail.next_retry_at.is_not(None),
                    DocumentDetail.next_retry_at <= now,
                    DocumentDetail.attempt_count < 3,
                ),
            ),
        )
        .order_by(Document.id)
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    selected = [(int(i), url) for i, url in session.execute(stmt).all()]
    # 查询事务先结束；网络请求期间不占用数据库连接和行锁。
    session.commit()
    settings = get_settings()
    workers = max_workers if max_workers is not None else settings.max_workers
    if fetcher is not None:
        return _fetch_selected(session, selected, fetcher, workers)
    with PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        settings.per_host_interval,
        url_allowed=_government_url,
    ) as polite:
        return _fetch_selected(session, selected, polite, workers)
