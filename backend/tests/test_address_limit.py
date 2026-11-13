import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from youju.crawler.fetcher import PoliteFetcher


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_interval_shared_address_and_robots():
    clock = Clock()
    starts = []

    def handler(request):
        starts.append((request.url.host, request.url.path, clock.now))
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200)

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
        clock=lambda: clock.now,
        sleep=clock.sleep,
        per_address_interval=2,
    ) as fetcher:
        assert fetcher.fetch("https://a.gov.cn/page").status == "ok"
        assert fetcher.fetch("https://b.gov.cn/page").status == "ok"
    assert [t for _, _, t in starts] == [0, 2, 4, 6]
    assert clock.sleeps == [2, 2, 2]


@pytest.mark.parametrize("interval,shared", [(1, False), (0, True)])
def test_different_addresses_and_disabled_interval(interval, shared):
    resolver = lambda h: ["1.2.3.4" if shared or h.startswith("a") else "1.2.3.5"]  # noqa: E731
    clock = Clock()
    with PoliteFetcher(
        "test",
        per_host_interval=0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        resolver=resolver,
        clock=lambda: clock.now,
        sleep=clock.sleep,
        per_address_interval=interval,
        per_address_concurrency=0,
    ) as fetcher:
        assert fetcher._get("https://a.gov.cn/").status == "ok"
        assert fetcher._get("https://b.gov.cn/").status == "ok"
    assert clock.sleeps == []


@pytest.mark.parametrize("limit,expected", [(2, 2), (0, 3)])
def test_concurrency_limit_and_zero_unlimited(limit, expected):
    lock = threading.Lock()
    release = threading.Event()
    reached = threading.Event()
    active = 0
    peak = 0

    def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == expected:
                reached.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return httpx.Response(200)

    with (
        PoliteFetcher(
            "test",
            per_host_interval=0,
            per_address_interval=0,
            per_address_concurrency=limit,
            resolver=lambda h: ["1.2.3.4"],
            transport=httpx.MockTransport(handler),
        ) as fetcher,
        ThreadPoolExecutor(max_workers=3) as pool,
    ):
        futures = [pool.submit(fetcher._get, f"https://{i}.gov.cn/") for i in range(3)]
        try:
            assert reached.wait(2)
            if limit:
                time.sleep(0.1)
                with lock:
                    assert active == 2
        finally:
            release.set()
        assert all(f.result(timeout=3).status == "ok" for f in futures)
    assert peak == expected


def test_deadline_waiting_for_slot():
    entered = threading.Event()
    release = threading.Event()

    def handler(request):
        entered.set()
        assert release.wait(3)
        return httpx.Response(200)

    with (
        PoliteFetcher(
            "test",
            per_host_interval=0,
            per_address_interval=0,
            per_address_concurrency=1,
            resolver=lambda h: ["1.2.3.4"],
            transport=httpx.MockTransport(handler),
            deadline=time.monotonic() + 0.3,
        ) as fetcher,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        first = pool.submit(fetcher._get, "https://a.gov.cn/")
        try:
            assert entered.wait(1)
            result = fetcher._get("https://b.gov.cn/")
            assert result.status == "deadline"
            assert result.final_url == "https://b.gov.cn/"
        finally:
            release.set()
        first.result(timeout=2)


def test_network_retry_releases_slot_and_obeys_interval():
    clock = Clock()
    starts = []

    def handler(request):
        starts.append(clock.now)
        if len(starts) == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200)

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=2,
        per_address_concurrency=1,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
        clock=lambda: clock.now,
        sleep=clock.sleep,
    ) as fetcher:
        assert fetcher._get("https://a.gov.cn/").status == "ok"
    assert starts == [0, 2]
    assert clock.sleeps == [0, 2]
