"""平台适配器：栏目页上读不出条目，但页面自己写明了列表接口时，按页面给的参数取列表。

政府网站集约化平台把列表交给浏览器脚本去取，页面里只留一个"占位 + 接口地址 + 参数"。同一个平台
的几百个网站写法完全一样，认出一种写法就能读一个平台（全国覆盖第一阶段，见
docs/数据来源/全国覆盖/集约化平台盘点.md）。

规矩和抓页面一样：
- 只用页面自己声明的、同一网站上的公开接口，只发 GET；照常遵守 robots.txt、同一网站间隔 3 秒；
- 只认白名单里的接口路径，不拼别的地址；要登录、签名、验证码的接口一律不碰。数字广东的接口
  地址页面里没有整句写出，由同一网站的平台脚本按页面写明的站点地址、站点编号和分类编号拼出：
  这里照平台脚本的拼法、只用页面写明的这几个值，和浏览器打开这个分类时发的请求一样（见下）。
- 读取护栏（D-128）：
  ① 只按网页里写明的地址和参数去取，不改参数、不另外构造请求（find_unit_apis 只认白名单路径、
    同一网站，参数原样照页面的 queryData，不加分页、检索等参数；数字广东照平台脚本的拼法，只取
    第 1 页）；
  ② 接口返回不是成功、返回里带权限要求（roles、permissions 一类字段不为空）、要登录或验证码、
    网站明确拒绝（HTTP 401/403/412/521、robots 禁止）的，一律记"不能抓"、不读（api_refusal 给出
    原因；api_html 只在返回明确成功、没有权限要求时才给出列表，别的一律空串）；
  ③ 护栏上线之前，这类列表照旧不读：已经记成"不能抓"的栏目，等护栏上线、重新试读、抽查合格
    以后再放（D-122 ①、D-126 ③）；同一网站请求间隔不少于 3 秒（PoliteFetcher）。

现有适配器：
- 大汉 jpaas（浙江等省的集约化平台）：<script src=".../unitbuild.js"
  url="/api-gateway/jpaas-publish-server/front/page/build/unit"
  queryData="{'pageId':'…','tagId':'当前栏目list',…}">，GET 同样的参数，返回 JSON，
  data.html 是列表的 HTML。
- 数字广东 gkmlpt（广东省、市、县的政府信息公开平台）：部门页是脚本应用，页面内联脚本
  window._CONFIG 里写着站点地址 APP_URL、站点编号 SID、ENV 和分类树 TREE；盘点把分类记成栏目
  "部门页#xxgk-gkmlpt-分类编号"。列表由同一网站的平台脚本按分类加载：去掉协议的 APP_URL +
  /gkmlpt/api/all/分类编号?page=1&sid=SID，协议跟页面一样，GET，返回 JSON（articles 是条目）。
  地址照平台脚本的写法拼，用到的值只取页面写明的那几项；只取第 1 页，不加别的参数。平台脚本
  带的 X-CSRF-TOKEN 请求头取的是页面里没有的配置项（实际是空的），这里不带令牌；cookie 和抓页面
  一样，只有网站自己下发的会照常带回（和浏览器一样），不另外造。条目的发布日期取 create_time
  （平台列表"发布日期"一栏），不取 date（那是"成文日期"）。
"""

import ast
import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import lxml.html
from lxml import etree

from youju.crawler.dates import _safe_date
from youju.crawler.listing import ListItem, _looks_like_title, extract_items
from youju.crawler.titles import clean_title
from youju.crawler.urls import host_of, normalize_url, url_key

HANWEB_JPAAS_PATH = "/api-gateway/jpaas-publish-server/front/page/build/unit"
# 适配器名（写进来源 parser_config 的"经接口读"标记 list_api_open，正式抓取据此决定走不走接口）
HANWEB_JPAAS = "大汉jpaas"
GKMLPT = "数字广东gkmlpt"


@dataclass(frozen=True)
class ListApi:
    adapter: str  # 适配器名
    url: str  # 接口网址（和栏目页同一网站）
    base_url: str  # 接口返回的列表里的相对链接，以栏目页为准
    # 数字广东 gkmlpt：请求的分类编号（返回里的 classify.id 要和它一样）
    category_id: int | None = None
    # 数字广东几个分类合读的栏目：每个分类各一个接口（见 api_parts）；单个分类的为空
    parts: tuple["ListApi", ...] = ()
    # 数字广东"政策"一类父分类：只要直接挂在它上面的条目（条目的 classify_main 就是它），挂在子分类
    # （解读等）上的不要——要的子分类另外按叶子读
    direct_only: bool = False


_KNOWN_ATTRS = frozenset({"id", "src", "webid", "parsetype", "url", "querydata"})
_BASE_QUERY = frozenset(
    {"parseType", "webId", "tplSetId", "pageType", "tagId", "editType", "pageId"}
)
_KNOWN_QUERY_SETS = frozenset({_BASE_QUERY, _BASE_QUERY | {"unitType"}})
_STRAY_END = re.compile(r"</(?:html|body)\s*>", re.I)


def _unit_scripts(html: str) -> list[dict[str, str]]:
    """页面里真正的 <script src=".../unitbuild.js"> 元素上写的属性（属性名小写，字符实体已还原）。

    用 HTML 解析器找元素，不用文字匹配：注释、文本域、样式、脚本代码字符串、别的元素属性值里
    写着的都不是元素，浏览器不会执行，不认；noscript、template 里的也不认。同名属性和浏览器
    一样取第一个（D-128 读取护栏 ①：只按页面写明、浏览器真会用的地址和参数去取）。"""
    if not html or "unitbuild.js" not in html.lower():
        return []
    try:
        # 页面中间夹着的 </html>、</body>（中国政府网导航页有）会让解析器提前停下：先去掉
        doc = lxml.html.document_fromstring(_STRAY_END.sub("", html))
    except (etree.LxmlError, ValueError):
        return []
    out = []
    for el in doc.iter("script"):
        # 加载的就是 unitbuild.js（路径以它结尾），不是参数里碰巧带着这几个字
        if not urlsplit(el.get("src") or "").path.lower().endswith("/unitbuild.js"):
            continue
        if any(a.tag in ("noscript", "template") for a in el.iterancestors()):
            continue
        out.append({str(k).lower(): v for k, v in el.attrib.items()})
    return out


# 数字广东信息公开平台的分类栏目网址："部门页#xxgk-gkmlpt-分类编号"
# （盘点 deptdir.gkmlpt_tree_categories 记的）
# 几个分类合读的写成"编号,编号"（最多 5 个）；第一个前面带 p 的是父分类，只要直接挂在它上面的条目
_GK_COLUMN = re.compile(r"#xxgk-gkmlpt-(p?[0-9]+(?:,[0-9]+){0,4})$")
# 遮住 JS 字符串和注释，避免把其中的文字当作可执行声明或配置项。
# 页面脚本在声明以后又改写这几项配置（window._CONFIG.SID = '9'、window._CONFIG['ENV'] = …）
_CONFIG_OVERRIDE = re.compile(r"\b_CONFIG\s*(?:\.\s*(?:APP_URL|SID|ENV|TREE)\s*=(?!=)|\[)")
_JS_LITERAL = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\[\s\S]|[^'\\])*'|\"(?:\\[\s\S]|[^\"\\])*\"|`(?:\\[\s\S]|[^`\\])*`"
)


def _gkmlpt_spec(column_url: str | None) -> tuple[tuple[int, ...], bool] | None:
    """栏目网址里的数字广东分类：(编号们, 第一个是不是只取直接挂在它上面的父分类)；不是给 None。
    一至五个、不重复的编号。"""
    match = _GK_COLUMN.search(column_url or "")
    if not match:
        return None
    spec = match.group(1)
    ids = tuple(map(int, spec.lstrip("p").split(",")))
    return (ids, spec.startswith("p")) if len(set(ids)) == len(ids) else None


def gkmlpt_categories(column_url: str | None) -> tuple[int, ...] | None:
    """栏目网址是数字广东信息公开平台的分类（"部门页#xxgk-gkmlpt-编号[,编号…]"）时给分类编号，
    否则 None。"""
    spec = _gkmlpt_spec(column_url)
    return spec[0] if spec else None


def api_parts(api: ListApi) -> tuple[ListApi, ...]:
    return api.parts or (api,)


def api_items_merged(
    api: ListApi, texts: list[str], parser_config: dict | None, today: date | None
) -> list[ListItem]:
    """各分类已通过护栏后合并，按网址去重并按发布日期排序。"""
    parts = api_parts(api)
    if len(parts) == 1 and len(texts) == 1:
        return api_items(parts[0], texts[0], parser_config, today)
    items: dict[str, ListItem] = {}
    for part, text in zip(parts, texts, strict=True):
        for item in api_items(part, text, parser_config, today):
            items.setdefault(url_key(item.url), item)
    return sorted(items.values(), key=lambda i: i.publish_date or date.min, reverse=True)


def list_api_adapter(column_url: str) -> str:
    """打开"经接口读"的栏目时写进 list_api_open 的适配器名：数字广东的分类栏目是 gkmlpt，
    其余（页面上声明 unitbuild.js 的）是大汉 jpaas。和 find_list_api 认接口的分法一样。"""
    return GKMLPT if gkmlpt_categories(column_url) is not None else HANWEB_JPAAS


def find_gkmlpt_api(html: str, page_url: str, column_url: str) -> ListApi | None:
    """数字广东部门页上某个分类的列表接口（第 1 页）；认不出返回 None。

    只认得出才认：页面里真正的内联脚本（不在 noscript、template 里，不算注释和字符串）正好有一个
    window._CONFIG，APP_URL、SID、ENV、TREE 各出现一次；SID 全是数字，ENV 是 production（不是的话
    平台脚本走别的地址）；分类在 TREE 里而且不跳到别处（jump_url 为空）；APP_URL 是不带端口、
    查询的 http(s) 网址，和部门页同一主机，部门页在"APP_URL 路径/gkmlpt/"下面；页面脚本后面又改写
    这几项（window._CONFIG.SID = … 一类）的不认。地址和浏览器里的平台脚本一样按协议相对地址拼：
    协议跟部门页，主机是 APP_URL 的（和部门页一样），端口是协议的默认端口——部门页不在默认端口上
    的不认。主机、端口原样用部门页网址里的写法（带 :443 的照带），抓取程序按同一个网站排队、隔 3 秒
    （D-013）。page_url 是部门页抓取的最终网址。"""
    spec = _gkmlpt_spec(column_url)
    if spec is None:
        return None
    categories, parent_direct = spec
    try:
        doc = lxml.html.document_fromstring(_STRAY_END.sub("", html))
    except (etree.LxmlError, ValueError):
        return None
    declarations = []
    for el in doc.iter("script"):
        if el.get("src") is not None or any(
            a.tag in ("noscript", "template") for a in el.iterancestors()
        ):
            continue
        source = el.text or ""
        masked = _JS_LITERAL.sub(lambda m: " " * len(m.group()), source)
        if _CONFIG_OVERRIDE.search(masked):
            return None  # 页面脚本另外改写了配置项：浏览器用的不一定是声明里的值，不认
        declarations.extend(
            (source, masked, m.end())
            for m in re.finditer(r"\bwindow\s*\.\s*_CONFIG\s*=\s*\{", masked)
        )
    if len(declarations) != 1:
        return None
    source, masked, start = declarations[0]
    depth = 1
    fields: dict[str, list[int]] = {k: [] for k in ("APP_URL", "SID", "ENV", "TREE")}
    token = re.compile(r"[{}\[\]]|\b(?:APP_URL|SID|ENV|TREE)\s*:")
    for m in token.finditer(masked, start):
        value = m.group()
        if value in "{[":
            depth += 1
        elif value in "}]":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            fields[value.split(":")[0].strip()].append(m.end())
    else:
        return None
    if any(len(v) != 1 for v in fields.values()):
        return None
    values = {}
    for key in ("APP_URL", "SID", "ENV"):
        m = re.match(r"\s*'([^'\\]*)'\s*(?=,|})", source[fields[key][0] :])
        if not m:
            return None
        values[key] = m.group(1)
    if not re.fullmatch(r"[0-9]+", values["SID"]) or values["ENV"] != "production":
        return None
    try:
        tree_source = source[fields["TREE"][0] :].lstrip()
        tree, end = json.JSONDecoder().raw_decode(tree_source)
        if not re.match(r"\s*[,}]", tree_source[end:]):
            return None
        app, page = urlsplit(values["APP_URL"]), urlsplit(page_url)
        if (
            app.scheme not in ("http", "https")
            or not app.hostname
            or app.port is not None
            or app.username is not None
            or app.password is not None
            or app.query
            or app.fragment
            or page.scheme not in ("http", "https")
            or page.username is not None
            or page.password is not None
            or app.hostname != page.hostname
            or not page.path.startswith(app.path.rstrip("/") + "/gkmlpt/")
        ):
            return None
        stack = list(tree) if isinstance(tree, list) else []
        found: set[int] = set()
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if (
                    type(node.get("id")) is int
                    and node["id"] in categories
                    and node.get("jump_url") in (None, "")
                ):
                    found.add(node["id"])
                if isinstance(node.get("children"), list):
                    stack.extend(node["children"])
        if found != set(categories):
            return None
        # 浏览器请求的是 APP_URL 的主机、协议的默认端口；部门页不在默认端口上的对不上，不认
        if page.port not in (None, 443 if page.scheme == "https" else 80):
            return None
        parts = tuple(
            ListApi(
                GKMLPT,
                urlunsplit(
                    (
                        page.scheme,
                        page.netloc,
                        f"{app.path.rstrip('/')}/gkmlpt/api/all/{category}",
                        f"page=1&sid={values['SID']}",
                        "",
                    )
                ),
                page_url.split("#", 1)[0],
                category,
                direct_only=parent_direct and index == 0,
            )
            for index, category in enumerate(categories)
        )
    except (ValueError, TypeError):
        return None
    # 条目里的相对网址按部门页解析；部门页网址（抓取的最终网址可能带着栏目的 #xxgk-gkmlpt-编号）
    # 去掉 # 后面，回链判断（url_key）才对得上
    first = parts[0]
    if len(parts) == 1:
        return first
    return ListApi(GKMLPT, first.url, first.base_url, first.category_id, parts, first.direct_only)


def find_list_api(html: str, page_url: str, column_url: str | None = None) -> ListApi | None:
    """栏目页里声明的列表接口；认不出返回 None。

    column_url 是数字广东的分类栏目时只按 gkmlpt 认（认不出就是 None，不再去认大汉）。"""
    if gkmlpt_categories(column_url) is not None:
        return find_gkmlpt_api(html, page_url, column_url)
    apis = find_unit_apis(html, page_url)
    return apis[0] if apis else None


def find_unit_apis(html: str, page_url: str) -> list[ListApi]:
    """页面里声明的全部大汉 jpaas 单元接口（当前栏目的列表排最前，其次导航等其他单元）。"""
    candidates: list[tuple[int, ListApi]] = []
    for attrs in _unit_scripts(html):
        # 认得出才认：属性正好是已知那 6 个（缓存里 714 个真实声明全是这样）
        if set(attrs) != _KNOWN_ATTRS:
            continue
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
        # 参数名正好是已知的组合（真实声明 686 个是那 7 个，28 个再加 unitType），值都是文字
        if (
            not isinstance(query, dict)
            or frozenset(map(str, query)) not in _KNOWN_QUERY_SETS
            or not all(isinstance(v, str) for v in query.values())
        ):
            continue
        query = {str(k): str(v) for k, v in query.items()}
        tag = query.get("tagId") or ""
        # 一页上常有几个占位（导航、侧栏）：当前栏目的列表优先
        rank = 0 if "list" in tag.lower() or "列表" in tag else 1
        if query.get("pageType") != "column":
            rank += 2
        # 地址原样照页面写的（协议、参数都不动，只去掉 # 后面），页面的参数接在后面，和浏览器
        # 脚本发的一样（D-128 读取护栏 ①：不改参数、不另外构造请求）
        base = declared._replace(fragment="").geturl()
        params = urlencode(query)
        url = base + ("&" if declared.query else "?") + params if params else base
        candidates.append((rank, ListApi(HANWEB_JPAAS, url, page_url)))
    candidates.sort(key=lambda c: c[0])
    out: list[ListApi] = []
    for _, api in candidates:
        if api not in out:  # 同一接口地址只要一次
            out.append(api)
    return out


def api_empty_list(api: ListApi, text: str) -> bool:
    """接口明确成功、过了护栏，列表那段在但是空的（这会儿没有条目，不是读不出）。"""
    if api.adapter == GKMLPT:
        payload = _json_payload(text)
        return payload is not None and not _gkmlpt_refusal(api, payload) and not payload["articles"]
    payload = _json_payload(text) if api.adapter == HANWEB_JPAAS else None
    if payload is None or _payload_refusal(payload):
        return False
    inner = payload.get("data")
    return isinstance(inner, dict) and isinstance(inner.get("html"), str) and not inner["html"]


def api_readable(api: ListApi, text: str) -> bool:
    """接口的返回是认得出、过了护栏的列表（条目可能是空的）。空白页、维护页这类护栏记"这次没取到"
    的返回不算：照样不读。"""
    if api.adapter == GKMLPT:
        payload = _json_payload(text)
        return payload is not None and not _gkmlpt_refusal(api, payload)
    return bool(api_html(api, text)) or api_empty_list(api, text)


def api_html(api: ListApi, text: str) -> str:
    """接口返回的内容里列表那段 HTML。不是预期格式，或按读取护栏（D-128）不该读的
    （返回不是成功、带权限要求）都返回空串。"""
    if api.adapter == HANWEB_JPAAS:
        payload = _json_payload(text)
        if payload is None or _payload_refusal(payload):
            return ""
        inner = payload.get("data")
        html = inner.get("html") if isinstance(inner, dict) else None
        return html if isinstance(html, str) else ""
    return ""


# 抓取状态：列表接口被读取护栏（D-128）拦下。和 robots 禁止一样记"不能抓"、停抓
LIST_API_REFUSED = "list_api_refused"
# 来源上"经接口读"的标记（parser_config 里，值是适配器名）：D-128 要求这类栏目每个都先复检、
# 抽查合格再打开——只在打开那一步（enable_sources）写上；正式抓取只对有标记的来源走接口，
# 不看页面内容推断"该读接口了"
LIST_API_OPEN_KEY = "list_api_open"
# 试读时经接口读出的栏目，导入清单的验证状态里带这几个字（打开时据此写标记）
VIA_API_WORD = "经接口读"
# 网站明确拒绝或要求登录的 HTTP 状态（和盘点的防护判断一致）
_REFUSE_HTTP = frozenset({401, 403, 412, 521})
# 跳转去的网址像登录、验证页面（跳转改了的主机名、路径、参数里的整词）
_LOGIN_URL = re.compile(
    r"(?:^|[/_.?=&-])(?:login|logon|signin|sso|cas|auth|passport|oauth2?|authorize|captcha|"
    r"verify(?:code)?)(?:$|[/_.?=&-])",
    re.I,
)
# 返回内容像登录、验证码或防护拦截页面（只看不是 JSON 的返回和失败提示，不看列表本身：
# 通知标题里也会有"验证码""身份认证"）
_LOGIN_TEXT = re.compile(
    r"请(?:先)?登[录陆]|(?:需要|需|未|重新)登[录陆]|登[录陆](?:后|超时|失效|过期)|用户登录|身份认证|"
    r"未授权|无权限|没有权限|权限不足|无权(?:访问|查看|阅读)|验证码|滑块|人机验证|安全验证|"
    r"captcha|geetest|unauthori[sz]ed|login required",
    re.I,
)
# 列表那段里有密码输入框：返回的是登录框，不是公开列表（11/12 查缓存里 616 次返回，一次也没有）
_PASSWORD_INPUT = re.compile(r"<input\b[^>]*type\s*=\s*[\"']?password", re.I)
# 认得出的列表里不会出现"登录""验证码"（616 次真实返回，条目标题和别处一次也没有）：出现了就
# 不读。链接地址不看：正常通知也会链到外站的 login.html（湖州门户就有一条直播通知这样写）
_LIST_HINT = re.compile(r"登[录陆]|验证码")
# 没权限一类说法（列表里出现同样不读）
_GATE_PHRASE = re.compile(
    r"(?:您)?(?:没有|无)(?:权限|权访问|权查看|权阅读|访问权限)|权限不足|未授权|人机验证|"
    r"请完成安全验证|滑块验证|拖动滑块"
)
_GATE_TEXT = re.compile(
    r"访问被拒绝|拒绝访问|禁止访问|访问受限|非法请求|防火墙|安全防护|访问被拦截|访问被阻断|已被拦截|"
    r"访问异常|异常访问|"
    r"访问过于频繁|请求过于频繁|access denied|forbidden",
    re.I,
)
_TAGS = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>|\s+", re.S | re.I)
# 维护页的说法：只认明写维护、升级、暂停服务的（"稍后再试""系统繁忙"拦截页也常这样写，不算）
_MAINTENANCE = re.compile(r"系统维护|升级中|暂停服务")
# 页面里有脚本、跳转、表单、内嵌页这类会动的东西：不当空白页、维护页（登录跳转、防护挑战页常这样）
# （包括写在外壳标签上的 onload 一类事件和 javascript: 链接）
_ACTIVE = re.compile(
    r"<(?:script|meta|iframe|frame|form|input|object|embed|noscript)\b|\bon[a-z]{3,}\s*=|javascript:",
    re.I,
)
# 空白页只剩这些外壳
_SHELL = re.compile(
    r"<!--.*?-->|</?(?:html|head|body|div|span|p|br|title|center|h\d)\b[^>]*>", re.S | re.I
)
# 去掉脚本、样式和标签（留着空白，"access denied"这类照样认得出）
_MARKUP = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>", re.S | re.I)
_LINK = re.compile(r"<a\s[^>]*href=", re.I)


def _bare_page(text: str) -> bool:
    """整页就是一个关卡：正文很少、链接也很少（和盘点的防护判断一样）。网站模板做的 404、
    维护页页头常有"用户登录""验证码"字样，链接多，不算。"""
    head = text[:200000]
    return len(_TAGS.sub("", head)) < 1500 and len(_LINK.findall(head)) < 15


# 权限一类字段：不为空就说明这段内容有阅读权限要求
_PERMISSION_KEY = re.compile(
    r"^(?:roles?|permissions?|authorit(?:y|ies)|privileges?|(?:need|require)s?_?login|"
    r"login_?required|auth(?:orization)?_?required)$",
    re.I,
)


def _json_payload(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _permission_fields(payload: dict) -> list[str]:
    """返回里不为空的权限一类字段（各层字典、列表都看，不限层数）。"""
    found: list[str] = []
    stack: list[tuple[str, object]] = [("", payload)]
    while stack:
        prefix, obj = stack.pop()
        items = obj.items() if isinstance(obj, dict) else enumerate(obj)  # type: ignore[arg-type]
        for key, value in items:
            name = f"{prefix}{key}"
            # 只有 JSON 里的空值算空（null、false、0、空串、空列表）；写成 "false" "null" 的
            # 字符串不算空，从严拦下（实测这两个字段都是 null）
            empty = value is None or value is False or value in ("", 0, [], {})
            if isinstance(key, str) and _PERMISSION_KEY.match(key) and not empty:
                found.append(name)
            elif isinstance(value, (dict, list)):
                stack.append((f"{name}.", value))
    return sorted(found)


# 返回里放提示文字的字段（哪个写了都看）
_MESSAGE_KEYS = ("message", "msg", "error", "errorMsg", "errMsg", "tip")


def _messages(payload: dict) -> str:
    return " ".join(
        str(payload[k]).strip() for k in _MESSAGE_KEYS if isinstance(payload.get(k), str)
    )


# 认得出才读（D-128）：只有和已知正常返回一模一样的才读。11/12 查缓存里 615 次成功返回，全是
# 这 8 个字段（3 次少了 messageType），roles、permissions 为 null，提示是"生成成功"，data 里只有
# html 一段；别的样子一律记不能抓，不靠一条条认登录提示的写法
_KNOWN_KEYS = frozenset(
    {"roles", "permissions", "success", "message", "code", "data", "traceId", "messageType"}
)
_NORMAL_MESSAGE = "生成成功"


def _unknown_shape(payload: dict) -> str:
    """不是已知的正常返回时说哪里不一样；一样给空串。"""
    extra = set(payload) - _KNOWN_KEYS
    if extra:
        return f"多了字段 {'、'.join(sorted(map(str, extra)))}"
    for key in ("roles", "permissions"):
        if key not in payload or payload[key] is not None:
            return f"{key} 不是 null"
    if payload.get("message") != _NORMAL_MESSAGE:
        return f"提示不是“{_NORMAL_MESSAGE}”"
    if "messageType" in payload and (
        type(payload["messageType"]) is not int or payload["messageType"] != 2
    ):
        return "messageType 不是 2"
    if not isinstance(payload.get("traceId"), str) or not payload["traceId"].strip():
        return "没有 traceId 或不是非空文字"
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {"html"} or not isinstance(data["html"], str):
        return "data 里不是只有一段列表 HTML"
    return ""


def _payload_refusal(payload: dict) -> str:
    """接口 JSON 返回的护栏：认得出（和已知正常返回一样、列表里没有登录提示）给空串，可以读；
    别的给原因，记不能抓、不读。"""
    success, code = payload.get("success"), payload.get("code")
    messages = _messages(payload)
    message = messages[:40]
    if success is not True or code != "200":
        if _LOGIN_TEXT.search(messages):
            return f"列表接口要登录或验证码（{message}）"
        parts = []
        if success is not True:
            flag = json.dumps(success, ensure_ascii=False)
            parts.append("没有成功标志" if success is None else f"success={flag}")
        if code != "200":
            parts.append("没有 code" if code is None else f"code={code}")
        if message:
            parts.append(message)
        return f"列表接口返回不是成功（{'，'.join(parts)}）"
    if _LOGIN_TEXT.search(messages):
        # 标着成功、提示却是要登录或没权限：自相矛盾，按要登录处理，不读
        return f"列表接口要登录或验证码（{message}）"
    fields = _permission_fields(payload)
    if fields:
        return f"列表接口返回带权限要求（{'、'.join(fields)} 不为空）"
    shape = _unknown_shape(payload)
    if shape:
        return f"列表接口返回认不出（{shape}）"
    html = payload["data"]["html"]
    plain = html_lib.unescape(html)
    if _PASSWORD_INPUT.search(plain):
        return "列表接口返回的是登录框"
    # 普通列表里不会出现"登录""验证码"（616 次真实返回里，条目标题和别处一次也没有）：
    # 出现了就不是认得出的列表，不读——不再一条条认登录提示的说法
    words = html_lib.unescape(_TAGS.sub("", html))
    hint = _LIST_HINT.search(words) or _GATE_PHRASE.search(words)
    if hint:
        return f"列表接口返回里有登录或权限提示（{hint.group(0)}）"
    return ""


def _gkmlpt_refusal(api: ListApi, payload: dict) -> str:
    """数字广东列表接口的 JSON 要不要记"不能抓"、不读（D-128 读取护栏 ②，认得出才读）。给原因；
    可以读给空串。

    平台脚本自己把 errcode 不为 0、errmessage 不是 ok 的当失败；带权限要求字段的不读；整份的形状
    要和实测的正常返回一样：顶层正好 articles、classify、offset、total、updated 五项，classify.id
    是请求的分类，offset 是 0（第 1 页），total 是非负整数，articles 是列表、不超过 _GKMLPT_MAX_ROWS
    条（正常一页 100 条）；有条目时至少要有一条带发布时间（create_time），一条都没有多半是平台改了
    字段，认不出。单条字段不对的（缺标题、网址、发布时间）只跳过那一条或记成没日期（api_items），
    不拖累整栏。"""
    code, message = payload.get("errcode"), payload.get("errmessage")
    if code not in (None, "", 0) or ("errmessage" in payload and message != "ok"):
        hint = str(message or code)[:40]
        if _LOGIN_TEXT.search(hint):
            return f"列表接口要登录或验证码（{hint}）"
        return f"列表接口返回不是成功（{hint}）"
    fields = _permission_fields(payload)
    if fields:
        return f"列表接口返回带权限要求（{'、'.join(fields)} 不为空）"
    articles, classify = payload.get("articles"), payload.get("classify")
    if (
        set(payload) != {"articles", "classify", "offset", "total", "updated"}
        or not isinstance(articles, list)
        # 有条目却没有一条带发布时间：平台多半改了字段，认不出（不悄悄全记成没日期）
        or (articles and not any(_publish_time(row) for row in articles))
        or len(articles) > _GKMLPT_MAX_ROWS
        or not isinstance(classify, dict)
        or type(classify.get("id")) is not int
        or classify["id"] != api.category_id
        or type(payload["offset"]) is not int
        or payload["offset"] != 0
        or type(payload["total"]) is not int
        or payload["total"] < 0
    ):
        return "列表接口返回认不出"
    return ""


# 数字广东第 1 页正常 100 条；多得离谱的不是认得出的那种返回
_GKMLPT_MAX_ROWS = 500
# 数字广东接口的 create_time 是发布时间的 Unix 秒数（平台列表"发布日期"一栏），按北京时间换成日子
_BEIJING = timezone(timedelta(hours=8))
# 标题里夹的网页标签（"一图速览<strong> |</strong> ……"）：只认 < 后面紧跟字母或 / 的，
# "金额<5万元且>3万元"这类正文字符照留
_TITLE_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def _publish_time(row: object) -> int | None:
    """数字广东一条的发布时间（create_time，Unix 秒数）；没有、不是正整数的给 None。"""
    value = row.get("create_time") if isinstance(row, dict) else None
    return value if type(value) is int and value > 0 else None


def _gkmlpt_item(api: ListApi, row: object, today: date) -> ListItem | None:
    """数字广东接口的一条 → 列表条目；字段不对、不像标题、网址不能用的给 None（跳过这一条）。"""
    if not isinstance(row, dict):
        return None
    if api.direct_only and not (
        type(row.get("classify_main")) is int and row["classify_main"] == api.category_id
    ):
        return None  # 父分类：挂在子分类（解读等）上的不要
    title, raw_url, published = row.get("title"), row.get("url"), _publish_time(row)
    if not isinstance(title, str) or not isinstance(raw_url, str):
        return None
    title = clean_title(" ".join(html_lib.unescape(_TITLE_TAG.sub("", title)).split()))
    if not _looks_like_title(title) or not raw_url.strip():
        return None  # 空网址按部门页解析会变成部门页自己，不要
    try:
        url = normalize_url(api.base_url, raw_url.strip())
        # 端口写坏的（:99999）、括号没合上的（http://[bad）网址存不进库，跳过这一条
        _ = urlsplit(url).port if url else None
        # 指回部门页自己的（"政府信息公开"外链）不是文件；按规范网址比，只差 http、https 的也算
        if not url or url_key(url) == url_key(api.base_url):
            return None
    except ValueError:
        return None
    day = None
    if published is not None:
        try:
            d = datetime.fromtimestamp(published, _BEIJING).date()
        except (OverflowError, OSError, ValueError):
            d = None
        day = _safe_date(d.year, d.month, d.day, today) if d else None
    return ListItem(title, url, day)


def api_items(
    api: ListApi, text: str, parser_config: dict | None, today: date | None
) -> list[ListItem]:
    """接口返回里的条目（两个适配器都用；过不了读取护栏的给空列表）。

    大汉 jpaas：列表那段 HTML 照列表页的识别规则读。数字广东 gkmlpt：每一条按字段读（_gkmlpt_item）
    ——标题去掉标签、还原字符实体、空白合成一个，再按列表页的规则清理，不像标题的不要；网址去掉
    前后空白后按部门页解析，空的、解析成部门页自己的、端口写坏的不要（外链照收，和列表页上的外链
    一样）；发布日期取 create_time 换成北京时间的日子，再过列表页同一个合理性规则（2000 年以前、
    比今天晚 2 天以上的当没有日期）；同一网址只留一次，顺序照接口。"""
    if api.adapter == HANWEB_JPAAS:
        return extract_items(api_html(api, text), api.base_url, parser_config, today=today)
    payload = _json_payload(text)
    if api.adapter != GKMLPT or payload is None or _gkmlpt_refusal(api, payload):
        return []
    items, seen = [], set()
    for row in payload["articles"]:
        item = _gkmlpt_item(api, row, today or date.today())
        if item is not None and item.url not in seen:
            items.append(item)
            seen.add(item.url)
    return items


def _redirect_words(api_url: str, final: str) -> str:
    """跳转改了什么：主机名变了算主机名，路径或参数变了算路径和参数。只换协议的不算跳转
    （如 http 跳 https 落在 *.cas.cn 这类主机上，不能当成跳到登录）；只是写不写协议默认端口的
    （https://x:443 和 https://x）也不算，和抓取程序认网站的口径一样（host_of）。"""
    a, b = urlsplit(api_url), urlsplit(final)
    host_a, host_b = host_of(api_url), host_of(final)
    words = []
    if host_a != host_b:
        words.append(b.netloc)
    if (host_a, a.path, a.query) != (host_b, b.path, b.query):
        words.append(f"{b.path}?{b.query}" if b.query else b.path)
    return " ".join(words)


def _retry_later(text: str) -> bool:
    """不是 JSON 的返回里，算"这次没取到、下次再试"（照样不读）的：整页很少字、链接也少（不是网站
    模板），没有脚本、跳转、表单这类会动的东西，没有任何登录、验证、权限、拦截字样，也不是防护页，
    而且是真空白或明写维护的页面。别的认不出的返回一律记不能抓，免得把关卡当成维护页反复去请求。"""
    from youju.govsites.platform import blocker

    head = text[:200000]
    if not _bare_page(head) or _ACTIVE.search(head) or _PASSWORD_INPUT.search(head):
        return False
    plain = html_lib.unescape(_MARKUP.sub(" ", head))
    if any(p.search(plain) for p in (_LOGIN_TEXT, _GATE_TEXT, _GATE_PHRASE, _LIST_HINT)):
        return False
    if blocker("ok", 200, head):
        return False
    blank = not html_lib.unescape(_SHELL.sub("", head)).strip()
    return blank or bool(_MAINTENANCE.search(plain))


def api_refusal(api: ListApi, result) -> str:
    """读取护栏（D-128）：列表接口这次的返回要不要记"不能抓"、不读。给原因；可以读给空串。

    result 是抓取结果（FetchResult：status、http_status、final_url、text）。网络不通、超时、
    5xx 这类"这次没取到"不算，照常记失败、下次再试。"""
    if api.adapter not in (HANWEB_JPAAS, GKMLPT):
        return ""
    status = getattr(result, "status", "")
    http_status = getattr(result, "http_status", None)
    if status == "robots_disallowed":
        return "列表接口 robots.txt 禁止"
    if http_status in _REFUSE_HTTP:
        return f"列表接口 HTTP {http_status}：拒绝访问或要求登录"
    if status in ("network_error", "deadline", "ssl_error", "robots_error", "too_large"):
        return ""  # 网络不通、超时这类：这次没取到，照常记失败、下次再试
    final = getattr(result, "final_url", None) or api.url
    words = _redirect_words(api.url, final)
    if words:
        # 跳到了页面没写的地址（只换协议的不算）：不跟着读（D-128 读取护栏 ①），记不能抓
        if _LOGIN_URL.search(words):
            return "列表接口跳到登录或验证页面"
        return "列表接口跳到了页面没写的地址"
    if status != "ok":
        return ""
    text = getattr(result, "text", "") or ""
    payload = _json_payload(text)
    if payload is None:
        # 不是 JSON：认不出，记不能抓、不读（认得出才读）；原因里分一下是不是登录、拦截关卡
        head = html_lib.unescape(text[:20000])
        bare = _bare_page(text)
        if bare and (_LOGIN_TEXT.search(head) or _PASSWORD_INPUT.search(head)):
            return "列表接口要登录或验证码"
        if bare and (_GATE_TEXT.search(head) or _GATE_PHRASE.search(head)):
            return "列表接口返回拦截页面"
        if _retry_later(text):
            return ""
        return "列表接口返回认不出（不是 JSON）"
    return _gkmlpt_refusal(api, payload) if api.adapter == GKMLPT else _payload_refusal(payload)


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
