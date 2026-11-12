"""检查来源：抓列表页 → 识别条目 → 入库新条目 → 记录检查结果 → 更新来源健康。"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from youju.categories import doc_category
from youju.classify import doc_kind
from youju.crawler.adapters import (
    LIST_API_OPEN_KEY,
    LIST_API_REFUSED,
    ListApi,
    api_items_merged,
    api_parts,
    api_readable,
    api_refusal,
    find_list_api,
    gkmlpt_categories,
)
from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import ListItem, extract_items
from youju.crawler.urls import host_of, public_url, url_key
from youju.dedup import reconcile_documents
from youju.models import URL_KEY_MAX, Document, Source, SourceCheck, TakedownBlock
from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

log = logging.getLogger(__name__)

PARSE_EMPTY_MESSAGE = (
    "页面可以访问，但没有识别到列表条目：可能网站改版，或列表由脚本加载需要浏览器渲染"
)


class Fetcher:
    """抓取器接口；测试时可以换成假的实现。"""

    def fetch(
        self, url: str, same_address_only: bool = False
    ) -> FetchResult:  # pragma: no cover - 接口定义
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
    # 首发前搬家（D-127）带过来的已下架网址：不再收录，已经在库里的也不更新
    # （见 youju/takedown_carry.py）
    blocked = set(
        session.scalars(
            select(TakedownBlock.url_key).where(TakedownBlock.url_key.in_(list(unique)))
        )
    )
    if blocked:
        log.info("来源 #%s：%d 条是已下架的网址，不收录", source.id, len(blocked))
        for key in blocked:
            del unique[key]
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
    if status in ("robots_disallowed", LIST_API_REFUSED):
        # robots 禁止、列表接口被读取护栏（D-128）拦下：记不能抓、停抓，不再计失败次数
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


# 通用规则在栏目页上识别出的条目少于这个数，才去看页面有没有声明列表接口
ADAPTER_MIN_ITEMS = 3
# 页面上的列表改由接口加载、来源却没标"经接口读"时记的原因（D-128：不读，等复检后再打开）
NEEDS_REVIEW_MESSAGE = "页面改用接口加载，待复检（来源没有“经接口读”的标记，D-128）"
# 数字广东信息公开平台的分类栏目，部门页上认不出列表接口时记的原因（不请求接口）
GKMLPT_UNRECOGNIZED_MESSAGE = (
    "部门页上认不出数字广东信息公开平台的列表接口（页面可能改版，或分类不在分类树里、跳到别处），"
    "待复检"
)


# 来源 parser_config 里记下列表接口被读取护栏（D-128）拦下（接口网址、原因、时间）：例行抓取
# 不带 --skip-blocked，不能只靠 health。栏目页还声明着同一个接口时，30 天内不再请求、照旧记
# 不能抓；页面换了接口，或过了 30 天（和试读对 401/403 等的复查间隔一样），再请求一次
LIST_API_REFUSED_KEY = "list_api_refused"
LIST_API_RECHECK_DAYS = 30


def _refused_before(parser_config: dict, api_url: str, now: datetime) -> str:
    note = parser_config.get(LIST_API_REFUSED_KEY)
    if not isinstance(note, dict) or note.get("url") != api_url:
        return ""
    try:
        if now - datetime.fromisoformat(str(note.get("at"))) > timedelta(
            days=LIST_API_RECHECK_DAYS
        ):
            return ""
    except (TypeError, ValueError):
        return ""
    return str(note.get("reason") or "列表接口被读取护栏拦下")


@dataclass
class _AdapterResult:
    items: list[ListItem]
    adapter: str | None = None  # 用了哪个平台适配器（认出了列表接口才有）
    fetch: FetchResult | None = None  # 接口没取到时的抓取结果
    refused: str = ""  # 读取护栏（D-128）拦下的原因：记"不能抓"、不读
    api_url: str = ""  # 认出的列表接口网址
    part: str = ""  # 几个分类合读时出问题的是哪个分类（"分类 编号："），写进原因


def _items_via_adapter(
    fetcher: Fetcher,
    page: FetchResult,
    url: str,
    parser_config: dict,
    today: date | None,
    api: ListApi | None = None,
) -> _AdapterResult:
    """栏目页自己声明了集约化平台的列表接口（见 crawler/adapters.py）时，按它取列表。

    只请求页面写明的那个接口地址、参数原样照页面（D-128 读取护栏 ①），不另外构造请求；数字广东的
    地址只用页面写明的值、照它自己平台脚本的拼法得到，和它自己的页面发出的请求一样（D-013，见
    crawler/adapters.py 开头的说明）。"""
    api = api or find_list_api(page.text, page.final_url or url, url)
    if api is None or parser_config.get(LIST_API_OPEN_KEY) != api.adapter:
        # 没认出接口，或来源没标"经接口读"（D-128）：不请求接口（调用的地方本来就先查过标记）
        return _AdapterResult([])
    # 只取页面写明的那个地址，跳到别处不跟（D-128 读取护栏 ①）
    parts = api_parts(api)
    texts: list[str] = []
    for part in parts:
        # 几个分类合读时一个一个取、一个一个查：前一个被拦下、没取到、读不出，后面的不再请求
        # （D-013、D-128：网站拒绝了就停，不接着敲门）；原因里写明出问题的是哪个分类
        label = f"分类 {part.category_id}：" if len(parts) > 1 else ""
        result = fetcher.fetch(part.url, same_address_only=True)
        if result.status == "robots_disallowed":
            return _AdapterResult([], api.adapter, result, api_url=api.url, part=label)
        refused = api_refusal(part, result)
        if refused:
            return _AdapterResult([], api.adapter, result, f"{label}{refused}", api.url, label)
        if result.status != "ok":
            return _AdapterResult([], api.adapter, result, api_url=api.url, part=label)
        if len(parts) > 1 and not api_readable(part, result.text):
            # 几个分类合读时，有一个回了但读不出（空白、维护页）：整栏这次没读出，记"读不出"、
            # 写明是哪个分类，下次再试；不拿别的分类的条目顶上
            return _AdapterResult([], api.adapter, api_url=api.url, part=label)
        texts.append(result.text)
    items = api_items_merged(api, texts, parser_config, today)
    return _AdapterResult(items, api.adapter, api_url=api.url)


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
    first_via_adapter = False
    # 要写回来源 parser_config 的改动：加锁以后合到库里最新的配置上，不拿加锁前的旧副本整个
    # 覆盖（两次检查同时进行时，不会把对方刚记下的拦下记录冲掉）
    config_set: dict[str, object] = {}
    config_drop: dict[str, object] = {}  # 要去掉的项 → 本次看到的旧值（库里还是它才去掉）
    if result.status == "ok":
        # 数字广东信息公开平台的分类：列表只在接口里（按分类加载），部门页上识别出的条目不用，
        # 一定去认接口
        gkmlpt = gkmlpt_categories(url) is not None
        items = (
            []
            if gkmlpt
            else extract_items(result.text, result.final_url or url, parser_config, today=today)
        )
        api = None
        if gkmlpt or (len(items) < ADAPTER_MIN_ITEMS and not parser_config.get("item_selector")):
            api = find_list_api(result.text, result.final_url or url, url)
        if gkmlpt and api is None:
            # 部门页上认不出数字广东的列表接口（改版、分类没了或跳到别处）：不请求接口，记读不出、
            # 写明原因，进失败清单等复检
            status, error = "parse_empty", GKMLPT_UNRECOGNIZED_MESSAGE
        elif api is not None and parser_config.get(LIST_API_OPEN_KEY) != api.adapter:
            # D-128：来源没标"经接口读"（复检、抽查合格、打开时才写）——不走接口、不读，也不悄悄
            # 记成 0 条：记"读不出"并写明待复检，进失败清单等复检
            items = []
            status, error = "parse_empty", NEEDS_REVIEW_MESSAGE
            log.warning("来源 #%s 页面改用接口加载，待复检（没有经接口读的标记）", source.id)
            api = None
        before = _refused_before(parser_config, api.url, started) if api is not None else ""
        if before:
            # 栏目页还声明着之前被读取护栏（D-128）拦下的接口：30 天内不再请求，照旧记不能抓
            items = []
            status, error = LIST_API_REFUSED, f"{before}（之前被读取护栏拦下，暂不再请求）"
            result = FetchResult(url, LIST_API_REFUSED, http_status=result.http_status)
        elif api is not None:
            via = _items_via_adapter(fetcher, result, url, parser_config, today, api)
            note = parser_config.get(LIST_API_REFUSED_KEY)
            moved = not isinstance(note, dict) or note.get("url") != via.api_url
            if note is not None and not via.refused and (moved or via.fetch is None):
                # 页面换了接口，或过了 30 天再请求、接口正常返回没被拦：去掉旧记录（这次只是
                # 网络不通、5xx 的，旧记录留着）
                config_drop[LIST_API_REFUSED_KEY] = note
            if via.items:
                items = via.items
                # 第一次经列表接口读出整页：以前只认出零星几条的来源，整页旧通知不能算"新增"
                first_via_adapter = parser_config.get("list_api") != via.adapter
                if first_via_adapter:
                    config_set["list_api"] = via.adapter
            elif via.refused:
                # 读取护栏（D-128）：接口返回不是成功、带权限要求、要登录或验证码、明确拒绝
                # → 记不能抓、停抓；栏目页上零星认出的一两条也不收（列表在接口里，读不了）
                items = []
                status, error = LIST_API_REFUSED, f"列表接口（{via.adapter}）：{via.refused}"
                note = {"url": via.api_url, "reason": error, "at": started.isoformat()}
                config_set[LIST_API_REFUSED_KEY] = note
                if via.fetch is not None and via.fetch.http_status:
                    result = FetchResult(url, LIST_API_REFUSED, http_status=via.fetch.http_status)
            elif via.fetch is not None and via.fetch.status == "robots_disallowed":
                # 列表接口被 robots 禁止：和读取护栏一样，零星条目不收，记 robots 禁止（停抓）
                items = []
                status = via.fetch.status
                error = f"列表接口（{via.adapter}）：{via.part}{via.fetch.error}"
            elif via.fetch is not None:
                # 列表接口这次没取到（网络不通、超时、5xx 等）：照实记失败、下次再试，不当成
                # "改版认不出"；栏目页上零星认出的一两条也不收，免得把这次失败盖成成功
                items = []
                why = via.fetch.error or (
                    f"要跳转到 {via.fetch.final_url}，只请求页面写明的地址、不跟"
                    if via.fetch.status == "redirect"
                    else via.fetch.status
                )
                status, error = via.fetch.status, f"列表接口（{via.adapter}）：{via.part}{why}"
                if via.fetch.http_status:
                    result = FetchResult(url, "http_error", http_status=via.fetch.http_status)
            else:
                # 接口回了，但读不出列表（空白、不是 JSON、没有列表那段，或列表本来就空）：
                # 栏目页零星认出的一两条也不收，按"没识别到列表条目"记，下次再试
                items = []
                if via.part:
                    status = "parse_empty"
                    error = f"列表接口（{via.adapter}）：{via.part}返回读不出列表，下次再试"
        if not items and status == "ok":
            status, error = "parse_empty", PARSE_EMPTY_MESSAGE

    _lock_source(session, source)
    if config_set or config_drop:
        # 记下这个栏目要经列表接口读，或列表接口被读取护栏拦下（以后不再请求）、去掉旧记录
        # 要去掉的项，只有库里还是本次看到的那个值才去掉（别的检查刚记下的新拦下记录不动）
        fresh = {
            k: v
            for k, v in (source.parser_config or {}).items()
            if k not in config_drop or v != config_drop[k]
        }
        source.parser_config = {**fresh, **config_set}
    # 来源第一次成功检查时看到的条目算基线，不计入"新增"；第一次经列表接口读出的整页也算基线
    baseline = bool(items) and (source.last_success_at is None or first_via_adapter)
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
