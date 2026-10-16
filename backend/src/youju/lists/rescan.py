"""一次性：重抓还没读出企业名的名单详情，补上按新规则才认得出的附件。

10/16 爬虫开始认本省政府网站群统一的附件文件服务器（crawler/detail.py 的 _FILE_SERVERS，
如河南 xcoss.henan.gov.cn）。以前抓过的详情页里，这种附件当时没认出来、也没记下，
补详情和名单解析都不会再回头看。这里只挑这些省份网站上、详情抓成功了、还没读出企业名的名单，
按补详情同一套抓取和解析（礼貌地重新打开页面）找出按新规则才认得出的附件：链到列出的文件服务器、
且这次打开的页面（跳转后）仍在这个省的网站上。别的附件变化不归这个命令管。
默认只预览，不写库；加 apply 才写入，而且只追加这些附件：正文、原有附件和抓取记录都不动，
页面临时缺内容也不会覆盖已有的。写入以后，下一轮名单解析（parse-lists）就会去读新附件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from youju.config import get_settings
from youju.crawler import detail_service
from youju.crawler.detail import _FILE_SERVERS, MAX_ATTACHMENTS, Attachment, _on_file_server
from youju.lists.rules import classify_title
from youju.models import Document, DocumentAttachment, DocumentDetail, ListEntry, Source


@dataclass
class RescanResult:
    document_id: int
    title: str
    # 按新规则新认出的附件（名称, 网址）；写入时是实际追加的。没抓成功或没写入时 reason 写原因
    new_attachments: list[tuple[str, str]] = field(default_factory=list)
    reason: str | None = None


def file_server_suffixes() -> list[str]:
    return sorted(set(_FILE_SERVERS.values()))


_BATCH = 500


def _candidates(session: Session, suffixes: list[str], limit: int) -> list[tuple[int, str, str]]:
    """返回 (编号, 网址, 标题)。只取需要的列、不读正文；按编号分批读，选够 limit 份就停。"""
    with_entries = select(ListEntry.document_id).distinct()
    # 粗筛：网址或跳转后的最终网址里有省份后缀（带端口的也算）；精确判断在下面
    near = or_(
        *(
            or_(Document.url.contains(suffix), DocumentDetail.final_url.contains(suffix))
            for suffix in suffixes
        )
    )
    picked: list[tuple[int, str, str]] = []
    after = 0
    while len(picked) < limit:
        rows = session.execute(
            select(
                Document.id,
                Document.url,
                Document.title,
                Document.kind_override,
                DocumentDetail.final_url,
            )
            .join(Source, Document.source_id == Source.id)
            .join(DocumentDetail, DocumentDetail.document_id == Document.id)
            .where(
                Source.rights_level == 1,
                DocumentDetail.status == "ok",
                Document.id.not_in(with_entries),
                near,
                Document.id > after,
            )
            .order_by(Document.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        after = rows[-1][0]
        for document_id, url, title, kind_override, final_url in rows:
            # 解析附件时按跳转后的最终网址判断页面在哪个网站，这里同样
            host = (urlsplit(final_url or url).hostname or "").lower()
            if not any(host.endswith(suffix) for suffix in suffixes):
                continue
            if not classify_title(title, kind_override).is_list:
                continue
            picked.append((int(document_id), url, title))
            if len(picked) >= limit:
                break
    return picked


def _append(
    session: Session, document_id: int, attachments: list[Attachment]
) -> tuple[list[Attachment], str | None]:
    """只追加：写入时再读一次已有附件，已有的跳过；总数不超过每页附件上限。"""
    detail = session.get(DocumentDetail, document_id)
    if detail is None or detail.status != "ok":
        session.rollback()
        return [], "详情已不是抓取成功状态，未写入"
    existing = {a.url for a in detail.attachments}
    position = max((a.position for a in detail.attachments), default=-1) + 1
    added: list[Attachment] = []
    over = 0
    for attachment in attachments:
        if attachment.url in existing:
            continue
        if len(detail.attachments) >= MAX_ATTACHMENTS:
            over += 1
            continue
        detail.attachments.append(
            DocumentAttachment(
                position=position,
                name=attachment.name,
                url=attachment.url,
                extension=attachment.extension,
            )
        )
        existing.add(attachment.url)
        position += 1
        added.append(attachment)
    session.commit()
    reason = f"附件已到 {MAX_ATTACHMENTS} 个上限，另有 {over} 个未写入" if over else None
    return added, reason


def rescan_list_details(
    session: Session,
    *,
    apply: bool = False,
    limit: int = 200,
    suffixes: list[str] | None = None,
    fetcher: detail_service.DetailFetcher | None = None,
    max_workers: int | None = None,
) -> list[RescanResult]:
    """预览或写入；返回每份名单按新规则新认出的附件。suffixes 默认是 _FILE_SERVERS 服务的省份。
    预览和写入走同一套抓取和解析（detail_service._fetch_outcomes），写入只是把预览的结果追加进去。"""
    targets = _candidates(session, suffixes or file_server_suffixes(), limit)
    if not targets:
        return []
    titles = {document_id: title for document_id, _, title in targets}
    known = {
        document_id: {a.url for a in session.get(DocumentDetail, document_id).attachments}
        for document_id, _, _ in targets
    }
    selected = [(document_id, url) for document_id, url, _ in targets]
    # 同补详情：网络请求期间不占用数据库连接和行锁
    session.commit()
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if fetcher is not None:
        outcomes = detail_service._fetch_outcomes(selected, fetcher, workers)
    else:
        with detail_service._polite_fetcher() as polite:
            outcomes = detail_service._fetch_outcomes(selected, polite, workers)
    results = []
    for document_id, url in selected:
        outcome = outcomes[document_id]
        if outcome.status != "ok" or outcome.parsed is None or outcome.fetch is None:
            # 没抓成功：什么都不写，原来的正文和附件照旧
            reason = outcome.failure_reason or outcome.status
            results.append(RescanResult(document_id, titles[document_id], reason=reason))
            continue
        # 只要按新规则才认得出的：链到文件服务器、且这次打开的页面（跳转后）仍在这个省的网站上。
        # 抓成功时 outcome.fetch 是详情页本身那次请求（只有附件 PDF 的页面另抓 PDF 取正文，
        # 也只在失败时才带回 PDF 那次请求），所以这里是页面网址，不是 PDF 网址
        page_host = urlsplit(outcome.fetch.final_url or url).hostname
        fresh = [
            a
            for a in outcome.parsed.attachments
            if a.url not in known[document_id] and _on_file_server(a.url, page_host)
        ]
        reason = None
        if apply and fresh:
            fresh, reason = _append(session, document_id, fresh)
        results.append(
            RescanResult(document_id, titles[document_id], [(a.name, a.url) for a in fresh], reason)
        )
    return results
