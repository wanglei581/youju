"""礼貌抓取：遵守 robots.txt（RFC 9309）、同一网站限速、透明标识，不绕过任何防护。"""

import re
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

import certifi
import httpx

from youju.crawler.urls import host_of, normalize_url, origin_of

ROBOTS_TOKEN = "YoujuBot"
_MAX_REDIRECTS = 5
_ROBOTS_TTL_SECONDS = 24 * 3600
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
# GBK、GB2312 都用超集 GB18030 解码，避免个别生僻字出错
_CHARSET_ALIASES = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030"}


@dataclass
class FetchResult:
    url: str
    # ok / http_error / network_error / ssl_error / robots_disallowed / robots_error；
    # unsupported_host 仅在配置了网址限制时返回。
    # redirect 只在内部使用，fetch() 不会返回它
    status: str
    final_url: str = ""
    http_status: int | None = None
    text: str = ""
    error: str | None = None
    content_type: str | None = None
    content: bytes = b""


@dataclass
class _RobotsEntry:
    state: str  # rules / allow_all / unreachable
    parser: RobotFileParser | None
    fetched_at: float


def compat_ssl_context() -> ssl.SSLContext:
    """兼容部分老旧政府网站的加密设置，证书仍照常校验。

    - 允许旧式重协商：OpenSSL 3 默认禁止，个别网站握手时报 UNSAFE_LEGACY_RENEGOTIATION；
    - 密钥交换固定用 P-256：OpenSSL 3.5 默认优先发送后量子混合密钥，个别网站报 BAD_ECPOINT。
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    ctx.set_ecdh_curve("prime256v1")
    return ctx


def decode_html(content: bytes, content_type: str | None) -> str:
    """优先用响应头声明的编码，其次页面 meta 声明，最后依次尝试 UTF-8 和 GB18030。"""
    candidates: list[str] = []
    if content_type and "charset=" in content_type.lower():
        candidates.append(content_type.lower().split("charset=")[-1].split(";")[0].strip(" \"'"))
    m = _META_CHARSET.search(content[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore").lower())
    candidates += ["utf-8", "gb18030"]
    for name in candidates:
        name = _CHARSET_ALIASES.get(name, name)
        try:
            return content.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


class PoliteFetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        per_host_interval: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        compat_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        url_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        self._client = httpx.Client(
            headers=headers, timeout=timeout, follow_redirects=False, transport=transport
        )
        # 兼容模式：只在默认握手失败时对该网站使用，证书照常校验
        self._compat_client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            verify=compat_ssl_context(),
            transport=compat_transport or transport,
        )
        self._compat_hosts: set[str] = set()
        self._interval = per_host_interval
        self._clock = clock
        self._sleep = sleep
        self._url_allowed = url_allowed
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._robots: dict[str, _RobotsEntry] = {}
        self._robots_locks: dict[str, threading.Lock] = {}

    def close(self) -> None:
        self._client.close()
        self._compat_client.close()

    def __enter__(self) -> "PoliteFetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, url: str) -> FetchResult:
        """抓取一个页面。重定向逐跳处理：每一跳都检查 robots.txt，并遵守目标网站的限速。"""
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            if self._url_allowed is not None and not self._url_allowed(current):
                return FetchResult(url, "unsupported_host", final_url=current, error="非政府网站")
            state, allowed = self.robots_allows(current)
            if state == "unreachable":
                # RFC 9309 §2.3.1.4：robots.txt 因服务器错误或网络问题无法获取，视为全部禁止
                return FetchResult(
                    url,
                    "robots_error",
                    final_url=current,
                    error="robots.txt 暂时无法获取，本次跳过",
                )
            if state == "unsupported_host":
                return FetchResult(url, "unsupported_host", final_url=current, error="非政府网站")
            if not allowed:
                return FetchResult(
                    url, "robots_disallowed", final_url=current, error="robots.txt 不允许抓取该页面"
                )
            result = self._get(current)
            if result.status != "redirect":
                result.url = url
                return result
            current = result.final_url
        return FetchResult(
            url, "network_error", final_url=current, error=f"重定向超过 {_MAX_REDIRECTS} 次"
        )

    def robots_allows(self, url: str) -> tuple[str, bool]:
        origin = origin_of(url)
        # 同一网站并发时只读取一次 robots.txt
        with self._lock_for(self._robots_locks, origin):
            entry = self._robots.get(origin)
            if entry is None or self._clock() - entry.fetched_at > _ROBOTS_TTL_SECONDS:
                entry = self._load_robots(origin)
                self._robots[origin] = entry
        if entry.state in {"unreachable", "unsupported_host"}:
            return entry.state, False
        if entry.state == "allow_all" or entry.parser is None:
            return entry.state, True
        return entry.state, entry.parser.can_fetch(ROBOTS_TOKEN, url)

    def _load_robots(self, origin: str) -> _RobotsEntry:
        current = origin + "/robots.txt"
        # RFC 9309 §2.3.1.2：robots.txt 的重定向至少跟随 5 次；超过则视为不可用（可以抓取）
        for _ in range(_MAX_REDIRECTS + 1):
            if self._url_allowed is not None and not self._url_allowed(current):
                return _RobotsEntry("unsupported_host", None, self._clock())
            result = self._get(current)
            if result.status != "redirect":
                break
            current = result.final_url
        else:
            return _RobotsEntry("allow_all", None, self._clock())
        now = self._clock()
        status = result.http_status
        if result.status in ("network_error", "ssl_error") or (
            status is not None and status >= 500
        ):
            return _RobotsEntry("unreachable", None, now)
        if status is not None and 400 <= status < 500:
            # RFC 9309 §2.3.1.3：4xx（包括 401、403）视为 robots.txt 不可用，可以抓取
            return _RobotsEntry("allow_all", None, now)
        body = result.text.lstrip()
        if not body or body.startswith("<"):
            # 返回的是 HTML（常见的"软 404"），视为没有 robots.txt
            return _RobotsEntry("allow_all", None, now)
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return _RobotsEntry("rules", parser, now)

    def _lock_for(self, locks: dict[str, threading.Lock], key: str) -> threading.Lock:
        with self._guard:
            return locks.setdefault(key, threading.Lock())

    def _get(self, url: str) -> FetchResult:
        host = host_of(url)
        with self._lock_for(self._host_locks, host):
            last = self._last_request.get(host)
            if last is not None:
                wait = self._interval - (self._clock() - last)
                if wait > 0:
                    self._sleep(wait)
            try:
                return self._request(url)
            finally:
                self._last_request[host] = self._clock()

    def _request(self, url: str) -> FetchResult:
        host = host_of(url)
        last_error = ""
        network_retries = 1  # 超时或连接失败时重试一次
        while True:
            client = self._compat_client if host in self._compat_hosts else self._client
            try:
                with client.stream("GET", url) as resp:
                    if resp.status_code < 300:
                        size = 0
                        chunks = []
                        for chunk in resp.iter_bytes():
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                return FetchResult(
                                    url,
                                    "too_large",
                                    final_url=str(resp.url),
                                    http_status=resp.status_code,
                                    error="文件超过 20MB",
                                )
                            chunks.append(chunk)
                        content = b"".join(chunks)
            except httpx.TimeoutException as exc:
                last_error = f"超时：{exc.__class__.__name__}"
            except httpx.ConnectError as exc:
                message = str(exc)
                upper = message.upper()
                if "CERTIFICATE" in upper:
                    # 证书问题绝不用兼容模式绕过。常见原因：网站没有发送中间证书
                    return FetchResult(
                        url, "ssl_error", error=f"证书校验失败（证书链可能不完整）：{message[:200]}"
                    )
                if "SSL" in upper:
                    if host in self._compat_hosts:
                        return FetchResult(url, "ssl_error", error=f"加密连接错误：{message[:200]}")
                    # 老旧服务器与 OpenSSL 3 默认设置不兼容：改用兼容模式立即重试，不占用重试次数
                    with self._guard:
                        self._compat_hosts.add(host)
                    continue
                last_error = f"连接失败：{message[:200]}"
            except httpx.HTTPError as exc:
                return FetchResult(
                    url, "network_error", error=f"{exc.__class__.__name__}: {exc}"[:300]
                )
            else:
                if 300 <= resp.status_code < 400:
                    target = normalize_url(url, resp.headers.get("location"))
                    if target is None:
                        return FetchResult(
                            url,
                            "http_error",
                            http_status=resp.status_code,
                            error=f"HTTP {resp.status_code}，重定向地址无效",
                        )
                    # 不自动跟随：交给 fetch() 逐跳检查 robots 和限速
                    return FetchResult(
                        url, "redirect", final_url=target, http_status=resp.status_code
                    )
                if resp.status_code >= 400:
                    return FetchResult(
                        url,
                        "http_error",
                        final_url=str(resp.url),
                        http_status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                    )
                return FetchResult(
                    url,
                    "ok",
                    final_url=str(resp.url),
                    http_status=resp.status_code,
                    text=(
                        ""
                        if content.startswith(b"%PDF-")
                        else decode_html(content, resp.headers.get("content-type"))
                    ),
                    content_type=resp.headers.get("content-type"),
                    content=content,
                )
            if network_retries == 0:
                return FetchResult(url, "network_error", error=last_error)
            network_retries -= 1
            self._sleep(self._interval)
