"""一轮里"这个主机拒绝过我们"的护栏：龙讯的栏目页、栏目树、列表接口共用（正式抓取、试读、抽查各用
一份，见 crawler/service.py、govsites/verify.py、govsites/spotcheck.py）。

D-013：网站拒绝了就停，不接着敲门。一个主机这一轮被拒（401/403/412/521、防护页、跳到页面没写的
地址、读取护栏判了不能抓）或者回了 429，同一轮这个主机的别的地址不再去，记"这次没取到"
（site_paused，下次照常再试），不写不能抓的记录。robots 不许的只是那个路径不读（别的路径各按
robots 判，本来就不发请求），不算主机拒绝了我们。查、取、判、记在同一把主机锁里做完：几个线程同时
要去同一个主机时，判出拒绝之前不会有第二个请求溜过去。"""

from __future__ import annotations

import inspect
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


def answered_hosts(url: str, result: FetchResult) -> set[str]:
    """这次请求里回了话的主机：请求过的每一跳（result.hops，栏目页会跟着跳转）；没记跳转的（缓存里
    旧的结果、测试替身）用请求的地址和最后的地址。只取地址本身、跳转不跟的回 redirect 时，要跳去的
    那个主机没请求过，不算。"""
    hops = [hop for hop in getattr(result, "hops", ()) or () if hop]
    if hops:
        return {_host(hop) for hop in hops} - {""}
    final = url if result.status == "redirect" else (result.final_url or url)
    return {_host(url), _host(final)} - {""}


def _accepts_hops(fetch: Callable[..., FetchResult]) -> bool:
    """取的函数认不认 hop_allowed（PoliteFetcher.fetch、PageCache.get 认；测试替身多半不认）。"""
    try:
        params = inspect.signature(fetch).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "hop_allowed" or p.kind is p.VAR_KEYWORD for p in params)


class HostRoundGuard:
    def __init__(self) -> None:
        self._lock = Lock()
        self._locks: dict[str, Lock] = {}
        self._results: dict[str, FetchResult] = {}
        self._refused: set[str] = set()  # 缓存的结果里被拒的那些地址
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
        """按本轮的主机护栏取 url：这个主机这一轮拒绝过我们就不去（round_skip）；跟着跳转时，跳转
        经过本轮已停的主机也不去（取的函数认 hop_allowed 就交给它，返回 site_paused；用的是缓存、
        替身时，跳进已停主机的结果不用）；取回来照通用规则（refused_result）和 refused(结果) 判，
        被拒就记下请求的主机和这次回了话的每个主机（a 跳到 b、b 回 403：真正拒绝我们的是 b）。
        cache：同一地址这一轮只取一次。"""
        host = _host(url)
        with self._lock:
            lock = self._locks.setdefault(host, Lock())
        with lock:
            if cache and url in self._results:
                if url in self._refused:
                    # 被拒的是别的来源去的那一次：这个来源自己没去，记"这次没取到"，不记不能抓
                    return round_skip(url)
                return self._results[url]
            if self.blocked(url):
                return round_skip(url)
            if _accepts_hops(fetch):
                kwargs["hop_allowed"] = lambda target, record=True: not self.blocked(target)
            result = fetch(url, **kwargs)
            answered = answered_hosts(url, result)
            if result.status != "site_paused" and any(
                self._host_blocked(name) for name in answered - {host}
            ):
                # 跳进了本轮已停的主机（缓存里的结果、不认 hop_allowed 的取法）：这次的结果不用
                return round_skip(url)
            refused_now = result.status != "robots_disallowed" and (  # robots 不许不算主机拒绝
                refused_result(url, result, redirects=redirects)
                or refused is not None
                and refused(result)
            )
            if cache:
                self._results[url] = result
                if refused_now:
                    self._refused.add(url)
            if refused_now:
                with self._lock:
                    self._blocked_hosts |= answered | {host}
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
        return self._host_blocked(_host(url))

    def _host_blocked(self, host: str) -> bool:
        with self._lock:
            return host in self._blocked_hosts

    def block(self, url: str) -> None:
        with self._lock:
            self._blocked_hosts.add(_host(url))
