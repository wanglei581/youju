"""地市部门栏目补充规则（地市第 2 批，D-126）：通用的 find_columns 认不出的两种地市部门频道写法。

只在部门自己的目录下找，不猜网址，只跟页面上已有的链接；门户自己的栏目（exclude）照旧不要。
只用于市级的部门（inventory._fill_from_page 里通用的"沿信息公开再找一层"之前接入：部门自己的页
先补，门户公共页只补还缺的），区县级照旧；只补还缺的栏目类型，通用规则在部门页上找到的不动。

1. 部门频道挂在门户下、栏目层级深（江门 /bmpd/jmskxjsj/zwdt/tzgg/index.html 有 5 层，通用规则
   只收 4 层以内）：部门目录下、同一主机的栏目链接放宽到 8 层；文章、附件网址照旧不要；大汉分组
   还原的栏目不在这里认（通用规则里另有栏目名核对）。
2. 部门自己的"信息公开"页（同一主机、网址在部门目录下）优先于门户顶上公共的"政务公开"：通用
   规则只跟页面上第一个信息公开链接，常常跟到门户的公共页去了。打开后跳到部门目录外的不用；
   那一页上只在部门目录下的普通链接里找（独立网站是同一主机的）；大汉分组还原的链接、信息
   公开分类树不在这里认，照旧由通用规则处理。

部门目录要确实归这个部门：挂在门户上的部门页（门户频道）得是官方导航上写着本部门名称的链接
指过来的，而且是它所在目录的首页（/x/、/x/index.html），不带参数；是公共目录里的一篇
（/zwgk/kjj.html）或靠参数区分（?dept=…）时，这两条规则都不用。
"""

import re
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlsplit

from youju.crawler.urls import page_dir
from youju.govsites.names import COLUMN_KINDS, column_kind, dept_categories
from youju.govsites.platform import URL_SHAPES

_MAX_DEPTH = 8
_INFO = re.compile(
    r"^(?:部门|政府)?信息公开(?:目录|专栏|平台)?$|^法定主动公开内容$|^主动公开$|^政务公开$"
)
_FILE = re.compile(r"\.(?:pdf|docx?|xlsx?|wps|ofd|zip|rar|jpe?g|png)$", re.I)
_INDEX = re.compile(r"/(?:index|default|main)\.[a-z]+$", re.I)
_GROUP = "jpaas分组"  # 大汉分组还原的栏目（links.Link.via）


def _article(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path or "/"
    target = path + ("?" + parts.query if parts.query else "")
    # 文章网址后面带参数（…/art_1_2.html?from=x）时只看路径也要认得出
    shapes = any(p.search(path) or p.search(target) for _, p in URL_SHAPES)
    return shapes or bool(_FILE.search(path))


def _depth(url: str) -> int:
    return len([s for s in (urlsplit(url).path or "/").split("/") if s])


def dir_page(url: str) -> bool:
    """这一页是它所在目录的首页（/x/、/x、/x/index.html），不带参数：这个目录归这一页。"""
    parts = urlsplit(url)
    path = parts.path or "/"
    last = path.rsplit("/", 1)[-1]
    return not parts.query and (path.endswith("/") or "." not in last or bool(_INDEX.search(path)))


def named_on_nav(row) -> bool:
    """官方导航链接的文字（不是事后补的部门全称）写着本部门名称。"""
    return bool(row.nav_text) and row.dept in dept_categories(row.nav_text)


def deep_columns(page, exclude: frozenset[str], hit_cls) -> dict:
    """这一页目录下、同一主机、8 层以内的栏目链接（规则 1）：{类型: 栏目}。"""
    base = page.final_url
    pdir = page_dir(base)
    host = urlsplit(base).netloc.lower()
    if pdir == "/":
        return {}
    best: dict[str, tuple[tuple[int, int], object]] = {}
    for order, link in enumerate(page.links):
        if link.via == _GROUP:
            continue
        hit = column_kind(link.text)
        if hit is None or link.url in exclude:
            continue
        parts = urlsplit(link.url)
        if parts.netloc.lower() != host or not parts.path.startswith(pdir):
            continue
        if _article(link.url) or _depth(link.url) > _MAX_DEPTH:
            continue
        kind, strength = hit
        key = (strength, order)
        if kind not in best or key < best[kind][0]:
            best[kind] = (key, hit_cls(kind, link.text, link.url, base))
    return {kind: hit for kind, (_, hit) in best.items()}


def own_info_page(page, prefix: str) -> str | None:
    """同一主机、网址在部门目录（prefix）下的信息公开链接（规则 2）。"""
    host = urlsplit(page.final_url).netloc.lower()
    if prefix == "/":
        return None
    for link in page.links:
        parts = urlsplit(link.url)
        if (
            _INFO.search(link.text)
            and parts.netloc.lower() == host
            and (parts.path or "/").startswith(prefix)
        ):
            return link.url
    return None


def more_columns(
    row,
    page,
    hits: dict,
    exclude: frozenset[str],
    fetch: Callable,
    find: Callable,
    hit_cls,
) -> dict:
    """在通用规则的结果 hits 上补栏目（只补缺的类型），返回合并后的结果。

    fetch(url) -> PageInfo（Inventory.page）；find(pages, exclude) -> {类型: 栏目}（通用规则，
    带专题路径和大汉分组核对）；hit_cls 是 ColumnHit（不在这里引 inventory，免得循环引用）。
    政府办看门户本身，不走这里。"""
    if row.dept == "政府办" or page is None or all(k in hits for k in COLUMN_KINDS):
        return hits
    base = page.final_url
    shared = row.site_type == "门户频道"  # 部门挂在门户主机上：目录要确实归这个部门
    if shared and not (named_on_nav(row) and dir_page(base)):
        return hits
    host = urlsplit(base).netloc.lower()
    prefix = page_dir(base)
    out = dict(hits)
    for kind, hit in deep_columns(page, exclude, hit_cls).items():
        out.setdefault(kind, hit)
    if all(k in out for k in COLUMN_KINDS):
        return out
    info_url = own_info_page(page, prefix)
    if not info_url:
        return out
    info = fetch(info_url)
    if info.result.status != "ok" or info.block:
        return out
    final = urlsplit(info.final_url or info_url)
    if final.netloc.lower() != host or not (final.path or "/").startswith(prefix):
        return out  # 跳到了部门目录外：不是部门自己的信息公开页

    def own(url: str) -> bool:
        parts = urlsplit(url)
        return parts.netloc.lower() == host and (not shared or parts.path.startswith(prefix))

    # 先筛候选再择优：通用规则只看这一页上部门目录下的链接（含"更多"链接），目录外的不参与比较
    from youju.govsites.inventory import heading_more_links  # 延迟引入：inventory 引着本模块

    cand = [link for link in info.links if own(link.url) and link.via != _GROUP]
    if info.doc is not None:
        cand += [link for link in heading_more_links(info.doc, info.final_url) if own(link.url)]
    view = replace(info, links=cand, doc=None, categories=[])
    found = {k: h for k, h in find([view], exclude).items() if own(h.url)}
    for kind, hit in {**deep_columns(info, exclude, hit_cls), **found}.items():
        out.setdefault(kind, hit)
    return out
