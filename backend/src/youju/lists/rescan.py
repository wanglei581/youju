"""一次性：重抓还没读出企业名的名单详情，补上按新规则才认得出的附件。

10/16 爬虫开始认本省政府网站群统一的附件文件服务器（crawler/detail.py 的 _FILE_SERVERS，
如河南 xcoss.henan.gov.cn）。以前抓过的详情页里，这种附件当时没认出来、也没记下，
补详情和名单解析都不会再回头看。这里只挑这些省份网站上、详情抓成功了、还没读出企业名的名单，
默认只预览（礼貌地重新打开页面，按新规则解析，报告会多认出哪些附件，不写库），
加 apply 才用补详情的强制重抓写入；写入以后，下一轮名单解析（parse-lists）就会去读新附件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from youju.config import get_settings
from youju.crawler import detail_service
from youju.crawler.detail import _FILE_SERVERS
from youju.lists.rules import classify_title
from youju.models import Document, DocumentDetail, ListEntry, Source


@dataclass
class RescanResult:
    document_id: int
    title: str
    # 按新规则新认出的附件（名称, 网址）；页面没抓成功时为空、reason 写原因
    new_attachments: list[tuple[str, str]] = field(default_factory=list)
    reason: str | None = None


def file_server_suffixes() -> list[str]:
    return sorted(set(_FILE_SERVERS.values()))


def _candidates(session: Session, suffixes: list[str], limit: int) -> list[Document]:
    with_entries = select(ListEntry.document_id).distinct()
    documents = session.scalars(
        select(Document)
        .join(Source, Document.source_id == Source.id)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(
            Source.rights_level == 1,
            DocumentDetail.status == "ok",
            Document.id.not_in(with_entries),
            or_(*(Document.url.contains(f"{suffix}/") for suffix in suffixes)),
        )
        .order_by(Document.id)
    ).all()
    picked: list[Document] = []
    for doc in documents:
        host = (urlsplit(doc.url).hostname or "").lower()
        if not any(host.endswith(suffix) for suffix in suffixes):
            continue
        if not classify_title(doc.title, doc.kind_override).is_list:
            continue
        picked.append(doc)
        if len(picked) >= limit:
            break
    return picked


def rescan_list_details(
    session: Session,
    *,
    apply: bool = False,
    limit: int = 200,
    suffixes: list[str] | None = None,
    fetcher: detail_service.DetailFetcher | None = None,
    max_workers: int | None = None,
) -> list[RescanResult]:
    """预览或写入；返回每份名单新认出的附件。suffixes 默认是 _FILE_SERVERS 服务的省份。
    预览和写入走补详情同一套抓取和解析（detail_service._fetch_outcomes），预览看到的就是写入时会记下的。"""
    targets = _candidates(session, suffixes or file_server_suffixes(), limit)
    if not targets:
        return []
    titles = {doc.id: doc.title for doc in targets}
    known = {
        doc.id: {a.url for a in session.get(DocumentDetail, doc.id).attachments} for doc in targets
    }
    selected = [(doc.id, doc.url) for doc in targets]
    if apply:
        summaries = detail_service.fetch_details(
            session,
            limit=len(selected),
            document_ids=[document_id for document_id, _ in selected],
            fetcher=fetcher,
            max_workers=max_workers,
            refetch=True,
        )
        session.expire_all()
        by_id = {summary.document_id: summary for summary in summaries}
        results = []
        for document_id, _ in selected:
            summary = by_id.get(document_id)
            if summary is None or summary.status != "ok":
                # 重抓失败时补详情保留原来的正文和附件，这里只报原因
                reason = (summary.failure_reason or summary.status) if summary else "未重抓"
                results.append(RescanResult(document_id, titles[document_id], reason=reason))
                continue
            detail = session.get(DocumentDetail, document_id)
            fresh = [(a.name, a.url) for a in detail.attachments if a.url not in known[document_id]]
            results.append(RescanResult(document_id, titles[document_id], fresh))
        return results
    # 同补详情：网络请求期间不占用数据库连接和行锁
    session.commit()
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if fetcher is not None:
        outcomes = detail_service._fetch_outcomes(selected, fetcher, workers)
    else:
        with detail_service._polite_fetcher() as polite:
            outcomes = detail_service._fetch_outcomes(selected, polite, workers)
    results = []
    for document_id, _ in selected:
        outcome = outcomes[document_id]
        if outcome.status != "ok" or outcome.parsed is None:
            reason = outcome.failure_reason or outcome.status
            results.append(RescanResult(document_id, titles[document_id], reason=reason))
            continue
        fresh = [
            (a.name, a.url) for a in outcome.parsed.attachments if a.url not in known[document_id]
        ]
        results.append(RescanResult(document_id, titles[document_id], fresh))
    return results
