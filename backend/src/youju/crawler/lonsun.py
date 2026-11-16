"""龙讯（安徽集约化平台）信息公开栏目的列表适配器：认页面（find_lonsun_api）、定请求
（resolve_lonsun）两步，脚本一律不执行——栏目树只按 JSON 文本解析，内联脚本只用受限求值器。

协调会话 11/13 的两条口径：
1. D-013 的读法：列表接口是页面自己的 PublicTree 脚本用页面写明的值拼出的 GET 请求，照它的拼法
   得到的地址和浏览器发的只少不多，不算另外构造请求；要 POST、令牌、签名或者要绕过防护的一律
   不读、先问；robots 不许的不读。
2. 栏目树脚本六个条件：只读页面原样写明的那一个 src（document.write 字面量后面防缓存的随机数
   不加，字面量不改不补）；主机在 gov.cn 下、和栏目页同一个市的政府网站域名，区县站只认审过的
   本市门户白名单（LONSUN_CITY_PORTALS，来源配置的门户得正是来源所在城市的那个）；守脚本主机
   自己的 robots，读不到按不许；门户按一个站守限速，同一地址每轮只取一次；只按文本解析、不执行；
   写进适配器说明、部门目录规则和 PR。

认不出的一律不读，原因写进"待人工处理"；认页面的规则见 docs/数据来源/全国覆盖/部门目录规则.md。

浏览器会不会真的发出列表请求（第七、八轮审阅）：
- 发列表请求之前出错，浏览器就不发。照栏目树算的：setSelect、preprocessData 读节点字段的成员、
  显示回调给栏目名折行（getNewline）；照真实写法逐个记号比对的：建树设置里建树时就跑的显示回调
  （fontCss、addDiyDom）、open 里发列表请求之前跑的显示方法（setMeta、getNav、getGd）、包住
  setSelect 的 try/catch（catch 要真把异常吞掉）。别的加载时会跑的方法（bindEvt、preprocessData、
  initTree 其余部分、fuzzySearch、getReportyear）只查改了什么、调了什么，不逐句推算会不会出错——
  前提是真实页面在浏览器里能跑（出错的页面用户也看不到列表）。
- 芜湖这类 init 里 me.getPublicQcsmCatIds().done(getData)：浏览器等那个请求成功（回 2xx、按
  JSON 解析得了、done 里的回调不出错）才发列表请求。我们也先发它（同一网站、页面写明的路径），
  照同样的条件看结果，不成功就不发列表请求（被拒记不能抓，读不出记这次没取到）。

页面脚本会不会改到我们照着推请求用的东西（第九到十一轮审阅）：查名字——直接写的、前面带 window.、
this. 这类全局对象的、写入路径里任何一段是页面依赖的全局名字的；查别名——$、jQuery、Ls、内置对象、
window 和函数里的 this 都不许整个当值用（只认 this.名字、$(this)、var X = this）；查看不见的写法——
解构、for-in/of、复合赋值、按算出来的名字取全局对象、函数里的 this、调用结果、下标结果、document 的
成员。前提：经事件对象（e.view、e.currentTarget 这类）、jQuery 包装对象取回原对象（$(x)[0]、
.get(0)、.each 回调的参数）或别的 DOM 属性拿到 window、再按算出来的名字改全局的写法不追踪——只有专门
构造才会出现，157 个真实页面一处都没有；照名字改的（e.view.jQuery.param = …）照样拦。
"""

import collections
import html
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date
from functools import cached_property, lru_cache
from urllib.parse import parse_qsl, unquote, urlsplit

from youju.crawler.dates import _safe_date
from youju.crawler.listing import ListItem, _looks_like_title
from youju.crawler.titles import clean_title
from youju.crawler.urls import normalize_url, url_key

# 依据 2026-11-13 区县栏目页实测；新增城市须先审查同城集约化平台关系。
# robots.txt 的 4xx 按 RFC 9309 视为没有规则；5xx、网络错误和超时按不可读处理。
LONSUN_CITY_PORTALS = {("安徽", "芜湖"): "www.wuhu.gov.cn", ("安徽", "淮南"): "www.huainan.gov.cn"}


def _odd_url(text: str) -> bool:
    """网址里只认可见的 ASCII 字符（不含反斜杠）。浏览器解析网址前去掉首尾空白和控制字符、删掉中间
    的换行和制表符、把反斜杠当斜杠（\\x.example/… 是别的主机）、空格和汉字转成 %xx；urljoin 都不
    这样做，两边拼出来的地址、读到的 location.href 可能不一样。"""
    return any(c == "\\" or not "!" <= c <= "~" for c in text)


def _words(text):
    """照写法逐个记号比对用的模板：记号之间用空格隔开写。"""
    return text.split()


def city_catalog_host(province: object, city: object, configured: object) -> str | None:
    """正式抓取用的本市门户：来源配置的（parser_config.lonsun_catalog_host，启用时写）必须正是
    白名单里来源所在城市的那个门户；城市缺失、配置缺失或对不上的都不认（协调会话 11/13：记待人工
    处理），不临时推断。"""
    expected = LONSUN_CITY_PORTALS.get((province, city)) if province and city else None
    return expected if expected is not None and configured == expected else None


class LonsunFormatError(ValueError):
    """返回不符合已确认的平台格式。"""


def column_spec(url: str) -> tuple[str, str] | None:
    """只接受页面已确认的公开栏目网址及参数。"""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            return None
        _ = parts.port
        if "%" in parts.query or "+" in parts.query:
            # 平台按 location.search 原文取参数，我们按解码后的取：编码过的认不准
            return None
        match = re.search(r"/public/column/([0-9]+)$", parts.path)
        if not match:
            return None
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        if (
            len(params) != len(pairs)
            or set(params) - {"type", "action", "catId", "nav", "sub", "isParent"}
            or params.get("type") != "4"
            or params.get("action") != "list"
            or not re.fullmatch(r"[0-9]+", params.get("catId", ""))
            or ("nav" in params and not re.fullmatch(r"[0-9]*", params["nav"]))
            or ("sub" in params and params["sub"] != "")
            or ("isParent" in params and params["isParent"] not in ("true", "false"))
        ):
            return None
        return match.group(1), params["catId"]
    except ValueError:
        return None


# 省级政府网站域名（xx.gov.cn 的 xx）：底下是省里不同部门的网站，不能当同一个市互相取栏目树
_PROVINCE_LABELS = frozenset(
    {
        "beijing",
        "bj",
        "tj",
        "hebei",
        "shanxi",
        "nmg",
        "ln",
        "jl",
        "hlj",
        "shanghai",
        "sh",
        "jiangsu",
        "js",
        "zj",
        "ah",
        "fujian",
        "fj",
        "jiangxi",
        "jx",
        "shandong",
        "sd",
        "henan",
        "hubei",
        "hunan",
        "gd",
        "gxzf",
        "gx",
        "hainan",
        "cq",
        "sc",
        "guizhou",
        "yn",
        "xizang",
        "shaanxi",
        "gansu",
        "qinghai",
        "nx",
        "xinjiang",
        "xj",
    }
)


def catalog_host_allowed(
    page_url: str, script_url: str, catalog_host: str | None = None, *, county: bool = False
) -> bool:
    """栏目树允许同主机，或同一个市政府域名下的主机（协调会话 11/13 口径 2）。区县站（county）
    跨主机只认来源配置的本市白名单门户（口径 3），不按同城域名放行。"""
    try:
        page, script = urlsplit(page_url), urlsplit(script_url)
        if (
            script.scheme not in ("http", "https")
            or not page.hostname
            or not script.hostname
            or script.username is not None
            or script.password is not None
        ):
            return False
        page_port, script_port = page.port, script.port
        if script_port not in (None, {"http": 80, "https": 443}[script.scheme], page_port):
            return False
        a, b = page.hostname.lower(), script.hostname.lower()
        # 来源配置的本市门户：还得在审过的白名单里（配置写错、白名单撤掉的不认）
        if catalog_host and b == catalog_host and b in LONSUN_CITY_PORTALS.values():
            return True
        if a == b:
            return a.endswith(".gov.cn")
        if county:
            return False
        a_parts, b_parts = a.split("."), b.split(".")
        return (
            len(a_parts) >= 4
            and len(b_parts) >= 4
            and a_parts[-2:] == b_parts[-2:] == ["gov", "cn"]
            and a_parts[-3] == b_parts[-3]
            # ah.gov.cn 这类是省的域名，底下是省里各个部门的网站，不是同一个市
            and a_parts[-3] not in _PROVINCE_LABELS
        )
    except ValueError:
        return False


def _reject_constant(name: str):
    raise ValueError(f"JSON 里不该有 {name}")


def _strict_json(text: str):
    """严格按 JSON 解析：Python 自带的 json 还认 NaN、Infinity，JSON 里没有，认不出。"""
    return json.loads(text, parse_constant=_reject_constant)


def parse_catalog(text: str, organ_id: str) -> list[dict]:
    """只接受页面对应机构的单个 JSON 栏目树，并拒绝重复编号。"""
    if not re.fullmatch(r"[0-9]+", organ_id):
        raise LonsunFormatError("栏目树机构编号无效")
    match = re.fullmatch(
        r"var\s+catalogs\s*=\s*\{\s*([0-9]+)\s*:\s*(\[.*\])\s*\}\s*;?",
        text.strip().lstrip("\ufeff").strip(),
        re.DOTALL,
    )
    if not match or match.group(1) != organ_id:
        raise LonsunFormatError("栏目树脚本格式或机构编号不符")
    try:
        nodes = _strict_json(match.group(2))
    except (ValueError, RecursionError) as exc:  # 嵌套太深也当认不出
        raise LonsunFormatError("栏目树 JSON 无效") from exc
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict) or type(node.get("id")) is not int for node in nodes
    ):
        raise LonsunFormatError("栏目树节点编号无效")
    for node in nodes:
        # zTree 按 parentId 原样当键找上级：写成字符串、带前导零的找不到，当成根；节点自带
        # children（哪怕是空数组）在浏览器里就算有下级。这两种都认不准
        if "children" in node:
            raise LonsunFormatError("栏目树节点自带 children")
        if node.get("parentId") is not None and type(node.get("parentId")) is not int:
            raise LonsunFormatError("栏目树节点的上级编号不是整数")
    ids = [node["id"] for node in nodes]
    if len(ids) != len(set(ids)):
        raise LonsunFormatError("栏目树节点编号重复")
    return nodes


_LIST_KEYS = {"data", "pageCount", "pageIndex", "pageSize", "startNumber", "total"}
_CITY_KEYS = {
    "catId",
    "name",
    "link",
    "redirectLink",
    "childPublicContentList",
    "childDataList",
    "isParent",
    "parent",
}
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _published(row: object) -> date | None:
    value = row.get("publishDate") if isinstance(row, dict) else None
    if not isinstance(value, str) or not _DATE.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _city_rows(nodes: object, *, nested: bool = False, excluded: tuple[int, ...] = ()) -> list:
    if not isinstance(nodes, list):
        raise LonsunFormatError("下级栏目不是列表")
    rows = []
    for node in nodes:
        if (
            not isinstance(node, dict)
            or set(node) - _CITY_KEYS
            or type(node.get("catId")) is not int
            or not isinstance(node.get("name"), str)
            or type(node.get("redirectLink")) is not bool
            or any(type(node[key]) is not bool for key in ("isParent", "parent") if key in node)
            or (nested and "childDataList" in node)
            or ("childDataList" in node and "childPublicContentList" in node)
        ):
            raise LonsunFormatError("下级栏目格式无效")
        if not nested and node["catId"] in excluded:
            continue
        if node["redirectLink"]:
            continue
        if "childDataList" in node:
            rows.extend(_city_rows(node["childDataList"], nested=True))
        elif "childPublicContentList" in node:
            children = node["childPublicContentList"]
            if not isinstance(children, list):
                raise LonsunFormatError("下级栏目条目不是列表")
            rows.extend(children)
        if len(rows) > 500:
            raise LonsunFormatError("下级栏目条目超过 500 条")
    return rows


def parse_response(
    text: str,
    mode: str,
    base_url: str,
    today: date,
    excluded_children: tuple[int, ...] = (),
) -> tuple[list[ListItem], bool]:
    """读取已取得的 JSON；第二项表示接口明确返回空列表。"""
    try:
        payload = _strict_json(text.lstrip("\ufeff").strip())
    except (ValueError, RecursionError) as exc:  # 嵌套太深也当认不出
        raise LonsunFormatError("列表接口 JSON 无效") from exc
    if mode == "list":
        if (
            not isinstance(payload, dict)
            or set(payload) != _LIST_KEYS
            or not isinstance(payload["data"], list)
            or type(payload["pageSize"]) is not int
            or payload["pageSize"] < 0
            or len(payload["data"]) > min(payload["pageSize"], 100)
            or type(payload["total"]) is not int
            or payload["total"] < 0
        ):
            raise LonsunFormatError("列表接口格式无效")
        rows = payload["data"]
        if rows and not any(_published(row) for row in rows):
            raise LonsunFormatError("列表没有可解析的发布日期")
    elif mode == "city_page":
        rows = _city_rows(payload, excluded=excluded_children)
    else:
        raise LonsunFormatError("未知列表模式")
    items: list[ListItem] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title, link = row.get("title"), row.get("link")
        if not isinstance(title, str) or not isinstance(link, str) or not link.strip():
            continue
        title = clean_title(" ".join(html.unescape(_TAG.sub("", title)).split()))
        if not _looks_like_title(title):
            continue
        try:
            url = normalize_url(base_url, link.strip())
            if not url:
                continue
            _ = urlsplit(url).port
            key = url_key(url)
            if key == url_key(base_url) or key in seen:
                continue
        except ValueError:
            continue
        published = _published(row)
        day = (
            _safe_date(published.year, published.month, published.day, today) if published else None
        )
        items.append(ListItem(title, url, day))
        seen.add(key)
    return items, not rows


# 词法切分保留原文偏移，未执行的方法也必须完整跨过，不能让字符串里的括号影响结构。
@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int


_JS_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|(?:\d+(?:\.\d*)?|\.\d+)")
_TEMPLATE_NAME = re.compile(r"\s*[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*\s*")


_COMPOUND_ASSIGNMENTS = (">>>=", "**=", "<<=", ">>=", "&&=", "||=", "??=", "|=", "&=", "^=")


def _js_tokens(source: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    i = 0
    operators = (
        "===",
        "!==",
        ">>>",
        "=>",
        "==",
        "!=",
        "<=",
        ">=",
        "&&",
        "||",
        "+=",
        "-=",
        "++",
        "--",
        "*=",
        "/=",
        "%=",
        "<<",
        ">>",
        "**",
        "?.",
        "??",
    )
    regex_after = {
        "(",
        "[",
        "{",
        ",",
        ":",
        ";",
        "=",
        "==",
        "===",
        "!=",
        "!==",
        "!",
        "&&",
        "||",
        "?",
        "return",
        "case",
        "=>",
        # 运算符、关键字后面也是表达式开头：/ 是正则
        "+",
        "-",
        "*",
        "%",
        "&",
        "|",
        "^",
        "~",
        "<",
        ">",
        "<=",
        ">=",
        "+=",
        "-=",
        "*=",
        "/=",
        "%=",
        "<<",
        ">>",
        ">>>",
        "**",
        "??",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "else",
        "do",
        "yield",
        "await",
    }
    # 每个没合上的 (：前面是不是 if、while、for、with；这种括号合上以后接的是语句，/ 是正则
    keyword_parens: list[bool] = []
    closed_keyword = False
    while i < len(source):
        start = i
        c = source[i]
        if c.isspace() or c == "\ufeff":
            i += 1
            continue
        if source.startswith("<!--", i) or (
            source.startswith("-->", i) and not source[source.rfind("\n", 0, i) + 1 : i].strip()
        ):
            # 浏览器把 <!--、行首的 --> 当成单行注释（附录 B），我们照运算符切：两边不一样
            raise LonsunFormatError("脚本里有 <!-- 或 --> 这种注释写法，认不准")
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise LonsunFormatError("JavaScript 注释未闭合")
            i = end + 2
            continue
        if c in "'\"`":
            quote = c
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise LonsunFormatError("JavaScript 字符串未闭合")
            if quote == "`" and "${" in source[start:i]:
                # 模板字符串里的 ${…} 会执行：只认插一个变量名或成员访问（${paramName}、${a.b}），
                # 别的写法（调用、赋值……）不分析
                body = source[start + 1 : i - 1]
                parts = body.split("${")[1:]
                if not all(
                    "}" in part and _TEMPLATE_NAME.fullmatch(part.split("}", 1)[0])
                    for part in parts
                ):
                    raise LonsunFormatError("模板字符串的 ${…} 里不只是变量名，我们不分析")
        elif (
            c == "/"
            and tokens
            and tokens[-1].value == "}"
            and not source.startswith(("//", "/*"), i)
        ):
            # } 后面的 / 是除号还是正则要看前面是代码块还是对象字面量：认不准，不读
            raise LonsunFormatError("} 后面的 / 认不出是除号还是正则")
        elif c == "/" and (
            not tokens
            or tokens[-1].value in regex_after
            # o.in / 2：点号后面的关键字是属性名，后面的 / 是除号
            and not (
                len(tokens) > 1
                and tokens[-2].value in (".", "?.")
                and _IDENT.fullmatch(tokens[-1].value)
            )
            or tokens[-1].value == ")"
            and closed_keyword
        ):
            i += 1
            in_class = False
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == "[":
                    in_class = True
                    i += 1
                elif source[i] == "]":
                    in_class = False
                    i += 1
                elif source[i] == "/" and not in_class:
                    i += 1
                    while i < len(source) and source[i].isalpha():
                        i += 1
                    break
                elif source[i] in "\r\n":
                    raise LonsunFormatError("JavaScript 正则未闭合")
                else:
                    i += 1
            else:
                raise LonsunFormatError("JavaScript 正则未闭合")
        else:
            if c == "\\" or ord(c) > 127:
                # 非 ASCII 的名字、\u0061 这样转义的名字：我们按 ASCII 切，别名、改写都跟不上
                raise LonsunFormatError("脚本里有非 ASCII 或转义写的名字，认不准")
            match = _JS_WORD.match(source, i)
            if match:
                i = match.end()
            else:
                if source.startswith(_COMPOUND_ASSIGNMENTS, i):
                    # 只认 =、+= 这几种赋值（_ASSIGN_OPS）；|=、&&= 这类会切成两个记号，改了什么就
                    # 看不见了（真实页面上没有）
                    raise LonsunFormatError("脚本里有 |=、&&= 这类复合赋值，改了什么看不全")
                op = next((op for op in operators if source.startswith(op, i)), c)
                i += len(op)
        value = source[start:i]
        if value == "(":
            keyword_parens.append(
                bool(tokens) and tokens[-1].value in ("if", "while", "for", "with")
            )
        elif value == ")":
            closed_keyword = keyword_parens.pop() if keyword_parens else False
        tokens.append(_Token(value, start, i))
    return _Tokens(tokens)


class _Tokens(tuple):
    """_js_tokens 的结果：values 是各记号的文字，只算一次（别名检查里会对同一段脚本反复取，
    每次重建整份列表在大脚本上是平方级）。拿到的列表只读，不要改。"""

    @cached_property
    def values(self):
        return [token.value for token in self]


def _values(tokens):
    return tokens.values if isinstance(tokens, _Tokens) else [t.value for t in tokens]


def _matching_token(tokens: tuple[_Token, ...], start: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    if start >= len(tokens) or tokens[start].value not in pairs:
        raise LonsunFormatError("JavaScript 缺少开始括号")
    stack = [pairs[tokens[start].value]]
    for i in range(start + 1, len(tokens)):
        value = tokens[i].value
        if value in pairs:
            stack.append(pairs[value])
        elif value in pairs.values():
            if not stack or value != stack.pop():
                raise LonsunFormatError("JavaScript 括号不匹配")
            if not stack:
                return i
    raise LonsunFormatError("JavaScript 括号未闭合")


def _public_tree_methods(source: str) -> tuple[tuple[str, str, str], ...]:
    """切分原型对象；结果依次为方法名、参数原文、方法体原文。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    marker = ["$", ".", "extend", "(", "PublicTree", ".", "prototype", ",", "{"]
    positions = [
        i for i in range(len(tokens) - len(marker) + 1) if values[i : i + len(marker)] == marker
    ]
    if len(positions) != 1:
        raise LonsunFormatError("PublicTree 原型对象数量不对")
    obj = positions[0] + len(marker) - 1
    end = _matching_token(tokens, obj)
    if end + 1 >= len(tokens) or values[end + 1] != ")":
        raise LonsunFormatError("PublicTree 原型调用格式不对")
    methods = []
    seen = set()
    i = obj + 1
    while i < end:
        if (
            i + 3 >= end
            or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", values[i])
            or values[i + 1 : i + 4] != [":", "function", "("]
        ):
            raise LonsunFormatError("PublicTree 原型方法格式不对")
        name = values[i]
        if name in seen:
            raise LonsunFormatError("PublicTree 原型方法重复")
        seen.add(name)
        args_start = i + 3
        args_end = _matching_token(tokens, args_start)
        body_start = args_end + 1
        if body_start >= end or values[body_start] != "{":
            raise LonsunFormatError("PublicTree 方法体格式不对")
        body_end = _matching_token(tokens, body_start)
        if body_end >= end:
            raise LonsunFormatError("PublicTree 方法体超出原型对象")
        methods.append(
            (
                name,
                source[tokens[args_start].end : tokens[args_end].start],
                source[tokens[body_start].end : tokens[body_end].start],
            )
        )
        i = body_end + 1
        if i < end:
            if values[i] != ",":
                raise LonsunFormatError("PublicTree 方法之间缺少逗号")
            i += 1
    return tuple(methods)


class _Special:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return self.name


UNDEFINED = _Special("undefined")
UNKNOWN = _Special("UNKNOWN")


def _number_literal(token: str) -> float:
    """数字字面量：0 开头的（010 在 JS 里是八进制）和超过 15 位的整数部分（超出双精度能精确表示的
    范围）认不出。"""
    if re.fullmatch(r"0[0-9]+(?:\.[0-9]*)?", token) or len(token.split(".")[0]) > 15:
        raise LonsunFormatError(f"数字写法认不出 {token}")
    return float(token)


def _string_literal(value: str) -> str:
    result = []
    i = 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while i < len(value) - 1:
        c = value[i]
        i += 1
        if c == "\\":
            if i >= len(value) - 1:
                raise LonsunFormatError("字符串转义不完整")
            c = value[i]
            i += 1
            if c in "ux":
                size = 4 if c == "u" else 2
                digits = value[i : i + size]
                if len(digits) != size or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise LonsunFormatError("字符串转义无效")
                c = chr(int(digits, 16))
                i += size
            elif c in "\r\n":
                if c == "\r" and i < len(value) - 1 and value[i] == "\n":
                    i += 1
                continue
            elif c in "1234567" or c == "0" and value[i : i + 1] in set("0123456789"):
                # 非严格模式下是八进制（'\1' 是 \x01），严格模式下是语法错误：认不准
                raise LonsunFormatError("字符串里有老式八进制转义")
            else:
                c = escapes.get(c, c)
        result.append(c)
    return "".join(result).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


class _JSParser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = _js_tokens(source)
        self.i = 0

    def peek(self):
        return self.tokens[self.i].value if self.i < len(self.tokens) else "<结束>"

    def take(self, value=None):
        token = self.peek()
        if token == "<结束>" or (value is not None and token != value):
            raise LonsunFormatError(f"认不出记号 {token}，期望 {value}")
        self.i += 1
        return token

    def function(self, declaration=False):
        self.take("function")
        name = None
        if self.peek() != "(":
            name = self.take()
            if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name, re.ASCII):
                raise LonsunFormatError(f"认不出记号 {name}")
        if declaration and name is None:
            raise LonsunFormatError("函数声明缺少名字")
        self.take("(")
        args = []
        while self.peek() != ")":
            arg = self.take()
            if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", arg, re.ASCII):
                raise LonsunFormatError(f"认不出记号 {arg}")
            args.append(arg)
            if self.peek() != ",":
                break
            self.take(",")
        self.take(")")
        start = self.i
        end = _matching_token(self.tokens, start)
        if self.peek() != "{":
            raise LonsunFormatError(f"认不出记号 {self.peek()}")
        self.i = end + 1
        return ("function", name, tuple(args), self.tokens[start].end, self.tokens[end].start)

    def expr(self, minimum=0):
        token = self.peek()
        if token in ("!", "+", "-", "typeof"):
            self.take()
            left = ("unary", token, self.expr(8))
        elif token == "function":
            left = self.function()
        elif token == "(":
            self.take()
            left = self.expr()
            self.take(")")
        elif token == "[":
            self.take()
            items = []
            while self.peek() != "]":
                items.append(self.expr())
                if self.peek() != ",":
                    break
                self.take()
            self.take("]")
            left = ("array", tuple(items))
        elif token == "{":
            self.take()
            items = []
            while self.peek() != "}":
                key = self.take()
                if key.startswith(("'", '"')):
                    key = _string_literal(key)
                elif re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", key):
                    key = _js_string(_number_literal(key))
                elif not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", key):
                    raise LonsunFormatError(f"认不出记号 {key}")
                self.take(":")
                items.append((key, self.expr()))
                if self.peek() != ",":
                    break
                self.take()
            self.take("}")
            left = ("object", tuple(items))
        elif token.startswith(("'", '"')):
            self.take()
            left = ("literal", _string_literal(token))
        elif re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", token):
            self.take()
            left = ("literal", _number_literal(token))
        elif token in ("true", "false", "null", "undefined"):
            self.take()
            left = (
                "literal",
                {"true": True, "false": False, "null": None, "undefined": UNDEFINED}[token],
            )
        elif token not in {"new", "delete", "void", "for", "while", "return"} and re.fullmatch(
            r"[A-Za-z_$][A-Za-z0-9_$]*", token
        ):
            self.take()
            left = ("identifier", token)
        else:
            raise LonsunFormatError(f"认不出记号 {token}")
        precedence = {
            "||": 1,
            "&&": 2,
            "==": 3,
            "!=": 3,
            "===": 3,
            "!==": 3,
            "<": 4,
            ">": 4,
            "<=": 4,
            ">=": 4,
            "in": 4,
            "+": 5,
            "-": 5,
            "*": 6,
            "/": 6,
            "%": 6,
        }
        while True:
            op = self.peek()
            if op in (".", "[", "(") and minimum <= 9:
                self.take()
                if op == ".":
                    name = self.take()
                    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                        raise LonsunFormatError(f"认不出记号 {name}")
                    left = ("member", left, ("literal", name))
                elif op == "[":
                    key = self.expr()
                    self.take("]")
                    left = ("member", left, key)
                else:
                    args = []
                    while self.peek() != ")":
                        args.append(self.expr())
                        if self.peek() != ",":
                            break
                        self.take()
                    self.take(")")
                    left = ("call", left, tuple(args))
            elif op == "?" and minimum <= 0:
                self.take()
                yes = self.expr()
                self.take(":")
                left = ("conditional", left, yes, self.expr())
            elif op in precedence and precedence[op] >= minimum:
                self.take()
                left = ("binary", op, left, self.expr(precedence[op] + 1))
            else:
                return left

    def end_statement(self):
        if self.peek() == ";":
            self.take()
        elif self.peek() not in ("}", "<结束>") and not self.newline():
            raise LonsunFormatError(f"认不出记号 {self.peek()}")

    def newline(self):
        return (
            self.i > 0
            and self.i < len(self.tokens)
            and bool(
                re.search(
                    r"[\r\n]", self.source[self.tokens[self.i - 1].end : self.tokens[self.i].start]
                )
            )
        )

    def unknown_statement(self):
        start = self.i
        while self.i < len(self.tokens):
            v = self.peek()
            if v == "}":
                break
            if v in ("(", "[", "{"):
                self.i = _matching_token(self.tokens, self.i) + 1
                if v == "{":
                    break
            else:
                self.i += 1
                if v == ";":
                    break
        if start >= len(self.tokens):
            raise LonsunFormatError("脚本在语句中间结束")
        end = self.tokens[self.i - 1].end if self.i > start else self.tokens[start].end
        if self.i == start:
            self.i += 1
        return ("unknown", self.source[self.tokens[start].start : end][:40])

    def statement(self):
        start = self.i
        try:
            return self._statement()
        except LonsunFormatError:
            self.i = start
            return self.unknown_statement()

    def _statement(self):
        v = self.peek()
        if v in {
            "for",
            "while",
            "do",
            "switch",
            "try",
            "throw",
            "break",
            "continue",
            "new",
            "delete",
        }:
            return self.unknown_statement()
        if v == ";":
            self.take()
            return ("empty",)
        if v == "{":
            self.take()
            statements = []
            while self.peek() not in ("}", "<结束>"):
                statements.append(self.statement())
            self.take("}")
            return ("block", tuple(statements))
        if v == "if":
            self.take()
            self.take("(")
            cond = self.expr()
            self.take(")")
            yes = self.statement()
            no = None
            if self.peek() == "else":
                self.take()
                no = self.statement()
            return ("if", cond, yes, no)
        if v == "function":
            return self.function(True)
        if v == "return":
            self.take()
            expr = None if self.peek() in (";", "}", "<结束>") or self.newline() else self.expr()
            self.end_statement()
            return ("return", expr)
        if v == "var":
            self.take()
            declarations = []
            while True:
                name = self.take()
                if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                    raise LonsunFormatError(f"认不出记号 {name}")
                init = None
                if self.peek() == "=":
                    self.take()
                    init = self.expr()
                declarations.append((name, init))
                if self.peek() != ",":
                    break
                self.take()
            self.end_statement()
            return ("var", tuple(declarations))
        start = self.i
        try:
            expr = self.expr()
            if self.peek() in ("=", "+=", "-="):
                op = self.take()
                if expr[0] not in ("identifier", "member"):
                    raise LonsunFormatError("赋值左边无效")
                expr = ("assign", op, expr, self.expr())
            self.end_statement()
            return ("expression", expr)
        except LonsunFormatError:
            self.i = start
            return self.unknown_statement()


def _parse_expression(source: str):
    parser = _JSParser(source)
    tree = parser.expr()
    if parser.peek() != "<结束>":
        raise LonsunFormatError(f"认不出记号 {parser.peek()}")
    return tree


def _parse_statements(source: str):
    parser = _JSParser(source)
    result = []
    while parser.peek() != "<结束>":
        result.append(parser.statement())
    return tuple(result)


class _PartialDict(dict):
    """只知道列出的属性，不假设其他属性不存在。"""


class _Environment:
    def __init__(self, values=None, parent=None, member_resolver=None, call_resolver=None):
        self.values = {} if values is None else values
        self.parent = parent
        self.member_resolver = member_resolver
        self.call_resolver = call_resolver
        self.unknown_reason = ""

    def get(self, name):
        if name in self.values:
            value = self.values[name]
            if value is UNKNOWN:
                root = self
                while root.parent:
                    root = root.parent
                if not root.unknown_reason:
                    root.unknown_reason = f"条件里用到的 {name} 的值无法确定"
            return value
        if self.parent:
            return self.parent.get(name)
        if not self.unknown_reason:
            self.unknown_reason = f"条件里用到了不认识的 {name}"
        return UNKNOWN

    def resolve_member(self, obj, key):
        if self.member_resolver:
            value = self.member_resolver(obj, key)
            if value is not UNKNOWN:
                return value
        return self.parent.resolve_member(obj, key) if self.parent else UNKNOWN

    def call(self, function, args, receiver=UNDEFINED):
        if self.call_resolver:
            value = self.call_resolver(function, args, receiver)
            if value is not UNKNOWN:
                return value
        if self.parent:
            return self.parent.call(function, args, receiver)
        return UNKNOWN


def _js_string(value):
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        return str(int(value)) if value == int(value) and abs(value) < 1e21 else str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ",".join("" if v is None or v is UNDEFINED else _js_string(v) for v in value)
    return "[object Object]"


def _js_number(value):
    if value is UNDEFINED:
        return math.nan
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple, dict)):
        value = _js_string(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0.0
        try:
            if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", value):
                try:
                    return float(int(value, 0))
                except OverflowError:
                    return math.inf  # 和 JS 一样，太大的数是 Infinity
            if not re.fullmatch(r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", value):
                return math.nan
            return float(value)
        except ValueError:
            pass
    return math.nan


def _truth(value):
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0  # 超大整数也照常（math.isnan 会溢出）
    if isinstance(value, float):
        return value != 0 and not math.isnan(value)
    return value != "" if isinstance(value, str) else True


def _strict_equal(a, b):
    if (
        isinstance(a, (int, float))
        and not isinstance(a, bool)
        and isinstance(b, (int, float))
        and not isinstance(b, bool)
    ):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, (dict, list, tuple, _Special)):
        return a is b
    return a == b


def _loose_equal(a, b):
    if (a is None or a is UNDEFINED) and (b is None or b is UNDEFINED):
        return True
    if type(a) is bool:
        return _loose_equal(_js_number(a), b)
    if type(b) is bool:
        return _loose_equal(a, _js_number(b))
    if isinstance(a, (int, float)) and isinstance(b, str):
        return a == _js_number(b)
    if isinstance(b, (int, float)) and isinstance(a, str):
        return _js_number(a) == b
    return _strict_equal(a, b)


def _member(obj, key, env):
    if obj is UNKNOWN or key is UNKNOWN:
        return UNKNOWN
    key = _js_string(key)
    if isinstance(obj, _PartialDict):
        return obj.get(key, UNKNOWN)
    if isinstance(obj, dict):
        return obj.get(key, UNDEFINED)
    if isinstance(obj, (list, tuple, str)):
        if key == "length":
            return float(
                len(obj.encode("utf-16-le", "surrogatepass")) // 2
                if isinstance(obj, str)
                else len(obj)
            )
        if key.isdigit() and str(int(key)) == key:
            index = int(key)
            return obj[index] if index < len(obj) else UNDEFINED
    return env.resolve_member(obj, key)


def _evaluate(tree, env):
    kind = tree[0]
    if kind == "literal":
        return tree[1]
    if kind == "identifier":
        return env.get(tree[1])
    if kind == "function":
        return tree
    if kind in ("array", "object"):
        if kind == "array":
            values = [_evaluate(item, env) for item in tree[1]]
            return UNKNOWN if any(v is UNKNOWN for v in values) else values
        result = {key: _evaluate(item, env) for key, item in tree[1]}
        return UNKNOWN if any(v is UNKNOWN for v in result.values()) else result
    if kind == "member":
        return _member(_evaluate(tree[1], env), _evaluate(tree[2], env), env)
    if kind == "call":
        callee = tree[1]
        receiver = UNDEFINED
        if callee[0] == "member":
            receiver = _evaluate(callee[1], env)
            key = _evaluate(callee[2], env)
            if receiver is UNKNOWN or key is UNKNOWN:
                return UNKNOWN
            if key == "indexOf" and isinstance(receiver, (list, tuple, str)):
                if len(tree[2]) >= 2:
                    return UNKNOWN
                args = [_evaluate(arg, env) for arg in tree[2]]
                if any(arg is UNKNOWN for arg in args):
                    return UNKNOWN
                needle = args[0] if args else UNDEFINED
                if isinstance(receiver, str):
                    haystack = receiver.encode("utf-16-le", "surrogatepass")
                    target = _js_string(needle).encode("utf-16-le", "surrogatepass")
                    index = next(
                        (
                            i // 2
                            for i in range(0, len(haystack) + 1, 2)
                            if haystack.startswith(target, i)
                        ),
                        -1,
                    )
                    return float(index)
                return float(
                    next((i for i, v in enumerate(receiver) if _strict_equal(v, needle)), -1)
                )
            function = _member(receiver, key, env)
        else:
            function = _evaluate(callee, env)
        args = [_evaluate(arg, env) for arg in tree[2]]
        if function is UNKNOWN or any(arg is UNKNOWN for arg in args):
            return UNKNOWN
        return env.call(function, tuple(args), receiver)
    if kind == "conditional":
        condition = _evaluate(tree[1], env)
        return (
            UNKNOWN
            if condition is UNKNOWN
            else _evaluate(tree[2] if _truth(condition) else tree[3], env)
        )
    if kind == "unary":
        op, value = tree[1], _evaluate(tree[2], env)
        if value is UNKNOWN:
            return UNKNOWN
        if op == "!":
            return not _truth(value)
        if op in ("+", "-") and isinstance(value, (list, tuple, dict)):
            return UNKNOWN
        if op == "+":
            return _js_number(value)
        if op == "-":
            return -_js_number(value)
        if value is UNDEFINED:
            return "undefined"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            return "string"
        return (
            "function"
            if callable(value) or (isinstance(value, tuple) and value and value[0] == "function")
            else "object"
        )
    if kind == "binary":
        op = tree[1]
        a = _evaluate(tree[2], env)
        if a is UNKNOWN:
            return UNKNOWN
        if op == "&&" and not _truth(a) or op == "||" and _truth(a):
            return a
        b = _evaluate(tree[3], env)
        if b is UNKNOWN:
            return UNKNOWN
        if op in ("&&", "||"):
            return b
        if op in ("==", "!=") and (
            isinstance(a, (dict, list, tuple)) != isinstance(b, (dict, list, tuple))
        ):
            return UNKNOWN
        if op in ("===", "!==", "==", "!="):
            equal = _strict_equal(a, b) if op in ("===", "!==") else _loose_equal(a, b)
            return not equal if op in ("!==", "!=") else equal
        if op == "in":
            return _js_string(a) in b if isinstance(b, dict) else UNKNOWN
        if op in ("<", ">", "<=", ">="):
            numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b))
            if not numbers and not (isinstance(a, str) and isinstance(b, str)):
                return UNKNOWN
            if isinstance(a, str):
                a = a.encode("utf-16-be", "surrogatepass")
                b = b.encode("utf-16-be", "surrogatepass")
            return {
                "<": lambda: a < b,
                ">": lambda: a > b,
                "<=": lambda: a <= b,
                ">=": lambda: a >= b,
            }[op]()
        if isinstance(a, (dict, list, tuple)) or isinstance(b, (dict, list, tuple)):
            return UNKNOWN
        if op == "+" and (isinstance(a, str) or isinstance(b, str)):
            return _js_string(a) + _js_string(b)
        a, b = _js_number(a), _js_number(b)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                return (
                    math.nan
                    if a == 0 or math.isnan(a)
                    else math.copysign(math.inf, a * math.copysign(1, b))
                )
            return a / b
        if op == "%":
            if b == 0 or math.isinf(a):
                return math.nan
            return math.fmod(a, b)
    return UNKNOWN


@dataclass(frozen=True)
class LonsunPlan:
    organ_id: str
    prefix: str
    label: str
    list_params: tuple
    globals: tuple
    methods: tuple
    raw_methods: tuple
    # init 里 getData 要等着的那个请求的路径（芜湖这类，见 _ready_path）；没有就是空串
    ready: str = ""


def _immutable(value):
    if isinstance(value, list):
        return tuple(_immutable(v) for v in value)
    if isinstance(value, dict):
        return tuple((k, _immutable(v)) for k, v in value.items())
    return value


_MUTATORS = {"push", "pop", "shift", "unshift", "splice", "sort", "reverse"}
# 每次都新建一个数组返回的方法：对返回值本身原地排序、push 碰不到原来的数据（改它的元素不算）
_FRESH_RESULTS = frozenset(
    {
        "split",
        "slice",
        "concat",
        "map",
        "filter",
        "keys",
        "values",
        "entries",
        "from",
        "match",
        "grep",
        "makeArray",
        "toArray",
        "flat",
        "flatMap",
    }
)
_TRACKED = {
    "listParams",
    "hideIds",
    "organId",
    "prefix",
    "actions",
    "query",
    "currCatId",
    "refresh",
    "ztreeData",
    "ztreeData2",
}


_ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=", "++", "--")


def _write_sites(tokens, values, skipped=frozenset()):
    """找属性、下标、更新运算和常见原地修改：(路径, 操作, 根名字的记号位置)；不执行任何脚本。
    经过函数调用的（a.b(…).c = …、a(…)[0].d++）路径里记一个 "()"：改的是函数返回的对象。"""
    v = values
    result = []
    for i, name in enumerate(v):
        if i in skipped or not _IDENT.fullmatch(name):
            continue
        if i and v[i - 1] in (".", "var"):
            continue
        parts = [name]
        j = i + 1
        while j < len(v):
            if v[j] == "." and j + 1 < len(v):
                parts.append(v[j + 1])
                j += 2
            elif v[j] == "[":
                end = _matching_token(tokens, j)
                part = v[j + 1] if end == j + 2 else "*"
                part = _string_literal(part) if part.startswith(("'", '"')) else "*"
                parts.append(part)
                j = end + 1
            elif (
                v[j] == "("
                and name not in _KEYWORD_PARENS
                and (i == 0 or v[i - 1] not in ("function", "new"))
            ):
                # 调用以后还接着 .x 或 [ ] 的：接着往后找（末尾是调用的照原来的规矩看）
                end = _matching_token(tokens, j)
                if v[end + 1 : end + 2] not in (
                    [
                        ".",
                    ],
                    ["["],
                ):
                    break
                parts.append("()")
                j = end + 1
            else:
                break
        op = v[j] if j < len(v) else ""
        if op in _ASSIGN_OPS or (i and v[i - 1] in ("delete", "++", "--")):
            result.append((tuple(parts), op, i))
        elif op == "(" and len(parts) > 1 and parts[-1] in _MUTATORS:
            base = parts[:-1]
            while len(base) > 2 and base[-1] == "()" and base[-2] in ("sort", "reverse"):
                base = base[:-2]  # sort()、reverse() 返回的还是同一个数组
            if len(base) > 1 and base[-1] == "()" and base[-2] in _FRESH_RESULTS:
                continue  # x.split(',').sort().reverse()：改的是新建的数组
            result.append((tuple(parts[:-1]), parts[-1], i))
        elif (
            parts in (["$", "extend"], ["jQuery", "extend"], ["$", "merge"], ["jQuery", "merge"])
            or len(parts) == 2
            and parts[0] in ("Object", "Reflect")
        ) and op == "(":
            # 只需要找目标；复杂目标一律作为未知写入。$.extend(true, X, …) 的第一个参数是"深拷贝"
            # 标志，目标是第二个；$.merge(X, …) 把后面的数组并进 X
            k = j + 1
            if parts[-1] == "extend" and k < len(v) and v[k] in ("true", "false", "!"):
                k += 2 if v[k] == "!" else 1
                if k < len(v) and v[k] == ",":
                    k += 1
            if k < len(v):
                target = v[k]
                if (
                    target in ("me", "this", "window")
                    and v[k + 1 : k + 2] == ["."]
                    and k + 2 < len(v)
                ):
                    result.append(((target, v[k + 2]), "extend", k))
                elif target in ("me", "this") and v[k + 1 : k + 2] == ["["]:
                    result.append(((target, "*"), "extend", k))
                elif _IDENT.fullmatch(target):
                    result.append(((target,), "extend", k))
    return result


def _writes(source, *, skip_functions=False):
    """找属性、下标、更新运算和常见原地修改；不执行任何脚本。"""
    tokens = _js_tokens(source)
    v = _values(tokens)
    skipped = set()
    if skip_functions:
        for i, token in enumerate(v):
            if token != "function" or i in skipped:
                continue
            j = i + 1
            if j < len(v) and v[j] != "(":
                j += 1
            if j < len(v) and v[j] == "(":
                j = _matching_token(tokens, j) + 1
                if j < len(v) and v[j] == "{":
                    skipped.update(range(i, _matching_token(tokens, j) + 1))
    return [(path, op) for path, op, _ in _write_sites(tokens, v, skipped)]


# 只在用户点击、输入时才跑的事件处理函数（页面加载、发列表请求之前不跑）：jQuery 的 .click(fn)
# 这类简写、.on/.one/.bind 只挂这些事件的，以及 zTree 设置里的点击回调。focus、blur、load、scroll
# 这类加载时就可能触发的不算
_USER_EVENTS = frozenset(
    {
        "click",
        "dblclick",
        "change",
        "input",
        "keyup",
        "keydown",
        "keypress",
        "submit",
        "mouseenter",
        "mouseleave",
        "mouseover",
        "mouseout",
        "mousedown",
        "mouseup",
        "contextmenu",
        "touchstart",
        "touchend",
    }
)
_EVENT_SHORTHANDS = (_USER_EVENTS - {"input", "touchstart", "touchend"}) | {"hover"}
_ZTREE_CLICKS = frozenset(
    {"onClick", "beforeClick", "onDblClick", "beforeDblClick", "onRightClick", "beforeRightClick"}
)
# 加载时替用户触发事件的写法（另有 .click() 这类不带参数的简写）
_TRIGGERS = frozenset({"trigger", "triggerHandler", "dispatchEvent", "fireEvent"})


def _function_at(tokens, values, f):
    """f 处的 function：给出 (参数左括号, 函数体左花括号, 函数体右花括号)，认不出给 None。"""
    j = f + 1
    if j < len(values) and values[j] != "(":
        j += 1
    if j >= len(values) or values[j] != "(":
        return None
    body = _matching_token(tokens, j) + 1
    if body >= len(values) or values[body] != "{":
        return None
    return j, body, _matching_token(tokens, body)


def _handler_spans(tokens, values):
    """只在用户点击、输入时才跑的事件处理函数体的记号范围 [(左花括号, 右花括号)]。"""
    spans = []
    for f, value in enumerate(values):
        if value != "function":
            continue
        found = _function_at(tokens, values, f)
        if found is None:
            continue
        _, body, close = found
        if f >= 2 and values[f - 1] == ":" and values[f - 2] in _ZTREE_CLICKS:
            spans.append((body, close))
            continue
        if not f or values[f - 1] not in ("(", ","):
            continue
        callee = _enclosing_callee(values, f)
        if callee is None or callee < 1 or values[callee - 1] != ".":
            continue
        name = values[callee]
        first = values[callee + 2] if callee + 2 < len(values) else ""
        if name in _EVENT_SHORTHANDS:
            spans.append((body, close))
        elif name in ("on", "one", "bind") and first[:1] in ("'", '"'):
            events = {event.split(".")[0] for event in _string_literal(first).split()}
            if events and events <= _USER_EVENTS:
                spans.append((body, close))
    return spans


def _fresh_roots(tokens, values, source):
    """本段代码自己新建的空对象、空数组：每次赋值（含 var）右边都只是 {} 或 []，名字第一次出现
    就是这样的赋值，也不当任何函数的参数。给它加一层属性、往里 push 碰不到栏目数据和 me。"""
    params = set()
    for f, value in enumerate(values):
        if value == "function":
            found = _function_at(tokens, values, f)
            if found is not None:
                params.update(values[found[0] + 1 : _matching_token(tokens, found[0])])
    first, assigned = {}, {}
    for i, value in enumerate(values):
        if (
            not _IDENT.fullmatch(value)
            or i
            and values[i - 1] == "."
            or values[i + 1 : i + 2] == [":"]
            and i
            and values[i - 1] in ("{", ",")
        ):
            continue  # 成员名、对象字面量的键
        first.setdefault(value, i)
        if (
            values[i + 1 : i + 2]
            and values[i + 1] in _ASSIGN_OPS
            or i
            and values[i - 1] in ("++", "--")
        ):
            assigned.setdefault(value, []).append(i)

    def fresh(i):
        if values[i + 1] != "=" or values[i + 2 : i + 4] not in (["{", "}"], ["[", "]"]):
            return False
        k = i + 4
        if k >= len(values) or values[k] in (";", "}", ")", ","):
            return True
        # 换行以后接着写名字：自动补分号，{} 后面接不了别的
        newline = "\n" in source[tokens[k - 1].end : tokens[k].start]
        return (
            newline and bool(_IDENT.fullmatch(values[k])) and values[k] not in ("in", "instanceof")
        )

    return {
        name
        for name, positions in assigned.items()
        if name not in params
        and name not in ("me", "this")
        and first[name] == positions[0]
        and all(fresh(i) for i in positions)
    }


def _callback_scopes(tokens, values, marker):
    """每处 marker（如 $ . each ( me . ztreeData2）后面紧跟的 , function (…) {…} 回调：
    [(参数名, 函数体范围)]。参数在函数体里被重新赋值、var 声明，或者被里面的函数、catch 用作
    参数名的不算（那里它就不一定是原来的节点了）。"""
    scopes = []
    n = len(marker)
    for i in range(len(values) - n):
        if values[i : i + n] != marker or values[i + n : i + n + 2] != [",", "function"]:
            continue
        found = _function_at(tokens, values, i + n + 1)
        if found is None:
            continue
        paren, body, close = found
        names = {
            v for v in values[paren + 1 : _matching_token(tokens, paren)] if _IDENT.fullmatch(v)
        }
        rebound = set()
        for k in range(body + 1, close):
            value = values[k]
            if value in names and (
                values[k - 1] == "var"
                or values[k + 1] in _ASSIGN_OPS
                and values[k - 1] != "."
                or values[k - 1] in ("++", "--")
            ):
                rebound.add(value)
            if value in ("function", "catch"):
                j = k + 1
                if j < close and values[j] != "(":
                    j += 1
                if j < close and values[j] == "(":
                    rebound.update(values[j + 1 : _matching_token(tokens, j)])
        if names - rebound:
            scopes.append((frozenset(names - rebound), (body, close)))
    return scopes


def _tree1_scopes(source):
    """initTree 里遍历树一（_organTree，页面左边的机构树）节点的 $.each 回调。zTree 建树时把数据
    深拷贝一份（tools.clone），树一的节点和我们照着推请求的树二互不相干，改它们不碍事。条件：
    _organTree 在 initTree 里只赋值这一次（不另行 var 声明）、在顶层、就是 $.fn.zTree.init(…)
    的结果，回调写在它后面。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    assigns = [
        k
        for path, op, k in _write_sites(tokens, values)
        if path == ("_organTree",) and op in _ASSIGN_OPS
    ]
    declared = any(values[k : k + 2] == ["var", "_organTree"] for k in range(len(values)))
    if declared or len(assigns) != 1:
        return []
    k = assigns[0]
    if values[k + 1 : k + 9] != ["=", "$", ".", "fn", ".", "zTree", ".", "init"] or (
        _inside_function(source, tokens[k].start)
    ):
        return []
    marker = ["$", ".", "each", "(", "_organTree", ".", "getNodes", "(", ")"]
    return [scope for scope in _callback_scopes(tokens, values, marker) if scope[1][0] > k]


def _node_scopes(source):
    """preprocessData 里遍历栏目数据的 $.each(me.ztreeData…, function (i, el) {…}) 回调：改节点的
    哪些字段另有规则（parentId、children 不许改，别的字段记成改过）。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    return [
        scope
        for lib in ("$", "jQuery")
        for key in ("ztreeData", "ztreeData2")
        for scope in _callback_scopes(tokens, values, [lib, ".", "each", "(", "me", ".", key])
    ]


def _write_root_problem(source, *, scoped=(), me_ok=True):
    """加载时会跑的代码（构造函数、getData 之前的方法和它们的回调；用户点了才跑的事件处理函数
    除外）只许改这几种对象：me/this（字段另有规则），本段自己新建的空对象、空数组（_fresh_roots：
    只加一层属性、往里 push），scoped 里各自回调里的参数（核对过的树一节点、preprocessData 遍历
    栏目时的节点）。别的对象——栏目节点、经别名拿到的 me 或它的字段、函数返回值……——看不出是不是
    我们照着推请求的那些，改它的属性一律认不出。也不许在加载时用 trigger 这类方法替用户点（那样
    事件处理函数就在加载时跑了）。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    spans = _handler_spans(tokens, values)

    def in_handler(k):
        return any(a < k < b for a, b in spans)

    fresh = _fresh_roots(tokens, values, source)
    for path, op, k in _write_sites(tokens, values):
        if in_handler(k) or len(path) == 1 and op not in _MUTATORS and op != "extend":
            continue  # 用户点了才跑；或者只是给变量赋值，不改对象
        root = path[0]
        if root in ("me", "this") and me_ok:
            continue
        if any(root in names and a < k < b for names, (a, b) in scoped):
            continue
        if root in fresh and (
            len(path) == 1 if op in _MUTATORS else len(path) == 2 and op != "extend"
        ):
            continue
        return f"改了 {'.'.join(path)}，看不出是不是栏目节点或 me 上的数据"
    for k in range(len(values) - 2):
        if values[k] != "." or in_handler(k):
            continue
        name = values[k + 1]
        if (name in _TRIGGERS and values[k + 2] == "(") or (
            name in _EVENT_SHORTHANDS | {"focus", "blur"} and values[k + 2 : k + 4] == ["(", ")"]
        ):
            return f"加载时用 .{name}() 替用户触发了事件"
    return None


def _method_references(source, names):
    """方法体里用到的 PublicTree 方法：me.X、this.X、me['X']，回调里、括号里、先存进变量的都算
    （用户点了才跑的事件处理函数除外）。按算出来的名字取 me 的成员（me[k]）给 None：认不出。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    spans = _handler_spans(tokens, values)
    found = []
    for k, value in enumerate(values):
        if value not in ("me", "this") or k and values[k - 1] == ".":
            continue
        if any(a < k < b for a, b in spans):
            continue
        after = values[k + 1] if k + 1 < len(values) else ""
        if after == "." and k + 2 < len(values):
            name = values[k + 2]
        elif after == "[":
            end = _matching_token(tokens, k + 1)
            key = values[k + 2] if end == k + 3 else ""
            if key[:1] not in ("'", '"'):
                return None
            name = _string_literal(key)
        else:
            continue
        if name in names:
            found.append(name)
    return found


def _load_trigger(tokens, values, mask=()):
    """这段代码在加载时替用户触发点击这类事件没有（.trigger('click')、.click() 这类不带参数的
    简写、dispatchEvent；事件名认不出的也算，自定义事件不算；用户点了才跑的事件处理函数里的
    不算）。mask：不看的记号范围。"""
    spans = [*_handler_spans(tokens, values), *mask]
    for k in range(len(values) - 2):
        if values[k] != "." or any(a < k < b for a, b in spans):
            continue
        name = values[k + 1]
        if name in ("trigger", "triggerHandler") and values[k + 2] == "(":
            first = values[k + 3] if k + 3 < len(values) else ""
            if first[:1] in ("'", '"'):
                events = {e.split(".")[0] for e in _string_literal(first).split()}
                if not events & (_USER_EVENTS | {"focus", "blur"}):
                    continue  # 自定义事件（validate 这类）：碰不到"点了才跑"的处理函数
            return True
        if (name in _TRIGGERS and values[k + 2] == "(") or (
            name in _EVENT_SHORTHANDS | {"focus", "blur"} and values[k + 2 : k + 4] == ["(", ")"]
        ):
            return True
    return False


def _ready_path(raw):
    """init 里 getData 等着的那个请求（me.X().done(function () { me.getData(); })）：照模板逐个
    记号比对 X，给出它请求的路径；写法认不出给 None。"""
    init = _values(_js_tokens(raw.get("init", "")))
    names = [
        init[k + 2]
        for k in range(len(init) - 7)
        if init[k : k + 2] == ["me", "."] and init[k + 3 : k + 8] == ["(", ")", ".", "done", "("]
    ]
    if len(names) != 1 or names[0] not in raw:
        return None
    values = _values(_js_tokens(raw[names[0]]))
    n = len(_READY_HEAD)
    if values[:n] != _READY_HEAD or len(values) <= n or values[n][:1] not in ("'", '"'):
        return None
    rest = values[n + 1 :]
    if rest[:1] == [","]:
        rest = rest[1:]
    if rest[-1:] == [";"]:
        rest = rest[:-1]
    path = _string_literal(values[n])
    if rest != _READY_TAIL or not re.fullmatch(r"(/[A-Za-z0-9_\-]+)+", path):
        return None
    return path


def _getdata_deferred(init_body):
    """init 是不是把 me.getData() 放在回调里（.done 这类）推迟着跑：那样页面别的脚本有机会在发
    列表请求之前跑。"""
    tokens = _js_tokens(init_body)
    values = _values(tokens)
    return any(
        values[k : k + 3] == ["me", ".", "getData"] and _inside_function(init_body, tokens[k].start)
        for k in range(len(values) - 2)
    )


def _me_alias_problem(source, method_names=frozenset()):
    """me、this 只许直接用成员（me.x、me['x']、this.x）；除了开头的 var me = this，把 me 或 this
    整个当值用（赋给别的变量、当参数传、放进数组或对象、返回）或者给 me 重新赋值的，后面经它调了
    什么、改了什么都跟不上，认不出。也不许改 PublicTree 的方法（me.open = …，实例属性会盖住原型
    方法）。嵌套函数里的 this 不是 me，不管；用户点了才跑的事件处理函数也不管。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    spans = _handler_spans(tokens, values)
    for path, _op, _k in _write_sites(tokens, values):
        if path[0] in ("me", "this") and len(path) > 1 and path[1] in method_names:
            return f"改了 PublicTree 的方法 {path[1]}"
    for k, value in enumerate(values):
        if value not in ("me", "this") or k and values[k - 1] == ".":
            continue
        if any(a < k < b for a, b in spans):
            continue
        after = values[k + 1] if k + 1 < len(values) else ""
        if after in (".", "[") or after == ":" and k and values[k - 1] in ("{", ","):
            continue
        if (
            value == "me"
            and values[k - 1 : k] == ["var"]
            and values[k + 1 : k + 3] == ["=", "this"]
        ):
            continue
        if value == "this" and values[max(0, k - 3) : k] == ["var", "me", "="]:
            continue
        if value == "this" and _inside_function(source, tokens[k].start):
            continue
        return f"把 {value} 整个当值用或者重新赋值，跟不上经它做了什么"
    return None


def _tracked_write(path):
    return (
        len(path) > 1
        and path[0] in ("me", "this")
        # 经 me 的方法返回的对象改（me.getParams().pageSize = …）：看不出改的是不是请求参数
        and (path[1] in _TRACKED or path[1] == "*" or "()" in path)
    ) or path[0] in ("catalogs", "_organTree2")


def _check_pre_methods(raw, globals_, *, widget=False):
    all_globals = set(globals_) - {"_organTree"}
    used = {
        t.value
        for name in ("getData", "open", "getList", "getNewList")
        for t in _js_tokens(raw.get(name, ""))
    }
    globals_ = set(globals_) & used
    pending = [
        "init",
        "isRefresh",
        "preprocessData",
        "findParentId",
        "initTree",
        "bindEvt",
        "getReportyear",
        "setSelect",
    ]
    init_tokens = [t.value for t in _js_tokens(raw.get("init", ""))]
    for i in range(len(init_tokens) - 6):
        if init_tokens[i : i + 2] == ["me", "."] and init_tokens[i + 3 : i + 7] == [
            "(",
            ")",
            ".",
            "done",
        ]:
            called = init_tokens[i + 2]
            if called not in raw:
                raise LonsunFormatError(f"init 调用了未建模的方法 {called}")
            pending.append(called)
    checked = set()
    while pending:
        name = pending.pop()
        if name in checked:
            continue
        checked.add(name)
        source = raw.get(name, "")
        if not (name == "fuzzySearch" and widget):  # 核对过的搜索插件，见 _harmless_search_widget
            scoped = (
                _tree1_scopes(source)
                if name == "initTree"
                else _node_scopes(source)
                if name == "preprocessData"
                else ()
            )
            problem = _write_root_problem(source, scoped=scoped)
            if problem:
                raise LonsunFormatError(f"{name} {problem}")
        problem = _me_alias_problem(source, frozenset(raw))
        if not problem:
            source_tokens = _js_tokens(source)
            problem = _url_change_problem(source_tokens, _values(source_tokens))
        if problem:
            raise LonsunFormatError(f"{name} {problem}")
        # 方法体里用到的别的方法也在加载时跑（回调里调的、me['X']()、(me.X)() 都算）
        references = _method_references(source, frozenset(raw))
        if references is None:
            raise LonsunFormatError(f"{name} 按算出来的名字用 me 的成员，跟不上调了什么")
        for called in references:
            if called in _MODELED:
                if name == "init" and called == "getData":
                    continue
                raise LonsunFormatError(f"{name} 在 getData 之前用了 {called}，跟不上")
            pending.append(called)
        if name != "init":
            call_tokens = _js_tokens(source)
            call_values = [token.value for token in call_tokens]
            local_functions = {
                call_values[i + 1]
                for i in range(len(call_values) - 2)
                if call_values[i] == "function" and call_values[i + 2] == "("
            }
            called_locals = {
                call_values[i]
                for i in range(len(call_values) - 1)
                if call_values[i] in local_functions
                and call_values[i + 1] == "("
                and (i == 0 or call_values[i - 1] not in ("function", "."))
                and not _inside_function(source, call_tokens[i].start)
            }
            for i in range(len(call_values) - 2):
                if (
                    call_values[i] != "function"
                    or call_values[i + 2] != "("
                    or call_values[i + 1] not in called_locals
                    or _inside_function(source, call_tokens[i].start)
                ):
                    continue
                end_args = _matching_token(call_tokens, i + 2)
                start_body = end_args + 1
                if start_body >= len(call_values) or call_values[start_body] != "{":
                    raise LonsunFormatError("局部函数的函数体认不出")
                end_body = _matching_token(call_tokens, start_body)
                function_body = source[call_tokens[start_body].end : call_tokens[end_body].start]
                body_values = [t.value for t in _js_tokens(function_body)]
                if any(
                    body_values[j] in ("me", "this") and body_values[j + 1] in (".", "[")
                    for j in range(len(body_values) - 1)
                ) or any(v in globals_ or v in {"catalogs", "_organTree2"} for v in body_values):
                    raise LonsunFormatError(f"{name} 的局部函数引用了依赖状态")
                if any(
                    _tracked_write(path) for path, _ in _writes(function_body, skip_functions=True)
                ):
                    raise LonsunFormatError(f"{name} 的局部函数改写了依赖状态")
            for i in range(len(call_values) - 1):
                if (
                    re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", call_values[i])
                    and call_values[i + 1] == "("
                    and (i == 0 or call_values[i - 1] not in (".", "function"))
                    and call_values[i]
                    not in {
                        "if",
                        "for",
                        "while",
                        "switch",
                        "catch",
                        "function",
                        "$",
                        "setTimeout",
                        "RegExp",
                    }
                    and call_values[i] not in local_functions
                    and not _inside_function(source, call_tokens[i].start)
                ):
                    raise LonsunFormatError(f"{name} 调用了未建模的函数 {call_values[i]}")
            for i in range(len(call_values) - 3):
                if (
                    call_values[i] in ("me", "this")
                    and call_values[i + 1] == "."
                    and call_values[i + 3] == "("
                    and not _inside_function(source, call_tokens[i].start)
                ):
                    called = call_values[i + 2]
                    if called not in raw:
                        raise LonsunFormatError(f"{name} 调用了未建模的方法 {called}")
                    pending.append(called)
        aliases = set(
            re.findall(
                r"\b(?:var\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:me|this)\s*\.\s*(?:listParams|hideIds|actions|query|ztreeData2?)\b",
                source,
            )
        )
        assignments = re.findall(
            r"\b(?:var\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)\b",
            source,
        )
        while True:
            more = {dest for dest, src in assignments if src in aliases} - aliases
            if not more:
                break
            aliases.update(more)
        if name != "init":
            for i in range(len(call_values) - 1):
                if call_values[i] not in local_functions or call_values[i + 1] != "(":
                    continue
                if i and call_values[i - 1] in ("function", "."):
                    continue
                end = _matching_token(call_tokens, i + 1)
                arguments = call_values[i + 2 : end]
                if any(
                    v in aliases or v in {"me", "this", "catalogs", "_organTree2"}
                    for v in arguments
                ):
                    raise LonsunFormatError(f"{name} 向局部函数传入了依赖状态")
        writes = _writes(source, skip_functions=True)
        if any(path[0] in aliases for path, _ in writes):
            raise LonsunFormatError(f"{name} 通过别名改写了依赖状态")
        # 回调里的写入也算：$.each、$.grep 这类同步回调就在加载时跑，.done 这类回调也可能在
        # getData 之前跑。顶层之外（只在回调里）对依赖状态的写入一律认不出
        nested = collections.Counter(_writes(source)) - collections.Counter(writes)
        for path, _op in nested:
            if _tracked_write(path) or path[0] in aliases:
                raise LonsunFormatError(f"{name} 的回调改写了依赖状态 {'.'.join(path)}")
        if name != "init":
            local_names = {
                call_values[i + 1] for i in range(len(call_values) - 1) if call_values[i] == "var"
            }
            uncertain_aliases = {
                dest
                for dest, src in assignments
                if src not in local_names | {"me", "this", "true", "false", "null", "undefined"}
            }
            while True:
                more = {
                    dest for dest, src in assignments if src in uncertain_aliases
                } - uncertain_aliases
                if not more:
                    break
                uncertain_aliases.update(more)
            allowed_roots = local_names | {
                "me",
                "this",
                "$",
                "jQuery",
                "Ls",
                "_organTree",
                "console",
            }
            for i in range(len(call_values) - 3):
                if (
                    re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", call_values[i])
                    and (i == 0 or call_values[i - 1] != ".")
                    and call_values[i + 1] == "."
                    and call_values[i + 3] == "("
                    and call_values[i] not in allowed_roots
                    and not _inside_function(source, call_tokens[i].start)
                ):
                    raise LonsunFormatError(f"{name} 调用了未建模的对象方法")
                if (
                    call_values[i]
                    in aliases
                    | uncertain_aliases
                    | all_globals
                    | {"document", "window", "Object", "Reflect"}
                    and call_values[i + 1] == "."
                    and call_values[i + 3] == "("
                    and not _inside_function(source, call_tokens[i].start)
                ):
                    raise LonsunFormatError(f"{name} 调用了未建模的对象方法")
        for path, op in writes:
            if path == ("window",) and op == "extend":
                raise LonsunFormatError(f"{name} 可能改写页面全局变量")
            if not _tracked_write(path):
                global_name = path[1] if path[0] == "window" and len(path) > 1 else path[0]
                if global_name in globals_:
                    raise LonsunFormatError(f"{name} 改写了页面全局变量 {global_name}")
                continue
            if name == "isRefresh" and path == ("me", "refresh") and op == "=":
                continue
            if (
                name == "preprocessData"
                and path in (("me", "hideIds"), ("me", "ztreeData"))
                and op == "="
            ):
                continue
            if name == "initTree" and path == ("_organTree2",) and op == "=":
                continue
            raise LonsunFormatError(f"{name} 改写了依赖状态 {'.'.join(path)}")
    # preprocessData 的节点回调加载时运行，parentId 会影响建树。
    for path, op in _writes(raw.get("preprocessData", "")):
        if path[0] not in ("me", "$", "jQuery") and (
            len(path) > 1
            and path[-1] in ("parentId", "*", "children")
            # 对节点整个 $.extend、Object.assign：看不出改了哪些字段
            or op == "extend"
        ):
            raise LonsunFormatError("preprocessData 改写了建树字段")
    # 删除节点可能通过别名或调用结果发生，无法静态证明不影响目录树。
    tree_source = raw.get("initTree", "")
    tree_tokens = _js_tokens(tree_source)
    for token in tree_tokens:
        if token.value == "delete" and not _inside_function(tree_source, token.start):
            raise LonsunFormatError("initTree 含删除操作")
    tree_values = [t.value for t in tree_tokens]
    for i in range(len(tree_values) - 3):
        if tree_values[i : i + 4] == ["$", ".", "each", "("]:
            end = _matching_token(tree_tokens, i + 3)
            args = tree_values[i + 4 : end]
            if "_organTree2" in args and "delete" in args:
                raise LonsunFormatError("initTree 删除了栏目树节点")
    # 回调里对目录树的其他改动也不能放行。
    for path, _op in _writes(raw.get("initTree", "")):
        if _tracked_write(path) and path != ("_organTree2",):
            raise LonsunFormatError("initTree 改写了栏目树")


@lru_cache(maxsize=128)
def _function_ranges(source):
    tokens = _js_tokens(source)
    v = _values(tokens)
    ranges = []
    for i, token in enumerate(v):
        if token != "function":
            continue
        j = i + 1
        if j < len(v) and v[j] != "(":
            j += 1
        if j < len(v) and v[j] == "(":
            j = _matching_token(tokens, j) + 1
            if j < len(v) and v[j] == "{":
                end = _matching_token(tokens, j)
                ranges.append((tokens[j].start, tokens[end].end))
    return tuple(ranges)


def _inside_function(source, position):
    return any(start < position < end for start, end in _function_ranges(source))


def _page_globals(scripts):
    """页面脚本顶层用 var 声明的全局变量：只记名字，值一律当不知道。

    别的脚本改全局变量的写法太多（经别名、this、$.extend(window)、字符串代码……），静态看不全；
    11/16 用安徽 96 个栏目实测，值全当不知道结果一个不变，所以不推断。PublicTree 的条件用到
    这些变量的，照"认不出"处理。"""
    names = {}
    for source in scripts:
        tokens = _js_tokens(source)
        values = _values(tokens)
        # 函数体里的 var 是局部声明；块里的 var 仍是页面全局声明。
        local = set()
        for i, value in enumerate(values):
            if value == "function":
                j = i + 1
                if j < len(values) and values[j] != "(":
                    j += 1
                if j < len(values) and values[j] == "(":
                    j = _matching_token(tokens, j) + 1
                    if j < len(values) and values[j] == "{":
                        end = _matching_token(tokens, j)
                        local.update(range(i, end + 1))
        for i in range(len(tokens) - 2):
            if values[i] == "var" and i not in local and values[i + 2] == "=":
                names[values[i + 1]] = UNKNOWN
    return tuple(names.items())


def _written_script(source):
    tokens = _js_tokens(source)
    values = list(_values(tokens))
    if len(values) == 15 and values[-1] == ";":
        values.pop()
    if (
        len(values) != 14
        or values[:4] != ["document", ".", "write", "("]
        or values[5:11] != ["+", "Math", ".", "random", "(", ")"]
        or values[11] != "+"
        or values[13] != ")"
    ):
        return None
    if not (values[4].startswith(("'", '"')) and values[12].startswith(("'", '"'))):
        return None
    first, last = _string_literal(values[4]), _string_literal(values[12])
    match = re.fullmatch(r'<script\s+src=(["\'])(.*?)', first, re.DOTALL | re.IGNORECASE)
    if not match or last != match.group(1) + "></script>":
        return None
    if not match.group(2).endswith(("?", "=", "&")):
        return None  # 随机数会拼进路径里：浏览器从不请求我们去取的那个地址
    return match.group(2) if re.fullmatch(r"[A-Za-z0-9/_.\-?=&%:]+", match.group(2)) else None


# 改 jQuery 全局 AJAX 设置的调用（可能加令牌、改地址）：不管接收者是谁（$、jQuery 或它们的别名）
_AJAX_SETTERS = {"ajaxSetup", "ajaxPrefilter", "ajaxTransport", "ajaxSend"}
# 我们照着推请求、又可能被页面脚本改动的页面全局对象：每次出现都得是认得的写法
_ROOTS = ("Ls", "catalogs", "PublicTree", "_organTree2")
_GLOBAL_OBJECTS = ("window", "globalThis", "self", "top", "parent", "frames")
# 内置对象和全局函数：页面脚本改了它们，浏览器的行为就和我们照着推的不一样了
_BUILTINS = frozenset(
    {
        "Array",
        "Object",
        "String",
        "Function",
        "Number",
        "Boolean",
        "Date",
        "RegExp",
        "JSON",
        "Math",
        "Symbol",
        "Promise",
        "Reflect",
        "Proxy",
        "Error",
        "encodeURIComponent",
        "encodeURI",
        "decodeURIComponent",
        "decodeURI",
        "escape",
        "unescape",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        # 发请求用的：改了它们，请求就可能被加上令牌这类参数（协调会话 11/13：要停下来问）
        "XMLHttpRequest",
        "XDomainRequest",
        "ActiveXObject",
        "fetch",
        "Request",
        "Headers",
    }
)
_PROTOTYPE_OWNERS = frozenset(
    {
        "Array",
        "Object",
        "String",
        "Function",
        "Number",
        "Boolean",
        "Date",
        "RegExp",
        "Error",
        "XMLHttpRequest",
        "EventTarget",
    }
)


def _global_prefix_stripper(tokens, values):
    """写入路径开头的全局对象去掉以后才是真正改的东西：window.window.GLOBAL_CONTEXTPATH、
    document.defaultView.XMLHttpRequest 改的就是 GLOBAL_CONTEXTPATH、XMLHttpRequest。self、parent
    这类在那个位置是局部变量的不去。"""
    shadowable = tuple(g for g in _GLOBAL_OBJECTS if g not in ("window", "globalThis"))
    local = _local_name_ranges(tokens, values, shadowable)

    def strip(path, k):
        path = tuple(path)
        while len(path) > 1:
            if path[0] in _GLOBAL_OBJECTS and not any(a < k < b for a, b in local.get(path[0], ())):
                path = path[1:]
            elif path[:2] == ("document", "defaultView") and len(path) > 2:
                path = path[2:]
            else:
                break
        return path

    return strip


# 脚本顶层用 var、function 声明这些名字也是改全局（GLOBAL_CONTEXTPATH 一改，浏览器拼的接口地址
# 就变了）；PublicTree、catalogs、_organTree2 另有规则查
_PROTECTED_DECLARATIONS = _BUILTINS | {"GLOBAL_CONTEXTPATH", "Ls", "$", "jQuery"}


def _window_alias_problem(tokens, values):
    """内联脚本（不是元素属性里的）：脚本顶层的 this 就是 window，document.defaultView 也是，经它们
    改了什么看不全，不认（真实页面上都没有）；import(…) 动态加载模块不认；脚本顶层用 var、function
    声明内置对象、GLOBAL_CONTEXTPATH、Ls、jQuery 的名字不认。"""
    spans = []
    for f, value in enumerate(values):
        if value == "function":
            found = _function_at(tokens, values, f)
            if found is not None:
                spans.append((found[0], found[2]))

    def top(k):
        return not any(a < k < b for a, b in spans)

    for k, value in enumerate(values):
        before = values[k - 1] if k else ""
        after = values[k + 1] if k + 1 < len(values) else ""
        if value == "this" and top(k):
            return "脚本顶层用了 this（就是 window），经它改了什么看不全"
        if value == "defaultView" and before == "." and k >= 2 and values[k - 2] == "document":
            return "用了 document.defaultView（就是 window），经它改了什么看不全"
        if value == "import" and after == "(" and before != ".":
            return "用 import(…) 动态加载代码"
        if (
            value in _PROTECTED_DECLARATIONS
            and before in ("var", "function")
            and top(k)
            and (after == "=" or before == "function")
        ):
            return f"脚本顶层用 {before} 声明了 {value}（改了全局）"
    return None


# 拿到原型、构造函数（经它们就能改内置对象、jQuery），或者给属性挂取值函数的写法（真实页面上都没有）
_PROTOTYPE_HOOKS = frozenset(
    {
        "__proto__",
        "setPrototypeOf",
        "getPrototypeOf",
        "constructor",
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
        "noConflict",
    }
)


def _builtin_problem(tokens, values):
    """改内置对象（Array.prototype.indexOf = …、JSON.parse = …、Object.assign(Math, …)）、
    用 __proto__、setPrototypeOf、getPrototypeOf、.constructor、__defineGetter__ 这类（拿到原型、
    构造函数就能改内置对象；jQuery.noConflict() 交回的就是 jQuery），或者把 Array.prototype 这类
    整个当值用（经别名再改）的，不认。
    Array.prototype.slice.call(…) 这样只读的照常。前面带 window.、self. 这类的也算。"""
    strip = _global_prefix_stripper(tokens, values)
    for path, _op, k in _write_sites(tokens, values):
        path = strip(path, k)
        if path[0] in _BUILTINS:
            return f"改了内置的 {'.'.join(path)}"
    for k, value in enumerate(values):
        text = _string_literal(value) if value[:1] in ("'", '"') else value
        if text in _PROTOTYPE_HOOKS:
            return f"用了 {text}"
        if (
            value in _PROTOTYPE_OWNERS
            and values[k + 1 : k + 3] == [".", "prototype"]
            and (not k or values[k - 1] != ".")
            and values[k + 3 : k + 4]
            not in (
                [
                    ".",
                ],
                ["["],
            )
        ):
            return f"把 {value}.prototype 整个当值用"
    return None


# 页面依赖的全局名字（发请求、拼地址、建树、读栏目数据要用的）：经任何对象的成员写到这些名字都算
# 改了全局——函数里的 this、事件对象的 view 都可能就是 window（this.GLOBAL_CONTEXTPATH = …）
_DEPENDED = _PROTECTED_DECLARATIONS | frozenset(_ROOTS)
# 存进变量、交给函数以后，经别名就能改它成员的全局对象。GLOBAL_CONTEXTPATH 是字符串；
# encodeURIComponent 这类全局函数经别名也改不了原来的；catalogs、_organTree2 另有别名规则
_GLOBAL_FUNCTIONS = frozenset(
    {
        "encodeURIComponent",
        "encodeURI",
        "decodeURIComponent",
        "decodeURI",
        "escape",
        "unescape",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "fetch",
    }
)
_ALIASABLE = (_BUILTINS - _GLOBAL_FUNCTIONS) | {"$", "jQuery", "Ls"}
# 这些关键字后面的 [ 开始一个数组字面量（解构就从这里开始），不是取成员
_EXPRESSION_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "in",
        "of",
        "case",
        "do",
        "else",
        "void",
        "delete",
        "new",
        "throw",
        "instanceof",
        "yield",
        "await",
        "var",
        "let",
        "const",
    }
)
_COMPARE = ("==", "!=", "===", "!==")


def _index_access(values, k):
    """values[k] 是 [：前面是名字、)、]、字面量、?. 的是取成员（a[0]、f()[0]），别的是
    数组字面量。"""
    before = values[k - 1] if k else ""
    return (
        before in (")", "]", "?.")
        or bool(_IDENT.fullmatch(before))
        and before not in _EXPRESSION_KEYWORDS
        or before[:1] in ("'", '"', "`")
        or bool(re.fullmatch(r"[0-9.]+", before))
    )


def _pattern_write_problem(tokens, values):
    """_write_sites 看不见的写法（第十一轮审阅）：解构赋值（[me.listParams.pageSize] = […]、
    ({x: Ls.ajaxGet} = …)、var [a] = …），for-in/of 每次写进成员、解构或页面依赖的全局名字，
    for await，class、对象字面量里的简写方法和取值函数（this 跟着谁走、改了什么看不清），with
    （里面的名字先按对象的成员找）。真实页面上都没有（for (var i in …) 这种照常），一律不认。"""
    for k, value in enumerate(values):
        before = values[k - 1] if k else ""
        after = values[k + 1] if k + 1 < len(values) else ""
        if value in ("[", "{"):
            close = _matching_token(tokens, k)
            if values[close + 1 : close + 2] == ["="] and not (
                value == "[" and _index_access(values, k)
            ):
                return "用解构赋值写变量（[…] = …、{…} = …），看不出改了什么"
            if before in ("var", "let", "const"):
                return "用解构声明变量（var [a] = …），看不出改了什么"
        if value == "class" and before not in (".", "?."):
            return "用了 class（我们不分析）"
        if value == "with" and after == "(" and before not in (".", "?."):
            # with (e) { view[k] = … }：里面的名字先按对象的成员找（e.view 就是 window）
            return "用了 with（名字跟着对象走，改了什么看不清）"
        if (
            before in ("{", ",")
            and (
                _IDENT.fullmatch(value)
                and value not in _JS_WORDS
                or value[:1] in ("'", '"')
                or re.fullmatch(r"[0-9.]+", value)
            )
            and after == "("
            and values[_matching_token(tokens, k + 1) + 1 : _matching_token(tokens, k + 1) + 2]
            == ["{"]
        ) or (
            before in ("{", ",")
            and value in ("get", "set", "async", "*")
            and (_IDENT.fullmatch(after) or after[:1] in ("'", '"', "["))
        ):
            return "对象里写了简写方法或取值函数（this 跟着谁走、改了什么看不清）"
        if value != "for":
            continue
        if after == "await":
            return "用了 for await"
        if after != "(":
            continue
        close = _matching_token(tokens, k + 1)
        depth, split = 0, None
        for j in range(k + 2, close):
            token = values[j]
            if token in ("(", "[", "{"):
                depth += 1
            elif token in (")", "]", "}"):
                depth -= 1
            elif depth == 0 and token == ";":
                break
            elif depth == 0 and token in ("in", "of"):
                split = j
                break
        if split is None:
            continue
        start = k + 2 + (values[k + 2] in ("var", "let", "const"))
        target = values[start:split]
        if len(target) != 1 or not _IDENT.fullmatch(target[0]):
            return "for-in/of 每次写进成员或解构，看不出改了什么"
        if target[0] in _DEPENDED or target[0] in _GLOBAL_OBJECTS or target[0] == "this":
            return f"for-in/of 每次把值写进 {target[0]}（改了页面依赖的全局）"
    return None


def _new_only_constructors(token_sets, sources=()):
    """只用 new 调的函数声明（淮南老日期脚本 function initArray(){ this[i] = … } 配
    new initArray(…)）：它最外层的 this 是新建的对象，不是 window。名字在各段内联脚本（token_sets）
    和元素属性脚本（sources）里至少有一处 new 名字(…)，每次出现都得是声明本身、紧跟在 new 后面
    调用，或者读 名字.arguments；写成字符串的、当别的对象的成员（this.名字()、window.名字 这类
    调用时 this 就是那个对象）的都不算。"""
    all_values = [_values(tokens) for tokens in token_sets]
    all_values += [_values(_js_tokens(source)) for source in sources]
    declared = set()
    for values in all_values:
        for k in range(len(values) - 2):
            if (
                values[k] == "function"
                and _IDENT.fullmatch(values[k + 1])
                and values[k + 2] == "("
                and (k == 0 or values[k - 1] in (";", "{", "}", ")"))
            ):
                declared.add(values[k + 1])
    created, refused = set(), set()
    for values in all_values:
        for k, value in enumerate(values):
            text = _string_literal(value) if value[:1] in ("'", '"') else value
            if text not in declared:
                continue
            before = values[k - 1] if k else ""
            after = values[k + 1] if k + 1 < len(values) else ""
            if value[:1] in ("'", '"') or before in (".", "?."):
                refused.add(text)
            elif before == "new" and after == "(":
                created.add(text)
            elif not (
                before == "function"
                and after == "("
                or after == "."
                and values[k + 2 : k + 3] == ["arguments"]
            ):
                refused.add(text)
    return frozenset(created - refused)


def _reach_problem(tokens, values, source="", *, instance=(), element_this=False, constructors=()):
    """经别的途径改到页面依赖的全局（第十一轮审阅）。不认的：
    - 写入路径里根以外的某一段是页面依赖的全局名字（this.GLOBAL_CONTEXTPATH、e.view.jQuery.param、
      x.Ls.ajaxGet = …）：函数里的 this、事件对象的 view 都可能就是 window。只有本段代码新建的空
      对象上直接加的同名属性（var obj = {}; obj.Ls = 1，见 _fresh_roots）不算；
    - 整个换掉 $、jQuery（$ = …、window.jQuery = …），声明叫 $、jQuery 的变量、函数、参数；
    - document.defaultView、parentWindow 不管怎么写（写成字符串也算），按算出来的名字取
      document 的成员；
    - $、jQuery、Ls 和内置对象整个当值用（var q = jQuery、交给函数、当返回值），前面带
      window.、this. 的、window['jQuery'] 也算：经别名改了什么跟不上；
    - 函数里的 this 可能就是 window（非严格模式直接调用时）。PublicTree 自己的构造函数、原型
      方法最外层（instance），元素事件属性最外层（element_this），只用 new 调的函数最外层
      （constructors）以外，只认 this.名字、$(this)、var X = this（X 照同样的规矩查）；按算出来的
      名字取它的成员、交给函数、当返回值的都不认；
    - 按算出来的名字取调用结果、下标结果的成员（$(window)[0][k]、f()[k]）；
    - .attr、.prop 这类设的属性名不是直接写的字符串（包着 window 的 jQuery 对象经它们能改全局），
      或者正是页面依赖的全局名字；
    - 用到 jQuery 的全局请求设置（ajaxSetup、ajaxSettings 这类，不只是调用）。"""
    strip = _global_prefix_stripper(tokens, values)
    fresh = _fresh_roots(tokens, values, source) if source else set()
    for path, _op, k in _write_sites(tokens, values):
        hit = next((part for part in path[1:] if part in _DEPENDED), None)
        if hit and not (len(path) == 2 and path[0] in fresh):
            return f"经 {'.'.join(path)} 改了页面依赖的 {hit}"
        if strip(path, k) in (("$",), ("jQuery",)):
            return f"整个换掉了 {strip(path, k)[0]}"
    functions = []
    for f, value in enumerate(values):
        if value == "function":
            found = _function_at(tokens, values, f)
            if found is not None:
                functions.append((f, *found))
    for _f, paren, _body, _close in functions:
        if {"$", "jQuery"} & set(values[paren + 1 : _matching_token(tokens, paren)]):
            return "函数参数叫 $ 或 jQuery（在函数里它就不一定是 jQuery 了）"
    for k, value in enumerate(values):
        text = _string_literal(value) if value[:1] in ("'", '"') else value
        before = values[k - 1] if k else ""
        after = values[k + 1] if k + 1 < len(values) else ""
        for word in ("defaultView", "parentWindow"):
            if text == word or value[:1] in ("'", '"', "`") and word in text:
                return f"用了 {word}（就是 window），经它改了什么看不全"
        if text in _AJAX_SETTERS or text == "ajaxSettings":
            return f"用到了 jQuery 的全局请求设置 {text}"
        if value in ("$", "jQuery") and (
            before in ("var", "let", "const", "function")
            or before == "("
            and k >= 2
            and values[k - 2] == "catch"
        ):
            return f"声明了叫 {value} 的变量（在那里它就不一定是 jQuery 了）"
        if value == "[" and _index_access(values, k):
            close = _matching_token(tokens, k)
            key = values[k + 1 : close]
            if len(key) == 1 and (key[0][:1] in ("'", '"') or re.fullmatch(r"[0-9]+", key[0])):
                continue  # 写明的名字、数字下标
            if before in (")", "]", "?."):
                return "按算出来的名字取调用结果、下标结果的成员，看不出取的是什么"
            if before in ("document", "ownerDocument"):
                return f"按算出来的名字取 {before} 的成员（可能就是 defaultView）"
            continue
        # 整个当值用的全局对象：jQuery、window.jQuery、this.jQuery、window['jQuery']
        name, start, end = None, k, k
        if value in _ALIASABLE and before not in (".", "?."):
            name = value
        elif (
            value in _ALIASABLE
            and before == "."
            and k >= 2
            and (values[k - 2] in _GLOBAL_OBJECTS or values[k - 2] == "this")
        ):
            name, start = value, k - 2
        elif (
            value[:1] in ("'", '"')
            and text in _ALIASABLE
            and before == "["
            and after == "]"
            and k >= 2
            and (values[k - 2] in _GLOBAL_OBJECTS or values[k - 2] == "this")
        ):
            name, start, end = text, k - 2, k + 1
        if name is None:
            continue
        first = values[start - 1] if start else ""
        last = values[end + 1] if end + 1 < len(values) else ""
        if (
            last in (".", "[", "(", "?.", "instanceof")
            or first in ("typeof", "new", "instanceof", "delete", "var", "let", "const", "function")
            or first in ("{", ",")
            and last == ":"
            or first in _COMPARE
            or last in _COMPARE
        ):
            continue
        return f"把 {name} 整个当值用（存进变量、交给函数、当返回值），经别名改了什么跟不上"

    def innermost(k):
        best = None
        for f, _paren, body, close in functions:
            if body < k < close and (best is None or body > best[1]):
                best = (f, body, close)
        return best

    instance = {tuple(span) for span in instance}
    aliases = []
    for k, value in enumerate(values):
        if value != "this":
            continue
        found = innermost(k)
        if found is None:
            if element_this:
                continue
            return "最外层的 this 就是 window，经它改了什么看不全"
        f, body, close = found
        name = values[f + 1] if values[f + 1] != "(" else ""
        if (body, close) in instance or (
            name in constructors and (f == 0 or values[f - 1] in (";", "{", "}", ")"))
        ):
            continue
        before = values[k - 1] if k else ""
        after = values[k + 1] if k + 1 < len(values) else ""
        if after == "." and _IDENT.fullmatch(values[k + 2] if k + 2 < len(values) else ""):
            continue  # this.名字：写到页面依赖的名字另有规则拦
        if values[max(0, k - 2) : k] in (["$", "("], ["jQuery", "("]) and after == ")":
            continue
        if (
            before == "="
            and k >= 3
            and values[k - 3] in ("var", "let", "const")
            and _IDENT.fullmatch(values[k - 2])
            and after in (";", ",")
        ):
            aliases.append((values[k - 2], k - 2, body, close))
            continue
        return (
            "函数里的 this 可能就是 window（不是 PublicTree 自己的方法），按算出来的名字取它的"
            "成员、交给函数、当返回值都看不出改了什么"
        )
    for alias, declared, body, close in aliases:
        for k in range(body + 1, close):
            if values[k] != alias or k == declared or values[k - 1] in (".", "?."):
                continue
            after = values[k + 1]
            if values[k - 1] in ("{", ",") and after == ":":
                continue  # 对象字面量的键
            if after == "." and _IDENT.fullmatch(values[k + 2]):
                continue
            if values[k - 2 : k] in (["$", "("], ["jQuery", "("]) and after == ")":
                continue
            return (
                f"{alias} 就是函数里的 this（可能就是 window），按算出来的名字取它的成员、"
                "交给函数、当返回值都看不出改了什么"
            )
    for k in range(1, len(values) - 1):
        if (
            values[k] not in ("attr", "prop", "removeAttr", "removeProp")
            or values[k - 1] not in (".", "?.")
            or values[k + 1] != "("
            or k >= 2
            and values[k - 2] in ("$", "jQuery")
        ):
            continue  # $.attr(元素, 名字, 值)：元素是 this、window 的另有规则拦
        close = _matching_token(tokens, k + 1)
        first = values[k + 2] if k + 2 < close else ""
        names = []
        if first[:1] in ("'", '"'):
            names = [_string_literal(first)]
        elif first == "{":
            end = _matching_token(tokens, k + 2)
            j = k + 3
            while j < end:
                key = values[j]
                if values[j + 1 : j + 2] != [":"] or not (
                    _IDENT.fullmatch(key) or key[:1] in ("'", '"')
                ):
                    names = None
                    break
                names.append(_string_literal(key) if key[:1] in ("'", '"') else key)
                depth, j = 0, j + 2
                while j < end and not (depth == 0 and values[j] == ","):
                    if values[j] in ("(", "[", "{"):
                        depth += 1
                    elif values[j] in (")", "]", "}"):
                        depth -= 1
                    j += 1
                j += 1
        elif first:
            names = None
        if names is None:
            return (
                f".{values[k]} 设的属性名不是直接写的字符串（包着 window 的 jQuery 对象经它能"
                "改全局）"
            )
        hit = next((n for n in names if n in _DEPENDED), None)
        if hit:
            return f".{values[k]} 设了页面依赖的 {hit}"
    return None


# 浏览器当普通脚本执行的 type（HTML 标准的 JavaScript MIME 类型：去掉首尾空白、不分大小写，
# 带参数的不算）；没写 type 时看 language
_JS_TYPES = frozenset(
    {
        "",
        "application/ecmascript",
        "application/javascript",
        "application/x-ecmascript",
        "application/x-javascript",
        "text/ecmascript",
        "text/javascript",
        "text/javascript1.0",
        "text/javascript1.1",
        "text/javascript1.2",
        "text/javascript1.3",
        "text/javascript1.4",
        "text/javascript1.5",
        "text/jscript",
        "text/livescript",
        "text/x-ecmascript",
        "text/x-javascript",
    }
)
# 元素属性里的脚本只在用户操作时才跑的事件（别的 on 属性，如 onload、onerror，加载时就可能跑）；
# 两种都照别的内联脚本的规矩查，见 _attribute_scripts
_JS_URL_ATTRIBUTES = ("href", "src", "action", "formaction", "data")


def _script_kind(el):
    """浏览器怎么对待这个 <script>："classic"（当普通脚本执行，我们照着分析）、"module"（会执行，
    我们不分析）、"data"（不执行：text/html 模板、JSON、写错的 type，以及支持模块的浏览器跳过的
    nomodule 脚本）。"""
    kind = el.get("type")
    if kind is None:
        language = (el.get("language") or "").strip().lower()
        classic = not language or "text/" + language in _JS_TYPES
    else:
        kind = kind.strip().lower()
        if kind == "module":
            return "module"
        classic = kind in _JS_TYPES
    return "classic" if classic and el.get("nomodule") is None else "data"


def _attribute_scripts(doc):
    """页面元素属性里的脚本：[(脚本, 最外层的 this 是不是这个元素)]。onclick、onload 这类事件属性
    最外层的 this 是元素；body、frameset 上的事件属性挂在 window 上，javascript: 开头的链接也在
    window 上跑，this 就是 window。带 srcdoc 的内嵌页面（里面的脚本能改本页）给 None：我们
    不分析。"""
    found = []
    for el in doc.iter():
        if not isinstance(el.tag, str) or any(
            a.tag in ("noscript", "template") for a in el.iterancestors()
        ):
            continue
        for name, value in el.attrib.items():
            name = name.lower()
            if name == "srcdoc":
                return None
            if name.startswith("on"):
                found.append((value, el.tag.lower() not in ("body", "frameset")))
            elif name in _JS_URL_ATTRIBUTES and value.strip().lower().startswith("javascript:"):
                found.append((value.strip()[len("javascript:") :], False))
    return found


def _local_name_ranges(tokens, values, names):
    """names 里各个名字在哪些记号范围里是局部变量：函数参数（在那个函数里）、var 声明和函数声明
    （在声明所在的那个函数里，脚本顶层的就是整段）、函数表达式的名字（在它自己里面）。let、const、
    catch 的参数不算（宁可当成没遮蔽）。{名字: [(开始, 结束)]}，开始和结束都不含。"""
    functions = []
    for f, value in enumerate(values):
        if value == "function":
            found = _function_at(tokens, values, f)
            if found is not None:
                paren, body, close = found
                functions.append((f, paren, _matching_token(tokens, paren), body, close))

    def scope_of(position):
        best = (-1, len(values))
        for _f, _paren, _pclose, body, close in functions:
            if body < position < close and body > best[0]:
                best = (body, close)
        return best

    ranges = {name: [] for name in names}
    for f, paren, pclose, body, close in functions:
        for k in range(paren + 1, pclose):
            if values[k] in names and values[k - 1] in ("(", ","):
                ranges[values[k]].append((paren, close))  # 参数表和函数体里
        name = values[f + 1] if f + 1 < len(values) else ""
        if name in names:
            ranges[name].append((body, close))
            if f == 0 or values[f - 1] in (";", "{", "}", ")"):
                ranges[name].append(scope_of(f))  # 函数声明：名字属于外层
    for d, value in enumerate(values):
        if value != "var":
            continue
        depth, k, expect = 0, d + 1, True
        while k < len(values):
            token = values[k]
            if token in ("(", "[", "{"):
                depth += 1
            elif token in (")", "]", "}"):
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and token == ";":
                break
            elif depth == 0 and token == ",":
                expect = True
                k += 1
                continue
            if expect and depth == 0 and token in names:
                ranges[token].append(scope_of(d))
            expect = False
            k += 1
    return ranges


def _global_object_problem(tokens, values):
    """整个改 window 这类全局对象（$.extend(window, …)、Object.assign(window, …)、window[k] = …），
    或者把它整个当值用（var w = window、交给函数），改了哪些全局就看不出来，不认。只有 $(window)
    挂事件、取尺寸这种可以（经它 .prop、.attr 改属性的不行），typeof window、window === top 这种
    比较也可以。self、parent 这类在那个位置是局部变量（var self = this、函数参数）的不算全局对象；
    按词法作用域判断，外层的 self 照样是窗口。"""
    shadowable = tuple(g for g in _GLOBAL_OBJECTS if g not in ("window", "globalThis"))
    local = _local_name_ranges(tokens, values, shadowable)

    def global_at(name, k):
        return name in _GLOBAL_OBJECTS and not any(a < k < b for a, b in local.get(name, ()))

    for path, _op, k in _write_sites(tokens, values):
        if global_at(path[0], k) and (len(path) == 1 or path[1] == "*"):
            return f"整个改了 {path[0]}，看不出改了哪些全局"
    compare = ("==", "!=", "===", "!==")
    for i, value in enumerate(values):
        if not global_at(value, i) or i and values[i - 1] == ".":
            continue
        before = values[i - 1] if i else ""
        after = values[i + 1] if i + 1 < len(values) else ""
        if after in (".", "[") or before == "typeof" or before in compare or after in compare:
            continue
        if before == "function":
            continue  # 函数表达式自己的名字（function self() {}），不是在用它
        if values[max(0, i - 2) : i] in (["$", "("], ["jQuery", "("]) and after == ")":
            chain = values[i - 2 : _chain_end(tokens, i - 2)]
            if any(
                chain[k] == "." and chain[k + 1] in ("prop", "attr", "removeProp", "removeAttr")
                for k in range(len(chain) - 1)
            ):
                return f"经 $({value}) 改了全局对象的属性"
            continue
        return f"把 {value} 整个当值用，跟不上经它改了什么"
    return None


def _name_positions(values, name):
    """name 出现在哪些记号上：名字本身，或者字符串、模板字符串里含着这几个字。"""
    found = []
    for k, value in enumerate(values):
        text = _string_literal(value) if value[:1] in ("'", '"') else value
        if text == name or value[:1] in ("'", '"', "`") and name in text:
            found.append(k)
    return found


def _top_level_parent_problem(tokens, values, keep=()):
    """setSelect 往上找到 me.topLevelParentId 为止：这个值只许 findParentId 定、
    findTopLevelParentNode 读（keep 是这两个方法体的记号范围）。别处出现（名字、字符串都算）
    就算不出 setSelect 会不会出错。"""
    for k in _name_positions(values, "topLevelParentId"):
        if not any(a < k < b for a, b in keep):
            return (
                "别处也用到 topLevelParentId（只许 findParentId 定、findTopLevelParentNode 读）"
                + _UNCLEAR
            )
    return None


def _attribute_script_problem(source, functions, widget, element_this=True, constructors=()):
    """元素属性里的脚本：照别的内联脚本的规矩查——可以读那几个全局对象，不能改；不能改依赖的
    全局（GLOBAL_CONTEXTPATH 这类）、jQuery 的请求设置、zTree、内置对象，不能整个改 window。
    element_this：最外层的 this 是元素（见 _attribute_scripts）。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    strip = _global_prefix_stripper(tokens, values)
    for path, _op, k in _write_sites(tokens, values):
        path = strip(path, k)
        if path[0] in (*_ROOTS, "GLOBAL_CONTEXTPATH", "$", "jQuery") or {
            "ajax",
            "ajaxGet",
            "ajaxSettings",
            "zTree",
        } & set(path):
            return f"改了 {'.'.join(path)}"
    for i in range(len(values) - 2):
        if values[i] == "." and values[i + 1] in _AJAX_SETTERS and values[i + 2] == "(":
            return "改了 AJAX 设置"
    return (
        _global_object_problem(tokens, values)
        or _ztree_problem(values)
        or _builtin_problem(tokens, values)
        or _dynamic_code_problem(tokens, values)
        or _top_level_parent_problem(tokens, values)
        or _pattern_write_problem(tokens, values)
        or _reach_problem(
            tokens, values, source, element_this=element_this, constructors=constructors
        )
        or _foreign_problem(source, functions, widget)
    )


def _literal_text(tokens, values, start, end):
    """start 到 end 里字符串字面量的文字连起来（unescape("…")、decodeURIComponent("…") 先解码）；
    第二项：有没有字面量以外的东西（变量、调用结果）。"""
    from urllib.parse import unquote

    parts, dynamic, k = [], False, start
    while k < end:
        value = values[k]
        if value in ("unescape", "decodeURIComponent", "decodeURI") and values[k + 1 : k + 2] == [
            "("
        ]:
            close = _matching_token(tokens, k + 1)
            inner = values[k + 2 : close]
            if len(inner) == 1 and inner[0][:1] in ("'", '"'):
                parts.append(unquote(_string_literal(inner[0])))
            else:
                dynamic = True
            k = close + 1
            continue
        if value[:1] in ("'", '"'):
            parts.append(_string_literal(value))
        elif value[:1] == "`":
            parts.append(value[1:-1])
        elif value not in ("+", "(", ")", ","):
            dynamic = True
        k += 1
    return "".join(parts), dynamic


# 全局对象：按算出来的名字取它的成员（window['ev' + 'al']）看不出调了什么
_GLOBAL_NAMES = frozenset({"window", "self", "top", "parent", "frames", "globalThis"})


def _dynamic_code_problem(tokens, values):
    """会执行我们看不见的代码的写法：eval、Function、x.constructor(…)、第一个参数是字符串的定时器、
    document.write 写内联脚本（写外链脚本的照常，unescape 编码的也解开看）；改 jQuery 自己的
    方法（$.X = …、单参数的 $.extend(…)；$.fn 下定义插件的照常）。"""
    local_globals = _local_name_ranges(
        tokens, values, tuple(g for g in _GLOBAL_NAMES if g not in ("window", "globalThis"))
    )
    for k, value in enumerate(values):
        text = _string_literal(value) if value[:1] in ("'", '"') else value
        after = values[k + 1] if k + 1 < len(values) else ""
        before = values[k - 1] if k else ""
        if text == "eval" or text in ("execScript", "setImmediate") and after == "(":
            return f"用了 {text}"
        if value == "Function" and after == "(" and before != ".":
            return "用 Function(…) 拼代码"
        if value == "constructor" and before == "." and after == "(":
            return "用 .constructor(…) 拼代码"
        if text in ("setTimeout", "setInterval") and (value == text or after == "]"):
            # 不管前面有没有 window.、this.，也不管是不是按字符串取的：只认直接调、第一个参数写着
            # function（真实页面都这样）；字符串、装着字符串的变量、.call/.apply 都会当代码执行
            call = k + 1 if value == text else k + 2
            first = values[call + 1] if call + 1 < len(values) else ""
            if values[call : call + 1] != ["("] or first != "function":
                return f"{text} 的第一个参数不是直接写的函数（可能是当代码执行的字符串）"
        if text == "Function" and before != "instanceof":
            return "用了 Function（拼代码）"
        if (
            value in _GLOBAL_NAMES
            and after == "["
            and before != "."
            and not any(a < k < b for a, b in local_globals.get(value, ()))
        ):
            close = _matching_token(tokens, k + 1)
            if close != k + 3 or values[k + 2][:1] not in ("'", '"'):
                return f"按算出来的名字取 {value} 的成员"
        if value[:1] in ("'", '"') and re.match(r"\s*javascript:", text, re.I):
            lhs = values[max(0, k - 4) : k]
            if (
                lhs[-2:] in (["location", "="],)
                or lhs[-4:] == ["location", ".", "href", "="]
                or lhs[-1:] == ["("]
                and (
                    values[max(0, k - 4) : k - 1]
                    in (["location", ".", "assign"], ["location", ".", "replace"])
                    or before == "("
                    and k >= 2
                    and values[k - 2] == "open"
                )
            ):
                return "把 javascript: 地址交给 location、open（会当代码执行）"
        if text in ("globalEval", "getScript") and (before == "." or after == "]"):
            return f"用了 {text}（执行我们看不见的代码）"
        if value == "load" and before == "." and after == "(":
            first = values[k + 2] if k + 2 < len(values) else ""
            if first not in ("function", ")"):
                return "用 .load(…) 取网页片段（里面的脚本会执行）"
        if value in ("write", "writeln") and before == "." and after == "(":
            close = _matching_token(tokens, k + 1)
            literal, dynamic = _literal_text(tokens, values, k + 2, close)
            lowered = literal.lower()
            for tag in re.findall(r"<script\b[^>]*>?", lowered):
                if "src=" not in tag:
                    return "document.write 写了内联脚本"
            if re.search(r"<\s*base\b", lowered):
                return "document.write 写了 <base>（改了浏览器拼相对地址的基准）"
            if dynamic and "<script" in lowered and "src=" not in lowered:
                return "document.write 写了内联脚本"
            if dynamic and not literal:
                return "document.write 写的内容看不出来"
        if (
            value in ("$", "jQuery")
            and before != "."
            and values[k + 1 : k + 4] == [".", "extend", "("]
        ):
            close = _matching_token(tokens, k + 3)
            args = [t for t in _depth_zero(tokens, k + 4, close) if values[t] == ","]
            flag = values[k + 4] in ("true", "false", "!")
            if len(args) == (1 if flag else 0):
                return f"用 {value}.extend(…) 改了 jQuery 自己"
    strip = _global_prefix_stripper(tokens, values)
    for path, _op, k in _write_sites(tokens, values):
        path = strip(path, k)
        if path[0] in ("$", "jQuery") and len(path) > 1 and path[1] not in ("fn", "()"):
            return f"改了 jQuery 的 {'.'.join(path)}"
    script_element = any(
        values[k] == "createElement"
        and values[k + 1 : k + 2] == ["("]
        and values[k + 2][:1] in ("'", '"')
        and _string_literal(values[k + 2]).strip().lower() == "script"
        for k in range(len(values) - 2)
    )
    code_props = ("text", "textContent", "innerHTML", "innerText", "outerHTML")
    if script_element and (
        "createTextNode" in values
        or any(
            values[k] == "."
            and values[k + 1] in code_props
            and values[k + 2 : k + 3] == ["="]
            or values[k] == "["
            and values[k + 1][:1] in ("'", '"')
            and _string_literal(values[k + 1]) in code_props
            and values[k + 2 : k + 4] == ["]", "="]
            for k in range(len(values) - 3)
        )
        or any(
            value[:1] in ("'", '"')
            and re.match(r"\s*(?:data|javascript):", _string_literal(value), re.I)
            for value in values
        )
    ):
        # 真实页面上 createElement('script') 都只设 src（统计脚本）；往里写代码的会执行
        return "新建 script 元素往里写代码"
    return _script_text_problem(tokens, values)


def _script_text_problem(tokens, values):
    """document.write 以外，字符串里写着 <script（$(…).append、.html、$('<script>…') 这类往页面里
    写脚本，浏览器会执行）。几个字面量用 + 连起来的连起来看；unescape 编码的 %3Cscript 也算。真实
    页面上只有 document.write 写栏目树脚本那一处（照 document.write 的规矩查）。"""
    writes = [
        (k + 1, _matching_token(tokens, k + 1))
        for k in range(1, len(values) - 1)
        if values[k] in ("write", "writeln") and values[k - 1] == "." and values[k + 1] == "("
    ]
    pattern = re.compile(r"<\s*scr|%3c\s*scr", re.I)
    k = 0
    while k < len(values):
        if values[k][:1] not in ("'", '"', "`") or any(a < k < b for a, b in writes):
            k += 1
            continue
        parts, j = [], k
        while j < len(values) and values[j][:1] in ("'", '"', "`"):
            parts.append(_string_literal(values[j]) if values[j][:1] != "`" else values[j][1:-1])
            if (
                values[j + 1 : j + 2] == ["+"]
                and values[j + 2 : j + 3][:1]
                and values[j + 2][:1] in ("'", '"', "`")
            ):
                j += 2
                continue
            break
        if any(pattern.search(part) for part in parts) or re.search(
            r"<\s*script|%3c\s*script", "".join(parts), re.I
        ):
            return "字符串里写着 <script（往页面里写脚本，浏览器会执行）"
        if re.search(r"<\s*base\b|%3c\s*base\b", "".join(parts), re.I):
            return "字符串里写着 <base（改了浏览器拼相对地址的基准）"
        if re.search(r"\bon(?:error|load)\s*=", "".join(parts), re.I):
            return "字符串里写着 onerror=、onload=（插进页面就会自己执行）"
        k = j + 1
    return None


def _url_change_problem(tokens, values, mask=()):
    """加载时不跳走、只改网址（history.pushState、replaceState，平台的 Ls.url.updateSearchParam，
    按字符串调用的也算）：PublicTree 读到的栏目编号就和页面网址不一样了。用户点了才跑的处理函数里
    的不算；mask 是另有规则查的范围（open 里那句照着走）。给 location 赋值这类跳转是离开页面，不改
    这一页读到的网址，不管。"""
    skip = [*_handler_spans(tokens, values), *mask]
    names = ("pushState", "replaceState", "updateSearchParam")
    for k, value in enumerate(values):
        if any(a < k < b for a, b in skip):
            continue
        before = values[k - 1] if k else ""
        after = values[k + 1] if k + 1 < len(values) else ""
        text = _string_literal(value) if value[:1] in ("'", '"') else ""
        if text in names and after == "]":
            return f"按字符串调用 {text}"
        if before == "." and value in names:
            return f"加载时用 {value} 改网址"
    return None


def _ztree_problem(values):
    """zTree 只许用 $.fn.zTree.init(…) 建树；getZTreeObj、$.fn.zTree._z 这类拿到的是建好的树
    本身或者内部数据，跟不上会改什么。"""
    for k, value in enumerate(values):
        text = _string_literal(value) if value[:1] in ("'", '"') else value
        if text == "getZTreeObj" or value[:1] in ("'", '"') and text == "zTree":
            return f"用了 {text}"
        if value == "zTree" and not (
            values[max(0, k - 4) : k] in (["$", ".", "fn", "."], ["jQuery", ".", "fn", "."])
            and values[k + 1 : k + 4] == [".", "init", "("]
        ):
            return "用了 $.fn.zTree.init 以外的 zTree 接口"
    return None


# 栏目树对象上只查找、遍历或只改显示的 zTree 方法；改结构的（removeNode、addNodes、moveNode、
# reAsyncChildNodes 等）不在里面
_TREE_READS = frozenset(
    {
        "getNodeByParam",
        "getNodesByParam",
        "getNodes",
        "getNodeByTId",
        "transformToArray",
        "getSelectedNodes",
        "selectNode",
        "cancelSelectedNode",
        "expandNode",
        "updateNode",
    }
)
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _method(methods, name):
    found = [m for m in methods if m[0] == name]
    return found[0] if len(found) == 1 else None


def _collapse_functions(tokens):
    """顶层记号：每个 function …(…) {…} 收成一个 ("fn", 名字) 占位（匿名的名字是空串）。"""
    values = _values(tokens)
    out = []
    i = 0
    while i < len(values):
        if values[i] != "function":
            out.append(values[i])
            i += 1
            continue
        j = i + 1
        name = ""
        if j < len(values) and values[j] != "(":
            name = values[j]
            j += 1
        if j >= len(values) or values[j] != "(":
            raise LonsunFormatError("函数写法认不出")
        body = _matching_token(tokens, j) + 1
        if body >= len(values) or values[body] != "{":
            raise LonsunFormatError("函数写法认不出")
        out.append(("fn", name))
        i = _matching_token(tokens, body) + 1
    return out


def _harmless_search_widget(methods):
    """芜湖 initTree 里的 me.fuzzySearch(_organTree2)：网上通用的 zTree 模糊搜索插件。页面加载时
    它只读 P.setting.data.key.name、把 P.setting.view.nameIsHTML 设成真、把搜索函数挂到
    P.searchNodeLazy 上（P 是唯一的参数）；过滤节点的几个局部函数只在用户输入搜索词时才跑，
    不会在发列表请求之前动栏目树。方法逐句照这个样子才认，多一句、换一种写法都不认。"""
    method = _method(methods, "fuzzySearch")
    if method is None:
        return False
    _, args, body = method
    param = args.strip()
    if not _IDENT.fullmatch(param) or param in _ROOTS:
        return False
    try:
        top = _collapse_functions(_js_tokens(body))
    except LonsunFormatError:
        return False
    declared = {item[1] for item in top if isinstance(item, tuple) and item[1]}
    if len(declared) != sum(isinstance(item, tuple) for item in top):
        return False  # 匿名函数或同名函数：认不出
    k = 0
    while k < len(top):
        if isinstance(top[k], tuple):
            k += 1  # 局部函数的声明本身不执行
            continue
        end = next((m for m in range(k, len(top)) if top[m] == ";"), None)
        if end is None:
            return False
        statement = top[k:end]
        if any(isinstance(item, tuple) for item in statement):
            return False
        if not _widget_statement(statement, param, declared):
            return False
        k = end + 1
    return True


def _widget_statement(statement, param, declared):
    def plain(value):
        return (
            value in ("true", "false", "null")
            or re.fullmatch(r"[0-9]+", value) is not None
            or value[:1] in ("'", '"')
        )

    local = len(statement) > 1 and statement[0] == "var" and _IDENT.fullmatch(statement[1])
    names = {param, *declared, *_ROOTS, "me", "this"}
    if statement == ["var", "me", "=", "this"]:
        return True
    if local and statement[1] not in names and len(statement) == 4:
        return statement[2] == "=" and plain(statement[3])
    if local and statement[1] not in names:
        if statement[2:] == ["=", param, ".", "setting", ".", "data", ".", "key", ".", "name"]:
            return True
        args = statement[6:-1]
        return (
            statement[2:6] == ["=", "new", "RegExp", "("]
            and statement[-1] == ")"
            and 1 <= len(args) <= 3
            and all(
                (_IDENT.fullmatch(a) and a not in names) or a[:1] in ("'", '"') for a in args[0::2]
            )
            and all(a == "," for a in args[1::2])
        )
    if statement[:8] == [param, ".", "setting", ".", "view", ".", "nameIsHTML", "="]:
        value = statement[8:]
        return len(value) == 1 and (
            value[0] in ("true", "false") or _IDENT.fullmatch(value[0]) and value[0] not in names
        )
    return (
        len(statement) == 5
        and statement[:4] == [param, ".", "searchNodeLazy", "="]
        and statement[4] in declared
    )


# 只读检查里放行的调用：不带接收者的只认这几个内置函数，带接收者的认 jQuery、Ls 这类库对象，
# 或者字符串、数组上不改原值的方法
_SAFE_CALLS = frozenset(
    {
        "$",
        "parseInt",
        "parseFloat",
        "String",
        "Number",
        "Boolean",
        "isNaN",
        "encodeURIComponent",
        "decodeURIComponent",
        "encodeURI",
        "decodeURI",
    }
)
_SAFE_ROOTS = frozenset({"$", "jQuery", "Ls", "JSON", "Math", "console"})
# jQuery 自己的静态方法里不改参数的（$.merge、$.uniqueSort 这类会原地改，不在里面；$.extend 的
# 目标由 _writes 另查，栏目数据当来源交给它可以）
_JQUERY_READS = frozenset(
    {
        "each",
        "grep",
        "map",
        "inArray",
        "isArray",
        "isFunction",
        "isPlainObject",
        "isEmptyObject",
        "isNumeric",
        "isWindow",
        "type",
        "trim",
        "param",
        "extend",
        "contains",
        "makeArray",
        "parseJSON",
    }
)
_PURE_METHODS = frozenset(
    {
        "indexOf",
        "lastIndexOf",
        "includes",
        "slice",
        "concat",
        "join",
        "split",
        "trim",
        "replace",
        "toLowerCase",
        "toUpperCase",
        "charAt",
        "charCodeAt",
        "substring",
        "substr",
        "toString",
        "valueOf",
        "startsWith",
        "endsWith",
        "match",
        "test",
        "filter",
        "map",
        "some",
        "every",
        "forEach",
        "find",
        "findIndex",
        "reduce",
        "toFixed",
        "getTime",
    }
)
_KEYWORD_PARENS = frozenset(
    {"if", "while", "for", "switch", "return", "function", "catch", "typeof"}
)


def _read_only_body(args, body):
    """函数只读不写：函数体里（含回调）只给不带属性的变量赋值，不改对象属性、不删除、不用原地修改的
    数组方法、不当 $.extend、Object.assign 的目标；调用只认 _SAFE_CALLS、_SAFE_ROOTS 开头的和
    _PURE_METHODS，不调用 me/this 的方法、不调用页面别的函数（它们改不改看不出来）。"""
    params = [a.strip() for a in args.split(",") if a.strip()]
    if not all(_IDENT.fullmatch(a) for a in params):
        return False
    try:
        tokens = _js_tokens(body)
        writes = _writes(body)
    except LonsunFormatError:
        return False
    if any(len(path) != 1 or op in _MUTATORS or op == "extend" for path, op in writes):
        return False
    values = _values(tokens)
    if "delete" in values:
        return False
    for k, value in enumerate(values):
        if value != "(" or k == 0:
            continue
        prev = values[k - 1]
        if prev in _KEYWORD_PARENS or k > 1 and values[k - 2] == "function":
            continue  # 条件、循环，或者函数定义
        if not (_IDENT.fullmatch(prev) or prev in (")", "]")):
            continue  # 分组括号
        if prev in (")", "]"):
            return False  # f()(…)、a[0](…)
        if k < 2 or values[k - 2] != ".":
            if prev in _SAFE_CALLS:
                continue
            return False
        root = k - 1
        while root >= 2 and values[root - 1] == "." and _IDENT.fullmatch(values[root - 2]):
            root -= 2
        if values[root] in ("me", "this"):
            return False
        if values[root] in ("$", "jQuery"):
            if root == k - 3 and prev in _JQUERY_READS:
                continue  # $.each(…) 这类
            return False
        if values[root] in _SAFE_ROOTS or prev in _PURE_METHODS:
            continue
        return False
    return True


def _read_only_method(methods, name):
    """setSelect 把 catalogs[me.organId] 交给页面自己的 me.findTopLevelParentNode(…)：被调方法
    必须只读（见 _read_only_body）。"""
    method = _method(methods, name)
    return method is not None and _read_only_body(method[1], method[2])


def _page_functions(scripts):
    """各段内联脚本顶层声明的函数 {名字: (参数原文, 函数体原文)}；同名的（后面的会盖掉前面的）
    记作 None，当认不出。"""
    found = {}
    for source in scripts:
        tokens = _js_tokens(source)
        values = _values(tokens)
        depth = 0
        k = 0
        while k < len(values):
            value = values[k]
            if value in ("(", "[", "{"):
                depth += 1
            elif value in (")", "]", "}"):
                depth -= 1
            elif (
                value == "function"
                and depth == 0
                and k + 2 < len(values)
                and _IDENT.fullmatch(values[k + 1])
                and values[k + 2] == "("
            ):
                args_end = _matching_token(tokens, k + 2)
                if args_end + 1 < len(values) and values[args_end + 1] == "{":
                    body_end = _matching_token(tokens, args_end + 1)
                    entry = (
                        source[tokens[k + 2].end : tokens[args_end].start],
                        source[tokens[args_end + 1].end : tokens[body_end].start],
                    )
                    name = values[k + 1]
                    found[name] = None if name in found else entry
                    k = body_end + 1
                    continue
            k += 1
    return found


def _catalog_sink(values, i, methods):
    """catalogs[me.organId] 只认这几种用处：构造函数里赋给 me.ztreeData、me.ztreeData2，
    JSON.parse(JSON.stringify(…)) 深拷贝，或者交给只读的页面方法（见 _read_only_method）。"""
    after = values[i + 6] if i + 6 < len(values) else ""
    before = values[max(0, i - 4) : i]
    if before in (["me", ".", "ztreeData", "="], ["me", ".", "ztreeData2", "="]):
        return after == ";"
    if before == ["JSON", ".", "stringify", "("]:
        return after == ")"
    if after not in (")", ","):
        return False
    depth = 0
    for k in range(i - 1, -1, -1):
        value = values[k]
        if value in (")", "]", "}"):
            depth += 1
        elif value in ("(", "[", "{"):
            if depth:
                depth -= 1
                continue
            return (
                value == "("
                and k >= 3
                and values[k - 3 : k - 1] == ["me", "."]
                and _read_only_method(methods, values[k - 1])
            )
    return False


# getData 和它之后的这几个方法由受限求值照着走（走到认不出的就不读）
_MODELED = frozenset({"getData", "open", "getList", "getNewList"})


def _method_spans(tokens):
    """PublicTree 脚本里构造函数体（记作 ""）和各原型方法体的记号范围 {名字: (开始, 结束)}。"""
    values = _values(tokens)
    spans = {}
    for i in range(len(values) - 2):
        if values[i : i + 3] == ["function", "PublicTree", "("]:
            body = _matching_token(tokens, i + 2) + 1
            if body < len(values) and values[body] == "{":
                spans[""] = (body, _matching_token(tokens, body))
    marker = ["$", ".", "extend", "(", "PublicTree", ".", "prototype", ",", "{"]
    for i in range(len(values) - len(marker) + 1):
        if values[i : i + len(marker)] != marker:
            continue
        end = _matching_token(tokens, i + len(marker) - 1)
        k = i + len(marker)
        while k < end and values[k + 1 : k + 4] == [":", "function", "("]:
            body = _matching_token(tokens, k + 3) + 1
            close = _matching_token(tokens, body)
            spans[values[k]] = (body, close)
            k = close + 2  # 跳过方法之间的逗号
    return spans


def _load_time_methods(methods):
    """页面加载、发列表请求之前会跑的原型方法：从 init 起，凡是 me.X(…)、this.X(…) 调到的都算
    （回调里调的也算，宁严勿漏），getData 及之后由受限求值管的那几个除外。"""
    bodies = {name: body for name, _, body in methods}
    seen = set()
    pending = ["init"]
    while pending:
        name = pending.pop()
        if name in seen or name in _MODELED or name not in bodies:
            continue
        seen.add(name)
        values = [t.value for t in _js_tokens(bodies[name])]
        for k in range(len(values) - 3):
            if values[k] in ("me", "this") and values[k + 1] == "." and values[k + 3] == "(":
                pending.append(values[k + 2])
    return seen


_ITERATORS = (
    ["$", ".", "each", "("],
    ["$", ".", "grep", "("],
    ["$", ".", "map", "("],
    ["jQuery", ".", "each", "("],
    ["jQuery", ".", "grep", "("],
    ["jQuery", ".", "map", "("],
)
_ITERATOR_METHODS = frozenset({"forEach", "map", "filter", "some", "every", "find", "findIndex"})


def _expression_end(tokens, start):
    """从 start 起到这个表达式结束（同层的 ; , ) ] } 之前）；中间的函数体整个跳过。"""
    values = _values(tokens)
    depth = 0
    k = start
    while k < len(values):
        value = values[k]
        if value == "function":
            j = k + 1
            while j < len(values) and values[j] != "(":
                j += 1
            if j < len(values):
                body = _matching_token(tokens, j) + 1
                if body < len(values) and values[body] == "{":
                    k = _matching_token(tokens, body) + 1
                    continue
        if value in ("(", "[", "{"):
            depth += 1
        elif value in (")", "]", "}"):
            if depth == 0:
                return k
            depth -= 1
        elif value in (";", ",") and depth == 0:
            return k
        k += 1
    return k


def _enclosing_callee(values, i):
    """i 处的值是不是函数调用的参数（含放在参数的对象、数组字面量里）：是的话给出被调函数名的位置，
    不是（在条件、代码块里）给 None。"""
    depth = 0
    for k in range(i - 1, -1, -1):
        value = values[k]
        if value in (")", "]", "}"):
            depth += 1
            continue
        if value not in ("(", "[", "{"):
            continue
        if depth:
            depth -= 1
            continue
        prev = values[k - 1] if k else ""
        if value == "[":
            continue
        if value == "{":
            if prev in ("(", ",", ":", "=", "[", "return", "?"):
                continue  # 对象字面量
            return None  # 代码块
        if prev in _KEYWORD_PARENS or k > 1 and values[k - 2] == "function":
            return None
        if _IDENT.fullmatch(prev) or prev in (")", "]"):
            return k - 1
    return None


def _foreign_problem(source, functions, widget, *, occurrences=True, mask=()):
    """PublicTree 以外的内联脚本（搜索框、标签页、统计这类），以及 PublicTree 脚本里构造函数、原型
    方法以外的代码：可以读那几个全局对象，不能改它们（直接改的另有规则查）。这里查：
    - occurrences：PublicTree 出现、名字写成字符串、经 window 访问、Ls/catalogs/_organTree2 整个
      当值用、Ls.url 当值用、_organTree2 上调查找类以外的方法，都不认；
    - 别名：从 catalogs[…]、catalogs.x、_organTree2.查找(…) 读出来存进变量的，从别名再读出来的，
      $.each/$.grep/$.map 和数组 forEach 等遍历别名时回调的参数，都算别名；经别名改属性、删属性、
      用原地修改的数组方法、当 $.extend/Object.assign 的目标，都不认；
    - 别名或 catalogs、_organTree2 交给函数当参数：被调的是页面自己声明的函数就要只读
      （_read_only_body），是 jQuery、Ls 这类库对象的方法或者不改原值的内置方法可以，别的都不认。
    mask：不查的源码范围（PublicTree 脚本里构造函数体和原型方法体，那里另有规则）。"""
    for a, b in mask:
        source = source[:a] + " " * (b - a) + source[b:]
    tokens = _js_tokens(source)
    values = _values(tokens)

    def at(k):
        return values[k] if 0 <= k < len(values) else ""

    if occurrences:
        for i, value in enumerate(values):
            if value[:1] in ("'", '"'):
                if _string_literal(value) in _ROOTS:
                    return f"{_string_literal(value)} 写成字符串访问"
                continue
            if value not in _ROOTS:
                continue
            if at(i - 1) == ".":
                if at(i - 2) in _GLOBAL_OBJECTS:
                    return f"经 {at(i - 2)} 访问 {value}"
                continue
            after = at(i + 1)
            if value == "PublicTree":
                return "PublicTree 脚本以外用到 PublicTree"
            if value == "Ls":
                if after != "." or not _IDENT.fullmatch(at(i + 2)):
                    return "Ls 当值传出去或按下标访问"
                if at(i + 2) == "url" and at(i + 3) not in (".", "("):
                    return "Ls.url 当值传出去"
                continue
            if value == "catalogs":
                if after not in (".", "["):
                    return "catalogs 当值传出去"
                continue
            reads = at(i + 2) in _TREE_READS or widget and at(i + 2) == "searchNodeLazy"
            if after == "." and at(i + 3) == "(" and reads:
                continue
            return "_organTree2 的用法认不出"
    return _alias_problem(tokens, source, {"catalogs", "_organTree2"}, functions)


# 页面函数里再调用页面函数时最多往下看几层；再深的认不出
_CALL_DEPTH = 4
_STORE_METHODS = frozenset({"push", "unshift", "splice"})


def _alias_problem(tokens, source, sources, functions, depth=0, visiting=frozenset()):
    """sources 里的名字是栏目数据（catalogs、_organTree2，或者被调函数里收到栏目数据的参数）。
    别名：赋值时右边读到栏目数据的（读成员、下标、条件、数组或对象字面量里，函数调用的结果见
    _tainted_value），$.each/$.grep/$.map、数组 forEach 等遍历别名时回调的参数，push 进别名的数组。
    认不出（给原因）的：经别名改属性、删属性、用原地修改的数组方法、当 $.extend 的目标；把栏目
    数据存进对象属性或数组元素；当返回值交出去；交给页面函数（那个函数照同样的规矩查，最多往下
    _CALL_DEPTH 层）、看不出改不改的函数。"""
    values = _values(tokens)

    def at(k):
        return values[k] if 0 <= k < len(values) else ""

    aliases = set()
    changed = True
    while changed:
        changed = False
        tainted = set(sources) | aliases
        for i, value in enumerate(values):
            new = set()
            if (
                value == "="
                and _IDENT.fullmatch(at(i - 1))
                and at(i - 2) != "."
                and at(i - 1) not in tainted
            ):
                end = _expression_end(tokens, i + 1)
                if _tainted_value(tokens, i + 1, end, tainted, functions):
                    new.add(at(i - 1))
            if values[i : i + 4] in _ITERATORS and at(i + 4) in tainted and at(i + 3) == "(":
                new |= _callback_params(tokens, i + 3)
            if (
                value == "."
                and at(i + 1) in _ITERATOR_METHODS
                and at(i + 2) == "("
                and _chain_root(values, i) in tainted
            ):
                new |= _callback_params(tokens, i + 2)
            if value == "." and at(i + 1) in _STORE_METHODS and at(i + 2) == "(":
                close = _matching_token(tokens, i + 2)
                root = _chain_root(values, i)
                if root and _tainted_value(tokens, i + 3, close, tainted, functions):
                    new.add(root)  # 把栏目数据 push 进数组：数组也是别名
            if new - aliases:
                aliases |= new
                changed = True
    tainted = set(sources) | aliases
    for path, op in _writes(source):
        # 被调函数里收到栏目数据的参数也算（catalogs、_organTree2 本身的改写另有规则查）
        if path[0] in tainted and (len(path) > 1 or op in _MUTATORS or op == "extend"):
            return f"经别名 {path[0]} 改了栏目数据"
    for i, value in enumerate(values):
        if value == "=" and (at(i - 1) == "]" or at(i - 2) == "."):
            end = _expression_end(tokens, i + 1)
            if _tainted_value(tokens, i + 1, end, tainted, functions):
                return "把栏目数据存进了对象属性或数组元素"
        if value == "return":
            end = _expression_end(tokens, i + 1)
            if _tainted_value(tokens, i + 1, end, tainted, functions):
                return "把栏目数据当返回值交出去"
    for i, value in enumerate(values):
        if value not in tainted or at(i - 1) == ".":
            continue
        if not _operand_tainted(tokens, i, _chain_end(tokens, i), tainted, functions):
            continue  # 交出去的是 .id、.length 这类字符串、数字
        callee = _enclosing_callee(values, i)
        if callee is None:
            continue
        name = values[callee]
        if at(callee - 1) == ".":
            root = _chain_root(values, callee - 1)
            if root in ("$", "jQuery"):
                if name in _JQUERY_READS and at(callee - 2) == root:
                    continue
                return f"栏目数据交给了 {root} 的 {name}，看不出它改不改"
            if root in _SAFE_ROOTS or name in _PURE_METHODS:
                continue
            return f"栏目数据交给了 {name}，看不出它改不改"
        if name in ("$", "jQuery"):
            return f"栏目数据交给 {name}(…) 包起来，接着能改它（.prop、.attr 这类）"
        if name in _SAFE_CALLS:
            continue
        problem = _callee_problem(tokens, callee, tainted, functions, depth, visiting)
        if problem:
            return problem
    return None


def _callee_problem(tokens, callee, tainted, functions, depth, visiting):
    """页面函数 values[callee](…) 收到了栏目数据：按收到的参数位置，用同样的规矩查它的函数体。"""
    values = _values(tokens)
    name = values[callee]
    function = functions.get(name)
    if function is None:
        return f"栏目数据交给了 {name}，看不出它改不改"
    open_paren = callee + 1
    close = _matching_token(tokens, open_paren)
    positions, position, start = set(), 0, open_paren + 1
    k = open_paren + 1
    while k <= close:
        value = values[k]
        if value in ("(", "[", "{"):
            k = _matching_token(tokens, k) + 1
            continue
        if value in (",", ")") and k >= start:
            if _tainted_value(tokens, start, k, tainted, functions):
                positions.add(position)
            position, start = position + 1, k + 1
        k += 1
    args, body = function
    params = [a.strip() for a in args.split(",") if a.strip()]
    if not all(_IDENT.fullmatch(a) for a in params):
        return f"页面函数 {name} 的参数写法认不出"
    key = (name, frozenset(positions))
    if key in visiting:
        return None  # 递归调用自己：这一层正在查
    if depth >= _CALL_DEPTH:
        return f"栏目数据经页面函数一层层往下交，超过 {_CALL_DEPTH} 层，看不过来"
    received = {params[p] for p in positions if p < len(params)}
    if "arguments" in [t.value for t in _js_tokens(body)]:
        return f"页面函数 {name} 用 arguments 取参数，认不出"
    problem = _alias_problem(
        _js_tokens(body),
        body,
        received | {"catalogs", "_organTree2"},
        functions,
        depth + 1,
        visiting | {key},
    )
    return f"页面函数 {name}：{problem}" if problem else None


# 节点上这些属性是字符串、数字或真假值：读出来不是栏目数据本身
_PRIMITIVE_PROPS = frozenset(
    {
        "length",
        "id",
        "name",
        "title",
        "parentId",
        "pid",
        "isParent",
        "isShow",
        "link",
        "uri",
        "url",
        "redirectLink",
        "catId",
        "organId",
        "code",
        "level",
    }
)
# 调用结果是字符串、数字或真假值的方法、函数
_PRIMITIVE_METHODS = frozenset(
    {"indexOf", "lastIndexOf", "includes", "join", "toString", "some", "every", "findIndex", "test"}
)
_PRIMITIVE_CALLS = frozenset(
    {
        "parseInt",
        "parseFloat",
        "String",
        "Number",
        "Boolean",
        "isNaN",
        "encodeURIComponent",
        "decodeURIComponent",
        "encodeURI",
        "decodeURI",
    }
)
_PRIMITIVE_OPERATORS = frozenset(
    {
        "+",
        "-",
        "*",
        "/",
        "%",
        "==",
        "!=",
        "===",
        "!==",
        "<",
        ">",
        "<=",
        ">=",
        "instanceof",
        "in",
        "&",
        "|",
        "^",
        "<<",
        ">>",
        ">>>",
    }
)


def _depth_zero(tokens, start, end):
    """start 到 end 里同层（不在括号里）的记号位置。"""
    values = _values(tokens)
    out = []
    k = start
    while k < end:
        if values[k] in ("(", "[", "{"):
            out.append(k)
            k = _matching_token(tokens, k) + 1
            continue
        out.append(k)
        k += 1
    return out


def _tainted_value(tokens, start, end, tainted, functions):
    """start 到 end 这段表达式的值会不会是栏目数据（tainted 里的对象或它的一部分）。
    看值本身：条件运算只看两个分支，&& 只看最后一项，|| 每项都看；算术、比较、取反、拼字符串的
    结果是字符串、数字或真假值，不算。读到 .id、.name、.length 这类属性、调 indexOf 这类方法得到的
    也不算。函数调用：页面自己的函数不算（它收到栏目数据时另按同样的规矩查，不许把栏目数据当
    返回值交出去），别的调用（$.grep、alias.filter 这类会把原来的对象交回来）参数里有就算。"""
    values = _values(tokens)
    if start >= end:
        return False
    top = _depth_zero(tokens, start, end)
    marks = [values[k] for k in top]
    if "=" in marks:  # a = b = 值：看最后一个等号右边
        last = max(k for k in top if values[k] == "=")
        return _tainted_value(tokens, last + 1, end, tainted, functions)
    if "?" in marks:
        q = marks.index("?")
        depth, colon = 0, None
        for k in top[q + 1 :]:
            if values[k] == "?":
                depth += 1
            elif values[k] == ":":
                if depth == 0:
                    colon = k
                    break
                depth -= 1
        if colon is None:
            return True  # 认不出：当是
        return _tainted_value(tokens, top[q] + 1, colon, tainted, functions) or _tainted_value(
            tokens, colon + 1, end, tainted, functions
        )
    if "||" in marks:
        cuts = [start] + [k + 1 for k in top if values[k] == "||"]
        stops = [k for k in top if values[k] == "||"] + [end]
        return any(
            _tainted_value(tokens, a, b, tainted, functions)
            for a, b in zip(cuts, stops, strict=True)
        )
    if "&&" in marks:
        last = max(k for k in top if values[k] == "&&")
        return _tainted_value(tokens, last + 1, end, tainted, functions)
    if values[start] in ("!", "typeof", "void", "delete") or any(
        m in _PRIMITIVE_OPERATORS for m in marks[1:]
    ):
        return False
    return _operand_tainted(tokens, start, end, tainted, functions)


def _operand_tainted(tokens, start, end, tainted, functions):
    values = _values(tokens)
    k = start
    while k < end:
        value = values[k]
        prev = values[k - 1] if k > start else ""
        if value == "function":
            j = k + 1
            while j < end and values[j] != "(":
                j += 1
            if j < end:
                body = _matching_token(tokens, j) + 1
                if body < end and values[body] == "{":
                    k = _matching_token(tokens, body) + 1
                    continue
        call = _IDENT.fullmatch(prev) or prev in (")", "]")
        if value == "(" and call and prev not in _KEYWORD_PARENS:
            close = _matching_token(tokens, k)
            member = k >= 2 and values[k - 2] == "."
            page_function = _IDENT.fullmatch(prev) and not member and prev in functions
            primitive = (
                prev in _PRIMITIVE_METHODS
                if member
                else prev in _PRIMITIVE_CALLS or prev in _SAFE_CALLS and prev != "$"
            ) or values[k - 3 : k] in (["JSON", ".", "stringify"], ["$", ".", "inArray"])
            if (
                not page_function
                and not primitive
                and _tainted_value(tokens, k + 1, close, tainted, functions)
            ):
                return True
            k = close + 1
            continue
        if value in tainted and prev != ".":
            # 顺着成员访问往后看：最后读的是 .id、.name 这类，或者调的是 indexOf 这类，就不算
            j = k + 1
            last = None
            while j < end:
                if values[j] == "." and j + 1 < end and _IDENT.fullmatch(values[j + 1]):
                    last = values[j + 1]
                    j += 2
                elif values[j] == "[":
                    last = None
                    j = _matching_token(tokens, j) + 1
                elif values[j] == "(" and last is not None:
                    if last in _PRIMITIVE_METHODS:
                        last = "()"
                    else:
                        return True
                    j = _matching_token(tokens, j) + 1
                else:
                    break
            if last is None or (last not in _PRIMITIVE_PROPS and last != "()"):
                return True
            k = j
            continue
        k += 1
    return False


def _chain_end(tokens, start):
    """start 处的名字连同后面的成员访问、下标、调用（a.b[0].c(…)）到哪里结束。"""
    values = _values(tokens)
    k = start + 1
    while k < len(values):
        if values[k] == "." and k + 1 < len(values) and _IDENT.fullmatch(values[k + 1]):
            k += 2
        elif values[k] in ("[", "("):
            k = _matching_token(tokens, k) + 1
        else:
            break
    return k


def _chain_root(values, dot):
    """dot 处的 . 前面那串成员访问（a.b[0].c）最开头的名字；认不出给空串。"""
    k = dot - 1
    while k >= 0:
        if values[k] in ("]", ")"):
            closer, opener, depth = values[k], "[" if values[k] == "]" else "(", 0
            while k >= 0:
                if values[k] == closer:
                    depth += 1
                elif values[k] == opener:
                    depth -= 1
                    if depth == 0:
                        break
                k -= 1
            k -= 1
            continue
        if not _IDENT.fullmatch(values[k]):
            return ""
        if k >= 1 and values[k - 1] == ".":
            k -= 2
            continue
        return values[k]
    return ""


def _callback_params(tokens, open_paren):
    """open_paren 处的调用里第一个 function (…) 回调的参数名。"""
    values = _values(tokens)
    end = _matching_token(tokens, open_paren)
    for k in range(open_paren + 1, end):
        if values[k] == "function":
            j = k + 1
            if j < end and values[j] != "(":
                j += 1
            if j < end and values[j] == "(":
                close = _matching_token(tokens, j)
                return {v for v in values[j + 1 : close] if _IDENT.fullmatch(v)}
    return set()


def _root_misuse(tokens, is_tree_script, methods, widget, load):
    """这几个全局对象每次出现都得是认得的写法，否则给出对象名、认不出：
    - Ls：只认调用 Ls.a(…)、Ls.a.b(…) 和给成员赋值 Ls.a = …（改哪些成员不行由写入规则查）；
      当值传出去、读成员再存起来（var u = Ls.url）都不认；
    - PublicTree：只认 function PublicTree(、new PublicTree(、$.extend(PublicTree.prototype, …；
    - catalogs：只认 PublicTree 脚本里的 catalogs[me.organId]；在构造函数、加载时会跑的方法里，
      用处只认 _catalog_sink 那几种，只在 getData 以后才跑的方法里交给受限求值；
    - _organTree2：只认 PublicTree 脚本里 var _organTree2 = null; 和
      _organTree2 = $.fn.zTree.init(…)，调用只查找、遍历或只改显示的方法（_TREE_READS），
      以及核对过的模糊搜索插件（widget）：me.fuzzySearch(_organTree2)、_organTree2.searchNodeLazy(…)。
    经 window 访问、名字写成字符串（window["Ls"]）一律不认。别的对象上同名的属性（x.Ls）不是这几个
    全局，不管。zTree 建树时把节点整份深拷贝（平台 ztree.min.js 的 init 用 tools.clone），
    所以建树以后再动 catalogs 里的对象，影响不到 _organTree2。"""
    values = _values(tokens)
    spans = _method_spans(tokens) if is_tree_script else {}

    def at(k):
        return values[k] if 0 <= k < len(values) else ""

    def region(k):
        inside = [name for name, (a, b) in spans.items() if a < k < b]
        return inside[0] if len(inside) == 1 else None

    for i, name in enumerate(values):
        if name[:1] in ("'", '"'):
            if _string_literal(name) in _ROOTS:
                return _string_literal(name)
            continue
        if name not in _ROOTS:
            continue
        if at(i - 1) == ".":
            if at(i - 2) in _GLOBAL_OBJECTS:
                return name
            continue  # 别的对象上同名的属性（x.Ls），不是这个全局
        if name == "catalogs" and is_tree_script:
            where = region(i)
            if (
                where
                and where not in load
                and values[i + 1 : i + 6]
                == [
                    "[",
                    "me",
                    ".",
                    "organId",
                    "]",
                ]
            ):
                continue  # 只在 getData 以后才跑的方法（蚌埠 open 里的 zdlyxxgkList）：受限求值管
        if not _known_root_use(values, i, is_tree_script, methods, widget):
            return name
    return None


def _known_root_use(values, i, is_tree_script, methods, widget):
    name = values[i]

    def at(k):
        return values[k] if 0 <= k < len(values) else ""

    before, after = at(i - 1), at(i + 1)
    if name == "Ls":
        j = i + 1
        while at(j) == "." and _IDENT.fullmatch(at(j + 1)):
            j += 2
        # 调用，或者给成员赋值（改哪些成员不行由写入规则查）
        return j > i + 1 and at(j) in ("(", "=")
    if name == "PublicTree":
        if after == "(" and before in ("function", "new"):
            return True
        return values[i - 4 : i] in (
            ["$", ".", "extend", "("],
            ["jQuery", ".", "extend", "("],
        ) and values[i + 1 : i + 4] == [".", "prototype", ","]
    if name == "catalogs":
        return (
            is_tree_script
            and values[i + 1 : i + 6] == ["[", "me", ".", "organId", "]"]
            and _catalog_sink(values, i, methods)
        )
    # _organTree2
    if after == "." and at(i + 3) == "(":
        return at(i + 2) in _TREE_READS or widget and at(i + 2) == "searchNodeLazy"
    if is_tree_script and before == "var":
        return values[i + 1 : i + 4] == ["=", "null", ";"]
    if is_tree_script and before in (";", "{", "}") and after == "=":
        return values[i + 1 : i + 9] == ["=", "$", ".", "fn", ".", "zTree", ".", "init"]
    return (
        widget
        and values[i - 4 : i] == ["me", ".", "fuzzySearch", "("]
        and values[i + 1 : i + 3] == [")", ";"]
    )


def _instantiated_on_load(scripts, token_sets, tree_index):
    """new PublicTree() 正好出现一次，就在 PublicTree 所在的脚本里，而且在页面加载时一定会执行：
    脚本顶层的一句（给 "top"：解析到这里就执行），或者顶层 $(function () {…})、jQuery(function
    () {…})、$(document).ready(function () {…}) 回调里的一句（回调里只有这一句；给 "ready"：DOM
    解析完才执行）。写在 if、别的函数、事件回调里的不算（给 None）。"""
    count = sum(
        _values(tokens)[i : i + 2] == ["new", "PublicTree"]
        for tokens in token_sets
        for i in range(len(tokens) - 1)
    )
    if count != 1:
        return False
    tokens = token_sets[tree_index]
    values = _values(tokens)
    depth = []
    level = 0
    for value in values:
        if value in (")", "]", "}"):
            level -= 1
        depth.append(level)
        if value in ("(", "[", "{"):
            level += 1
    for i in range(len(values) - 3):
        if values[i : i + 4] != ["new", "PublicTree", "(", ")"]:
            continue
        after = values[i + 4 : i + 5]
        if depth[i] == 0 and (i == 0 or values[i - 1] in (";", "}")) and after in ([], [";"]):
            return "top"
        body = ["new", "PublicTree", "(", ")"] + (after if after == [";"] else [])
        end = i + len(body)
        for opener in (
            ["$", "(", "function", "(", ")", "{"],
            ["jQuery", "(", "function", "(", ")", "{"],
            ["$", "(", "document", ")", ".", "ready", "(", "function", "(", ")", "{"],
            ["jQuery", "(", "document", ")", ".", "ready", "(", "function", "(", ")", "{"],
        ):
            start = i - len(opener)
            if start < 0 or values[start:i] != opener or depth[start] != 0:
                continue
            if start and values[start - 1] not in (";", "}"):
                continue
            if values[end : end + 2] == ["}", ")"]:
                return "ready"
    return None


def find_lonsun_api(
    html: str, page_url: str, column_url: str, *, catalog_host=None, county=False, _diagnosis=None
):
    """识别已确认的栏目页，返回栏目树请求及后续执行所需计划。"""
    from urllib.parse import urljoin

    from lxml import etree
    from lxml import html as lxml_html

    from youju.crawler.adapters import _STRAY_END, LONSUN, ListApi

    def fail(reason):
        if _diagnosis is not None:
            _diagnosis.append(reason)
        return None

    spec = column_spec(column_url)
    if spec is None:
        return None
    try:
        if url_key(page_url) != url_key(column_url):
            return None
        doc = lxml_html.document_fromstring(_STRAY_END.sub("", html))
        if next(doc.iter("base"), None) is not None:
            # 浏览器按 <base> 解析脚本和接口的相对地址，我们按页面地址拼：两边可能不一样
            return fail("页面有 <base>，脚本和接口的相对地址认不准")
        scripts = []
        candidates = []  # (地址, 什么时候执行)：("sync"/"defer"/"async", 在第几段脚本的位置)
        script_positions = []  # 各段内联脚本在页面脚本里的先后
        for position, el in enumerate(doc.iter("script")):
            if any(a.tag in ("noscript", "template") for a in el.iterancestors()):
                continue
            kind = _script_kind(el)
            if kind == "module":
                return fail("页面有 type=module 的脚本：浏览器会执行，我们不分析")
            if kind != "classic":
                continue  # 不执行的模板、数据
            src = el.get("src")
            if src is not None:
                timing = (
                    "async"
                    if el.get("async") is not None
                    else "defer"
                    if el.get("defer") is not None
                    else "sync"
                )
                candidates.append((src, (timing, position)))
            else:
                source = el.text or ""
                scripts.append(source)
                script_positions.append(position)
                written = _written_script(source)
                if written is not None:
                    # document.write 写出来的脚本在写它的这段脚本跑完以后、接着往下解析之前执行
                    candidates.append((written, ("written", position)))
        token_sets = [_js_tokens(source) for source in scripts]
        tree_scripts = [
            source
            for source, tokens in zip(scripts, token_sets, strict=True)
            if any(
                [t.value for t in tokens[i : i + 3]] == ["function", "PublicTree", "("]
                for i in range(len(tokens) - 2)
            )
        ]
        if len(tree_scripts) != 1:
            return None
        tree_methods = _public_tree_methods(tree_scripts[0])
        widget = _harmless_search_widget(tree_methods)
        load = _load_time_methods(tree_methods)
        functions = _page_functions(scripts)
        # 元素属性脚本先取出来（只用 new 调的函数要连它们一起看），srcdoc 照原来的位置报
        attribute_scripts = _attribute_scripts(doc)
        new_only = _new_only_constructors(
            token_sets, [source for source, _ in attribute_scripts or ()]
        )
        tree_declarations = 0
        tree_extensions = 0
        for source, tokens in zip(scripts, token_sets, strict=True):
            values = _values(tokens)
            is_tree_script = any(
                values[i : i + 3] == ["function", "PublicTree", "("] for i in range(len(values) - 2)
            )
            tree_writes = 0
            strip = _global_prefix_stripper(tokens, values)
            for path, op, site in _write_sites(tokens, values):
                # window.catalogs、window.window.catalogs、window["catalogs"] 同样是页面全局状态
                path = strip(path, site)
                if path[0] == "PublicTree" and op == "extend":
                    tree_extensions += 1
                if path[0] == "_organTree2" and is_tree_script:
                    tree_writes += 1
                    if path == ("_organTree2",) and op == "=" and tree_writes == 1:
                        continue
                if (
                    path[0] in ("catalogs", "_organTree2")
                    or path[0] == "PublicTree"
                    and op != "extend"
                    # 整个 Ls、按下标改的、我们照着推请求的那几个成员（发请求、取网址参数）；
                    # 给 Ls 加新函数（芜湖的 Ls.isPublicQcsmCatIds）不碍事：请求流程里要是用到，
                    # 受限求值认不出、不读
                    or path[0] == "Ls"
                    and (len(path) == 1 or path[1] in ("ajax", "ajaxGet", "url", "*"))
                    # 任何对象上的 .ajax、.ajaxGet 被改写（经别名改 $.ajax 也算）
                    or len(path) > 1
                    and path[-1] in ("ajax", "ajaxGet")
                    # 平台 Ls.ajax 拼地址用的 GLOBAL_CONTEXTPATH、jQuery 的全局请求设置、建树函数
                    or path[0] == "GLOBAL_CONTEXTPATH"
                    or "ajaxSettings" in path
                    or "zTree" in path
                ):
                    return fail("内联脚本改写了依赖状态")
            keep = (
                [
                    span
                    for name, span in _method_spans(tokens).items()
                    if name in ("findParentId", "findTopLevelParentNode")
                ]
                if is_tree_script
                else []
            )
            problem = (
                _ztree_problem(values)
                or _builtin_problem(tokens, values)
                or _global_object_problem(tokens, values)
                or _window_alias_problem(tokens, values)
                or _dynamic_code_problem(tokens, values)
                or _top_level_parent_problem(tokens, values, keep)
                or _pattern_write_problem(tokens, values)
                or _reach_problem(
                    tokens,
                    values,
                    source,
                    instance=_method_spans(tokens).values() if is_tree_script else (),
                    constructors=new_only,
                )
            )
            if not problem:
                # PublicTree 的原型方法另有规则查（open 里的 pushState 照着走）；构造函数在这里查
                spans = _method_spans(tokens) if is_tree_script else {}
                mask = [span for name, span in spans.items() if name]
                problem = _url_change_problem(tokens, values, mask)
            if problem:
                return fail(f"内联脚本{problem}")
            if is_tree_script:
                # PublicTree 自己的脚本：每处用法都得认得（白名单）；构造函数、原型方法以外的代码
                # 再照别的脚本的规矩查别名
                misuse = _root_misuse(tokens, True, tree_methods, widget, load)
                if misuse:
                    return fail(f"内联脚本里 {misuse} 的用法认不出，跟不上它会不会被改写")
                mask = [(tokens[a].end, tokens[b].start) for a, b in _method_spans(tokens).values()]
                problem = _foreign_problem(source, functions, widget, occurrences=False, mask=mask)
            else:
                # 别的脚本（搜索框、标签页、统计这类）：可以读，不能改
                problem = _foreign_problem(source, functions, widget)
            if problem:
                return fail(f"内联脚本：{problem}")
            for i in range(len(values) - 3):
                if values[i] == "." and values[i + 1] in _AJAX_SETTERS and values[i + 2] == "(":
                    return fail("页面改写了 AJAX 设置")
            for i in range(len(values) - 5):
                if (
                    values[i] in ("Object", "Reflect")
                    and values[i + 1] == "."
                    and values[i + 2] in ("assign", "defineProperty", "defineProperties", "set")
                    and values[i + 3 : i + 5] in (["(", "window"], ["(", "globalThis"])
                ):
                    end = _matching_token(tokens, i + 3)
                    args = values[i + 5 : end]
                    if any(
                        value.strip("\"'") in ("catalogs", "PublicTree", "Ls", "_organTree2")
                        for value in args
                    ):
                        return fail("内联脚本改写了依赖状态")
            for i in range(len(values) - 2):
                if (
                    values[i] == "var"
                    and values[i + 1] in ("catalogs", "PublicTree")
                    and values[i + 2] == "="
                ):
                    return fail("内联脚本改写了依赖状态")
                if values[i : i + 3] == ["var", "_organTree2", "="]:
                    tree_declarations += 1
                    if (
                        not is_tree_script
                        or tree_declarations > 1
                        or values[i + 3 : i + 5] != ["null", ";"]
                    ):
                        return fail("内联脚本改写了栏目树状态")
                if values[i : i + 3] in (
                    ["$", ".", "ajaxSetup"],
                    ["jQuery", ".", "ajaxSetup"],
                    ["$", ".", "ajaxPrefilter"],
                    ["jQuery", ".", "ajaxPrefilter"],
                ) and values[i + 3 : i + 4] == ["("]:
                    return fail("页面改写了 AJAX 设置")
        if attribute_scripts is None:
            return fail("页面有 srcdoc 内嵌页面：里面的脚本能改本页，我们不分析")
        for source, element_this in attribute_scripts:
            problem = _attribute_script_problem(source, functions, widget, element_this, new_only)
            if problem:
                return fail(f"页面元素属性里的脚本（onclick 这类、javascript: 链接）{problem}")
        init_method = _method(tree_methods, "init")
        ready = ""
        if init_method is not None and _getdata_deferred(init_method[2]):
            # 浏览器等那个请求成功才发列表请求：我们也先发它、看结果（只认真实页面那一种写法）
            ready = _ready_path({name: body for name, _, body in tree_methods})
            if ready is None:
                return fail("getData 要等页面另一个请求的结果，那个请求的写法认不出")
            # getData 推迟到回调里跑：别的脚本加载时替用户点一下，PublicTree 挂的点击处理函数就可能
            # 在发列表请求之前跑了（我们只把它们当"点了才跑"），跟不上
            for tokens in token_sets:
                values = _values(tokens)
                mask = []
                if any(
                    values[i : i + 3] == ["function", "PublicTree", "("]
                    for i in range(len(values) - 2)
                ):
                    mask = list(_method_spans(tokens).values())  # PublicTree 的方法另有规则查
                if _load_trigger(tokens, values, mask):
                    return fail("getData 推迟到回调里跑，页面别的脚本又在加载时替用户触发事件")
            for source, _ in attribute_scripts:
                tokens = _js_tokens(source)
                if _load_trigger(tokens, _values(tokens)):
                    return fail("getData 推迟到回调里跑，页面元素属性里的脚本又替用户触发事件")
        constructors = []
        extends = 0
        for n, tokens in enumerate(token_sets):
            v = _values(tokens)
            for i in range(len(v) - 2):
                if v[i : i + 7] == ["$", ".", "extend", "(", "PublicTree", ".", "prototype"]:
                    extends += 1
                if v[i : i + 3] == ["function", "PublicTree", "("]:
                    constructors.append((n, i))
        if len(constructors) != 1 or extends != 1 or tree_extensions != 1:
            return None
        n, index = constructors[0]
        tokens = token_sets[n]
        values = _values(tokens)
        if values[index : index + 5] != ["function", "PublicTree", "(", ")", "{"]:
            return None
        created = _instantiated_on_load(scripts, token_sets, n)
        if not created:
            return fail("页面没有在加载时创建 PublicTree")
        if created == "top":
            # 157 个真实页面都在就绪回调里创建；脚本顶层创建的，原型方法、平台脚本、栏目树脚本是不是
            # 都已经在前面跑过，先后认不准
            return fail("PublicTree 在脚本顶层创建，前后脚本的先后认不准")
        tree_position = script_positions[n]
        end = _matching_token(tokens, index + 4)
        statements = _parse_statements(scripts[n][tokens[index + 4].end : tokens[end].start])
        methods = _public_tree_methods(scripts[n])
        wanted = {
            "init",
            "getData",
            "open",
            "getList",
            "getNewList",
            "preprocessData",
            "isRefresh",
            "initTree",
        }
        if not {"init", "getData", "open", "getList", "getNewList"} <= {m[0] for m in methods}:
            return None
        parsed_methods = tuple(
            (name, args, _parse_statements(body)) for name, args, body in methods if name in wanted
        )
        me = {}
        env = _Environment({"me": me})
        assigned = {}
        for statement in statements:
            if statement[0] != "expression":
                continue
            expr = statement[1]
            if (
                expr[0] == "assign"
                and expr[1] == "="
                and expr[2][0] == "member"
                and expr[2][1] == ("identifier", "me")
                and expr[2][2][0] == "literal"
            ):
                key = expr[2][2][1]
                if key in (
                    "organId",
                    "prefix",
                    "actions",
                    "listParams",
                    "currCatId",
                    "query",
                    "hideIds",
                ):
                    if key in assigned:
                        return None
                    assigned[key] = expr[3]
                    if key in ("organId", "prefix", "actions"):
                        me[key] = _evaluate(expr[3], env)
        permitted = set(assigned) | {"ztreeData", "ztreeData2", "refresh"}
        # 回调里的写入也算（$.each(…, function () { me.listParams.x = … }) 在 init 之前就跑了）
        constructor_writes = _writes(scripts[n][tokens[index + 4].end : tokens[end].start])
        for path, op in constructor_writes:
            if _tracked_write(path) and not (
                len(path) == 2 and path[0] == "me" and path[1] in permitted and op == "="
            ):
                return None
        for key in ("ztreeData", "ztreeData2", "refresh"):
            if sum(path == ("me", key) for path, _ in constructor_writes) != 1:
                return None
        constructor_source = scripts[n][tokens[index + 4].end : tokens[end].start]
        method_names = frozenset(m[0] for m in methods)
        problem = _write_root_problem(constructor_source) or _me_alias_problem(
            constructor_source, method_names
        )
        if problem:
            return fail(f"构造函数{problem}")
        if _method_references(constructor_source, method_names) != ["init"]:
            # 只认末尾那一句 me.init()：别的方法在 init 之前跑，跟不上它们做了什么
            return fail("构造函数在 me.init() 以外用了 PublicTree 的方法")
        init_values = {}
        for statement in statements:
            if statement[0] == "expression" and statement[1][0] == "assign":
                expr = statement[1]
                target = _path(expr[2])
                if target in ("me.ztreeData", "me.ztreeData2", "me.refresh"):
                    if target in init_values or expr[1] != "=":
                        return None
                    init_values[target] = expr[3]
        order = [
            _path(statement[1][2])
            if statement[0] == "expression" and statement[1][0] == "assign"
            else None
            for statement in statements
        ]
        if "me.organId" in order and any(
            order.index(key) < order.index("me.organId")
            for key in ("me.ztreeData", "me.ztreeData2")
            if key in order
        ):
            return fail("构造函数在定下 organId 之前就取了栏目数据")
        tree_values = (
            _parse_expression("catalogs[me.organId]"),
            _parse_expression("JSON.parse(JSON.stringify(catalogs[me.organId]))"),
        )
        if init_values.get("me.ztreeData2") not in tree_values:
            return fail("构造函数的 ztreeData2 来源认不出")
        if init_values.get("me.ztreeData") not in tree_values or init_values.get("me.refresh") != (
            "literal",
            True,
        ):
            return fail("构造函数初值认不出")
        # 字段若在嵌套语句中被改写，不能只采用顶层赋值。
        constructor_values = values[index + 5 : end]
        for key in assigned:
            count = sum(
                constructor_values[i : i + 3] == ["me", ".", key]
                and constructor_values[i + 3] in ("=", "+=", "-=")
                for i in range(len(constructor_values) - 3)
            )
            if count != 1:
                return None
        init = ("expression", ("call", ("member", ("identifier", "me"), ("literal", "init")), ()))
        if not statements or statements[-1] != init:
            return None
        if me.get("organId") != spec[0] or not isinstance(me.get("organId"), str):
            return None
        if "prefix" in assigned and not isinstance(me.get("prefix"), str):
            return None
        label = me.get("actions", {}).get("label") if isinstance(me.get("actions"), dict) else None
        if (
            not isinstance(label, str)
            or not re.fullmatch(r"(/[A-Za-z0-9_\-]+)*/site/label/8888", label)
            or not label.startswith("/")
            or label.startswith("//")
            or not label.endswith("/site/label/8888")
            or "?" in label
            or "#" in label
            or ".." in label.split("/")
        ):
            return fail("接口路径认不出")
        required = {
            "currCatId": "Ls.url.get('catId')",
            "query": "Ls.url.parseQuery()",
            "hideIds": "[]",
        }
        if any(assigned.get(k) != _parse_expression(v) for k, v in required.items()):
            return None
        params = assigned.get("listParams")
        if params is None:
            # 蚌埠的 getList 自己构造参数，构造函数中没有 listParams。
            if any(v == "listParams" for v in values):
                return None
            params = ("object", ())
        if params[0] != "object" or len(dict(params[1])) != len(params[1]):
            return None
        if any(
            re.fullmatch(r"0|[1-9][0-9]*", str(k)) and int(k) < 4294967295 for k, _ in params[1]
        ):
            return fail("列表参数含整数键，顺序无法确定")
        organ = _parse_expression("me.organId")
        if any(tree[0] != "literal" and tree != organ for _, tree in params[1]):
            return None
        list_params = tuple((k, _immutable(_evaluate(v, env))) for k, v in params[1])
        urls = []
        for candidate, (timing, position) in candidates:
            # 浏览器先去首尾空白、删换行和制表符、把反斜杠当斜杠：规整以后像栏目树脚本、规整前后又不
            # 一样的，浏览器会去取、我们拼的地址不一样，不读
            tidy = re.sub(r"[\t\n\r]", "", candidate.strip(" \t\n\f\r")).replace("\\", "/")
            tidy_path = urlsplit(urljoin(page_url, tidy)).path
            if tidy != candidate and re.search(
                r"/[0-9]+/" + re.escape(spec[0]) + r"\.js$", tidy_path
            ):
                return fail("栏目树脚本地址里有反斜杠、空白、控制字符或非 ASCII 字符，认不准")
            url = urljoin(page_url, candidate)
            parts = urlsplit(url)
            if re.search(r"/[0-9]+/" + re.escape(spec[0]) + r"\.js$", parts.path):
                if _odd_url(candidate):
                    return fail("栏目树脚本地址里有反斜杠、空白、控制字符或非 ASCII 字符，认不准")
                # 构造函数执行时栏目树脚本得已经跑过（catalogs 有了），浏览器才会发列表请求：
                # async 的什么时候跑说不准；就绪回调里创建的，defer、写在后面的都来得及；脚本
                # 顶层创建的，栏目树脚本得是写在前面的普通脚本（或者前面的脚本 document.write 的）
                if timing == "async":
                    return fail("栏目树脚本带 async，什么时候执行说不准")
                if created == "top" and (timing == "defer" or position >= tree_position):
                    return fail("PublicTree 在脚本顶层创建，栏目树脚本那时还没执行")
                if urlsplit(page_url).scheme == "https" and parts.scheme == "http":
                    return fail("HTTPS 页面引用了 HTTP 栏目树脚本")
                if not catalog_host_allowed(page_url, url, catalog_host, county=county):
                    page_host = urlsplit(page_url).hostname
                    if catalog_host_allowed(
                        f"https://{page_host}/",
                        f"https://{parts.hostname}/",
                        catalog_host,
                        county=county,
                    ):
                        return fail("栏目树脚本的协议、端口或用户名密码不符合要求")
                    if county:
                        return fail(
                            f"区县站的栏目树只认来源配置的本市白名单门户：栏目页 {page_host}，"
                            f"脚本 {parts.hostname}；没配置、配置的不是本市门户，或本市不在白名单里"
                        )
                    return fail(
                        f"栏目树脚本不在同一个政府网站域名上：栏目页 {page_host}，"
                        f"脚本 {parts.hostname}；脚本不是本市白名单门户，"
                        "或本市不在白名单里、所属城市为空"
                    )
                urls.append(url)
        if not urls:
            return fail("没找到栏目树脚本")
        if len(set(urls)) != 1:
            # 只读页面原样写着的那一个 src（协调会话 11/13）：同一份脚本写了几次可以（地址完全
            # 一样）；主机、路径或查询串不一样的（比如 ?v=1 和 ?v=2，返回可能不同，浏览器两份都会
            # 执行），说不清最后用的是哪份栏目树，认不出
            return fail("栏目树脚本有几处、地址不一样")
        plan = LonsunPlan(
            spec[0],
            me.get("prefix", ""),
            label,
            list_params,
            _page_globals(scripts),
            parsed_methods,
            methods,
            ready,
        )
        return ListApi(adapter=LONSUN, url=urls[-1], base_url=page_url, plan=plan)
    except LonsunFormatError as exc:
        return fail(f"脚本认不出：{exc}")
    except (
        ValueError,
        etree.LxmlError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
        OverflowError,
    ):
        return None


def lonsun_unrecognized_reason(
    html: str,
    page_url: str,
    column_url: str,
    *,
    catalog_host: str | None = None,
    county: bool = False,
) -> str | None:
    """仅对龙讯栏目页给出第一步识别失败的原因。"""
    if column_spec(column_url) is None:
        return None
    from lxml import etree
    from lxml import html as lxml_html

    from youju.crawler.adapters import _STRAY_END

    try:
        doc = lxml_html.document_fromstring(_STRAY_END.sub("", html))
        sources = [
            el.text or ""
            for el in doc.iter("script")
            if el.get("src") is None
            and not any(a.tag in ("noscript", "template") for a in el.iterancestors())
            and _script_kind(el) == "classic"
        ]

        def has_constructor(source):
            tokens = [token.value for token in _js_tokens(source)]
            return any(
                tokens[i : i + 3] == ["function", "PublicTree", "("] for i in range(len(tokens) - 2)
            )

        if not any(has_constructor(source) for source in sources):
            return None
    except (ValueError, etree.LxmlError):
        return None
    reasons = []
    if (
        find_lonsun_api(
            html,
            page_url,
            column_url,
            catalog_host=catalog_host,
            county=county,
            _diagnosis=reasons,
        )
        is not None
    ):
        return None
    return reasons[0] if reasons else "页面脚本认不出（PublicTree 写法或构造函数）"


def _platform_query(query: str) -> dict[str, str]:
    """按平台 parseQuery 解码，不把加号变为空格。"""

    def decode(value: str) -> str:
        try:
            if re.search(r"%(?![0-9a-fA-F]{2})", value):
                return value
            return unquote(value, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return value

    result = {}
    for part in query.removeprefix("?").split("&"):
        key, _, value = part.partition("=")
        result[decode(key)] = decode(value)
    return result


# 列表请求里认得的参数名（样本和缓存的真实页面里出现过的）；别的一律不发（令牌、签名这类停下来问）
_REQUEST_KEYS = frozenset(
    {
        "action",
        "catId",
        "catIds",
        "catalogType",
        "dateFormat",
        "isDate",
        "isJson",
        "isRel",
        "isSetValue",
        "keyWords",
        "labelName",
        "length",
        "num",
        "organId",
        "pageIndex",
        "pageSize",
        "queryDetail",
        "result",
        "siteId",
        "type",
    }
)


@dataclass(frozen=True)
class LonsunRequestPlan:
    mode: str
    cat_id: str
    excluded_children: tuple[int, ...] = ()


@dataclass(frozen=True)
class LonsunReadyPlan:
    """列表请求前面那个请求（芜湖这类，见 _ready_path）：浏览器等它成功才发列表请求。"""

    path: str


@dataclass(frozen=True)
class _Node:
    data: dict
    children: object
    modified: frozenset


def _path(tree):
    if tree[0] == "identifier":
        return tree[1]
    if tree[0] == "member" and tree[2][0] == "literal":
        parent = _path(tree[1])
        return parent + "." + str(tree[2][1]) if parent else None
    return None


def _call_path(tree):
    return _path(tree[1]) if tree[0] == "call" else None


def _tree_rules(raw):
    pre = raw.get("preprocessData", "")
    tokens = [t.value for t in _js_tokens(pre)]
    modified = frozenset(
        tokens[i + 1] for i in range(len(tokens) - 2) if tokens[i] == "." and tokens[i + 2] == "="
    )
    for i in range(len(tokens) - 3):
        if tokens[i : i + 3] == ["me", ".", "ztreeData2"] and tokens[i + 3] in (
            "=",
            "+=",
            "-=",
            "++",
            "--",
        ):
            raise LonsunFormatError("preprocessData 改写栏目树")
        if tokens[i : i + 3] == ["me", ".", "ztreeData"] and tokens[i + 3] in (
            "=",
            "+=",
            "-=",
            "++",
            "--",
        ):
            tail = tokens[i + 4 :]
            pattern = ["$", ".", "grep", "(", "me", ".", "ztreeData", ",", "function", "("]
            if tokens[i + 3] != "=" or tail[:10] != pattern or len(tail) < 27:
                raise LonsunFormatError("栏目树过滤方式认不出")
            arg = tail[10]
            expected = [
                arg,
                ")",
                "{",
                "return",
                "ids",
                ".",
                "indexOf",
                "(",
                arg,
                ".",
                "id",
                ")",
                "===",
                "-",
                "1",
                ";",
                "}",
                ")",
            ]
            if tail[10:28] != expected or (len(tail) > 28 and tail[28] not in (";", "}")):
                raise LonsunFormatError("栏目树过滤方式认不出")
    modified = modified | frozenset(
        path[-1]
        for path, _ in _writes(pre)
        if len(path) > 1 and path[0] not in ("me", "$", "jQuery")
    )
    occurrences = [i for i, v in enumerate(tokens) if v == "ids"]
    hide_ok = not pre
    if pre:
        decl = assign = 0
        hide_ok = True
        for i in occurrences:
            if tokens[max(0, i - 1) : i + 4] == ["var", "ids", "=", "[", "]"]:
                decl += 1
            elif tokens[max(0, i - 4) : i + 1] == ["me", ".", "hideIds", "=", "ids"]:
                assign += 1
            elif tokens[i + 1 : i + 4] == [".", "indexOf", "("]:
                pass
            else:
                hide_ok = False
        hide_ok = hide_ok and decl == assign == 1
    hide_writes = sum(
        tokens[i : i + 3] == ["me", ".", "hideIds"]
        and tokens[i + 3] in ("=", "+=", "-=", "++", "--")
        for i in range(len(tokens) - 3)
    )
    if pre and hide_writes != 1:
        hide_ok = False
    for name, body in raw.items():
        if name == "preprocessData":
            continue
        v = [t.value for t in _js_tokens(body)]
        if any(
            v[i : i + 3] == ["me", ".", "hideIds"] and v[i + 3] in ("=", "+=", "-=", "++", "--")
            for i in range(len(v) - 3)
        ):
            hide_ok = False
    v = [t.value for t in _js_tokens(raw.get("initTree", ""))]
    marker = ["_organTree2", "=", "$", ".", "fn", ".", "zTree", ".", "init", "("]
    found = [i for i in range(len(v)) if v[i : i + len(marker)] == marker]
    writes = sum(
        v[i] == "_organTree2" and v[i + 1] in ("=", "+=", "-=", "++", "--")
        for i in range(len(v) - 1)
    )
    if len(found) != 1 or writes != 1:
        raise LonsunFormatError("initTree 栏目树写法认不出")
    i = found[0] + len(marker) - 1
    ts = _js_tokens(raw["initTree"])
    end = _matching_token(ts, i)
    args = v[i + 1 : end]
    depth = 0
    commas = 0
    for token in args:
        if token in ("(", "[", "{"):
            depth += 1
        elif token in (")", "]", "}"):
            depth -= 1
        elif token == "," and depth == 0:
            commas += 1
    if commas != 2:
        raise LonsunFormatError("initTree 栏目树参数认不出")
    if (
        args[-3:] not in (["me", ".", "ztreeData"], ["me", ".", "ztreeData2"])
        or len(args) < 4
        or args[-4] != ","
    ):
        raise LonsunFormatError("initTree 栏目树写法认不出")
    cuts, depth = [], 0
    for k, token in enumerate(args):
        if token in ("(", "[", "{"):
            depth += 1
        elif token in (")", "]", "}"):
            depth -= 1
        elif token == "," and depth == 0:
            cuts.append(k)
    problem = _tree_settings_problem(raw["initTree"], args[cuts[0] + 1 : cuts[1]])
    if problem:
        raise LonsunFormatError(f"initTree {problem}")
    if args[-1] == "ztreeData" and not hide_ok:
        raise LonsunFormatError("页面会按自己的设置隐去部分下级栏目，认不出隐去哪些")
    return modified, hide_ok


# getData 以后、发列表请求以前，受限求值当成只动页面显示、直接跳过的方法
_UI_METHODS = ("setMeta", "getNav", "getGd")


def _ui_method_problem(body, method_names=frozenset()):
    """被跳过的方法体里（含回调）不能改 me/this 上的任何东西、不能改那几个全局对象，也不能再用
    PublicTree 的方法（me.X()、me['X']、(me.X)() 都算；真实页面都不这样写）。"""
    references = _method_references(body, method_names)
    if references is None:
        return "按算出来的名字用 me 的成员"
    if references:
        return f"用了 PublicTree 的方法 {references[0]}"
    for path, _op in _writes(body):
        if path[0] in ("me", "this", "catalogs", "_organTree2", "Ls", "PublicTree"):
            return f"改了 {'.'.join(path)}"
    values = [t.value for t in _js_tokens(body)]
    for i in range(len(values) - 3):
        if values[i] in ("me", "this") and values[i + 1] == "." and values[i + 3] == "(":
            return f"调用了 {values[i]}.{values[i + 2]}()"
    return _write_root_problem(body, me_ok=False) or _me_alias_problem(body)


# 建树设置里建树时就跑的显示回调（view.fontCss、view.addDiyDom）和 open 里发列表请求之前跑的显示
# 方法（setMeta、getNav）：出错的话浏览器不发列表请求。照真实页面的写法逐个记号比对（157 页只有下面
# 这几种；§A 是一串字符串或数字字面量，§S 是一个字符串字面量），比对上的只读节点字段、me 的字段，
# 用 $ 选元素、改元素，发不阻塞的请求，不会出错。
_SHOW_COND = (
    "( ids . indexOf ( node . id ) > - 1 || node . link "
    "|| ( node . isTreeShow === 0 && node . treeLink ) )"
)
_FONT_CSS = _words(
    "function setFontCss ( treeId , node ) { var ids = [ §A ] ; "
    "if " + _SHOW_COND + " { var color = §S ; var organIds = [ §A ] ; "
    "if ( organIds . indexOf ( me . organId ) > - 1 ) { color = §S ; } "
    "return { color : color } ; } else { return { } ; } }"
)
_DIY_HEAD = "function addDiyDom ( treeId , node ) { var ids = [ §A ] ; "
_DIY_COND = (
    "if " + _SHOW_COND + " { var cls = §S var organIds = [ §A ] ; "
    "if ( organIds . indexOf ( me . organId ) > - 1 ) { cls = §S ; } "
    'var aObj = $ ( "#" + node . tId + "_a" ) ; '
)
_DIY_APPEND = "aObj . append ( §S + cls + §S ) . addClass ( §S ) ; } }"
_ADD_DIY_DOM = (
    _words(_DIY_HEAD + _DIY_COND + _DIY_APPEND),
    # 六安、淮北：名字按级别折行（getNewline 是 initTree 里的局部函数，见 _GET_NEWLINE）
    _words(
        _DIY_HEAD
        + _DIY_COND
        + "var name = getNewline ( node . name , node . level ) ; "
        + "aObj . html ( name ) . "
        + _DIY_APPEND.removeprefix("aObj . ")
    ),
    # 省发改委：先给链接加无障碍属性
    _words(
        _DIY_HEAD
        + 'var aObj = $ ( "#" + node . tId + "_a" ) '
        + ". attr ( { 'tabindex' : 0 , 'data-wza-enter-trigger' : 'click' } ) "
        + ". removeAttr ( 'aria-expanded' ) . removeAttr ( 'target' ) ; "
        + 'var switchObj = $ ( "#" + node . tId + "_switch" ) '
        + ". removeAttr ( 'tabindex' ) ; "
        + _DIY_COND
        + _DIY_APPEND
    ),
)
_GET_NEWLINE = _words(
    "function getNewline ( str , level ) { var newNum = 8 - level || 8 ; var res = '' ; "
    "for ( var i = 0 , l = str . length ; i < l ; i ++ ) { "
    "if ( i > 0 && i % newNum == 0 ) { res += str [ i ] + '<br>' ; } "
    "else { res += str [ i ] ; } } return res ; }"
)
_SET_META = _words(
    "var me = this ; "
    '$ ( \'meta[name="ColumnType"],meta[name="ColumnDescription"],'
    "meta[name=\"ColumnKeywords\"]' ) . attr ( 'content' , me . currNode . name ) ; "
    "$ ( 'meta[name=\"Attribute\"]' ) . attr ( 'content' , me . currNode . attribute ) ;"
)
# getNav、getGd：同步的部分只是发一个不阻塞的请求（我们不发，只少不多）；done 里的回调等它回来才跑，
# 那时列表请求早发出去了（照加载时的规矩另查改了什么）
_UI_HEAD = (
    "var me = this ; Ls . ajaxGet ( { url : me . actions . label , dataType : 'HTML' , data : { "
)
_UI_REQUEST = {
    "getNav": _words(
        _UI_HEAD + "labelName : 'publicNav' , organId : me . organId , "
        "catId : me . currNode . id , type : 4 , } , } ) . done ( function ( str ) {"
    ),
    "getGd": _words(
        _UI_HEAD + "labelName : 'publicGuiDangPic' , organId : me . organId , "
        "catId : me . currNode . id , isJson : true } , } ) . done ( function ( res ) {"
    ),
}
_RISKY_TEXT = re.compile(
    r"script|javascript:|\bon[a-z]+\s*=|<\s*(?:img|iframe|svg|object|embed|base|meta|link)", re.I
)


def _shape_match(template, values, start=0):
    """values 从 start 起是不是照 template 的写法（§A：零个或几个逗号隔开的字符串、数字字面量；
    §S：一个字符串字面量，里面不许有脚本、事件属性这类字）。是就给比对完的位置，不是给 None。"""
    k = start
    for want in template:
        if want == "§S":
            if k >= len(values) or values[k][:1] not in ("'", '"') or _RISKY_TEXT.search(values[k]):
                return None
            k += 1
        elif want == "§A":
            while k < len(values) and values[k] != "]":
                literal = values[k]
                if not (
                    literal[:1] in ("'", '"')
                    and not _RISKY_TEXT.search(literal)
                    or re.fullmatch(r"[0-9]+", literal)
                ):
                    return None
                k += 1
                if k < len(values) and values[k] == ",":
                    k += 1
        elif k >= len(values) or values[k] != want:
            return None
        else:
            k += 1
    return k


def _view_problem(source, tokens, values, start, end):
    """建树设置的 view：只许 nameIsHTML（真假）、fontCss、addDiyDom，回调照真实写法比对。"""
    if values[start] != "{" or _matching_token(tokens, start) != end - 1:
        return "建树设置的 view 写法认不出"
    k, seen = start + 1, set()
    while k < end - 1:
        key = values[k]
        if key in seen or values[k + 1 : k + 2] != [":"]:
            return "建树设置的 view 写法认不出"
        seen.add(key)
        if key == "nameIsHTML" and values[k + 2] in ("true", "false"):
            k += 3
        elif key in ("fontCss", "addDiyDom"):
            templates = (_FONT_CSS,) if key == "fontCss" else _ADD_DIY_DOM
            ends = [_shape_match(t, values, k + 2) for t in templates]
            done = next((e for e in ends if e is not None), None)
            if done is None:
                return f"建树设置的显示回调 {key} 写法认不出，算不出建树时会不会出错"
            if "getNewline" in values[k + 2 : done]:
                locals_ = [
                    f
                    for f in range(len(values) - 1)
                    if values[f : f + 2] == ["function", "getNewline"]
                ]
                if len(locals_) != 1 or _shape_match(_GET_NEWLINE, values, locals_[0]) is None:
                    return "建树设置的显示回调用的 getNewline 写法认不出"
            k = done
        else:
            return f"建树设置的 view 有认不出的项 {key}"
        if k < end - 1:
            if values[k] != ",":
                return "建树设置的 view 写法认不出"
            k += 1
    return None


def _callback_problem(tokens, values, start, end):
    """建树设置的 callback：只许用户点了才跑的（onClick 这类）。"""
    if values[start] != "{" or _matching_token(tokens, start) != end - 1:
        return "建树设置的 callback 写法认不出"
    k = start + 1
    while k < end - 1:
        key = values[k]
        if key not in _ZTREE_CLICKS or values[k + 1 : k + 3] != [":", "function"]:
            return f"建树设置的 callback 有建树时就会跑的项 {key}"
        found = _function_at(tokens, values, k + 2)
        if found is None:
            return "建树设置的 callback 写法认不出"
        k = found[2] + 1
        if k < end - 1:
            if values[k] != ",":
                return "建树设置的 callback 写法认不出"
            k += 1
    return None


def _ui_shape_problem(name, body):
    """open 里发列表请求之前跑的显示方法（setMeta、getNav、getGd）照真实写法比对。"""
    tokens = _js_tokens(body)
    values = _values(tokens)
    if name == "setMeta":
        return None if values == _SET_META else "写法认不出，算不出发列表请求之前会不会出错"
    head = _UI_REQUEST[name]
    if values[: len(head)] != head:
        return "写法认不出，算不出发列表请求之前会不会出错"
    close = _matching_token(tokens, len(head) - 1)
    tail = values[close + 1 :]
    if tail not in ([")"], [")", ";"]):
        return "写法认不出，算不出发列表请求之前会不会出错"
    return None


def _display_name_problem(raw, nodes):
    """六安、淮北的 addDiyDom 用 getNewline 给栏目名折行（str.length、str[i]）：栏目树里有节点的
    名字不是字符串，建树时就出错，浏览器不发列表请求。"""
    tree = _values(_js_tokens(raw.get("initTree", "")))
    if "getNewline" in tree and any(not isinstance(node.get("name"), str) for node in nodes):
        return "栏目树里有节点的名字不是字符串，建树时显示回调会出错，浏览器不发列表请求"
    return None


def _tree_settings_problem(source, setting):
    """建树设置：只认 initTree 里 var 声明的一个对象，顶层只有 data、view、callback；data 里
    simpleData 正好是 idKey 'id'、pIdKey 'parentId'（可带 enable: true），key 只能改显示用的
    title、name。建好以后不许再改。真实页面都是这样写的。"""
    tokens = _js_tokens(source)
    values = _values(tokens)
    if len(setting) != 1 or not _IDENT.fullmatch(setting[0]):
        return "建树设置不是一个变量"
    name = setting[0]
    starts = [i for i in range(len(values) - 3) if values[i : i + 4] == ["var", name, "=", "{"]]
    if len(starts) != 1:
        return "建树设置找不到或声明了几次"
    if any(path[0] == name for path, _ in _writes(source)):
        return "建树设置建好以后又改了"
    open_brace = starts[0] + 3
    close = _matching_token(tokens, open_brace)
    props, k = {}, open_brace + 1
    while k < close:
        key = values[k]
        key = _string_literal(key) if key[:1] in ("'", '"') else key
        if key in props or values[k + 1 : k + 2] != [":"]:
            return "建树设置写法认不出"
        start = k + 2
        k = start
        while k < close and values[k] != ",":
            k = _matching_token(tokens, k) + 1 if values[k] in ("(", "[", "{") else k + 1
        props[key] = (start, k)
        k += 1
    if not set(props) <= {"data", "view", "callback"} or "data" not in props:
        return "建树设置有认不出的项"
    start, end = props["data"]
    data = _evaluate(
        _parse_expression(source[tokens[start].start : tokens[end - 1].end]), _Environment()
    )
    simple = data.get("simpleData") if isinstance(data, dict) else None
    key = data.get("key", {}) if isinstance(data, dict) else None
    if (
        not isinstance(simple, dict)
        or not set(data) <= {"simpleData", "key"}
        or {k: v for k, v in simple.items() if k != "enable"}
        != {"idKey": "id", "pIdKey": "parentId"}
        or simple.get("enable", True) is not True
        or not isinstance(key, dict)
        or not set(key) <= {"title", "name"}
        or not all(isinstance(v, str) for v in key.values())
    ):
        return "建树设置的字段名认不出（要 idKey id、pIdKey parentId，不改 children）"
    if "view" in props:
        problem = _view_problem(source, tokens, values, *props["view"])
        if problem:
            return problem
    if "callback" in props:
        problem = _callback_problem(tokens, values, *props["callback"])
        if problem:
            return problem
    return None


def _init_calls_getdata(body):
    direct = _parse_statements("me.getData();")[0]
    seen = set()
    allowed = {
        "bindEvt",
        "isRefresh",
        "preprocessData",
        "findParentId",
        "initTree",
        "getReportyear",
        "setSelect",
    }
    for st in _parse_statements(body):
        if st == direct:
            return {"isRefresh", "initTree"} <= seen
        if st == _parse_statements("var me = this;")[0]:
            continue
        if st[0] != "expression":
            return False
        expr = st[1]
        path = _call_path(expr)
        if path and path.startswith("me.") and path[3:] in allowed and not expr[2]:
            seen.add(path[3:])
            continue
        if (
            expr[0] == "call"
            and expr[1][0] == "member"
            and expr[1][2] == ("literal", "done")
            and len(expr[2]) == 1
        ):
            inner, callback = expr[1][1], expr[2][0]
            if (
                (_call_path(inner) or "").startswith("me.")
                and not inner[2]
                and callback[0] == "function"
                and not callback[2]
                and _parse_statements(body[callback[3] : callback[4]]) == (direct,)
            ):
                return {"isRefresh", "initTree"} <= seen
        return False
    return False


def _has_unmodeled_call(tree, env):
    if not isinstance(tree, tuple) or not tree:
        return False
    if tree[0] == "function":
        return False
    if tree[0] == "call" and _evaluate(tree, env) is UNKNOWN:
        return True
    return any(
        _has_unmodeled_call(x, env)
        for part in tree[1:]
        for x in (
            part if isinstance(part, tuple) and part and isinstance(part[0], tuple) else (part,)
        )
    )


def _callback_exclusions(source, callback, parent, hide_ok, mode):
    """只接受已知的列表回调，按实际分支记录页面隐藏的下级。"""
    if callback is None or len(callback[2]) != 1:
        raise LonsunFormatError("页面对返回的列表另有处理：回调参数")
    response = callback[2][0]
    env = _Environment({response: UNKNOWN}, parent)
    excluded = []
    display_vars = set()
    is_link_empty = False
    data_ready = response == "data"
    body = source[callback[3] : callback[4]]

    def fail(text):
        raise LonsunFormatError("页面对返回的列表另有处理：" + text.strip()[:40])

    def pure(tree):
        if tree is None:
            return True
        if not isinstance(tree, tuple):
            return True
        if tree and tree[0] in ("call", "function", "assign", "unknown"):
            return False
        return all(pure(part) for part in (tree[1:] if tree and isinstance(tree[0], str) else tree))

    def function_body(fn):
        return _parse_statements(body[fn[3] : fn[4]])

    def grep(expr, input_name, kind):
        if _call_path(expr) != "$.grep" or len(expr[2]) != 2:
            return None
        target, fn = expr[2]
        if target != ("identifier", input_name) or fn[0] != "function" or len(fn[2]) != 1:
            return None
        arg = fn[2][0]
        statements = function_body(fn)
        if kind == "hide":
            expected = _parse_statements(f"return me.hideIds.indexOf({arg}.catId) === -1;")
            return True if statements == expected and hide_ok else None
        if len(statements) != 1 or statements[0][0] != "return":
            return None
        value = statements[0][1]
        if (
            value
            and value[0] == "binary"
            and value[1] == "!=="
            and _path(value[2]) == arg + ".catId"
            and value[3][0] == "literal"
            and type(value[3][1]) is float
            and value[3][1].is_integer()
            and value[3][1] >= 0
        ):
            return int(value[3][1])
        return None

    positions = {}

    def run(st, text, conditional=False):
        nonlocal is_link_empty, data_ready
        text = positions.get(id(st), text)
        kind = st[0]
        if kind == "empty":
            return
        if kind == "block":
            for child in st[1]:
                run(child, text, conditional)
            return
        if kind == "if":
            condition = _evaluate(st[1], env)
            if condition is UNKNOWN:
                fail(text)
            branch = st[2] if _truth(condition) else st[3]
            if branch:
                run(branch, text, True)
            return
        if kind == "var":
            for name, value in st[1]:
                if name == "isLink":
                    if is_link_empty or value != ("array", ()):
                        fail(text)
                    is_link_empty = True
                elif name == "data":
                    if data_ready:
                        fail(text)
                    if value in (
                        ("identifier", response),
                        ("member", ("identifier", response), ("literal", "data")),
                    ) or (value and grep(value, response, "hide") is True):
                        data_ready = True
                    else:
                        fail(text)
                elif name == response or not pure(value):
                    fail(text)
                else:
                    display_vars.add(name)
                    env.values[name] = _evaluate(value, env) if value else UNDEFINED
            return
        if kind == "expression":
            expr = st[1]
            if expr[0] == "assign" and expr[1] == "=":
                target = _path(expr[2])
                if target == "data" and data_ready and conditional:
                    child = grep(expr[3], "data", "child")
                    if child is not None:
                        if mode == "list":
                            fail(text)
                        excluded.append(child)
                        return
                if target in display_vars and pure(expr[3]):
                    env.values[target] = _evaluate(expr[3], env)
                    return
                fail(text)
            path = _call_path(expr)
            if path in {
                "Ls.template",
                "Ls.www.scroll",
                "me.setPagination",
                "me.setItemRelInfo",
                "tipsLoading.hide",
                "console.log",
                "setTimeout",
            }:
                return
            if (
                expr[0] == "call"
                and expr[1][0] == "member"
                and expr[1][2] == ("literal", "html")
                and _call_path(expr[1][1]) == "$"
            ):
                return
            if (
                path == "$.each"
                and len(expr[2]) == 2
                and expr[2][0] in (("identifier", "data"), ("identifier", "res"))
                and (data_ready or expr[2][0] == ("identifier", response))
            ):
                fn = expr[2][1]
                if fn[0] == "function" and len(fn[2]) == 2:
                    index, item = fn[2]
                    each_body = body[fn[3] : fn[4]]
                    tokens = [t.value for t in _js_tokens(each_body)]
                    locals_ = {
                        tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token == "var"
                    }
                    forbidden = {
                        "push",
                        "splice",
                        "pop",
                        "shift",
                        "unshift",
                        "sort",
                        "reverse",
                        "filter",
                        "slice",
                        "map",
                        "concat",
                    }
                    writes = _writes(each_body)
                    if (
                        not ({"data", "res"} & set(tokens))
                        and not any(
                            tokens[i] in forbidden and tokens[i + 1] == "("
                            for i in range(len(tokens) - 1)
                        )
                        and all(
                            (len(path) == 1 and path[0] in locals_)
                            or path in ((item, "index"), (item, "relInfo"), (item, "relInfo2"))
                            for path, _ in writes
                        )
                        and not any(t in ("function", "=>", "delete") for t in tokens)
                    ):
                        if expr[2][0] == ("identifier", response):
                            data_ready = True
                        return
                    statements = function_body(fn)
                    link = _parse_statements(
                        f"if (isLink.indexOf({item}.catId) > -1) {{ {item}.redirectLink = true; }}"
                    )
                    if statements == link and is_link_empty:
                        return
                    if len(statements) == 1 and statements[0][0] == "expression":
                        assignment = statements[0][1]
                        if (
                            assignment[0] == "assign"
                            and assignment[1] == "="
                            and _path(assignment[2]) == item + ".index"
                            and pure(assignment[3])
                        ):
                            return
            fail(text)
        fail(text)

    class CallbackParser(_JSParser):
        def statement(self):
            start = self.tokens[self.i].start
            statement = super().statement()
            end = self.tokens[self.i - 1].end
            positions[id(statement)] = body[start:end]
            return statement

    parser = CallbackParser(body)
    while parser.peek() != "<结束>":
        start = parser.tokens[parser.i].start
        st = parser.statement()
        end = parser.tokens[parser.i - 1].end
        run(st, body[start:end])
    if not data_ready:
        fail("未识别列表数据")
    return tuple(excluded)


# setSelect（initTree 里调，在 getData 之前）：不是树一顶层的栏目，顺着 parentId 往上找 findParentId
# 定下的上级（topLevelParentId）。栏目树有几个顶层上级、本栏目又不挂在那个下面时，会走到不存在的
# 节点抛错（或者绕圈出不来），init 就此中断、浏览器不发列表请求。157 个真实页面上这几个方法只有
# 下面这几种写法：照写法逐个记号比对，算出会不会出错

# 芜湖这类：init 里 me.getPublicQcsmCatIds().done(function () { me.getData(); })——浏览器要等这个
# 请求成功（回 2xx、按 JSON 解析得了、done 里的回调不出错）才发列表请求。19 个真实页面上这个方法
# 只有这一种写法（路径是字面量）；回调在 res.status 为真而 res.data 不是字符串时出错（见
# adapters.lonsun_ready_ok）
_READY_HEAD = _words("var me = this ; return Ls . ajaxGet ( { url :")
_READY_TAIL = _words(
    "} ) . done ( function ( res ) { if ( res . status ) { var data = res . data ; "
    "publicQcsmCatIds = [ ] ; $ . each ( data . split ( ',' ) , function ( i , item ) { "
    "item && publicQcsmCatIds . push ( item ) ; } ) } } )"
)


_SET_SELECT_PREFIX = _words(
    "var me = this ; var currCatId = me . currCatId ; if ( currCatId ) { var nodeCat = "
    "_organTree . getNodeByParam ( 'id' , currCatId , null ) ; if ( ! nodeCat ) { var pNode = "
    "me . findTopLevelParentNode ( + currCatId , catalogs [ me . organId ] ) ; if ( pNode ) { "
    "nodeCat = _organTree . getNodeByParam ( 'id' , pNode . id , null ) ; } } if ( nodeCat ) { "
    "_organTree . selectNode ( nodeCat , false , true ) ;"
)
_FIND_TOP = _words(
    "var me = this ; var currentNode = $ . grep ( nodes , function ( node ) { return node . id "
    "== nodeId ; } ) [ 0 ] ; while ( currentNode . parentId !== me . topLevelParentId ) { "
    "currentNode = $ . grep ( nodes , function ( node ) { return node . id === currentNode . "
    "parentId ; } ) [ 0 ] ; } return currentNode ;"
)
_FIND_PARENT_HEAD = _words(
    "var me = this ; var idMap = { } ; $ . each ( me . ztreeData2 , function ( i , item ) { idMap "
    "[ item . id ] = true ; } ) ; var topLevelParentIds = [ ] ; $ . each ( me . ztreeData2 , "
    "function ( i , item ) { if ( item . parentId != null && ! idMap [ item . parentId ] ) { "
    "topLevelParentIds . push ( item . parentId ) ; } } ) ;"
)
_FIND_PARENT_FIRST = _FIND_PARENT_HEAD + _words("me . topLevelParentId = topLevelParentIds [ 0 ] ;")
_FIND_PARENT_FREQUENT = _FIND_PARENT_HEAD + _words(
    "me . topLevelParentId = findMostFrequent ( topLevelParentIds ) ; function findMostFrequent "
    "( arr ) { var map = { } ; var maxCount = 0 ; var result = arr [ 0 ] ; for ( var i = 0 ; i "
    "< arr . length ; i ++ ) { var num = arr [ i ] ; map [ num ] = ( map [ num ] || 0 ) + 1 ; if "
    "( map [ num ] > maxCount ) { maxCount = map [ num ] ; result = num ; } } return result ; }"
)


# 淮北这类把 setSelect 包在 try 里，catch 里调的 noData（40 个真实页面都是这一种写法）：往页面上写
# "暂无数据"，Ls.template 是平台的模板函数
_NO_DATA = _values(
    _js_tokens(
        "var me = this; $('.xxgk_lmright #xxgk_lmcon').html(Ls.template("
        "'noData_xxgk_list_tpl', { name: $('#gk-navigation a:last').text() }));"
    )
)


def _catch_swallows(tokens, values, close, raw):
    """try { … } 后面是不是把异常吞掉、接着往下跑：紧跟 catch (标识符) { … }，catch 里只有
    console.log(…)（参数是字面量或这个标识符）、me.noData()（照真实写法比对）这两种不会出错的语句，
    没有 finally 或者 finally 是空的。只有 finally、catch 里再抛出或者调别的，异常照样往外抛。"""
    k = close + 1
    if (
        values[k : k + 2] != ["catch", "("]
        or not _IDENT.fullmatch(values[k + 2] if k + 2 < len(values) else "")
        or values[k + 3 : k + 5] != [")", "{"]
    ):
        return False
    param, end = values[k + 2], _matching_token(tokens, k + 4)
    body, i = values[k + 5 : end], 0
    while i < len(body):
        if body[i : i + 4] == ["console", ".", "log", "("]:
            j = i + 4
            while j < len(body) and body[j] != ")":
                if not (body[j][:1] in ("'", '"') or body[j] in (param, ",")):
                    return False
                j += 1
            if body[j : j + 2] != [")", ";"]:
                return False
            i = j + 2
        elif body[i : i + 6] == ["me", ".", "noData", "(", ")", ";"]:
            if _values(_js_tokens((raw or {}).get("noData", ""))) != _NO_DATA:
                return False
            i += 6
        else:
            return False
    after = values[end + 1 : end + 2]
    return after != ["finally"] or values[end + 2 : end + 4] == ["{", "}"]


def _member_sites(body, name, raw=None):
    """方法体里用到 me.name 的地方（this.name、me['name'] 也算；用户点了才跑的处理函数里的不算）：
    [(记号位置, 是不是被 try 接住)]。接住要 catch 把异常吞掉（_catch_swallows），而且调用不在
    try 里面另起的函数里（回调可能在 try 结束以后才跑）。"""
    tokens = _js_tokens(body)
    values = _values(tokens)
    spans = _handler_spans(tokens, values)
    guarded = []
    for j in range(len(values) - 1):
        if values[j] == "try" and values[j + 1] == "{":
            close = _matching_token(tokens, j + 1)
            if _catch_swallows(tokens, values, close, raw):
                guarded.append((j + 1, close))
    functions = []
    for f, value in enumerate(values):
        if value == "function":
            found = _function_at(tokens, values, f)
            if found is not None:
                functions.append((found[1], found[2]))
    sites = []
    for k in range(len(values) - 2):
        member = values[k + 2] if values[k + 1] == "." else ""
        if values[k + 1] == "[" and values[k + 3 : k + 4] == ["]"]:
            key = values[k + 2]
            member = _string_literal(key) if key[:1] in ("'", '"') else ""
        if values[k] in ("me", "this") and member == name and not any(a < k < b for a, b in spans):
            caught = any(
                a < k < b and not any(a < fa < k < fb for fa, fb in functions) for a, b in guarded
            )
            sites.append((k, caught))
    return sites


def _set_select_unguarded(raw):
    """有没有方法（用户点了才跑的处理函数以外）调 setSelect 而异常不会被 try 接住。"""
    return any(
        not guarded for body in raw.values() for _, guarded in _member_sites(body, "setSelect", raw)
    )


_UNCLEAR = "，算不出 setSelect 会不会出错"


def _init_order_problem(raw):
    """setSelect 往上找到 me.topLevelParentId 为止，这个值是 findParentId 定的：init 顶层得正好
    调一次 findParentId，而且在 initTree（它调 setSelect）之前；别的方法不许在加载时调 initTree、
    findParentId（157 个真实页面都这样）。"""
    calls = []
    for st in _parse_statements(raw.get("init", "")):
        expr = st[1] if st[0] == "expression" else None
        plain = expr is not None and expr[0] == "call" and not expr[2]
        calls.append(_call_path(expr) if plain else None)
    if (
        calls.count("me.findParentId") != 1
        or "me.initTree" not in calls
        or calls.index("me.findParentId") > calls.index("me.initTree")
    ):
        return "init 没有在 initTree 之前正好调一次 findParentId" + _UNCLEAR
    for name in ("findParentId", "initTree"):
        for method, body in raw.items():
            if method != "init" and _member_sites(body, name):
                return f"{method} 在加载时用到 {name}" + _UNCLEAR
    return None


_TREE_INIT = ["=", "$", ".", "fn", ".", "zTree", ".", "init", "("]
# 建完树一，删掉顶层节点的 children（setSelect 在树一里就只找得到顶层栏目）：157 个真实页面里不在
# try 里调 setSelect 的都是这样写的
_TREE1_EACH = _words(
    "$ . each ( _organTree . getNodes ( ) , function ( i , node ) { delete node . children ; "
    "node . isParent = false ; _organTree . updateNode ( node ) ; } )"
)
_NOT_A_READ = frozenset(
    {
        "(",
        ".",
        "[",
        *_ASSIGN_OPS,
        "**=",
        "<<=",
        ">>=",
        ">>>=",
        "&=",
        "|=",
        "^=",
        "&&=",
        "||=",
        "??=",
    }
)
# 不当名字看的关键字（let、of 这类可以当变量名的不在里面：当成别处的名字，宁严）
_JS_WORDS = frozenset(
    {
        "var",
        "const",
        "if",
        "else",
        "for",
        "while",
        "do",
        "return",
        "break",
        "continue",
        "switch",
        "case",
        "default",
        "function",
        "new",
        "delete",
        "typeof",
        "instanceof",
        "in",
        "void",
        "try",
        "catch",
        "finally",
        "throw",
        "true",
        "false",
        "null",
        "this",
    }
)


def _tree_statement(tokens, values, k, tree, data):
    """k 处是 `<tree> = $.fn.zTree.init($('…'), <建树设置>, me.<data>);`：给 (分号后面的位置,
    建树设置的名字)；不是给 None。"""
    if values[k : k + 1] != [tree] or values[k + 1 : k + 10] != _TREE_INIT:
        return None
    close = _matching_token(tokens, k + 9)
    args = values[k + 10 : close]
    if (
        len(args) != 10
        or args[:2] != ["$", "("]
        or args[2][:1] not in ("'", '"')
        or args[3:5] != [")", ","]
        or not _IDENT.fullmatch(args[5])
        or args[6:9] != [",", "me", "."]
        or args[9] not in data
        or values[close + 1 : close + 2] != [";"]
    ):
        return None
    return close + 2, args[5]


def _tree_one_problem(raw, args):
    """推算 setSelect 时当树一（_organTree）是整棵栏目树建的、只删了顶层节点的 children。照真实页面
    的写法核对：initTree 顶层先用和树二同一个建树设置、me.ztreeData 建树一，紧接着建树二、删顶层
    children，然后直接调 setSelect。这之前会跑的代码（含建树设置里的显示回调；用户点了才跑的除外）
    只许用局部变量、$ 选元素、$.each、zTree.init、读 me 的字段，碰不到别的脚本和 PublicTree 的别的
    方法；_organTree 除了上面这几处不许再用；字符串里不许有 script（往页面里写脚本），不许替用户
    触发事件。"""
    source = raw.get("initTree", "")
    tokens = _js_tokens(source)
    values = _values(tokens)
    if "_organTree" in {a.strip() for a in args.get("initTree", "").split(",")}:
        return "initTree 的参数遮住了 _organTree" + _UNCLEAR
    first = next((k for k, value in enumerate(values) if value == "_organTree"), None)
    one = (
        None
        if first is None
        else _tree_statement(tokens, values, first, "_organTree", ("ztreeData",))
    )
    if (
        one is None
        or first
        and values[first - 1] not in (";", "}")
        or first not in _depth_zero(tokens, 0, len(tokens))
    ):
        return "initTree 没有在顶层先用整棵栏目树（me.ztreeData）建树一" + _UNCLEAR
    after, setting = one
    two = _tree_statement(tokens, values, after, "_organTree2", ("ztreeData2", "ztreeData"))
    each = two[0] if two is not None and two[1] == setting else None
    call = None if each is None else each + len(_TREE1_EACH)
    if call is not None and values[call : call + 1] == [";"]:
        call += 1
    if (
        each is None
        or values[each:call][: len(_TREE1_EACH)] != _TREE1_EACH
        or values[call : call + 6] != ["me", ".", "setSelect", "(", ")", ";"]
    ):
        return "initTree 建树一到 setSelect 之间的写法认不出" + _UNCLEAR
    unguarded = [
        (method, k)
        for method, body in raw.items()
        for k, guarded in _member_sites(body, "setSelect", raw)
        if not guarded
    ]
    if unguarded != [("initTree", call)]:
        return "setSelect 不是只在 initTree 建好树以后直接调一次" + _UNCLEAR
    spans = _handler_spans(tokens, values)
    keep = {first, after, each + 4, each + 28}  # 上面那几处 _organTree、_organTree2
    positions = [k for k in range(call) if not any(a < k < b for a, b in spans)]
    local = _local_name_ranges(
        tokens, values, {values[k] for k in positions if _IDENT.fullmatch(values[k])}
    )
    declared = {
        values[f + 1]: f
        for f in range(len(values) - 2)
        if values[f] == "function"
        and _IDENT.fullmatch(values[f + 1])
        and values[f + 2] == "("
        and (f == 0 or values[f - 1] in (";", "{", "}", ")"))
    }
    text = []
    for k in positions:
        value = values[k]
        prev = values[k - 1] if k else ""
        following = values[k + 1] if k + 1 < len(values) else ""
        if value[:1] in ("'", '"'):
            text.append(_string_literal(value))
        elif value[:1] == "`":
            text.append(value[1:-1])
        elif value in ("_organTree", "_organTree2"):
            if k not in keep:
                return f"initTree 在 setSelect 之前另外用了 {value}" + _UNCLEAR
        elif value == "this":
            if values[max(0, k - 3) : k] != ["var", "me", "="]:
                return "initTree 在 setSelect 之前用了 this" + _UNCLEAR
        elif prev == ".":
            if (
                value in _TRIGGERS
                or value in _EVENT_SHORTHANDS
                and values[k + 1 : k + 3] == ["(", ")"]
            ):
                return f"initTree 在 setSelect 之前替用户触发事件（.{value}）" + _UNCLEAR
        elif (
            not _IDENT.fullmatch(value)
            or value in _JS_WORDS
            or prev == "function"  # 函数的名字
            or following == ":"
            and prev in ("{", ",")  # 对象的键
        ):
            continue
        elif value == "me":
            reads = (
                following == "."
                and _IDENT.fullmatch(values[k + 2] if k + 2 < len(values) else "")
                and values[k + 3 : k + 4] != []
                and values[k + 3] not in _NOT_A_READ
                and prev not in ("++", "--", "delete")
            )
            if not (reads or prev == "var" and values[k + 1 : k + 3] == ["=", "this"]):
                return "initTree 在 setSelect 之前除了读字段还用了 me" + _UNCLEAR
        elif any(a < k < b for a, b in local.get(value, ())):
            # 局部函数只许调写在 setSelect 之前的函数声明（它们的函数体照这里的规矩查过）
            if following == "(" and declared.get(value, call) >= call:
                return f"initTree 在 setSelect 之前调用了 {value}" + _UNCLEAR
        elif not (
            value == "undefined"
            or value == "$"
            and (
                following == "("
                or values[k + 1 : k + 4] == [".", "each", "("]
                or values[k + 1 : k + 8] == [".", "fn", ".", "zTree", ".", "init", "("]
            )
        ):
            return f"initTree 在 setSelect 之前用了 {value}" + _UNCLEAR
    if re.search(r"<\s*script|%3c\s*script", "".join(text), re.I):
        return "initTree 在 setSelect 之前往页面里写脚本" + _UNCLEAR
    return None


def _ancestor_loop(nodes, cat_id):
    """从这个栏目顺着 parentId 往上走，碰到走过的节点（含上级是自己的）：给原因。"""
    by_id = {node["id"]: node for node in nodes}
    current, seen = by_id.get(int(cat_id)), set()
    while current is not None:
        if current["id"] in seen or current.get("parentId") == current["id"]:
            return "栏目树里这个栏目的上级关系绕圈（含上级是自己），页面加载时 setSelect 走不出来"
        seen.add(current["id"])
        parent = current.get("parentId")
        current = by_id.get(parent) if isinstance(parent, int) else None
    return None


def _select_shape_problem(raw, args):
    """setSelect、findTopLevelParentNode、findParentId 照真实写法逐个记号比对（setSelect 只比同步
    的部分，setTimeout 回调是 500 毫秒以后才跑的）。认不出的给原因。"""
    tokens = _js_tokens(raw.get("setSelect", ""))
    values = _values(tokens)
    n = len(_SET_SELECT_PREFIX)
    try:
        timer = (
            n
            if values[:n] == _SET_SELECT_PREFIX
            and values[n : n + 3] == ["setTimeout", "(", "function"]
            else None
        )
        found = _function_at(tokens, values, n + 2) if timer is not None else None
        close = _matching_token(tokens, n + 1) if found is not None else None
    except LonsunFormatError:
        found = close = None
    # setTimeout 回调是 500 毫秒以后才跑的（里面的写法照加载时的规矩另查）；同步的部分必须一字不差
    if (
        found is None
        or values[found[2] + 1 : found[2] + 2] != [","]
        or not re.fullmatch(r"[0-9]+", values[found[2] + 2] if found[2] + 2 < len(values) else "")
        or found[2] + 3 != close
        or values[close + 1 :] != [";", "}", "}"]
    ):
        return "setSelect 的写法认不出，算不出加载时会不会出错"
    if (
        args.get("findTopLevelParentNode", "").replace(" ", "") != "nodeId,nodes"
        or _values(_js_tokens(raw.get("findTopLevelParentNode", ""))) != _FIND_TOP
    ):
        return "findTopLevelParentNode 的写法认不出，算不出 setSelect 会不会出错"
    if _values(_js_tokens(raw.get("findParentId", ""))) not in (
        _FIND_PARENT_FIRST,
        _FIND_PARENT_FREQUENT,
    ):
        return "findParentId 的写法认不出，算不出 setSelect 会不会出错"
    return None


def _set_select_problem(raw, args, nodes, cat_id, hide_ok, modified=frozenset()):
    """在 try 外面调的 setSelect 会不会在 getData 之前出错（出错就给原因：浏览器不发列表请求）。"""
    if any(_member_sites(body, "setSelect", raw) for body in raw.values()):
        # 包在 try 里也一样：catch 接得住异常，接不住死循环（findTopLevelParentNode 在上级是自己的
        # 节点上一直转），浏览器到不了 getData。写法也得是认得的（改了循环的写法就可能转不出来）
        problem = _ancestor_loop(nodes, cat_id) or _select_shape_problem(raw, args)
        if problem:
            return problem
    if not _set_select_unguarded(raw):
        return None
    problem = _init_order_problem(raw) or _tree_one_problem(raw, args)
    if problem:
        return problem
    problem = _select_shape_problem(raw, args)
    if problem:
        return problem
    parent_values = _values(_js_tokens(raw.get("findParentId", "")))
    if not hide_ok:
        return "页面隐去部分栏目，算不出 setSelect 会不会出错"
    if "id" in modified:
        # 树一按 id 找栏目（getNodeByParam('id', …)）
        return "preprocessData 改了栏目节点的 id" + _UNCLEAR
    ids = {node["id"] for node in nodes}
    target = int(cat_id)  # Ls.url.get('catId') 是数字串；zTree 按 == 比，就是数值相等
    # 树一（me.ztreeData，就是整棵栏目树）的顶层节点：上级不在树里的，或者上级是自己的；建完树
    # 顶层节点的 children 被删掉，getNodeByParam 只找得到顶层
    if any(
        node["id"] == target
        and (node.get("parentId") not in ids or node.get("parentId") == node["id"])
        for node in nodes
    ):
        return None
    # findParentId：上级不在树里的那些上级编号（按栏目树顺序，空的跳过），取第一个或出现最多的
    tops = [
        node["parentId"]
        for node in nodes
        if node.get("parentId") is not None and node["parentId"] not in ids
    ]
    if parent_values == _FIND_PARENT_FIRST:
        top = tops[0] if tops else UNDEFINED
    else:
        top, best, counts = (tops[0] if tops else UNDEFINED), 0, {}
        for value in tops:
            counts[value] = counts.get(value, 0) + 1
            if counts[value] > best:
                best, top = counts[value], value
    # findTopLevelParentNode(+catId, catalogs[organId])：往上走到 parentId === topLevelParentId
    current = next((node for node in nodes if node["id"] == target), None)
    seen = set()
    while True:
        if current is None:
            return (
                f"栏目 {cat_id} 往上找不到 findParentId 定下的上级，页面加载时 setSelect 会出错，"
                "浏览器不发列表请求"
            )
        parent = current.get("parentId", UNDEFINED)
        if parent is top or isinstance(parent, int) and parent == top and not isinstance(top, bool):
            return None
        if current["id"] in seen:
            return "栏目树的上级关系绕圈，页面加载时 setSelect 走不出来，浏览器不发列表请求"
        seen.add(current["id"])
        current = (
            next((node for node in nodes if node["id"] == parent), None)
            if isinstance(parent, int)
            else None
        )


def _node_field_problem(raw, nodes):
    """preprocessData 遍历栏目树时读节点字段的成员（el.name.length、el.name.slice(…)）：哪个节点
    的这个字段不是字符串，浏览器在这里就出错，init 中断、不发列表请求。"""
    pre = raw.get("preprocessData", "")
    tokens = _js_tokens(pre)
    values = _values(tokens)
    grep = [
        scope
        for lib in ("$", "jQuery")
        for key in ("ztreeData", "ztreeData2")
        for scope in _callback_scopes(tokens, values, [lib, ".", "grep", "(", "me", ".", key])
    ]
    for names, (a, b) in [*_node_scopes(pre), *grep]:
        for k in range(a + 1, b - 3):
            if values[k] in names and values[k + 1] == "." and values[k + 3] in (".", "["):
                field = values[k + 2]
                if any(not isinstance(node.get(field), str) for node in nodes):
                    return (
                        f"栏目树里有节点的 {field} 不是字符串，页面 preprocessData 读 "
                        f"{values[k]}.{field} 的成员会出错，浏览器不发列表请求"
                    )
    return None


def resolve_lonsun(api, catalog_text):
    """按页面受限执行路径推导列表接口；不执行页面脚本。"""
    from urllib.parse import quote_plus

    from youju.crawler.adapters import LONSUN, ListApi

    plan = api.plan
    if not isinstance(plan, LonsunPlan):
        return "页面执行计划无效"
    try:
        nodes = parse_catalog(catalog_text, plan.organ_id)
        raw = {name: body for name, _, body in plan.raw_methods}
        methods = {name: (args, body) for name, args, body in plan.methods}
        _check_pre_methods(
            raw, dict(plan.globals), widget=_harmless_search_widget(plan.raw_methods)
        )
        for ui in _UI_METHODS:
            if ui not in raw:
                if any(_member_sites(body, ui) for body in raw.values()):
                    raise LonsunFormatError(f"{ui} 没有定义，调用它会出错")
                continue
            problem = _ui_method_problem(raw[ui], frozenset(raw)) or _ui_shape_problem(ui, raw[ui])
            if problem:
                raise LonsunFormatError(f"{ui} {problem}")
        modified, hide_ok = _tree_rules(raw)
        if not hide_ok:
            raise LonsunFormatError("hideIds 的写法无法确定")
        if not _init_calls_getdata(raw["init"]):
            return "init 里找不到可识别的 getData 调用"
        spec = column_spec(api.base_url)
        if spec is None:
            return "栏目网址无效"
        cat_id = spec[1]
        # getData 之前就会出错的（浏览器不发列表请求）：preprocessData 读节点字段的成员、setSelect
        problem = (
            _node_field_problem(raw, nodes)
            or _display_name_problem(raw, nodes)
            or _set_select_problem(
                raw,
                {name: args for name, args, _ in plan.raw_methods},
                nodes,
                cat_id,
                hide_ok,
                modified,
            )
        )
        if problem:
            return problem
        wrapped = []
        for node in nodes:
            children = [n for n in nodes if n is not node and n.get("parentId") == node["id"]]
            has_parent = node.get("isParent", UNDEFINED)
            value = (
                UNKNOWN
                if has_parent is not UNDEFINED and _truth(has_parent) != bool(children)
                else (children or UNDEFINED)
            )
            # ztree.min.js 的 simpleData 默认 rootPId=0；页面只改 idKey/pIdKey。
            # transformTozTreeFormat 仅在真有下级时创建 children（jx、芜湖 11/13 实测）。
            wrapped.append(_Node(node, value, modified))

        def wrap_node(n, ancestors=frozenset()):
            if id(n.data) in ancestors:
                raise LonsunFormatError("栏目树存在循环")
            children = n.children
            if isinstance(children, list):
                by_id = {id(item.data): item for item in wrapped}
                children = [wrap_node(by_id[id(c)], ancestors | {id(n.data)}) for c in children]
            return _Node(n.data, children, n.modified)

        wrapped = [wrap_node(n) for n in wrapped]
        node = next((n for n in wrapped if _loose_equal(n.data["id"], cat_id)), None)
        query = _platform_query(urlsplit(api.base_url).query)
        refresh_expected = _parse_statements(
            "var me = this; if ('pushState' in history) { me.refresh = false; }"
        )
        refresh = (
            False if methods.get("isRefresh", (None, None))[1] == refresh_expected else UNKNOWN
        )
        me = _PartialDict(
            {
                "organId": plan.organ_id,
                "prefix": plan.prefix,
                "actions": {"label": plan.label},
                "listParams": dict(plan.list_params),
                "currCatId": cat_id,
                "query": query,
                "refresh": refresh,
                "currNode": None,
                "hideIds": [] if hide_ok else UNKNOWN,
            }
        )
        globals_ = dict(plan.globals)
        current = {"url": api.base_url, "query": query}  # 现在的网址（pushState 会改）
        globals_.update(
            {
                "this": me,
                "me": me,
                "location": _PartialDict({"href": api.base_url}),
                "Ls": _PartialDict(
                    {"url": _PartialDict({"get": "url.get", "stringify": "url.stringify"})}
                ),
                "$": _PartialDict({"extend": "extend"}),
                "_organTree2": _PartialDict({"getNodeByParam": "node"}),
            }
        )

        def member(obj, key):
            if isinstance(obj, _Node):
                if key in obj.modified:
                    if not root.unknown_reason:
                        root.unknown_reason = (
                            f"条件里用到了节点的 {key}，页面 preprocessData 改过它"
                        )
                    return UNKNOWN
                if key == "children":
                    if obj.children is UNKNOWN and not root.unknown_reason:
                        root.unknown_reason = "节点的 isParent 与下级栏目不一致"
                    return obj.children
                if key in {"id", "name", "parentId", "uri", "link", "isEnableTransferToFdf"}:
                    return obj.data.get(key, UNDEFINED)
                if not root.unknown_reason:
                    root.unknown_reason = f"条件里用到了不认识的节点属性 {key}"
            return UNKNOWN

        def call(fn, args, receiver):
            if (
                fn == "node"
                and len(args) == 3
                and args[0] == "id"
                and args[1] == cat_id
                and args[2] is None
            ):
                return node
            if fn == "url.get" and len(args) == 1 and isinstance(args[0], str):
                if not re.fullmatch(r"[A-Za-z0-9_]+", args[0]):
                    # 平台把参数名拼进正则：名字里有元字符的，两边取到的不一样
                    if not root.unknown_reason:
                        root.unknown_reason = "Ls.url.get 的参数名认不准"
                    return UNKNOWN
                if current["query"] is None:
                    if not root.unknown_reason:
                        root.unknown_reason = "pushState 改的网址参数认不准"
                    return UNKNOWN
                # 平台 common.min.js：get 通过 TT.isEmpty 将缺失值转为空串。
                return current["query"].get(args[0], "")
            if fn == "url.stringify" and len(args) == 1 and isinstance(args[0], dict):
                from urllib.parse import quote

                if any(
                    isinstance(v, (dict, list, tuple)) or v is UNKNOWN for v in args[0].values()
                ):
                    return UNKNOWN
                return "&".join(
                    str(k) + "=" + quote(_js_string(v), safe="-_.!~*'()")
                    for k, v in args[0].items()
                )
            if (
                fn == "extend"
                and len(args) == 3
                and all(isinstance(x, dict) for x in args)
                and not args[0]
            ):
                return {
                    k: v for source in args[1:] for k, v in source.items() if v is not UNDEFINED
                }
            return UNKNOWN

        root = _Environment(globals_, member_resolver=member, call_resolver=call)

        class Stop(Exception):
            def __init__(self, result):
                self.result = result

        def special(text):
            raise Stop("页面在这个栏目上走的是特别的读法：" + text[:40])

        def request(name):
            args, body = methods[name]
            env = _Environment({x.strip(): UNDEFINED for x in args.split(",") if x.strip()}, root)
            phase = 0
            ajax = None
            callback = None
            for st in body:
                if st[0] == "var" and len(st[1]) == 1:
                    key, value = st[1][0]
                    if phase == 0 and key == "me" and value == ("identifier", "this"):
                        env.values[key] = me
                        phase = 1
                        continue
                    if (
                        phase == 1
                        and key == "tipsLoading"
                        and value
                        and _call_path(value) == "Ls.tipsLoading"
                    ):
                        phase = 2
                        continue
                    if (
                        phase == 2
                        and key == "params"
                        and value
                        and _call_path(value) == "$.extend"
                        and len(value[2]) == 3
                        and value[2][0] == ("object", ())
                        and _path(value[2][1]) == "me.listParams"
                        and value[2][2][0] == "object"
                    ):
                        env.values[key] = _evaluate(value, env)
                        phase = 3
                        continue
                if st[0] == "expression" and phase in (2, 3) and ajax is None:
                    expr = st[1]
                    if (
                        expr[0] == "call"
                        and expr[1][0] == "member"
                        and expr[1][2] == ("literal", "always")
                        and len(expr[2]) == 1
                        and expr[2][0][0] == "function"
                    ):
                        expr = expr[1][1]
                    if (
                        expr[0] == "call"
                        and expr[1][0] == "member"
                        and expr[1][2] == ("literal", "done")
                        and len(expr[2]) == 1
                        and expr[2][0][0] == "function"
                    ):
                        callback = expr[2][0]
                        ajax = expr[1][1]
                        phase = 4
                        continue
                raise Stop("列表请求方法含有认不出的语句")
            if (
                not ajax
                or _call_path(ajax) != "Ls.ajaxGet"
                or len(ajax[2]) != 1
                or ajax[2][0][0] != "object"
            ):
                raise Stop("列表请求写法认不出")
            pairs = ajax[2][0][1]
            if len(pairs) != 2 or set(k for k, _ in pairs) != {"url", "data"}:
                raise Stop("列表请求参数格式认不出")
            values = dict(pairs)
            data_tree = values["data"]
            if _path(values["url"]) != "me.actions.label" or not (
                data_tree[0] == "object" or _path(data_tree) in ("me.listParams", "params")
            ):
                raise Stop("列表请求参数写法认不出")
            data = _evaluate(data_tree, env)
            if not isinstance(data, dict) or not all(
                v is None
                or v is UNDEFINED
                or isinstance(v, (str, bool))
                or isinstance(v, (int, float))
                and math.isfinite(v)
                for v in data.values()
            ):
                raise Stop("列表请求参数无法求值")
            if any(re.fullmatch(r"0|[1-9][0-9]*", str(k)) and int(k) < 4294967295 for k in data):
                raise Stop("列表参数含整数键，顺序无法确定")
            # 平台 Ls.ajaxGet、Ls.ajax 用 $.extend(true, …) 合并请求设置，jQuery 的 extend 跳过值是
            # undefined 的键：浏览器不带这些参数。null 照 jQuery.param 发成空串
            strings = {
                k: "" if v is None else _js_string(v) for k, v in data.items() if v is not UNDEFINED
            }
            mode = strings.get("action")
            unknown = set(strings) - _REQUEST_KEYS
            if unknown:
                raise Stop(
                    f"列表请求带着认不出的参数 {'、'.join(sorted(unknown))}（令牌、签名这类不发）"
                )
            if (
                strings.get("labelName") != "publicInfoList"
                or strings.get("organId") != plan.organ_id
                or strings.get("isJson") != "true"
                or strings.get("pageIndex") != "1"
                or "file" in data
                or strings.get("keyWords", "") != ""
                or not re.fullmatch(r"[0-9]+", strings.get("catId", ""))
            ):
                raise Stop("列表请求参数不符合支持的格式")
            if mode == "list":
                if strings.get("type") != "4":
                    raise Stop("列表请求类型不符合支持的格式")
            elif mode == "city_page":
                if strings["catId"] != cat_id:
                    raise Stop("下级汇总栏目编号不符")
                if not hide_ok:
                    raise Stop("页面会按自己的设置隐去部分下级栏目，认不出隐去哪些")
            else:
                raise Stop("列表请求模式不符合支持的格式")
            excluded = _callback_exclusions(raw[name], callback, env, hide_ok, mode)
            parts = urlsplit(api.base_url)
            encoded = "&".join(
                quote_plus(k, safe="-_.!~*'()") + "=" + quote_plus(v, safe="-_.!~*'()")
                for k, v in strings.items()
            )
            listing = ListApi(
                adapter=LONSUN,
                url=f"{parts.scheme}://{parts.netloc}{plan.label}?{encoded}",
                base_url=api.base_url,
                plan=LonsunRequestPlan(mode, cat_id, excluded),
            )
            if plan.ready:
                # 先发 getData 等着的那个请求（平台 Ls.ajax 加的防缓存随机数不加），成功了
                # 才发列表请求
                ready = ListApi(
                    adapter=LONSUN,
                    url=f"{parts.scheme}://{parts.netloc}{plan.ready}",
                    base_url=api.base_url,
                    plan=LonsunReadyPlan(plan.ready),
                )
                listing = replace(listing, parts=(ready, listing))
            raise Stop(listing)

        def execute(name, argument=UNDEFINED):
            args, _ = methods[name]
            source = raw[name]
            positions = {}

            class SourceParser(_JSParser):
                def statement(self):
                    start = self.tokens[self.i].start
                    statement = super().statement()
                    positions[id(statement)] = source[start : self.tokens[self.i - 1].end]
                    return statement

            parser = SourceParser(source)
            body = []
            while parser.peek() != "<结束>":
                body.append(parser.statement())
            names = [x.strip() for x in args.split(",") if x.strip()]
            env = _Environment(
                {key: argument if i == 0 else UNDEFINED for i, key in enumerate(names)}, root
            )

            def run(st):
                kind = st[0]
                if kind == "block":
                    for child in st[1]:
                        if run(child):
                            return True
                elif kind == "if":
                    root.unknown_reason = ""
                    condition = _evaluate(st[1], env)
                    if condition is UNKNOWN:
                        raise Stop(root.unknown_reason or "页面条件无法确定，认不出栏目读取方式")
                    branch = st[2] if _truth(condition) else st[3]
                    if branch and run(branch):
                        return True
                elif kind == "var":
                    for key, value in st[1]:
                        if value and _has_unmodeled_call(value, env):
                            raise Stop("变量初始化调用了无法识别的方法")
                        env.values[key] = _evaluate(value, env) if value else UNDEFINED
                elif kind in ("empty", "function"):
                    pass
                elif kind == "return":
                    return True
                elif kind == "unknown":
                    special(st[1])
                elif kind == "expression":
                    expr = st[1]
                    path = _call_path(expr)
                    if path in (
                        "me.setMeta",
                        "me.getNav",
                        "me.getGd",
                        "console.log",
                    ):
                        return False
                    if path == "window.history.pushState":
                        from urllib.parse import urljoin

                        if len(expr[2]) != 3 or any(
                            arg != ("literal", None) for arg in expr[2][:2]
                        ):
                            # 前两个参数浏览器要结构化克隆：带函数的对象（me.currNode）会抛错
                            raise Stop("pushState 参数认不出")
                        target = _evaluate(expr[2][2], env)
                        if not isinstance(target, str):
                            raise Stop("pushState 地址无法确定")
                        if _odd_url(target):
                            raise Stop("pushState 地址里有反斜杠、空白、控制字符或非 ASCII 字符")
                        # 相对的是现在的网址（前面 pushState 过就是改过的）
                        source_url, target_url = (
                            urlsplit(current["url"]),
                            urlsplit(urljoin(current["url"], target)),
                        )

                        def origin(u):
                            return (
                                u.scheme.lower(),
                                u.hostname,
                                u.port or {"http": 80, "https": 443}.get(u.scheme),
                            )

                        if (
                            target_url.username is not None
                            or target_url.password is not None
                            or origin(source_url) != origin(target_url)
                        ):
                            raise Stop("pushState 地址跨源")
                        # 网址改了：之后读网址参数（Ls.url.get）、location 照浏览器按新网址；
                        # me.query 是构造函数时取的，不变
                        current["url"] = urljoin(current["url"], target)
                        query = urlsplit(current["url"]).query
                        keys = [part.partition("=")[0] for part in query.split("&")]
                        # 平台 Ls.url.get 用正则取第一个 ?name= 或 &name=、先把 + 换成空格再解码，
                        # 我们按 & 切开、同名取后一个：编码过的、带 + 或第二个 ?、有同名参数的两边
                        # 可能不一样，认不准（进页面时的网址 column_spec 挡掉了这些）
                        current["query"] = (
                            None
                            if {"%", "+", "?"} & set(query) or len(set(keys)) != len(keys)
                            else _platform_query(query)
                        )
                        globals_["location"] = _PartialDict({"href": current["url"]})
                        return False
                    if path == "me.open" and len(expr[2]) <= 1:
                        value = _evaluate(expr[2][0], env) if expr[2] else UNDEFINED
                        execute("open", value)
                        return False
                    if path in ("me.getList", "me.getNewList") and not expr[2]:
                        request(path[3:])
                    if expr[0] == "assign":
                        dest = _path(expr[2])
                        value = _evaluate(expr[3], env)
                        if expr[1] == "=" and dest == "me.currNode":
                            me["currNode"] = value
                            return False
                        if (
                            expr[1] == "="
                            and dest
                            and dest.startswith("me.listParams.")
                            and dest.count(".") == 2
                        ):
                            me["listParams"][dest.split(".")[-1]] = value
                            return False
                        if expr[2][0] == "identifier" and expr[2][1] in env.values:
                            key = expr[2][1]
                            if expr[1] == "=":
                                env.values[key] = value
                            elif expr[1] == "+=":
                                env.values[key] = _evaluate(
                                    (
                                        "binary",
                                        "+",
                                        ("literal", env.values[key]),
                                        ("literal", value),
                                    ),
                                    env,
                                )
                            else:
                                env.values[key] = UNKNOWN
                            return False
                    special(positions.get(id(st), "页面语句认不出"))
                return False

            for st in body:
                if run(st):
                    return

        try:
            execute("getData")
        except Stop as stop:
            return stop.result
        return "栏目树里没有这个栏目，或页面在这个栏目上不加载列表"
    except LonsunFormatError as exc:
        return f"页面或栏目树认不出：{exc}"
    except (ValueError, TypeError, IndexError, KeyError, RecursionError, OverflowError):
        return "页面或栏目树认不出：解析失败"
