from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentMerge, ListEntry, Source

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
