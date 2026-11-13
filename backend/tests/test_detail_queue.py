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
    assert [doc_id for doc_id, _ in batches[0]] == [docs[0].id, other.id]
    assert (summary.batches, summary.total, summary.stop_reason) == (1, 2, "每轮要锁")


@pytest.mark.parametrize(
    "codes, expected", [([403, 403, 403, 200], 3), ([403, 200, 403, 403, 200], 5)]
)
def test_403_streak(session, codes, expected):
    docs = [add_document(session, 9300 + i) for i in range(len(codes))]
    fake = FakeFetcher({d.url: response(d, code) for d, code in zip(docs, codes, strict=True)})
    summary = run_detail_queue(session, minutes=1, fetcher=fake)
    assert summary.total == expected
    assert len(fake.calls) == expected
    assert bool(summary.paused_hosts) == (expected == 3)


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


def test_pdf_429_pauses_original_host_only(session):
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
    assert summary.paused_hosts == {"a.example.gov.cn": "HTTP 429"}
    assert session.get(DocumentDetail, first.id).next_retry_at is not None
    assert session.get(DocumentDetail, later.id) is None
    assert session.get(DocumentDetail, other.id) is not None


def test_fetch_details_reports_pause(session):
    docs = [add_document(session, 9500 + i) for i in range(2)]
    fake = FakeFetcher({docs[0].url: response(docs[0], 429), docs[1].url: response(docs[1])})
    result = fetch_details(session, limit=2, fetcher=fake)
    assert result.paused_hosts == {"gxt.ln.gov.cn": "HTTP 429"}
    assert [r.status for r in result] == ["http_error", "site_paused"]
