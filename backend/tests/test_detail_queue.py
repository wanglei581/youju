from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_detail_service import FakeFetcher, add_document
from youju import cli
from youju.ai import pipeline
from youju.config import get_settings
from youju.crawler.detail_service import _SitePause, fetch_details, run_detail_queue
from youju.crawler.fetcher import FetchResult
from youju.models import DocumentDetail


def response(doc, status=200):
    return FetchResult(
        doc.url,
        "ok" if status == 200 else "http_error",
        http_status=status,
        text="<div id='zoom'><p>这是足够长的政府公开文件正文内容，需要保存。</p></div>",
    )


def test_priority_and_fetch_order(session):
    today = date(2026, 11, 13)
    docs = [add_document(session, 9000 + i) for i in range(8)]
    values = [
        (today, "其他", None),
        (today - timedelta(days=31), "政策", None),
        (None, "其他", None),
        (today, "其他", "政策"),
        (today, "政策", "其他"),
        (None, "政策", None),
        (today - timedelta(days=30), "申报通知", None),
        (today - timedelta(days=31), "其他", None),
    ]
    for doc, (published, kind, override) in zip(docs, values, strict=True):
        doc.publish_date, doc.rule_kind, doc.kind_override = published, kind, override
    session.commit()
    fake = FakeFetcher({d.url: response(d) for d in docs})
    results = fetch_details(session, limit=8, today=today, fetcher=fake, max_workers=1)
    assert [r.document_id for r in results] == [docs[i].id for i in (3, 6, 0, 4, 1, 5, 7, 2)]


def test_batches_and_429(session):
    docs = [add_document(session, 9100 + i) for i in range(4)]
    fake = FakeFetcher({d.url: response(d) for d in docs})
    summary = run_detail_queue(session, minutes=1, per_host=1, batch=3, fetcher=fake)
    assert (summary.total, summary.batches, summary.stop_reason) == (4, 4, "没有了")

    docs = [add_document(session, 9200 + i) for i in range(2)]
    fake = FakeFetcher({docs[0].url: response(docs[0], 429), docs[1].url: response(docs[1])})
    summary = run_detail_queue(session, minutes=1, fetcher=fake)
    detail = session.get(DocumentDetail, docs[0].id)
    assert detail.next_retry_at.replace(tzinfo=UTC) == detail.fetched_at.replace(
        tzinfo=UTC
    ) + timedelta(hours=6)
    assert session.get(DocumentDetail, docs[1].id) is None
    assert summary.paused_hosts == {"gxt.ln.gov.cn": "HTTP 429"}


def test_batch_window_keeps_other_hosts_and_allows_short_batch(session, monkeypatch):
    from tests.test_detail_service import _doc_on, _source_on
    from youju.crawler import detail_service

    a = _source_on(session, "a.example.gov.cn")
    b = _source_on(session, "b.example.gov.cn")
    c = _source_on(session, "c.example.gov.cn")
    docs = [_doc_on(session, a, "a.example.gov.cn", i) for i in range(29)]
    other = _doc_on(session, b, "b.example.gov.cn", 1)
    outside = _doc_on(session, c, "c.example.gov.cn", 1)
    docs += [other, outside]
    fake = FakeFetcher({d.url: response(d) for d in docs})
    batches = []
    finished = []
    original = detail_service._fetch_selected

    def fetch_selected(session, selected, *args, **kwargs):
        batches.append(list(selected))
        results = original(session, selected, *args, **kwargs)
        finished.append(True)
        return results

    monkeypatch.setattr(detail_service, "_fetch_selected", fetch_selected)
    summary = run_detail_queue(
        session,
        minutes=1,
        batch=3,
        per_host=1,
        workers=1,
        fetcher=fake,
        stop=lambda: bool(finished),
    )
    assert [doc_id for doc_id, _ in batches[0]] == [docs[0].id, other.id, outside.id]
    assert (summary.batches, summary.total, summary.stop_reason) == (1, 3, "每轮要锁")


@pytest.mark.parametrize(
    "codes, expected", [([403, 403, 403, 200], 1), ([403, 200, 403, 403, 200], 1)]
)
def test_403_streak(session, codes, expected):
    docs = [add_document(session, 9300 + i) for i in range(len(codes))]
    fake = FakeFetcher({d.url: response(d, code) for d, code in zip(docs, codes, strict=True)})
    summary = run_detail_queue(session, minutes=1, fetcher=fake)
    assert summary.total == expected
    assert len(fake.calls) == expected
    assert summary.paused_hosts == {"gxt.ln.gov.cn": "HTTP 403"}


def test_stop_after_committed_result(session, monkeypatch):
    from youju.crawler import detail_service

    docs = [add_document(session, 9400 + i) for i in range(2)]
    saved = []
    original = detail_service._save_result

    def save(session, outcome):
        result = original(session, outcome)
        saved.append(outcome.document_id)
        return result

    monkeypatch.setattr(detail_service, "_save_result", save)
    # 第二个请求等待首个提交，证明写入发生在整批结束之前。
    import threading

    written = threading.Event()

    def save_signal(session, outcome):
        result = save(session, outcome)
        written.set()
        return result

    monkeypatch.setattr(detail_service, "_save_result", save_signal)

    waited = []

    class WaitingFetcher(FakeFetcher):
        def fetch(self, url):
            if url == docs[1].url:
                # 在取页线程里断言会被当成处理异常吞掉：记下来，运行完再断言
                waited.append(written.wait(5))
            return super().fetch(url)

    fake = WaitingFetcher({d.url: response(d) for d in docs})
    # 同一个网站的两份按先后取：第二份取之前，第一份已经写进库（不是整批结束才写）
    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake)
    assert waited == [True]
    assert "processing_error" not in summary.statuses
    assert session.get(DocumentDetail, docs[1].id) is not None


def test_stop_signal_after_first_write(session):
    docs = [add_document(session, 9450 + i) for i in range(2)]
    fake = FakeFetcher({d.url: response(d) for d in docs})

    def stop():
        return "每轮要锁" if fake.calls else None

    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake, stop=stop)
    assert summary.stop_reason == "每轮要锁"
    assert fake.calls == [docs[0].url]
    # 没写库的那份是停下时还没开始取的，不算网站停取（第十六轮 Codex）
    assert summary.statuses.get("not_started") == 1 and "site_paused" not in summary.statuses
    assert session.get(DocumentDetail, docs[0].id) is not None
    assert session.get(DocumentDetail, docs[1].id) is None


def test_document_cut_off_before_its_pdf_counts_as_interrupted(session):
    # 详情页取到了、正要取它唯一的 PDF 时别的任务开始了（第十七轮 Codex）：这份是开始了又被打断，
    # 记 interrupted（为什么停看 stop_reason），不算还没开始，也不算网站停取
    doc = add_document(session, 9470)
    page = FetchResult(
        doc.url,
        "ok",
        http_status=200,
        text="<div id='zoom'><a href='https://gxt.ln.gov.cn/f/1.pdf'>附件.pdf</a></div>",
    )
    fake = FakeFetcher({doc.url: page})

    def stop():
        return "别的任务在跑：backup" if fake.calls else None

    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake, stop=stop)
    assert fake.calls == [doc.url]
    assert summary.stop_reason == "别的任务在跑：backup"
    assert summary.statuses == {"interrupted": 1}
    assert session.get(DocumentDetail, doc.id) is None


def test_document_whose_pdf_host_is_paused_for_this_run_counts_as_site_paused(session, monkeypatch):
    # 详情页取到了、唯一的 PDF 所在网站这次运行停着（连续 3 份 405 这类，只停这次运行）：这份被停取
    # 挡下，汇总记 site_paused，不算"开始了又被打断"（第十八轮独立 Claude）
    from youju.crawler import detail_service

    class Paused(detail_service._SitePause):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.add("files.example.gov.cn", "连续 3 份 HTTP 405", persistent=False)

    monkeypatch.setattr(detail_service, "_SitePause", Paused)
    doc = add_document(session, 9471)
    pdf = "https://files.example.gov.cn/f/1.pdf"
    page = FetchResult(
        doc.url, "ok", http_status=200, text=f"<div id='zoom'><a href='{pdf}'>附件.pdf</a></div>"
    )
    fake = FakeFetcher({doc.url: page})
    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake)
    assert fake.calls == [doc.url]
    assert summary.stop_reason == "没有了"
    assert summary.statuses == {"site_paused": 1}
    assert session.get(DocumentDetail, doc.id) is None


@pytest.mark.parametrize("kind", ["captcha-words", "odd-4xx"])
def test_streaks_count_across_batches(session, kind):
    # 每批每个网站只取一份（per_host=1）时，"连续 3 份"也跨批数（第十九轮 Codex）：第 3 份以后这次
    # 运行先停这个网站（不写进共享文件），第 4 份不再取
    from urllib.parse import urlsplit

    from tests.test_detail_pause_unit import _SECURITY_NOTICE

    docs = [add_document(session, 9480 + i) for i in range(4)]

    def answer(doc):
        if kind == "odd-4xx":
            return FetchResult(doc.url, "http_error", http_status=405)
        return FetchResult(doc.url, "ok", http_status=200, text=_SECURITY_NOTICE)

    fake = FakeFetcher({d.url: answer(d) for d in docs})
    summary = run_detail_queue(session, minutes=1, workers=1, per_host=1, fetcher=fake)
    assert len(fake.calls) == 3
    host = urlsplit(docs[0].url).hostname
    assert host in summary.paused_hosts
    assert _SitePause(get_settings().detail_pause_path).expiry(host) is None


def test_time_stop_without_database():
    ticks = iter([0, 60])
    summary = run_detail_queue(None, minutes=1, fetcher=object(), clock=lambda: next(ticks))
    assert (summary.stop_reason, summary.total) == ("到时间", 0)


@pytest.mark.parametrize("running,wanted", [(True, False), (False, True)])
def test_command_skips_round(monkeypatch, capsys, running, wanted):
    monkeypatch.setattr(pipeline, "round_running", lambda: running)
    monkeypatch.setattr(pipeline, "round_wanted", lambda: wanted)
    assert cli._cmd_fetch_details_queue(SimpleNamespace()) == 0
    assert "跳过：每轮抓取在跑或在等锁" in capsys.readouterr().out


def test_pdf_429_pauses_pdf_host_only(session):
    from tests.test_detail_service import _doc_on, _source_on

    source = _source_on(session, "a.example.gov.cn")
    first = _doc_on(session, source, "a.example.gov.cn", 1)
    later = _doc_on(session, source, "a.example.gov.cn", 2)
    other_source = _source_on(session, "b.example.gov.cn")
    other = _doc_on(session, other_source, "b.example.gov.cn", 3)
    pdf = "https://b.example.gov.cn/a.pdf"
    fake = FakeFetcher(
        {
            first.url: FetchResult(
                first.url, "ok", http_status=200, text="<a href='" + pdf + "'>附件.pdf</a>"
            ),
            pdf: FetchResult(pdf, "http_error", http_status=429),
            later.url: response(later),
            other.url: response(other),
        }
    )
    summary = run_detail_queue(session, minutes=1, fetcher=fake, workers=1)
    assert summary.paused_hosts == {"b.example.gov.cn": "HTTP 429"}
    assert session.get(DocumentDetail, first.id).next_retry_at is not None
    assert session.get(DocumentDetail, later.id) is not None
    assert session.get(DocumentDetail, other.id) is None


def test_fetch_details_reports_pause(session):
    docs = [add_document(session, 9500 + i) for i in range(2)]
    fake = FakeFetcher({docs[0].url: response(docs[0], 429), docs[1].url: response(docs[1])})
    result = fetch_details(session, limit=2, fetcher=fake)
    assert result.paused_hosts == {"gxt.ln.gov.cn": "HTTP 429"}
    assert [r.status for r in result] == ["http_error", "site_paused"]


def test_queue_skips_a_whole_paused_page(session):
    from tests.test_detail_service import _doc_on, _source_on
    from youju.config import get_settings
    from youju.crawler.detail_service import _SitePause

    a = _source_on(session, "a.example.gov.cn")
    b = _source_on(session, "b.example.gov.cn")
    blocked = [_doc_on(session, a, "a.example.gov.cn", i) for i in range(10)]
    available = _doc_on(session, b, "b.example.gov.cn", 1)
    _SitePause(get_settings().detail_pause_path).add("a.example.gov.cn", "HTTP 403")
    fake = FakeFetcher({available.url: response(available)})
    result = run_detail_queue(session, minutes=1, batch=1, workers=1, fetcher=fake)
    assert result.total == 1
    assert session.get(DocumentDetail, available.id) is not None
    assert all(session.get(DocumentDetail, doc.id) is None for doc in blocked)


@pytest.mark.parametrize("code", [401, 403, 412, 521])
def test_refusal_is_saved_once_and_not_retried(session, code):
    docs = [add_document(session, 9700 + i) for i in range(2)]
    fake = FakeFetcher({d.url: response(d, code) for d in docs})
    first = fetch_details(session, limit=2, fetcher=fake, max_workers=1)
    assert [r.status for r in first] == ["http_error", "site_paused"]
    assert session.get(DocumentDetail, docs[0].id).next_retry_at is None
    assert session.get(DocumentDetail, docs[1].id) is None
    # 下一轮：停取中的网站选取时就跳过，不再去取
    second = fetch_details(session, limit=2, fetcher=fake, max_workers=1)
    assert [r.status for r in second] == []
    assert fake.calls == [docs[0].url]


@pytest.mark.parametrize(
    "hosts,interrupt,expected", [(5, False, True), (4, False, False), (5, True, False)]
)
def test_network_circuit_breaker(session, hosts, interrupt, expected):
    from tests.test_detail_service import _doc_on, _source_on

    sources = [_source_on(session, f"q{i}.example.gov.cn") for i in range(hosts)]
    # 单线程按主机顺序完成，每个主机有四份。
    docs = [
        _doc_on(session, source, f"q{i}.example.gov.cn", j)
        for i, source in enumerate(sources)
        for j in range(4)
    ]
    if hosts == 4:
        docs += [_doc_on(session, sources[0], "q0.example.gov.cn", j + 10) for j in range(4)]
    replies = {d.url: FetchResult(d.url, "network_error") for d in docs}
    if interrupt:
        replies[docs[len(docs) // 2].url] = response(docs[len(docs) // 2])
    summary = run_detail_queue(
        session, minutes=1, workers=1, batch=100, per_host=30, fetcher=FakeFetcher(replies)
    )
    assert ("熔断" in summary.stop_reason) is expected
    if expected:
        assert all(session.get(DocumentDetail, d.id) is None for d in docs)
    else:
        assert all(session.get(DocumentDetail, d.id) is not None for d in docs)


def test_queue_robots_error_saved_and_host_paused_across_runs(session):
    from tests.test_detail_service import _doc_on, _source_on

    a = _source_on(session, "ra.example.gov.cn")
    b = _source_on(session, "rb.example.gov.cn")
    first = _doc_on(session, a, "ra.example.gov.cn", 1)
    later = _doc_on(session, a, "ra.example.gov.cn", 2)
    other = _doc_on(session, b, "rb.example.gov.cn", 3)
    unreachable = FetchResult(first.url, "robots_error", robots_failure="network_error")
    fake = FakeFetcher({first.url: unreachable, other.url: response(other)})
    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake)
    # 连不上、超时：照常写库（带重试次数，过几小时再来），主机停 1 小时、写进共享文件
    saved = session.get(DocumentDetail, first.id)
    assert saved.status == "robots_error" and saved.next_retry_at is not None
    assert session.get(DocumentDetail, later.id) is None
    assert session.get(DocumentDetail, other.id) is not None
    assert summary.paused_hosts == {"ra.example.gov.cn": "robots.txt 读不到（连不上、超时）"}
    pause = _SitePause(get_settings().detail_pause_path)
    left = pause.expiry("ra.example.gov.cn") - datetime.now(UTC)
    assert timedelta(minutes=50) < left <= timedelta(hours=1)
    # 下一次运行：选取时就跳过这个主机，不会每次都把它选回来
    again = FakeFetcher({})
    run_detail_queue(session, minutes=1, workers=1, fetcher=again)
    assert again.calls == []


def test_certificate_errors_do_not_trip_circuit(session):
    from tests.test_detail_service import _doc_on, _source_on

    good_source = _source_on(session, "good.example.gov.cn")
    good = _doc_on(session, good_source, "good.example.gov.cn", 99)
    bad = [
        _doc_on(session, _source_on(session, f"c{i}.example.gov.cn"), f"c{i}.example.gov.cn", i)
        for i in range(25)
    ]
    replies = {d.url: FetchResult(d.url, "robots_error", robots_failure="ssl_error") for d in bad}
    replies[good.url] = readable(good)
    summary = run_detail_queue(
        session, minutes=1, workers=1, per_host=30, fetcher=FakeFetcher(replies)
    )
    # 证书、加密连接出错是对方网站的问题：不算网络失败，不熔断；照常写库，主机停 6 小时
    assert "熔断" not in summary.stop_reason
    assert session.get(DocumentDetail, good.id).status == "ok"
    assert all(session.get(DocumentDetail, d.id).status == "robots_error" for d in bad)
    assert summary.paused_hosts["c0.example.gov.cn"] == "robots.txt 读不到（证书或加密连接出错）"
    pause = _SitePause(get_settings().detail_pause_path)
    assert pause.expiry("c0.example.gov.cn") - datetime.now(UTC) > timedelta(hours=5)


def test_circuit_trip_pauses_hosts_and_exits_with_failure(session, monkeypatch):
    from tests.test_detail_service import _doc_on, _source_on
    from youju import cli
    from youju.crawler import detail_service

    bad = [
        _doc_on(session, _source_on(session, f"n{i}.example.gov.cn"), f"n{i}.example.gov.cn", i)
        for i in range(20)
    ]
    replies = {
        d.url: FetchResult(d.url, "robots_error", robots_failure="network_error") for d in bad
    }
    summary = run_detail_queue(
        session, minutes=1, workers=1, per_host=30, fetcher=FakeFetcher(replies)
    )
    assert summary.stop_reason.startswith(detail_service.CIRCUIT_STOP)
    assert all(session.get(DocumentDetail, d.id) is None for d in bad)
    # 这些主机停了 1 小时（写进共享文件）：真是一批网站坏了，下次运行也不会卡在它们身上
    again = FakeFetcher({})
    run_detail_queue(session, minutes=1, workers=1, fetcher=again)
    assert again.calls == []
    # 熔断按失败退出，job.sh 发提醒
    monkeypatch.setattr(pipeline, "round_wanted", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "round_running", lambda *a, **k: False)
    monkeypatch.setattr(
        detail_service,
        "run_detail_queue",
        lambda session, **kwargs: detail_service.DetailQueueSummary(
            {}, 0, {}, summary.stop_reason, 0
        ),
    )
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: _NoClose(None))
    args = SimpleNamespace(minutes=1, workers=1, batch=10, per_host=10)
    assert cli._cmd_fetch_details_queue(args) == 1


@pytest.mark.parametrize("robots_status,page_status", [(403, 200), (403, 403), (429, 200)])
def test_robots_http_status_controls_detail_writes(session, robots_status, page_status):
    import httpx

    from youju.crawler.fetcher import PoliteFetcher

    docs = [add_document(session, 9750 + i) for i in range(2)]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status)
        if page_status != 200:
            return httpx.Response(page_status)
        return httpx.Response(
            200,
            text=(
                "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。</p>"
                "<p>辽宁省工业和信息化厅</p></div></body></html>"
            ),
            headers={"content-type": "text/html"},
        )

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        results = fetch_details(session, limit=2, fetcher=fetcher, max_workers=1)
    if (robots_status, page_status) == (403, 200):
        # robots.txt 回 403 照 RFC 9309 当没有 robots：页面照常读、写库
        assert [r.status for r in results] == ["ok", "ok"]
        assert all(session.get(DocumentDetail, doc.id) is not None for doc in docs)
        assert len(calls) == 3
        assert results.paused_hosts == {}
    elif robots_status == 403:
        # 页面也回 403 才算被拒绝：第一份记下、网站停取（写进共享的停取文件），第二份不再去
        assert [r.status for r in results] == ["http_error", "site_paused"]
        assert session.get(DocumentDetail, docs[1].id) is None
        assert calls == ["/robots.txt", calls[1]]
        assert results.paused_hosts == {"gxt.ln.gov.cn": "HTTP 403"}
        assert _SitePause(get_settings().detail_pause_path).reasons == {"gxt.ln.gov.cn": "HTTP 403"}
    else:
        # robots.txt 回 429：不取页面；第一份照常写库（带重试次数），网站停 6 小时
        assert [r.status for r in results] == ["robots_error", "site_paused"]
        assert session.get(DocumentDetail, docs[0].id).next_retry_at is not None
        assert session.get(DocumentDetail, docs[1].id) is None
        assert calls == ["/robots.txt"]
        assert results.paused_hosts == {"gxt.ln.gov.cn": "robots.txt HTTP 429"}


@pytest.mark.parametrize("task", cli.DETAIL_QUEUE_YIELD_JOBS)
def test_command_skips_other_job(monkeypatch, capsys, task):
    monkeypatch.setattr(pipeline, "round_wanted", lambda: False)
    monkeypatch.setattr(
        pipeline, "round_running", lambda path=None: path == f"/tmp/youju-{task}.lock"
    )
    assert cli._cmd_fetch_details_queue(SimpleNamespace()) == 0
    assert f"别的任务在跑：{task}" in capsys.readouterr().out


@pytest.mark.parametrize("task", [None, *cli.DETAIL_QUEUE_YIELD_JOBS])
def test_running_job_stops_queue_before_next_document(session, monkeypatch, task):
    docs = [add_document(session, 9800 + i) for i in range(2)]
    active = False

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch(self, url):
            nonlocal active
            active = True
            return response(docs[0])

    monkeypatch.setattr(pipeline, "round_wanted", lambda: False)
    monkeypatch.setattr(
        pipeline,
        "round_running",
        lambda path=None: (
            active
            and (
                (task is None and path is None)
                or (task is not None and path == f"/tmp/youju-{task}.lock")
            )
        ),
    )
    from youju.crawler import detail_service

    monkeypatch.setattr(detail_service, "_polite_fetcher", lambda **kwargs: Fake())
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: _NoClose(session))
    # 让路的判断结果用 2 秒：这里每问一次就过 3 秒，每次都重新查
    ticks = iter(range(0, 10_000, 3))
    monkeypatch.setattr(cli.time, "monotonic", lambda: float(next(ticks)))
    assert (
        cli._cmd_fetch_details_queue(SimpleNamespace(minutes=1, workers=1, batch=10, per_host=10))
        == 0
    )
    assert session.get(DocumentDetail, docs[0].id) is not None
    assert session.get(DocumentDetail, docs[1].id) is None


class _NoClose:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *args):
        pass


def readable(doc):
    """能解析出正文的详情页（写库的状态是 ok）。"""
    return FetchResult(
        doc.url,
        "ok",
        http_status=200,
        text=(
            "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。</p>"
            "<p>某市工业和信息化局</p></div></body></html>"
        ),
        content_type="text/html",
    )


class _TimedFetcher(FakeFetcher):
    """网络失败的每份要 cost 秒（假时钟）。"""

    def __init__(self, responses, now, cost):
        super().__init__(responses)
        self.now, self.cost = now, cost

    def fetch(self, url):
        result = super().fetch(url)
        if result.status == "network_error":
            self.now[0] += self.cost
        return result


@pytest.mark.parametrize("slow", [True, False])
def test_network_circuit_time_window(session, slow):
    from tests.test_detail_service import _doc_on, _source_on

    good = [
        _doc_on(session, _source_on(session, f"g{i}.example.gov.cn"), f"g{i}.example.gov.cn", 1)
        for i in range(60)
    ]
    failing = 6 if slow else 5
    bad = []
    for i in range(failing):
        host = f"f{i}.example.gov.cn"
        source = _source_on(session, host)
        bad += [_doc_on(session, source, host, j) for j in range(4)]
    replies = {d.url: readable(d) for d in good}
    replies.update({d.url: FetchResult(d.url, "network_error") for d in bad})
    now = [0.0]
    fetcher = _TimedFetcher(replies, now, 40.0 if slow else 0.5)
    summary = run_detail_queue(
        session, minutes=600, workers=1, per_host=30, fetcher=fetcher, clock=lambda: now[0]
    )
    if slow:
        # 只剩几个超时很慢的坏站：连续 20 份失败跨了 60 秒以上，不是我们自己断网，照常写库、记次数
        assert "熔断" not in summary.stop_reason
        assert summary.statuses == {"ok": 60, "network_error": 24}
        assert all(session.get(DocumentDetail, d.id) is not None for d in bad)
    else:
        # 20 份失败在 10 秒内、来自 5 个主机：熔断，这些失败一份都不写
        assert "熔断" in summary.stop_reason
        assert summary.statuses == {"ok": 60, "circuit_dropped": 20}
        assert all(session.get(DocumentDetail, d.id) is None for d in bad)


def _bulk_docs(session, source_id, host, count):
    from datetime import datetime

    from youju.models import Document

    now = datetime.now(UTC)
    docs = [
        Document(
            source_id=source_id,
            url=f"https://{host}/detail/{n}.html",
            url_key=f"{host}/detail/{n}.html",
            title=f"关于第{n}项公开事项的通知",
            first_seen_at=now,
            last_seen_at=now,
            is_baseline=False,
            status="discovered",
        )
        for n in range(count)
    ]
    session.add_all(docs)
    session.commit()
    return docs


def test_queue_scans_past_fifty_pages(session):
    from tests.test_detail_service import _doc_on, _source_on

    a = _source_on(session, "a.example.gov.cn")
    b = _source_on(session, "b.example.gov.cn")
    # 一批选 1 份、一页 10 行：前 51 页全是停取网站的文件，后面那一份照样选得上
    blocked = _bulk_docs(session, a, "a.example.gov.cn", 510)
    available = _doc_on(session, b, "b.example.gov.cn", 1)
    _SitePause(get_settings().detail_pause_path).add("a.example.gov.cn", "HTTP 403")
    fake = FakeFetcher({available.url: readable(available)})
    summary = run_detail_queue(session, minutes=1, batch=1, workers=1, fetcher=fake)
    assert summary.statuses == {"ok": 1}
    assert fake.calls == [available.url]
    assert session.get(DocumentDetail, blocked[-1].id) is None


def test_queue_summary_counts_saved_and_paused(session):
    from tests.test_detail_service import _doc_on, _source_on

    a = _source_on(session, "a.example.gov.cn")
    b = _source_on(session, "b.example.gov.cn")
    refused = _doc_on(session, a, "a.example.gov.cn", 1)
    behind = _doc_on(session, a, "a.example.gov.cn", 2)
    fine = _doc_on(session, b, "b.example.gov.cn", 3)
    fake = FakeFetcher({refused.url: response(refused, 403), fine.url: readable(fine)})
    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake)
    # 写了库的照实际状态；被拒绝以后同一网站没去取的那份没写库，算 site_paused（只算一次）
    assert summary.statuses == {"http_error": 1, "ok": 1, "site_paused": 1}
    assert summary.total == 2  # 写入的份数
    assert session.get(DocumentDetail, behind.id) is None
    assert behind.url not in fake.calls


def test_queue_skips_deferred_documents(session):
    from datetime import datetime

    from tests.test_detail_service import _doc_on, _source_on

    a = _source_on(session, "a.example.gov.cn")
    deferred = _doc_on(session, a, "a.example.gov.cn", 1)
    other = _doc_on(session, a, "a.example.gov.cn", 2)
    _SitePause(get_settings().detail_pause_path).add(
        f"doc:{deferred.id}",
        "PDF 在停取中的 files.example.gov.cn 上",
        until=datetime.now(UTC) + timedelta(hours=1),
    )
    fake = FakeFetcher({other.url: readable(other)})
    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake)
    assert fake.calls == [other.url]
    assert summary.statuses == {"ok": 1}
    assert not any(h.startswith("doc:") for h in summary.paused_hosts)


def test_queue_command_checks_other_jobs_sparingly(monkeypatch, capsys):
    from youju import cli
    from youju.ai import pipeline
    from youju.crawler import detail_service

    probes = []
    monkeypatch.setattr(pipeline, "round_wanted", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "round_running", lambda *a, **k: probes.append(a) or False)
    now = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])

    def fake_run(session, *, stop, **kwargs):
        for _ in range(50):
            stop()  # 每次请求前都会问一次
        now[0] += 2.5
        stop()
        return detail_service.DetailQueueSummary({}, 0, {}, "没有了", 0)

    monkeypatch.setattr(detail_service, "run_detail_queue", fake_run)
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: _NoClose(None))
    args = SimpleNamespace(minutes=1, workers=1, batch=10, per_host=10)
    assert cli._cmd_fetch_details_queue(args) == 0
    # 一开始查一次（6 把锁），2 秒以内都用这个结果，过了 2 秒再查一次
    assert len(probes) == 12


def test_deadline_results_do_not_trip_circuit(session):
    from tests.test_detail_service import _doc_on, _source_on

    docs = []
    for i in range(6):
        host = f"d{i}.example.gov.cn"
        source = _source_on(session, host)
        docs += [_doc_on(session, source, host, j) for j in range(4)]
    replies = {d.url: FetchResult(d.url, "deadline", error="到了期限") for d in docs}
    summary = run_detail_queue(
        session, minutes=1, workers=1, per_host=30, fetcher=FakeFetcher(replies)
    )
    # 到了期限没取完的不算网络失败：不熔断，也不写库
    assert "熔断" not in summary.stop_reason
    assert summary.statuses == {"interrupted": 24}
    assert all(session.get(DocumentDetail, d.id) is None for d in docs)


def test_round_fetch_skips_paused_hosts_when_selecting(session):
    from tests.test_detail_service import _doc_on, _source_on

    a = _source_on(session, "a.example.gov.cn")
    b = _source_on(session, "b.example.gov.cn")
    blocked = [_doc_on(session, a, "a.example.gov.cn", i) for i in range(3)]
    fresh = _doc_on(session, b, "b.example.gov.cn", 9)
    _SitePause(get_settings().detail_pause_path).add("a.example.gov.cn", "HTTP 403")
    fake = FakeFetcher({fresh.url: readable(fresh)})
    results = fetch_details(session, limit=1, fetcher=fake, max_workers=1)
    # 排在前面的都是停取网站的：选取时就跳过，名额给后面的文件
    assert [r.document_id for r in results] == [fresh.id]
    assert fake.calls == [fresh.url]
    assert all(session.get(DocumentDetail, d.id) is None for d in blocked)


def test_queue_stops_when_shared_limits_break(session, monkeypatch):
    from youju.crawler import detail_service
    from youju.crawler.fetcher import SharedLimitError

    docs = [add_document(session, 9600 + i) for i in range(2)]

    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch(self, url):
            raise SharedLimitError("磁盘满了")

    monkeypatch.setattr(detail_service, "_polite_fetcher", lambda **kwargs: Broken())
    summary = run_detail_queue(session, minutes=1, workers=1)
    assert summary.stop_reason.startswith(detail_service.SHARED_LIMIT_STOP)
    assert all(session.get(DocumentDetail, d.id) is None for d in docs)
