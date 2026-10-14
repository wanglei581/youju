"""原网页还在不在。

10/15 全面测试：近 30 天的 832 份里有 20 份原网页已打不开，多是公示期满后撤下的。

每天抽一批近 90 天发布的公开文件，打开原文网址（同抓取一样遵守 robots、同一网站限速、YoujuBot 标识，
不绕过任何防护）。每份大约一周查一次，没查过的先查；上次是 404/410 的隔天再确认一次。
- 连续两次（相隔至少 20 小时）都是 404 或 410，才记"原网页已失效"（link_gone_at）；
- 一次能打开（200）就恢复；
- 403、5xx、超时、robots 不让访问：说明不了网页没了，状态不变，只记检查时间。
小程序详情页据此提示"原网页已撤下或改了地址"，正文照样可以看（我们保存的）。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from youju.config import get_settings
from youju.crawler.fetcher import FetchResult, PoliteFetcher
from youju.models import Document, Source
from youju.takedown import available_condition

log = logging.getLogger("youju.link_check")
BEIJING = ZoneInfo("Asia/Shanghai")
WINDOW = timedelta(days=90)  # 只查近 90 天发布的（申报和名单的时效）
RECHECK_AFTER = timedelta(days=6)  # 正常的大约一周查一次
CONFIRM_AFTER = timedelta(hours=20)  # 第一次 404/410 后隔天再确认
GONE_STATUSES = (404, 410)


@dataclass
class LinkOutcome:
    document_id: int
    result: FetchResult


def _government_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "gov.cn" or host.endswith(".gov.cn")


def _select(session: Session, now: datetime, limit: int) -> list[tuple[int, str]]:
    since = (now.astimezone(BEIJING) - WINDOW).date()
    base = (
        select(Document.id, Document.url)
        .join(Source, Source.id == Document.source_id)
        .where(Source.rights_level == 1, available_condition(), Document.publish_date >= since)
    )
    # 待确认的（上次 404/410、隔了 20 小时）最先；再是没查过的；再是一周前查过的，最早的先
    confirm = base.where(
        Document.link_suspect_at.is_not(None),
        Document.link_gone_at.is_(None),
        Document.link_checked_at <= now - CONFIRM_AFTER,
    ).order_by(Document.link_checked_at)
    due = base.where(
        or_(
            Document.link_checked_at.is_(None),
            and_(
                Document.link_checked_at <= now - RECHECK_AFTER,
                or_(Document.link_suspect_at.is_(None), Document.link_gone_at.is_not(None)),
            ),
        )
    ).order_by(Document.link_checked_at.is_not(None), Document.link_checked_at, Document.id)
    picked: list[tuple[int, str]] = []
    seen: set[int] = set()
    for stmt in (confirm, due):
        for doc_id, url in session.execute(stmt.limit(limit)).all():
            if doc_id not in seen and len(picked) < limit:
                picked.append((doc_id, url))
                seen.add(doc_id)
    return picked


def _apply(doc: Document, result: FetchResult, now: datetime) -> str:
    doc.link_checked_at = now
    if result.status == "ok":
        doc.link_suspect_at = None
        doc.link_gone_at = None
        return "ok"
    if result.status == "http_error" and result.http_status in GONE_STATUSES:
        if doc.link_suspect_at is None:
            doc.link_suspect_at = now
            return "suspect"
        if doc.link_gone_at is None and now - _utc(doc.link_suspect_at) >= CONFIRM_AFTER:
            doc.link_gone_at = now
            return "gone"
        return "gone" if doc.link_gone_at is not None else "suspect"
    return "unknown"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def check_links(
    session: Session,
    *,
    limit: int = 400,
    fetcher: PoliteFetcher | None = None,
    now: datetime | None = None,
    max_workers: int = 4,
) -> dict[str, int]:
    now = _utc(now or datetime.now(UTC))
    selected = _select(session, now, limit)
    # 查询事务先结束；网络请求期间不占用数据库连接
    session.commit()
    counts = {"checked": 0, "ok": 0, "suspect": 0, "gone": 0, "unknown": 0}
    if not selected:
        return counts

    def run(polite: PoliteFetcher) -> list[LinkOutcome]:
        by_host: dict[str, list[tuple[int, str]]] = {}
        for doc_id, url in selected:
            by_host.setdefault((urlsplit(url).hostname or "").lower(), []).append((doc_id, url))

        def run_host(items: list[tuple[int, str]]) -> list[LinkOutcome]:
            return [LinkOutcome(doc_id, polite.fetch(url)) for doc_id, url in items]

        # 网络并行只跨网站（同一网站按限速排队）；数据库写入在主线程
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            return [o for group in pool.map(run_host, by_host.values()) for o in group]

    if fetcher is not None:
        outcomes = run(fetcher)
    else:
        settings = get_settings()
        with PoliteFetcher(
            settings.user_agent,
            settings.request_timeout,
            settings.per_host_interval,
            url_allowed=_government_url,
        ) as polite:
            outcomes = run(polite)
    for outcome in outcomes:
        doc = session.get(Document, outcome.document_id)
        if doc is None:
            continue
        counts[_apply(doc, outcome.result, now)] += 1
        counts["checked"] += 1
    session.commit()
    log.info("原网页检查：%s", counts)
    return counts
