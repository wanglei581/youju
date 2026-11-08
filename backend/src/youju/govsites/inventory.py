"""逐级走官方导航，生成全国"门户—部门—栏目"清单。

起点是中国政府网的"地方政府网站"导航页（31 个省级门户），然后：
1. 在省级门户的首页和导航页（"市县政府网站""省直部门网站""网站导航"等）里，按国家地名信息库的
   名称认出各地市、区县的门户网址，按名称认出省直目标部门；
2. 地市门户同样找区县门户和市直部门；区县门户找县直部门（集约化以后县直部门多半没有独立网站，
   是门户里的部门频道或信息公开目录）；
3. 每个目标部门的页面上按文字找"通知公告""政策文件/规范性文件""公示公告"栏目；首页没有就沿
   "信息公开"再找一层；
4. 记下每一步是在哪个官方页面上找到的（可追溯），以及页面的平台特征。

每一级只信官方导航：导航里找不到的记"导航未找到"，不靠猜域名补。
"""

import csv
import json
import logging
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree

from youju.crawler.adapters import (
    api_html,
    find_unit_apis,
    jpaas_tree_categories,
    jpaas_xxgk_groups,
)
from youju.crawler.fetcher import FetchResult, resolve_host
from youju.crawler.urls import page_dir
from youju.govsites.divisions import (
    Division,
    DivisionIndex,
    NameMatcher,
    clean_place_text,
    name_variants,
)
from youju.govsites.links import (
    REPORT_GROUP,
    REPORT_WORDS,
    Link,
    footer_scripts,
    is_gov_url,
    nav_links,
    page_links,
    parse_html,
    registrable,
    script_links,
)
from youju.govsites.names import COLUMN_KINDS, column_kind, dept_categories
from youju.govsites.pagecache import PageCache
from youju.govsites.platform import URL_SHAPES, Fingerprint, blocker, fingerprint

log = logging.getLogger(__name__)

GOV_LOCAL_NAV = "https://www.gov.cn/home/2023-03/29/content_5748954.htm"
GOV_DEPT_NAV = "https://www.gov.cn/home/2023-03/29/content_5748953.htm"
GOV_HOME = "https://www.gov.cn/"
# 国家级按一个"行政区"处理：门户是中国政府网，部门从"国务院部门网站"导航里找
NATIONAL = Division("000000", "国务院", "国务院", "国家级", "国家", None, None, None)
# 各级都要找的部门（数据局、开发区不是每地都有，单列为"有就收"）
STANDARD_DEPTS = (
    "政府办",
    "发改委",
    "工信",
    "科技",
    "商务",
    "财政",
    "人社",
    "市场监管",
    "农业农村",
    "文旅",
    "生态环境",
)
OPTIONAL_DEPTS = ("数据局", "开发区")
# 部门页面上找不到栏目时，沿这些链接再找一层
_INFO_WORDS = re.compile(
    r"^(?:政府)?信息公开(?:目录|专栏|平台)?$|^法定主动公开内容$|^主动公开$|^政务公开$"
)
_MORE = re.compile(r"^(?:更多|more|MORE|More|查看更多|更多>+|>>|»|\+|＋|更多\+)$")
_HEADING_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "span",
    "div",
    "strong",
    "b",
    "p",
    "li",
    "em",
    "dt",
    "td",
    "th",
    "font",
}


@dataclass
class Found:
    url: str
    text: str
    source: str  # 在哪个官方页面上找到的


@dataclass
class PortalRow:
    code: str
    level: str
    province: str
    city: str
    district: str
    name: str
    portal_url: str = ""
    nav_source: str = ""
    nav_text: str = ""
    status: str = "导航未找到"  # 已找到 / 导航未找到 / 打不开 / 不能抓
    reason: str = ""
    site_id_code: str = ""
    site_name: str = ""
    platform: str = ""
    support: str = ""


@dataclass
class ColumnHit:
    kind: str
    name: str
    url: str
    source: str  # 在哪个页面上找到的


@dataclass
class DeptRow:
    code: str
    level: str
    province: str
    city: str
    district: str
    dept: str
    dept_full_name: str = ""
    site_url: str = ""
    site_type: str = ""  # 独立网站 / 门户频道 / 门户本身
    nav_source: str = ""
    nav_text: str = ""
    status: str = "导航未找到"  # 已找到 / 导航未找到 / 打不开 / 不能抓
    reason: str = ""
    site_id_code: str = ""
    platform: str = ""
    support: str = ""
    columns: list[ColumnHit] = field(default_factory=list)


@dataclass
class PageInfo:
    url: str  # 请求的网址
    final_url: str
    result: FetchResult
    doc: etree._Element | None = None
    links: list[Link] = field(default_factory=list)
    fp: Fingerprint | None = None
    block: str | None = None
    nav_text: str = ""  # 从门户首页哪个导航链接进来的（首页本身为空）
    # 大汉 jpaas 信息公开分类树上的分类（名称, 键）：栏目在，但列表要按分类由脚本加载
    categories: list[tuple[str, str]] = field(default_factory=list)
    level_hint: str = ""  # 门户首页：所属行政区的层级


class CachingResolver:
    """域名解析结果缓存一小时：几百个线程同时抓，系统解析的并发名额（32 个）不至于被挤满。"""

    def __init__(self, ttl: float = 3600) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def __call__(self, host: str) -> list[str]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(host)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        last: OSError | None = None
        for attempt in range(3):
            try:
                addrs = resolve_host(host)
                with self._lock:
                    self._cache[host] = (now, addrs)
                return addrs
            except OSError as exc:
                # 并发名额满、解析慢，或系统解析器在大量并发查询时偶尔回"找不到"：稍等再试，
                # 三次都失败才算解析失败
                last = exc
                time.sleep(1 + attempt)
        raise last or OSError("域名解析失败")


def _site_like(url: str, current: str) -> bool:
    """像不像一个网站（或频道）的入口，而不是一篇文章：不是文章网址形状，路径不太深。"""
    parts = urlsplit(url)
    path = parts.path or "/"
    for _, pattern in URL_SHAPES:
        if pattern.search(path + ("?" + parts.query if parts.query else "")):
            return False
    if path.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".png", ".zip")):
        return False
    depth = len([seg for seg in path.split("/") if seg])
    return depth <= 4


def _portal_like(url: str, current: str) -> bool:
    """下级政府门户：和当前门户不是同一个主机，而且是网站首页（路径很浅）。"""
    if not _site_like(url, current):
        return False
    parts = urlsplit(url)
    if parts.netloc.lower() == urlsplit(current).netloc.lower():
        return False
    depth = len([seg for seg in (parts.path or "/").split("/") if seg])
    return depth <= 2


class Inventory:
    def __init__(
        self,
        index: DivisionIndex,
        cache: PageCache,
        provinces: Iterable[str] | None = None,
        max_level: str = "区县级",
        workers: int = 96,
        per_province: int = 6,
        progress: Callable[[str], None] | None = None,
        inner: int = 4,
    ) -> None:
        self.index = index
        self.cache = cache
        self.only = set(provinces or [])
        self.max_level = max_level
        self.workers = workers
        self.per_province = per_province
        self.inner = inner
        self.progress = progress or (lambda msg: None)
        self.portals: dict[str, PortalRow] = {}
        self.depts: list[DeptRow] = []
        self._lock = threading.Lock()
        self._queued: set[str] = set()
        self.done = 0
        # 国务院部门的网站（主机后缀）：省市县门户页脚常有"国务院部门网站"导航，不能当成本地部门
        self.ministry_hosts: set[str] = set()

    # —— 页面 ——

    def page(self, url: str, hops: int = 2) -> PageInfo:
        result = self.cache.get(url)
        target = _script_redirect(result, url) if hops > 0 else None
        if target:
            # 只有一句 location.href 的跳转页（浙江县级部门频道常见）：跟过去，记跳转后的页面
            nxt = self.page(target, hops - 1)
            nxt.url = url
            return nxt
        info = PageInfo(url, result.final_url or url, result)
        if result.status != "ok":
            info.block = blocker(result.status, result.http_status, result.text)
            return info
        info.block = blocker(result.status, result.http_status, result.text)
        info.doc = parse_html(result.text)
        info.links = page_links(result.text, info.final_url, info.doc)
        info.fp = fingerprint(
            result.text, info.final_url, info.doc, [link.url for link in info.links]
        )
        if "unitbuild.js" in result.text:
            # 大汉 jpaas：导航、栏目列表都由页面声明的单元接口拼出来，按页面给的参数取（最多 4 个）
            for api in find_unit_apis(result.text, info.final_url)[:4]:
                r = self.cache.get(api.url)
                html = api_html(api, r.text) if r.status == "ok" else ""
                if html:
                    info.links += page_links(html, info.final_url)
                    info.categories += jpaas_tree_categories(html)
                    # 信息公开分组列表：分组名 + 由条目网址还原的分组栏目页，当作栏目链接
                    info.links += [
                        Link(name, url, "jpaas分组")
                        for name, url in jpaas_xxgk_groups(html, info.final_url)
                    ]
        return info

    def _nav_pages(self, home: PageInfo, limit: int = 10) -> list[PageInfo]:
        """门户首页 + 导航页（"市县政府网站""部门网站""政府网站年度报表"……）+ 拼页脚的脚本。

        年度报表目录再往下走一层分组页（"省级部门""设区市""县（市、区）"）：国办要求每个政府网站
        每年发布年度报表，门户的年报目录就是本地政府网站的官方名录。
        """
        pages = [home]
        seen = {home.url, home.final_url}

        def visit(link: Link, label: str) -> PageInfo | None:
            if link.url in seen:
                return None
            seen.add(link.url)
            p = self.page(link.url)
            if p.result.status != "ok" or p.block:
                return None
            p.nav_text = label
            pages.append(p)
            return p

        for link in nav_links(home.links, home.final_url, limit=limit):
            p = visit(link, link.text)
            if p is not None and REPORT_WORDS.search(link.text):
                groups = [
                    g
                    for g in p.links
                    if REPORT_GROUP.search(g.text)
                    and len(g.text) <= 12
                    and registrable(g.url) == registrable(home.final_url)
                ]
                for g in groups[:6]:
                    visit(g, f"{link.text}/{g.text}")
        if home.doc is not None:
            # 省、市门户的"市县网站""部门网站"常由脚本拼出来：多看几个本站脚本
            extra = 6 if home.level_hint in ("省级", "市级", "国家级") else 0
            # 安徽等省每个地市一个导航脚本：省级门户多看一些
            named = 24 if home.level_hint == "省级" else 8
            for src in footer_scripts(home.doc, home.final_url, extra=extra, named_limit=named):
                r = self.cache.get(src)
                if r.status == "ok":
                    links = script_links(r.text, home.final_url)
                    if links:
                        pages.append(PageInfo(src, src, r, None, links, nav_text="页脚脚本"))
        return pages

    # —— 走一遍 ——

    def run(self) -> None:
        start = self.page(GOV_LOCAL_NAV)
        if start.result.status != "ok":
            raise RuntimeError(f"中国政府网地方政府网站导航打不开：{start.result.error}")
        ministries = self.page(GOV_DEPT_NAV)
        for link in ministries.links:
            host = (urlsplit(link.url).hostname or "").lower()
            if is_gov_url(link.url) and host not in ("www.gov.cn", "gov.cn"):
                self.ministry_hosts.add(host.removeprefix("www."))
        provinces = [p for p in self.index.provinces() if not self.only or p.province in self.only]
        matcher = NameMatcher(provinces)
        found: dict[str, Found] = {}
        for link in start.links:
            d = matcher.match(link.text)
            if d and d.code not in found and is_gov_url(link.url):
                found[d.code] = Found(link.url, link.text, GOV_LOCAL_NAV)
        tasks: deque[tuple[Division, Found | None]] = deque()
        if not self.only or "国家" in self.only:
            tasks.append((NATIONAL, Found(GOV_HOME, "中国政府网", GOV_LOCAL_NAV)))
        for p in provinces:
            tasks.append((p, found.get(p.code)))
            self._queued.add(p.code)
        self._schedule(tasks)

    def _schedule(self, initial: deque[tuple[Division, Found | None]]) -> None:
        pending: dict[str, deque[tuple[Division, Found | None]]] = {}
        running: Counter[str] = Counter()
        futures: dict[Future, str] = {}

        def push(div: Division, f: Found | None) -> None:
            pending.setdefault(div.province, deque()).append((div, f))

        for div, f in initial:
            push(div, f)
        with ThreadPoolExecutor(self.workers, thread_name_prefix="coverage") as ex:
            while True:
                # 每个省同时最多 per_province 个任务：同一省的集约化平台常在同一组服务器上
                for prov, queue in pending.items():
                    while (
                        queue and running[prov] < self.per_province and len(futures) < self.workers
                    ):
                        div, f = queue.popleft()
                        running[prov] += 1
                        futures[ex.submit(self._process, div, f)] = prov
                if not futures:
                    break
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for fut in done:
                    prov = futures.pop(fut)
                    running[prov] -= 1
                    try:
                        children = fut.result()
                    except Exception:  # 单个地方出错不影响别处
                        log.exception("处理 %s 的一个行政区时出错", prov)
                        continue
                    for div, f in children:
                        push(div, f)

    def _process(self, div: Division, found: Found | None) -> list[tuple[Division, Found | None]]:
        """处理一个行政区；出了意外也记一行"出错"、照常排下级，整棵子树不会从结果里消失。"""
        try:
            return self._process_inner(div, found)
        except Exception as exc:  # noqa: BLE001 - 单个行政区的意外不影响别处
            log.exception("处理 %s %s 时出错", div.province, div.name)
            row = PortalRow(
                div.code, div.level, div.province, div.city or "", div.district or "", div.name
            )
            row.status, row.reason = "出错", f"{exc.__class__.__name__}: {str(exc)[:100]}"
            if found is not None:
                row.portal_url, row.nav_source, row.nav_text = found.url, found.source, found.text
            with self._lock:
                already = row.code in self.portals
            if not already:
                self._finish(row, [])
            return self._enqueue(div, self._expected_children(div), {})

    def _process_inner(
        self, div: Division, found: Found | None
    ) -> list[tuple[Division, Found | None]]:
        row = PortalRow(
            div.code, div.level, div.province, div.city or "", div.district or "", div.name
        )
        children = self._expected_children(div)
        if found is None:
            self._finish(row, [])
            return self._enqueue(div, children, {})
        row.portal_url, row.nav_source, row.nav_text = found.url, found.source, found.text
        home = self.page(found.url)
        if home.result.status != "ok" or home.block:
            row.status = "不能抓" if home.block else "打不开"
            row.reason = home.block or home.result.error or home.result.status
            self._finish(row, [])
            return self._enqueue(div, children, {})
        row.portal_url = home.final_url
        row.status = "已找到"
        fp = home.fp or Fingerprint()
        row.site_id_code, row.site_name = fp.site_id_code, fp.site_name
        row.platform, row.support = fp.platform, fp.support
        code6 = fp.site_id_code[:6]
        title = (home.doc.findtext(".//title") or "") if home.doc is not None else ""
        named = any(v in fp.site_name + title for v in name_variants(div.name, div.kind))
        if div.level != "国家级" and code6 != div.code and not named:
            # 导航链接可能指到了别的网站（如政务服务网）：网站名、标题、标识码都对不上的，待核对
            row.reason = "网站名称、标题和标识码都对不上这个行政区，待核对"
        elif code6.isdigit() and div.level != "国家级" and code6 != div.code:
            # 标识码前 6 位是行政区划代码：对不上的可能是导航链到了别的网站（如政务服务网），待核对
            same_prov = code6[:2] == div.code[:2]
            row.reason = f"网站标识码 {fp.site_id_code} 和区划代码 {div.code} 不一致" + (
                "（同省，待核对）" if same_prov else "（不是同一省，待核对）"
            )
        home.level_hint = div.level
        pages = self._nav_pages(home)
        if div.level == "国家级":
            dept_nav = self.page(GOV_DEPT_NAV)
            if dept_nav.result.status == "ok":
                dept_nav.nav_text = "国务院部门网站"
                pages.append(dept_nav)
        child_found = self._find_children(div, children, pages)
        try:
            depts = self._find_depts(div, home, pages)
        except Exception as exc:  # noqa: BLE001 - 部门出意外不影响下级照常排队
            log.exception("找 %s %s 的部门时出错", div.province, div.name)
            row.reason = f"找部门时出错：{exc.__class__.__name__}"
            depts = []
        self._finish(row, depts)
        return self._enqueue(div, children, child_found)

    def _finish(self, row: PortalRow, depts: list[DeptRow]) -> None:
        with self._lock:
            self.portals[row.code] = row
            self.depts.extend(depts)
            self.done += 1
            n = self.done
        self.progress(
            f"[{n}] {row.province} {row.name}：{row.status} {row.portal_url} 部门 {len(depts)}"
        )

    def _expected_children(self, div: Division) -> list[Division]:
        if div.level in ("区县级", "国家级") or (self.max_level == "省级"):
            return []
        kids = self.index.children(div.code)
        if div.level == "省级":
            # 省级门户上常常直接列出全省区县：一并认
            grand = [g for k in kids if k.level == "市级" for g in self.index.children(k.code)]
            kids = kids + grand
        if self.max_level == "市级":
            kids = [k for k in kids if k.level != "区县级"]
        return kids

    def _enqueue(
        self, div: Division, children: list[Division], found: dict[str, Found]
    ) -> list[tuple[Division, Found | None]]:
        """排下一级的任务。导航里找到的直接排；没找到的只由直接上级排（记"导航未找到"）：
        省级门户上没列出的区县，要等它的地市门户再找一次。"""
        out: list[tuple[Division, Found | None]] = []
        with self._lock:
            for child in children:
                if child.code in self._queued:
                    continue
                f = found.get(child.code)
                if f is None and child.parent_code != div.code:
                    continue
                self._queued.add(child.code)
                out.append((child, f))
        return out

    def _find_children(
        self, div: Division, children: list[Division], pages: list[PageInfo]
    ) -> dict[str, Found]:
        if not children:
            return {}
        prov = self.index.province_of(div)
        prefixes = [prov.name, prov.province]
        if div.level == "市级":
            prefixes += sorted(name_variants(div.name, div.kind), key=len, reverse=True)
        matcher = NameMatcher(children, prefixes)
        found: dict[str, Found] = {}
        for p in pages:
            for link in p.links:
                d = matcher.match(link.text)
                if d is None or d.code in found or not is_gov_url(link.url):
                    continue
                url = link.url
                source = p.final_url
                if not _portal_like(url, p.final_url):
                    parts = urlsplit(url)
                    if parts.netloc.lower() == urlsplit(p.final_url).netloc.lower():
                        if not REPORT_WORDS.search(p.nav_text):
                            continue
                        # 年度报表目录里本站的"某市"页：进去找这个市政府网站的年报，读"首页网址"
                        resolved = self._report_portal(d, url)
                        if not resolved:
                            continue
                        url, source = resolved
                    else:
                        # 指向下级网站内页的链接（年报目录等）：取那个网站的首页，记下是取的首页
                        url = f"{parts.scheme}://{parts.netloc}/"
                        found[d.code] = Found(
                            url, f"{link.text}（链到 {link.url}，取网站首页）", source
                        )
                        continue
                found[d.code] = Found(url, link.text, source)
        return found

    def _report_portal(self, d: Division, url: str) -> tuple[str, str] | None:
        """年报目录里某个行政区的分页 → 它的政府门户网站年报 → 首页网址。

        返回（门户网址, 年报页网址）。"""
        idx = self.page(url)
        if idx.result.status != "ok":
            return None
        site = report_homepage(idx)
        if site:
            return site, idx.final_url
        names = sorted(name_variants(d.name, d.kind), key=len, reverse=True)
        for link in idx.links:
            if not any(n in link.text for n in names):
                continue
            if not re.search(r"人民政府|政府网|门户", link.text) or dept_categories(link.text):
                continue
            rp = self.page(link.url)
            site = report_homepage(rp)
            if not site and rp.result.status == "ok":
                pdf = report_pdf(rp)
                site = report_homepage_text(self.cache.get(pdf).text) if pdf else None
            if site:
                return site, rp.final_url
        return None

    # —— 部门 ——

    def _other_places(self, div: Division) -> list[str]:
        """本省其他行政区（含上级）的名称写法：部门链接文字以它们开头的，是别处的部门。"""
        if div.level == "国家级":
            names = {v for p in self.index.provinces() for v in name_variants(p.name, p.kind)}
            return sorted(names, key=len, reverse=True)
        prov = self.index.province_of(div)
        own = name_variants(div.name, div.kind) | {div.province}
        out: set[str] = set()
        if div.level != "省级":
            out |= {prov.name, prov.province}
        for d in self.index.descendants(prov.code):
            if d.code != div.code:
                out |= {v for v in name_variants(d.name, d.kind) if len(v) >= 2}
        return sorted(out - own, key=len, reverse=True)

    def _find_depts(self, div: Division, home: PageInfo, pages: list[PageInfo]) -> list[DeptRow]:
        others = self._other_places(div)
        own = sorted(name_variants(div.name, div.kind) | {div.province}, key=len, reverse=True)
        own_chars = level_chars(div)
        candidates: dict[str, list[tuple[int, Found]]] = {}
        for p in pages:
            report = bool(REPORT_WORDS.search(p.nav_text))
            nav_bonus = (
                2 if report or re.search(r"部门|省直|市直|区直|县直|机构|单位", p.nav_text) else 0
            )
            for link in p.links:
                text = link.text
                if any(text.startswith(o) for o in others) and not _own_branch(text, div):
                    continue
                cats = dept_categories(text)
                if not cats or not is_gov_url(link.url):
                    continue
                if div.level != "国家级" and self._is_ministry(text, link.url):
                    continue  # 页脚"国务院部门网站"里的部委，不是本地部门
                if _service_hall(link.url) and not (
                    "数据局" in cats and not _SERVICE_PATH.search(urlsplit(link.url).path)
                ):
                    continue  # 政务服务网的部门办事页（"部门服务"），不是部门信息发布网站
                    # （数据局的网站有的就叫 zwfw.xx.gov.cn，链接是数据局、路径不是办事页的放行）
                if not _site_like(link.url, p.final_url) and not report:
                    continue
                prefix = level_prefix(text)
                if prefix and prefix not in own_chars:
                    continue  # "省科技厅"出现在市县门户上：是上级部门
                score = nav_bonus
                if any(text.startswith(o) for o in own) or (prefix and prefix in own_chars):
                    score += 3
                if urlsplit(link.url).netloc.lower() != urlsplit(home.final_url).netloc.lower():
                    score += 1
                for cat in cats:
                    if cat == "政府办":
                        continue  # 政府办看门户本身
                    candidates.setdefault(cat, []).append(
                        (score, Found(link.url, text, p.final_url))
                    )
        rows: list[DeptRow] = []
        # 政府办公厅：门户本身的政府文件、通知公告
        gov = self._dept_row(div, "政府办")
        gov.dept_full_name = (
            home.fp.site_name if home.fp and home.fp.site_name else f"{div.name}人民政府"
        )
        gov.site_url, gov.site_type, gov.nav_source = home.final_url, "门户本身", home.url
        gov.status = "已找到"
        self._fill_from_page(gov, home, pages)
        rows.append(gov)
        cats = STANDARD_DEPTS[1:] + OPTIONAL_DEPTS
        # 各部门网站多在不同主机上，并行抓（同一主机仍由抓取器排队、间隔 3 秒）
        with ThreadPoolExecutor(self.inner, thread_name_prefix="coverage-dept") as ex:
            portal_cols = frozenset(c.url for c in gov.columns)
            picked = list(
                ex.map(lambda c: self._pick(div, home, c, candidates.get(c, []), portal_cols), cats)
            )
        for rows_for_cat in picked:
            rows.extend(rows_for_cat)
        before = {id(r): {c.kind for c in r.columns} for r in rows}
        shared = _drop_shared_columns(rows)
        if shared:
            # 丢了共用栏目的部门：把共用栏目排除后重找一遍缺的类型（会沿"信息公开"再进一层），
            # 补回来的再查一次是不是共用的
            for r in rows:
                if r.dept == "政府办" or r.status != "已找到" or not r.site_url:
                    continue
                lost = before[id(r)] - {c.kind for c in r.columns}
                if not lost:
                    continue
                kept = list(r.columns)
                page = self.page(r.site_url)
                self._fill_from_page(r, page, [page], portal_cols | shared)
                have = {c.kind for c in kept}
                r.columns = kept + [c for c in r.columns if c.kind in lost and c.kind not in have]
                if r.columns and r.reason == SHARED_ONLY:
                    r.reason = ""
            _drop_shared_columns(rows)
        return rows

    def _pick(
        self,
        div: Division,
        home: PageInfo,
        cat: str,
        options: list[tuple[int, Found]],
        portal_cols: frozenset[str] = frozenset(),
    ) -> list[DeptRow]:
        if not options:
            return [self._dept_row(div, cat)] if cat in STANDARD_DEPTS else []
        rows: list[DeptRow] = []
        seen: set[str] = set()
        for _, f in sorted(options, key=lambda o: -o[0]):
            if f.url in seen:
                continue
            seen.add(f.url)
            if not _site_like(f.url, f.source):
                # 年度报表里的一页：表里"首页网址"才是部门网站
                rp = self.page(f.url)
                site = report_homepage(rp)
                pdf = report_pdf(rp) if rp.result.status == "ok" and not site else None
                if pdf:
                    site = report_homepage_text(self.cache.get(pdf).text)
                if not site or site in seen:
                    continue
                f = Found(site, f.text, f.url)
                seen.add(site)
            row = self._dept_row(div, cat)
            row.dept_full_name, row.site_url = f.text, f.url
            row.nav_source, row.nav_text = f.source, f.text
            same_host = urlsplit(f.url).netloc.lower() == urlsplit(home.final_url).netloc.lower()
            row.site_type = "门户频道" if same_host else "独立网站"
            dp = self.page(f.url)
            # 门户自己的栏目不算部门的；几个部门都链到的同一个栏目（如省政策文件库）由
            # _drop_shared_columns 去掉。部门挂在门户主机上的信息公开栏目照常认
            exclude = portal_cols
            if dp.result.status != "ok" or dp.block:
                row.status = "不能抓" if dp.block else "打不开"
                row.reason = dp.block or dp.result.error or dp.result.status
            else:
                row.status = "已找到"
                row.site_url = dp.final_url
                self._fill_from_page(row, dp, [dp], exclude)
            rows.append(row)
            # 开发区可能有好几个（国家级、省级各一），其他部门只取最像的一个
            if cat != "开发区" or len(rows) >= 6:
                break
        return rows or ([self._dept_row(div, cat)] if cat in STANDARD_DEPTS else [])

    def _is_ministry(self, text: str, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if any(host == m or host.endswith("." + m) for m in self.ministry_hosts):
            return True
        # "国家级××经济技术开发区管委会""××国家高新区"不是部委，名称规则不管开发区
        if "开发区" in dept_categories(text):
            return False
        return bool(_MINISTRY_NAME.search(text))

    def _dept_row(self, div: Division, dept: str) -> DeptRow:
        return DeptRow(div.code, div.level, div.province, div.city or "", div.district or "", dept)

    def _fill_from_page(
        self,
        row: DeptRow,
        page: PageInfo,
        pages: list[PageInfo],
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        fp = page.fp or Fingerprint()
        row.site_id_code, row.platform, row.support = fp.site_id_code, fp.platform, fp.support
        # 部门网站自己写的网站名（meta SiteName）是同一类部门的全称，就用它
        if row.dept != "政府办" and fp.site_name and row.dept in dept_categories(fp.site_name):
            row.dept_full_name = fp.site_name
        hits = find_columns(pages, exclude)
        missing = [k for k in COLUMN_KINDS if k not in hits]
        if missing and row.dept != "政府办":
            # 部门首页没有：沿"信息公开"再找一层
            for link in page.links:
                if _INFO_WORDS.search(link.text) and registrable(link.url) == registrable(
                    page.final_url
                ):
                    sub = self.page(link.url)
                    if sub.result.status == "ok" and not sub.block:
                        for kind, hit in find_columns([sub], exclude).items():
                            hits.setdefault(kind, hit)
                    break
        row.columns = [hits[k] for k in COLUMN_KINDS if k in hits]


_REPORT_HOME = re.compile(r"首页网址\s*[:：]?\s*((?:https?://)?[A-Za-z0-9.-]+\.gov\.cn[^\s<>\"']*)")


_PDF_LINK = re.compile(r"""pdfurl=([^"'&\s]+\.pdf)|href=["']([^"']+\.pdf)["']""", re.I)


def report_homepage(page: PageInfo) -> str | None:
    """政府网站年度报表里的"首页网址"。"""
    if page.result.status != "ok":
        return None
    return report_homepage_text(page.result.text)


def report_pdf(page: PageInfo) -> str | None:
    """年度报表页面只嵌了一个 PDF 时，PDF 的网址。"""
    from youju.crawler.urls import normalize_url

    m = _PDF_LINK.search(page.result.text or "")
    return normalize_url(page.final_url, m.group(1) or m.group(2)) if m else None


def report_homepage_text(html: str) -> str | None:
    text = re.sub(r"<[^>]+>", " ", html or "")
    m = _REPORT_HOME.search(re.sub(r"\s+", " ", text))
    if not m:
        return None
    url = m.group(1).rstrip("/.,，。;；")
    if not url.startswith("http"):
        url = "http://" + url
    return url + ("/" if urlsplit(url).path in ("", "/") and not url.endswith("/") else "")


def _own_branch(text: str, div: Division) -> bool:
    """区县里上级部门的派出机构算本区县的部门：如"杭州市生态环境局西湖分局"
    （生态环境在区县一级多是市局的分局）。"""
    if div.level != "区县级" or not re.search(r"(分局|监管局|管理所)$", text):
        return False
    shorts = [v for v in name_variants(div.name, div.kind) if len(v) >= 2]
    return any(v in text[2:] for v in shorts)


# 整段脚本只有一句跳转（不认 if 分支里的、拼接出来的地址）：location.href="…"、location.replace("…")
_JS_REDIRECT_STMT = re.compile(
    r"""^(?:window\.|self\.|top\.)?location(?:\.href)?\s*=\s*(["'])([^"'+]+)\1\s*;?\s*(?://[^\n]*)?$"""
    r"""|^(?:window\.|self\.|top\.)?location\.replace\(\s*(["'])([^"'+]+)\3\s*\)\s*;?\s*(?://[^\n]*)?$""",
    re.I,
)
_META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+url=([^"'>\s]+)""", re.I)
_SCRIPT_BODY = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def _script_redirect(result: FetchResult, url: str) -> str | None:
    """只有一句无条件跳转、没有正文的页面：跳到哪里（同一家网站内才跟；防护页不跟）。"""
    from youju.crawler.urls import normalize_url

    if result.status != "ok" or not result.text:
        return None
    if blocker(result.status, result.http_status, result.text):
        return None
    body = re.sub(
        r"<script[^>]*src=[^>]*>\s*</script>|<link[^>]*>|<meta[^>]*name=[^>]*>", "", result.text
    )
    visible = re.sub(
        r"<[^>]+>|\s+", "", re.sub(r"<script.*?</script>", "", body, flags=re.S | re.I)
    )
    if len(visible) > 200:
        return None
    target = None
    for script in _SCRIPT_BODY.findall(result.text):
        # 只去掉 /* … */ 和整行的 // 注释：网址里的 http:// 不能当注释删
        code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        code = "\n".join(
            line for line in code.splitlines() if not line.strip().startswith("//")
        ).strip()
        if not code:
            continue
        m = _JS_REDIRECT_STMT.match(code)
        if m:
            target = m.group(2) or m.group(4)
            break
    if target is None:
        m = _META_REFRESH.search(result.text)
        target = m.group(1) if m else None
    if not target:
        return None
    base = result.final_url or url
    full = normalize_url(base, target)
    if not full or full == base or registrable(full) != registrable(base):
        return None
    return full


# 信息公开分类树上的分类写成"部门页#xxgk-分类键"（不是能直接打开的栏目，试读记"需接口"）
XXGK_TAG = "xxgk-"


# 政务服务网、公共资源交易等办事系统的部门页（门户"部门服务"导航常链到这里）：没有通知公告栏目。
# 按主机名的一段认（zwfw.hubei.gov.cn、ahzwfw.gov.cn、ggzy.xx.gov.cn），不按子串：
# 北京"政务服务和数据管理局"的主机是 zwfwj.beijing.gov.cn，不能误排
_SERVICE_PATH = re.compile(
    r"bszn|branch-hall|deptThing|onething|/icity/|workProcess|departmentHoem|/bsdt/", re.I
)


def _service_hall(url: str) -> bool:
    parts = urlsplit(url)
    labels = (parts.hostname or "").lower().split(".")
    if any(
        lab in ("zwfw", "ggzy", "zwdt") or (lab.endswith("zwfw") and len(lab) <= 8)
        for lab in labels
    ):
        return True
    return bool(_SERVICE_PATH.search(parts.path + "?" + parts.query))


# 部委的写法：国家…、中华人民共和国…、国务院…，或以"部""总局""总署"结尾（地方部门不这样叫）
_MINISTRY_NAME = re.compile(r"^(?:国家|中华人民共和国|国务院)|(?:部|总局|总署)$")


SHARED_ONLY = "部门页上只有本级共用的栏目（按部门筛选的列表要接口或人工确认）"


def _drop_shared_columns(rows: list[DeptRow]) -> frozenset[str]:
    """同一行政区里，不同部门网站（频道）指向同一个栏目网址：那是模板里共用的栏目（县政府文件、
    信息公开通用栏目），不算任何一个部门的。同一个合并部门（同一网址）算几类的不受影响。"""
    sites: dict[str, set[str]] = {}
    for r in rows:
        if r.dept == "政府办":
            continue
        for c in r.columns:
            sites.setdefault(c.url, set()).add(r.site_url)
    shared = {url for url, owners in sites.items() if len(owners) > 1}
    if not shared:
        return frozenset()
    for r in rows:
        if r.dept != "政府办" and r.columns:
            kept = [c for c in r.columns if c.url not in shared]
            if not kept:
                r.reason = SHARED_ONLY
            r.columns = kept
    return frozenset(shared)


_LEVEL_CHARS = "省市州盟县区旗"


def level_prefix(text: str) -> str | None:
    """部门简称开头表示层级的字（"省科技厅"的"省"）；"市场监管局"的"市"不算。"""
    if len(text) > 2 and text[0] in _LEVEL_CHARS and not text.startswith(("市场", "区块", "州际")):
        return text[0]
    return None


def level_chars(div: Division) -> str:
    """本级部门简称开头的字："省科技厅""市科技局""县科技局""区科技局"；县级市自称"市"。"""
    if div.level == "国家级":
        return "国"
    if div.level == "省级":
        return "省区" if div.kind == "自治区" else ("市" if div.kind == "直辖市" else "省")
    if div.level == "市级":
        return {"自治州": "州", "盟": "盟", "地区": "区"}.get(div.kind, "市")
    last = div.name[-1]
    return {"市": "市", "县": "县", "区": "区", "旗": "旗"}.get(last, last)


def complete_slots(portals: Iterable[PortalRow], depts: list[DeptRow]) -> list[DeptRow]:
    """门户没找到、打不开或拦截的行政区，盘点时没有部门行：按门户的状态补上 11 个标准部门格，
    "应接入"才不会少算。已有部门行的行政区不动。"""
    have = {d.code for d in depts}
    out = list(depts)
    for p in portals:
        if p.code in have:
            continue
        status = p.status if p.status in ("打不开", "不能抓") else "导航未找到"
        if p.status == "出错":
            reason = f"门户盘点出错：{p.reason[:80]}"
        elif p.status == "已找到":
            reason = "门户首页上没认出部门"
        elif p.status == "导航未找到":
            reason = "上级官方导航里没找到这个行政区的门户"
        else:
            reason = f"门户{p.status}：{p.reason[:80]}"
        for dept in STANDARD_DEPTS:
            out.append(
                DeptRow(
                    p.code,
                    p.level,
                    p.province,
                    p.city,
                    p.district,
                    dept,
                    status=status,
                    reason=reason,
                )
            )
    return out


def heading_more_links(doc: etree._Element, base_url: str) -> list[Link]:
    """ "通知公告"写成栏目标题（不是链接）、旁边一个"更多"链接时，取"更多"的地址。"""
    from youju.crawler.urls import normalize_url

    out: list[Link] = []
    for el in doc.iter(*_HEADING_TAGS):
        own = re.sub(r"\s+", "", "".join(el.xpath("text()")))
        if not own or len(own) > 10 or column_kind(own) is None:
            continue
        node = el
        for _ in range(3):
            node = node.getparent()
            if node is None:
                break
            for a in node.iter("a"):
                text = re.sub(r"\s+", "", a.text_content() or "")
                if _MORE.match(text) or (not text and "more" in (a.get("class") or "").lower()):
                    url = normalize_url(base_url, a.get("href"))
                    if url:
                        out.append(Link(own, url, "more"))
                        break
            else:
                continue
            break
    return out


def find_columns(
    pages: list[PageInfo], exclude: frozenset[str] = frozenset()
) -> dict[str, ColumnHit]:
    """在一组页面上按链接文字找目标栏目。

    完全相同的写法优先；同一写法里，在这个页面目录下的优先，其次同一主机，再次同一家网站
    （xx.gov.cn）。别家网站的不要；exclude 里的（门户自己的栏目，部门频道页顶上常有）不要。
    """
    best: dict[str, tuple[tuple[int, int, int, int], ColumnHit]] = {}
    for p in pages:
        links = list(p.links)
        if p.doc is not None:
            links += heading_more_links(p.doc, p.final_url)
        home = registrable(p.final_url)
        host = urlsplit(p.final_url).netloc.lower()
        pdir = page_dir(p.final_url)
        for order, link in enumerate(links):
            hit = column_kind(link.text)
            if hit is None or link.url in exclude:
                continue
            kind, strength = hit
            if not _site_like(link.url, p.final_url):
                continue
            if registrable(link.url) != home:
                continue  # 别家网站的栏目（如部门页上链到的省政策文件库）不算这个部门的栏目
            parts = urlsplit(link.url)
            under = 0 if pdir != "/" and parts.path.startswith(pdir) else 1
            same_host = 0 if parts.netloc.lower() == host else 1
            key = (strength, under, same_host, order)
            current = best.get(kind)
            if current is None or key < current[0]:
                best[kind] = (key, ColumnHit(kind, link.text, link.url, p.final_url))
        # 信息公开分类树上的分类：没有真正的栏目链接时才用（排在链接后面）
        for order, (title, cat_key) in enumerate(p.categories):
            hit = column_kind(title)
            if hit is None:
                continue
            kind, strength = hit
            key = (strength + 2, 1, 1, order)
            current = best.get(kind)
            if current is None or key < current[0]:
                url = f"{p.final_url.split('#', 1)[0]}#{XXGK_TAG}{cat_key}"
                best[kind] = (key, ColumnHit(kind, f"{title}（信息公开分类）", url, p.final_url))
    return {kind: hit for kind, (_, hit) in best.items()}


# —— 输出 ——

PORTAL_FIELDS = (
    "层级",
    "省份",
    "城市",
    "区县",
    "区划代码",
    "名称",
    "门户网址",
    "导航来源页",
    "导航文字",
    "状态",
    "原因",
    "网站标识码",
    "网站名称",
    "平台",
    "技术支持",
)
DEPT_FIELDS = (
    "层级",
    "省份",
    "城市",
    "区县",
    "区划代码",
    "部门",
    "部门全称",
    "网站URL",
    "网站类型",
    "导航来源页",
    "导航文字",
    "状态",
    "原因",
    "通知公告",
    "通知公告URL",
    "政策文件",
    "政策文件URL",
    "公示公告",
    "公示公告URL",
    "栏目来源页",
    "平台",
    "网站标识码",
    "技术支持",
)
_LEVEL_ORDER = {"国家级": -1, "省级": 0, "市级": 1, "区县级": 2}


def portal_rows(portals: Iterable[PortalRow]) -> list[list[str]]:
    rows = sorted(portals, key=lambda r: (r.code[:2], _LEVEL_ORDER[r.level], r.code))
    return [
        [
            r.level,
            r.province,
            r.city,
            r.district,
            r.code,
            r.name,
            r.portal_url,
            r.nav_source,
            r.nav_text,
            r.status,
            r.reason,
            r.site_id_code,
            r.site_name,
            r.platform,
            r.support,
        ]
        for r in rows
    ]


def dept_rows(depts: Iterable[DeptRow]) -> list[list[str]]:
    order = {d: i for i, d in enumerate(STANDARD_DEPTS + OPTIONAL_DEPTS)}
    rows = sorted(depts, key=lambda r: (r.code[:2], _LEVEL_ORDER[r.level], r.code, order[r.dept]))
    out = []
    for r in rows:
        cols = {c.kind: c for c in r.columns}
        sources = sorted({c.source for c in r.columns})
        out.append(
            [
                r.level,
                r.province,
                r.city,
                r.district,
                r.code,
                r.dept,
                r.dept_full_name,
                r.site_url,
                r.site_type,
                r.nav_source,
                r.nav_text,
                r.status,
                r.reason,
                cols["通知公告"].name if "通知公告" in cols else "",
                cols["通知公告"].url if "通知公告" in cols else "",
                cols["政策文件"].name if "政策文件" in cols else "",
                cols["政策文件"].url if "政策文件" in cols else "",
                cols["公示公告"].name if "公示公告" in cols else "",
                cols["公示公告"].url if "公示公告" in cols else "",
                " ".join(sources),
                r.platform,
                r.site_id_code,
                r.support,
            ]
        )
    return out


def write_table(path: Path, header: Iterable[str], rows: Iterable[list[str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(row)
            n += 1
    return n


def save_state(inv: Inventory, path: Path) -> None:
    """原始结果（含每个栏目找到的位置）存成 JSON，后续试跑、看板统计都从这里读。"""
    data = {
        "portals": [asdict(p) for p in inv.portals.values()],
        "depts": [asdict(d) for d in inv.depts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_state(path: Path) -> tuple[list[PortalRow], list[DeptRow]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    portals = [PortalRow(**p) for p in data["portals"]]
    depts = []
    for d in data["depts"]:
        cols = [ColumnHit(**c) for c in d.pop("columns", [])]
        depts.append(DeptRow(**d, columns=cols))
    return portals, depts


def clean_dept_name(text: str, div: Division) -> str:
    """去掉导航文字里的空白和括号注释，保留机关名。"""
    return clean_place_text(text) if text else ""
