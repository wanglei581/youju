"""从政府网站栏目列表页里识别文件条目（标题、网址、发布日期）。

通用规则覆盖常见的政务网站模板：
- <ul><li><a>标题</a><span>日期</span></li></ul>
- <table><tr><td><a>标题</a></td><td>日期</td></tr></table>
- <div class="item"><a>标题</a><span>日期</span></div>
- 大汉 JCMS：条目藏在 <script type="text/xml"><datastore><record><![CDATA[…]]></record> 里

通用规则识别不准的栏目，在 sources.parser_config 里用 CSS 选择器覆盖：
{"item_selector": "ul.list li", "link_selector": "a", "date_selector": "span", "require_date": true}
"""

import json
import re
from dataclasses import dataclass
from datetime import date

import lxml.html
from lxml import etree

from youju.crawler.dates import date_from_url, parse_date
from youju.crawler.urls import normalize_url


@dataclass(frozen=True)
class ListItem:
    title: str
    url: str
    publish_date: date | None


_MIN_TITLE_LEN = 6
# 行文本比标题最多多出的字数；超过说明已经走到了列表容器
_MAX_ROW_EXTRA = 160
_MAX_ROW_DEPTH = 4
# 同一模板的其他列表块（如分页签），条目数达到这个值才合并
_MIN_SIBLING_GROUP = 5

_NAV_PATTERN = re.compile(
    r"^(第?\d+页?|更多.*|more.*|下一页|上一页|首页|尾页|末页|返回.*|打印.*|关闭.*|>>|»|…+|\.+)$",
    re.IGNORECASE,
)
_BOILERPLATE = ("版权所有", "ICP备", "网站地图", "联系我们", "网站标识码", "公网安备", "主办单位")
_DATASTORE_RECORD = re.compile(r"<record>\s*<!\[CDATA\[(.*?)\]\]>\s*</record>", re.S)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)
_EDGE_DATE = re.compile(
    r"^\s*[\[(（【]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)\s*[\])）】]?|"
    r"[\[(（【]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)\s*[\])）】]?\s*$"
)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _title_of(a: etree._Element) -> str:
    text = _clean(a.text_content())
    attr = _clean(a.get("title"))
    # 链接文字常被截断成"……"，title 属性里才是完整标题
    if attr and (len(attr) >= len(text) or text.endswith(("...", "…"))):
        return attr
    return text


def _looks_like_title(title: str) -> bool:
    if len(title) < _MIN_TITLE_LEN or _NAV_PATTERN.match(title):
        return False
    return not any(word in title for word in _BOILERPLATE)


_BODY_END = re.compile(r"</body\s*>", re.I)


def _expand_datastore(html: str) -> str:
    """把大汉 JCMS 藏在 datastore 里的条目展开成普通 HTML，插到 </body> 之前。

    不能追加到 </html> 之后：解析器会把它挂到第二个根元素下，普通遍历找不到。
    """
    records = _DATASTORE_RECORD.findall(html)
    if not records:
        return html
    block = "<div data-youju-datastore='1'><ul>" + "".join(records) + "</ul></div>"
    ends = list(_BODY_END.finditer(html))
    if not ends:
        return html + block
    cut = ends[-1].start()
    return html[:cut] + block + html[cut:]


class _Page:
    """一次识别过程的上下文：缓存每个元素里包含的标题链接，避免重复计算。"""

    def __init__(self, doc: etree._Element, page_url: str, today: date) -> None:
        self.doc = doc
        self.self_url = normalize_url(page_url, page_url)
        # 页面声明了 <base href> 时，相对链接要按它解析
        base = doc.xpath("//base[@href]/@href")
        self.page_url = normalize_url(page_url, base[0]) or page_url if base else page_url
        self.today = today
        self._hrefs: dict[etree._Element, set[str]] = {}

    def link(self, a: etree._Element) -> tuple[str, str] | None:
        url = normalize_url(self.page_url, a.get("href"))
        if not url or url == self.self_url:
            return None
        title = _title_of(a)
        if not _looks_like_title(title):
            return None
        return title, url

    def title_hrefs(self, el: etree._Element) -> set[str]:
        cached = self._hrefs.get(el)
        if cached is None:
            cached = set()
            for a in el.iter("a"):
                link = self.link(a)
                if link:
                    cached.add(link[1])
            self._hrefs[el] = cached
        return cached

    def find_row(self, a: etree._Element, title: str) -> tuple[etree._Element | None, date | None]:
        """向上找"一行"：只含这一条链接、文本不太长、并且写着日期的祖先元素。"""
        anchor_text = _clean(a.text_content())
        for depth, el in enumerate(a.iterancestors()):
            if depth >= _MAX_ROW_DEPTH or el.tag in ("body", "html"):
                break
            if len(self.title_hrefs(el)) > 1:
                break  # 已经包含多条链接，说明走到了列表容器
            text = _clean(el.text_content())
            if len(text) > len(title) + _MAX_ROW_EXTRA:
                break
            found = parse_date(text.replace(anchor_text, " "), self.today) if anchor_text else None
            if found:
                return el, found
        return None, None


def _date_inside_anchor(title: str, today: date) -> tuple[str, date | None]:
    """日期写在链接文字的开头或结尾时，把它拆出来。"""
    m = _EDGE_DATE.search(title)
    if not m:
        return title, None
    found = parse_date(m.group(1) or m.group(2), today)
    if not found:
        return title, None
    return _clean(title[: m.start()] + title[m.end() :]), found


def _group_key(container: etree._Element) -> tuple:
    # 同一父元素下、标签和样式相同的多个列表块（如分成几段的 ul）算作同一个列表
    return (container.getparent(), container.tag, container.get("class"))


def _extract_heuristic(page: _Page, require_date: bool) -> list[ListItem]:
    groups: dict[tuple, list[ListItem]] = {}
    # 用 XPath 遍历整个文档：不规范的页面可能在根元素之外还有内容
    for a in page.doc.xpath("//a"):
        link = page.link(a)
        if not link:
            continue
        title, url = link
        row, found = page.find_row(a, title)
        if found is None:
            title, found = _date_inside_anchor(title, page.today)
            if found is not None:
                row = a
        if found is None:
            found = date_from_url(url, page.today)
        if found is None and require_date:
            continue
        if not _looks_like_title(title):
            continue
        container = (row if row is not None else a).getparent()
        if container is None:
            continue
        groups.setdefault(_group_key(container), []).append(ListItem(title, url, found))

    if not groups:
        return []

    def score(key: tuple) -> tuple[int, date]:
        items = groups[key]
        latest = max((i.publish_date for i in items if i.publish_date), default=date.min)
        return len({i.url for i in items}), latest

    best_key = max(groups, key=score)
    result = list(groups[best_key])
    # 同一模板的其他大列表（例如"通知""公告"两个页签同时渲染）一并收下
    for key, items in groups.items():
        if key is best_key or key[1:] != best_key[1:]:
            continue
        if len({i.url for i in items}) >= _MIN_SIBLING_GROUP:
            result.extend(items)
    return result


def _extract_with_config(page: _Page, config: dict) -> list[ListItem]:
    items: list[ListItem] = []
    link_selector = config.get("link_selector", "a")
    date_selector = config.get("date_selector")
    require_date = config.get("require_date", True)
    for el in page.doc.cssselect(config["item_selector"]):
        anchors = [el] if el.tag == "a" else el.cssselect(link_selector)
        if not anchors:
            continue
        a = anchors[0]
        url = normalize_url(page.page_url, a.get("href"))
        title = _title_of(a)
        if not url or not title:
            continue
        date_text = ""
        if date_selector:
            date_els = el.cssselect(date_selector)
            date_text = _clean(date_els[0].text_content()) if date_els else ""
        else:
            date_text = _clean(el.text_content()).replace(_clean(a.text_content()), " ")
        found = parse_date(date_text, page.today) or date_from_url(url, page.today)
        if found is None and require_date:
            continue
        items.append(ListItem(title, url, found))
    return items


_JSON_TITLE_KEYS = ("title", "showTitle", "docTitle", "name")
_JSON_DATE_KEYS = (
    "pubDate",
    "publishDate",
    "publishTime",
    "releaseDate",
    "docDate",
    "createTime",
    "date",
    "time",
)
_JSON_URL_KEYS = ("url", "urls", "linkUrl", "link", "href", "docUrl", "pageUrl")
_MAX_JSON_CANDIDATES = 30


def _matching_bracket(text: str, start: int) -> int | None:
    """从 text[start] 的 '[' 开始，找到与之配对的 ']'，跳过字符串里的括号。"""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
    return None


def _json_url(value: object) -> str | None:
    # 有的网站把链接写成嵌套的 JSON 字符串，如 "{\"pc\":\"/col/…/content_1.html\"}"
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        value = value.get("pc") or next((v for v in value.values() if isinstance(v, str)), None)
    return value if isinstance(value, str) else None


def _extract_embedded_json(html: str, page: _Page) -> list[ListItem]:
    """列表数据写在脚本变量里（如 var listData = {articleList: [...]}），由前端渲染。

    数据已经在页面里，不需要浏览器渲染，直接读取 JSON 数组。
    """
    items: list[ListItem] = []
    for count, m in enumerate(re.finditer(r"\[\s*\{", html)):
        if count >= _MAX_JSON_CANDIDATES:
            break
        end = _matching_bracket(html, m.start())
        if end is None:
            continue
        try:
            data = json.loads(html[m.start() : end + 1])
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else []:
            if not isinstance(obj, dict):
                continue
            title = next((_clean(str(obj[k])) for k in _JSON_TITLE_KEYS if obj.get(k)), "")
            href = next((_json_url(obj[k]) for k in _JSON_URL_KEYS if obj.get(k)), None)
            url = normalize_url(page.page_url, href)
            if not url or not _looks_like_title(title):
                continue
            date_text = next((str(obj[k]) for k in _JSON_DATE_KEYS if obj.get(k)), "")
            found = parse_date(date_text, page.today) or date_from_url(url, page.today)
            if found is not None:
                items.append(ListItem(title, url, found))
    return items


def _dedupe(items: list[ListItem]) -> list[ListItem]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            result.append(item)
    return result


def extract_items(
    html: str, page_url: str, config: dict | None = None, today: date | None = None
) -> list[ListItem]:
    """识别列表页中的文件条目，按页面顺序返回，同一网址只保留一次。"""
    config = config or {}
    html = _XML_DECL.sub("", _expand_datastore(html))
    if not html.strip():
        return []
    try:
        doc = lxml.html.document_fromstring(html)
    except (etree.ParserError, ValueError):
        return []
    page = _Page(doc, page_url, today or date.today())
    if config.get("item_selector"):
        items = _extract_with_config(page, config)
    else:
        items = _extract_heuristic(page, require_date=config.get("require_date", True))
        if not items:
            items = _extract_embedded_json(html, page)
    return _dedupe(items)
