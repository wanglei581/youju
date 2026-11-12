"""认出政府网站用的是哪套建站平台（集约化平台），以及有没有防护、验证码。

同一个省的部门、市、县网站常建在同一个集约化平台上：网址形状、分页写法、详情页结构都一样，
一套读取规则就能用在几百个网站上。这里只看页面本身（不另外请求）：
- 页面元数据：<meta name="SiteIDCode">（政府网站标识码，前 6 位是行政区划代码）、SiteName；
- 页脚"技术支持：××公司"；
- 平台特有的标记（大汉 JCMS 的 datastore、拓尔思的 createPageHTML 等）；
- 页面上文章链接的网址形状（/art/2026/11/5/art_123_456.html、/t20261105_123.html……）。
"""

import html as html_lib
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from lxml import etree

# 文章网址形状 → 名称。顺序有意义：越具体越靠前
URL_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("art_日期_编号(大汉)", re.compile(r"/art/20\d{2}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html?$")),
    ("post_编号(广东集约化)", re.compile(r"/post_\d+\.html?$")),
    ("t日期_编号(拓尔思)", re.compile(r"/t20\d{6}_\d+\.s?html?$")),
    ("年/月-日/编号", re.compile(r"/20\d{2}/\d{2}-\d{2}/\d+\.s?html?$")),
    ("content_编号", re.compile(r"/content_\d+\.s?html?$")),
    ("32位编号.shtml", re.compile(r"/[0-9a-f]{32}\.s?html?$")),
    ("年/月/日/编号.shtml", re.compile(r"/20\d{2}/\d{1,2}/\d{1,2}/\d+\.s?html?$")),
    ("system/年/月/日/编号", re.compile(r"/system/20\d{2}/\d{2}/\d{2}/\d+\.s?html?$")),
    ("20位编号/index.shtml", re.compile(r"/20\d{18,}/index\.s?html?$")),
    ("c编号/日期/编号", re.compile(r"/c\d+/20\d{2}-\d{2}-\d{2}/\d+\.s?html?$")),
    ("columns-uuid", re.compile(r"/columns/[0-9a-f-]{36}/")),
    ("日期目录/编号", re.compile(r"/20\d{4}/[0-9a-z_]{6,}\.s?html?$")),
    ("动态页?id=", re.compile(r"\.(?:jsp|aspx?|php|do)\?.*(?:id|ID|Id)=")),
)
# 平台标记（页面源码里出现就算）→ 平台
MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("大汉", re.compile(r"/module/web/jpage/|<datastore>|jcms|hanweb", re.I)),
    ("拓尔思", re.compile(r"createPageHTML|TRS_Editor|trs_|TRSWCM|\btrs\b|trscms", re.I)),
    ("开普云", re.compile(r"kaipuyun|kpaas|kpcms|开普云", re.I)),
    ("南威", re.compile(r"linewell|南威", re.I)),
    ("中软", re.compile(r"cicro|中软", re.I)),
    ("动易", re.compile(r"SiteFactory|PowerEasy|动易", re.I)),
    ("UCAP", re.compile(r"\bucap\b", re.I)),
    (
        "Vue/前端渲染",
        re.compile(r"<div id=\"app\">\s*</div>|chunk-vendors|__NUXT__|webpackJsonp", re.I),
    ),
)
_SUPPORT = re.compile(
    r"(?:技术支持|技术支撑|技术保障|建设运维|运维支持|技术服务)\s*[:：]?\s*([^<>|\n\r]{2,40})"
)
# 页脚"技术支持"后面常跟着电话、邮箱、备案号（"××公司 电话：0xxx""技术运维电话：…""公网安备…号"）：
# 清单放在公开仓库里，只留单位名，碰到这些就截断（11/12 地市一组报：main 上市级 49 行、区县 155 行）
_SUPPORT_CUT = re.compile(
    r"(?:联系|运维|咨询|值班)?电话|手机|联系方式|邮箱|e-?mail|地址|传真|[\u4e00-\u9fff]?(?:ICP|公网安备)|备案|网站标识码"
    r"|\d{5,}|[\w.+-]+@[\w-]+\.[\w.]+|总访问量|隐私声明|仅受理|建议使用",
    re.I,
)
_SUPPORT_LEAD = re.compile(r"^(?:与|及|和)?(?:维护|运维|管理)?(?:单位)?[:：]")


def clean_support(text: str) -> str:
    """页脚"技术支持"只留单位名：去掉网页实体、空白，碰到电话、邮箱、备案号这类就截断。"""
    text = re.sub(r"\s+", "", html_lib.unescape(text or "")).replace("\xa0", "")
    m = _SUPPORT_CUT.search(text)
    if m:
        text = text[: m.start()]
    text = _SUPPORT_LEAD.sub("", text)
    return text.strip("：:，,;；|/-—（(")[:30]


_VENDOR_WORDS = (
    ("大汉", "大汉"),
    ("拓尔思", "拓尔思"),
    ("开普云", "开普云"),
    ("南威", "南威"),
    ("国泰新点", "国泰新点"),
    ("浪潮", "浪潮"),
    ("太极", "太极"),
    ("中国电信", "中国电信"),
    ("中国联通", "中国联通"),
    ("中国移动", "中国移动"),
    ("数字广东", "数字广东"),
    ("中软", "中软"),
    ("博思", "博思"),
    ("华宇", "华宇"),
    ("东软", "东软"),
    ("易建", "易建"),
    ("万户", "万户"),
    ("麦达", "麦达"),
    ("新华网", "新华网"),
    ("人民网", "人民网"),
)


@dataclass
class Fingerprint:
    site_name: str = ""
    site_id_code: str = ""
    support: str = ""  # 页脚"技术支持"写的公司（截短）
    vendor: str = ""  # 认出的平台厂商
    markers: list[str] = field(default_factory=list)
    shape: str = ""  # 本站文章链接最多的网址形状
    shape_count: int = 0

    @property
    def platform(self) -> str:
        """平台标签：厂商 + 网址形状，如"大汉|art_日期_编号(大汉)"。认不出写"未识别"。"""
        parts = [p for p in (self.vendor, self.shape) if p]
        return "|".join(parts) or "未识别"


def _meta(doc: etree._Element, name: str) -> str:
    for el in doc.iter("meta"):
        if (el.get("name") or "").strip().lower() == name.lower():
            return (el.get("content") or "").strip()
    return ""


def fingerprint(
    html: str, page_url: str, doc: etree._Element | None, link_urls: list[str]
) -> Fingerprint:
    fp = Fingerprint()
    if doc is not None:
        fp.site_name = _meta(doc, "SiteName")[:60]
        fp.site_id_code = re.sub(r"\s", "", _meta(doc, "SiteIDCode"))[:20]
    if not fp.site_id_code:
        m = re.search(r"(?:网站标识码|标识码)\s*[:：]?\s*([0-9A-Za-z]{10,12})", html or "")
        if m:
            fp.site_id_code = m.group(1)
    m = _SUPPORT.search(re.sub(r"<[^>]+>", " ", html[-20000:] if html else ""))
    if m:
        fp.support = clean_support(m.group(1))
    for name, pattern in MARKERS:
        if pattern.search(html or ""):
            fp.markers.append(name)
    for word, vendor in _VENDOR_WORDS:
        if word in fp.support:
            fp.vendor = vendor
            break
    if not fp.vendor and fp.markers:
        fp.vendor = fp.markers[0]
    host = urlsplit(page_url).netloc.lower()
    shapes: Counter[str] = Counter()
    for url in link_urls:
        parts = urlsplit(url)
        if parts.netloc.lower() != host:
            continue
        path = parts.path + ("?" + parts.query if parts.query else "")
        for name, pattern in URL_SHAPES:
            if pattern.search(path):
                shapes[name] += 1
                break
    if shapes:
        fp.shape, fp.shape_count = shapes.most_common(1)[0]
    if not fp.vendor and fp.shape.endswith(")"):
        fp.vendor = fp.shape[fp.shape.rindex("(") + 1 : -1]
    return fp


# 防护、验证码、登录：一律记"不能抓"，不绕过
_BLOCK_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("瑞数防护", re.compile(r"\$_ts\s*=|\$_ts\.|9DhefwqGPrzGxEp9hPaoag|FSSBBIl1UgzbN7N", re.I)),
    ("加速乐防护", re.compile(r"__jsl_clearance|jsl_clearance_s", re.I)),
    ("云锁/安全狗", re.compile(r"yunsuo_session|safedog|安全狗", re.I)),
    (
        "WAF 拦截",
        re.compile(r"waf\.tencent-cloud|aliyun_waf|_waf_|访问被拦截|您的访问被阻断|访问受限", re.I),
    ),
)
_CAPTCHA = re.compile(r"请输入验证码|滑动验证|滑块验证|人机验证|安全验证|captcha", re.I)
_PASSWORD = re.compile(r"<input[^>]+type\s*=\s*['\"]?password", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_GATE_TITLE = re.compile(
    r"登录|登陆|统一身份认证|身份认证|用户认证|login|sign\s*in|验证|安全检查", re.I
)


def blocker(status: str, http_status: int | None, html: str) -> str | None:
    """页面是不是防护页、验证码页或登录页；是就返回原因（记"不能抓"），否则 None。

    验证码、登录按"页面本身就是关卡"判断：标题写着登录、认证、验证，或者正文很少、只剩一个关卡；
    门户首页角落里的"用户登录"小框、留言板的"验证码"不算（它们旁边就是正常内容）。
    """
    text = html or ""
    head = text[:200000]
    for name, pattern in _BLOCK_MARKERS:
        if pattern.search(head):
            return name
    m = _TITLE.search(head)
    title = re.sub(r"\s+", "", m.group(1)) if m else ""
    gate_title = bool(_GATE_TITLE.search(title))
    # 整页就是一个关卡：正文很少、链接也很少（页头有登录小框、列表靠接口加载的栏目页
    # 正文同样很少，但导航链接多，不算）
    text_len = len(re.sub(r"<[^>]+>|\s+", "", re.sub(r"<script.*?</script>", "", head, flags=re.S)))
    bare = text_len < 1500 and len(re.findall(r"<a\s[^>]*href=", head, re.I)) < 15
    if _PASSWORD.search(head) and (gate_title or bare):
        return "需要登录"
    if _CAPTCHA.search(head) and (gate_title or bare):
        return "验证码"
    if http_status in (401, 412, 521) or (http_status == 403 and status == "http_error"):
        return f"HTTP {http_status} 拒绝访问"
    if status == "robots_disallowed":
        return "robots.txt 禁止"
    return None
