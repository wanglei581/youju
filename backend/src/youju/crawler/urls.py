import re
from urllib.parse import urljoin, urlsplit, urlunsplit

_SKIP_PREFIXES = ("javascript:", "mailto:", "tel:", "#")
_WHITESPACE = re.compile(r"[\r\n\t]+")


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


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
