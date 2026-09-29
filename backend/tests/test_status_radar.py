from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentDeadline, DocumentDetail, Source

BJ = ZoneInfo("Asia/Shanghai")


def test_application_status_boundaries(monkeypatch):
    def deadline(due, clock=None):
        return SimpleNamespace(kind="企业申报截止", approved_date=due, approved_time=clock, id=1)

    monkeypatch.setattr(v1, "now", lambda: datetime(2026, 9, 29, 10, 0, tzinfo=BJ))
    assert v1.application_status([deadline(date(2026, 9, 29))])["code"] == "closing"
    assert v1.application_status([deadline(date(2026, 9, 29), time(10, 0))])["code"] == "closing"
    assert v1.application_status([deadline(date(2026, 9, 29), time(9, 59))])["code"] == "closed"
    assert v1.application_status([deadline(date(2026, 10, 6))])["code"] == "closing"
    assert v1.application_status([deadline(date(2026, 10, 8))])["code"] == "open"


@pytest.fixture
def radar_client(session_factory, monkeypatch):
    now = datetime(2026, 9, 29, 10, 0, tzinfo=BJ)
    monkeypatch.setattr(v1, "now", lambda: now)
    source = Source(
        level="省级",
        province="辽宁",
        dept="工信",
        dept_full_name="辽宁省工业和信息化厅",
        column_name="通知",
        url="https://example.gov.cn/notice",
        rights_level=1,
    )
    session = session_factory()
    session.add(source)
    session.flush()
    docs = []
    for index, (due, clock, state) in enumerate(
        [
            (date(2026, 10, 3), None, "approved"),
            (date(2026, 10, 20), None, "approved"),
            (date(2026, 10, 4), None, "pending"),
        ],
        1,
    ):
        doc = Document(
            source_id=source.id,
            url=f"https://example.gov.cn/{index}",
            url_key=f"example/{index}",
            title=f"辽宁项目申报通知 {index}",
            publish_date=date(2026, 9, 20),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(doc)
        session.flush()
        session.add(
            DocumentDetail(
                document_id=doc.id,
                issuer=source.dept_full_name,
                doc_number=f"辽〔{index}〕号",
                fetched_at=now,
                status="ok",
            )
        )
        session.add(
            DocumentDeadline(
                document_id=doc.id,
                fingerprint=str(index),
                kind="企业申报截止",
                candidate_date=due,
                approved_date=due if state == "approved" else None,
                approved_time=clock,
                evidence="企业申报截止",
                confidence="高",
                status=state,
            )
        )
        docs.append(doc.id)
    session.commit()

    def override():
        yield session

    app.dependency_overrides[get_session] = override
    v1._REPORT_CACHE.clear()
    yield TestClient(app), docs
    app.dependency_overrides.clear()
    session.close()


def test_open_filter_and_report_only_verified(radar_client):
    client, docs = radar_client
    result = client.get(
        "/api/v1/search", params={"kind": "申报通知", "time": "不限", "open": 1}
    ).json()
    assert result["total"] == 2 and {int(item["id"]) for item in result["items"]} == set(docs[:2])
    assert all(isinstance(item["status"], dict) for item in result["items"])
    report = client.get("/api/v1/reports/deadlines", params={"days": 7, "size": 1}).json()
    assert report["total"] == 1 and report["summary"] == {"7": 1, "14": 1, "30": 2}
    assert report["items"][0]["deadline_type"] == "企业申报截止"


def test_report_cache_can_be_cleared(radar_client, monkeypatch):
    client, _ = radar_client
    calls = 0
    original = v1._report_payload

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(v1, "_report_payload", counted)
    client.get("/api/v1/reports/deadlines", params={"days": 7})
    client.get("/api/v1/reports/deadlines", params={"days": 7})
    assert calls == 1
    v1._REPORT_CACHE.clear()
    client.get("/api/v1/reports/deadlines", params={"days": 7})
    assert calls == 2
