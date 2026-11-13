import posixpath
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_SKIP_PREFIXES = ("javascript:", "mailto:", "tel:", "#")
_WHITESPACE = re.compile(r"[\r\n\t]+")
# 会话标识：同一份文件每次访问可能带不同的值，不能参与"是不是同一份文件"的判断
# sid 不在其中：不少网站用它区分栏目或站点（如 show.asp?sid=2&id=100）
_SESSION_PARAMS = {"jsessionid", "phpsessid", "sessionid", "aspsessionid"}
_PATH_SESSION = re.compile(r";(?:jsessionid|sessionid)=[^/?#]*", re.IGNORECASE)
# 目录的默认页：/tzgg/index.html 与 /tzgg/ 是同一页
_INDEX_PAGE = re.compile(r"/(?:index|default)\.(?:html?|shtml|jsp|php|aspx?)$", re.IGNORECASE)
# 数字广东信息公开平台的分类栏目（"部门页#xxgk-gkmlpt-分类编号"，见 crawler/adapters.py）：同一个
# 部门页上的不同分类是不同的来源，这段片段要留在键里；别的片段（页面内锚点等）照旧去掉
_GKMLPT_FRAGMENT = re.compile(r"xxgk-gkmlpt-p?[0-9]+(?:,p?[0-9]+)*")


def public_url(url: str) -> str:
    """修正被来源误写为 HTTP 443 的原文地址。"""
    parts = urlsplit(url)
    if parts.scheme.lower() != "http" or not parts.netloc.lower().endswith(":443"):
        return url
    return urlunsplit(
        ("https", parts.netloc.rsplit(":", 1)[0], parts.path, parts.query, parts.fragment)
    )


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
    return public_url(
        urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    )


def url_key(url: str) -> str:
    """判断"是不是同一份文件"用的规范键，入库时以它去重。

    - 不区分 http 和 https，去掉 80、443 端口；
    - 去掉路径里的会话标识（;jsessionid=…）和查询里的会话参数；
    - 折叠 /./ 与 /../，目录默认页（index.html 等）收成目录，查询参数按名称排序；
    - 去掉 # 后面的片段，只有数字广东信息公开平台的分类（#xxgk-gkmlpt-编号）留着：那是不同的来源。
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
    path = _INDEX_PAGE.sub("/", path)
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _SESSION_PARAMS
        )
    )
    return (
        f"{netloc}{path}"
        + (f"?{query}" if query else "")
        + (f"#{parts.fragment}" if _GKMLPT_FRAGMENT.fullmatch(parts.fragment) else "")
    )


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _site_netloc(parts) -> str:
    """主机名小写；协议的默认端口不算：https://x:443 和 https://x 是同一个网站（限速、robots、
    抓取分组都按它，同一网站两次请求照样隔 3 秒，D-013）。端口写坏的照原样返回。"""
    netloc = parts.netloc.lower()
    try:
        port = parts.port
    except ValueError:
        return netloc
    if port is not None and port == _DEFAULT_PORTS.get(parts.scheme.lower()):
        return netloc.rsplit(":", 1)[0]
    return netloc


def host_of(url: str) -> str:
    """网站标识：抓取程序按它排队、隔 3 秒，抓取和试读按它分组（同一网站内串行）。"""
    return _site_netloc(urlsplit(url))


def origin_of(url: str) -> str:
    """协议 + 网站（读 robots.txt、缓存 robots 结果用）。"""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{_site_netloc(parts)}"


def page_dir(url: str) -> str:
    """列表页所在目录，如 /zwgk/tzgg/index.html → /zwgk/tzgg/。

    末段没有扩展名时当作目录：/zwgk/tzgg 与 /zwgk/tzgg/ 是同一个栏目。
    """
    path = urlsplit(url).path or "/"
    if path.endswith("/"):
        return path
    head, _, last = path.rpartition("/")
    return path + "/" if "." not in last else head + "/"
