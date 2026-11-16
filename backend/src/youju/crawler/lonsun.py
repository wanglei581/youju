"""龙讯平台保守解析：栏目树只读页面原样声明的 src。

document.write 字面量后面的随机数用于防缓存，不加；只取字面量，不改不补。
栏目树只按 JSON 解析，内联脚本只用受限求值器，均不执行。
"""

import html
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlsplit

from youju.crawler.dates import _safe_date
from youju.crawler.listing import ListItem, _looks_like_title
from youju.crawler.titles import clean_title
from youju.crawler.urls import normalize_url, url_key

# 依据 2026-11-13 区县栏目页实测；新增城市须先审查同城集约化平台关系。
# robots.txt 的 4xx 按 RFC 9309 视为没有规则；5xx、网络错误和超时按不可读处理。
LONSUN_CITY_PORTALS = {("安徽", "芜湖"): "www.wuhu.gov.cn", ("安徽", "淮南"): "www.huainan.gov.cn"}


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


def catalog_host_allowed(page_url: str, script_url: str, catalog_host: str | None = None) -> bool:
    """栏目树允许同主机，或同一个地方政府域名下的主机。"""
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
        if catalog_host and b == catalog_host and b.endswith(".gov.cn"):
            return True
        if a == b:
            return a.endswith(".gov.cn")
        a_parts, b_parts = a.split("."), b.split(".")
        return (
            len(a_parts) >= 4
            and len(b_parts) >= 4
            and a_parts[-2:] == b_parts[-2:] == ["gov", "cn"]
            and a_parts[-3] == b_parts[-3]
        )
    except ValueError:
        return False


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
        nodes = json.loads(match.group(2))
    except ValueError as exc:
        raise LonsunFormatError("栏目树 JSON 无效") from exc
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict) or type(node.get("id")) is not int for node in nodes
    ):
        raise LonsunFormatError("栏目树节点编号无效")
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
        payload = json.loads(text.lstrip("\ufeff").strip())
    except ValueError as exc:
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
    }
    while i < len(source):
        start = i
        c = source[i]
        if c.isspace():
            i += 1
            continue
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
        elif c == "/" and (not tokens or tokens[-1].value in regex_after):
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
            match = _JS_WORD.match(source, i)
            if match:
                i = match.end()
            else:
                op = next((op for op in operators if source.startswith(op, i)), c)
                i += len(op)
        tokens.append(_Token(source[start:i], start, i))
    return tuple(tokens)


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
    values = [t.value for t in tokens]
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
                    key = _js_string(float(key))
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
            left = ("literal", float(token))
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
                    root.unknown_reason = f"条件里用到的 {name} 无法确定（可能重复声明或被改写）"
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
                return float(int(value, 0))
            if not re.fullmatch(r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", value):
                return math.nan
            return float(value)
        except ValueError:
            pass
    return math.nan


def _truth(value):
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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


def _immutable(value):
    if isinstance(value, list):
        return tuple(_immutable(v) for v in value)
    if isinstance(value, dict):
        return tuple((k, _immutable(v)) for k, v in value.items())
    return value


_MUTATORS = {"push", "pop", "shift", "unshift", "splice", "sort", "reverse"}
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


def _writes(source, *, skip_functions=False):
    """找属性、下标、更新运算和常见原地修改；不执行任何脚本。"""
    tokens = _js_tokens(source)
    v = [t.value for t in tokens]
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
    result = []
    for i, name in enumerate(v):
        if i in skipped or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
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
            else:
                break
        op = v[j] if j < len(v) else ""
        if op in ("=", "+=", "-=", "*=", "/=", "%=", "++", "--") or (
            i and v[i - 1] in ("delete", "++", "--")
        ):
            result.append((tuple(parts), op))
        elif op == "(" and len(parts) > 1 and parts[-1] in _MUTATORS:
            result.append((tuple(parts[:-1]), parts[-1]))
        elif (
            parts in (["$", "extend"], ["jQuery", "extend"])
            or len(parts) == 2
            and parts[0] in ("Object", "Reflect")
        ) and op == "(":
            # 只需要找目标；复杂目标一律作为未知写入。
            k = j + 1
            if k < len(v):
                target = v[k]
                if (
                    target in ("me", "this", "window")
                    and v[k + 1 : k + 2] == ["."]
                    and k + 2 < len(v)
                ):
                    result.append(((target, v[k + 2]), "extend"))
                elif target in ("me", "this") and v[k + 1 : k + 2] == ["["]:
                    result.append(((target, "*"), "extend"))
                elif re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", target):
                    result.append(((target,), "extend"))
    return result


def _tracked_write(path):
    return (
        len(path) > 1 and path[0] in ("me", "this") and (path[1] in _TRACKED or path[1] == "*")
    ) or path[0] in ("catalogs", "_organTree2")


def _computed_method_call(tokens, start):
    values = [token.value for token in tokens]
    j = start + 1
    computed = False
    while j < len(values):
        if values[j] == "[":
            computed = True
            j = _matching_token(tokens, j) + 1
        elif values[j] == "." and j + 1 < len(values):
            j += 2
        else:
            break
    return computed and j < len(values) and values[j] == "("


def _safe_event_timer(tokens, start):
    """只认延时注册点击事件；事件处理函数不会在页面加载时执行。"""
    values = [token.value for token in tokens]
    try:
        if values[start : start + 5] != ["setTimeout", "(", "function", "(", ")"]:
            return False
        body = start + 5
        if values[body : body + 3] != ["{", "$", "("]:
            return False
        if not values[body + 3].startswith(("'", '"')):
            return False
        if values[body + 4 : body + 9] != [")", ".", "on", "(", "'click'"] and values[
            body + 4 : body + 9
        ] != [")", ".", "on", "(", '"click"']:
            return False
        if values[body + 9 : body + 12] != [",", "function", "("]:
            return False
        callback_body = _matching_token(tokens, body + 11) + 1
        if values[callback_body] != "{":
            return False
        after_callback = _matching_token(tokens, callback_body) + 1
        if values[after_callback : after_callback + 2] != [")", ";"]:
            return False
        end_body = after_callback + 2
        return (
            values[end_body : end_body + 2] == ["}", ","]
            and re.fullmatch(r"[0-9]+", values[end_body + 2]) is not None
            and values[end_body + 3] == ")"
        )
    except (IndexError, LonsunFormatError):
        return False


def _check_pre_methods(raw, globals_):
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
    for path, _op in _writes(raw.get("preprocessData", "")):
        if (
            len(path) > 1
            and path[-1] in ("parentId", "*", "children")
            and path[0] not in ("me", "$", "jQuery")
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
    v = [t.value for t in tokens]
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
    declarations = {}
    assignments = {}
    for source in scripts:
        tokens = _js_tokens(source)
        values = [t.value for t in tokens]
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
                name = values[i + 1]
                try:
                    parser = _JSParser(source[tokens[i + 2].end :])
                    tree = parser.expr()
                    if tree[0] not in ("literal", "array"):
                        value = UNKNOWN
                    else:
                        value = _evaluate(tree, _Environment())
                        if value is None or value is UNDEFINED:
                            value = UNKNOWN
                except LonsunFormatError:
                    value = UNKNOWN
                declarations.setdefault(name, []).append(value)
            if (
                values[i + 1] in ("=", "+=")
                and (i == 0 or values[i - 1] != ".")
                and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", values[i])
            ):
                assignments[values[i]] = assignments.get(values[i], 0) + 1
    for source in scripts:
        for path, op in _writes(source):
            if path == ("window",) and op == "extend":
                for name in declarations:
                    assignments[name] = assignments.get(name, 0) + 1
                continue
            name = path[1] if path[0] in ("window", "globalThis") and len(path) > 1 else path[0]
            if name in declarations:
                assignments[name] = assignments.get(name, 0) + 1
        tokens = _js_tokens(source)
        values = [token.value for token in tokens]
        for i in range(len(values) - 2):
            if values[i] != "function" or values[i + 1] != "(":
                continue
            if values[max(0, i - 2) : i] not in (["$", "("], ["ready", "("]):
                continue
            start = _matching_token(tokens, i + 1) + 1
            if start >= len(values) or values[start] != "{":
                continue
            end = _matching_token(tokens, start)
            body = source[tokens[start].end : tokens[end].start]
            for path, _op in _writes(body, skip_functions=True):
                name = path[1] if path[0] == "window" and len(path) > 1 else path[0]
                if name in declarations:
                    assignments[name] = assignments.get(name, 0) + 1
    return tuple(
        (name, _immutable(vals[0]) if len(vals) == 1 and assignments.get(name) == 1 else UNKNOWN)
        for name, vals in declarations.items()
    )


def _written_script(source):
    tokens = _js_tokens(source)
    values = [t.value for t in tokens]
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
    return match.group(2) if re.fullmatch(r"[A-Za-z0-9/_.\-?=&%:]+", match.group(2)) else None


# 改 jQuery 全局 AJAX 设置的调用（可能加令牌、改地址）：不管接收者是谁（$、jQuery 或它们的别名）
_AJAX_SETTERS = {"ajaxSetup", "ajaxPrefilter", "ajaxTransport"}
# 我们照着推请求、又可能被页面脚本改动的页面全局对象：每次出现都得是认得的写法
_ROOTS = ("Ls", "catalogs", "PublicTree", "_organTree2")
_GLOBAL_OBJECTS = ("window", "globalThis", "self", "top", "parent", "frames")
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
    values = [t.value for t in tokens]
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
    values = [t.value for t in tokens]
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
        values = [t.value for t in tokens]
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
    values = [t.value for t in tokens]
    spans = {}
    for i in range(len(values) - 4):
        if values[i : i + 5] == ["function", "PublicTree", "(", ")", "{"]:
            spans[""] = (i + 4, _matching_token(tokens, i + 4))
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
    values = [t.value for t in tokens]
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
    values = [t.value for t in tokens]

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
    # 别名：反复扫到不再增加
    sources = {"catalogs", "_organTree2"}
    aliases = set()
    changed = True
    while changed:
        changed = False
        tainted = sources | aliases
        for i, value in enumerate(values):
            new = set()
            if (
                value == "="
                and _IDENT.fullmatch(at(i - 1))
                and at(i - 2) != "."
                and at(i - 1) not in tainted
            ):
                end = _expression_end(tokens, i + 1)
                if _tainted_value(tokens, i + 1, end, tainted):
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
            if new - aliases:
                aliases |= new
                changed = True
    for path, op in _writes(source):
        if path[0] in aliases and (len(path) > 1 or op in _MUTATORS or op == "extend"):
            return f"经别名 {path[0]} 改了栏目数据"
    for i, value in enumerate(values):
        if value not in aliases | sources or at(i - 1) == ".":
            continue
        callee = _enclosing_callee(values, i)
        if callee is None:
            continue
        name = values[callee]
        if at(callee - 1) == ".":
            root = callee
            while root >= 2 and values[root - 1] == "." and _IDENT.fullmatch(values[root - 2]):
                root -= 2
            if values[root] in _SAFE_ROOTS or name in _PURE_METHODS:
                continue
            return f"栏目数据交给了 {name}，看不出它改不改"
        if name in _SAFE_CALLS:
            continue
        function = functions.get(name)
        if function is None or not _read_only_body(*function):
            return f"栏目数据交给了页面函数 {name}，它会改或者看不出来"
    return None


def _tainted_value(tokens, start, end, tainted):
    """start 到 end 这段表达式的值会不会是 tainted 里的对象或者它的一部分：读成员、下标、条件、
    或运算里出现就算；函数调用的结果不算（交给函数当参数另查），函数体里的也不算。"""
    values = [t.value for t in tokens]
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
            k = _matching_token(tokens, k) + 1
            continue
        if value in tainted and prev != ".":
            return True
        k += 1
    return False


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
    values = [t.value for t in tokens]
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
    values = [token.value for token in tokens]
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
    脚本顶层的一句，或者顶层 $(function () {…})、jQuery(function () {…})、$(document).ready(
    function () {…}) 回调里的一句（回调里只有这一句）。写在 if、别的函数、事件回调里的不算。"""
    count = sum(
        [t.value for t in tokens][i : i + 2] == ["new", "PublicTree"]
        for tokens in token_sets
        for i in range(len(tokens) - 1)
    )
    if count != 1:
        return False
    tokens = token_sets[tree_index]
    values = [token.value for token in tokens]
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
            return True
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
                return True
    return False


def find_lonsun_api(
    html: str, page_url: str, column_url: str, *, catalog_host=None, _diagnosis=None
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
        scripts = []
        candidates = []
        for el in doc.iter("script"):
            if any(a.tag in ("noscript", "template") for a in el.iterancestors()):
                continue
            if (el.get("type") or "").strip().lower() not in (
                "",
                "text/javascript",
                "application/javascript",
            ):
                continue
            src = el.get("src")
            if src is not None:
                candidates.append(src)
            else:
                source = el.text or ""
                scripts.append(source)
                written = _written_script(source)
                if written is not None:
                    candidates.append(written)
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
        tree_declarations = 0
        tree_extensions = 0
        for source, tokens in zip(scripts, token_sets, strict=True):
            values = [token.value for token in tokens]
            is_tree_script = any(
                values[i : i + 3] == ["function", "PublicTree", "("] for i in range(len(values) - 2)
            )
            tree_writes = 0
            for path, op in _writes(source):
                # window.catalogs 和 window["catalogs"] 同样是页面全局状态。
                if path[0] in ("window", "globalThis") and len(path) > 1:
                    path = path[1:]
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
                ):
                    return fail("内联脚本改写了依赖状态")
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
        constructors = []
        extends = 0
        for n, tokens in enumerate(token_sets):
            v = [t.value for t in tokens]
            for i in range(len(v) - 2):
                if v[i : i + 7] == ["$", ".", "extend", "(", "PublicTree", ".", "prototype"]:
                    extends += 1
                if v[i : i + 3] == ["function", "PublicTree", "("]:
                    constructors.append((n, i))
        if len(constructors) != 1 or extends != 1 or tree_extensions != 1:
            return None
        n, index = constructors[0]
        tokens = token_sets[n]
        values = [t.value for t in tokens]
        if values[index : index + 5] != ["function", "PublicTree", "(", ")", "{"]:
            return None
        if not _instantiated_on_load(scripts, token_sets, n):
            return fail("页面没有在加载时创建 PublicTree")
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
        constructor_writes = _writes(
            scripts[n][tokens[index + 4].end : tokens[end].start], skip_functions=True
        )
        for path, op in constructor_writes:
            if _tracked_write(path) and not (
                len(path) == 2 and path[0] == "me" and path[1] in permitted and op == "="
            ):
                return None
        for key in ("ztreeData", "ztreeData2", "refresh"):
            if sum(path == ("me", key) for path, _ in constructor_writes) != 1:
                return None
        init_values = {}
        for statement in statements:
            if statement[0] == "expression" and statement[1][0] == "assign":
                expr = statement[1]
                target = _path(expr[2])
                if target in ("me.ztreeData", "me.ztreeData2", "me.refresh"):
                    if target in init_values or expr[1] != "=":
                        return None
                    init_values[target] = expr[3]
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
        for candidate in candidates:
            url = urljoin(page_url, candidate)
            parts = urlsplit(url)
            if re.search(r"/[0-9]+/" + re.escape(spec[0]) + r"\.js$", parts.path):
                if urlsplit(page_url).scheme == "https" and parts.scheme == "http":
                    return fail("HTTPS 页面引用了 HTTP 栏目树脚本")
                if not catalog_host_allowed(page_url, url, catalog_host):
                    page_host = urlsplit(page_url).hostname
                    if catalog_host_allowed(f"https://{page_host}/", f"https://{parts.hostname}/"):
                        return fail("栏目树脚本的协议、端口或用户名密码不符合要求")
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
        )
        return ListApi(adapter=LONSUN, url=urls[-1], base_url=page_url, plan=plan)
    except (ValueError, etree.LxmlError, TypeError, AttributeError, RecursionError):
        return None


def lonsun_unrecognized_reason(
    html: str, page_url: str, column_url: str, *, catalog_host: str | None = None
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
            and (el.get("type") or "").strip().lower()
            in ("", "text/javascript", "application/javascript")
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
        find_lonsun_api(html, page_url, column_url, catalog_host=catalog_host, _diagnosis=reasons)
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


@dataclass(frozen=True)
class LonsunRequestPlan:
    mode: str
    cat_id: str
    excluded_children: tuple[int, ...] = ()


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
    if args[-1] == "ztreeData" and not hide_ok:
        raise LonsunFormatError("页面会按自己的设置隐去部分下级栏目，认不出隐去哪些")
    return modified, hide_ok


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
        _check_pre_methods(raw, dict(plan.globals))
        modified, hide_ok = _tree_rules(raw)
        if not hide_ok:
            raise LonsunFormatError("hideIds 的写法无法确定")
        if not _init_calls_getdata(raw["init"]):
            return "init 里找不到可识别的 getData 调用"
        spec = column_spec(api.base_url)
        if spec is None:
            return "栏目网址无效"
        cat_id = spec[1]
        wrapped = []
        for node in nodes:
            children = [
                n
                for n in nodes
                if n is not node and _loose_equal(n.get("parentId", UNDEFINED), node["id"])
            ]
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
                # 平台 common.min.js：get 通过 TT.isEmpty 将缺失值转为空串。
                return query.get(args[0], "")
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
            if not isinstance(data, dict) or any(
                v is UNKNOWN or isinstance(v, (list, dict, tuple)) for v in data.values()
            ):
                raise Stop("列表请求参数无法求值")
            if any(re.fullmatch(r"0|[1-9][0-9]*", str(k)) and int(k) < 4294967295 for k in data):
                raise Stop("列表参数含整数键，顺序无法确定")
            strings = {
                k: "" if v is None or v is UNDEFINED else _js_string(v) for k, v in data.items()
            }
            mode = strings.get("action")
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
            raise Stop(
                ListApi(
                    adapter=LONSUN,
                    url=f"{parts.scheme}://{parts.netloc}{plan.label}?{encoded}",
                    base_url=api.base_url,
                    plan=LonsunRequestPlan(mode, cat_id, excluded),
                )
            )

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

                        if len(expr[2]) != 3:
                            raise Stop("pushState 参数认不出")
                        target = _evaluate(expr[2][2], env)
                        if not isinstance(target, str):
                            raise Stop("pushState 地址无法确定")
                        source_url, target_url = (
                            urlsplit(api.base_url),
                            urlsplit(urljoin(api.base_url, target)),
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
    except (ValueError, TypeError, IndexError, KeyError, RecursionError):
        return "页面或栏目树认不出：解析失败"
