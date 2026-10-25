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
    SigningKey,
    Source,
)

URL = "https://kjt.shandong.gov.cn/attach/2026list.xlsx"
NOW = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
ROWS = [
    ["序号", "企业名称", "统一社会信用代码", "所在地区", "联系人", "联系电话"],
    # 全是数字的信用代码（校验位对；按号码规则会被当成银行卡号遮掉）：名单模式不遮
    [1, "山东星河科技有限公司", "913701020001000284", "济南市", "张三", "13812345678"],
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


def test_entry_token_is_opaque_and_tamper_proof(session) -> None:
    tokens = evidence.entry_tokens(session, [5, 6])
    # 加密后的编号：一样长、看不出是第几条，同一条目每次一样
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{32}", token) for token in tokens.values())
    assert tokens[5] != tokens[6] and evidence.entry_tokens(session, [5]) == {5: tokens[5]}
    assert evidence.parse_entry_token(session, tokens[5]) == 5
    assert evidence.parse_entry_token(session, tokens[6]) == 6
    # 改一个字、乱写的、以前"编号.签名"的写法，都认不出
    for i in (0, 15, 31):
        flipped = "A" if tokens[5][i] != "A" else "B"
        assert (
            evidence.parse_entry_token(session, tokens[5][:i] + flipped + tokens[5][i + 1 :])
            is None
        )
    for bad in ("", "5", "5." + "A" * 16, "A" * 31, "A" * 33, "A" * 32, "*" * 32):
        assert evidence.parse_entry_token(session, bad) is None
    # 换了密钥，旧编号作废
    key = session.get(SigningKey, "list_entry")
    key.key_hex = "22" * 32
    session.commit()
    assert evidence.parse_entry_token(session, tokens[5]) is None


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
    assert row["cells"][1] == {"h": "企业名称", "t": "山东星河科技有限公司", "n": 1}
    assert row["cells"][2]["t"] == "913701020001000284"
    assert row["hidden"] == 2
    # 只存了命中行：别的企业、联系人、电话都没进库
    stored = json.dumps(
        [r.rows for r in session.scalars(select(ListEntryLocation))], ensure_ascii=False
    )
    assert "张三" not in stored and "13812345678" not in stored and "0535" not in stored
    # 同样的文件、同样的条目：下一轮不再读
    again = evidence.refresh(session, fetcher=fetcher, now=NOW + timedelta(days=1))
    assert again.due == 0 and fetcher.calls == [URL]


def test_list_mode_keeps_credit_codes_but_masks_other_numbers() -> None:
    rows = [
        ["企业名称", "统一社会信用代码", "开户银行账号", "备注"],
        ["甲科技有限公司", "913701020001000284", "6222021234567890128", "913701020001000763"],
    ]
    data = _xlsx(rows)

    def cells(limits):
        [row] = [
            s for s in attachment_text.extract(data, limits=limits).segments if s.kind == "row"
        ]
        return [c["t"] for c in row.cells]

    # 给 AI 用的照旧遮；名单模式：信用代码那一列、没写列名但开头是常见单位代码的不遮，账号照样遮
    assert cells(None)[1] != "913701020001000284"
    kept = cells(attachment_text.LIST_LIMITS)
    assert kept[1] == "913701020001000284"
    assert "6222021234567890128" not in kept[2]
    # 列名写着"备注"（没有"代码"）：不放行
    assert kept[3] != "913701020001000763"
    assert (
        attachment_text.mask_cell("913701020001000284", "统一社会信用代码") == "913701020001000284"
    )
    assert attachment_text.mask_cell("913701020001000284", None) == "913701020001000284"
    assert attachment_text.mask_cell("923701020000010135", None) != "923701020000010135"


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
    assert columns == {(None, None, 1, "企业名称", 1)}


def _names(n, start=0):
    return [f"{chr(0x7532 + start + i)}科技有限公司" for i in range(n)]


def test_name_column_is_found_table_by_table() -> None:
    # PDF 跨页没接上的续表：第一页有列名，后面几页没有；每张表分别认，第一页的几家也找得到
    first = [
        _seg(
            i + 1, [{"c": 1, "t": name, "h": "企业名称"}], loc={"page": 1, "table": 1, "row": i + 2}
        )
        for i, name in enumerate(_names(2))
    ]
    rest = [
        _seg(
            10 + i,
            [{"c": 0, "t": str(i + 3)}, {"c": 1, "t": name}],
            loc={"page": 2, "table": 2, "row": i + 1},
        )
        for i, name in enumerate(_names(12, start=2))
    ]
    names = _names(14)
    found, columns = evidence.locate(
        first + rest, [(i, name, None) for i, name in enumerate(names)]
    )
    assert all(found[i].state == "ok" for i in range(14))
    assert columns == {(None, None, 1, "企业名称", 1), (None, None, 2, "", 1)}


def test_header_rows_are_compared_too() -> None:
    # Word 里整张表都设了"标题行重复"：名单全在表头行里
    segments = [_seg(1, [{"c": 0, "t": "序号"}, {"c": 1, "t": "单位名称"}], kind="head")] + [
        _seg(i + 2, [{"c": 0, "t": str(i + 1)}, {"c": 1, "t": name}], kind="head")
        for i, name in enumerate(_names(3))
    ]
    found, _ = evidence.locate(segments, [(i, name, None) for i, name in enumerate(_names(3))])
    assert [found[i].segments for i in range(3)] == [[2], [3], [4]]


def test_main_column_counts_when_entries_are_a_minority() -> None:
    # 名单里一多半是解析时不收的政府部门：名称列里条目不到一半，但它是名称最多的那一列
    agencies = [f"{city}市科学技术局" for city in "甲乙丙丁戊己庚辛"]
    names = _names(4)
    segments = [
        _seg(i + 1, [{"c": 0, "t": str(i + 1)}, {"c": 1, "t": value}])
        for i, value in enumerate(agencies + names)
    ]
    found, _ = evidence.locate(segments, [(i, name, None) for i, name in enumerate(names)])
    assert [found[i].segments for i in range(4)] == [[9], [10], [11], [12]]


def test_summary_table_of_recommenders_is_not_a_name_column() -> None:
    names = _names(10)
    main = [_seg(i + 1, [{"c": 1, "t": name, "h": "企业名称"}]) for i, name in enumerate(names)]
    # 另一张表"推荐单位 | 数量"：只有一格碰巧是名单里的单位
    summary = [
        _seg(
            20 + i,
            [{"c": 0, "t": value, "h": "推荐单位"}, {"c": 1, "t": "3", "h": "数量"}],
            loc={"table": 2, "row": i + 2},
        )
        for i, value in enumerate([names[0], "某市科学技术局", "某区科学技术局", "某县科学技术局"])
    ]
    found, columns = evidence.locate(main + summary, [(i, n, None) for i, n in enumerate(names)])
    assert (found[0].state, found[0].segments) == ("ok", [1])
    assert columns == {(None, None, 1, "企业名称", 1)}


def test_only_public_columns_are_stored() -> None:
    seg = _seg(
        1,
        [
            {"c": 0, "t": "1"},
            {"c": 1, "t": "甲科技有限公司", "h": "申请人"},
            {"c": 2, "t": "张三", "h": "项目负责人"},
            {"c": 3, "t": "乙大学", "h": "推荐单位"},
            {"c": 4, "t": "历下区", "h": "所属区"},
            {"c": 5, "t": "请联系王五", "h": "备注"},
            {"c": 6, "t": "李四"},
            {"c": 7, "t": "历城区"},
            {"c": 8, "t": "913701020001000284"},
            {"c": 9, "t": ""},
        ],
    )
    stored = evidence._stored_row(seg, {(None, None, 1, "申请人", 1)})
    # 名称列（列名像个人信息也照样存）、推荐单位、地区、没有列名的序号和信用代码；负责人、备注、
    # 没有列名的人名和地名不存
    assert [(c["h"], c["t"]) for c in stored["cells"]] == [
        ("", "1"),
        ("申请人", "甲科技有限公司"),
        ("推荐单位", "乙大学"),
        ("所属区", "历下区"),
        ("", "913701020001000284"),
    ]
    assert stored["cells"][1]["n"] == 1 and stored["hidden"] == 4


def test_locate_many_hits_and_no_name_column() -> None:
    same = [_seg(n, [{"c": 0, "t": f"{n}.甲科技有限公司", "h": "单位名称"}]) for n in range(1, 8)]
    found, _ = evidence.locate(same, [(1, "甲科技有限公司", None)])
    # 超过 5 处只存前 5 处，记 ambiguous；开头的序号去掉再比
    assert (found[1].state, found[1].segments) == ("ambiguous", [1, 2, 3, 4, 5])
    found, columns = evidence.locate(
        [_seg(1, [{"c": 0, "t": "项目名称"}])], [(1, "甲科技有限公司", None)]
    )
    assert found[1].state == "no_name_column" and columns == set()


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


def test_list_published_as_a_file_itself_is_read(session) -> None:
    # 通知网址本身就是名单文件（source_kind=document）：一样读、一样定位
    document = _list(session)
    session.query(ListParse).filter(ListParse.document_id == document.id).update(
        {"source_kind": "document"}
    )
    session.commit()
    assert evidence.candidates(session) == {URL: "xlsx"}
    report = evidence.refresh(session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW)
    assert report.states == {"ok": 3}


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
    assert list(evidence._due(session, wanted, NOW + timedelta(days=6))) == []
    assert evidence._due(session, wanted, NOW + timedelta(days=8)) == {URL: "retry"}


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
    due = evidence._due(session, evidence.candidates(session), NOW + timedelta(hours=1))
    assert due == {URL: "changed"}


def test_recheck_of_an_unchanged_file_skips_locating(session) -> None:
    _list(session)
    fetcher = FakeFetcher(_xlsx(ROWS))
    evidence.refresh(session, fetcher=fetcher, now=NOW)
    later = NOW + evidence.OK_RECHECK + timedelta(hours=1)
    assert evidence._due(session, evidence.candidates(session), later) == {URL: "recheck"}
    report = evidence.refresh(session, fetcher=fetcher, now=later)
    assert (report.read, report.unchanged) == (0, 1)
    source = session.scalar(select(ListEvidenceSource))
    session.refresh(source)
    assert source.checked_at.replace(tzinfo=UTC) == later
    assert session.get(ListEntryLocation, _entry(session, "山东星河科技有限公司").id) is not None
    # 原网站换了文件：重新定位
    changed = ROWS[:3]
    fetcher.content = _xlsx(changed)
    report = evidence.refresh(session, fetcher=fetcher, now=later + evidence.OK_RECHECK)
    assert report.read == 1 and report.states == {"ok": 2, "notfound": 1}


def test_temporary_failure_keeps_the_rows_already_read(session) -> None:
    _list(session)
    evidence.refresh(session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW)
    later = NOW + evidence.OK_RECHECK + timedelta(hours=1)
    down = FakeFetcher(status="http_error", http_status=503)
    report = evidence.refresh(session, fetcher=down, now=later)
    assert report.failed == {"unavailable": 1}
    source = session.scalar(select(ListEvidenceSource))
    session.refresh(source)
    assert (
        source.status == "ok"
        and source.retry_at.replace(tzinfo=UTC) == later + evidence.RETRY_AFTER
    )
    assert session.get(ListEntryLocation, _entry(session, "山东星河科技有限公司").id) is not None
    wanted = evidence.candidates(session)
    assert list(evidence._due(session, wanted, later + timedelta(hours=1))) == []
    assert evidence._due(session, wanted, later + timedelta(hours=25)) == {URL: "recheck"}


def test_unexpected_error_is_recorded_and_retried_later(session, monkeypatch) -> None:
    _list(session)

    def boom(data, limits=None):
        raise RuntimeError("解析器崩了")

    monkeypatch.setattr(attachment_text, "extract", boom)
    report = evidence.refresh(session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW)
    assert report.errors == 1 and report.read == 0
    source = session.scalar(select(ListEvidenceSource))
    assert (source.status, source.reason) == ("failed", "unavailable")
    assert "RuntimeError" in source.detail
    wanted = evidence.candidates(session)
    assert list(evidence._due(session, wanted, NOW + timedelta(hours=1))) == []
    assert evidence._due(session, wanted, NOW + timedelta(hours=25)) == {URL: "retry"}


def test_refresh_stops_at_the_time_limit(session) -> None:
    document = _list(session)
    # 同一份通知的第二个名单附件
    second = URL.replace("2026list", "2026list2")
    session.add(
        ListParse(
            document_id=document.id,
            source_url=second,
            source_kind="attachment",
            status="ok",
            entry_count=1,
            attempted_at=NOW,
        )
    )
    session.add(
        ListEntry(
            document_id=document.id,
            source_url=second,
            name="山东星河科技有限公司",
            normalized_name="山东星河科技有限公司",
            category="科技型中小企业",
        )
    )
    session.commit()
    ticks = iter([0.0, 0.0, evidence.RUN_SECONDS + 1.0])
    report = evidence.refresh(
        session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW, clock=lambda: next(ticks)
    )
    assert report.due == 2 and report.read == 1 and report.out_of_time


def test_refresh_lock(session, monkeypatch) -> None:
    # 拿到锁时能查到锁在；另一个任务拿不到锁
    with evidence._exclusive(session) as held:
        assert held is not None and held()
        with evidence._exclusive(session) as second:
            assert second is None
        assert evidence.refresh(session, fetcher=FakeFetcher()).skipped_lock
    # 读到一半锁丢了（数据库连接断开）：停下，不和别的任务同时读
    _list(session)
    from contextlib import contextmanager

    @contextmanager
    def lost(_session):
        yield lambda: False

    monkeypatch.setattr(evidence, "_exclusive", lost)
    report = evidence.refresh(session, fetcher=FakeFetcher(_xlsx(ROWS)), now=NOW)
    assert report.lost_lock and report.read == 0


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
    assert [(cell["h"], cell["t"], cell["name"]) for cell in row["cells"]] == [
        ("序号", "1", False),
        ("企业名称", "山东星河科技有限公司", True),
        ("统一社会信用代码", "913701020001000284", False),
        ("所在地区", "济南市", False),
    ]
    assert row["hidden"] == 2
    text = json.dumps(found, ensure_ascii=False)
    assert "张三" not in text and "13812345678" not in text

    for bad in (token[:-1] + ("A" if token[-1] != "A" else "B"), "1.AAAAAAAAAAAAAAAA", "A" * 32):
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


def test_cli_previews_and_reports(session, monkeypatch, capsys) -> None:
    from youju import cli

    _list(session)
    assert cli.main(["list-evidence"]) == 0
    out = capsys.readouterr().out
    assert "一共 1 份" in out and "该读 1 份" in out and "没有下载" in out
    monkeypatch.setattr(
        evidence,
        "refresh",
        lambda s, limit: evidence.EvidenceReport(due=3, read=1, unchanged=1, out_of_time=True),
    )
    assert cli.main(["list-evidence", "--apply", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "本轮读了 1 份" in out and "文件没变的 1 份" in out and "时间上限" in out
    monkeypatch.setattr(evidence, "refresh", lambda s, limit: evidence.EvidenceReport(errors=2))
    assert cli.main(["list-evidence", "--apply"]) == 1
