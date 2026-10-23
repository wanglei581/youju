"""按网站抓取详情，复用列表抓取器的 robots、限速与标识。"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from youju.categories import doc_category
from youju.classify import doc_kind
from youju.config import get_settings
from youju.crawler.detail import DetailResult, parse_detail
from youju.crawler.fetcher import FetchResult, PoliteFetcher
from youju.crawler.pdf import extract_pdf_body
from youju.crawler.titles import clean_title, completed_title, is_truncated
from youju.dbtypes import BODY_TEXT_MAX_CHARS
from youju.dedup import reconcile_documents
from youju.issuer import normalize_issuer
from youju.models import Document, DocumentAttachment, DocumentDetail, Source
from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetailFetchSummary:
    document_id: int
    status: str
    failure_reason: str | None = None


class DetailFetcher:
    def fetch(self, url: str) -> FetchResult:  # pragma: no cover - 接口定义
        raise NotImplementedError


_RETRY_DELAYS = (timedelta(hours=6), timedelta(hours=24))
REFETCH_FAILURE_PREFIX = "详情重抓未完成："
_FILE_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".wps", ".et", ".zip", ".rar", ".ofd")
# 政府网站的互动交流平台（意见征集、留言回复）：页面带验证码，按合规红线不抓，连请求都不发。
# 广东各级政府网站（广州、深圳科技局、广东省工信厅等）列表里的"公开征求意见"会链到这里
_INTERACTIVE_PATHS = ("/hdjlpt/",)
_INTERACTIVE_REASON = "互动交流平台页面带验证码，不抓"


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


def _interactive(url: str) -> bool:
    return any(part in urlsplit(url).path.lower() for part in _INTERACTIVE_PATHS)


def _detail_url_allowed(url: str) -> bool:
    """补正文时每一跳（含重定向）都查：只抓政府网站，不进互动交流平台。"""
    return _government_url(url) and not _interactive(url)


def _apply_full_title(doc: Document, title: str) -> None:
    """换成详情页的完整标题；按标题算的类型、类别和筛选标签随之重算（同入库和 youju reclassify）。
    人工定的类型（kind_override）不动。"""
    doc.title = title
    doc.rule_kind = doc_kind(title)
    doc.rule_category = doc_category(title)
    doc.rule_supports = SUPPORT_RULES.stored(title)
    doc.rule_industries = INDUSTRY_RULES.stored(title)


def _save_result(
    session: Session,
    outcome: _FetchOutcome,
) -> DetailFetchSummary:
    document_id = outcome.document_id
    doc = session.get(Document, document_id)
    if doc is None:
        return DetailFetchSummary(document_id, "missing", "题录已删除")
    detail = session.get(DocumentDetail, document_id)
    if detail is not None and detail.status == "ok" and outcome.status != "ok":
        # 补附件失败不能抹掉已经成功入库的正文、附件与原抓取时间。复用重试字段，
        # 用固定标记区分本次维护与首次抓取的次数；永久失败不自动重试。
        attempts = (
            (detail.attempt_count or 0) + 1
            if (detail.failure_reason or "").startswith(REFETCH_FAILURE_PREFIX)
            else 1
        )
        detail.attempt_count = attempts
        detail.failure_reason = REFETCH_FAILURE_PREFIX + outcome.status
        detail.next_retry_at = (
            outcome.fetched_at + _RETRY_DELAYS[attempts - 1]
            if attempts <= len(_RETRY_DELAYS)
            and _retryable(outcome.status, outcome.fetch.http_status if outcome.fetch else None)
            else None
        )
        session.commit()
        return DetailFetchSummary(document_id, outcome.status, outcome.failure_reason)
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
    body = outcome.parsed.body_text if outcome.parsed else ""
    # 数据库正文列有上限（MEDIUMTEXT 16MB），超长的截断，不让整份详情写入失败
    detail.body_text = body[:BODY_TEXT_MAX_CHARS]
    detail.issuer = (
        normalize_issuer(outcome.parsed.issuer)
        if outcome.parsed and outcome.parsed.issuer
        else None
    )
    detail.doc_number = outcome.parsed.doc_number if outcome.parsed else None
    detail.publish_date = outcome.parsed.publish_date if outcome.parsed else None
    detail.fetched_at = outcome.fetched_at
    detail.http_status = outcome.fetch.http_status if outcome.fetch else None
    detail.status = outcome.status
    detail.failure_reason = outcome.failure_reason
    detail.final_url = outcome.fetch.final_url if outcome.fetch else None
    titles = {doc.title}
    if outcome.parsed and outcome.status == "ok":
        detail.attachments = [
            DocumentAttachment(position=i, name=a.name, url=a.url, extension=a.extension)
            for i, a in enumerate(outcome.parsed.attachments)
        ]
        doc.status = "fetched"
        # 列表页截断的标题（以"..."或"…"结尾）用详情页标明的完整标题补全，对不上不改
        full = completed_title(doc.title, outcome.parsed.page_titles)
        if full is not None:
            _apply_full_title(doc, full)
            titles.add(full)
    # 截断的旧标题不参与归并；补全后按新标题归并（同一文件的其他网址可能早就是完整标题）
    reconcile_documents(session, titles)
    session.commit()
    return DetailFetchSummary(document_id, outcome.status, outcome.failure_reason)


def _fetch_one(document_id: int, url: str, fetcher: DetailFetcher) -> _FetchOutcome:
    now = datetime.now(UTC)
    if not _government_url(url):
        return _FetchOutcome(document_id, now, None, None, "unsupported_host", "非政府网站")
    if _interactive(url):
        return _FetchOutcome(document_id, now, None, None, "protected", _INTERACTIVE_REASON)
    if urlsplit(url).path.lower().endswith(_FILE_SUFFIXES) and not urlsplit(
        url
    ).path.lower().endswith(".pdf"):
        return _FetchOutcome(
            document_id, now, None, None, "unsupported_format", "链接不是 HTML 详情页"
        )
    fetched = fetcher.fetch(url)
    now = datetime.now(UTC)
    if fetched.status == "unsupported_host" and _interactive(fetched.final_url or ""):
        # 重定向进了互动交流平台：抓取器在那一跳之前就停了（见 _detail_url_allowed）
        return _FetchOutcome(document_id, now, fetched, None, "protected", _INTERACTIVE_REASON)
    if fetched.status != "ok":
        return _FetchOutcome(
            document_id, now, fetched, None, fetched.status, fetched.error or fetched.status
        )
    if not _government_url(fetched.final_url or url):
        return _FetchOutcome(document_id, now, fetched, None, "unsupported_host", "非政府网站")
    content_type = (fetched.content_type or "").split(";", 1)[0].strip().lower()
    is_pdf = (
        content_type == "application/pdf"
        or fetched.content.startswith(b"%PDF-")
        or fetched.text.lstrip().startswith("%PDF-")
    )
    if is_pdf:
        body, error = extract_pdf_body(fetched.content)
        parsed = DetailResult(body_text=body, failure_reason=error)
        return _FetchOutcome(
            document_id, now, fetched, parsed, "ok" if body else "pdf_error", error
        )
    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        return _FetchOutcome(
            document_id, now, fetched, None, "unsupported_format", "响应不是 HTML 详情页"
        )
    parsed = parse_detail(fetched.text, fetched.final_url or url)
    pdf_attachments = [a for a in parsed.attachments if a.extension == "pdf"]
    if not parsed.body_text and len(parsed.attachments) == 1 and len(pdf_attachments) == 1:
        pdf_url = pdf_attachments[0].url
        if not _government_url(pdf_url):
            return _FetchOutcome(
                document_id, now, fetched, parsed, "unsupported_host", "PDF 非政府网站"
            )
        pdf_fetch = fetcher.fetch(pdf_url)
        if pdf_fetch.status != "ok":
            return _FetchOutcome(
                document_id,
                now,
                pdf_fetch,
                parsed,
                pdf_fetch.status,
                pdf_fetch.error or pdf_fetch.status,
            )
        if not _government_url(pdf_fetch.final_url or pdf_url):
            return _FetchOutcome(
                document_id, now, pdf_fetch, parsed, "unsupported_host", "PDF 非政府网站"
            )
        body, error = extract_pdf_body(pdf_fetch.content)
        if error:
            return _FetchOutcome(document_id, now, pdf_fetch, parsed, "pdf_error", error)
        parsed.body_text = body
    status = (
        "protected"
        if parsed.failure_reason == "防护页或验证码"
        else "parse_empty"
        if parsed.failure_reason
        else "ok"
    )
    return _FetchOutcome(document_id, now, fetched, parsed, status, parsed.failure_reason)


def _fetch_outcomes(
    selected: list[tuple[int, str]], fetcher: DetailFetcher, max_workers: int
) -> dict[int, _FetchOutcome]:
    by_host: dict[str, list[tuple[int, str]]] = {}
    for document_id, url in selected:
        by_host.setdefault((urlsplit(url).hostname or "").lower(), []).append((document_id, url))

    def run_host(documents: list[tuple[int, str]]) -> list[_FetchOutcome]:
        results = []
        for document_id, url in documents:
            try:
                results.append(_fetch_one(document_id, url, fetcher))
            except Exception as exc:  # noqa: BLE001 - 一份异常文件不阻断整批详情保存
                # 依赖异常可能带请求信息；只记录类型，不记录异常正文或 traceback。
                reason = f"详情处理异常：{type(exc).__name__}"
                log.warning("详情 %s %s", document_id, reason)
                results.append(
                    _FetchOutcome(
                        document_id, datetime.now(UTC), None, None, "processing_error", reason
                    )
                )
        return results

    # 网络并行只跨网站；数据库写入仍在主线程，避免共享 Session。
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return {
            outcome.document_id: outcome
            for group in pool.map(run_host, by_host.values())
            for outcome in group
        }


def _fetch_selected(
    session: Session, selected: list[tuple[int, str]], fetcher: DetailFetcher, max_workers: int
) -> list[DetailFetchSummary]:
    if not selected:
        return []
    outcomes = _fetch_outcomes(selected, fetcher, max_workers)
    return [_save_result(session, outcomes[document_id]) for document_id, _ in selected]


def _polite_fetcher() -> PoliteFetcher:
    settings = get_settings()
    return PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        settings.per_host_interval,
        url_allowed=_detail_url_allowed,
    )


def fetch_details(
    session: Session,
    *,
    limit: int,
    source_id: int | None = None,
    fetcher: DetailFetcher | None = None,
    max_workers: int | None = None,
    since: date | None = None,
    notices_only: bool = False,
    document_ids: list[int] | None = None,
    refetch: bool = False,
) -> list[DetailFetchSummary]:
    """抓尚无详情或已到重试时间的公开政府文件；临时失败最多尝试三次。
    refetch：和 document_ids 一起用，详情已抓成功的也重抓一次（解析规则改进后补附件等）。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if refetch and document_ids is None:
        raise ValueError("refetch 必须指定 document_ids")
    now = datetime.now(UTC)
    stmt = (
        select(Document.id, Document.url)
        .join(Source, Document.source_id == Source.id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(Source.rights_level == 1)
        .order_by(Document.id)
        .limit(limit)
    )
    if not (refetch and document_ids is not None):
        stmt = stmt.where(
            or_(
                DocumentDetail.document_id.is_(None),
                and_(
                    DocumentDetail.next_retry_at.is_not(None),
                    DocumentDetail.next_retry_at <= now,
                    DocumentDetail.attempt_count < 3,
                ),
            )
        )
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    if since is not None:
        stmt = stmt.where(Document.publish_date >= since)
    if notices_only:
        stmt = stmt.where(
            or_(
                *(
                    Document.title.contains(term)
                    for term in ("申报", "征集", "推荐", "遴选", "认定")
                )
            )
        )
    if document_ids is not None:
        if not document_ids:
            return []
        stmt = stmt.where(Document.id.in_(document_ids))
    selected = [(int(i), url) for i, url in session.execute(stmt).all()]
    # 查询事务先结束；网络请求期间不占用数据库连接和行锁。
    session.commit()
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if fetcher is not None:
        return _fetch_selected(session, selected, fetcher, workers)
    with _polite_fetcher() as polite:
        return _fetch_selected(session, selected, polite, workers)


@dataclass(frozen=True)
class TitleBackfill:
    document_id: int
    old_title: str
    # filled：可补全（预览）或已补全（写入）；no_match：详情页没有对得上的完整标题，不改；
    # not_truncated：去掉装饰符和粘连的正文后不是截断标题（youju reclassify 会清理），不抓也不改；
    # unavailable：详情页没抓到或没解析出内容（原因见 reason）；missing：题录已删除
    result: str
    new_title: str | None = None
    reason: str | None = None
    page_titles: tuple[str, ...] = ()


def backfill_truncated_titles(
    session: Session,
    *,
    apply: bool = False,
    limit: int,
    source_id: int | None = None,
    since: date | None = None,
    after_id: int | None = None,
    fetcher: DetailFetcher | None = None,
    max_workers: int | None = None,
) -> list[TitleBackfill]:
    """一次性补全已入库的截断标题：重新打开详情页，按补详情时的同一规则（completed_title）找完整标题。

    默认只预览，apply=True 才写入。只改标题和按标题算的类型、类别、筛选标签，并按新标题重算归并；
    详情、附件、抓取记录都不动。补全后不再以省略号结尾，重复运行不会再选中，也就不会再改。
    按编号从小到大处理；对不上的每次都还会选中，分批时用 after_id 从上一批最后一份之后接着处理。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    stmt = (
        select(Document.id, Document.url, Document.title)
        .join(Source, Document.source_id == Source.id)
        .where(
            Source.rights_level == 1,
            or_(Document.title.like("%..."), Document.title.like("%…")),
        )
        .order_by(Document.id)
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    if since is not None:
        stmt = stmt.where(Document.publish_date >= since)
    if after_id is not None:
        stmt = stmt.where(Document.id > after_id)
    rows = [(int(i), url, title) for i, url, title in session.execute(stmt).all()]
    # 同补详情：网络请求期间不占用数据库连接和行锁。
    session.commit()
    selected = [(i, url) for i, url, title in rows if is_truncated(clean_title(title))]
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if not selected:
        outcomes: dict[int, _FetchOutcome] = {}
    elif fetcher is not None:
        outcomes = _fetch_outcomes(selected, fetcher, workers)
    else:
        with _polite_fetcher() as polite:
            outcomes = _fetch_outcomes(selected, polite, workers)
    results: list[TitleBackfill] = []
    for document_id, _, title in rows:
        outcome = outcomes.get(document_id)
        if outcome is None:
            results.append(TitleBackfill(document_id, title, "not_truncated"))
            continue
        if outcome.status != "ok" or outcome.parsed is None:
            reason = outcome.status
            results.append(TitleBackfill(document_id, title, "unavailable", reason=reason))
            continue
        page_titles = tuple(outcome.parsed.page_titles)
        doc = session.get(Document, document_id) if apply else None
        if apply:
            if doc is None:
                results.append(TitleBackfill(document_id, title, "missing"))
                continue
            # 按写入时库里的标题再判断一次：期间被别的任务改过的，按改过的算
            title = doc.title
        full = completed_title(title, page_titles)
        if full is None:
            result = "no_match" if is_truncated(clean_title(title)) else "not_truncated"
            results.append(TitleBackfill(document_id, title, result, page_titles=page_titles))
            continue
        if doc is not None:
            _apply_full_title(doc, full)
            reconcile_documents(session, {title, full})
            session.commit()
        results.append(TitleBackfill(document_id, title, "filled", full, page_titles=page_titles))
    return results
