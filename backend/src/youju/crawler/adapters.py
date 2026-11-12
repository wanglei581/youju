"""平台适配器：栏目页上读不出条目，但页面自己写明了列表接口时，按页面给的参数取列表。

政府网站集约化平台把列表交给浏览器脚本去取，页面里只留一个"占位 + 接口地址 + 参数"。同一个平台
的几百个网站写法完全一样，认出一种写法就能读一个平台（全国覆盖第一阶段，见
docs/数据来源/全国覆盖/集约化平台盘点.md）。

规矩和抓页面一样：
- 只用页面自己声明的、同一网站上的公开接口，只发 GET；照常遵守 robots.txt、同一网站间隔 3 秒；
- 只认白名单里的接口路径，不拼别的地址；要登录、签名、验证码的接口一律不碰。
- 读取护栏（D-128）：
  ① 只按网页里写明的地址和参数去取，不改参数、不另外构造请求（find_unit_apis 只认白名单路径、
    同一网站，参数原样照页面的 queryData，不加分页、检索等参数）；
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
"""

import ast
import html as html_lib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

import lxml.html
from lxml import etree

HANWEB_JPAAS_PATH = "/api-gateway/jpaas-publish-server/front/page/build/unit"


@dataclass(frozen=True)
class ListApi:
    adapter: str  # 适配器名
    url: str  # 接口网址（和栏目页同一网站）
    base_url: str  # 接口返回的列表里的相对链接，以栏目页为准


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


def find_list_api(html: str, page_url: str) -> ListApi | None:
    """栏目页里声明的列表接口；认不出返回 None。"""
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
        candidates.append((rank, ListApi("大汉jpaas", url, page_url)))
    candidates.sort(key=lambda c: c[0])
    out: list[ListApi] = []
    for _, api in candidates:
        if api not in out:  # 同一接口地址只要一次
            out.append(api)
    return out


def api_empty_list(api: ListApi, text: str) -> bool:
    """接口明确成功、过了护栏，列表那段在但是空的（这会儿没有条目，不是读不出）。"""
    payload = _json_payload(text) if api.adapter == "大汉jpaas" else None
    if payload is None or _payload_refusal(payload):
        return False
    inner = payload.get("data")
    return isinstance(inner, dict) and isinstance(inner.get("html"), str) and not inner["html"]


def api_html(api: ListApi, text: str) -> str:
    """接口返回的内容里列表那段 HTML。不是预期格式，或按读取护栏（D-128）不该读的
    （返回不是成功、带权限要求）都返回空串。"""
    if api.adapter == "大汉jpaas":
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
    r"(?:您)?(?:没有|无)(?:权限|权访问|权查看|权阅读|访问权限)|权限不足|未授权|人机验证"
)
_GATE_TEXT = re.compile(
    r"访问被拒绝|拒绝访问|禁止访问|访问受限|非法请求|防火墙|安全防护|access denied|forbidden",
    re.I,
)
_TAGS = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>|\s+", re.S | re.I)
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
    if not isinstance(payload.get("traceId"), str):
        return "没有 traceId 或不是文字"
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


def _redirect_words(api_url: str, final: str) -> str:
    """跳转改了什么：主机名变了算主机名，路径或参数变了算路径和参数。只换协议的不算跳转
    （如 http 跳 https 落在 *.cas.cn 这类主机上，不能当成跳到登录）。"""
    a, b = urlsplit(api_url), urlsplit(final)
    words = []
    if a.netloc.lower() != b.netloc.lower():
        words.append(b.netloc)
    if (a.netloc.lower(), a.path, a.query) != (b.netloc.lower(), b.path, b.query):
        words.append(f"{b.path}?{b.query}" if b.query else b.path)
    return " ".join(words)


def api_refusal(api: ListApi, result) -> str:
    """读取护栏（D-128）：列表接口这次的返回要不要记"不能抓"、不读。给原因；可以读给空串。

    result 是抓取结果（FetchResult：status、http_status、final_url、text）。网络不通、超时、
    5xx 这类"这次没取到"不算，照常记失败、下次再试。"""
    if api.adapter != "大汉jpaas":
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
        # 不是 JSON：认不出，一律记不能抓、不读（认得出才读）；原因里分一下是不是登录、拦截关卡
        head = html_lib.unescape(text[:20000])
        if _bare_page(text) and (_LOGIN_TEXT.search(head) or _PASSWORD_INPUT.search(head)):
            return "列表接口要登录或验证码"
        if _bare_page(text) and _GATE_TEXT.search(head):
            return "列表接口返回拦截页面"
        return "列表接口返回认不出（不是 JSON）"
    return _payload_refusal(payload)


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
