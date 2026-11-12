"""地市部门栏目补充规则（地市第 2 批，D-126）：通用的 find_columns 认不出的几种部门频道写法。

只在部门页本身、部门目录下找，不猜网址；门户自己的栏目（exclude）照旧不要。只用于市级的部门
（inventory._fill_from_page 里"沿信息公开再找一层"之后接入），区县级照旧。

1. 部门频道挂在门户下面时栏目层级深（江门 /bmpd/jmskxjsj/zwdt/tzgg/index.html 有 5 层，
   通用规则只收 4 层以内）：部门目录下、同一主机的栏目链接放宽到 8 层，文章网址照旧不要。
2. 认到的部门页是频道里的一个子栏目（韶关 /bmpdlm/fgw/dt/ 是"动态信息"）：退到链接文字
   写着本部门名称、网址是它上级目录的那一页再找。
3. 部门自己的"信息公开"页（网址在部门目录下）优先于门户顶上的公共"政务公开"：通用规则
   只跟页面上第一个信息公开链接，常常跟到门户的公共页去了。
"""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from youju.crawler.urls import page_dir
from youju.govsites.names import COLUMN_KINDS, column_kind, dept_categories
from youju.govsites.platform import URL_SHAPES

_MAX_DEPTH = 8
_INFO = re.compile(
    r"^(?:部门|政府)?信息公开(?:目录|专栏|平台)?$|^法定主动公开内容$|^主动公开$|^政务公开$"
)
_FILE = re.compile(r"\.(?:pdf|docx?|xlsx?|wps|ofd|zip|rar|jpe?g|png)$", re.I)


def _article(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path or "/"
    target = path + ("?" + parts.query if parts.query else "")
    return any(p.search(target) for _, p in URL_SHAPES) or bool(_FILE.search(path))


def _depth(url: str) -> int:
    return len([s for s in (urlsplit(url).path or "/").split("/") if s])


def deep_columns(page, exclude: frozenset[str], hit_cls) -> dict:
    """部门目录下、同一主机、8 层以内的栏目链接（规则 1）：{类型: 栏目}。"""
    base = page.final_url
    pdir = page_dir(base)
    host = urlsplit(base).netloc.lower()
    if pdir == "/":
        return {}
    best: dict[str, tuple[tuple[int, int], object]] = {}
    for order, link in enumerate(page.links):
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


def channel_root(page, dept: str) -> str | None:
    """链接文字是本部门名称、网址是当前页上级目录的那一页（规则 2）；没有返回 None。"""
    base = urlsplit(page.final_url)
    for link in page.links:
        parts = urlsplit(link.url)
        if parts.netloc.lower() != base.netloc.lower():
            continue
        root = parts.path or "/"
        if root == "/" or not root.endswith("/") or root == page_dir(page.final_url):
            continue
        if not base.path.startswith(root):
            continue
        if dept in dept_categories(link.text):
            return link.url
    return None


def own_info_page(page, prefix: str) -> str | None:
    """网址在部门目录（prefix）下的信息公开链接（规则 3）。"""
    for link in page.links:
        parts = urlsplit(link.url)
        if _INFO.search(link.text) and prefix != "/" and parts.path.startswith(prefix):
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

    fetch(url) -> PageInfo（Inventory.page）；find(pages, exclude) -> {类型: 栏目}（find_columns）；
    hit_cls 是 ColumnHit（不在这里引 inventory，免得循环引用）。政府办看门户本身，不走这里。"""
    if row.dept == "政府办" or all(k in hits for k in COLUMN_KINDS) or page is None:
        return hits
    out = dict(hits)
    for kind, hit in deep_columns(page, exclude, hit_cls).items():
        out.setdefault(kind, hit)
    pages = [page]
    root_url = channel_root(page, row.dept)
    if root_url and not all(k in out for k in COLUMN_KINDS):
        root = fetch(root_url)
        if root.result.status == "ok" and not root.block:
            pages.append(root)
            found = {**deep_columns(root, exclude, hit_cls), **find([root], exclude)}
            for kind, hit in found.items():
                out.setdefault(kind, hit)
    if not all(k in out for k in COLUMN_KINDS):
        prefix = page_dir(root_url) if root_url else page_dir(page.final_url)
        for p in pages:
            info_url = own_info_page(p, prefix)
            if not info_url:
                continue
            info = fetch(info_url)
            if info.result.status == "ok" and not info.block:
                found = {**deep_columns(info, exclude, hit_cls), **find([info], exclude)}
                for kind, hit in found.items():
                    out.setdefault(kind, hit)
            break
    return out
