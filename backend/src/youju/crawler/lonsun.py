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
from urllib.parse import parse_qsl, unquote, urlsplit

from youju.crawler.dates import _safe_date
from youju.crawler.listing import ListItem, _looks_like_title
from youju.crawler.titles import clean_title
from youju.crawler.urls import normalize_url, url_key

# 依据 2026-11-13 区县栏目页实测；新增城市须先审查同城集约化平台关系。
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
            or set(params) - {"type", "action", "catId", "nav"}
            or params.get("type") != "4"
            or params.get("action") != "list"
            or not re.fullmatch(r"[0-9]+", params.get("catId", ""))
            or ("nav" in params and not re.fullmatch(r"[0-9]+", params["nav"]))
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
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", name, re.ASCII):
                raise LonsunFormatError(f"认不出记号 {name}")
        if declaration and name is None:
            raise LonsunFormatError("函数声明缺少名字")
        self.take("(")
        args = []
        while self.peek() != ")":
            arg = self.take()
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", arg, re.ASCII):
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
    return match.group(2)


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
        token_sets = [_js_tokens(s) for s in scripts]
        constructors = []
        for n, tokens in enumerate(token_sets):
            v = [t.value for t in tokens]
            for i in range(len(v) - 2):
                if v[i : i + 3] == ["function", "PublicTree", "("]:
                    constructors.append((n, i))
                if v[i : i + 2] == ["PublicTree", "="] or (
                    v[i : i + 3] == ["PublicTree", ".", "prototype"]
                    and (i < 4 or v[i - 4 : i] != ["$", ".", "extend", "("])
                ):
                    return None
        if len(constructors) != 1:
            return None
        n, index = constructors[0]
        tokens = token_sets[n]
        values = [t.value for t in tokens]
        if values[index : index + 5] != ["function", "PublicTree", "(", ")", "{"]:
            return None
        if not any(
            values[i : i + 4] == ["new", "PublicTree", "(", ")"] for i in range(len(values) - 3)
        ):
            return None
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
        organ = _parse_expression("me.organId")
        if any(tree[0] != "literal" and tree != organ for _, tree in params[1]):
            return None
        list_params = tuple((k, _immutable(_evaluate(v, env))) for k, v in params[1])
        urls = []
        for candidate in candidates:
            url = urljoin(page_url, candidate)
            parts = urlsplit(url)
            if re.search(r"/[0-9]+/" + re.escape(spec[0]) + r"\.js$", parts.path):
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
        if len({urlsplit(u).path for u in urls}) != 1:
            return fail("栏目树脚本有几处、路径不一样")
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
    for st in _parse_statements(body):
        if st == direct:
            return True
        if st[0] != "expression":
            continue
        expr = st[1]
        if _call_path(expr) is not None or expr[0] != "call" or _path(expr[1]) is not None:
            continue
        callee = expr[1]
        if callee[0] != "member" or callee[2] != ("literal", "done") or len(expr[2]) != 1:
            continue
        inner, callback = callee[1], expr[2][0]
        if (
            not (_call_path(inner) or "").startswith("me.")
            or callback[0] != "function"
            or callback[2]
        ):
            continue
        if _parse_statements(body[callback[3] : callback[4]]) == (direct,):
            return True
    return False


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
                and expr[2][0] == ("identifier", "data")
                and data_ready
            ):
                fn = expr[2][1]
                if fn[0] == "function" and len(fn[2]) == 2:
                    index, item = fn[2]
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
        modified, hide_ok = _tree_rules(raw)
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
            wrapped.append(_Node(node, value, modified))
        node = next((n for n in wrapped if _loose_equal(n.data["id"], cat_id)), None)
        query = _platform_query(urlsplit(api.base_url).query)
        refresh_expected = _parse_statements(
            "var me = this; if ('pushState' in history) { me.refresh = false; }"
        )
        refresh = (
            False if methods.get("isRefresh", (None, None))[1] == refresh_expected else UNKNOWN
        )
        me = {
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
        globals_ = dict(plan.globals)
        globals_.update(
            {
                "this": me,
                "me": me,
                "location": {"href": api.base_url},
                "Ls": {"url": {"get": "url.get"}},
                "$": {"extend": "extend"},
                "_organTree2": {"getNodeByParam": "node"},
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
                return query.get(args[0], "")
            if (
                fn == "extend"
                and len(args) == 3
                and all(isinstance(x, dict) for x in args)
                and not args[0]
            ):
                return {**args[1], **args[2]}
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
                        "window.history.pushState",
                        "console.log",
                    ):
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
    except (LonsunFormatError, ValueError, TypeError, IndexError, KeyError, RecursionError) as exc:
        return f"页面或栏目树认不出：{exc}"
