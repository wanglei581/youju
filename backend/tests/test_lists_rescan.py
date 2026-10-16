"""一次性重抓名单详情补附件（10/16，河南附件文件服务器）。"""

from datetime import UTC, date, datetime

from youju import cli
from youju.crawler import detail_service
from youju.crawler.fetcher import FetchResult
from youju.lists.rescan import file_server_suffixes, rescan_list_details
from youju.models import Document, DocumentDetail, ListEntry, Source

LIST_URL = "http://kjt.henan.gov.cn/2026/09-02/3410350.html"
PDF_NAME = "河南省2026年第一批入库科技型中小企业名单"
PDF_URL = "https://xcoss.henan.gov.cn/typtfile/20260902/6f6b.pdf"
PAGE = (
    "<html><head><title>河南省2026年第一批入库科技型中小企业名单公告</title></head><body>"
    "<div id='zoom'><p>现将通过公示的河南省2026年第一批入库企业名单（详见附件）予以公告。</p>"
    f"<p>附件：<a href='{PDF_URL}'>{PDF_NAME}</a></p></div></body></html>"
)


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


def _document(session, source, url, title, *, entries=0) -> Document:
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
    session.add(DocumentDetail(document_id=doc.id, body_text="见附件", fetched_at=now, status="ok"))
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
    henan = _document(session, source, LIST_URL, "河南省2026年第一批入库科技型中小企业名单公告")
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


def test_rescan_previews_then_applies(session) -> None:
    assert file_server_suffixes() == [".henan.gov.cn"]
    henan, fake = _henan_lists(session)

    preview = rescan_list_details(session, fetcher=fake)
    assert [(r.document_id, r.new_attachments, r.reason) for r in preview] == [
        (henan.id, [(PDF_NAME, PDF_URL)], None)
    ]
    assert fake.calls == [LIST_URL]
    session.expire_all()
    assert session.get(DocumentDetail, henan.id).attachments == []  # 预览不写库

    applied = rescan_list_details(session, apply=True, fetcher=fake)
    assert [(r.new_attachments, r.reason) for r in applied] == [([(PDF_NAME, PDF_URL)], None)]
    session.expire_all()
    stored = session.get(DocumentDetail, henan.id).attachments
    assert [(a.url, a.extension) for a in stored] == [(PDF_URL, "pdf")]
    # 再跑一次：附件已记下，不再报新的
    again = rescan_list_details(session, fetcher=fake)
    assert [(r.new_attachments, r.reason) for r in again] == [([], None)]
    # 指定别的网站后缀就不查河南
    assert rescan_list_details(session, suffixes=[".hebei.gov.cn"], fetcher=fake) != []
    assert rescan_list_details(session, suffixes=[".shanxi.gov.cn"], fetcher=fake) == []
    assert rescan_list_details(session, limit=1, fetcher=fake)[0].document_id == henan.id


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
    session.expire_all()
    detail = session.get(DocumentDetail, doc.id)
    assert detail.status == "ok"
    assert detail.body_text == "见附件"


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
    assert "预览（未写入）：检查 1 份名单（.henan.gov.cn），会新认出附件的 1 份" in out
    assert "加 --apply 写入" in out

    assert cli.main(["rescan-list-details", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已写入：检查 1 份名单（.henan.gov.cn），新记下附件的 1 份" in out
    assert "下一轮名单解析（youju parse-lists）会读这些附件。" in out

    assert cli.main(["rescan-list-details", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "会新认出附件的 0 份" in out
    assert "已到 --limit 1 上限" in out
