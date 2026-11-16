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
        # robots.txt 回 429：服务器忙、读不到，不取页面；第一份照常写库（带重试次数），网站停
        # 6 小时、写进共享的停取文件
        assert calls == ["/robots.txt"]
        assert saved == [1]
        assert [r.status for r in results] == ["robots_error", "site_paused"]
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


def test_robots_network_error_pauses_one_hour(tmp_path):
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
    # 连不上、超时这类读不到 robots.txt：停 1 小时、写进共享文件，下次运行不再先选它（真是我们
    # 自己断网，熔断会停下这次运行）；证书、加密连接出错停 6 小时（见下一条）
    reason = "robots.txt 读不到（连不上、超时）"
    assert pause.reasons == {"a.gov.cn": reason}
    assert _SitePause(str(path)).reasons == {"a.gov.cn": reason}
    assert _hours_left(path, "a.gov.cn") == pytest.approx(1, abs=0.05)


def test_robots_protection_page_pauses_host(tmp_path):
    path = tmp_path / "pause.json"

    class Fake:
        def fetch(self, url):
            return FetchResult(url, "robots_error", robots_status=200, robots_failure="protected")

    pause = _SitePause(str(path))
    _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=pause)
    assert pause.reasons == {"a.gov.cn": "robots.txt 回了防护页或验证码"}
    assert _hours_left(path, "a.gov.cn") == pytest.approx(24, abs=0.05)


def test_robots_too_large_pauses_six_hours(tmp_path):
    path = tmp_path / "pause.json"

    class Fake:
        def fetch(self, url):
            return FetchResult(url, "robots_error", robots_status=200, robots_failure="too_large")

    pause = _SitePause(str(path))
    _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=pause)
    assert pause.reasons == {"a.gov.cn": "robots.txt 读不到（太大读不完）"}
    assert _hours_left(path, "a.gov.cn") == pytest.approx(6, abs=0.05)


def test_robots_certificate_error_pauses_six_hours(tmp_path):
    path = tmp_path / "pause.json"

    class Fake:
        def fetch(self, url):
            return FetchResult(url, "robots_error", robots_failure="ssl_error")

    pause = _SitePause(str(path))
    _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=pause)
    assert pause.reasons == {"a.gov.cn": "robots.txt 读不到（证书或加密连接出错）"}
    assert _hours_left(path, "a.gov.cn") == pytest.approx(6, abs=0.05)


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


def test_pdf_host_robots_redirect_into_paused_host_defers_document(tmp_path):
    from datetime import UTC, datetime, timedelta

    from youju.crawler import detail_service

    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))
    until = datetime.now(UTC) + timedelta(hours=5)
    pause.add("c.gov.cn", "HTTP 403", until=until)
    fetched = FetchResult(
        "https://b.gov.cn/f.pdf",
        "site_paused",
        final_url="https://b.gov.cn/f.pdf",
        robots_url="https://c.gov.cn/robots.txt",
    )
    outcome = detail_service._FetchOutcome(
        7, datetime.now(UTC), fetched, object(), "site_paused", ""
    )
    detail_service._follow_paused(pause, 7, "https://a.gov.cn/detail", outcome)
    # 详情页在 A、PDF 在 B，B 的 robots.txt 跳进停取中的 C：B 跟着停，这份文件也延后到 C 恢复
    assert pause.expiry("b.gov.cn") == pause.expiry("c.gov.cn")
    assert f"{detail_service.DEFERRED_PREFIX}7" in pause.reasons
    assert "a.gov.cn" not in pause.reasons


def test_thread_start_failure_stops_started_groups(monkeypatch):
    # 中途起不了线程：已经起来的取页线程得停下（不然卡在排队上空转，进程退不出、一直占着队列锁）
    fetched = threading.Event()
    threads = set()

    class Pool(ThreadPoolExecutor):
        submitted = 0

        def submit(self, *args, **kwargs):
            Pool.submitted += 1
            if Pool.submitted == 3:
                assert fetched.wait(5)  # 第一组已经在取、排队排满了
                raise RuntimeError("can't start new thread")
            return super().submit(*args, **kwargs)

    class Fake:
        def fetch(self, url):
            threads.add(threading.current_thread())
            if url.endswith("/3"):
                fetched.set()
            return FetchResult(url, "ok", text="<html></html>")

    monkeypatch.setattr(detail_service, "ThreadPoolExecutor", Pool)
    selected = [(i, f"https://h{i % 3}.gov.cn/{i}") for i in range(30)]
    with pytest.raises(RuntimeError, match="start new thread"):
        _fetch_outcomes(selected, Fake(), 1)
    assert threads
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()


def test_fetch_details_prints_at_most_30_paused_hosts(monkeypatch, capsys):
    import argparse
    import contextlib

    from youju import cli
    from youju.crawler.detail_service import DetailFetchResults

    hosts = {f"h{i:02d}.gov.cn": "HTTP 403" for i in range(35)}
    monkeypatch.setattr(
        detail_service,
        "fetch_details",
        lambda session, **kwargs: DetailFetchResults([], paused_hosts=hosts),
    )
    monkeypatch.setattr(cli, "session_factory", lambda: contextlib.nullcontext)
    args = argparse.Namespace(limit=10, source_id=None, workers=1, since=None, notices_only=False)
    assert cli._cmd_fetch_details(args) == 0
    out = capsys.readouterr().out
    assert out.count("停取 ") == 30
    assert "另有 5 个停取中的主机" in out


def _text_result(url, body, content_type):
    return FetchResult(
        url,
        "ok",
        final_url=url,
        http_status=200,
        text=body,
        content=body.encode("utf-8"),
        content_type=content_type,
    )


@pytest.mark.parametrize(
    "first,content_type,body",
    [
        # 回 200 的纯文字验证码页、拦截页（D-013：网站拦着我们）
        ("https://a.gov.cn/1", "text/plain", "请输入验证码"),
        ("https://a.gov.cn/1", "text/plain; charset=utf-8", "您的访问已被网站防火墙拦截"),
        # 说是 PDF，其实是验证码页
        ("https://a.gov.cn/1.pdf", "application/pdf", "请输入验证码后继续访问"),
    ],
)
def test_plain_text_gate_detail_pauses_host(tmp_path, first, content_type, body):
    path = str(tmp_path / "pause.json")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return _text_result(url, body, content_type)

    outcomes = _fetch_outcomes(
        [(1, first), (2, "https://a.gov.cn/2")], Fake(), 1, pause=_SitePause(path)
    )
    assert outcomes[1].status == "protected"
    # 停这个网站、写进共享文件（24 小时）：同一网站的下一份不再去
    assert calls == [first]
    assert _SitePause(path).expiry("a.gov.cn") is not None


@pytest.mark.parametrize("content_type", ["text/plain", "application/pdf"])
def test_plain_text_gate_pdf_attachment_pauses_pdf_host(tmp_path, content_type):
    path = str(tmp_path / "pause.json")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            if url.endswith(".pdf"):
                return _text_result(url, "请输入验证码", content_type)
            return _page_with_pdf(url)

    outcomes = _fetch_outcomes(
        [(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")], Fake(), 1, pause=_SitePause(path)
    )
    # PDF 附件回的是纯文字验证码页：记防护页、停 PDF 所在网站，不当成 PDF 读不出
    assert outcomes[1].status == "protected"
    assert _SitePause(path).expiry("files.gov.cn") is not None
    assert calls.count("https://files.gov.cn/f.pdf") == 1


def test_plain_text_that_is_not_a_gate_stays_unsupported():
    class Fake:
        def fetch(self, url):
            return _text_result(url, "关于印发某某办法的通知", "text/plain")

    outcome = detail_service._fetch_one(1, "https://a.gov.cn/1", Fake())
    assert outcome.status == "unsupported_format"


def _host_file_locked(rate_dir, host):
    """别的进程这时能不能拿到这个网站的跨进程锁（另开一个文件描述符试，拿不到就是被占着）。"""
    import fcntl
    import os

    from youju.crawler.fetcher import _SharedLimits

    fd = os.open(_SharedLimits(str(rate_dir)).path("h", host), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


@pytest.mark.parametrize("where", ["page", "robots"])
def test_gate_page_paused_while_holding_host_lock(tmp_path, monkeypatch, where):
    # 回 200 的验证码页：判防护页、写停取的时候还拿着这个网站的跨进程锁，别的进程排在后面，等它
    # 放锁时停取已经写好，不会等够 3 秒又去敲门（robots.txt 回防护页也一样）
    gate = "<html><title>安全验证</title><body>请输入验证码</body></html>"
    rate = tmp_path / "rate"
    seen = []
    name = "_gate_page" if where == "page" else "robots_gate"
    real = getattr(detail_service, name)

    def checking(*args):
        seen.append(_host_file_locked(rate, "a.gov.cn"))
        return real(*args)

    monkeypatch.setattr(detail_service, name, checking)

    def handler(request):
        if request.url.path == "/robots.txt":
            if where == "robots":
                return httpx.Response(200, text=gate, headers={"content-type": "text/html"})
            return httpx.Response(404)
        return httpx.Response(200, text=gate, headers={"content-type": "text/html"})

    path = str(tmp_path / "pause.json")
    with PoliteFetcher(
        "test",
        per_host_interval=0.05,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
        shared_limits_dir=str(rate),
    ) as fetcher:
        _fetch_outcomes(
            [(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")],
            fetcher,
            1,
            pause=_SitePause(path),
        )
    assert seen and seen[0] is True
    assert _SitePause(path).expiry("a.gov.cn") is not None


_SAFEDOG = (
    "<html><head><title>网站防火墙</title></head><body><div>"
    "<img src='http://404.safedog.cn/images/safedogsite/head.png'>"
    "<p>您的请求带有不合法参数，已被网站管理员设置拦截！</p>"
    "<p>可能原因：您提交的内容包含危险的攻击请求</p></div></body></html>"
)
_YUNSUO = (
    "<html><head><script>var yunsuo_session_verify = 'abc'; document.cookie = 'x';"
    "</script></head><body></body></html>"
)


def _html_result(url, html, content_type="text/html"):
    return FetchResult(
        url, "ok", final_url=url, http_status=200, text=html, content_type=content_type
    )


@pytest.mark.parametrize("html", [_SAFEDOG, _YUNSUO], ids=["safedog", "yunsuo"])
def test_platform_block_page_is_protected(tmp_path, html):
    # 安全狗拦截页、云锁验证页：网站拦着我们，停这个网站，不把拦截页上的字当正文存下
    path = str(tmp_path / "pause.json")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return _html_result(url, html)

    outcomes = _fetch_outcomes(
        [(1, "https://a.gov.cn/1"), (2, "https://a.gov.cn/2")], Fake(), 1, pause=_SitePause(path)
    )
    assert outcomes[1].status == "protected"
    assert calls == ["https://a.gov.cn/1"]
    assert _SitePause(path).expiry("a.gov.cn") is not None


def test_normal_page_with_safedog_badge_is_read():
    # 正常详情页页脚挂"网站安全狗"徽标：有正文、有导航链接，照常读
    body = "关于开展2026年度专项资金申报工作的通知。各有关单位：现将有关事项通知如下。" * 30
    nav = "".join(f"<a href='/c{i}.html'>栏目{i}</a>" for i in range(20))
    html = (
        f"<html><head><title>通知</title></head><body><div>{nav}</div>"
        f"<div id='zoom'><p>{body}</p></div>"
        "<div class='footer'><img src='/images/safedog.png' alt='网站安全狗'></div></body></html>"
    )

    class Fake:
        def fetch(self, url):
            return _html_result(url, html)

    outcome = detail_service._fetch_one(1, "https://a.gov.cn/1", Fake())
    assert outcome.status == "ok", outcome.failure_reason


@pytest.mark.parametrize(
    "body,content_type,expected",
    [
        # 接口的 JSON、长的正常文字：不是拦截页
        ('{"code":0,"captcha":false,"msg":"ok"}', "text/plain", "unsupported_format"),
        ("关于开展网络安全防火墙产品采购的通知。" * 200, "text/plain", "unsupported_format"),
        # 没有 Content-Type 的纯文字拦截页；写着 text/html、其实是纯文字的也一样
        ("Access Denied", None, "protected"),
        ("Access Denied", "text/html", "protected"),
        # JSON 只看值：拒绝的话写在值里的认，captcha 这类键名不算
        ('{"code":403,"msg":"访问被拒绝"}', "application/json", "protected"),
        # 300 字以上的纯文字是正常文件，开头写着"禁止访问"也不算
        ("禁止访问区域名单的通知。" + "附表说明。" * 80, "text/plain", "unsupported_format"),
    ],
    ids=["json", "long-notice", "no-content-type", "html-type", "json-refusal", "notice-300"],
)
def test_plain_text_gate_edges(body, content_type, expected):
    class Fake:
        def fetch(self, url):
            return FetchResult(
                url,
                "ok",
                final_url=url,
                http_status=200,
                text=body,
                content=body.encode("utf-8"),
                content_type=content_type,
            )

    outcome = detail_service._fetch_one(1, "https://a.gov.cn/1", Fake())
    assert outcome.status == expected, outcome.failure_reason


def test_manual_commands_share_pause_file():
    # body_refresh、refetch_redacted、名单 rescan、补标题都不传停取记录：默认用配置的共享文件，
    # 队列刚停的网站它们不去，它们被拒也写进去（队列下次照样看得到）
    from youju.config import get_settings

    path = get_settings().detail_pause_path
    _SitePause(path).add("a.gov.cn", "HTTP 403")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return FetchResult(url, "http_error", final_url=url, http_status=403)

    selected = [(1, "https://a.gov.cn/1"), (2, "https://b.gov.cn/2")]
    _fetch_outcomes(selected, Fake(), 1)
    assert calls == ["https://b.gov.cn/2"]  # a.gov.cn 在停取中，不去
    assert _SitePause(path).expiry("b.gov.cn") is not None  # b 被拒，写进共享文件


def test_block_page_text_not_kept_as_body():
    # 认出安全狗这类拦截页：交回的结果不带正文，拦截页上的字不会写进详情
    class Fake:
        def fetch(self, url):
            return _html_result(url, _SAFEDOG)

    outcome = detail_service._fetch_one(1, "https://a.gov.cn/1", Fake())
    assert outcome.status == "protected"
    assert outcome.parsed is None or not outcome.parsed.body_text


@pytest.mark.parametrize(
    "robots,expected",
    [
        # 没有 robots.txt、回首页（软 404）：页脚挂安全狗徽标、标题里有"访问受限"也照常当没有 robots
        (
            "<html><head><title>首页</title></head><body>"
            + "".join(f"<a href='/c{i}.html'>栏目{i}</a>" for i in range(40))
            + "<p>关于部分系统访问受限的说明</p>"
            "<img src='/images/safedog.png' alt='网站安全狗'></body></html>",
            "ok",
        ),
        # 只剩云锁脚本的裸页：网站拦着我们
        (_YUNSUO, "robots_error"),
    ],
    ids=["soft-404-homepage", "yunsuo"],
)
def test_robots_html_gate_needs_bare_page(robots, expected):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots, headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html><body>ok</body></html>")

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        assert fetcher.fetch("https://a.gov.cn/list.html").status == expected


def test_gate_pause_written_while_holding_host_lock(tmp_path, monkeypatch):
    # 不只是在锁里判过：停取记录也是在还拿着这个网站的跨进程锁时写进去的
    gate = "<html><title>安全验证</title><body>请输入验证码</body></html>"
    rate = tmp_path / "rate"
    writes = []
    real_add = _SitePause.add

    def add(self, host, reason, **kwargs):
        writes.append((host, _host_file_locked(rate, host)))
        return real_add(self, host, reason, **kwargs)

    monkeypatch.setattr(_SitePause, "add", add)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=gate, headers={"content-type": "text/html"})

    with PoliteFetcher(
        "test",
        per_host_interval=0.05,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
        shared_limits_dir=str(rate),
    ) as fetcher:
        _fetch_outcomes(
            [(1, "https://a.gov.cn/1")], fetcher, 1, pause=_SitePause(str(tmp_path / "p.json"))
        )
    assert writes and writes[0] == ("a.gov.cn", True)


_LOGIN = (
    "<html><head><title>统一身份认证登录</title></head><body><form>"
    "<input name='user'><input type='password' name='pwd'><button>登录</button>"
    "</form></body></html>"
)


@pytest.mark.parametrize("where", ["detail", "pdf"])
def test_login_page_is_protected_and_carries_no_body(tmp_path, where):
    # 整页就是登录框（标题写着登录、有密码框）：网站拦着我们，停取；交回的结果不带网页内容
    path = str(tmp_path / "pause.json")

    class Fake:
        def fetch(self, url):
            if where == "pdf" and not url.endswith(".pdf"):
                return _page_with_pdf(url)
            return _html_result(url, _LOGIN)

    outcomes = _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=_SitePause(path))
    outcome = outcomes[1]
    assert outcome.status == "protected"
    assert outcome.fetch.text == "" and outcome.fetch.content == b""
    host = "files.gov.cn" if where == "pdf" else "a.gov.cn"
    assert _SitePause(path).expiry(host) is not None


def _long_page(title, extra=""):
    body = "现将有关事项通知如下。各单位要按要求做好申报工作，材料齐全、内容真实。" * 30
    nav = "".join(f"<a href='/c{i}.html'>栏目{i}</a>" for i in range(20))
    return (
        f"<html><head><title>{title}</title></head><body><div>{nav}{extra}</div>"
        f"<div id='zoom'><p>{body}</p></div></body></html>"
    )


@pytest.mark.parametrize(
    "html,expected",
    [
        # 正常通知讲验证码使用要求：不是整页关卡，不停网站（这份记读不出、不取正文）
        (_long_page("关于规范使用短信验证码的通知"), "parse_empty"),
        # 页头挂个登录小框：照常读
        (_long_page("关于开展申报工作的通知", "<input type='password' name='p'>"), "ok"),
        # 文章标题带着验证、登录这类字（第十二轮独立 Claude）：标题长，不像关卡
        (
            _long_page(
                "关于开展2026年度检验检测机构能力验证工作的通知",
                "<input type='password' name='p'>",
            ),
            "ok",
        ),
        # 标题短、写着登录，但 captcha 只在脚本名里、页面有正文：照常读
        (_long_page("登录须知", "<script src='/js/captcha.js'></script>"), "ok"),
        (_long_page("安全检查 - 某局", "<input type='password' name='p'>"), "ok"),
    ],
    ids=["captcha-notice", "login-widget", "long-title", "script-captcha", "short-title-body"],
)
def test_normal_pages_mentioning_gates_do_not_pause(tmp_path, html, expected):
    path = str(tmp_path / "pause.json")

    class Fake:
        def fetch(self, url):
            return _html_result(url, html)

    outcomes = _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=_SitePause(path))
    assert outcomes[1].status == expected, outcomes[1].failure_reason
    assert _SitePause(path).expiry("a.gov.cn") is None


@pytest.mark.parametrize(
    "robots",
    [
        '{"captcha": false, "rules": []}',  # JSON 的键名不算
        "本站说明：" + "我们的防火墙会记录异常访问。" * 30,  # 超过 300 字的正常文字
    ],
    ids=["json-key", "long-text"],
)
def test_robots_text_gate_matches_detail_rules(robots):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots, headers={"content-type": "text/plain"})
        return httpx.Response(200, text="<html><body>ok</body></html>")

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        assert fetcher.fetch("https://a.gov.cn/list.html").status == "ok"


_FIREWALL = (
    "<html><head><title>访问被拒绝</title></head><body>"
    "<h1>您的访问已被网站防火墙拦截</h1><p>如有疑问请联系网站管理员。</p></body></html>"
)


def test_html_firewall_page_pauses_at_first_document(tmp_path):
    # 防火墙回 200 的拦截页多是网页：整页几乎没字、标题写着访问被拒绝，第一份就停这个网站
    path = str(tmp_path / "pause.json")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return _html_result(url, _FIREWALL)

    selected = [(i, f"https://a.gov.cn/{i}") for i in range(1, 21)]
    outcomes = _fetch_outcomes(selected, Fake(), 1, pause=_SitePause(path))
    assert outcomes[1].status == "protected"
    assert calls == ["https://a.gov.cn/1"]
    assert _SitePause(path).expiry("a.gov.cn") is not None


@pytest.mark.parametrize(
    "robots",
    [
        _FIREWALL,
        "<html><head><title>提示</title></head><body>请求过于频繁，请稍后再试</body></html>",
    ],
    ids=["firewall", "rate-limit"],
)
def test_robots_html_firewall_page_is_protected(robots):
    # robots.txt 回同样的网页拦截页：网站拦着我们，不当成软 404 接着去请求页面
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots, headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html><body>ok</body></html>")

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        result = fetcher.fetch("https://a.gov.cn/list.html")
    assert result.status == "robots_error" and result.robots_failure == "protected"
    assert paths == ["/robots.txt"]


def test_gate_check_does_not_decode_pdfs(monkeypatch):
    # PDF 先看开头的 %PDF-，不把整份解码一遍（取页器给 PDF 的 text 是空的）
    def boom(*args, **kwargs):
        raise AssertionError("PDF 不该整份解码")

    monkeypatch.setattr(detail_service, "decode_text", boom)
    pdf = FetchResult(
        "https://a.gov.cn/f.pdf",
        "ok",
        final_url="https://a.gov.cn/f.pdf",
        http_status=200,
        content=b"%PDF-1.7\n" + b"0" * 1000,
        content_type="application/pdf",
    )
    assert detail_service._gate_page(pdf, pdf.url) is False


def test_long_json_with_gate_word_is_not_a_gate():
    # 长的 JSON（正常接口返回）里有个"请输入验证码"的值也不算拦截页
    body = '{"tip":"请输入验证码","data":"' + "x" * 400 + '"}'

    class Fake:
        def fetch(self, url):
            return FetchResult(
                url,
                "ok",
                final_url=url,
                http_status=200,
                text=body,
                content=body.encode(),
                content_type="application/json",
            )

    assert detail_service._fetch_one(1, "https://a.gov.cn/1", Fake()).status == "unsupported_format"


_UTF16_LOGIN = (
    "<html><head><title>用户登录</title></head><body><input type='password'></body></html>"
)


@pytest.mark.parametrize(
    "content,content_type,text",
    [
        # 响应头写明 UTF-16：取页器解码好的文字是对的，不能按字节里的 NUL 当成二进制
        (_UTF16_LOGIN.encode("utf-16-le"), "text/html; charset=utf-16le", _UTF16_LOGIN),
        # 只有字节顺序标记：浏览器照它解码，取页器按 UTF-8、GB18030 解出来是乱码
        (b"\xff\xfe" + _UTF16_LOGIN.encode("utf-16-le"), "text/html", "\x00乱码\x00"),
        (b"\xfe\xff" + _UTF16_LOGIN.encode("utf-16-be"), "text/html", ""),
    ],
    ids=["declared", "bom-le", "bom-be"],
)
def test_utf16_login_page_is_a_gate(content, content_type, text):
    result = FetchResult(
        "https://a.gov.cn/1",
        "ok",
        final_url="https://a.gov.cn/1",
        http_status=200,
        text=text,
        content=content,
        content_type=content_type,
    )
    assert detail_service._gate_page(result, result.url) is True


def test_binary_file_is_not_a_gate():
    # 解码以后还带 NUL 的是二进制文件（.doc 这类），不算关卡
    content = b"\xd0\xcf\x11\xe0" + b"\x00" * 600 + "验证码".encode()
    result = FetchResult(
        "https://a.gov.cn/f",
        "ok",
        final_url="https://a.gov.cn/f",
        http_status=200,
        content=content,
        content_type="application/octet-stream",
    )
    assert detail_service._gate_page(result, result.url) is False


def test_vanished_pause_file_stops_run(tmp_path):
    # 运行中读到过的停取文件不见了（被删、挂载的目录没了）：不当成没有停取接着抓
    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))
    pause.add("a.gov.cn", "HTTP 403")
    assert pause.hosts == {"a.gov.cn": "HTTP 403"}
    path.unlink()
    with pytest.raises(detail_service._PauseStoreError):
        pause.reasons  # noqa: B018
    with pytest.raises(detail_service._PauseStoreError):
        pause.add("b.gov.cn", "HTTP 403")


def test_missing_pause_file_at_start_is_empty(tmp_path):
    # 一开始就没有停取文件：当成没有停取（第一次写的时候建）
    pause = _SitePause(str(tmp_path / "sub" / "pause.json"))
    assert pause.reasons == {}
    pause.add("a.gov.cn", "HTTP 403")
    assert pause.hosts == {"a.gov.cn": "HTTP 403"}


def test_known_pause_survives_file_losing_records(tmp_path):
    # 文件被换成少了记录的（停取只会变长、到期才去掉）：这个进程知道、没到期的照样停
    path = tmp_path / "pause.json"
    pause = _SitePause(str(path))
    pause.add("a.gov.cn", "HTTP 403")
    until = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    path.write_text(json.dumps({"b.gov.cn": {"reason": "HTTP 429", "until": until}}))
    assert pause.hosts == {"a.gov.cn": "HTTP 403", "b.gov.cn": "HTTP 429"}
    pause.add("c.gov.cn", "HTTP 412")
    assert set(json.loads(path.read_text())) == {"a.gov.cn", "b.gov.cn", "c.gov.cn"}


def test_login_page_with_navigation_still_pauses(tmp_path):
    # 真的登录页：标题短、写着登录，有密码框，页面上有导航（不是整页几乎没字）但取不到正文：停这个网站
    nav = "".join(f"<a href='/c{i}.html'>栏目{i}</a>" for i in range(20))
    html = (
        "<html><head><title>用户登录 - 某市人民政府</title></head><body>"
        f"<div>{nav}</div><form><input name='u'><input type='password' name='p'>"
        "<button>登录</button></form></body></html>"
    )
    path = str(tmp_path / "pause.json")

    class Fake:
        def fetch(self, url):
            return _html_result(url, html)

    outcomes = _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=_SitePause(path))
    assert outcomes[1].status == "protected"
    assert _SitePause(path).expiry("a.gov.cn") is not None


def test_bare_short_notice_mentioning_firewall_is_not_a_gate():
    # 整页几乎没字（链接少），标题是长的文章标题，看得见的字超过 300 字：不按"防火墙"这类字停网站
    text = "本项目采购防火墙设备一批，经评审确定中标供应商，现将结果公告如下。" * 12
    html = (
        "<html><head><title>某局防火墙设备采购结果公告</title></head><body>"
        f"<div id='zoom'><p>{text}</p></div></body></html>"
    )
    result = _html_result("https://a.gov.cn/1", html)
    assert detail_service._gate_page(result, result.url) is False
    # 同样很短的拦截页（看得见的字不到 300 字）照样认
    blocked = _html_result("https://a.gov.cn/2", _FIREWALL)
    assert detail_service._gate_page(blocked, blocked.url) is True


def _fetch_with_robots_bytes(robots_bytes, content_type="text/plain"):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=robots_bytes, headers={"content-type": content_type})
        return httpx.Response(200, text="<html><body>ok</body></html>")

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        return fetcher.fetch("https://a.gov.cn/list.html"), paths


@pytest.mark.parametrize("bom,codec", [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")])
def test_utf16_robots_rules_are_respected(bom, codec):
    # robots.txt 用 UTF-16 写（带字节顺序标记）：照标记解码，Disallow 照样守（第十三轮 Codex）
    result, paths = _fetch_with_robots_bytes(bom + "User-agent: *\nDisallow: /\n".encode(codec))
    assert result.status == "robots_disallowed"
    assert paths == ["/robots.txt"]


def test_utf16_robots_login_page_is_protected():
    page = "<html><head><title>用户登录</title></head><body><input type='password'></body></html>"
    result, paths = _fetch_with_robots_bytes(b"\xff\xfe" + page.encode("utf-16-le"), "text/html")
    assert result.status == "robots_error" and result.robots_failure == "protected"
    assert paths == ["/robots.txt"]


def test_robots_without_readable_text_pauses_six_hours(tmp_path):
    # 没写编码、也没有字节顺序标记的 UTF-16：解出来带 NUL，规则看不全，不当成"没有 robots"放行
    raw = "User-agent: *\nDisallow: /\n".encode("utf-16-le")
    result, paths = _fetch_with_robots_bytes(raw)
    assert result.status == "robots_error" and result.robots_failure == "unreadable"
    assert paths == ["/robots.txt"]
    path = tmp_path / "pause.json"

    class Fake:
        def fetch(self, url):
            return FetchResult(url, "robots_error", robots_status=200, robots_failure="unreadable")

    _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=_SitePause(str(path)))
    assert _hours_left(path, "a.gov.cn") == pytest.approx(6, abs=0.05)


_STCSM = (
    "https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260924/47c0b4118e0540bfab43d7e9af025b14.html"
)


def test_bare_real_notice_with_comment_captcha_script_is_read(tmp_path):
    # 上海科委的真实页面：链接少（整页几乎没字），留言区脚本里有 onCaptcha()（第十三轮独立 Claude）：
    # 照常读正文、附件，不停这个网站
    from tests.conftest import read_fixture_html

    html = read_fixture_html("detail_shanghai_stcsm")
    result = _html_result(_STCSM, html)
    assert detail_service._gate_page(result, _STCSM) is False
    path = str(tmp_path / "pause.json")
    calls = []

    class Fake:
        def fetch(self, url):
            calls.append(url)
            return _html_result(url, html)

    second = _STCSM.replace("47c0b411", "57c0b411")
    outcomes = _fetch_outcomes([(1, _STCSM), (2, second)], Fake(), 1, pause=_SitePause(path))
    assert outcomes[1].status == "ok" and outcomes[1].parsed.attachments
    assert calls == [_STCSM, second]
    assert _SitePause(path).expiry("stcsm.sh.gov.cn") is None


def test_bare_notice_titled_captcha_does_not_pause(tmp_path):
    # 链接少、正文约 900 字、标题"关于规范使用短信验证码的通知"：不是关卡，不停网站
    body = "为加强短信验证码管理，现就有关事项通知如下，请各单位认真贯彻执行。" * 28
    nav = "".join(f"<a href='/c{i}.html'>栏目{i}</a>" for i in range(5))
    html = (
        "<html><head><title>关于规范使用短信验证码的通知</title></head><body>"
        f"<div>{nav}</div><div id='zoom'><p>{body}</p></div></body></html>"
    )
    assert detail_service._gate_page(_html_result("https://a.gov.cn/1", html), "x") is False
    path = str(tmp_path / "pause.json")

    class Fake:
        def fetch(self, url):
            return _html_result(url, html)

    outcomes = _fetch_outcomes([(1, "https://a.gov.cn/1")], Fake(), 1, pause=_SitePause(path))
    assert outcomes[1].status != "protected"
    assert _SitePause(path).expiry("a.gov.cn") is None
