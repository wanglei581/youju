"""部门目录：从区县门户的信息公开平台里找出"本县有哪些部门、各自的栏目"。

集约化以后县直部门多半没有独立网站，部门列表藏在门户"政务公开→部门信息公开"一类的页面里
（第二阶段计划第五节第 3 条）。这里分两步：

1. 找目录页（各平台通用）：从门户首页和已走过的导航页出发，沿"部门信息公开""组织机构""信息公开
   部门目录"这类链接找；"政务公开""政府信息公开"这类总入口再进一层。页面上链到本网站、像机关名
   的链接够多（至少 6 个、其中至少 2 个是目标部门）才算目录页。目录页交给盘点当导航页用，部门
   照常按名称认、按归属规则核对（attribution.py）。
2. 读部门页上的栏目（按平台）：
   - 大汉 jpaas 信息公开（浙江等）：部门页的"组配分类"单元按分类分组列条目（"政策文件"等），条目网址
     带着分类自己的目录（…/fdzdgknr/zcwj/xzbmfgfxwj/art/…），还原成分类栏目页
     …/fdzdgknr/zcwj/xzbmfgfxwj/index.html（这页的列表单元现有大汉 jpaas 适配器能读）；
   - 数字广东（广东省市县的 gkmlpt 信息公开平台）：部门页里写着分类树 TREE（编号、名称），
     列表由 /gkmlpt/api/all/<编号> 接口按分类加载。记成"部门页#xxgk-gkmlpt-编号"，试读记"需接口"；
   - 山西等拓尔思县级平台：部门页先跳到"公开指南"，侧栏是"法定主动公开内容"下的栏目，
     沿用盘点现有的栏目识别（不另写规则）；部门页网址比别处深，目录页上的链接放宽深度。

只读公开页面，抓取沿用盘点的页面缓存（遵守 robots.txt、同一网站间隔 3 秒、透明标识），
要登录、验证码、防护页的照记"不能抓"，不绕过。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from youju.govsites.links import REPORT_WORDS, Link, registrable
from youju.govsites.names import dept_categories, looks_like_org
from youju.govsites.platform import URL_SHAPES

if TYPE_CHECKING:
    from youju.govsites.divisions import Division
    from youju.govsites.inventory import Inventory, PageInfo

# 直接像部门目录的入口
_DIR_LINK = re.compile(
    r"部门.{0,6}(?:信息)?公开|信息公开.{0,4}部门|部门目录|部门导航|政府部门|组织机构|机构设置|"
    r"机构职能|政府机构|部门街道|区级部门|县级部门|市级部门"
)
# 总入口：自己多半不是目录，目录在下一层
_HUB_LINK = re.compile(
    r"^(?:政务公开|政府信息公开|信息公开|政府信息公开目录|信息公开目录|政务公开目录|"
    r"政府信息公开平台|政务公开平台|信息公开平台|政府信息公开专栏|政务公开专栏|"
    r"法定主动公开内容|主动公开)$"
)
_TRAIL = re.compile(r"[\s>»›＞〉》+＋]+$|^[\s>»›·•]+")
_ORG_TAIL = re.compile(r"(局|委|委员会|办|办公室|中心|分局|管委会|管理委员会)$")
DIR_MIN_ORGS = 6  # 目录页上至少这么多个像机关名的本站链接
DIR_MIN_TARGETS = 2  # 其中至少这么多个是目标部门
DIR_NAV = "部门目录"  # 目录页的导航文字前缀（含"部门"，部门候选照常加导航分）
# 年报一类的页面：部门链到自己的年报，不是部门目录（年报目录的"首页网址"由盘点原有规则读）
_REPORTISH_URL = re.compile(r"ndbb|zfxxgknb|xxgknb|gzndbb|nianbao|/nb/|/bmnb/", re.I)
_REPORTISH_TEXT = re.compile(r"年报|年度报告|年度报表|工作报表")


def _clean(text: str) -> str:
    return _TRAIL.sub("", re.sub(r"\s+", "", text or ""))


def directory_score(page: PageInfo, home_url: str) -> tuple[int, int]:
    """页面上链到本网站、像机关名的链接数（按网址去重）和其中目标部门的个数。"""
    home = registrable(home_url)
    orgs: set[str] = set()
    targets: set[str] = set()
    for link in page.links:
        if registrable(link.url) != home or _article(link.url) or _reportish(link):
            continue  # 别家网站的、链到文章的（柯城区"公开指南"页上各部门链到自己的指南文章）、
            # 链到年报的不算
        text = re.sub(r"^[\s>》›»·•|｜\-—–:：、]+", "", link.text or "").strip()
        # 去掉括号注释，以及显示时被截断的尾巴（"……局（长兴县人民政府区域合..."）
        bare = re.sub(r"[（(][^）)]*[）)]", "", text)
        bare = re.sub(r"[（(][^）)]*$|(?:\.{2,}|…+)$", "", bare).strip()
        if not looks_like_org(bare) or not _ORG_TAIL.search(bare):
            continue
        orgs.add(link.url)
        if [c for c in dept_categories(bare) if c != "政府办"]:
            targets.add(link.url)
    return len(orgs), len(targets)


def _reportish(link: Link) -> bool:
    path = urlsplit(link.url).path
    return bool(_REPORTISH_URL.search(path) or _REPORTISH_TEXT.search(link.text or ""))


def _article(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    return any(pattern.search(path) for _, pattern in URL_SHAPES)


def is_directory(page: PageInfo, home_url: str) -> bool:
    if page.result.status != "ok" or page.block:
        return False
    orgs, targets = directory_score(page, home_url)
    return orgs >= DIR_MIN_ORGS and targets >= DIR_MIN_TARGETS


def _rank(text: str) -> int:
    t = _clean(text)
    if _DIR_LINK.search(t) and len(t) <= 16:
        return 0
    if _HUB_LINK.match(t):
        return 1
    return 9


def find_directories(
    inv: Inventory, div: Division, home: PageInfo, pages: list[PageInfo], budget: int = 8
) -> list[PageInfo]:
    """门户信息公开平台里的部门目录页。最多再抓 budget 个页面。

    已走过的导航页里有目录页时只把它们标上 directory（不再找），返回空列表；
    返回的只是这里新抓到的目录页，调用方追加到导航页后面。"""
    home_url = home.final_url
    site = registrable(home_url)
    found: list[PageInfo] = []
    seen = {p.final_url for p in pages} | {p.url for p in pages}
    for p in pages:
        # 首页、年报目录不当部门目录（年报目录上的部门链接由盘点原有规则读"首页网址"）
        if p is home or p.final_url.rstrip("/") == home_url.rstrip("/"):
            continue
        if _REPORTISH_TEXT.search(p.nav_text):
            continue
        if REPORT_WORDS.search(p.nav_text):
            continue
        if p.directory or is_directory(p, home_url):
            p.directory = True
            found.append(p)
    if found:
        return []  # 已有的导航页就是目录页：只标记，不再找

    def candidates(src: list[PageInfo]) -> list[tuple[int, Link]]:
        out: list[tuple[int, Link]] = []
        for p in src:
            for link in p.links:
                rank = _rank(link.text)
                if rank > 1 or link.url in seen or registrable(link.url) != site:
                    continue
                out.append((rank, link))
        out.sort(key=lambda x: x[0])
        uniq: list[tuple[int, Link]] = []
        urls: set[str] = set()
        for rank, link in out:
            if link.url not in urls:
                urls.add(link.url)
                uniq.append((rank, link))
        return uniq

    # 直接像目录的入口最多 3 个，总入口另留 2 个（别让一串"机构简介"挤掉"政务公开"）
    first = candidates(pages)
    level1 = [c for c in first if c[0] == 0][:3] + [c for c in first if c[0] == 1][:2]
    hubs: list[PageInfo] = []
    for rank, link in level1:
        if budget <= 0 or (found and rank == 1):
            break  # 直接入口已经找到目录，总入口不再抓
        budget -= 1
        seen.add(link.url)
        p = inv.page(link.url)
        if p.result.status != "ok" or p.block or _not_directory_page(p, link, home_url):
            continue
        p.nav_text = f"{DIR_NAV}：{_clean(link.text)}"
        if is_directory(p, home_url):
            p.directory = True
            found.append(p)
        else:
            hubs.append(p)
    if found:
        return found
    # 总入口页（"政务公开"）里再找一层目录入口
    for _, link in [c for c in candidates(hubs) if c[0] == 0][:4]:
        if budget <= 0:
            break
        budget -= 1
        seen.add(link.url)
        p = inv.page(link.url)
        if p.result.status != "ok" or p.block or _not_directory_page(p, link, home_url):
            continue
        p.nav_text = f"{DIR_NAV}：{_clean(link.text)}"
        if is_directory(p, home_url):
            p.directory = True
            found.append(p)
    return found


def _not_directory_page(p: PageInfo, link: Link, home_url: str) -> bool:
    """抓来的页面是门户首页（"机构设置"跳回了首页）或年报一类的，不当部门目录。"""
    if p.final_url.rstrip("/") == home_url.rstrip("/"):
        return True
    return _reportish(link) or bool(_REPORTISH_URL.search(urlsplit(p.final_url).path))


# —— 大汉 jpaas 信息公开：分组条目 → 分类栏目页 ——

_GROUP = re.compile(
    r"<label[^>]*class=[\"']?[^\"'>]*\blabel-className\b[^>]*>\s*([^<]{2,20}?)\s*</label>"
    r"(.*?)(?=<label[^>]*label-className|$)",
    re.S,
)
_HREF = re.compile(r"""href=["']([^"']+)["']""")


_ART_ID = re.compile(r"/art_(\d+)_\d+\.html?$")
GROUP_MAJORITY = 0.6  # 组里至少这么多条目来自同一个分类栏目，才认它是这一组的栏目


def jpaas_path_groups(html: str, page_url: str) -> list[tuple[str, str]]:
    """大汉 jpaas"组配分类"单元里的分组（名称, 分类栏目页）。

    条目网址是 …/分类目录/art/年/art_编号.html 的写法（长兴等县），还原成 …/分类目录/index.html；
    …/art/年/月/日/art_栏目号_文章号.html 的按栏目号还原成 /col/col栏目号/index.html（同一网站）。
    组里的条目常混着别的栏目（拱墅区各部门的"政策文件"组里夹着共用的年报栏目），只有一个栏目占到
    六成以上才用它；拿不准的不还原（盘点照旧记信息公开分类，试读记"需接口"）。"""
    out: list[tuple[str, str]] = []
    host = urlsplit(page_url).netloc.lower()
    for m in _GROUP.finditer(html or ""):
        name, block = m.group(1).strip(), m.group(2)
        dirs: Counter[str] = Counter()
        for href in _HREF.findall(block):
            parts = urlsplit(urljoin(page_url, href))
            if parts.netloc.lower() != host or "/art/" not in parts.path:
                continue
            base = parts.path.split("/art/", 1)[0]
            art = _ART_ID.search(parts.path)
            if base and base != "/":
                dirs[f"{parts.scheme}://{parts.netloc}{base}"] += 1
            elif art:
                dirs[f"{parts.scheme}://{parts.netloc}/col/col{art.group(1)}"] += 1
        if not dirs:
            continue
        best, count = dirs.most_common(1)[0]
        if count / sum(dirs.values()) >= GROUP_MAJORITY:
            out.append((name, best + "/index.html"))
    return out


# —— 数字广东 gkmlpt：部门页里的分类树 ——

_GKML_TREE = re.compile(r"\bTREE\s*:\s*(?=\[)")
GKMLPT_KEY = "gkmlpt-"


def gkmlpt_tree_categories(text: str) -> list[tuple[str, str]]:
    """数字广东信息公开平台部门页里的分类（名称, "gkmlpt-编号"）；跳到别的网站的分类不要。"""
    m = _GKML_TREE.search(text or "")
    if not m:
        return []
    try:
        # 从"["起按 JSON 读一个完整的值：分类树带子分类、换不换行都不会截错
        nodes, _ = json.JSONDecoder().raw_decode(text, m.end())
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    stack = list(nodes) if isinstance(nodes, list) else []
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        name, nid = node.get("name"), node.get("id")
        if isinstance(name, str) and isinstance(nid, int) and not node.get("jump_url"):
            out.append((name.strip(), f"{GKMLPT_KEY}{nid}"))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return out
