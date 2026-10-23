"""从政府网站栏目列表页里识别文件条目（标题、网址、发布日期）。

通用规则覆盖常见的政务网站模板：
- <ul><li><a>标题</a><span>日期</span></li></ul>
- <table><tr><td><a>标题</a></td><td>日期</td></tr></table>
- <div class="item"><a>标题</a><span>日期</span></div>
- 大汉 JCMS：条目藏在 <script type="text/xml"><datastore><record><![CDATA[…]]></record> 里
- 列表写在脚本变量里的 JSON（如 var listData = {articleList: [...]}）

一个页面上常有多组带日期的链接（正文列表、侧栏热点、页脚），按以下顺序挑正文列表：
条目数不少于 3 → 不在侧栏、页头页脚等外围区域 → 过半条目位于本栏目目录下 → 条目多 → 日期新。
脚本里的 JSON 列表按同样的顺序和页面上的列表比较。

通用规则识别不准的栏目，在 sources.parser_config 里用 CSS 选择器覆盖：
{"item_selector": "ul.list li", "link_selector": "a", "date_selector": "span", "require_date": true}
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import lxml.html
from lxml import etree

from youju.crawler.dates import date_from_url, parse_date_detail
from youju.crawler.titles import clean_title, is_truncated
from youju.crawler.urls import normalize_url, page_dir


@dataclass(frozen=True)
class ListItem:
    title: str
    url: str
    publish_date: date | None


_MIN_TITLE_LEN = 6
# 行文本比标题最多多出的字数；只在没有单独的日期节点时用于兜底
_MAX_ROW_EXTRA = 160
_MAX_ROW_DEPTH = 4
# 单独写日期的节点（如 <span>2026-09-20</span>）文字不会很长
_MAX_DATE_NODE_LEN = 30
# 条目数不少于这个值，才算像样的列表
_MIN_LIST_ITEMS = 3
# 同一列表分成几块显示时，这几块的共同祖先离列表块不超过这么多层
_MAX_BLOCK_LEVELS = 3

_NAV_PATTERN = re.compile(
    r"^(第?\d+页?|更多.*|more.*|下一页|上一页|首页|尾页|末页|返回.*|打印.*|关闭.*|>>|»|…+|\.+)$",
    re.IGNORECASE,
)
_BOILERPLATE = ("版权所有", "ICP备", "网站地图", "联系我们", "网站标识码", "公网安备", "主办单位")
# 专题专栏的归档入口（如湖南工信厅"2025湖南两会（已归档"），不是文件。
# 只认结尾，标题中间提到"已归档"的照收
_ARCHIVE_SUFFIXES = ("（已归档", "(已归档", "（已归档）", "(已归档)")
# 行内的附件链接（"附件：…""下载"，或指向 pdf、Word 等文件）属于同一条文件，不算另一条
_ATTACHMENT_TEXT = re.compile(r"^(附件|下载|点击下载|查看附件|附件下载)")
_ATTACHMENT_EXT = re.compile(r"\.(pdf|docx?|xlsx?|wps|et|ofd|zip|rar|7z)$", re.IGNORECASE)
# 侧栏、页头页脚、友情链接等外围区域的常见标记（标签，或 class/id 里的词）
_PERIPHERAL_TAGS = {"aside", "nav", "header", "footer"}
_PERIPHERAL_WORDS = {
    "side",
    "sidebar",
    "aside",
    "hot",
    "rank",
    "ranking",
    "recommend",
    "friend",
    "links",
    "yqlj",
    "footer",
    "foot",
    "header",
    "nav",
    "menu",
    "banner",
}
_TOKEN_SPLIT = re.compile(r"[-_]+")
_DATASTORE_RECORD = re.compile(r"<record>\s*<!\[CDATA\[(.*?)\]\]>\s*</record>", re.S)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)
_BODY_END = re.compile(r"</body\s*>", re.I)
# 写在链接文字开头或结尾的日期
_EDGE_DATE = re.compile(
    r"^\s*[\[(（【]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)\s*[\])）】]?"
    r"|[\[(（【]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)\s*[\])）】]?\s*$"
    r"|[\[(（【]\s*(\d{1,2}-\d{1,2})\s*[\])）】]\s*$"
    r"|\s(\d{2}-\d{2})\s*$"
)


_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
# 链接文字开头的列表序号（"8""10."）
_ORDINAL = re.compile(r"\s*\d{1,3}\s*[.、]?\s*")
# 链接里除了标题还写着日期（"2026-10""2026年10月"）：整条做成链接的列表行
_ROW_DATE = re.compile(r"20\d{2}\s*[-./年]\s*\d{1,2}")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _link_text(a: etree._Element) -> str:
    return clean_title(_clean(a.text_content()))


def _inner_title(a: etree._Element, text: str) -> str | None:
    """整条做成一个链接、标题写在里面某个元素的 title 属性里的（武汉市经信局：
    <a><span>23</span><span>2026-10</span><span title="完整标题">完整标题</span>摘要……</a>），
    链接文字里混着日期和正文摘要：用那个 title 属性。只认像标题、链接文字里原样写着的，
    而且链接里标题以外的文字写着日期——只是标题后面多一段补充说明的不算，照旧用整段文字。"""
    compact = text.replace(" ", "")
    for el in a.iterdescendants():
        if not isinstance(el.tag, str):
            continue
        value = clean_title(_clean(el.get("title")))
        inner = value.replace(" ", "")
        if not inner or len(inner) >= len(compact) or inner not in compact:
            continue
        if _looks_like_title(value) and _ROW_DATE.search(compact.replace(inner, " ", 1)):
            return value
    return None


def _leading_ordinal(a: etree._Element, prefix: str) -> bool:
    """链接文字开头那段数字是不是单独写在第一个子元素里的序号（<a><em>8</em>标题</a>）。"""
    children = [el for el in a if isinstance(el.tag, str)]
    return (
        bool(children)
        and not (a.text or "").strip()
        and _clean(children[0].text_content()) == _clean(prefix).rstrip(".、")
    )


def _title_of(a: etree._Element) -> str:
    text = _link_text(a)
    attr = clean_title(_clean(a.get("title")))
    if not attr:
        inner = _inner_title(a, text)
        if inner:
            return inner
    # 个别网站把索引号、发布时间等元数据写成 HTML 放在 title 属性里（如山西省科技厅）：
    # 链接文字像标题就用链接文字；不像标题（如"详情"）才用去掉标签后的 title 属性
    if _MARKUP.search(attr):
        stripped = clean_title(_clean(_MARKUP.sub(" ", attr)))
        return text if _looks_like_title(text) or not stripped else stripped
    # 序号写在链接里（深圳市工信局：<a title="完整标题"><em>8</em>完整标题</a>）：链接文字是
    # 单独一个元素里的序号加 title 属性，用 title 属性
    prefix = text[: -len(attr)] if attr and text.endswith(attr) else ""
    if prefix and _ORDINAL.fullmatch(prefix) and _leading_ordinal(a, prefix):
        return attr
    # 链接文字常被截断成"……"，title 属性里才是完整标题
    if attr and (len(attr) >= len(text) or is_truncated(text)):
        return attr
    # 日期写在链接里时（天津科技局、工信局：
    # <a title="完整标题">截断的标题...<span>2026-09-09</span></a>），文字结尾是日期，
    # 上面看不出截断，长度也多算了日期。去掉两端的日期再看：title 属性是剩下文字的完整写法、
    # 本身不带日期才用（"详情"这类无关文字不用）；日期由 _extract_heuristic 从链接文字里取
    shown = _clean(_EDGE_DATE.sub(" ", text))
    if shown != text and _completes(attr, shown) and not _EDGE_DATE.search(attr):
        return attr
    return text


def _completes(full: str, shown: str) -> bool:
    """full 是不是 shown 的完整写法：以 shown（去掉末尾省略号）开头，并且更长。空格不算。"""
    head = shown.rstrip(".…").replace(" ", "")
    full = full.replace(" ", "")
    return bool(head) and len(full) > len(head) and full.startswith(head)


def _looks_like_title(title: str) -> bool:
    if len(title) < _MIN_TITLE_LEN or _NAV_PATTERN.match(title):
        return False
    if _ATTACHMENT_TEXT.match(title) or title.endswith(_ARCHIVE_SUFFIXES):
        return False
    return not any(word in title for word in _BOILERPLATE)


def _is_attachment(url: str) -> bool:
    return bool(_ATTACHMENT_EXT.search(urlsplit(url).path))


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


def _is_peripheral(el: etree._Element) -> bool:
    """元素本身或其祖先是否属于侧栏、页头页脚等外围区域。"""
    for node in (el, *el.iterancestors()):
        if not isinstance(node.tag, str):
            continue
        if node.tag in _PERIPHERAL_TAGS:
            return True
        attrs = f"{node.get('class') or ''} {node.get('id') or ''}".lower()
        for token in attrs.split():
            if set(_TOKEN_SPLIT.split(token)) & _PERIPHERAL_WORDS:
                return True
    return False


class _Page:
    """一次识别过程的上下文：缓存每个元素里包含的标题链接，避免重复计算。"""

    def __init__(self, doc: etree._Element, page_url: str, today: date) -> None:
        self.doc = doc
        self.self_url = normalize_url(page_url, page_url)
        self.dir = page_dir(page_url)
        # 页面声明了 <base href> 时，相对链接要按它解析
        base = doc.xpath("//base[@href]/@href")
        self.page_url = (normalize_url(page_url, base[0]) or page_url) if base else page_url
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

    def date_node(
        self, el: etree._Element, a: etree._Element, skip: set
    ) -> tuple[date | None, bool]:
        """行内单独写日期的节点（如 <span>2026-09-20</span>），取最靠后的一个。

        链接自身和包含链接的节点不算，避免把标题里的日期当成发布日期。
        """
        found: tuple[date | None, bool] = (None, False)
        inside_anchor = set(a.iter())
        for node in el.iter():
            if node in skip or node in inside_anchor or not isinstance(node.tag, str):
                continue
            text = _clean(node.text_content())
            if not text or len(text) > _MAX_DATE_NODE_LEN:
                continue
            d, full = parse_date_detail(text, self.today)
            if d:
                found = (d, full)
        return found

    def find_row(
        self, a: etree._Element, title: str, url: str
    ) -> tuple[etree._Element | None, date | None, bool]:
        """向上找"一行"：只含这一条文件链接、并且写着日期的祖先元素。返回（行，日期，是否写明年份）。"""
        anchor_text = _link_text(a)
        ancestors = list(a.iterancestors())
        skip = set(ancestors)
        for depth, el in enumerate(ancestors):
            if depth >= _MAX_ROW_DEPTH or el.tag in ("body", "html"):
                break
            others = self.title_hrefs(el) - {url}
            if not _is_attachment(url):
                # 正文链接旁边的附件（pdf、Word 等）属于同一行
                others = {u for u in others if not _is_attachment(u)}
            if others:
                break  # 已经包含其他文件链接，说明走到了列表容器
            found, full = self.date_node(el, a, skip)
            if found:
                return el, found, full
            text = _clean(el.text_content())
            if anchor_text and len(text) <= len(title) + _MAX_ROW_EXTRA:
                found, full = parse_date_detail(text.replace(anchor_text, " "), self.today)
                if found:
                    return el, found, full
        return None, None, False


def _date_inside_anchor(title: str, today: date) -> tuple[str, date | None, bool]:
    """日期写在链接文字的开头或结尾时，把它拆出来。"""
    m = _EDGE_DATE.search(title)
    if not m:
        return title, None, False
    text = next(g for g in m.groups() if g)
    found, full = parse_date_detail(text, today)
    if not found:
        return title, None, False
    return _clean(title[: m.start()] + title[m.end() :]), found, full


# 只表示显示状态的样式名（页签选中、显隐等），比较结构时忽略
_STATE_CLASSES = {
    "active",
    "on",
    "cur",
    "current",
    "curr",
    "selected",
    "show",
    "hide",
    "hidden",
    "first",
    "last",
}
_NO_SCORE = (False, False, False, 0, date.min)


def _class_key(el: etree._Element) -> tuple[str, ...]:
    tokens = (el.get("class") or "").lower().split()
    return tuple(sorted(t for t in tokens if t not in _STATE_CLASSES))


def _group_key(container: etree._Element) -> tuple:
    # 同一父元素下标签相同的几个列表块算作同一个列表，例如手机版只显示前 5 条、
    # 其余条目放在另一个带不同样式的 ul 里。侧栏、推荐这类外围块单独成组，不并入正文
    return (container.getparent(), container.tag, _is_peripheral(container))


def _parallel(a: etree._Element, b: etree._Element) -> bool:
    """两个列表块是否是同一结构的重复：同一列表分块显示，或同一栏目的几个页签。

    从两边同时往上找共同祖先（不超过 _MAX_BLOCK_LEVELS 层）。共同祖先下面的那一层
    （重复单元）标签和样式都相同（忽略 active 这类状态样式）、再往下到列表块的标签路径
    也相同，才算重复。侧栏、页脚与正文列表的外层结构不同，不会被并进来。
    """
    if a.tag != b.tag:
        return False
    up_a = [a, *a.iterancestors()][: _MAX_BLOCK_LEVELS + 1]
    up_b = [b, *b.iterancestors()][: _MAX_BLOCK_LEVELS + 1]
    for level in range(1, min(len(up_a), len(up_b))):
        if up_a[level] is up_b[level]:
            unit_a, unit_b = up_a[level - 1], up_b[level - 1]
            same_unit = (unit_a.tag, _class_key(unit_a)) == (unit_b.tag, _class_key(unit_b))
            return same_unit and [n.tag for n in up_a[:level]] == [n.tag for n in up_b[:level]]
    return False


def _in_dir(items: list[ListItem], page: _Page) -> float:
    """条目中位于本栏目目录下的比例。"""
    urls = {i.url for i in items}
    return sum(1 for u in urls if urlsplit(u).path.startswith(page.dir)) / len(urls)


def _score(items: list[ListItem], page: _Page, peripheral: bool) -> tuple:
    """挑正文列表的排序依据：条目数够 → 不在外围区域 → 过半位于本栏目目录下 → 条目多 → 日期新。"""
    if not items:
        return _NO_SCORE
    urls = {i.url for i in items}
    latest = max((i.publish_date for i in items if i.publish_date), default=date.min)
    return (
        len(urls) >= _MIN_LIST_ITEMS,
        not peripheral,
        _in_dir(items, page) >= 0.5,
        len(urls),
        latest,
    )


def _clusters(
    containers: list[etree._Element], peripheral: list[bool], in_dir: list[bool]
) -> list[list[int]]:
    """把互为重复结构的列表块并成一组，返回每组的块编号。

    外围区域的块不参与合并；一块过半位于本栏目目录下、另一块不是的，也不合并（多半是正文和侧栏）。
    """
    root = list(range(len(containers)))

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    for i in range(len(containers)):
        for j in range(i + 1, len(containers)):
            if peripheral[i] or peripheral[j] or in_dir[i] != in_dir[j] or find(i) == find(j):
                continue
            if _parallel(containers[i], containers[j]):
                root[find(j)] = find(i)
    merged: dict[int, list[int]] = {}
    for i in range(len(containers)):
        merged.setdefault(find(i), []).append(i)
    return list(merged.values())


def _extract_heuristic(page: _Page, require_date: bool) -> tuple[tuple, list[ListItem]]:
    """从页面链接里找正文列表，返回（评分，条目）。"""
    # 每个列表块里的条目，连同它在页面中的先后位置
    blocks: dict[tuple, list[tuple[int, ListItem]]] = {}
    containers: dict[tuple, etree._Element] = {}
    # 用 XPath 遍历整个文档：不规范的页面可能在根元素之外还有内容
    for position, a in enumerate(page.doc.xpath("//a")):
        link = page.link(a)
        if not link:
            continue
        title, url = link
        row, found, full = page.find_row(a, title, url)
        if found is None:
            title, found, full = _date_inside_anchor(title, page.today)
            if found is None:
                # 标题取自 title 属性时，链接文字里写的日期（如天津科技局）照样是发布日期
                _, found, full = _date_inside_anchor(_link_text(a), page.today)
            if found is not None:
                row = a
        url_date = date_from_url(url, page.today)
        if found is None or (not full and url_date):
            # 行里只写了"月-日"而网址里有完整日期时，以网址为准（年份不会推错）
            found = url_date or found
        if found is None and require_date:
            continue
        if not _looks_like_title(title):
            continue
        container = (row if row is not None else a).getparent()
        if container is None:
            continue
        key = _group_key(container)
        blocks.setdefault(key, []).append((position, ListItem(title, url, found)))
        containers.setdefault(key, container)

    if not blocks:
        return _NO_SCORE, []

    keys = list(blocks)
    peripheral = [k[2] for k in keys]
    in_dir = [_in_dir([item for _, item in blocks[k]], page) >= 0.5 for k in keys]
    candidates = []
    for members in _clusters([containers[k] for k in keys], peripheral, in_dir):
        entries = sorted(entry for i in members for entry in blocks[keys[i]])
        items = [item for _, item in entries]
        candidates.append((_score(items, page, peripheral[members[0]]), items))
    return max(candidates, key=lambda c: c[0])


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
        if date_selector:
            date_els = el.cssselect(date_selector)
            date_text = _clean(date_els[0].text_content()) if date_els else ""
        else:
            date_text = _clean(el.text_content()).replace(_clean(a.text_content()), " ")
        found, full = parse_date_detail(date_text, page.today)
        url_date = date_from_url(url, page.today)
        if found is None or (not full and url_date):
            found = url_date or found
        if found is None and require_date:
            continue
        items.append(ListItem(title, url, found))
    return items


# 只认明确表示"标题""发布时间"的字段；name、time 这类泛化字段常见于导航数据，不用
_JSON_TITLE_KEYS = ("title", "showTitle", "docTitle")
_JSON_DATE_KEYS = (
    "pubDate",
    "publishDate",
    "publishTime",
    "pubTime",
    "releaseDate",
    "docDate",
    "createTime",
    "createDate",
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


def _items_from_json_array(data: object, page: _Page) -> list[ListItem]:
    items: list[ListItem] = []
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        title = next((clean_title(_clean(str(obj[k]))) for k in _JSON_TITLE_KEYS if obj.get(k)), "")
        href = next((_json_url(obj[k]) for k in _JSON_URL_KEYS if obj.get(k)), None)
        url = normalize_url(page.page_url, href)
        if not url or not _looks_like_title(title):
            continue
        date_text = next((str(obj[k]) for k in _JSON_DATE_KEYS if obj.get(k)), "")
        found, full = parse_date_detail(date_text, page.today)
        url_date = date_from_url(url, page.today)
        if found is None or (not full and url_date):
            found = url_date or found
        if found is not None:
            items.append(ListItem(title, url, found))
    return items


def _extract_embedded_json(html: str, page: _Page) -> tuple[tuple, list[ListItem]]:
    """列表数据写在脚本变量里（如 var listData = {articleList: [...]}），由前端渲染。

    数据已经在页面里，不需要浏览器渲染。页面上可能有多个 JSON 数组（导航、推荐等），
    按与页面列表相同的评分取最像正文列表的一个。返回（评分，条目）。
    """
    best: tuple[tuple, list[ListItem]] = (_NO_SCORE, [])
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
        items = _dedupe(_items_from_json_array(data, page))
        scored = (_score(items, page, peripheral=False), items)
        if scored[0] > best[0]:
            best = scored
    return best


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
        return _dedupe(_extract_with_config(page, config))
    score, items = _extract_heuristic(page, require_date=config.get("require_date", True))
    # 页面上的链接和脚本里的列表数据都看，取更像正文列表的一种
    json_score, json_items = _extract_embedded_json(html, page)
    if json_score > score:
        items = json_items
    return _dedupe(items)
