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
    candidates: list[tuple[int, ListApi]] = []
    for m in _HANWEB_SCRIPT.finditer(html or ""):
        attrs = _attrs(m.group(0))
        path = attrs.get("url", "")
        if urlsplit(path).path != HANWEB_JPAAS_PATH:
            continue
        try:
            query = ast.literal_eval(attrs.get("querydata", ""))
        except (ValueError, SyntaxError):
            continue
        if not isinstance(query, dict) or not all(isinstance(v, str) for v in query.values()):
            continue
        tag = query.get("tagId", "")
        # 一页上常有几个占位（导航、侧栏）：当前栏目的列表优先
        rank = 0 if "list" in tag.lower() or "列表" in tag else 1
        if query.get("pageType") != "column":
            rank += 2
        url = urljoin(page_url, HANWEB_JPAAS_PATH) + "?" + urlencode(query)
        candidates.append((rank, ListApi("大汉jpaas", url, page_url)))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    api = candidates[0][1]
    # 只认同一网站上的接口
    if urlsplit(api.url).netloc.lower() != urlsplit(page_url).netloc.lower():
        return None
    return api


def api_html(api: ListApi, text: str) -> str:
    """接口返回的内容里列表那段 HTML；不是预期格式返回空串。"""
    if api.adapter == "大汉jpaas":
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        html = (data.get("data") or {}).get("html") if isinstance(data, dict) else None
        return html if isinstance(html, str) else ""
    return ""
