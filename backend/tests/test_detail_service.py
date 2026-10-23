from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import select

from youju.categories import doc_category
from youju.classify import doc_kind
from youju.crawler.detail_service import fetch_details
from youju.crawler.fetcher import FetchResult
from youju.models import Document, DocumentDetail, DocumentMerge, Source
from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response


def add_document(session, number, *, source_id=None, rights_level=1, title=None):
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
        title=title or f"关于第{number}项公开事项的通知",
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


def test_fetch_details_normalizes_new_issuer_on_ingest(session) -> None:
    doc = add_document(session, 12)
    sample = (
        "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。"
        "申报单位应按规定提交材料。</p><p>安 徽 省 财 政 厅 福建省职称改革工作办公室</p>"
        "<p>2026年9月24日</p></div></body></html>"
    )
    fake = FakeFetcher(
        {doc.url: FetchResult(doc.url, "ok", final_url=doc.url, http_status=200, text=sample)}
    )
    assert fetch_details(session, limit=1, fetcher=fake)[0].status == "ok"
    assert session.get(DocumentDetail, doc.id).issuer == "安徽省财政厅、福建省职称改革工作办公室"


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


@pytest.mark.parametrize(
    "status,http_status",
    [
        ("network_error", None),
        ("http_error", 503),
        ("robots_disallowed", None),
        ("http_error", 404),
    ],
)
def test_failed_refetch_preserves_good_detail_and_bounds_automatic_retries(
    session, status, http_status
):
    from youju.crawler.detail_service import REFETCH_FAILURE_PREFIX
    from youju.lists.service import RESCAN_DETAILS_BEFORE, _details_to_rescan
    from youju.models import DocumentAttachment

    doc = add_document(session, 81)
    old_time = RESCAN_DETAILS_BEFORE - timedelta(days=1)
    detail = DocumentDetail(
        document_id=doc.id,
        status="ok",
        body_text="已有正式正文不可因补附件失败被清空。",
        issuer="辽宁省工业和信息化厅",
        doc_number="辽工信〔2026〕81号",
        fetched_at=old_time,
        http_status=200,
        final_url=doc.url,
        attempt_count=3,
        attachments=[
            DocumentAttachment(position=0, name="原附件", url=doc.url + ".xlsx", extension="xlsx")
        ],
    )
    session.add(detail)
    session.commit()
    fake = FakeFetcher({doc.url: FetchResult(doc.url, status, http_status=http_status)})
    assert _details_to_rescan(session, [doc.id]) == [doc.id]
    assert (
        fetch_details(session, limit=1, document_ids=[doc.id], refetch=True, fetcher=fake)[0].status
        == status
    )
    assert detail.status == "ok"
    assert detail.body_text == "已有正式正文不可因补附件失败被清空。"
    assert detail.issuer == "辽宁省工业和信息化厅"
    assert detail.doc_number == "辽工信〔2026〕81号"
    assert detail.fetched_at == old_time
    assert detail.http_status == 200 and detail.final_url == doc.url
    assert detail.attachments[0].url == doc.url + ".xlsx"
    assert detail.failure_reason == REFETCH_FAILURE_PREFIX + status
    assert detail.attempt_count == 1
    assert _details_to_rescan(session, [doc.id]) == []
    assert fetch_details(session, limit=1, fetcher=fake) == []
    transient = status == "network_error" or http_status == 503
    if transient:
        for attempt in (2, 3):
            detail.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
            assert _details_to_rescan(session, [doc.id]) == [doc.id]
            assert fetch_details(session, limit=1, fetcher=fake)[0].status == status
            assert detail.attempt_count == attempt
            assert detail.status == "ok" and detail.fetched_at == old_time
        assert len(fake.calls) == 3
    else:
        assert len(fake.calls) == 1
    assert detail.next_retry_at is None
    assert _details_to_rescan(session, [doc.id]) == []
    assert fetch_details(session, limit=1, fetcher=fake) == []


def test_refetch_success_updates_attachments_and_finishes_rescan(session):
    from youju.lists.service import RESCAN_DETAILS_BEFORE, _details_to_rescan

    doc = add_document(session, 82)
    detail = DocumentDetail(
        document_id=doc.id,
        status="ok",
        body_text="原正文",
        fetched_at=RESCAN_DETAILS_BEFORE - timedelta(days=1),
        attempt_count=1,
    )
    session.add(detail)
    session.commit()
    sample = """<div id='zoom'>这是重新取得的政府公开通知正文，申报单位应当按规定准备材料。<script>var a = '<a href="/new.xlsx">企业名单.xlsx</a>';</script></div>"""
    fake = FakeFetcher({doc.url: FetchResult(doc.url, "ok", http_status=200, text=sample)})
    fetch_details(session, limit=1, document_ids=[doc.id], refetch=True, fetcher=fake)
    assert detail.attachments[0].url == "https://gxt.ln.gov.cn/new.xlsx"
    assert detail.fetched_at >= RESCAN_DETAILS_BEFORE
    assert detail.failure_reason is None and detail.next_retry_at is None
    assert _details_to_rescan(session, [doc.id]) == []


def test_refetch_requires_explicit_ids_and_still_obeys_rights(session):
    doc = add_document(session, 83, rights_level=2)
    fake = FakeFetcher({})
    with pytest.raises(ValueError, match="document_ids"):
        fetch_details(session, limit=1, refetch=True, fetcher=fake)
    assert fetch_details(session, limit=1, document_ids=[doc.id], refetch=True, fetcher=fake) == []
    assert fetch_details(session, limit=1, document_ids=[], refetch=True, fetcher=fake) == []
    assert fake.calls == []


def test_one_refetch_exception_does_not_lose_other_results_or_expose_error(session, caplog):
    from youju.lists.service import RESCAN_DETAILS_BEFORE

    first = add_document(session, 91)
    second = add_document(session, 92, source_id=first.source_id)
    session.add(
        DocumentDetail(
            document_id=first.id,
            status="ok",
            body_text="此前成功正文",
            fetched_at=RESCAN_DETAILS_BEFORE - timedelta(days=1),
            attempt_count=1,
        )
    )
    session.commit()

    class Fetcher:
        def fetch(self, url):
            if url == first.url:
                raise RuntimeError("do-not-print-sensitive-marker")
            return FetchResult(
                url,
                "ok",
                http_status=200,
                text="<div id='zoom'>这份公开通知应当正常保存，单个文件处理失败不会影响其他文件。</div>",
            )

    results = fetch_details(
        session, limit=2, document_ids=[first.id, second.id], refetch=True, fetcher=Fetcher()
    )
    assert [r.status for r in results] == ["processing_error", "ok"]
    assert session.get(DocumentDetail, first.id).body_text == "此前成功正文"
    assert session.get(DocumentDetail, first.id).status == "ok"
    assert session.get(DocumentDetail, second.id).status == "ok"
    assert "do-not-print-sensitive-marker" not in caplog.text + repr(results)


def test_rescan_does_not_run_before_cutoff(session, monkeypatch):
    import youju.lists.service as lists

    doc = add_document(session, 93)
    session.add(
        DocumentDetail(
            document_id=doc.id,
            status="ok",
            body_text="原正文",
            fetched_at=lists.RESCAN_DETAILS_BEFORE - timedelta(days=1),
            attempt_count=1,
        )
    )
    session.commit()

    class Before(datetime):
        @classmethod
        def now(cls, tz=None):
            return lists.RESCAN_DETAILS_BEFORE - timedelta(seconds=1)

    monkeypatch.setattr(lists, "datetime", Before)
    assert lists._details_to_rescan(session, [doc.id]) == []


# 10/16 模拟器验收（详情 id=35765）：河南工信厅列表页把标题截成"……享受增..."，详情页的
# ArticleTitle 元数据和 h1 才是完整标题
HENAN_TRUNCATED = (
    "河南省工业和信息化厅 河南省科学技术厅 河南省财政厅 河南省税务局关于做好2026年度享受增..."
)
HENAN_FULL = (
    "河南省工业和信息化厅 河南省科学技术厅 河南省财政厅 河南省税务局关于做好2026年度"
    "享受增值税加计抵减政策先进制造业企业名单制定工作的通知"
)


def detail_page(
    title: str, *, article_title: str | None = None, site: str = "河南省工业和信息化厅"
):
    """仿河南工信厅详情页：<title> 带站名，ArticleTitle 元数据和 h1 是标题。"""
    meta = f"<meta name='ArticleTitle' content='{article_title or title}'>"
    return (
        f"<html><head><title>{title}-{site}</title>{meta}</head><body>"
        f"<h1 id='title'>{title}</h1><div id='zoom'><p>各省辖市工业和信息化主管部门：</p>"
        "<p>为做好享受增值税加计抵减政策的企业名单制定工作，现将有关事项通知如下。</p>"
        "</div></body></html>"
    )


def test_fetch_details_completes_truncated_list_title(session) -> None:
    truncated = add_document(session, 101, title=HENAN_TRUNCATED)
    assert truncated.rule_kind == doc_kind(HENAN_TRUNCATED) == "其他"
    # 对不上：详情页是另一份文件的标题；没截断：详情页标题再长也不改
    other = add_document(
        session,
        102,
        source_id=truncated.source_id,
        title="关于开展2026年度专精特新中小企业认定工作的...",
    )
    full = add_document(
        session, 103, source_id=truncated.source_id, title="关于开展2026年度绿色工厂申报工作的通知"
    )
    pages = {
        truncated.url: detail_page(HENAN_FULL),
        other.url: detail_page("关于公布2026年度专精特新中小企业名单的通知"),
        full.url: detail_page("关于开展2026年度绿色工厂申报工作的通知（第二批）"),
    }
    fake = FakeFetcher(
        {
            url: FetchResult(url, "ok", final_url=url, http_status=200, text=t)
            for url, t in pages.items()
        }
    )
    assert [r.status for r in fetch_details(session, limit=3, fetcher=fake)] == ["ok"] * 3
    doc = session.get(Document, truncated.id)
    assert doc.title == HENAN_FULL
    # 按标题算的类型、类别、筛选标签随完整标题重算（截断时是"其他"，进不了申报通知筛选）
    assert doc.rule_kind == doc_kind(HENAN_FULL) == "申报通知"
    assert (doc.rule_category, doc.rule_supports, doc.rule_industries) == (
        doc_category(HENAN_FULL),
        SUPPORT_RULES.stored(HENAN_FULL),
        INDUSTRY_RULES.stored(HENAN_FULL),
    )
    assert session.get(Document, other.id).title == "关于开展2026年度专精特新中小企业认定工作的..."
    assert session.get(Document, full.id).title == "关于开展2026年度绿色工厂申报工作的通知"
    assert session.get(DocumentDetail, truncated.id).body_text.startswith("各省辖市")

    # 再抓一次（解析规则改进后的重抓）：已是完整标题，不再变
    fetch_details(session, limit=1, document_ids=[truncated.id], refetch=True, fetcher=fake)
    assert session.get(Document, truncated.id).title == HENAN_FULL


def test_completed_title_joins_the_copy_that_already_had_the_full_title(session) -> None:
    """同一份文件在另一个栏目里是完整标题：截断时不参与归并，补全后按新标题归到一组。"""
    copy = add_document(session, 111, title=HENAN_FULL)
    truncated = add_document(session, 112, source_id=copy.source_id, title=HENAN_TRUNCATED)
    for doc in (copy, truncated):
        doc.publish_date = date(2026, 9, 26)
    session.commit()
    page = detail_page(HENAN_FULL)
    fake = FakeFetcher(
        {truncated.url: FetchResult(truncated.url, "ok", final_url=truncated.url, text=page)}
    )
    assert session.scalars(select(DocumentMerge)).all() == []
    fetch_details(session, limit=1, document_ids=[truncated.id], fetcher=fake)
    merge = session.get(DocumentMerge, truncated.id)
    assert merge is not None and merge.active and merge.canonical_id == copy.id


def test_interactive_platform_pages_are_never_requested(session) -> None:
    """互动交流平台（意见征集、留言）页面带验证码：不发请求，直接记为防护页，不再重试。"""
    doc = add_document(session, 31)
    doc.url = "http://kjj.gz.gov.cn/hdjlpt/yjzj/answer/53084"
    session.commit()
    fake = FakeFetcher({})
    results = fetch_details(session, limit=1, fetcher=fake)
    assert [(r.status, r.failure_reason) for r in results] == [
        ("protected", "互动交流平台页面带验证码，不抓")
    ]
    assert fake.calls == []
    detail = session.get(DocumentDetail, doc.id)
    assert (detail.status, detail.next_retry_at) == ("protected", None)
    assert fetch_details(session, limit=1, fetcher=fake) == []


def test_redirect_into_interactive_platform_is_not_requested() -> None:
    """普通链接重定向进互动交流平台：抓取器在那一跳之前就停，记为防护页。"""
    import httpx

    from youju.crawler import detail_service
    from youju.crawler.fetcher import PoliteFetcher

    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://kjj.gz.gov.cn/hdjlpt/yjzj/1"})

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        url_allowed=detail_service._detail_url_allowed,
        resolver=lambda host: ["1.2.3.4"],  # 测试不做真实 DNS 查询，当作公网地址
    ) as fetcher:
        outcome = detail_service._fetch_one(1, "https://kjj.gz.gov.cn/content/post_1.html", fetcher)
    assert (outcome.status, outcome.failure_reason) == (
        "protected",
        "互动交流平台页面带验证码，不抓",
    )
    assert "/hdjlpt/yjzj/1" not in requested
    assert detail_service._polite_fetcher()._url_allowed is detail_service._detail_url_allowed


def test_single_pdf_attachment_on_interactive_platform_is_not_requested(session) -> None:
    """正文为空、只有一个 PDF 附件，而附件在互动交流平台上：不请求，记为防护页。"""
    doc = add_document(session, 32)
    page = (
        "<html><body><div id='zoom'><a href='/hdjlpt/files/a.pdf'>附件1.pdf</a></div></body></html>"
    )
    fake = FakeFetcher(
        {doc.url: FetchResult(doc.url, "ok", final_url=doc.url, http_status=200, text=page)}
    )
    results = fetch_details(session, limit=1, fetcher=fake)
    assert [(r.status, r.failure_reason) for r in results] == [
        ("protected", "互动交流平台页面带验证码，不抓")
    ]
    assert fake.calls == [doc.url]
