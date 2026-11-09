"""从门户网站页面里取出导航链接：<a>、下拉框 <option>、点击事件里的网址、脚本里写死的网址。

政府门户的"市县网站""部门网站"导航有几种常见写法：
- 页脚一组 <a>（常先隐藏，点"市县政府网站"再展开，链接仍在 HTML 里）；
- <select><option value="网址">名称</option></select> 下拉框；
- <a href="javascript:;" onclick="window.open('网址')">；
- 单独一个导航页（"网站导航""市县政府网站""省直单位网站"）；
- 页脚由脚本文件拼出来（document.write 或 JSON 数组）。
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import lxml.html
from lxml import etree

from youju.crawler.urls import normalize_url

_SPACES = re.compile(r"\s+")
_ONCLICK_URL = re.compile(r"""(?:open|location(?:\.href)?\s*=|href\s*=)\s*\(?\s*['"]([^'"]+)['"]""")
# 脚本里的"名称 + 网址"：{name:"杭州市",url:"http://…"}、["杭州市","http://…"]，
# 或 document.write 里的 <a href=…>名称</a>
_JS_PAIR = re.compile(
    r"""['"]([一-龥（）()]{2,30})['"]\s*[,:]\s*(?:url\s*:\s*)?['"](https?://[^'"\s]{6,200})['"]"""
    r"""|['"](https?://[^'"\s]{6,200})['"]\s*[,:]\s*(?:name\s*:\s*)?['"]([一-龥（）()]{2,30})['"]""",
    re.I,
)
_JS_ANCHOR = re.compile(
    r"""<a[^>]+href\s*=\s*\\?['"](https?://[^'"\\\s]{6,200})\\?['"][^>]*>([^<]{2,40})</a>""", re.I
)
# 门户页面上指向"导航页"的链接文字：沿着它们再找一层
NAV_WORDS = (
    "网站导航",
    "政府网站",
    "部门网站",
    "市县",
    "市州",
    "地级",
    "设区市",
    "盟市",
    "州市",
    "各市",
    "市（州）",
    "地市",
    "区县",
    "县区",
    "县（市",
    "县(市",
    "网站群",
    "相关链接",
    "友情链接",
    "省直",
    "市直",
    "区直",
    "县直",
    "直属单位",
    "政府部门",
    "政府机构",
    "机构设置",
    "组织机构",
    "部门信息公开",
    "部门公开",
    "政府信息公开",
    "信息公开目录",
    "网站地图",
    "年度报表",
    "工作报表",
    "网站报表",
)
_NAV_MAX_LEN = 20


@dataclass(frozen=True)
class Link:
    text: str
    url: str
    via: str  # a / option / onclick / script


def _text(el: etree._Element) -> str:
    return _SPACES.sub("", el.text_content() or "")


# 页面中间多出来的 </html>、</body>（常见于拼进来的页头）：libxml2 读到就不往下解析了，
# 浏览器照常显示后面的内容。解析前去掉
STRAY_END = re.compile(r"</(?:body|html)\s*>", re.I)
# 大汉 JCMS 把列表条目藏在 <script type="text/xml"><datastore><record><![CDATA[…]]></record> 里
_DATASTORE_RECORD = re.compile(r"<record>\s*<!\[CDATA\[(.*?)\]\]>\s*</record>", re.S)


def parse_html(html: str) -> etree._Element | None:
    if not html or not html.strip():
        return None
    records = _DATASTORE_RECORD.findall(html)
    if records:
        html = html + "<div data-youju-datastore='1'>" + "".join(records) + "</div>"
    try:
        return lxml.html.document_fromstring(STRAY_END.sub("", html))
    except (etree.ParserError, ValueError):
        return None


# 跳转包装页：参数里只有链接名称、真正的网址由页面脚本查出来（东莞首页的部门链接都是
# libs.dg.gov.cn/code/xhtml/loading.html?from=<名称>，那个网站 robots 禁止）。
# 看不出指向哪里，不当链接用
_WRAPPED = re.compile(r"/loading\.html?\?(?:[^#]*&)?from=", re.I)


def page_links(html: str, base_url: str, doc: etree._Element | None = None) -> list[Link]:
    """页面上所有带文字的链接（去重，保留第一次出现的写法）。"""
    doc = doc if doc is not None else parse_html(html)
    if doc is None:
        return []
    base = doc.xpath("//base[@href]/@href")
    page = (normalize_url(base_url, base[0]) or base_url) if base else base_url
    out: list[Link] = []
    seen: set[tuple[str, str]] = set()

    def add(text: str, href: str | None, via: str) -> None:
        url = normalize_url(page, href)
        text = (text or "").strip()
        if not url or not text or len(text) > 60 or _WRAPPED.search(url):
            return
        key = (text, url)
        if key not in seen:
            seen.add(key)
            out.append(Link(text, url, via))

    for a in doc.iter("a", "area"):
        text = _text(a) or _SPACES.sub("", a.get("title") or a.get("alt") or "")
        href = a.get("href") or ""
        if href.strip().lower().startswith(("javascript", "#")) or not href.strip():
            for attr in ("onclick", "data-href", "data-url", "url"):
                value = a.get(attr) or ""
                m = _ONCLICK_URL.search(value) if attr == "onclick" else None
                target = m.group(1) if m else (value if attr != "onclick" else "")
                if target:
                    add(text, target, "onclick")
                    break
            continue
        add(text, href, "a")
    for opt in doc.iter("option"):
        value = (opt.get("value") or "").strip()
        if value.lower().startswith(("http", "//", "/")) or ".gov.cn" in value:
            add(_text(opt), value, "option")
    for script in doc.iter("script"):
        body = script.text or ""
        if body:
            for link in script_links(body, page):
                key = (link.text, link.url)
                if key not in seen:
                    seen.add(key)
                    out.append(link)
    return out


def script_links(body: str, base_url: str) -> list[Link]:
    """脚本文字里写死的"名称—网址"对（页脚由脚本拼出时用）。"""
    out: list[Link] = []
    for m in _JS_PAIR.finditer(body):
        name, url = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        full = normalize_url(base_url, url)
        if full:
            out.append(Link(_SPACES.sub("", name), full, "script"))
    for m in _JS_ANCHOR.finditer(body):
        full = normalize_url(base_url, m.group(1))
        if full:
            out.append(Link(_SPACES.sub("", m.group(2)), full, "script"))
    return out


_LIB_SCRIPT = re.compile(
    r"jquery|swiper|layui|vue|echarts|bootstrap|polyfill|require|md5|crypto|baidu|cnzz|hm\.js|gray|aria|"
    r"barrier|wza|jiucuo|conac|share|qrcode|zepto|lazy|scroll|slide|/lib|\.min\.js|analytics|stat",
    re.I,
)
_NAV_SCRIPT = re.compile(
    r"foot|link|nav|yqlj|wzq|bottom|daohang|wzdh|ljgl|friend|/dh/|_dh|site", re.I
)


def footer_scripts(
    doc: etree._Element, base_url: str, extra: int = 0, named_limit: int = 8
) -> list[str]:
    """同一网站上可能拼页脚、导航的脚本文件：路径带 foot、link、nav、wzdh（网站导航）等的先取；
    extra > 0 时再取几个不是常见库的本站脚本（江苏等省的页脚脚本文件名只是一串数字）。"""
    host = urlsplit(base_url).netloc.lower()
    named: list[str] = []
    others: list[str] = []
    for script in doc.iter("script"):
        src = normalize_url(base_url, script.get("src"))
        if not src or urlsplit(src).netloc.lower() != host or _LIB_SCRIPT.search(src):
            continue
        target = named if _NAV_SCRIPT.search(urlsplit(src).path) else others
        if src not in target:
            target.append(src)
    # 网站导航（wzdh、daohang、nav）的先看，其次页脚，最后其他链接组
    named.sort(key=_script_rank)
    return named[:named_limit] + others[:extra]


def _script_rank(src: str) -> int:
    path = urlsplit(src).path.lower()
    if re.search(r"wzdh|daohang|/dh/|_dh|nav", path):
        return 0
    if re.search(r"foot|bottom", path):
        return 1
    return 2


def site_key(url: str) -> str:
    """网站粒度的键：主机名（不含 www. 前缀）。"""
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def registrable(url: str) -> str:
    """政府网站的"本级域名"：xx.gov.cn 取最后三段，其他取最后两段。用来判断两个网址是不是同一家。"""
    host = (urlsplit(url).hostname or "").lower()
    parts = host.split(".")
    if host.endswith(".gov.cn") and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def nav_links(links: list[Link], page_url: str, limit: int = 10) -> list[Link]:
    """值得再进一层的导航页：文字短、带导航词、和当前门户同一家域名。"""
    home = registrable(page_url)
    out: list[Link] = []
    seen: set[str] = set()
    for link in links:
        if len(link.text) > _NAV_MAX_LEN or link.url in seen:
            continue
        if not any(_word_in(w, link.text) for w in NAV_WORDS):
            continue
        if registrable(link.url) != home:
            continue
        seen.add(link.url)
        out.append(link)
    # 越像"市县/部门网站导航"的越先走
    out.sort(key=lambda link: _nav_rank(link.text))
    return out[:limit]


# 年度报表目录里分组的链接（"省级部门""设区市""县（市、区）"）
REPORT_GROUP = re.compile(
    r"^(?:省|市|县|区)?(?:级|直)?(?:部门|单位|政府|门户)|设区市|市级|县级|县（市|县\(市|区县|市县|地市|市州|部门网站|门户网站"
)


REPORT_WORDS = re.compile(r"年度报表|工作报表|网站报表")


# 别处的政府网站（国务院部门、其他省份、驻港澳机构）：本地门户上的这几个导航页排最后
_ELSEWHERE = ("国务院部门", "地方政府网站", "驻港澳", "驻外", "各省", "兄弟")
_NAV_RANKS = (
    (
        "市县",
        "市州",
        "市（州）",
        "地级",
        "设区市",
        "盟市",
        "州市",
        "各市",
        "区县",
        "县区",
        "县（市",
    ),
    ("部门网站", "省直", "市直", "区直", "县直", "网站导航"),
    ("年度报表", "工作报表", "网站报表"),
    ("政府网站", "政府部门", "政府机构", "机构设置", "组织机构", "网站群"),
    ("部门信息公开", "部门公开", "信息公开目录", "政府信息公开"),
    ("相关链接", "友情链接", "网站地图"),
)


def _nav_rank(text: str) -> int:
    if any(w in text for w in _ELSEWHERE):
        return 20
    for rank, words in enumerate(_NAV_RANKS):
        if any(_word_in(w, text) for w in words):
            return rank
    return 9


# "州市"（云南等省的"各州市"）也是"杭州市""福州市"这类地名的一部分：前面紧挨着别的汉字的不算
# 导航词（11/9：杭州门户上"杭州市稳岗就业"一类链接占满了导航名额，部门网站页反而没走到）
_ZHOU_SHI = re.compile(r"(?:^|[^一-龥]|[各全及和与])州市")


def _word_in(word: str, text: str) -> bool:
    if word == "州市":
        return bool(_ZHOU_SHI.search(text))
    return word in text


def is_gov_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(".gov.cn") or host == "gov.cn"
