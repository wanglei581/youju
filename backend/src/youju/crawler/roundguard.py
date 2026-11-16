"""一轮里"这个主机拒绝过我们"的护栏：龙讯的栏目页、栏目树、列表接口共用（正式抓取、试读、抽查各用
一份，见 crawler/service.py、govsites/verify.py、govsites/spotcheck.py）。

D-013：网站拒绝了就停，不接着敲门。一个主机这一轮被拒（401/403/412/521、防护页、跳到页面没写的
地址、读取护栏判了不能抓）或者回了 429，同一轮这个主机的别的地址不再去，记"这次没取到"
（site_paused，下次照常再试），不写不能抓的记录。robots 不许的只是那个路径不读（别的路径各按
robots 判，本来就不发请求），不算主机拒绝了我们。查、取、判、记在同一把主机锁里做完：几个线程同时
要去同一个主机时，判出拒绝之前不会有第二个请求溜过去。"""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from urllib.parse import urlsplit

from youju.crawler.fetcher import FetchResult
from youju.crawler.urls import url_key

ROUND_SKIP = "同一轮这个主机拒绝过我们或回了 429，这次不去"
REFUSE_HTTP = (401, 403, 412, 429, 521)


def round_skip(url: str) -> FetchResult:
    """没去的那个地址：记"这次没取到"（site_paused，下次照常再试），不写不能抓的记录。"""
    return FetchResult(url, "site_paused", final_url=url, error=ROUND_SKIP)


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def refused_result(url: str, result: FetchResult, *, redirects: bool = True) -> bool:
    """通用的"被拒"：401/403/412/429/521；redirects 时跳到页面没写的地址也算（只换协议的不算）。
    栏目页按普通网页取、跟着跳转，跳转不算被拒（redirects=False）。robots 不许的不算（见上）。"""
    from youju.crawler.adapters import _redirect_words

    if result.http_status in REFUSE_HTTP:
        return True
    if not redirects:
        return False
    moved = bool(result.final_url) and url_key(result.final_url or url) != url_key(url)
    return bool(
        (result.status == "redirect" or moved) and _redirect_words(url, result.final_url or url)
    )


class HostRoundGuard:
    def __init__(self) -> None:
        self._lock = Lock()
        self._locks: dict[str, Lock] = {}
        self._results: dict[str, FetchResult] = {}
        self._blocked_hosts: set[str] = set()

    def fetch(
        self,
        url: str,
        fetch: Callable[..., FetchResult],
        *,
        refused: Callable[[FetchResult], bool] | None = None,
        cache: bool = False,
        redirects: bool = True,
        **kwargs,
    ) -> FetchResult:
        """按本轮的主机护栏取 url：这个主机这一轮拒绝过我们就不去（round_skip）；取回来照通用
        规则（refused_result）和 refused(结果) 判，被拒就记下这个主机。cache：同一地址这一轮只取
        一次。"""
        host = _host(url)
        with self._lock:
            lock = self._locks.setdefault(host, Lock())
        with lock:
            if cache and url in self._results:
                return self._results[url]
            if self.blocked(url):
                return round_skip(url)
            result = fetch(url, **kwargs)
            if cache:
                self._results[url] = result
            if result.status != "robots_disallowed" and (  # robots 不许不算主机拒绝
                refused_result(url, result, redirects=redirects)
                or refused is not None
                and refused(result)
            ):
                self.block(url)
            return result

    def get(
        self,
        url: str,
        fetch: Callable[..., FetchResult],
        refused: Callable[[FetchResult], bool] | None = None,
    ) -> FetchResult:
        """龙讯栏目树：同一地址这一轮只取一次（各栏目共用），只取地址本身、跳转不跟。"""
        return self.fetch(url, fetch, refused=refused, cache=True, same_address_only=True)

    def blocked(self, url: str) -> bool:
        with self._lock:
            return _host(url) in self._blocked_hosts

    def block(self, url: str) -> None:
        with self._lock:
            self._blocked_hosts.add(_host(url))
