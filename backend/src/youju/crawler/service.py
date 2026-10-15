"""检查来源：抓列表页 → 识别条目 → 入库新条目 → 记录检查结果 → 更新来源健康。"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from youju.categories import doc_category
from youju.classify import doc_kind
from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import ListItem, extract_items
from youju.crawler.urls import host_of, public_url, url_key
from youju.dedup import reconcile_documents
from youju.models import URL_KEY_MAX, Document, Source, SourceCheck
from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

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
    recorded: bool = True


def _now() -> datetime:
    return datetime.now(UTC)


def save_items(
    session: Session, source: Source, items: Sequence[ListItem], baseline: bool, seen_at: datetime
) -> int:
    """新文件插入，已存在的文件只更新"最后一次看到"的时间。返回新插入的条数。

    以规范网址键判断是不是同一份文件（http 与 https、会话参数等不同的网址算同一份）。
    用一条 INSERT … ON DUPLICATE KEY UPDATE 完成，并按规范网址键排序：
    多个线程同时写入有重叠的文件时，加锁顺序一致，不会死锁。
    """
    unique: dict[str, ListItem] = {}
    for item in items:
        key = url_key(item.url)
        if len(key) > URL_KEY_MAX:
            # 数据库唯一索引有长度上限；政府网站的网址一般一两百字，超长的多半是异常链接
            log.warning("网址过长，跳过：来源 #%s %s…", source.id, key[:120])
            continue
        unique.setdefault(key, item)
    if not unique:
        return 0
    rows = [
        {
            "source_id": source.id,
            "url": public_url(item.url),
            "url_key": key,
            "title": item.title,
            # 一次插入多行时字段的自动默认值取不到本行的标题，这里直接按标题规则算好类型
            "rule_kind": doc_kind(item.title),
            "rule_category": doc_category(item.title),
            "rule_supports": SUPPORT_RULES.stored(item.title),
            "rule_industries": INDUSTRY_RULES.stored(item.title),
            "publish_date": item.publish_date,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "is_baseline": baseline,
            "status": "discovered",
        }
        for key, item in sorted(unique.items())
    ]
    # 先查哪些已经在库里，再写入。只影响"新增"计数：极少数情况下另一个线程恰好在两步之间
    # 写入了同一份文件，会多算一份，入库结果本身不受影响
    existing = set(
        session.scalars(select(Document.url_key).where(Document.url_key.in_(list(unique))))
    )
    stmt = mysql_insert(Document).values(rows)
    stmt = stmt.on_duplicate_key_update(last_seen_at=stmt.inserted.last_seen_at)
    session.execute(stmt)
    inserted_count = len(rows) - len(existing)
    reconcile_documents(session, {item.title for item in unique.values()})
    return inserted_count


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


def _lock_source(session: Session, source: Source) -> None:
    """锁住来源这一行直到提交，并重新读取它的状态。

    "是不是第一次成功检查"要在锁内重新读取：两次检查同时处理同一来源时，
    后提交的一方能看到前一方的结果，不会把期间新发布的文件也记成基线。
    """
    stmt = (
        select(Source)
        .where(Source.id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session.scalars(stmt).one()


def check_source(
    session: Session,
    source: Source,
    fetcher: Fetcher,
    failure_threshold: int = 3,
    today: date | None = None,
) -> SourceCheck:
    """检查一个来源：先抓取和识别，再在一个短事务里入库、记录结果。

    网络请求可能持续几十秒，期间不开数据库事务、不持有锁（开始前先结束会话里已有的事务）。
    """
    url, parser_config = source.url, source.parser_config
    session.commit()
    started = _now()
    t0 = time.monotonic()
    result = fetcher.fetch(url)
    status, error = result.status, result.error
    items: list[ListItem] = []
    if result.status == "ok":
        items = extract_items(result.text, result.final_url or url, parser_config, today=today)
        if not items:
            status, error = "parse_empty", PARSE_EMPTY_MESSAGE

    _lock_source(session, source)
    # 来源第一次成功检查时看到的条目算基线，不计入"新增"
    baseline = bool(items) and source.last_success_at is None
    new_count = save_items(session, source, items, baseline=baseline, seen_at=started)
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
        is_baseline=baseline,
    )
    session.add(check)
    _update_health(source, status, error, failure_threshold, finished)
    session.commit()
    return check


def _record_internal_error(
    session: Session, source_id: int, threshold: int, started: datetime, t0: float
) -> bool:
    """原事务可能已经失效；重新锁定来源记失败，不能让今天旧的成功掩盖异常。"""
    try:
        session.rollback()
        source = session.get(Source, source_id)
        if source is None:
            return False
        _lock_source(session, source)
        finished = _now()
        session.add(
            SourceCheck(
                source_id=source_id,
                started_at=started,
                finished_at=finished,
                status="internal_error",
                items_found=0,
                items_new=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error="内部错误",
                is_baseline=False,
            )
        )
        _update_health(source, "internal_error", "内部错误", threshold, finished)
        session.commit()
        return True
    except Exception:
        with suppress(Exception):
            session.rollback()
        # 异常可能含 SQL 参数或响应原文，只记录固定描述与来源编号。
        log.error("来源 #%s 的失败记录未能保存", source_id)
        return False


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
                started, t0 = _now(), time.monotonic()
                try:
                    source = session.get(Source, source_id)
                    if source is None:
                        continue
                    check = check_source(session, source, fetcher, failure_threshold)
                except Exception:  # 单个来源出错不影响其他来源
                    log.error("检查来源 #%s 时出错（内部错误）", source_id)
                    recorded = _record_internal_error(
                        session, source_id, failure_threshold, started, t0
                    )
                    summaries.append(
                        CheckSummary(
                            source_id, "internal_error", 0, 0, "内部错误", recorded=recorded
                        )
                    )
                    continue
                summaries.append(
                    CheckSummary(
                        source_id,
                        check.status,
                        check.items_found,
                        check.items_new,
                        check.error,
                        baseline=check.is_baseline,
                    )
                )
        return summaries

    results: list[CheckSummary] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for summaries in pool.map(run_host, by_host.values()):
            results.extend(summaries)
    return results
