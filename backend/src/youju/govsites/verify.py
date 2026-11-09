"""试读栏目：用正式抓取程序的列表识别（listing.extract_items）读一遍盘点找到的栏目，
看能不能直接读出带日期的条目；读不出的分清是"要浏览器渲染或接口"、"防护拦截"还是"打不开"。

只读列表页一页，不翻页、不抓正文；同一网站内排队、间隔 3 秒（PoliteFetcher）。
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from youju.crawler.adapters import api_html, find_list_api
from youju.crawler.listing import extract_items
from youju.crawler.urls import host_of
from youju.govsites.inventory import XXGK_TAG, DeptRow
from youju.govsites.links import page_links, parse_html
from youju.govsites.pagecache import PageCache
from youju.govsites.platform import blocker, fingerprint

# 列表是脚本加载的迹象（页面上读不出条目时据此判断"要浏览器渲染或接口"）
_SCRIPT_LIST = re.compile(
    r"/module/web/jpage/|dataproxy\.jsp|\.json['\"?]|ajax|\$\.get|\$\.post|axios|fetch\(|"
    r"createPageHTML|laypage|pagination|getList|loadData|template\(|v-for=|ng-repeat",
    re.I,
)
# 试读结论 → 看板状态
READABLE = "可读"
NEEDS_SCRIPT = "需浏览器渲染或接口"
NO_ITEMS = "未识别出条目"
BLOCKED = "不能抓"
DOWN = "打不开"


@dataclass
class ColumnCheck:
    code: str
    level: str
    province: str
    city: str
    district: str
    dept: str
    dept_full_name: str
    kind: str
    name: str
    url: str
    site_url: str
    nav_source: str
    column_source: str
    platform: str = ""
    result: str = ""  # 可读 / 需浏览器渲染或接口 / 未识别出条目 / 不能抓 / 打不开
    reason: str = ""
    http_status: int | None = None
    items: int = 0
    dated: int = 0
    latest: date | None = None
    samples: list[str] = field(default_factory=list)


def columns_of(depts: Iterable[DeptRow]) -> list[ColumnCheck]:
    """盘点结果里所有找到的栏目（同一网址只留一个：合并部门的同一栏目不重复）。"""
    out: list[ColumnCheck] = []
    seen: set[str] = set()
    for d in depts:
        if d.status != "已找到":
            continue
        for c in d.columns:
            if c.url in seen:
                continue
            seen.add(c.url)
            out.append(
                ColumnCheck(
                    d.code,
                    d.level,
                    d.province,
                    d.city,
                    d.district,
                    d.dept,
                    d.dept_full_name,
                    c.kind,
                    c.name,
                    c.url,
                    d.site_url,
                    d.nav_source,
                    c.source,
                    d.platform,
                )
            )
    return out


def check_one(cache: PageCache, col: ColumnCheck, today: date) -> ColumnCheck:
    if f"#{XXGK_TAG}gkmlpt-" in col.url:
        # 数字广东信息公开平台的分类（deptdir.py）：列表由 /gkmlpt/api/all/<编号> 接口按分类加载
        col.result = NEEDS_SCRIPT
        col.reason = "数字广东信息公开平台的分类，列表由接口按分类加载（待写广东集约化适配器）"
        col.platform = col.platform or "广东集约化"
        return col
    if f"#{XXGK_TAG}" in col.url:
        # 信息公开分类树上的分类：页面已读过，列表要按分类由脚本加载（大汉信息公开接口，第二阶段写）
        col.result = NEEDS_SCRIPT
        col.reason = "信息公开分类树上的分类，列表由脚本按分类加载（待写大汉信息公开适配器）"
        col.platform = col.platform or "大汉"
        return col
    r = cache.get(col.url)
    col.http_status = r.http_status
    block = blocker(r.status, r.http_status, r.text)
    if block:
        col.result, col.reason = BLOCKED, block
        return col
    if r.status != "ok":
        col.result, col.reason = DOWN, (r.error or r.status)[:120]
        return col
    final = r.final_url or col.url
    items = extract_items(r.text, final, today=today)
    if len(items) < 3:
        # 和正式抓取一样：页面声明了集约化平台的列表接口就按它读（crawler/adapters.py）
        api = find_list_api(r.text, final)
        if api is not None:
            ar = cache.get(api.url)
            html = api_html(api, ar.text) if ar.status == "ok" else ""
            via = extract_items(html, api.base_url, today=today) if html else []
            if len(via) > len(items):
                items = via
                col.reason = f"经{api.adapter}列表接口读取"
    dated = [i for i in items if i.publish_date]
    col.items, col.dated = len(items), len(dated)
    col.latest = max((i.publish_date for i in dated), default=None)  # type: ignore[type-var]
    col.samples = [i.title[:40] for i in items[:3]]
    doc = parse_html(r.text)
    links = page_links(r.text, final, doc)
    col.platform = fingerprint(r.text, final, doc, [link.url for link in links]).platform
    if len(dated) >= 3:
        col.result = READABLE
    elif _SCRIPT_LIST.search(r.text) or len(re.sub(r"<[^>]+>", "", r.text)) < 1500:
        col.result, col.reason = NEEDS_SCRIPT, "页面上没有带日期的条目，列表像是脚本加载的"
    else:
        col.result, col.reason = NO_ITEMS, f"识别出 {len(items)} 条、带日期 {len(dated)} 条"
    return col


def check_columns(
    cache: PageCache, cols: list[ColumnCheck], workers: int = 48, today: date | None = None
) -> list[ColumnCheck]:
    """按主机分组：同一主机的栏目排在一个任务里依次读，不同主机并行。"""
    today = today or date.today()
    groups: dict[str, list[ColumnCheck]] = defaultdict(list)
    for col in cols:
        groups[host_of(col.url)].append(col)

    def run(group: list[ColumnCheck]) -> list[ColumnCheck]:
        out = []
        for col in group:
            try:
                out.append(check_one(cache, col, today))
            except Exception as exc:  # noqa: BLE001 - 一个栏目出意外不让整批白跑
                col.result, col.reason = DOWN, f"试读出错：{exc.__class__.__name__}"
                out.append(col)
        return out

    out: list[ColumnCheck] = []
    with ThreadPoolExecutor(workers, thread_name_prefix="coverage-verify") as ex:
        for done in ex.map(run, groups.values()):
            out.extend(done)
    return out
