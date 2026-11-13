import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from youju.crawler import detail_service
from youju.crawler.detail_service import _fetch_outcomes, _SitePause
from youju.crawler.fetcher import FetchResult, PoliteFetcher


@pytest.mark.parametrize("code", [401, 403, 412, 429, 521])
def test_first_refusal_is_persisted_and_expires(tmp_path, code):
    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))

    class Fake:
        def fetch(self, url):
            return FetchResult(url, "http_error", final_url=url, http_status=code)

    selected = [(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")]
    result = _fetch_outcomes(selected, Fake(), 1, pause=pause)
    assert list(result) == [1]
    assert _fetch_outcomes(selected, Fake(), 1, pause=_SitePause(str(path))) == {}
    data = json.loads(path.read_text())
    data["a.gov.cn"]["until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(data))
    assert list(_fetch_outcomes(selected, Fake(), 1, pause=_SitePause(str(path)))) == [1]


def test_concurrent_pause_writes_are_merged(tmp_path):
    path = str(tmp_path / "pause.json")
    pauses = [_SitePause(path), _SitePause(path)]
    barrier = threading.Barrier(2)

    def write(index):
        barrier.wait()
        pauses[index].add(f"{index}.gov.cn", "HTTP 403")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    assert set(_SitePause(path).reasons) == {"0.gov.cn", "1.gov.cn"}


def test_gate_refusal_releases_address_slot():
    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        per_address_concurrency=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        fetcher._local.gate = lambda *args, **kwargs: False
        assert fetcher._request("https://a.gov.cn/a").status == "site_paused"
        fetcher._local.gate = None
        assert fetcher._request("https://a.gov.cn/a").status == "ok"


def test_stop_before_pdf():
    calls = []
    stop = threading.Event()

    class Fake:
        def fetch(self, url):
            calls.append(url)
            stop.set()
            return FetchResult(url, "ok", text='<a href="https://b.gov.cn/a.pdf">附件.pdf</a>')

    results = _fetch_outcomes([(1, "https://a.gov.cn/a")], Fake(), 1, should_stop=stop.is_set)
    assert results == {}
    assert calls == ["https://a.gov.cn/a"]


def test_write_failure_stops_other_groups():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            if "b.gov.cn" in url:
                started.set()
                release.wait(2)
            return FetchResult(url, "ok", text="<html></html>")

    def save(outcome):
        assert started.wait(2)
        release.set()
        raise ValueError("write failed")

    selected = [(1, "https://a.gov.cn/1")] + [(i, f"https://b.gov.cn/{i}") for i in range(2, 102)]
    with pytest.raises(ValueError, match="write failed"):
        _fetch_outcomes(selected, Fake(), 2, on_outcome=save)
    assert len(calls) < 101


def test_pdf_429_pauses_pdf_host_and_blocks_redirect(tmp_path):
    calls = []
    pause = _SitePause(str(tmp_path / "pause.json"))

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "b.gov.cn":
            return httpx.Response(429)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://b.gov.cn/target"})
        if request.url.path == "/first":
            return httpx.Response(
                200,
                text='<a href="https://b.gov.cn/a.pdf">附件.pdf</a>',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, text="<html>页面</html>")

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        results = _fetch_outcomes(
            [
                (1, "https://a.gov.cn/first"),
                (2, "https://a.gov.cn/next"),
                (3, "https://a.gov.cn/redirect"),
            ],
            fetcher,
            1,
            pause=pause,
        )
    assert results[1].fetch.http_status == 429
    assert 2 in results and 3 not in results
    assert pause.reasons == {"b.gov.cn": "HTTP 429"}
    assert "https://b.gov.cn/target" not in calls


def test_protected_is_persisted(tmp_path):
    path = str(tmp_path / "pause.json")

    class Fake:
        def fetch(self, url):
            return FetchResult(url, "ok", text="<html><title>验证码</title>请输入验证码</html>")

    selected = [(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")]
    outcomes = _fetch_outcomes(selected, Fake(), 1, pause=_SitePause(path))
    assert outcomes[1].status == "protected"
    assert _fetch_outcomes(selected, Fake(), 1, pause=_SitePause(path)) == {}


@pytest.mark.parametrize("robots_status", [403, 429])
def test_robots_response_controls_page_and_persistence(tmp_path, monkeypatch, robots_status):
    calls = []
    saved = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status)
        return httpx.Response(
            200,
            text=(
                "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。</p>"
                "<p>辽宁省工业和信息化厅</p></div></body></html>"
            ),
            headers={"content-type": "text/html"},
        )

    def save(session, outcome):
        saved.append(outcome.document_id)
        return detail_service.DetailFetchSummary(outcome.document_id, outcome.status, None)

    monkeypatch.setattr(detail_service, "_save_result", save)
    pause = _SitePause(str(tmp_path / "pause.json"))
    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        results = detail_service._fetch_selected(
            None,
            [(1, "https://a.gov.cn/detail"), (2, "https://a.gov.cn/next")],
            fetcher,
            1,
            pause=pause,
        )
    if robots_status == 403:
        assert calls == ["/robots.txt", "/detail", "/next"]
        assert saved == [1, 2]
        assert [r.status for r in results] == ["ok", "ok"]
        assert pause.reasons == {}
    else:
        assert calls == ["/robots.txt"]
        assert saved == []
        assert [r.status for r in results] == ["site_paused", "site_paused"]
        assert pause.reasons == {"a.gov.cn": "robots_error"}
        assert not (tmp_path / "pause.json").exists()


def test_streaming_returns_no_full_outcomes():
    class Fake:
        def fetch(self, url):
            return FetchResult(url, "ok", text="<html></html>")

    saved = []
    result = _fetch_outcomes(
        [(1, "https://a.gov.cn/1")],
        Fake(),
        1,
        on_outcome=lambda outcome: saved.append(outcome.document_id),
    )
    assert saved == [1]
    assert result == {}


def test_robots_error_pauses_only_this_run(tmp_path):
    calls = []
    path = tmp_path / "pause.json"

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return FetchResult(url, "robots_error" if "a.gov.cn" in url else "ok")

    pause = _SitePause(str(path))
    results = _fetch_outcomes(
        [(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2"), (3, "https://b.gov.cn/3")],
        Fake(),
        1,
        pause=pause,
    )
    assert set(results) == {1, 3}
    assert len(calls) == 2
    assert pause.reasons == {"a.gov.cn": "robots_error"}
    assert not path.exists()
