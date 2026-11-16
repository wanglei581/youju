from datetime import UTC, date, timedelta
from types import SimpleNamespace

import pytest

from tests.test_detail_service import FakeFetcher, add_document
from youju import cli
from youju.ai import pipeline
from youju.crawler.detail_service import fetch_details, run_detail_queue
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

    class WaitingFetcher(FakeFetcher):
        def fetch(self, url):
            if url == docs[1].url:
                assert written.wait(5)
            return super().fetch(url)

    fake = WaitingFetcher({d.url: response(d) for d in docs})
    summary = run_detail_queue(session, minutes=1, fetcher=fake, stop=written.is_set)
    assert summary.stop_reason == "每轮要锁"
    assert session.get(DocumentDetail, docs[0].id) is not None


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
    second = fetch_details(session, limit=2, fetcher=fake, max_workers=1)
    assert [r.status for r in second] == ["site_paused"]
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


def test_queue_robots_error_not_saved(session):
    from tests.test_detail_service import _doc_on, _source_on

    a = _source_on(session, "ra.example.gov.cn")
    b = _source_on(session, "rb.example.gov.cn")
    first = _doc_on(session, a, "ra.example.gov.cn", 1)
    later = _doc_on(session, a, "ra.example.gov.cn", 2)
    other = _doc_on(session, b, "rb.example.gov.cn", 3)
    fake = FakeFetcher(
        {first.url: FetchResult(first.url, "robots_error"), other.url: response(other)}
    )
    summary = run_detail_queue(session, minutes=1, workers=1, fetcher=fake)
    assert session.get(DocumentDetail, first.id) is None
    assert session.get(DocumentDetail, later.id) is None
    assert session.get(DocumentDetail, other.id) is not None
    assert summary.paused_hosts == {"ra.example.gov.cn": "robots_error"}


@pytest.mark.parametrize("robots_status", [403, 429])
def test_robots_http_status_controls_detail_writes(session, robots_status):
    import httpx

    from youju.crawler.fetcher import PoliteFetcher

    docs = [add_document(session, 9750 + i) for i in range(2)]
    calls = []

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

    with PoliteFetcher(
        "test",
        per_host_interval=0,
        per_address_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
    ) as fetcher:
        results = fetch_details(session, limit=2, fetcher=fetcher, max_workers=1)
    if robots_status == 403:
        assert [r.status for r in results] == ["ok", "ok"]
        assert all(session.get(DocumentDetail, doc.id) is not None for doc in docs)
        assert len(calls) == 3
        assert results.paused_hosts == {}
    else:
        assert [r.status for r in results] == ["site_paused", "site_paused"]
        assert all(session.get(DocumentDetail, doc.id) is None for doc in docs)
        assert calls == ["/robots.txt"]
        assert results.paused_hosts == {"gxt.ln.gov.cn": "robots_error"}


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

    monkeypatch.setattr(detail_service, "_polite_fetcher", Fake)
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: _NoClose(session))
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
