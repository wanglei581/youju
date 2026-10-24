"""公开名单分页接口（api/lookup.py）：身份、续页、修订号 409、游标篡改 422、不缓存。

数据库触发器（迁移 0041）随语义写入升修订号；采集心跳之类的字段不升。
"""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.lists.rules import _CREDIT_ALPHABET, _CREDIT_WEIGHTS
from youju.models import Document, ListEntry, LookupRevision, Source

NOW = datetime(2026, 10, 23, tzinfo=UTC)
NAME = "合成星河科技有限公司"
CODE = "91310117588657256C"
IDENTITIES = "/api/v1/lists/lookup/identities"
PAGE = "/api/v1/lists/lookup/page"


def _code(n):
    body = f"91310000{n:09d}"
    checksum = (
        31
        - sum(_CREDIT_ALPHABET.index(c) * w for c, w in zip(body, _CREDIT_WEIGHTS, strict=True))
        % 31
    ) % 31
    return body + _CREDIT_ALPHABET[checksum]


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(v1, "now", lambda: NOW)
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _source(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="合成科技厅",
        column_name="公示",
        url="https://example.gov.cn/source/1",
        rights_level=1,
    )
    session.add(source)
    session.flush()
    return source


def _document(session, source, n, published=date(2026, 10, 1)):
    doc = Document(
        id=n,
        source_id=source.id,
        url=f"https://example.gov.cn/{n}",
        url_key=f"example.gov.cn/{n}",
        title=f"合成第{n}批科技型中小企业名单公示",
        publish_date=published,
        kind_override="名单",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    return doc


def _entry(session, doc, seq, code=CODE, name=NAME):
    row = ListEntry(
        document_id=doc.id,
        source_url=doc.url,
        name=name,
        normalized_name=name,
        credit_code=code,
        sequence=str(seq),
        category="科技型中小企业",
    )
    session.add(row)
    session.flush()
    return row


def _revision(session):
    session.expire_all()
    return session.scalar(select(LookupRevision.revision))


def test_single_code_identity_and_no_store(client, session):
    source = _source(session)
    _entry(session, _document(session, source, 1), 1)
    session.commit()
    res = client.get(IDENTITIES, params={"q": NAME})
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert body["identityState"] == "single"
    assert [i["key"] for i in body["identities"]] == [f"code:{CODE}"]
    assert body["identities"][0]["verified"] is True
    assert body["nextCursor"] is None
    assert body["coverage"]["lists"] == 1


def test_same_name_two_codes_are_separate_identities(client, session):
    source = _source(session)
    doc = _document(session, source, 1)
    _entry(session, doc, 1)
    _entry(session, doc, 2, code=_code(7))
    session.commit()
    body = client.get(IDENTITIES, params={"q": NAME}).json()
    assert body["identityState"] == "multiple"
    assert {i["key"] for i in body["identities"]} == {f"code:{CODE}", f"code:{_code(7)}"}


def test_pages_cover_every_list_once_in_date_order(client, session):
    source = _source(session)
    for n in range(1, 26):
        _entry(session, _document(session, source, n, date(2026, 1, 1 + n % 28)), 1)
    session.commit()
    first = client.get(PAGE, params={"q": NAME, "identity": f"code:{CODE}"}).json()
    assert len(first["items"]) == 20 and first["nextCursor"]
    second = client.get(
        PAGE,
        params={
            "q": NAME,
            "identity": f"code:{CODE}",
            "cursor": first["nextCursor"],
            "revision": first["revision"],
        },
    ).json()
    assert second["nextCursor"] is None
    ids = [i["id"] for i in first["items"] + second["items"]]
    assert sorted(ids, key=int) == [str(n) for n in range(1, 26)]
    dates = [i["date"] for i in first["items"] + second["items"]]
    assert dates == sorted(dates, reverse=True)
    # 卡片带条目编号，后面"原文这一行"按它定位
    assert first["items"][0]["matched"]["entryId"]


def test_data_change_between_pages_returns_409(client, session):
    source = _source(session)
    for n in range(1, 23):
        _entry(session, _document(session, source, n), 1)
    session.commit()
    first = client.get(PAGE, params={"q": NAME, "identity": f"code:{CODE}"}).json()
    _entry(session, _document(session, source, 99), 1)
    session.commit()
    res = client.get(
        PAGE,
        params={
            "q": NAME,
            "identity": f"code:{CODE}",
            "cursor": first["nextCursor"],
            "revision": first["revision"],
        },
    )
    assert res.status_code == 409
    assert res.headers["cache-control"] == "no-store"


def test_cursor_is_signed_and_bound_to_query_and_identity(client, session):
    source = _source(session)
    for n in range(1, 23):
        _entry(session, _document(session, source, n), 1)
    session.commit()
    first = client.get(PAGE, params={"q": NAME, "identity": f"code:{CODE}"}).json()
    cursor, revision = first["nextCursor"], first["revision"]
    tampered = cursor[:-2] + ("A" if cursor[-2] != "A" else "B") + cursor[-1]
    for params in (
        {"q": NAME, "identity": f"code:{CODE}", "cursor": tampered, "revision": revision},
        {
            "q": "合成月河科技有限公司",
            "identity": f"code:{CODE}",
            "cursor": cursor,
            "revision": revision,
        },
        {"q": NAME, "identity": f"code:{_code(7)}", "cursor": cursor, "revision": revision},
        {"q": NAME, "identity": f"code:{CODE}", "cursor": cursor},
    ):
        res = client.get(PAGE, params=params)
        assert res.status_code == 422, params


def test_person_like_short_name_only_suggests_full_org_names(client, session):
    source = _source(session)
    _entry(session, _document(session, source, 1), 1)
    session.commit()
    body = client.get(IDENTITIES, params={"q": "星河"}).json()
    assert body["identityState"] == "none"
    assert body["identities"] == []
    assert NAME in body["candidates"]
    # 像个人姓名、又没有机构全称可建议的：不查
    assert client.get(IDENTITIES, params={"q": "张三"}).status_code == 422


def test_revision_moves_with_semantic_writes_only(session):
    source = _source(session)
    doc = _document(session, source, 1)
    session.commit()
    start = _revision(session)
    session.execute(
        text("UPDATE documents SET last_seen_at = :t WHERE id = 1"), {"t": datetime(2026, 10, 24)}
    )
    session.commit()
    assert _revision(session) == start  # 采集心跳不升
    _entry(session, doc, 1)
    session.commit()
    after_insert = _revision(session)
    assert after_insert > start
    session.execute(text("UPDATE documents SET title = '改过的标题名单公示' WHERE id = 1"))
    session.commit()
    assert _revision(session) > after_insert
    before_delete = _revision(session)
    session.execute(text("DELETE FROM list_entries WHERE document_id = 1"))
    session.commit()
    assert _revision(session) > before_delete
    assert session.scalar(select(ListEntry.id)) is None
