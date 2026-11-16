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
    # 第三份跳到停取中的 b.gov.cn：这份延后到 b 恢复，a.gov.cn 只停这次运行
    assert pause.reasons["a.gov.cn"] == "跳到停取中的 b.gov.cn"
    assert pause.expiry("a.gov.cn") is None
    assert pause.expiry("doc:3") == pause.expiry("b.gov.cn")
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
        # robots.txt 回 429：服务器忙、读不到，不取页面；网站停 6 小时，写进共享的停取文件
        assert calls == ["/robots.txt"]
        assert saved == []
        assert [r.status for r in results] == ["site_paused", "site_paused"]
        assert pause.reasons == {"a.gov.cn": "robots.txt HTTP 429"}
        assert _hours_left(tmp_path / "pause.json", "a.gov.cn") == pytest.approx(6, abs=0.05)


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


def _hours_left(path, host):
    until = datetime.fromisoformat(json.loads(path.read_text())[host]["until"])
    return (until - datetime.now(UTC)).total_seconds() / 3600


def test_robots_network_error_pauses_this_run_only(tmp_path):
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
    # 连不上、超时这类读不到 robots.txt：可能是我们自己断网，只停这次运行，不写进共享文件
    # （回 5xx 的停 1 小时，见 test_robots_server_errors_pause_in_file）
    reason = "robots.txt 读不到（连不上、超时或证书问题）"
    assert pause.reasons == {"a.gov.cn": reason}
    assert _SitePause(str(path)).reasons == {}


def _robots_fetch(tmp_path, robots, page=200):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots)
        if page != 200:
            return httpx.Response(page)
        return httpx.Response(
            200,
            text=(
                "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。</p>"
                "<p>辽宁省工业和信息化厅</p></div></body></html>"
            ),
            headers={"content-type": "text/html"},
        )

    pause = _SitePause(str(tmp_path / "pause.json"))
    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        outcomes = _fetch_outcomes(
            [(1, "https://a.gov.cn/detail"), (2, "https://a.gov.cn/next")], fetcher, 1, pause=pause
        )
    return calls, outcomes, pause


@pytest.mark.parametrize(
    "robots,robots_calls,reason,hours",
    [
        # 521 是防护页：不当场再请求一次，停 6 小时
        (521, 1, "robots.txt HTTP 521", 6),
        # 别的 5xx：按限速间隔再取一次，还取不到停 1 小时
        (503, 2, "robots.txt 读不到（HTTP 503）", 1),
    ],
)
def test_robots_server_errors_pause_in_file(tmp_path, robots, robots_calls, reason, hours):
    calls, outcomes, pause = _robots_fetch(tmp_path, robots)
    assert calls == ["/robots.txt"] * robots_calls
    assert [o.status for o in outcomes.values()] == ["robots_error"]
    assert pause.reasons == {"a.gov.cn": reason}
    assert _hours_left(tmp_path / "pause.json", "a.gov.cn") == pytest.approx(hours, abs=0.05)


def test_robots_403_then_page_403_pauses_host(tmp_path, caplog):
    # robots.txt 回 403 照 RFC 9309 当没有 robots、照常取页面；页面也回 403 才算被拒绝：停 24 小时
    with caplog.at_level("INFO", logger="youju.crawler.fetcher"):
        calls, outcomes, pause = _robots_fetch(tmp_path, 403, page=403)
    assert calls == ["/robots.txt", "/detail"]
    assert [o.status for o in outcomes.values()] == ["http_error"]
    assert pause.reasons == {"a.gov.cn": "HTTP 403"}
    assert _hours_left(tmp_path / "pause.json", "a.gov.cn") == pytest.approx(24, abs=0.05)
    assert caplog.text.count("robots.txt 回 HTTP 403") == 1


def test_robots_403_then_page_ok_reads(tmp_path, caplog):
    with caplog.at_level("INFO", logger="youju.crawler.fetcher"):
        calls, outcomes, pause = _robots_fetch(tmp_path, 403)
    assert calls == ["/robots.txt", "/detail", "/next"]
    assert [o.status for o in outcomes.values()] == ["ok", "ok"]
    assert pause.reasons == {}
    # 每个主机每次运行只记一次
    assert caplog.text.count("robots.txt 回 HTTP 403") == 1


def test_pause_written_by_other_process_blocks_next_admit(tmp_path):
    path = str(tmp_path / "pause.json")
    mine, other = _SitePause(path), _SitePause(path)
    guarded = detail_service._PausingFetcher(object(), mine)
    assert guarded._admit("https://a.gov.cn/1", record=False)
    other.add("a.gov.cn", "HTTP 403")
    # 文件变了：下一次就重读，挡下
    assert not guarded._admit("https://a.gov.cn/2", record=False)


def test_run_only_pause_survives_file_sync(tmp_path):
    path = str(tmp_path / "pause.json")
    mine, other = _SitePause(path), _SitePause(path)
    mine.add("b.gov.cn", "搜索限流防护页", persistent=False)
    other.add("a.gov.cn", "HTTP 403")
    mine.add("c.gov.cn", "HTTP 412")
    assert mine.reasons == {
        "a.gov.cn": "HTTP 403",
        "b.gov.cn": "搜索限流防护页",
        "c.gov.cn": "HTTP 412",
    }
    # 只停这次运行的不写进文件
    assert set(json.loads((tmp_path / "pause.json").read_text())) == {"a.gov.cn", "c.gov.cn"}


def test_shorter_pause_does_not_shorten_longer(tmp_path):
    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))
    pause.add("a.gov.cn", "HTTP 403")
    pause.add("a.gov.cn", "robots.txt 读不到（HTTP 503）", hours=1)
    assert pause.reasons == {"a.gov.cn": "HTTP 403"}
    assert _hours_left(path, "a.gov.cn") == pytest.approx(24, abs=0.05)


def test_unreadable_pause_file_stops_run(tmp_path):
    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))
    path.write_text("{not json")
    with pytest.raises(detail_service._PauseStoreError):
        pause.reasons  # noqa: B018


def test_protected_detail_page_skips_its_pdf(monkeypatch):
    from youju.crawler.detail import Attachment, DetailResult

    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return FetchResult(url, "ok", text="<html><body>请输入验证码</body></html>")

    # 就算解析结果里带了一个 PDF 附件，防护页也不再去取附件
    monkeypatch.setattr(
        detail_service,
        "parse_detail",
        lambda text, url: DetailResult(
            failure_reason="防护页或验证码",
            attachments=[Attachment("通知.pdf", "https://a.gov.cn/f.pdf", "pdf")],
        ),
    )
    outcome = detail_service._fetch_one(1, "https://a.gov.cn/detail", Fake())
    assert outcome.status == "protected"
    assert calls == ["https://a.gov.cn/detail"]


def _page_with_pdf(url):
    return FetchResult(
        url,
        "ok",
        final_url=url,
        http_status=200,
        text="<html><body><div id='zoom'><a href='https://files.gov.cn/f.pdf'>附件.pdf</a></div>"
        "</body></html>",
        content_type="text/html",
    )


def test_redirect_to_paused_host_pauses_source_host(tmp_path):
    path = str(tmp_path / "pause.json")
    pause = _SitePause(path)
    pause.add("b.gov.cn", "HTTP 403")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://b.gov.cn/same"})

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        outcomes = _fetch_outcomes(
            [(n, f"https://a.gov.cn/{n}") for n in range(1, 6)], fetcher, 1, pause=pause
        )
    # A 上的文件都跳到停取中的 B：取了第一份，这份延后到 B 恢复，A 这次运行不再去
    assert [c for c in calls if not c.endswith("/robots.txt")] == ["https://a.gov.cn/1"]
    assert outcomes == {}
    assert pause.reasons["a.gov.cn"] == "跳到停取中的 b.gov.cn"
    again = _SitePause(path)
    assert "a.gov.cn" not in again.reasons  # A 没拒绝过我们：不写进共享文件
    assert again.expiry("doc:1") == again.expiry("b.gov.cn")


def test_pdf_on_paused_host_defers_only_that_document(tmp_path, monkeypatch):
    from youju.crawler.detail import Attachment, DetailResult

    path = str(tmp_path / "pause.json")
    pause = _SitePause(path)
    pause.add("files.gov.cn", "HTTP 403")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return _page_with_pdf(url)

    monkeypatch.setattr(
        detail_service,
        "parse_detail",
        lambda text, url: DetailResult(
            attachments=[Attachment("附件.pdf", "https://files.gov.cn/f.pdf", "pdf")]
        ),
    )
    _fetch_outcomes([(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")], Fake(), 1, pause=pause)
    # 两份都去取了详情页（A 没拒绝过我们），PDF 在停取中的网站上：这两份延后，A 不停
    assert calls == ["https://a.gov.cn/1", "https://a.gov.cn/2"]
    again = _SitePause(path)
    assert "a.gov.cn" not in again.reasons
    assert {"doc:1", "doc:2"} <= set(again.reasons)
    assert "doc:1" not in again.hosts
    # 下次运行不再选这两份
    calls.clear()
    _fetch_outcomes([(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")], Fake(), 1, pause=again)
    assert calls == []


@pytest.mark.parametrize(
    "reply,reason", [(FetchResult("", "http_error", http_status=405), "连续 3 份 HTTP 405")]
)
def test_streak_pauses_host_for_this_run(tmp_path, reply, reason):
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return FetchResult(url, reply.status, final_url=url, http_status=reply.http_status)

    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))
    _fetch_outcomes([(n, f"https://a.gov.cn/{n}") for n in range(1, 7)], Fake(), 1, pause=pause)
    assert len(calls) == 3
    assert pause.reasons == {"a.gov.cn": reason}
    assert not path.exists()  # 只停这次运行


def test_streamed_outcomes_drop_page_content_only_when_asked():
    class Fake:
        def fetch(self, url):
            return FetchResult(
                url, "http_error", final_url=url, http_status=404, content=b"x" * 10, text="页面"
            )

    seen = []
    _fetch_outcomes(
        [(1, "https://a.gov.cn/1")], Fake(), 1, on_outcome=seen.append, strip_content=True
    )
    assert seen[0].fetch.content == b"" and seen[0].fetch.text == ""
    assert seen[0].fetch.http_status == 404
    # 默认照旧带着页面：body_refresh 要拿原网页核对去掉的字
    kept = _fetch_outcomes([(2, "https://a.gov.cn/2")], Fake(), 1)
    assert kept[2].fetch.text == "页面" and kept[2].fetch.content == b"x" * 10


def test_queue_refuses_without_shared_rate_dir(tmp_path, monkeypatch):
    from youju.config import get_settings
    from youju.crawler.detail_service import run_detail_queue

    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x")
    monkeypatch.setattr(get_settings(), "rate_limit_dir", str(blocked))
    summary = run_detail_queue(None, minutes=1)
    assert summary.stop_reason.startswith(detail_service.SHARED_LIMIT_STOP)
    assert summary.total == 0
    # 没配目录也一样
    monkeypatch.setattr(get_settings(), "rate_limit_dir", "")
    assert run_detail_queue(None, minutes=1).stop_reason.startswith(
        detail_service.SHARED_LIMIT_STOP
    )


def _robots_redirect_fetch(tmp_path, robots_status, paused=None, docs=1):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "a.gov.cn" and request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"location": "https://c.gov.cn/robots.txt"})
        if request.url.host == "c.gov.cn":
            return httpx.Response(robots_status)
        return httpx.Response(200, text="<html>页面</html>", headers={"content-type": "text/html"})

    pause = _SitePause(str(tmp_path / "pause.json"))
    if paused:
        pause.add(paused, "HTTP 403")
    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        _fetch_outcomes(
            [(n, f"https://a.gov.cn/{n}") for n in range(1, docs + 1)], fetcher, 1, pause=pause
        )
    return calls, pause


@pytest.mark.parametrize("code,hours", [(429, 6), (521, 6)])
def test_robots_redirect_pauses_the_host_that_answered(tmp_path, code, hours):
    calls, pause = _robots_redirect_fetch(tmp_path, code)
    # 回 429/521 的是 c：停 c；a 的 robots.txt 跳到 c，a 跟着停
    assert pause.reasons["c.gov.cn"] == f"robots.txt HTTP {code}"
    assert pause.reasons["a.gov.cn"] == "robots.txt 跳到读不到的 c.gov.cn"
    assert _hours_left(tmp_path / "pause.json", "c.gov.cn") == pytest.approx(hours, abs=0.05)
    assert pause.expiry("a.gov.cn") == pause.expiry("c.gov.cn")


def test_robots_redirect_into_paused_host_requests_robots_once(tmp_path):
    calls, pause = _robots_redirect_fetch(tmp_path, 200, paused="c.gov.cn", docs=4)
    # c 已停取：a 的 robots.txt 跳进 c 被挡，a 跟着 c 停，4 份文件只请求了一次 a/robots.txt
    assert calls == ["https://a.gov.cn/robots.txt"]
    assert pause.reasons["a.gov.cn"] == "robots.txt 跳到停取中的 c.gov.cn"
    assert pause.expiry("a.gov.cn") == pause.expiry("c.gov.cn")


def test_queue_requires_shared_limits(tmp_path):
    from youju.crawler.fetcher import SharedLimitError

    with pytest.raises(SharedLimitError):
        PoliteFetcher("test", require_shared_limits=True)
    shared = tmp_path / "shared"
    fetcher = PoliteFetcher(
        "test",
        per_host_interval=0.01,
        per_address_interval=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        resolver=lambda host: ["1.2.3.4"],
        shared_limits_dir=str(shared),
        require_shared_limits=True,
    )
    with fetcher:
        for f in shared.iterdir():
            f.unlink()
        shared.chmod(0o500)  # 中途目录写不进去了
        try:
            with pytest.raises(SharedLimitError):
                fetcher.fetch("https://a.gov.cn/page")
        finally:
            shared.chmod(0o700)
