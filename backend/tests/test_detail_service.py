from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import select

from youju.crawler.detail_service import fetch_details
from youju.crawler.fetcher import FetchResult
from youju.models import Document, DocumentDetail, Source


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response


def add_document(session, number, *, source_id=None, rights_level=1):
    if source_id is None:
        source = Source(
            level="省级",
            province="辽宁",
            dept="工信",
            dept_full_name="辽宁省工业和信息化厅",
            column_name="通知公告",
            url=f"https://gxt.ln.gov.cn/list/{number}",
            fetch_mode="static",
            enabled=True,
            parser_config={},
            rights_level=rights_level,
        )
        session.add(source)
        session.flush()
        source_id = source.id
    now = datetime.now(UTC)
    doc = Document(
        source_id=source_id,
        url=f"https://gxt.ln.gov.cn/detail/{number}.html",
        url_key=f"gxt.ln.gov.cn/detail/{number}.html",
        title=f"关于第{number}项公开事项的通知",
        first_seen_at=now,
        last_seen_at=now,
        is_baseline=False,
        status="discovered",
    )
    session.add(doc)
    session.commit()
    return doc


def test_fetch_details_records_success_and_failures_once(session) -> None:
    ok = add_document(session, 1)
    blocked = add_document(session, 2, source_id=ok.source_id)
    protected = add_document(session, 3, source_id=ok.source_id)
    sample = (
        "<html><meta name='PubDate' content='2026-09-24'><body><div id='zoom'>"
        "<p>辽工信〔2026〕1号</p><p>这是公开通知正文，需要保存这些段落内容。</p>"
        "<p>辽宁省工业和信息化厅</p><p>2026年9月24日</p>"
        "<a href='/a.pdf'>附件1.pdf</a></div></body></html>"
    )
    fake = FakeFetcher(
        {
            ok.url: FetchResult(ok.url, "ok", final_url=ok.url, http_status=200, text=sample),
            blocked.url: FetchResult(blocked.url, "robots_disallowed", error="robots 禁止"),
            protected.url: FetchResult(
                protected.url,
                "ok",
                final_url=protected.url,
                http_status=200,
                text="<html><title>安全验证</title><body>请输入验证码</body></html>",
            ),
        }
    )
    results = fetch_details(session, limit=3, source_id=ok.source_id, fetcher=fake)
    assert [r.status for r in results] == ["ok", "robots_disallowed", "protected"]
    details = {d.document_id: d for d in session.scalars(select(DocumentDetail)).all()}
    assert details[ok.id].body_text.startswith("辽工信〔2026〕1号")
    assert details[ok.id].http_status == 200
    assert details[ok.id].attachments[0].url == "https://gxt.ln.gov.cn/a.pdf"
    assert details[blocked.id].failure_reason == "robots 禁止"
    assert details[protected.id].failure_reason == "防护页或验证码"
    assert session.get(Document, ok.id).status == "fetched"
    assert fetch_details(session, limit=3, source_id=ok.source_id, fetcher=fake) == []
    assert len(fake.calls) == 3


def test_fetch_details_limit_source_rights_and_unsupported_url(session) -> None:
    first = add_document(session, 1)
    second = add_document(session, 2, source_id=first.source_id)
    add_document(session, 3, rights_level=2)
    fake = FakeFetcher(
        {
            first.url: FetchResult(first.url, "network_error", error="timeout"),
            second.url: FetchResult(second.url, "network_error", error="timeout"),
        }
    )
    assert [r.document_id for r in fetch_details(session, limit=1, fetcher=fake)] == [first.id]
    assert [
        r.document_id
        for r in fetch_details(session, limit=5, source_id=first.source_id, fetcher=fake)
    ] == [second.id]
    assert len(fake.calls) == 2


def test_temporary_failures_retry_after_6_and_24_hours(session) -> None:
    doc = add_document(session, 4)
    sample = (
        "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。"
        "申报单位应当按照规定提交材料，主管部门将依法审核并公告结果。</p></div></body></html>"
    )
    fake = FakeFetcher(
        {
            doc.url: [
                FetchResult(doc.url, "network_error", error="timeout"),
                FetchResult(doc.url, "http_error", http_status=503, error="HTTP 503"),
                FetchResult(doc.url, "ok", http_status=200, text=sample),
            ]
        }
    )
    assert fetch_details(session, limit=1, fetcher=fake)[0].status == "network_error"
    detail = session.get(DocumentDetail, doc.id)
    assert detail.attempt_count == 1
    assert detail.next_retry_at == detail.fetched_at + timedelta(hours=6)
    assert fetch_details(session, limit=1, fetcher=fake) == []
    detail.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert fetch_details(session, limit=1, fetcher=fake)[0].status == "http_error"
    assert detail.attempt_count == 2
    assert detail.http_status == 503
    assert detail.next_retry_at == detail.fetched_at + timedelta(hours=24)
    detail.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert fetch_details(session, limit=1, fetcher=fake)[0].status == "ok"
    assert detail.attempt_count == 3
    assert detail.next_retry_at is None
    assert fetch_details(session, limit=1, fetcher=fake) == []
    assert len(fake.calls) == 3


def test_third_temporary_failure_stops_retrying(session) -> None:
    doc = add_document(session, 5)
    fake = FakeFetcher({doc.url: FetchResult(doc.url, "http_error", http_status=500)})
    for attempt in range(1, 4):
        assert fetch_details(session, limit=1, fetcher=fake)[0].status == "http_error"
        detail = session.get(DocumentDetail, doc.id)
        assert detail.attempt_count == attempt
        if attempt < 3:
            detail.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    assert detail.next_retry_at is None
    assert fetch_details(session, limit=1, fetcher=fake) == []
    assert len(fake.calls) == 3


@pytest.mark.parametrize(
    "response",
    [
        FetchResult("", "robots_disallowed"),
        FetchResult("", "http_error", http_status=403),
        FetchResult("", "http_error", http_status=404),
        FetchResult("", "ok", http_status=200, content_type="application/pdf", text="%PDF-1.7"),
    ],
)
def test_permanent_failures_are_not_retried(session, response) -> None:
    doc = add_document(session, 6)
    fake = FakeFetcher({doc.url: response})
    fetch_details(session, limit=1, fetcher=fake)
    detail = session.get(DocumentDetail, doc.id)
    assert detail.attempt_count == 1
    assert detail.next_retry_at is None
    assert fetch_details(session, limit=1, fetcher=fake) == []
    assert len(fake.calls) == 1


def test_non_government_urls_and_redirects_are_permanent(session) -> None:
    external = add_document(session, 10)
    external.url = "https://example.com/detail/10.html"
    external.url_key = "example.com/detail/10.html"
    redirected = add_document(session, 11, source_id=external.source_id)
    session.commit()
    fake = FakeFetcher(
        {
            redirected.url: FetchResult(
                redirected.url,
                "ok",
                final_url="https://example.com/detail/11.html",
                http_status=200,
                text="<html><body>外站内容</body></html>",
            )
        }
    )
    assert [r.status for r in fetch_details(session, limit=2, fetcher=fake)] == [
        "unsupported_host",
        "unsupported_host",
    ]
    assert fake.calls == [redirected.url]
    assert fetch_details(session, limit=2, fetcher=fake) == []


def test_different_hosts_run_in_parallel_but_same_host_stays_serial(session) -> None:
    first = add_document(session, 7)
    second = add_document(session, 8, source_id=first.source_id)
    third = add_document(session, 9, source_id=first.source_id)
    third.url = "https://kjt.hubei.gov.cn/detail/9.html"
    third.url_key = "kjt.hubei.gov.cn/detail/9.html"
    session.commit()
    barrier = Barrier(2)

    class ConcurrentFetcher:
        def __init__(self):
            self.first_finished = False
            self.calls = []

        def fetch(self, url):
            if url in {first.url, third.url}:
                barrier.wait(timeout=5)
            if url == second.url:
                assert self.first_finished
            self.calls.append(url)
            if url == first.url:
                self.first_finished = True
            return FetchResult(url, "http_error", http_status=404)

    fake = ConcurrentFetcher()
    assert len(fetch_details(session, limit=3, fetcher=fake, max_workers=2)) == 3
    assert fake.calls.index(first.url) < fake.calls.index(second.url)


def test_pdf_detail_and_pdf_only_html_use_real_redacted_sample(session) -> None:
    pdf = (Path(__file__).parent / "fixtures" / "jiangsu_510.pdf").read_bytes()
    assert len(pdf) < 1_000_000
    direct = add_document(session, 100)
    direct.url = "https://kxjst.jiangsu.gov.cn/notice.pdf"
    direct.url_key = "kxjst.jiangsu.gov.cn/notice.pdf"
    html_doc = add_document(session, 101, source_id=direct.source_id)
    html_doc.url = "https://kxjst.jiangsu.gov.cn/notice.html"
    html_doc.url_key = "kxjst.jiangsu.gov.cn/notice.html"
    session.commit()
    attachment = "https://kxjst.jiangsu.gov.cn/attachment.pdf"
    fake = FakeFetcher(
        {
            direct.url: FetchResult(
                direct.url,
                "ok",
                final_url=direct.url,
                content_type="application/pdf",
                content=pdf,
                http_status=200,
            ),
            html_doc.url: FetchResult(
                html_doc.url,
                "ok",
                final_url=html_doc.url,
                content_type="text/html",
                http_status=200,
                text=f"<html><body><div id='zoom'><a href='{attachment}'>点击正文.pdf</a></div></body></html>",
            ),
            attachment: FetchResult(
                attachment,
                "ok",
                final_url=attachment,
                content_type="application/pdf",
                content=pdf,
                http_status=200,
            ),
        }
    )
    results = fetch_details(session, limit=2, fetcher=fake)
    assert [r.status for r in results] == ["ok", "ok"]
    assert len(session.get(DocumentDetail, direct.id).body_text) > 1000
    assert (
        session.get(DocumentDetail, html_doc.id).body_text
        == session.get(DocumentDetail, direct.id).body_text
    )
    assert len(session.get(DocumentDetail, html_doc.id).attachments) == 1
    assert fake.calls == [direct.url, html_doc.url, attachment]


def test_overlong_body_is_truncated_not_failed(session, monkeypatch) -> None:
    """正文列有长度上限（MEDIUMTEXT）：超长的截断保存，不让整份详情写入失败。"""
    monkeypatch.setattr("youju.crawler.detail_service.BODY_TEXT_MAX_CHARS", 20)
    doc = add_document(session, 7)
    sample = (
        "<html><body><div id='zoom'><p>这是一段很长的公开通知正文，用来检查截断是否生效。</p>"
        "<p>后面还有更多内容，都会被截掉。</p></div></body></html>"
    )
    fake = FakeFetcher(
        {doc.url: FetchResult(doc.url, "ok", final_url=doc.url, http_status=200, text=sample)}
    )
    assert [r.status for r in fetch_details(session, limit=1, fetcher=fake)] == ["ok"]
    detail = session.get(DocumentDetail, doc.id)
    assert len(detail.body_text) == 20
