"""核对台按截止日期筛选：先核快到期的，已过期的核了也进不了日历。"""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentDeadline, DocumentMerge, Source

HEADERS = {"X-Admin-Token": "secret"}
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    monkeypatch.setattr(v1, "now", lambda: NOW)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def candidates(session):
    src = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url="https://kw.beijing.gov.cn/tz/",
        enabled=True,
        parser_config={},
    )
    session.add(src)
    session.flush()
    ids = {}
    for key, due in {
        "past": date(2026, 9, 27),  # 昨天，已过期
        "today": date(2026, 9, 28),  # 今天截止，算未过期
        "soon": date(2026, 10, 28),  # 第 30 天
        "later": date(2026, 10, 29),  # 第 31 天
    }.items():
        doc = Document(
            source_id=src.id,
            url=f"https://kw.beijing.gov.cn/{key}.html",
            url_key=f"kw.beijing.gov.cn/{key}.html",
            title=f"关于开展申报工作的通知（{key}）",
            publish_date=date(2026, 9, 1),
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        session.add(doc)
        session.flush()
        row = DocumentDeadline(
            document_id=doc.id,
            fingerprint=key.ljust(64, "x"),
            kind="企业申报截止",
            candidate_date=due,
            evidence="截止",
            confidence="高",
            status="pending",
        )
        session.add(row)
        session.flush()
        ids[key] = row.id
    session.commit()
    return ids


def _listed(client, due=None):
    params = {"limit": 50} | ({"due": due} if due else {})
    response = client.get("/api/admin/deadlines", headers=HEADERS, params=params)
    assert response.status_code == 200
    return {item["id"] for item in response.json()}


def _count(client, due=None):
    params = {"due": due} if due else {}
    response = client.get("/api/admin/deadlines/count", headers=HEADERS, params=params)
    assert response.status_code == 200
    return response.json()["pending"]


def test_pending_can_be_filtered_by_due_date(client, candidates) -> None:
    ids = candidates
    # 默认全部（和以前一样）
    assert _listed(client) == set(ids.values()) and _count(client) == 4
    # 30 天内：今天到第 30 天，含两端
    assert _listed(client, "30") == {ids["today"], ids["soon"]} and _count(client, "30") == 2
    # 未过期：今天及以后
    assert _listed(client, "future") == {ids["today"], ids["soon"], ids["later"]}
    assert _count(client, "future") == 3
    assert _listed(client, "all") == set(ids.values())
    bad = client.get("/api/admin/deadlines", headers=HEADERS, params={"due": "7"})
    assert bad.status_code == 422


def test_twin_candidates_are_listed_and_counted_once_under_filter(client, session, candidates):
    # 同一通知归并出的两个网址各有一条相同候选（类型、日期、时间、证据句都相同）：日期一样，
    # 筛选时要么都在要么都不在，只列编号小的那条、只算一条
    soon = session.get(DocumentDeadline, candidates["soon"])
    master = session.get(Document, soon.document_id)
    twin_doc = Document(
        source_id=master.source_id,
        url="https://kw.beijing.gov.cn/soon-copy.html",
        url_key="kw.beijing.gov.cn/soon-copy.html",
        title=master.title,
        publish_date=master.publish_date,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(twin_doc)
    session.flush()
    session.add(
        DocumentMerge(duplicate_id=twin_doc.id, canonical_id=master.id, active=True, match_key="k")
    )
    twin = DocumentDeadline(
        document_id=twin_doc.id,
        fingerprint="twin".ljust(64, "x"),
        kind=soon.kind,
        candidate_date=soon.candidate_date,
        candidate_time=soon.candidate_time,
        evidence=soon.evidence,
        confidence="高",
        status="pending",
    )
    session.add(twin)
    session.commit()
    assert twin.id not in _listed(client, "30") and soon.id in _listed(client, "30")
    assert _count(client, "30") == 2  # 今天 + 第 30 天（两个副本只算一条）
    assert _count(client) == 4


def test_paging_under_filter_misses_nothing(client, candidates):
    # 每页一条，按编号游标翻完：30 天内的两条都出现且只出现一次
    seen, before = [], None
    while True:
        params = {"limit": 1, "due": "30"} | ({"before_id": before} if before else {})
        page = client.get("/api/admin/deadlines", headers=HEADERS, params=params).json()
        if not page:
            break
        seen += [item["id"] for item in page]
        before = min(item["id"] for item in page)
    assert sorted(seen) == sorted([candidates["today"], candidates["soon"]])
