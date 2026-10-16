"""一次性重抓名单详情补附件（10/16，河南附件文件服务器）。"""

from datetime import UTC, date, datetime

from youju import cli
from youju.crawler import detail_service
from youju.crawler.fetcher import FetchResult
from youju.lists.rescan import file_server_suffixes, rescan_list_details
from youju.models import Document, DocumentAttachment, DocumentDetail, ListEntry, Source

LIST_URL = "http://kjt.henan.gov.cn/2026/09-02/3410350.html"
PDF_NAME = "河南省2026年第一批入库科技型中小企业名单"
PDF_URL = "https://xcoss.henan.gov.cn/typtfile/20260902/6f6b.pdf"
OLD_URL = "http://kjt.henan.gov.cn/2026/09-02/old.pdf"
PAGE = (
    "<html><head><title>河南省2026年第一批入库科技型中小企业名单公告</title></head><body>"
    "<div id='zoom'><p>现将通过公示的河南省2026年第一批入库企业名单（详见附件）予以公告。</p>"
    f"<p>附件：<a href='{PDF_URL}'>{PDF_NAME}</a></p></div></body></html>"
)
FETCHED_AT = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.responses[url]

    def close(self):
        pass


def _source(session, dept="科技", url="http://kjt.henan.gov.cn/tzgg/") -> Source:
    source = Source(
        level="省级",
        province="河南",
        dept=dept,
        dept_full_name=f"河南省{dept}厅",
        column_name="通知公告",
        url=url,
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    return source


def _document(
    session, source, url, title, *, entries=0, final_url=None, attachments=()
) -> Document:
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url=url,
        url_key=url.split("://", 1)[1],
        title=title,
        publish_date=date(2026, 9, 2),
        first_seen_at=now,
        last_seen_at=now,
        is_baseline=False,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="见附件",
            fetched_at=FETCHED_AT,
            status="ok",
            final_url=final_url,
            attachments=[
                DocumentAttachment(position=i, name=name, url=link, extension="pdf")
                for i, (name, link) in enumerate(attachments)
            ],
        )
    )
    for i in range(entries):
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=url,
                name=f"甲{i}科技有限公司",
                normalized_name=f"甲{i}科技有限公司",
                sequence=str(i + 1),
                category="",
            )
        )
    return doc


def _henan_lists(session) -> tuple[Document, FakeFetcher]:
    source = _source(session)
    # 原来记下的附件这次页面上没有了：写入时也不删
    henan = _document(
        session,
        source,
        LIST_URL,
        "河南省2026年第一批入库科技型中小企业名单公告",
        attachments=[("旧附件", OLD_URL)],
    )
    # 不在列出省份的、已读出企业名的、不是名单的，都不查
    _document(
        session, source, "https://kjt.hebei.gov.cn/a/1.html", "河北省入库科技型中小企业名单公告"
    )
    _document(
        session, source, "http://kjt.henan.gov.cn/b/2.html", "河南省专精特新企业名单公告", entries=2
    )
    _document(session, source, "http://kjt.henan.gov.cn/c/3.html", "关于开展科技项目申报的通知")
    session.commit()
    ok = FetchResult(LIST_URL, "ok", final_url=LIST_URL, http_status=200, text=PAGE)
    return henan, FakeFetcher({LIST_URL: ok})


def _stored(session, document_id):
    session.expire_all()
    detail = session.get(DocumentDetail, document_id)
    return detail, [(a.position, a.name, a.url, a.extension) for a in detail.attachments]


def test_rescan_previews_then_appends_only(session) -> None:
    assert file_server_suffixes() == [".henan.gov.cn"]
    henan, fake = _henan_lists(session)
    before = [(0, "旧附件", OLD_URL, "pdf")]

    preview = rescan_list_details(session, fetcher=fake)
    assert [(r.document_id, r.new_attachments, r.reason) for r in preview] == [
        (henan.id, [(PDF_NAME, PDF_URL)], None)
    ]
    assert fake.calls == [LIST_URL]
    assert _stored(session, henan.id)[1] == before  # 预览不写库

    applied = rescan_list_details(session, apply=True, fetcher=fake)
    assert [(r.new_attachments, r.reason) for r in applied] == [([(PDF_NAME, PDF_URL)], None)]
    detail, stored = _stored(session, henan.id)
    # 只追加：原有附件（页面上已经没有了）不删，正文和抓取时间不动
    assert stored == before + [(1, PDF_NAME, PDF_URL, "pdf")]
    assert detail.body_text == "见附件"
    assert detail.fetched_at.replace(tzinfo=UTC) == FETCHED_AT
    # 再跑一次：附件已记下，不再报新的，也不重复写
    again = rescan_list_details(session, apply=True, fetcher=fake)
    assert [(r.new_attachments, r.reason) for r in again] == [([], None)]
    assert _stored(session, henan.id)[1] == stored
    # 指定别的网站后缀就不查河南
    assert rescan_list_details(session, suffixes=[".shanxi.gov.cn"], fetcher=fake) == []
    assert rescan_list_details(session, limit=1, fetcher=fake)[0].document_id == henan.id


def test_rescan_selects_by_final_page_host(session) -> None:
    """带端口的网址、跳转后才到本省网站的页面也选；跳到外省的不选（解析附件按最终网址判断）。"""
    source = _source(session)
    port = _document(session, source, "http://kjt.henan.gov.cn:8080/2026/1.html", "河南省名单公告")
    moved = _document(
        session,
        source,
        "https://www.gov.cn/r/2.html",
        "河南省绿色工厂名单公示",
        final_url="http://gxt.henan.gov.cn/2026/09-10/2.html",
    )
    _document(
        session,
        source,
        "http://kjt.henan.gov.cn/2026/3.html",
        "河北省绿色工厂名单公示",
        final_url="https://gxt.hebei.gov.cn/2026/3.html",
    )
    session.commit()
    pages = {
        doc.url: FetchResult(doc.url, "ok", final_url=doc.url, http_status=200, text=PAGE)
        for doc in (port, moved)
    }
    results = rescan_list_details(session, fetcher=FakeFetcher(pages))
    assert [r.document_id for r in results] == [port.id, moved.id]


def test_rescan_reports_failed_refetch_and_keeps_detail(session) -> None:
    source = _source(session, dept="工业和信息化", url="https://gxt.henan.gov.cn/tzgg/")
    url = "https://gxt.henan.gov.cn/2026/09-10/1.html"
    doc = _document(session, source, url, "河南省2026年绿色工厂名单公示")
    session.commit()

    gone = FetchResult(url, "http_error", final_url=url, http_status=503, error="HTTP 503")
    fake = FakeFetcher({url: gone})
    preview = rescan_list_details(session, fetcher=fake)
    assert [(r.document_id, r.new_attachments, r.reason) for r in preview] == [
        (doc.id, [], "HTTP 503")
    ]
    applied = rescan_list_details(session, apply=True, fetcher=fake)
    assert [(r.new_attachments, r.reason) for r in applied] == [([], "HTTP 503")]
    detail, stored = _stored(session, doc.id)
    assert (detail.status, detail.body_text, stored) == ("ok", "见附件", [])
    assert detail.fetched_at.replace(tzinfo=UTC) == FETCHED_AT
    assert detail.failure_reason is None


def test_rescan_respects_attachment_cap(session) -> None:
    source = _source(session)
    existing = [(f"附件{i}", f"http://kjt.henan.gov.cn/f/{i}.pdf") for i in range(49)]
    doc = _document(session, source, LIST_URL, "河南省名单公告", attachments=existing)
    session.commit()
    second = PAGE.replace(
        "</div>", "<p><a href='https://xcoss.henan.gov.cn/b.pdf'>第二批</a></p></div>"
    )
    fake = FakeFetcher({LIST_URL: FetchResult(LIST_URL, "ok", final_url=LIST_URL, text=second)})
    applied = rescan_list_details(session, apply=True, fetcher=fake)
    assert [(r.new_attachments, r.reason) for r in applied] == [
        ([(PDF_NAME, PDF_URL)], "附件已到 50 个上限，另有 1 个未写入")
    ]
    assert len(_stored(session, doc.id)[1]) == 50


def test_rescan_list_details_command(session, monkeypatch, capsys) -> None:
    henan, fake = _henan_lists(session)

    class Polite:
        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(detail_service, "_polite_fetcher", Polite)
    assert cli.main(["rescan-list-details"]) == 0
    out = capsys.readouterr().out
    assert f"  #{henan.id} 河南省2026年第一批入库科技型中小企业名单公告" in out
    assert f"      + {PDF_NAME}  {PDF_URL}" in out
    assert (
        "预览（未写入）：检查 1 份名单（.henan.gov.cn），会新认出附件的 1 份，"
        "没有新附件的 0 份，没抓成功或未写入的 0 份"
    ) in out
    assert "加 --apply 写入（只追加新附件，不改正文和原有附件）" in out

    assert cli.main(["rescan-list-details", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已写入：检查 1 份名单（.henan.gov.cn），新记下附件的 1 份" in out
    assert "下一轮名单解析（youju parse-lists）会读这些附件。" in out

    assert cli.main(["rescan-list-details", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "会新认出附件的 0 份，没有新附件的 1 份，没抓成功或未写入的 0 份" in out
    assert "已到 --limit 1 上限" in out
