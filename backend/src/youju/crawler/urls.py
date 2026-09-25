import posixpath
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_SKIP_PREFIXES = ("javascript:", "mailto:", "tel:", "#")
_WHITESPACE = re.compile(r"[\r\n\t]+")
# 会话标识：同一份文件每次访问可能带不同的值，不能参与"是不是同一份文件"的判断
_SESSION_PARAMS = {"jsessionid", "phpsessid", "sid", "sessionid", "aspsessionid"}
_PATH_SESSION = re.compile(r";(?:jsessionid|sessionid|sid)=[^/?#]*", re.IGNORECASE)


def normalize_url(base: str, href: str | None) -> str | None:
    """把相对链接转成绝对网址；去掉片段（#…）；非 http(s) 链接返回 None。"""
    if not href:
        return None
    href = _WHITESPACE.sub("", href).strip()
    if not href or href.lower().startswith(_SKIP_PREFIXES):
        return None
    parts = urlsplit(urljoin(base, href))
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def url_key(url: str) -> str:
    """判断"是不是同一份文件"用的规范键，入库时以它去重。

    - 不区分 http 和 https，去掉 80、443 端口；
    - 去掉路径里的会话标识（;jsessionid=…）和查询里的会话参数；
    - 折叠 /./ 与 /../，查询参数按名称排序。
    展示和访问仍使用原始网址。
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = _PATH_SESSION.sub("", parts.path or "/")
    trailing = path.endswith("/")
    path = posixpath.normpath(path) if path not in ("", "/") else "/"
    if trailing and not path.endswith("/"):
        path += "/"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _SESSION_PARAMS
        )
    )
    return f"{netloc}{path}" + (f"?{query}" if query else "")


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def page_dir(url: str) -> str:
    """列表页所在目录，如 /zwgk/tzgg/index.html → /zwgk/tzgg/。"""
    path = urlsplit(url).path or "/"
    return path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"
