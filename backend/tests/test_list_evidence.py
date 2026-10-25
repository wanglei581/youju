"""查名单站内闭环（D-088、D-091）：名单附件里找到企业那一行，只存命中行，个人信息列不存不显示。"""

import io
import json
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, select

from youju import attachment_text
from youju.api.app import app
from youju.crawler.fetcher import FetchResult
from youju.db import get_session
from youju.lists import evidence
from youju.lists.parser import MAX_PDF_PAGES
from youju.models import (
    Document,
    ListEntry,
    ListEntryLocation,
    ListEvidenceSource,
    ListParse,
    Source,
)

URL = "https://kjt.shandong.gov.cn/attach/2026list.xlsx"
NOW = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
ROWS = [
    ["序号", "企业名称", "统一社会信用代码", "所在地区", "联系人", "联系电话"],
    [1, "山东星河科技有限公司", "91310117588657256C", "济南市", "张三", "13812345678"],
    [2, "青岛海纳智能有限公司", "91370200MA3C4R7X1A", "青岛市", "李四", "13912345678"],
    [3, "烟台远航机械有限公司", None, "烟台市", "王五", "0535-1234567"],
]


@pytest.fixture
def client(session_factory):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _xlsx(rows) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "名单"
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


class FakeFetcher:
    def __init__(self, content=b"", status="ok", http_status=200):
        self.content, self.status, self.http_status = content, status, http_status
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        return FetchResult(
            url=url,
            status=self.status,
            final_url=url,
            http_status=self.http_status,
            content=self.content,
        )

    def close(self):
        pass


def _list(session, title="2026年第一批入库科技型中小企业名单公告", url=URL, names=None):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知公告",
        url="https://kjt.shandong.gov.cn/list/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    document = Document(
        source_id=source.id,
        url=f"https://kjt.shandong.gov.cn/{abs(hash(title)) % 10**8}.html",
        url_key=f"kjt.shandong.gov.cn/{abs(hash(title)) % 10**8}.html",
        title=title,
        publish_date=date(2026, 9, 2),
        first_seen_at=now,
        last_seen_at=now,
        is_baseline=False,
    )
    session.add(document)
    session.flush()
    names = names or [(row[1], row[2]) for row in ROWS[1:]]
    session.add(
        ListParse(
            document_id=document.id,
            source_url=url,
            source_kind="attachment",
            status="ok",
            entry_count=len(names),
            attempted_at=now,
        )
    )
    for i, (name, code) in enumerate(names, 1):
        session.add(
            ListEntry(
                document_id=document.id,
                source_url=url,
                name=name,
                normalized_name=name,
                credit_code=code,
                sequence=str(i),
                category="科技型中小企业",
                batch="第一批",
            )
        )
    session.commit()
    return document


def _entry(session, name):
    return session.scalar(select(ListEntry).where(ListEntry.name == name))


def test_list_limits_match_list_parser() -> None:
    # 名单条目最多来自前 1000 页；定位读的页数要和解析一样多，不然后面几页的企业都"没找到"
    assert attachment_text.LIST_LIMITS.pdf_pages == MAX_PDF_PAGES
    assert attachment_text.LIST_LIMITS.pdf_first_pages is True


def test_entry_token_round_trip_and_tampering(session) -> None:
    tokens = evidence.entry_tokens(session, [5, 6])
    assert re.fullmatch(r"5\.[A-Za-z0-9_-]{16}", tokens[5])
    assert evidence.parse_entry_token(session, tokens[5]) == 5
    # 改一个字、换一个编号、乱写的，都认不出
    tampered = tokens[5][:-1] + ("A" if tokens[5][-1] != "A" else "B")
    assert evidence.parse_entry_token(session, tampered) is None
    assert evidence.parse_entry_token(session, "6." + tokens[5].split(".")[1]) is None
    for bad in ("", "5", "5.", "abc.def", "0." + "A" * 16, "5." + "A" * 17):
        assert evidence.parse_entry_token(session, bad) is None


def test_refresh_stores_only_the_hit_rows_without_personal_columns(session) -> None:
    _list(session)
    fetcher = FakeFetcher(_xlsx(ROWS))
    report = evidence.refresh(session, fetcher=fetcher, now=NOW)
    assert fetcher.calls == [URL]
    assert report.read == 1 and report.states == {"ok": 3}
    source = session.scalar(select(ListEvidenceSource))
    assert (source.status, source.entries, source.located) == ("ok", 3, 3)
    assert source.extractor == attachment_text.EXTRACTOR_VERSION
    assert source.locator == evidence.LOCATOR_VERSION
    location = session.get(ListEntryLocation, _entry(session, "山东星河科技有限公司").id)
    assert location.state == "ok"
    [row] = location.rows
    assert row["where"] == {"sheet": "名单", "row": 2}
    assert [cell["h"] for cell in row["cells"]] == [
        "序号",
        "企业名称",
        "统一社会信用代码",
        "所在地区",
    ]
    assert row["cells"][1]["t"] == "山东星河科技有限公司"
    assert row["hidden"] == 2
    # 只存了命中行：别的企业、联系人、电话都没进库
    stored = json.dumps(
        [r.rows for r in session.scalars(select(ListEntryLocation))], ensure_ascii=False
    )
    assert "张三" not in stored and "13812345678" not in stored and "0535" not in stored
    # 同样的文件、同样的条目：下一轮不再读
    again = evidence.refresh(session, fetcher=fetcher, now=NOW + timedelta(days=1))
    assert again.due == 0 and fetcher.calls == [URL]


def _seg(n, cells, kind="row", loc=None):
    return SimpleNamespace(n=n, kind=kind, loc=loc or {"table": 1, "row": n}, cells=cells)


def test_locate_only_compares_the_name_column() -> None:
    def row(n, name, code, recommender):
        return _seg(
            n,
            [
                {"c": 0, "t": str(n), "h": "序号"},
                {"c": 1, "t": name, "h": "企业名称"},
                {"c": 2, "t": code, "h": "统一社会信用代码"},
                {"c": 3, "t": recommender, "h": "推荐单位"},
            ],
        )

    segments = [
        row(1, "甲科技有限公司", "91310117588657256C", "乙科技有限公司"),
        row(2, "乙科技有限公司", "91370200MA3C4R7X1A", "乙科技有限公司"),
        row(3, "丙科技有限公司", "91370200MA3C4R7X1C", "乙科技有限公司"),
        row(4, "丁科技有限公司", "", "乙科技有限公司"),
    ]
    entries = [
        (1, "甲科技有限公司", "91310117588657256C"),
        # 乙在"推荐单位"列出现了 4 次，但只认名称列里的那一行
        (2, "乙科技有限公司", "91370200MA3C4R7X1A"),
        # 这一行写了代码、和条目的代码不一样：不算这一行
        (3, "丙科技有限公司", "91370200MA3C4R7X1B"),
        # 这一行没写代码：不卡（条目有代码也照样认）
        (5, "丁科技有限公司", "91370200MA3C4R7X1D"),
        (4, "戊科技有限公司", None),
    ]
    found, columns = evidence.locate(segments, entries)
    assert (found[1].state, found[1].segments) == ("ok", [1])
    assert (found[2].state, found[2].segments) == ("ok", [2])
    assert found[3].state == "notfound"
    assert found[4].state == "notfound"
    assert (found[5].state, found[5].segments) == ("ok", [4])
    assert columns == {(None, "企业名称", 1)}


def test_locate_many_hits_and_no_name_column() -> None:
    same = [_seg(n, [{"c": 0, "t": f"{n}.甲科技有限公司", "h": "单位名称"}]) for n in range(1, 8)]
    found, _ = evidence.locate(same, [(1, "甲科技有限公司", None)])
    # 超过 5 处只存前 5 处，记 ambiguous；开头的序号去掉再比
    assert (found[1].state, found[1].segments) == ("ambiguous", [1, 2, 3, 4, 5])
    found, columns = evidence.locate(
        [_seg(1, [{"c": 0, "t": "项目名称"}])], [(1, "甲科技有限公司", None)]
    )
    assert found[1].state == "no_name_column" and columns == set()


def test_person_column_without_header_is_not_stored() -> None:
    rows = [
        _seg(1, [{"c": 0, "t": "甲科技有限公司"}, {"c": 1, "t": "张三"}, {"c": 2, "t": "历下区"}]),
        _seg(2, [{"c": 0, "t": "乙科技有限公司"}, {"c": 1, "t": "李四"}, {"c": 2, "t": "市中区"}]),
    ]
    found, columns = evidence.locate(
        rows, [(1, "甲科技有限公司", None), (2, "乙科技有限公司", None)]
    )
    hidden = evidence._person_columns(rows, columns)
    # 没有列名、整列是人名的藏掉；地名那列留着
    assert hidden == {(None, "", 1)}
    stored = evidence._stored_row(rows[0], hidden)
    assert [c["t"] for c in stored["cells"]] == ["甲科技有限公司", "历下区"]
    assert stored["hidden"] == 1


def test_partial_pdf_is_marked(session, monkeypatch) -> None:
    pdf_url = "https://kjt.shandong.gov.cn/attach/big.pdf"
    _list(session, url=pdf_url, names=[("甲科技有限公司", None)])
    segments = [
        attachment_text.Segment(
            1, "head", "单位名称", {"page": 1, "table": 1, "row": 1}, [{"c": 0, "t": "单位名称"}]
        ),
        attachment_text.Segment(
            2,
            "row",
            "甲科技有限公司",
            {"page": 1, "table": 1, "row": 2},
            [{"c": 0, "t": "甲科技有限公司", "h": "单位名称"}],
        ),
    ]
    monkeypatch.setattr(
        attachment_text,
        "extract",
        lambda data, limits=None: attachment_text.Extraction("pdf", segments, pages=1500),
    )
    evidence.refresh(session, fetcher=FakeFetcher(b"%PDF-1.4"), now=NOW)
    source = session.scalar(select(ListEvidenceSource))
    assert (source.status, source.pages_total, source.pages_read) == ("partial", 1500, 1000)


def test_gone_attachment_is_retried_a_week_later(session) -> None:
    _list(session)
    report = evidence.refresh(
        session, fetcher=FakeFetcher(status="http_error", http_status=404), now=NOW
    )
    assert report.failed == {"gone": 1}
    source = session.scalar(select(ListEvidenceSource))
    assert (source.status, source.reason) == ("failed", "gone")
    assert session.scalar(select(ListEntryLocation)) is None
    wanted = evidence.candidates(session)
    assert evidence._due(session, wanted, NOW + timedelta(days=6)) == []
    assert evidence._due(session, wanted, NOW + timedelta(days=8)) == [URL]


def test_reparsed_entries_are_read_again(session) -> None:
    document = _list(session)
    evidence.refresh(session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW)
    # 名单重新解析：条目删了重写，位置随外键删掉，下一轮要重读
    session.execute(delete(ListEntry).where(ListEntry.document_id == document.id))
    session.add(
        ListEntry(
            document_id=document.id,
            source_url=URL,
            name="山东星河科技有限公司",
            normalized_name="山东星河科技有限公司",
            category="科技型中小企业",
        )
    )
    session.commit()
    assert evidence._due(session, evidence.candidates(session), NOW + timedelta(hours=1)) == [URL]


def test_record_api_pending_found_and_tampered(client, session) -> None:
    _list(session, title="2026年第一批拟入库科技型中小企业名单公示")
    body = client.get("/api/v1/lists/lookup", params={"q": "山东星河科技有限公司"}).json()
    token = body["items"][0]["matched"]["entryToken"]
    pending = client.get("/api/v1/lists/record", params={"entry": token}).json()
    assert pending["evidence"]["state"] == "pending" and pending["evidence"]["rows"] == []
    assert pending["entry"]["name"] == "山东星河科技有限公司"
    assert pending["document"]["status"] == "公示"

    evidence.refresh(session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW)
    found = client.get("/api/v1/lists/record", params={"entry": token}).json()
    assert found["evidence"]["state"] == "found"
    [row] = found["evidence"]["rows"]
    assert row["where"] == "工作表“名单”第 2 行"
    assert {cell["h"] for cell in row["cells"]} == {
        "序号",
        "企业名称",
        "统一社会信用代码",
        "所在地区",
    }
    assert row["hidden"] == 2
    text = json.dumps(found, ensure_ascii=False)
    assert "张三" not in text and "13812345678" not in text

    for bad in (token[:-1] + ("A" if token[-1] != "A" else "B"), "1.AAAAAAAAAAAAAAAA"):
        missing = client.get("/api/v1/lists/record", params={"entry": bad})
        assert missing.status_code == 404


def test_record_api_failed_reason(client, session) -> None:
    _list(session)
    token = client.get("/api/v1/lists/lookup", params={"q": "山东星河科技有限公司"}).json()[
        "items"
    ][0]["matched"]["entryToken"]
    evidence.refresh(session, fetcher=FakeFetcher(status="http_error", http_status=404), now=NOW)
    failed = client.get("/api/v1/lists/record", params={"entry": token}).json()["evidence"]
    assert failed["state"] == "failed" and failed["reason"] == "原网站已经没有这个附件"


@pytest.mark.parametrize(
    "title, shown",
    [
        ("2026年第一批入库科技型中小企业名单公告", True),
        ("2026年严重失信企业名单", False),
        ("2026年经营异常名录企业公示", False),
        ("2026年度高层次人才名单公示", False),
        ("人才引进优秀企业名单公示", True),
    ],
)
def test_lookup_scope_excludes_punitive_and_person_lists(client, session, title, shown) -> None:
    _list(session, title=title)
    body = client.get("/api/v1/lists/lookup", params={"q": "山东星河科技有限公司"}).json()
    assert bool(body["items"]) is shown
