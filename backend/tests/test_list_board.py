from collections import defaultdict
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import or_, select

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.dedup import groups_for
from youju.lists.lookup import public_condition
from youju.models import Document, DocumentMerge, ListEntry, Source
from youju.report_revision import publication_revision

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def board_client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(v1, "now", lambda: NOW)
    v1._REPORT_CACHE.clear()
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    v1._REPORT_CACHE.clear()


def _doc(source: Source, number: int, title: str, published: date) -> Document:
    stamp = datetime.now(UTC)
    return Document(
        source_id=source.id,
        url=f"https://example.gov.cn/{number}.html",
        url_key=f"example.gov.cn/{number}.html",
        title=title,
        publish_date=published,
        first_seen_at=stamp,
        last_seen_at=stamp,
    )


def _entry(document_id: int, url: str, name: str, sequence: str) -> ListEntry:
    return ListEntry(
        document_id=document_id,
        source_url=url,
        name=name,
        normalized_name=name,
        credit_code=None,
        sequence=sequence,
        category="企业",
        batch=None,
        region=None,
    )


@pytest.fixture
def board_data(session):
    public = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知",
        url="https://example.gov.cn/notice",
        rights_level=1,
    )
    private = Source(
        level="省级",
        province="江苏",
        dept="科技",
        dept_full_name="江苏省科学技术厅",
        column_name="通知",
        url="https://private.example.gov.cn/notice",
        rights_level=2,
    )
    session.add_all([public, private])
    session.flush()
    high = _doc(public, 1, "山东省2026年高新技术企业名单公示", date(2026, 9, 27))
    duplicate = _doc(public, 2, "山东省2026年高新技术企业名单公示", date(2026, 9, 27))
    special = _doc(public, 3, "山东省2026年拟入选专精特新企业名单公布", date(2026, 9, 26))
    unparsed = _doc(public, 6, "山东省2026年绿色工厂名单公示", date(2026, 9, 25))
    old = _doc(public, 4, "山东省2025年科技型中小企业名单公布", date(2025, 1, 1))
    hidden = _doc(private, 5, "江苏省2026年高新技术企业名单公布", date(2026, 9, 26))
    session.add_all([high, duplicate, special, unparsed, old, hidden])
    session.flush()
    session.add(
        DocumentMerge(
            duplicate_id=duplicate.id,
            canonical_id=high.id,
            active=True,
            match_key="same-notification",
        )
    )
    session.add_all(
        [
            _entry(high.id, "https://example.gov.cn/high.pdf", "甲公司", "1"),
            _entry(high.id, "https://example.gov.cn/high.pdf", "乙公司", "2"),
            _entry(duplicate.id, "https://example.gov.cn/high-copy.pdf", "甲公司", "1"),
            _entry(duplicate.id, "https://example.gov.cn/high-copy.pdf", "丙公司", "3"),
            _entry(special.id, "https://example.gov.cn/special.pdf", "丁公司", "1"),
            _entry(hidden.id, "https://private.example.gov.cn/hidden.pdf", "不公开公司", "1"),
        ]
    )
    session.commit()


def test_board_counts_deduplicates_groups_and_names_and_keeps_public_status(
    board_client, board_data
):
    body = board_client.get("/api/v1/reports/lists").json()
    assert body["recent"]["total"] == 3
    assert [
        (item["title"], item["count"], item["wording"]) for item in body["recent"]["items"]
    ] == [
        ("山东省2026年高新技术企业名单公示", 3, "公示"),
        ("山东省2026年拟入选专精特新企业名单公布", 1, "公布"),
        ("山东省2026年绿色工厂名单公示", None, "公示"),
    ]
    assert set(body["recent"]["items"][0]) == {
        "id",
        "title",
        "date",
        "wording",
        "issuer",
        "province",
        "city",
        "district",
        "level",
        "year",
        "type",
        "count",
    }
    types = {item["type"]: item for item in body["stats"]["by_type"]}
    assert types["高新技术企业"]["lists"] == 1
    assert types["高新技术企业"]["entries"] == 3
    assert set(body["stats"]) == {"by_year", "by_province", "by_type"}
    assert "其他" not in types  # 未读出企业名称的名单仍可浏览，但不计入查名单范围
    assert sum(item["lists"] for item in types.values()) == 2
    assert "江苏省2026年高新技术企业名单公布" not in [
        item["title"] for item in body["recent"]["items"]
    ]


def test_board_filters_paginates_and_validates(board_client, board_data):
    response = board_client.get("/api/v1/reports/lists", params={"type": "专精特新", "size": 1})
    assert response.status_code == 200
    assert response.json()["recent"]["items"][0]["type"] == "专精特新"
    assert board_client.get("/api/v1/reports/lists", params={"page": 2, "size": 1}).json()[
        "recent"
    ]["items"]
    assert board_client.get("/api/v1/reports/lists", params={"days": 7}).status_code == 422
    assert board_client.get("/api/v1/reports/lists", params={"province": "火星"}).status_code == 422
    assert board_client.get("/api/v1/reports/lists", params={"type": "政策"}).status_code == 422


def test_board_cache_keeps_full_result_for_other_pages(board_client, board_data, monkeypatch):
    calls = 0
    original = v1._list_board_payload

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(v1, "_list_board_payload", counted)
    first = board_client.get("/api/v1/reports/lists", params={"size": 1}).json()
    second = board_client.get("/api/v1/reports/lists", params={"page": 2, "size": 2}).json()
    assert calls == 1
    assert first["recent"]["items"][0]["id"] != second["recent"]["items"][0]["id"]
    assert set(second["stats"]) == {"by_year", "by_province", "by_type"}


def _doc_id(session, number: int) -> int:
    return session.scalar(select(Document.id).where(Document.url == _doc_url(number)))


def _doc_url(number: int) -> str:
    return f"https://example.gov.cn/{number}.html"


def _board_counts(client) -> dict[int, int | None]:
    body = client.get("/api/v1/reports/lists", params={"days": 90, "size": 100}).json()
    return {int(item["id"]): item["count"] for item in body["recent"]["items"]}


def _python_counts(session) -> dict[int, int | None]:
    """改前的算法（把全部条目读进程序、按归并组逐条去重），作对照。"""
    v1.note_rules_complete(session)
    rows = session.execute(v1._base().where(v1._kind_is(["名单"]))).all()
    owner = {
        member: master
        for master, members in groups_for(session, [doc.id for doc, _ in rows]).items()
        for member in members
    }
    names: dict[int, set[str]] = defaultdict(set)
    for document_id, name in session.execute(
        select(ListEntry.document_id, ListEntry.normalized_name)
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ListEntry.document_id.in_(list(owner) or [-1]),
            public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
        )
    ):
        if name:
            names[owner[document_id]].add(name)
    return {doc.id: len(names[doc.id]) if doc.id in names else None for doc, _ in rows}


def _db_counts(session) -> dict[int, int | None]:
    v1.note_rules_complete(session)
    rows = session.execute(v1._base().where(v1._kind_is(["名单"]))).all()
    counts = v1._list_entry_counts(session, publication_revision(session, "lists"))
    return {doc.id: counts.get(doc.id) for doc, _ in rows}


def test_board_counts_in_database_match_python_dedup(board_client, board_data, session):
    """S2：条数改在数据库里汇总，口径和改前在程序里逐条去重一样：空名称不算，大小写或尾部空格
    不同的另算，副本人工改成别的类型就不算它的条目，二级来源与一级来源归并后整组公开，下架整组不公开。"""
    high, duplicate, special, hidden = (_doc_id(session, n) for n in (1, 2, 3, 5))
    public = session.get(Document, high).source_id
    url = "https://example.gov.cn/special.pdf"
    session.add_all(
        [
            _entry(special, url, "", "2"),
            _entry(special, url, "丁公司 ", "3"),
            _entry(special, url, "ABC公司", "4"),
            _entry(special, url, "abc公司", "5"),
            _entry(special, url + "#2", "ABC公司", "4"),
        ]
    )
    twin = Document(
        source_id=public,
        url=_doc_url(7),
        url_key=_doc_url(7).removeprefix("https://"),
        title="江苏省2026年高新技术企业名单公布",
        publish_date=date(2026, 9, 26),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(twin)
    session.flush()
    session.add_all(
        [
            DocumentMerge(duplicate_id=twin.id, canonical_id=hidden, active=True, match_key="t"),
            _entry(twin.id, "https://example.gov.cn/twin.pdf", "戊公司", "1"),
        ]
    )
    session.commit()
    expected = {high: 3, special: 4, hidden: 2, _doc_id(session, 6): None}
    assert _board_counts(board_client) == expected
    assert _db_counts(session) == _python_counts(session)

    session.get(Document, duplicate).kind_override = "政策"
    session.commit()
    assert _board_counts(board_client) == expected | {high: 2}
    assert _db_counts(session) == _python_counts(session)

    session.get(Document, twin.id).taken_down_at = datetime.now(UTC)
    session.commit()
    v1._REPORT_CACHE.clear()
    assert hidden not in _board_counts(board_client)
    assert _db_counts(session) == _python_counts(session)


def test_board_counts_reused_until_their_data_changes(board_client, board_data, session):
    """名单条数按它用到的数据的版本缓存：看板自己的 300 秒缓存到期后，数据没变就不重算；
    条目、归并、来源公开级别变了就重算；另有 1 小时兜底。"""
    high, duplicate, special, hidden = (_doc_id(session, n) for n in (1, 2, 3, 5))
    first = _board_counts(board_client)
    cached = v1._LIST_COUNTS["all"]
    v1._REPORT_CACHE.clear()  # 看板缓存到期
    assert _board_counts(board_client) == first
    assert v1._LIST_COUNTS["all"] is cached

    # 直接写入的新条目（没经过名单解析记录）
    session.add(_entry(special, "https://example.gov.cn/special.pdf", "己公司", "9"))
    session.commit()
    v1._REPORT_CACHE.clear()
    assert _board_counts(board_client)[special] == 2

    # 归并停用：副本成了单独的一份，主文件只剩自己的甲、乙
    session.get(DocumentMerge, duplicate).active = False
    session.commit()
    v1._REPORT_CACHE.clear()
    counts = _board_counts(board_client)
    assert (counts[high], counts[duplicate]) == (2, 2)

    # 来源公开级别：江苏那份的来源改成一级，它就公开了，条数也算出来
    hidden_source = session.get(Document, hidden).source_id
    session.get(Source, hidden_source).rights_level = 1
    session.commit()
    v1._REPORT_CACHE.clear()
    assert _board_counts(board_client)[hidden] == 1

    # 1 小时兜底：版本没变也重算一次
    revision, stamp, counts = v1._LIST_COUNTS["all"]
    v1._LIST_COUNTS["all"] = (revision, stamp - v1.LIST_COUNTS_TTL, counts)
    v1._REPORT_CACHE.clear()
    _board_counts(board_client)
    assert v1._LIST_COUNTS["all"][2] is not counts
    assert _db_counts(session) == _python_counts(session)
