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
from youju.govsites.attribution import (  # noqa: F401 - level_*、_own_branch 原在本模块，保留旧名字
    NOT_OWN,
    Places,
    branch_of_own,
    level_chars,
    level_prefix,
    name_verdict,
    places_for,
    portal_verdict,
    site_verdict,
    title_names,
)
from youju.govsites.attribution import own_branch as _own_branch  # noqa: F401
from youju.govsites.deptdir import (
    DIR_NAV,
    find_directories,
    gkmlpt_tree_categories,
    jpaas_path_groups,
)
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
from youju.govsites.names import COLUMN_KINDS, alias_categories, column_kind, dept_categories
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
    via: str = ""  # "部门目录"：从信息公开平台的部门目录页找到的（deptdir.py）
    alias: bool = False  # 这一类是按括号里加挂的牌子认的（备用候选）


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
    directory: bool = False  # 信息公开平台的部门目录页（deptdir.py）：上面的部门网址可以深一些


@dataclass(frozen=True)
class _Owner:
    """判断部门网站归属要用的上下文（一个行政区算一次，几个部门线程共用）。"""

    places: Places
    own_portal: str
    own_portal_code: str
    upper_portals: tuple[str, ...]


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


_PICK_TRIES = 3  # 每类部门最多比几个能打开的候选页面（取第一个有能直接读的栏目的）
_PICK_FETCHES = 6  # 每类部门最多抓几个候选页面（打不开、被否掉的也算）
_PICK_FETCHES_ZONE = 10  # 开发区可能有好几个
BRANCH_SECTION = (
    "分局版块"  # _not_own_site：派出分局在上级网站上的版块（算本区县的，栏目只认版块里的）
)
# 像文章、部门年报目录、脚本列表页的网址：同分时排在网站首页、栏目页后面
_WEAK_PAGE = re.compile(
    r"/info/\d+/\d+\.s?html?$|/content/|/gkmlpt/|zfxxgknb|/bmnb/|/nb/|ndbb|nianbao|"
    r"/jgjj/|/jianjie/|/ldxx/|/ldjs/|/ldzc/",
    re.I,
)


def _weak_page(url: str) -> int:
    parts = urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    return int(_WEAK_PAGE.search(path) is not None or any(p.search(path) for _, p in URL_SHAPES))


def _hostname(url: str) -> str:
    """主机名，小写、不带端口（"www.x.gov.cn:443" 和 "www.x.gov.cn" 是同一个）。"""
    return (urlsplit(url or "").hostname or "").lower()


def _site_root(url: str) -> bool:
    """网站首页：路径为空或只是 index.html / index.shtml 一类。

    派出分局链到上级部门首页时不算分局版块。"""
    path = urlsplit(url or "").path.strip("/")
    return (
        path == ""
        or re.fullmatch(r"(?:index|default|main)\.(?:s?html?|jsp|aspx?|php)", path) is not None
    )


def _column_name(page: PageInfo) -> str:
    """栏目页自己写的栏目名：meta ColumnName，没有就用标题的第一段。"""
    if page.doc is None:
        return ""
    for meta in page.doc.iter("meta"):
        if (meta.get("name") or "").lower() == "columnname" and (meta.get("content") or "").strip():
            return meta.get("content").strip()
    title = (page.doc.findtext(".//title") or "").strip()
    if not title:
        return ""
    # 标题各段里找像栏目名的一段（"××县人民政府 - 政策文件"），没有就取第一段
    parts = [t for t in re.split(r"\s*[-_|｜—]\s*", title) if t]
    return next((t for t in parts if column_kind(t)), parts[0] if parts else "")


def _readable(row: DeptRow) -> bool:
    """部门页上认出了能直接打开的栏目（不是要接口的信息公开分类）。"""
    return row.status == "已找到" and any(f"#{XXGK_TAG}" not in c.url for c in row.columns)


# 门户的专题专栏路径（/zt/、/ztzl/、/zhuanti/……）：部门页上链到这里的不算部门自己的栏目
_TOPIC_PATH = re.compile(r"/(?:zt|ztzl|ztlm|zhuanti|special|topic)(?:/|_|\d|$)", re.I)
# 部门目录页上的部门网址最多几层（山西县级平台 /lczw/zfxxgk/bmxxgk/zfzcbm/部门_编号/ 是 5 层）
_DIR_DEPTH = 6
# 部门目录里的部门页上，栏目最多几层（山西 /lczw/zfxxgk/bmxxgk/zfzcbm/部门/公开内容/栏目/ 7 层）
_COL_DEPTH = 9


def _site_like(url: str, current: str, max_depth: int = 4) -> bool:
    """像不像一个网站（或频道）的入口，而不是一篇文章：不是文章网址形状，路径不太深。"""
    parts = urlsplit(url)
    path = parts.path or "/"
    for _, pattern in URL_SHAPES:
        if pattern.search(path + ("?" + parts.query if parts.query else "")):
            return False
    if path.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".png", ".zip")):
        return False
    depth = len([seg for seg in path.split("/") if seg])
    return depth <= max_depth


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
    # 这几级的门户再找信息公开平台里的部门目录页（县直部门多半只在那里列出，deptdir.py）
    deptdir_levels: frozenset[str] = frozenset({"区县级"})

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
        seeds: dict[str, list[tuple[str, str]]] | None = None,
        dept_levels: Iterable[str] | None = None,
    ) -> None:
        self.index = index
        self.cache = cache
        self.only = set(provinces or [])
        self.max_level = max_level
        self.workers = workers
        self.per_province = per_province
        self.inner = inner
        # 导航入口补充：区划代码 → [(官方入口网址, 说明)]。门户打不开、或市县导航由脚本生成时，
        # 用本地区其他官方网站（如省直部门网站）的导航补找部门和下级门户
        self.seeds = seeds or {}
        # 只对这几级找部门（--levels）：上级只走门户和导航页找下级，不去抓上级的部门网站
        self.dept_levels = set(dept_levels) if dept_levels else None
        self.directories: dict[str, list[str]] = {}  # 区划代码 → 认出的部门目录页（可追溯）
        self._seen_portals: dict[str, str] = {}  # 区划代码 → 导航里认出的门户网址（排下级之前记）
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
                    groups = jpaas_xxgk_groups(html, info.final_url)
                    # 条目网址是"分类目录/art/…"写法的（长兴等县的部门页），按分类目录还原
                    named = {name for name, _ in groups}
                    groups += [
                        g for g in jpaas_path_groups(html, info.final_url) if g[0] not in named
                    ]
                    info.links += [Link(name, url, "jpaas分组") for name, url in groups]
        if "/gkmlpt/" in info.final_url and "TREE" in result.text:
            # 数字广东信息公开平台：分类树写在页面里，列表由接口按分类加载（试读记"需接口"）
            info.categories += gkmlpt_tree_categories(result.text)
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

    def _seed_pages(self, div: Division) -> list[PageInfo]:
        """导航入口补充里这个行政区的官方入口页，连同它们的导航页（页脚的"省直部门""市县网站"等）。"""
        out: list[PageInfo] = []
        for url, note in self.seeds.get(div.code, []):
            sp = self.page(url)
            if sp.result.status != "ok" or sp.block:
                continue
            sp.level_hint = div.level
            sp.nav_text = f"导航入口补充：{note}"
            for p in self._nav_pages(sp):
                if not p.nav_text or p is sp:
                    p.nav_text = sp.nav_text
                out.append(p)
        return out

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
        places = places_for(self.index, div)
        upper_hosts = {_hostname(u) for u in self._upper_portals(places)}
        if home.result.status != "ok" or home.block:
            row.status = "不能抓" if home.block else "打不开"
            row.reason = home.block or home.result.error or home.result.status
            if not self.seeds.get(div.code):
                self._finish(row, [])
                return self._enqueue(div, children, {})
            # 门户不通，但有官方入口补充：用它们找部门和下级门户（门户本身照记不通，不绕过）
            pages = self._seed_pages(div)
            child_found = self._find_children(div, children, pages, upper_hosts)
            self._remember_children(child_found)
            depts = (
                self._find_depts(div, home, pages, portal_ok=False)
                if pages and self._wants_depts(div)
                else []
            )
            self._finish(row, depts)
            return self._enqueue(div, children, child_found)
        fp = home.fp or Fingerprint()
        title = (home.doc.findtext(".//title") or "") if home.doc is not None else ""
        wrong = portal_verdict(
            [fp.site_name, *title_names(title)],
            fp.site_id_code,
            home.final_url,
            places,
            self.index,
            upper_hosts,
        )
        if wrong.kind in NOT_OWN:
            # 导航链接认错了（链到上级门户、外省网站）：这个行政区当"门户没找到"，不从错的网站找部门
            row.status, row.portal_url = "导航未找到", ""
            site = fp.site_name or title.strip()
            code = fp.site_id_code or "无"
            row.reason = (
                f"导航链接指向的不是本地门户（{wrong.reason}）："
                f"{site}（{home.final_url}，标识码 {code}）"
            )[:150]
            self._finish(row, [])
            return self._enqueue(div, children, {})
        row.portal_url = home.final_url
        row.status = "已找到"
        row.site_id_code, row.site_name = fp.site_id_code, fp.site_name
        row.platform, row.support = fp.platform, fp.support
        code6 = fp.site_id_code[:6]
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
        portal_pages = self._nav_pages(home)
        if div.level == "国家级":
            dept_nav = self.page(GOV_DEPT_NAV)
            if dept_nav.result.status == "ok":
                dept_nav.nav_text = "国务院部门网站"
                portal_pages.append(dept_nav)
        # 入口补充页（省直部门网站）只用来找部门和下级门户；政府办的栏目只看门户自己的页面
        pages = portal_pages + self._seed_pages(div)
        # 链回本门户、上级门户的不是下级门户（南通市年报页上区县名链到市门户内页）
        own_host = _hostname(home.final_url)
        child_found = self._find_children(div, children, pages, upper_hosts | {own_host})
        self._remember_children(child_found)
        if not self._wants_depts(div):
            self._finish(row, [])
            return self._enqueue(div, children, child_found)
        try:
            depts = self._find_depts(div, home, pages, portal_pages=portal_pages)
        except Exception as exc:  # noqa: BLE001 - 部门出意外不影响下级照常排队
            log.exception("找 %s %s 的部门时出错", div.province, div.name)
            row.reason = f"找部门时出错：{exc.__class__.__name__}"
            depts = []
        self._finish(row, depts)
        return self._enqueue(div, children, child_found)

    def _wants_depts(self, div: Division) -> bool:
        return self.dept_levels is None or div.level in self.dept_levels

    def _upper_portals(self, places: Places) -> list[str]:
        """上级行政区的门户网址：已处理完的取门户行，还没轮到的取导航里认出的网址。

        省门户直接列出的区县和地市并行跑，区县可能先于地市处理完，只看 self.portals 结果会
        取决于谁先跑完；导航里认出的网址在排下级之前就记下了（self._seen_portals）。"""
        out: list[str] = []
        with self._lock:
            for c in sorted(places.ancestor_codes):
                row = self.portals.get(c)
                if row is not None and row.portal_url:
                    out.append(row.portal_url)
                if self._seen_portals.get(c):
                    out.append(self._seen_portals[c])
        return list(dict.fromkeys(out))

    def _remember_children(self, found: dict[str, Found]) -> None:
        with self._lock:
            for code, f in found.items():
                self._seen_portals.setdefault(code, f.url)

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
        self,
        div: Division,
        children: list[Division],
        pages: list[PageInfo],
        skip_hosts: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Found]:
        if not children:
            return {}
        prov = self.index.province_of(div)
        prefixes = [prov.name, prov.province]
        if div.level == "市级":
            prefixes += sorted(name_variants(div.name, div.kind), key=len, reverse=True)
        # 下级简称和外省同名的（天津河北区的"河北"、青海海南州的"海南"）只认全称：
        # 门户上单写"河北"的多是兄弟省份链接
        matcher = NameMatcher(children, prefixes, exclude_short=set(self._foreign_names(div)))
        found: dict[str, Found] = {}
        for p in pages:
            for link in p.links:
                d = matcher.match(link.text)
                if d is None or d.code in found or not is_gov_url(link.url):
                    continue
                if _service_hall(link.url):
                    continue  # 政务服务网的地区页不是下级门户
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
                        if (parts.hostname or "").lower() in skip_hosts:
                            continue  # 链到本门户、上级门户的内页（南通市年报页）：不是下级门户
                        url = f"{parts.scheme}://{parts.netloc}/"
                        found[d.code] = Found(
                            url, f"{link.text}（链到 {link.url}，取网站首页）", source
                        )
                        continue
                if _hostname(url) in skip_hosts:
                    continue  # 认出来的"下级门户"其实是本门户或上级门户
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

    def _foreign_names(self, div: Division) -> list[str]:
        """外省的名称写法（长的在前）：部门链接文字以它们开头的是外省的部门。

        本行政区名字和外省同名开头的（青海海南州、河南县）由调用方比长短：本地写法更长的不算外省。"""
        if div.level == "国家级":
            return []
        prov = self.index.province_of(div)
        names = {
            v
            for p in self.index.provinces()
            if p.code != prov.code
            for v in name_variants(p.name, p.kind)
            if len(v) >= 2
        }
        return sorted(names, key=len, reverse=True)

    def _seed_sites(self, div: Division, own: list[str]) -> list[tuple[str, Found]]:
        """导航入口补充里的部门网站本身，作为它那个部门的候选：(部门, 找到的网站)。

        只认网站名（meta SiteName，没有就用标题）以本行政区名称开头、能归到目标部门的；
        导航来源页记入口网址本身（可追溯到导航入口补充表）。"""
        out: list[tuple[str, Found]] = []
        for url, _note in self.seeds.get(div.code, []):
            sp = self.page(url)
            if sp.result.status != "ok" or sp.block:
                continue
            name = (sp.fp.site_name if sp.fp else "") or (
                (sp.doc.findtext(".//title") or "").strip() if sp.doc is not None else ""
            )
            if not name or not any(name.startswith(o) for o in own):
                continue
            for cat in dept_categories(name):
                if cat != "政府办":
                    out.append((cat, Found(sp.final_url, name, url)))
        return out

    def _find_depts(
        self,
        div: Division,
        home: PageInfo,
        pages: list[PageInfo],
        portal_ok: bool = True,
        portal_pages: list[PageInfo] | None = None,
    ) -> list[DeptRow]:
        if portal_ok and div.level in self.deptdir_levels:
            pages = pages + self._directory_pages(div, home, pages)
        places = places_for(self.index, div)
        own = list(places.own)
        candidates: dict[str, list[tuple[int, Found]]] = {}
        for cat, found in self._seed_sites(div, own):
            # 入口补充里的部门网站本身（网站名写明是本省的哪个部门）：最优先
            candidates.setdefault(cat, []).append((10, found))
        for p in pages:
            report = bool(REPORT_WORDS.search(p.nav_text))
            nav_bonus = (
                2 if report or re.search(r"部门|省直|市直|区直|县直|机构|单位", p.nav_text) else 0
            )
            for link in p.links:
                text = _clean_link_text(link.text)
                cats = dept_categories(text)
                if not cats or not is_gov_url(link.url):
                    continue
                # 名称说明是上级、本省别处、外省的部门（"省科技厅""呼和浩特市发改委""财政厅"在
                # 县里、省直部门网站上的"兄弟省份"链接）不算本地的。开发区例外：地市开发区常以
                # 地市名打头，县里链到的可能是本县代管的，上级同一网址的由 _pick 去掉
                verdict = name_verdict(text, places)
                if verdict.kind in NOT_OWN and not (verdict.kind == "上级" and cats == ["开发区"]):
                    continue
                if div.level != "国家级" and self._is_ministry(text, link.url):
                    continue  # 页脚"国务院部门网站"里的部委，不是本地部门
                if _service_hall(link.url) and not (
                    "数据局" in cats and _data_bureau_site(link.url)
                ):
                    continue  # 政务服务网的部门办事页（"部门服务"），不是部门信息发布网站
                depth = _DIR_DEPTH if p.directory else 4
                if not _site_like(link.url, p.final_url, depth) and not report:
                    continue
                score = nav_bonus
                if verdict.kind == "本级":
                    score += 3
                if _hostname(link.url) != _hostname(home.final_url):
                    score += 1
                via = DIR_NAV if p.directory else ""
                for cat in cats:
                    if cat == "政府办":
                        continue  # 政府办看门户本身
                    candidates.setdefault(cat, []).append(
                        (score, Found(link.url, text, p.final_url, via))
                    )
                # 括号里加挂的牌子（"经信局（商务局）"）：备用候选，这一类没有正名候选时才用，
                # 免得"科学技术局（知识产权局）"抢走真正的市场监管局
                for cat in alias_categories(text):
                    candidates.setdefault(cat, []).append(
                        (score, Found(link.url, text, p.final_url, via, alias=True))
                    )
        rows: list[DeptRow] = []
        # 政府办公厅：门户本身的政府文件、通知公告
        gov = self._dept_row(div, "政府办")
        gov.dept_full_name = (
            home.fp.site_name if home.fp and home.fp.site_name else f"{div.name}人民政府"
        )
        gov.site_url, gov.site_type, gov.nav_source = home.final_url, "门户本身", home.url
        if portal_ok:
            gov.status = "已找到"
            self._fill_from_page(gov, home, portal_pages if portal_pages is not None else pages)
        else:
            # 门户不通：政府办（门户的政府文件、通知公告）照记不通，其他部门从官方入口补充里找
            gov.status = "不能抓" if home.block else "打不开"
            gov.reason = f"门户{gov.status}：{(home.block or home.result.error or '')[:80]}"
        rows.append(gov)
        cats = STANDARD_DEPTS[1:] + OPTIONAL_DEPTS
        # 抓到部门网站后判断归属要用：本地门户、上级门户
        owner = _Owner(
            places,
            home.final_url,
            home.fp.site_id_code if portal_ok and home.fp else "",
            tuple(self._upper_portals(places)),
        )
        # 各部门网站多在不同主机上，并行抓（同一主机仍由抓取器排队、间隔 3 秒）
        with ThreadPoolExecutor(self.inner, thread_name_prefix="coverage-dept") as ex:
            portal_cols = frozenset(c.url for c in gov.columns)
            picked = list(
                ex.map(
                    lambda c: self._pick(
                        div, home, c, candidates.get(c, []), portal_cols, owner=owner
                    ),
                    cats,
                )
            )
        for rows_for_cat in picked:
            rows.extend(rows_for_cat)
        before = {id(r): {c.kind for c in r.columns} for r in rows}
        dir_sources = {p.final_url for p in pages if p.directory}
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
                depth = _COL_DEPTH if r.nav_source in dir_sources else 4
                self._fill_from_page(r, page, [page], portal_cols | shared, depth)
                have = {c.kind for c in kept}
                r.columns = kept + [c for c in r.columns if c.kind in lost and c.kind not in have]
                if r.columns and r.reason == SHARED_ONLY:
                    r.reason = ""
            _drop_shared_columns(rows)
        return rows

    def _directory_pages(
        self, div: Division, home: PageInfo, pages: list[PageInfo]
    ) -> list[PageInfo]:
        """信息公开平台里的部门目录页（deptdir.py），当导航页用；出了意外只是不找目录，部门照常认。"""
        try:
            extra = find_directories(self, div, home, pages)
        except Exception:  # noqa: BLE001 - 找目录出意外不影响这个行政区别的部门
            log.exception("找 %s %s 的部门目录时出错", div.province, div.name)
            return []
        dirs = list(dict.fromkeys(p.final_url for p in pages + extra if p.directory))
        if dirs:
            with self._lock:
                self.directories[div.code] = dirs
        return extra

    def _pick(
        self,
        div: Division,
        home: PageInfo,
        cat: str,
        options: list[tuple[int, Found]],
        portal_cols: frozenset[str] = frozenset(),
        owner: "_Owner | None" = None,
    ) -> list[DeptRow]:
        if not options:
            return [self._dept_row(div, cat)] if cat in STANDARD_DEPTS else []
        # 括号里加挂的牌子只当备用候选：正名候选一个都不成（没有、或都被否掉）才用
        primary = [o for o in options if not o[1].alias]
        aliases = [o for o in options if o[1].alias]
        rows, rejected = self._pick_from(div, home, cat, primary or aliases, portal_cols, owner)
        if not rows and primary and aliases:
            rows, more = self._pick_from(div, home, cat, aliases, portal_cols, owner)
            rejected += more
        if not rows and rejected and cat in STANDARD_DEPTS:
            row = self._dept_row(div, cat)
            row.status = "导航未找到"
            row.reason = ("门户上的部门链接指向别处：" + "；".join(rejected[:2]))[:120]
            return [row]
        return rows or ([self._dept_row(div, cat)] if cat in STANDARD_DEPTS else [])

    def _pick_from(
        self,
        div: Division,
        home: PageInfo,
        cat: str,
        options: list[tuple[int, Found]],
        portal_cols: frozenset[str],
        owner: "_Owner | None",
    ) -> tuple[list[DeptRow], list[str]]:
        rows: list[DeptRow] = []
        rejected: list[str] = []
        seen: set[str] = set()
        top_score: int | None = None
        fetched = 0
        limit = _PICK_FETCHES_ZONE if cat == "开发区" else _PICK_FETCHES
        # 同分时网站首页、栏目页排在文章、部门年报、脚本页前面
        # （锦州"机构简介"文章、葫芦岛部门年报目录）
        for score, f in sorted(options, key=lambda o: (-o[0], _weak_page(o[1].url))):
            if f.url in seen:
                continue
            if rows and cat != "开发区" and score != top_score and not self._named_own(f, owner):
                # 已有首选、要往后试时，只试同分的、或链接文字明写是本地的：分更低、名称又说不清
                # 的别家网站（市局独立域名的"科技局"）不能顶掉本地的频道
                continue
            if fetched >= limit:
                break  # 打不开、被否掉的也算一次：每类部门最多抓这么多个候选页面
            fetched += 1
            seen.add(f.url)
            if not _site_like(f.url, f.source, _DIR_DEPTH if f.via else 4):
                # 年度报表里的一页：表里"首页网址"才是部门网站
                rp = self.page(f.url)
                site = report_homepage(rp)
                pdf = report_pdf(rp) if rp.result.status == "ok" and not site else None
                if pdf:
                    site = report_homepage_text(self.cache.get(pdf).text)
                if not site or site in seen:
                    continue
                f = Found(site, f.text, f.url, f.via, f.alias)
                seen.add(site)
            row = self._dept_row(div, cat)
            row.dept_full_name, row.site_url = f.text, f.url
            row.nav_source, row.nav_text = f.source, f.text
            same_host = _hostname(f.url) == _hostname(home.final_url)
            row.site_type = "门户频道" if same_host else "独立网站"
            dp = self.page(f.url)
            why = self._not_own_site(cat, f, dp, owner) if owner is not None else ""
            section = why == BRANCH_SECTION
            if why and not section:
                rejected.append(why)
                continue  # 链到的是上级、别处、外省的部门网站：看下一个候选
            # 门户自己的栏目不算部门的；几个部门都链到的同一个栏目（如省政策文件库）由
            # _drop_shared_columns 去掉。部门挂在门户主机上的信息公开栏目照常认
            exclude = portal_cols
            if dp.result.status != "ok" or dp.block:
                row.status = "不能抓" if dp.block else "打不开"
                row.reason = dp.block or dp.result.error or dp.result.status
            else:
                row.status = "已找到"
                row.site_url = dp.final_url
                # 部门目录里的部门页（信息公开平台）栏目在深处：…/部门/fdzdgknr/zcwj/子分类/
                self._fill_from_page(row, dp, [dp], exclude, _COL_DEPTH if f.via else 4)
                if section:
                    # 派出分局在上级网站上的版块：上级网站菜单里的栏目是上级的，只认版块下面的
                    base = page_dir(dp.final_url)
                    row.columns = [c for c in row.columns if urlsplit(c.url).path.startswith(base)]
            if cat == "开发区":
                rows.append(row)
                if len(rows) >= 6:  # 开发区可能有好几个（国家级、省级各一）
                    break
                continue
            # 其他部门只取最像的一个。首选能打开、但认不出能直接读的栏目时（杭州部门被"组织机构
            # 职能"页上的门户信息公开频道顶掉、常州的简介页），再试后面的，最多看 3 个，取第一个
            # 有能直接读的栏目的。首选打不开、被拦的照旧记它，后面打不开、被拦的也不拿来顶替
            if rows and row.status != "已找到":
                continue
            if not rows:
                top_score = score
            rows.append(row)
            if row.status != "已找到" or _readable(row) or len(rows) >= _PICK_TRIES:
                break
        if cat != "开发区" and len(rows) > 1:
            best = next((r for r in rows if _readable(r)), None)
            best = best or next((r for r in rows if r.columns), None) or rows[0]
            rows = [best]
        return rows, rejected

    def _named_own(self, f: Found, owner: "_Owner | None") -> bool:
        """链接文字明写是本地的部门（以本地地名、本级层级字打头，或本地的派出分局）。"""
        return owner is not None and name_verdict(f.text, owner.places).kind == "本级"

    def _check_group_columns(
        self,
        pages: list[PageInfo],
        hits: dict[str, ColumnHit],
        exclude: frozenset[str],
        max_depth: int,
        skip_topics: bool = False,
    ) -> dict[str, ColumnHit]:
        """由大汉分组条目还原出来的栏目页，看它自己叫什么（meta ColumnName，没有就看标题）：
        分组"政策文件"里的条目可能发在"其他文件政策解读"栏目（拱墅区），还原出的栏目名不是这一类
        的就不要；它自己的名字是另一类目标栏目、那一类又还空着的，记到那一类。"""
        groups = {link.url for p in pages for link in p.links if link.via == "jpaas分组"}
        bad: set[str] = set()
        moved: list[ColumnHit] = []  # 自己的名字是另一类目标栏目的
        for kind, hit in hits.items():
            if hit.url not in groups:
                continue
            cp = self.page(hit.url)
            ok = cp.result.status == "ok" and not cp.block
            name = _column_name(cp) if ok else ""
            own = column_kind(name) if name else None
            if own is not None and own[0] == kind:
                continue
            bad.add(hit.url)
            if own is not None:
                moved.append(ColumnHit(own[0], name, hit.url, hit.source))
        if not bad:
            return hits
        hits = find_columns(pages, exclude | frozenset(bad), max_depth, skip_topics)
        for hit in moved:
            hits.setdefault(hit.kind, hit)
        return hits

    def _not_own_site(self, cat: str, f: Found, dp: PageInfo, owner: "_Owner") -> str:
        """候选部门网站不是本地的（上级、别处、外省）时返回一句原因，是本地的或说不清返回空串。

        打不开、被拦的页面只按网址判断（域名）。开发区只认两种明确的错：在上级门户的域名下、
        外省的（地市开发区常以地市名打头，名称判不了归属）。"""
        ok = dp.result.status == "ok" and not dp.block and dp.fp is not None
        final = dp.final_url or f.url
        if ok and _service_hall(final) and not (cat == "数据局" and _data_bureau_site(final)):
            return f"「{f.text}」→{final}（链接跳到了政务服务网的办事页）"
        names: list[str] = []
        if ok:
            title = (dp.doc.findtext(".//title") or "") if dp.doc is not None else ""
            names = [n for n in [dp.fp.site_name, *title_names(title)] if n]
        v = site_verdict(
            names,
            dp.fp.site_id_code if ok else "",
            [f.url, final],
            owner.places,
            self.index,
            own_portal=owner.own_portal,
            own_portal_code=owner.own_portal_code,
            upper_portals=owner.upper_portals,
        )
        if cat == "开发区" and v.kind != "外省":
            # 只按域名判（名称以地市名打头的不算数）：在上级门户的域名下、不在本地的域名下
            v = site_verdict(
                [],
                "",
                [f.url, final],
                owner.places,
                self.index,
                own_portal=owner.own_portal,
                upper_portals=owner.upper_portals,
            )
        if v.kind not in NOT_OWN:
            return ""
        if (
            v.kind == "上级"
            and branch_of_own(f.text, owner.places)
            and not _site_root(f.url)
            and not _site_root(dp.final_url)
        ):
            # "银川市生态环境局西夏分局"链到市局网站上分局自己的版块（/hbfj/xxfj/）：派出分局的
            # 东西就在上级网站上，算本区县的；链到市局首页的才不算。栏目只认版块里的（_pick）
            return BRANCH_SECTION
        return f"「{f.text}」→{names[0] if names else dp.final_url}（{v.reason}）"

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
        max_depth: int = 4,
    ) -> None:
        fp = page.fp or Fingerprint()
        row.site_id_code, row.platform, row.support = fp.site_id_code, fp.platform, fp.support
        # 部门网站自己写的网站名（meta SiteName）是同一类部门的全称，就用它
        if row.dept != "政府办" and fp.site_name and row.dept in dept_categories(fp.site_name):
            row.dept_full_name = fp.site_name
        topics = row.dept != "政府办"
        hits = find_columns(pages, exclude, max_depth, topics)
        hits = self._check_group_columns(pages, hits, exclude, max_depth, topics)
        missing = [k for k in COLUMN_KINDS if k not in hits]
        if missing and row.dept != "政府办":
            # 部门首页没有：沿"信息公开"再找一层
            for link in page.links:
                if _INFO_WORDS.search(link.text) and registrable(link.url) == registrable(
                    page.final_url
                ):
                    sub = self.page(link.url)
                    if sub.result.status == "ok" and not sub.block:
                        for kind, hit in find_columns([sub], exclude, max_depth, topics).items():
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


# 链接文字开头常带的符号（">海南省商务厅""·省财政厅"）：去掉再比名称
_LINK_LEAD = re.compile(r"^[\s>》›»·•|｜\-—–:：、]+")


def _clean_link_text(text: str) -> str:
    return _LINK_LEAD.sub("", text or "").strip()


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


_SERVICE_QUERY = re.compile(
    r"portal/department|itemList|OrgItem|deptCode|regionCode|areaCode", re.I
)


def _data_bureau_site(url: str) -> bool:
    """有的地方数据局网站就叫 zwfw.某市.gov.cn：只放行这种本地子域的首页，
    省级政务服务网（hnzwfw.gov.cn 等）、部门办事页、事项页一律不放。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if labels[0] != "zwfw" or len(labels) < 4 or host.endswith("zwfw.gov.cn"):
        return False
    if parts.query or _SERVICE_QUERY.search(parts.path) or _SERVICE_PATH.search(parts.path):
        return False
    return len([seg for seg in parts.path.split("/") if seg]) <= 1


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
            # 导航认错被否掉的（链到上级门户、外省网站）门户行写了原因，照抄过来
            reason = p.reason[:80] if p.reason else "上级官方导航里没找到这个行政区的门户"
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
    pages: list[PageInfo],
    exclude: frozenset[str] = frozenset(),
    max_depth: int = 4,
    skip_topics: bool = False,
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
            # 由大汉分组条目还原的分类栏目页本来就深（…/fdzdgknr/zcwj/子分类/index.html）
            depth = max(max_depth, _COL_DEPTH) if link.via == "jpaas分组" else max_depth
            if not _site_like(link.url, p.final_url, depth):
                continue
            if skip_topics and _TOPIC_PATH.search(urlsplit(link.url).path):
                continue  # 门户的专题专栏（枣庄"财政预决算公开"专题）不是部门自己的栏目
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


def save_state(inv: Inventory, path: Path, levels: Iterable[str] | None = None) -> None:
    """原始结果（含每个栏目找到的位置）存成 JSON，后续试跑、看板统计都从这里读。

    levels 给了只存这几级的门户和部门：只写某几级时上级没找部门，存进去会被 complete_slots 补成
    "导航未找到"，再拿去导入看板就把上级格的网址和栏目冲掉。"""
    keep = set(levels or [])
    data = {
        "portals": [asdict(p) for p in inv.portals.values() if not keep or p.level in keep],
        "depts": [asdict(d) for d in inv.depts if not keep or d.level in keep],
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


def load_docs(
    out_dir: Path, provinces: Iterable[str] | None = None
) -> tuple[list[PortalRow], list[DeptRow]]:
    """从清单文件（门户网站/<省>.csv、部门栏目/<省>.csv）读回盘点结果：不用本机缓存也能导入看板。"""
    only = set(provinces or [])
    portals: list[PortalRow] = []
    depts: list[DeptRow] = []
    for path in sorted((out_dir / "门户网站").glob("*.csv")):
        if only and path.stem not in only:
            continue
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                portals.append(
                    PortalRow(
                        r["区划代码"],
                        r["层级"],
                        r["省份"],
                        r["城市"],
                        r["区县"],
                        r["名称"],
                        portal_url=r["门户网址"],
                        nav_source=r["导航来源页"],
                        nav_text=r["导航文字"],
                        status=r["状态"],
                        reason=r["原因"],
                        site_id_code=r["网站标识码"],
                        site_name=r["网站名称"],
                        platform=r["平台"],
                        support=r["技术支持"],
                    )
                )
    for path in sorted((out_dir / "部门栏目").glob("*.csv")):
        if only and path.stem not in only:
            continue
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                source = (r["栏目来源页"] or "").split(" ")[0]
                cols = [
                    ColumnHit(kind, r[kind], r[f"{kind}URL"], source)
                    for kind in COLUMN_KINDS
                    if r.get(f"{kind}URL")
                ]
                depts.append(
                    DeptRow(
                        r["区划代码"],
                        r["层级"],
                        r["省份"],
                        r["城市"],
                        r["区县"],
                        r["部门"],
                        dept_full_name=r["部门全称"],
                        site_url=r["网站URL"],
                        site_type=r["网站类型"],
                        nav_source=r["导航来源页"],
                        nav_text=r["导航文字"],
                        status=r["状态"],
                        reason=r["原因"],
                        site_id_code=r["网站标识码"],
                        platform=r["平台"],
                        support=r["技术支持"],
                        columns=cols,
                    )
                )
    return portals, depts


SEED_FIELDS = ("省份", "区划代码", "入口网址", "说明")


def load_seeds(path: Path) -> dict[str, list[tuple[str, str]]]:
    """导航入口补充：每行一个官方入口页，写明为什么用它。

    path 是目录（docs/数据来源/全国覆盖/导航入口补充/，按省按层各一个文件，如"江西_省级.csv"，
    几条线各写各的文件）就读里面所有 CSV；是文件就只读它。"""
    if not path.exists():
        return {}
    files = sorted(path.glob("*.csv")) if path.is_dir() else [path]
    out: dict[str, list[tuple[str, str]]] = {}
    for file in files:
        with file.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                url = (r.get("入口网址") or "").strip()
                code = (r.get("区划代码") or "").strip()
                if url and code and is_gov_url(url):
                    out.setdefault(code, []).append((url, (r.get("说明") or "").strip()))
    return out
