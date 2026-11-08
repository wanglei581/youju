"""平台适配器：栏目页上读不出条目，但页面自己写明了列表接口时，按页面给的参数取列表。

政府网站集约化平台把列表交给浏览器脚本去取，页面里只留一个"占位 + 接口地址 + 参数"。同一个平台
的几百个网站写法完全一样，认出一种写法就能读一个平台（全国覆盖第一阶段，见
docs/数据来源/全国覆盖/集约化平台盘点.md）。

规矩和抓页面一样：
- 只用页面自己声明的、同一网站上的公开接口，只发 GET；照常遵守 robots.txt、同一网站间隔 3 秒；
- 只认白名单里的接口路径，不拼别的地址；要登录、签名、验证码的接口一律不碰。

现有适配器：
- 大汉 jpaas（浙江等省的集约化平台）：<script src=".../unitbuild.js"
  url="/api-gateway/jpaas-publish-server/front/page/build/unit"
  queryData="{'pageId':'…','tagId':'当前栏目list',…}">，GET 同样的参数，返回 JSON，
  data.html 是列表的 HTML。
"""

import ast
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

_HANWEB_SCRIPT = re.compile(r"<script\b[^>]*unitbuild\.js[^>]*>", re.I)
_ATTR = re.compile(r"""\b([A-Za-z]+)\s*=\s*("([^"]*)"|'([^']*)')""")
HANWEB_JPAAS_PATH = "/api-gateway/jpaas-publish-server/front/page/build/unit"


@dataclass(frozen=True)
class ListApi:
    adapter: str  # 适配器名
    url: str  # 接口网址（和栏目页同一网站）
    base_url: str  # 接口返回的列表里的相对链接，以栏目页为准


def _attrs(tag: str) -> dict[str, str]:
    return {
        m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4))
        for m in _ATTR.finditer(tag)
    }


def find_list_api(html: str, page_url: str) -> ListApi | None:
    """栏目页里声明的列表接口；认不出返回 None。"""
    apis = find_unit_apis(html, page_url)
    return apis[0] if apis else None


def find_unit_apis(html: str, page_url: str) -> list[ListApi]:
    """页面里声明的全部大汉 jpaas 单元接口（当前栏目的列表排最前，其次导航等其他单元）。"""
    candidates: list[tuple[int, ListApi]] = []
    for m in _HANWEB_SCRIPT.finditer(html or ""):
        attrs = _attrs(m.group(0))
        declared = urlsplit(urljoin(page_url, attrs.get("url", "")))
        # 只认白名单路径，而且接口必须就在栏目页这个网站上（页面声明到别的主机的不请求）
        if declared.path != HANWEB_JPAAS_PATH:
            continue
        if declared.netloc.lower() != urlsplit(page_url).netloc.lower():
            continue
        try:
            query = ast.literal_eval(attrs.get("querydata", ""))
        except Exception:  # noqa: BLE001 - 页面写的参数什么样都有，解析不了就不用
            continue
        if not isinstance(query, dict) or not all(
            isinstance(v, (str, int)) and not isinstance(v, bool) for v in query.values()
        ):
            continue
        query = {str(k): str(v) for k, v in query.items()}
        tag = query.get("tagId", "")
        # 一页上常有几个占位（导航、侧栏）：当前栏目的列表优先
        rank = 0 if "list" in tag.lower() or "列表" in tag else 1
        if query.get("pageType") != "column":
            rank += 2
        url = urljoin(page_url, HANWEB_JPAAS_PATH) + "?" + urlencode(query)
        candidates.append((rank, ListApi("大汉jpaas", url, page_url)))
    candidates.sort(key=lambda c: c[0])
    out: list[ListApi] = []
    for _, api in candidates:
        if api not in out:  # 同一接口地址只要一次
            out.append(api)
    return out


def api_html(api: ListApi, text: str) -> str:
    """接口返回的内容里列表那段 HTML；不是预期格式返回空串。"""
    if api.adapter == "大汉jpaas":
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        inner = data.get("data") if isinstance(data, dict) else None
        html = inner.get("html") if isinstance(inner, dict) else None
        return html if isinstance(html, str) else ""
    return ""


_TREE = re.compile(r"""treeStr\s*=\s*(["'])(\[.*?\])\1\s*;""", re.S)


def jpaas_tree_categories(html: str) -> list[tuple[str, str]]:
    """大汉 jpaas 信息公开"组配分类"树里的分类（名称, 分类键）。

    县级部门的通知公告、政策文件常只是这棵树上的一个分类，列表点分类后由脚本按键加载；
    盘点据此知道"栏目在，但要接口"，不会误判成网站没有这个栏目。"""
    out: list[tuple[str, str]] = []
    for m in _TREE.finditer(html or ""):
        raw = m.group(2).replace("/#/", "\x00").replace("#", '"').replace("\x00", "/#/")
        try:
            nodes = json.loads(raw)
        except ValueError:
            continue
        stack = list(nodes) if isinstance(nodes, list) else []
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            title, key = node.get("title"), node.get("key")
            if isinstance(title, str) and isinstance(key, str):
                out.append((title.strip(), key))
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(children)
    return out


_GROUP = re.compile(
    r"<label[^>]*class=[\"']?label-className[\"']?[^>]*>\s*([^<]{2,20}?)\s*</label>(.*?)(?=<label[^>]*label-className|$)",
    re.S,
)
_COL_ID = re.compile(r"/col/col(\d+)/|/art_(\d+)_\d+\.html")


def jpaas_xxgk_groups(html: str, page_url: str) -> list[tuple[str, str]]:
    """大汉 jpaas 信息公开"分组列表"单元里的分组（名称, 分组栏目页）。

    单元 HTML 是一组组"<label class=label-className>政策法规</label> + 条目列表"，条目网址里带着
    这个分组的栏目号（/col/col栏目号/… 或 art_栏目号_文章号.html），据此还原分组栏目页
    /col/col栏目号/index.html（同一网站）。"""
    out: list[tuple[str, str]] = []
    host = urlsplit(page_url).netloc.lower()
    for m in _GROUP.finditer(html or ""):
        name, block = m.group(1).strip(), m.group(2)
        ids: set[str] = set()
        ok = True
        for href in re.findall(r"""href=["']([^"']+)["']""", block):
            full = urljoin(page_url, href)
            col = _COL_ID.search(full)
            if not col:
                continue
            if urlsplit(full).netloc.lower() != host:
                ok = False  # 条目在别的网站上：还原不出本站的分组栏目
                break
            ids.add(col.group(1) or col.group(2))
        # 组里的条目都来自同一个栏目才用；混着几个子栏目（如"政策文件"组里有"政策解读"）不猜
        if ok and len(ids) == 1:
            out.append((name, urljoin(page_url, f"/col/col{ids.pop()}/index.html")))
    return out
