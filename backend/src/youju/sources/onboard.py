"""来源接入 Agent（D-023）：输入栏目网址 → 礼貌抓取一次 → 通用规则识别条目，
AI 补登记信息；通用规则认不出（或人觉得认错了）时由 AI 写抓取规则 → 人看试跑结果确认后才接入。

- 只接政府网站（域名以 .gov.cn 结尾）；遵守 robots，拒绝抓取的不接入、不绕过。
- AI 写的抓取规则只是 CSS 选择器：用它在同一页面试跑，识别出至少 3 条带日期的条目才给人看。
- 登记信息（层级、省份、部门简称、部门全称、栏目名）是建议：部门全称、栏目名要在网页里写着，
  否则留空由人填；人确认或修改后才保存。
- 发给 AI 的只有这张政府网页本身（标题、网站名、栏目名、列表标题、去掉脚本的页面结构），
  不含任何用户数据。
"""

import re
from dataclasses import dataclass, field

import lxml.html
from cssselect import SelectorError
from lxml import etree
from lxml.cssselect import CSSSelector

from youju.ai.client import AIError, ChatClient
from youju.crawler.fetcher import PoliteFetcher
from youju.crawler.listing import ListItem, extract_items

LEVELS = ("国家级", "省级", "市级", "区县级")
PROVINCES = (
    "国家",
    "北京",
    "天津",
    "河北",
    "山西",
    "内蒙古",
    "辽宁",
    "吉林",
    "黑龙江",
    "上海",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "重庆",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
)
# 试跑至少识别出这么多条带日期的条目，规则才算可用
MIN_ITEMS = 3
# 抓取规则里允许的键，和 listing._extract_with_config 读的一致
CONFIG_KEYS = ("item_selector", "link_selector", "date_selector")
_SELECTOR_MAX = 200
# 发给 AI 的页面结构最多多少字；每段文字截到多少字
_OUTLINE_MAX = 8000
_TEXT_MAX = 30

_META_SYSTEM = """你是政府网站栏目登记员。必须只返回 JSON 对象。
根据网页标题、网站名、栏目名和列表前几条标题，给出这个栏目的登记信息：
level（国家级、省级、市级、区县级之一）、province（省级行政区简称，如"山东""内蒙古"；国家部委填"国家"）、
city（城市简称，市级和区县级必填，如"青岛"；直辖市区县父城市写"北京"等）、
district（区县级必填完整名称，如"黄岛区"；其他层级写空字符串）、
dept（部门简称，如 科技、工信、发改委、商务、财政、人社、市场监管）、
dept_full_name（部门全称，照抄网页里写的，如"山东省科学技术厅"）、
column_name（栏目名，照抄网页里写的，如"通知公告"）。拿不准的字段写空字符串。"""

_RULE_SYSTEM = """你是网页抓取规则工程师。必须只返回 JSON 对象。
下面是一个政府网站栏目列表页的结构：去掉了脚本和样式，只保留 class、id、href 属性，文字截短。
写出识别正文文件列表的 CSS 选择器：item_selector（每一条文件对应的元素，如"ul.news-list li"）、
link_selector（条目里的标题链接，一般是"a"）、date_selector（条目里发布日期所在的元素，
日期和标题写在一起时写空字符串）。只用标准 CSS 选择器；不要选侧栏、导航、页脚里的链接。"""


@dataclass
class PageFacts:
    title: str = ""
    site_name: str = ""
    column_name: str = ""
    # 页面上的文字（去掉脚本和样式、去掉空白），用来核对名称是不是网页里写着的
    text: str = ""


@dataclass
class Proposal:
    level: str = ""
    province: str = ""
    city: str = ""
    district: str = ""
    dept: str = ""
    dept_full_name: str = ""
    column_name: str = ""


@dataclass
class TrialResult:
    url: str
    final_url: str = ""
    # 抓取结果：ok / robots_disallowed / robots_error / http_error / network_error / ...
    status: str = ""
    error: str | None = None
    generic: list[ListItem] = field(default_factory=list)
    ai_config: dict | None = None
    ai_items: list[ListItem] = field(default_factory=list)
    # AI 写的规则没采用的原因（没让 AI 写时为空）
    ai_note: str | None = None
    proposal: Proposal = field(default_factory=Proposal)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _document(html: str):
    try:
        return lxml.html.document_fromstring(html)
    except (etree.ParserError, ValueError):
        return None


def page_facts(html: str) -> PageFacts:
    doc = _document(html)
    if doc is None:
        return PageFacts()
    title = " ".join((doc.findtext(".//title") or "").split())
    metas = {
        (meta.get("name") or "").lower(): " ".join((meta.get("content") or "").split())
        for meta in doc.iter("meta")
    }
    for node in doc.xpath("//script|//style|//noscript"):
        node.drop_tree()
    return PageFacts(
        title=title[:200],
        site_name=metas.get("sitename", "")[:128],
        column_name=metas.get("columnname", "")[:64],
        text=_compact(doc.text_content()),
    )


def _on_page(value: str, facts: PageFacts) -> bool:
    compact = _compact(value)
    return bool(compact) and (
        compact in _compact(facts.title)
        or compact in _compact(facts.site_name)
        or compact in facts.text
    )


def _checked(raw: dict, facts: PageFacts) -> Proposal:
    """只留下合规的建议：层级、省份在固定范围内且互相对得上；部门全称、栏目名网页里写着。"""
    if not isinstance(raw, dict):  # 客户端只返回 JSON 对象，这里再防一道
        return Proposal()
    level = raw.get("level") if raw.get("level") in LEVELS else ""
    province = raw.get("province") if raw.get("province") in PROVINCES else ""
    if (level == "国家级") != (province == "国家") and level and province:
        level = province = ""  # 国家级只对应"国家"，对不上就都不填
    city = str(raw.get("city") or "").strip()[:32]
    dept = str(raw.get("dept") or "").strip()[:32]
    full = str(raw.get("dept_full_name") or "").strip()[:128]
    column = str(raw.get("column_name") or "").strip()[:64]
    return Proposal(
        level=level,
        province=province,
        city=city if level in ("市级", "区县级") and _on_page(city, facts) else "",
        district=str(raw.get("district") or "").strip()[:32]
        if level == "区县级" and _on_page(str(raw.get("district") or "").strip(), facts)
        else "",
        dept=dept,
        dept_full_name=full if _on_page(full, facts) else "",
        column_name=column if _on_page(column, facts) else "",
    )


def checked_city(level: str, city: str | None) -> str | None:
    """接入和 CSV 共用约束，防止城市信息缺失或挂在错误层级上。"""
    city = (city or "").strip() or None
    if level in ("市级", "区县级") and not city:
        raise ValueError("市级来源必须填写城市")
    if level not in ("市级", "区县级") and city:
        raise ValueError("其他层级的城市必须为空")
    if city and len(city) > 32:
        raise ValueError("城市最多 32 字")
    from youju.regions import checked_area_name

    return checked_area_name(city, "城市") or None


def checked_district(level: str, district: str | None) -> str | None:
    from youju.regions import checked_area_name

    district = checked_area_name(district, "区县")
    if level == "区县级" and not district:
        raise ValueError("区县级来源必须填写区县")
    if level != "区县级" and district:
        raise ValueError("其他层级的区县必须为空")
    return district or None


def propose(client: ChatClient | None, facts: PageFacts, items: list[ListItem]) -> Proposal:
    """登记信息建议：网页自己写的网站名、栏目名打底，配了 AI 时由 AI 补全；
    AI 出错就只用网页写的。"""
    base = Proposal(dept_full_name=facts.site_name, column_name=facts.column_name)
    if client is None:
        return base
    titles = "\n".join(item.title[:60] for item in items[:8])
    user = (
        f"网页标题：{facts.title}\n网站名：{facts.site_name}\n栏目名：{facts.column_name}\n"
        f"列表前几条：\n{titles}"
    )
    try:
        raw = client.complete_json(_META_SYSTEM, user, max_tokens=200)
    except AIError:
        return base
    proposal = _checked(raw, facts)
    proposal.dept_full_name = proposal.dept_full_name or base.dept_full_name
    proposal.column_name = proposal.column_name or base.column_name
    return proposal


# 检查选择器能不能真的运行用的空白页面（编译通过的选择器运行时仍可能出错，如带命名空间的）
_PROBE = lxml.html.document_fromstring(
    "<html><body><ul><li><a href='/'>x</a></li></ul></body></html>"
)


def valid_config(raw: object) -> dict | None:
    """抓取规则：只收三个键，都是能运行的标准 CSS 选择器（不带命名空间），必须有 item_selector；
    不合规返回 None。"""
    if not isinstance(raw, dict) or set(raw) - set(CONFIG_KEYS):
        return None
    config: dict[str, str] = {}
    for key in CONFIG_KEYS:
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not isinstance(value, str) or len(value) > _SELECTOR_MAX or "|" in value:
            return None
        try:
            CSSSelector(value.strip())(_PROBE)
        except (SelectorError, etree.XPathError):
            return None
        config[key] = value.strip()
    return config if "item_selector" in config else None


def page_outline(html: str) -> str:
    """给 AI 看的页面结构：去掉脚本、样式和页头页尾导航，只留 class、id、href，文字截短。"""
    doc = _document(html)
    if doc is None:
        return ""
    body = doc.find("body")
    if body is None:
        body = doc
    for node in body.xpath(
        ".//script|.//style|.//noscript|.//svg|.//iframe|.//header|.//footer|.//nav|.//comment()"
    ):
        node.drop_tree()
    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        for name in list(el.attrib):
            if name not in ("class", "id", "href"):
                del el.attrib[name]
        if "href" in el.attrib:
            el.attrib["href"] = el.attrib["href"][:60]
        if el.text:
            el.text = " ".join(el.text.split())[:_TEXT_MAX]
        if el.tail:
            el.tail = " ".join(el.tail.split())[:_TEXT_MAX]
    outline = etree.tostring(body, encoding="unicode", method="html")
    return re.sub(r"\s+", " ", outline)[:_OUTLINE_MAX]


def ai_rule(
    client: ChatClient, html: str, page_url: str
) -> tuple[dict | None, list[ListItem], str | None]:
    """请 AI 写抓取规则并在同一页面试跑。

    可用时返回（规则, 条目, None），否则返回（None, [], 原因）。
    """
    outline = page_outline(html)
    if not outline:
        return None, [], "页面结构读不出来"
    try:
        raw = client.complete_json(_RULE_SYSTEM, f"页面网址：{page_url}\n页面结构：\n{outline}")
    except AIError as exc:
        return None, [], f"AI 出错：{exc}"
    config = valid_config(
        {key: raw.get(key) for key in CONFIG_KEYS if key in raw} if isinstance(raw, dict) else raw
    )
    if config is None:
        return None, [], "AI 写的规则不是合法的 CSS 选择器"
    try:
        items = extract_items(html, page_url, config)
    except (SelectorError, etree.XPathError, ValueError):
        return None, [], "AI 写的规则运行出错"
    if len(items) < MIN_ITEMS:
        return None, [], f"AI 写的规则只识别出 {len(items)} 条带日期的条目"
    return config, items, None


def trial_fetcher() -> PoliteFetcher:
    """试跑用的礼貌抓取器：只抓政府网站，遵守 robots，与定时抓取用同样的标识和限速。"""
    from youju.config import get_settings
    from youju.crawler.detail_service import _government_url

    settings = get_settings()
    return PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        settings.per_host_interval,
        url_allowed=_government_url,
    )


def trial(
    url: str, fetcher: PoliteFetcher, client: ChatClient | None, *, want_ai_rule: bool = False
) -> TrialResult:
    """试跑一个栏目：抓一次列表页，通用规则识别条目，AI 给登记建议；通用规则识别不出，
    或人要求时（want_ai_rule），再请 AI 写抓取规则试跑。不写数据库。"""
    result = TrialResult(url=url)
    fetched = fetcher.fetch(url)
    result.status, result.error = fetched.status, fetched.error
    result.final_url = fetched.final_url or url
    if fetched.status != "ok":
        return result
    html = fetched.text
    result.generic = extract_items(html, result.final_url, {})
    result.proposal = propose(client, page_facts(html), result.generic)
    if client is not None and (want_ai_rule or len(result.generic) < MIN_ITEMS):
        result.ai_config, result.ai_items, result.ai_note = ai_rule(client, html, result.final_url)
    return result
