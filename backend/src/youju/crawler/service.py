"""检查来源：抓列表页 → 识别条目 → 入库新条目 → 记录检查结果 → 更新来源健康。"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import ListItem, extract_items
from youju.crawler.urls import host_of
from youju.models import Document, Source, SourceCheck

log = logging.getLogger(__name__)

PARSE_EMPTY_MESSAGE = (
    "页面可以访问，但没有识别到列表条目：可能网站改版，或列表由脚本加载需要浏览器渲染"
)


class Fetcher:
    """抓取器接口；测试时可以换成假的实现。"""

    def fetch(self, url: str) -> FetchResult:  # pragma: no cover - 接口定义
        raise NotImplementedError


@dataclass
class CheckSummary:
    source_id: int
    status: str
    items_found: int
    items_new: int
    error: str | None
    # True：这是该来源第一次成功检查，新入库的条目都是基线，不算"新增"
    baseline: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def save_items(
    session: Session, source: Source, items: Sequence[ListItem], baseline: bool, seen_at: datetime
) -> int:
    """新网址插入，已存在的网址只更新"最后一次看到"的时间。返回新插入的条数。"""
    if not items:
        return 0
    rows = [
        {
            "source_id": source.id,
            "url": item.url,
            "title": item.title,
            "publish_date": item.publish_date,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "is_baseline": baseline,
            "status": "discovered",
        }
        for item in items
    ]
    stmt = (
        pg_insert(Document)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Document.url])
        .returning(Document.id)
    )
    inserted = session.execute(stmt).scalars().all()
    session.execute(
        update(Document)
        .where(Document.url.in_([item.url for item in items]))
        .values(last_seen_at=seen_at)
    )
    return len(inserted)


def _update_health(
    source: Source, status: str, error: str | None, threshold: int, at: datetime
) -> None:
    source.last_check_at = at
    if status == "ok":
        source.health = "ok"
        source.consecutive_failures = 0
        source.last_success_at = at
        source.last_error = None
        return
    source.last_error = error
    if status == "robots_disallowed":
        source.health = "blocked"
        return
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    if source.consecutive_failures >= threshold:
        source.health = "failing"
        if source.consecutive_failures == threshold:
            # 目前写日志；后续迭代接入邮件或企业微信提醒
            log.warning(
                "来源连续 %s 次检查失败：#%s %s %s %s（%s）",
                threshold,
                source.id,
                source.province,
                source.dept_full_name,
                source.column_name,
                error,
            )
    else:
        source.health = "degraded"


def check_source(
    session: Session,
    source: Source,
    fetcher: Fetcher,
    failure_threshold: int = 3,
    today: date | None = None,
) -> SourceCheck:
    started = _now()
    t0 = time.monotonic()
    result = fetcher.fetch(source.url)
    status, error = result.status, result.error
    items: list[ListItem] = []
    new_count = 0
    if result.status == "ok":
        items = extract_items(
            result.text, result.final_url or source.url, source.parser_config, today=today
        )
        if items:
            # 来源第一次成功检查时看到的条目算基线，不计入"新增"
            baseline = source.last_success_at is None
            new_count = save_items(session, source, items, baseline=baseline, seen_at=started)
        else:
            status, error = "parse_empty", PARSE_EMPTY_MESSAGE
    finished = _now()
    check = SourceCheck(
        source_id=source.id,
        started_at=started,
        finished_at=finished,
        status=status,
        http_status=result.http_status,
        items_found=len(items),
        items_new=new_count,
        duration_ms=int((time.monotonic() - t0) * 1000),
        error=error,
    )
    session.add(check)
    _update_health(source, status, error, failure_threshold, finished)
    session.commit()
    return check


def crawl_sources(
    session_factory: Callable[[], Session],
    source_ids: Sequence[int],
    fetcher: Fetcher,
    max_workers: int = 8,
    failure_threshold: int = 3,
) -> list[CheckSummary]:
    """按网站分组：不同网站并行，同一网站内串行（配合抓取器的限速）。"""
    with session_factory() as session:
        sources = session.scalars(select(Source).where(Source.id.in_(source_ids))).all()
        by_host: dict[str, list[int]] = defaultdict(list)
        for source in sources:
            by_host[host_of(source.url)].append(source.id)

    def run_host(ids: list[int]) -> list[CheckSummary]:
        summaries = []
        with session_factory() as session:
            for source_id in ids:
                source = session.get(Source, source_id)
                if source is None:
                    continue
                first_success = source.last_success_at is None
                try:
                    check = check_source(session, source, fetcher, failure_threshold)
                except Exception:  # 单个来源出错不影响其他来源
                    session.rollback()
                    log.exception("检查来源 #%s 时出错", source_id)
                    summaries.append(CheckSummary(source_id, "internal_error", 0, 0, "内部错误"))
                    continue
                summaries.append(
                    CheckSummary(
                        source_id,
                        check.status,
                        check.items_found,
                        check.items_new,
                        check.error,
                        baseline=first_success and check.status == "ok",
                    )
                )
        return summaries

    results: list[CheckSummary] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for summaries in pool.map(run_host, by_host.values()):
            results.extend(summaries)
    return results
