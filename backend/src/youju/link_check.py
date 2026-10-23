"""原网页还在不在。

10/15 全面测试：近 30 天的 832 份里有 20 份原网页已打不开，多是公示期满后撤下的。
10/22 宁夏科技厅一份 10/21 发的申报通知第二天就撤稿了，一周一查要一两周才标出来，
期间还能开始 AI 解读（设计说明：docs/设计/原网页撤稿检查_2026-10-22.md）。

每天抽一批近 90 天发布的公开文件，打开原文网址（同抓取一样遵守 robots、同一网站限速、YoujuBot 标识，
不绕过任何防护）。近 30 天发布的每天查一次（撤稿更正多在刚发出的几天，也是用户最常看、最常做
AI 解读的时候）；31–90 天的大约一周查一次；没查过的先查；上次像是撤下了的隔天再确认一次。

一次检查的结论（_classify）：
- 像是撤下了：404、410；跳到 404 页面；网站回 200，但页面写明"页面不存在""已撤稿或删除"这类话、
  页面里没有这份通知的标题（withdrawn_page）；
- 能打开：其余 200 的页面，以及 PDF 等文件；
- 判断不了：403、429、5xx 等其他错误码，超时，证书错误，robots 不让访问，没有可见文字的页面
  （防机器人脚本）：说明不了网页没了，状态不变，只记检查时间。
连续两次（相隔至少 20 小时）都像是撤下了，才记"原网页已失效"（link_gone_at）；中间一次能打开
就清空。小程序卡片标"原网页已撤下"、详情页写明哪天检查时打不开，正文照样可以看（我们保存的）；
不能再开始新的 AI 解读（interpret.eligible_body 的 link_gone）。
"""

import html
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import lxml.html
from lxml import etree
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from youju.config import get_settings
from youju.crawler.fetcher import FetchResult, PoliteFetcher
from youju.models import Document, Source
from youju.takedown import available_condition

log = logging.getLogger("youju.link_check")
BEIJING = ZoneInfo("Asia/Shanghai")
WINDOW = timedelta(days=90)  # 只查近 90 天发布的（申报和名单的时效）
RECENT = timedelta(days=30)  # 近 30 天发布的每天查（一半申报通知 30 天内截止，见设计说明）
RECENT_RECHECK = timedelta(hours=20)  # 每天 5:10 跑一次，留出前一天跑得晚的时间差
RECHECK_AFTER = timedelta(days=6)  # 31–90 天的大约一周查一次
CONFIRM_AFTER = timedelta(hours=20)  # 第一次像是撤下了，隔天再确认
GONE_STATUSES = (404, 410)
# 一次最多跑这么久：5:10 开始，6:30 第一轮抓取之前停下，免得两边同时访问同一网站；
# 没查完的算"该查还没轮到"，第二天接着查
BUDGET_SECONDS = 70 * 60

# 跳到网站自己的 404 页面（10/22 实测：内蒙古 /404/40x.html、海南 /404.html、财政部 /404.htm、
# 生态环境部 /404/index.shtml）
_ERROR_PAGE = re.compile(r"/(?:404|40x)(?:\.s?html?|/(?:40x\.html?|index\.s?html?)?)?$", re.I)
# 页面写明不存在或撤下了（10/22 实测的模板："您访问的页面已撤稿或删除""您所访问的页面不存在"
# "网页不存在或已删除""地址有错或页面不存在""信息不存在"）。只认这类说法，不认单独的"不存在"
# "删除"（通知正文里常有"不存在弄虚作假""予以删除"）
_GONE_TEXT = re.compile(
    r"撤稿"
    r"|(?:页面|网页|网址|文章|信息|稿件)(?:不存在|找不到|未找到|没有找到|无法找到)"
    r"|(?:页面|网页|文章|信息|稿件)已(?:经)?(?:被)?(?:删除|撤下|撤销|撤回|移除|下线)"
    r"|不存在或已(?:被)?删除"
)
# 撤稿页、"页面不存在"页可见文字的上限：见过最长的 1853 字（生态环境部，底部一长串部委链接）。
# 超过的当成正常页面，免得通知正文里碰巧有这些话
TEMPLATE_MAX = 3000
# 标题切成这么长的片段，一半以上在页面里找得到就算页面里有这份通知的标题
_TITLE_GRAM = 6
_NOT_WORD = re.compile(r"[\W_]+")
# lxml 不收带编码声明的字符串（XHTML 页面开头的 <?xml … encoding=…?>），先去掉
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


@dataclass
class LinkOutcome:
    # 只留结论，不留页面内容：一次查几千份，页面都留着要占几百 MB
    document_id: int
    verdict: str  # ok / 404 / 410 / withdrawn_page / unknown（_classify）


def _government_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "gov.cn" or host.endswith(".gov.cn")


def visible_text(page: str) -> str:
    """页面上看得见的字（去掉脚本、样式），空白合成一个空格；解析不了的给空串。"""
    page = _XML_DECL.sub("", page)
    if not page.strip():
        return ""
    try:
        root = lxml.html.document_fromstring(page)
    except (etree.ParserError, ValueError):
        return ""
    for node in root.xpath("//script|//style|//noscript|//template"):
        node.drop_tree()
    return " ".join(root.text_content().split())


def _squash(text: str) -> str:
    return _NOT_WORD.sub("", html.unescape(text)).lower()


def title_on_page(title: str, page: str) -> bool:
    """页面里有没有这份通知的标题：标题和整页源码（含 <title>、<meta>）都去掉空格标点，标题按 6 字
    切片，一半以上找得到就算有。我们存的标题有时和页面上的不一样（财政部存的是新闻标题，前面带
    "·""》"的），所以不要求一字不差。"""
    words = _squash(title)
    if not words:
        return False
    source = _squash(page)
    if len(words) <= _TITLE_GRAM:
        return words in source
    grams = {words[i : i + _TITLE_GRAM] for i in range(len(words) - _TITLE_GRAM + 1)}
    return sum(gram in source for gram in grams) * 2 >= len(grams)


def withdrawn_page(result: FetchResult, url: str, title: str, text: str | None = None) -> bool:
    """网站回 200，但这是"页面不存在""已撤稿"页：跳到了 404 页面；或者页面写明不存在、撤下了，
    页面里没有这份通知的标题，可见文字不长。text 是算好的可见文字（不传就现算）。"""
    final = result.final_url or url
    if final != url and _ERROR_PAGE.search(urlsplit(final).path):
        return True
    text = visible_text(result.text) if text is None else text
    if not text or len(text) > TEMPLATE_MAX or not _GONE_TEXT.search(text):
        return False
    return not title_on_page(title, result.text)


def _html(result: FetchResult) -> bool:
    if result.content.startswith(b"%PDF-"):
        return False
    content_type = (result.content_type or "").lower()
    return "html" in content_type or (not content_type and result.text.lstrip()[:1] == "<")


def _classify(result: FetchResult, url: str, title: str) -> str:
    """一次检查的结论：ok、404、410、withdrawn_page（像是撤下了的三种），unknown（判断不了）。"""
    if result.status == "ok":
        if not _html(result):
            # PDF、Word 等文件：能下载就是还在；跳到 404 页面的照样算撤下了
            return "withdrawn_page" if withdrawn_page(result, url, title, "") else "ok"
        text = visible_text(result.text)
        if withdrawn_page(result, url, title, text):
            return "withdrawn_page"
        # 没有可见文字：多半是防机器人脚本（人社部的正常页面也是这样），判断不了
        return "ok" if text else "unknown"
    if result.status == "http_error" and result.http_status in GONE_STATUSES:
        return str(result.http_status)
    return "unknown"


def _select(session: Session, now: datetime, limit: int) -> tuple[list[tuple[int, str, str]], int]:
    """这次要查的（编号、网址、标题），以及该查而这次轮不到的份数。"""
    today = now.astimezone(BEIJING).date()
    base = (
        select(Document.id, Document.url, Document.title)
        .join(Source, Source.id == Document.source_id)
        .where(
            Source.rights_level == 1,
            available_condition(),
            Document.publish_date >= today - WINDOW,
        )
    )
    recent = Document.publish_date >= today - RECENT
    # 待确认的另算；已确认失效的照常查（能打开就恢复）
    settled = or_(Document.link_suspect_at.is_(None), Document.link_gone_at.is_not(None))
    first = (Document.link_checked_at.is_not(None), Document.link_checked_at, Document.id)
    # 待确认的（上次像是撤下了、隔了 20 小时）最先；再是近 30 天该查的；再是 31–90 天该查的。
    # 后两种都是没查过的先查，再按上次检查从早到晚
    queues = [
        base.where(
            Document.link_suspect_at.is_not(None),
            Document.link_gone_at.is_(None),
            Document.link_checked_at <= now - CONFIRM_AFTER,
        ).order_by(Document.link_checked_at, Document.id),
        base.where(
            recent,
            settled,
            or_(
                Document.link_checked_at.is_(None),
                Document.link_checked_at <= now - RECENT_RECHECK,
            ),
        ).order_by(*first),
        base.where(
            ~recent,
            settled,
            or_(
                Document.link_checked_at.is_(None),
                Document.link_checked_at <= now - RECHECK_AFTER,
            ),
        ).order_by(*first),
    ]
    picked: list[tuple[int, str, str]] = []
    due = 0
    for stmt in queues:
        due += session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        if len(picked) < limit:
            picked += [tuple(row) for row in session.execute(stmt.limit(limit - len(picked)))]
    return picked, due - len(picked)


def _apply(doc: Document, verdict: str, now: datetime) -> str:
    doc.link_checked_at = now
    if verdict == "ok":
        doc.link_suspect_at = None
        doc.link_gone_at = None
        return "ok"
    if verdict == "unknown":
        return "unknown"
    if doc.link_suspect_at is None:
        doc.link_suspect_at = now
        return "suspect"
    if doc.link_gone_at is None and now - _utc(doc.link_suspect_at) >= CONFIRM_AFTER:
        doc.link_gone_at = now
        return "gone"
    return "gone" if doc.link_gone_at is not None else "suspect"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def check_links(
    session: Session,
    *,
    limit: int = 3000,
    fetcher: PoliteFetcher | None = None,
    now: datetime | None = None,
    max_workers: int = 4,
    budget: float | None = BUDGET_SECONDS,
    clock=time.monotonic,
) -> dict[str, int]:
    """counts：checked 查了几份，ok / suspect / gone / unknown 各几份，withdrawn_page 其中几次是
    网站回 200 的撤稿页、404 页，left 该查而这次没轮到的份数（超过上限或到了 budget 秒没查完的；
    天天不是 0 就该调大 --limit）。"""
    now = _utc(now or datetime.now(UTC))
    stop_at = clock() + budget if budget is not None else None
    selected, left = _select(session, now, limit)
    # 查询事务先结束；网络请求期间不占用数据库连接
    session.commit()
    counts = {
        "checked": 0,
        "ok": 0,
        "suspect": 0,
        "gone": 0,
        "unknown": 0,
        "withdrawn_page": 0,
        "left": left + len(selected),  # 查一份减一份
    }
    if not selected:
        return counts
    by_host: dict[str, list[tuple[int, str, str]]] = {}
    for item in selected:
        by_host.setdefault((urlsplit(item[1]).hostname or "").lower(), []).append(item)
    gone_hosts: Counter[str] = Counter()

    def record(host: str, outcomes: list[LinkOutcome]) -> None:
        # 每查完一个网站写一次：中途被打断（部署重启、超时）也不丢已经查过的
        for outcome in outcomes:
            counts["left"] -= 1
            doc = session.get(Document, outcome.document_id)
            if doc is None:
                continue
            counts[_apply(doc, outcome.verdict, now)] += 1
            counts["checked"] += 1
            if outcome.verdict == "withdrawn_page":
                counts["withdrawn_page"] += 1
            if outcome.verdict not in ("ok", "unknown"):
                gone_hosts[host] += 1
        session.commit()

    def run(polite: PoliteFetcher) -> None:
        def run_host(items: list[tuple[int, str, str]]) -> list[LinkOutcome]:
            outcomes = []
            for doc_id, url, title in items:
                if stop_at is not None and clock() >= stop_at:
                    break  # 到时间了：这个网站剩下的留到明天
                outcomes.append(LinkOutcome(doc_id, _classify(polite.fetch(url), url, title)))
            return outcomes

        # 网络并行只跨网站（同一网站按限速排队）；数据库写入在主线程
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {pool.submit(run_host, items): host for host, items in by_host.items()}
            for future in as_completed(futures):
                record(futures[future], future.result())

    if fetcher is not None:
        run(fetcher)
    else:
        settings = get_settings()
        with PoliteFetcher(
            settings.user_agent,
            settings.request_timeout,
            settings.per_host_interval,
            url_allowed=_government_url,
        ) as polite:
            run(polite)
    # 一个网站一下子"撤下"好几份，可能是改版或者整站出错：写进日志，方便回头看
    busy = [f"{host} {n} 份" for host, n in gone_hosts.most_common(3) if n >= 3]
    log.info(
        "原网页检查：%s%s", counts, f"；像是撤下了最多的网站：{'、'.join(busy)}" if busy else ""
    )
    return counts
