"""字段重抽、公开读取、核对队列和雷达的回归测试，仅使用测试夹具的专用库。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from youju import fields
from youju.api import app as app_module
from youju.api import v1
from youju.db import get_session
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    DocumentMerge,
    Source,
)

BODY = "申报对象：本省企业。\n须正常经营。企业可申请贷款贴息。"
STAMP = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


def add_document(session, number=1, rights=1, body=BODY, deadline_status="approved"):
    source = Source(
        level="省级",
        province="辽宁",
        dept="科技",
        dept_full_name="辽宁省科技厅",
        column_name="通知",
        url=f"https://example.gov.cn/source/{number}",
        rights_level=rights,
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        url=f"https://example.gov.cn/{number}",
        url_key=f"example.gov.cn/{number}",
        title=f"关于组织申报科技项目的通知{number}",
        first_seen_at=STAMP,
        last_seen_at=STAMP,
        publish_date=date(2026, 9, 29),
    )
    session.add(doc)
    session.flush()
    session.add_all(
        [
            DocumentDetail(document_id=doc.id, body_text=body, fetched_at=STAMP, status="ok"),
            DocumentDeadline(
                document_id=doc.id,
                fingerprint=str(number),
                kind="企业申报截止",
                candidate_date=date(2026, 10, 3),
                approved_date=date(2026, 10, 3) if deadline_status == "approved" else None,
                evidence="申报截至10月3日。",
                confidence="高",
                status=deadline_status,
            ),
        ]
    )
    session.commit()
    return doc


def add_field(session, doc_id, *, field="support", value="贷款贴息", status="approved", body=BODY):
    row = DocumentField(
        document_id=doc_id,
        field=field,
        value=value,
        evidence="企业可申请贷款贴息。",
        evidence_offset=BODY.index("企业可申请"),
        body_hash=fields.body_fingerprint(body),
        status=status,
        source="rule",
        approved_value=value if status == "approved" else None,
        reviewed_by="创始人" if status == "approved" else None,
        reviewed_at=STAMP if status == "approved" else None,
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(v1, "now", lambda: STAMP)
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(admin_token="secret"))
    app_module.app.dependency_overrides[get_session] = override
    v1._REPORT_CACHE.clear()
    with TestClient(app_module.app) as client:
        yield client
    app_module.app.dependency_overrides.clear()
    v1._REPORT_CACHE.clear()


def test_body_change_hidden_immediately_without_reextraction_or_cache_clear(session, client):
    doc = add_document(session)
    add_field(session, doc.id)
    add_field(session, doc.id, field="audience", value="本省企业。")
    detail_url = f"/api/v1/documents/{doc.id}"
    radar_url = "/api/v1/reports/deadlines?days=7&support=贷款贴息"
    assert client.get(detail_url).json()["fields"] == {
        "support": ["贷款贴息"],
        "support_evidence": {"贷款贴息": "企业可申请贷款贴息。"},
    }
    assert client.get(radar_url).json()["total"] == 1
    session.get(DocumentDetail, doc.id).body_text += "\n补充条件。"
    session.commit()
    assert "fields" not in client.get(detail_url).json()
    assert client.get(radar_url).json()["total"] == 0
    # GET 不写库；逻辑上 stale 的已核字段要等重抽后重新送审。
    assert set(session.scalars(select(DocumentField.status))) == {"approved"}


def test_disabled_audience_is_not_generated_or_deleted(session):
    doc = add_document(session)
    old = add_field(session, doc.id, field="audience", value="本省企业。", status="pending")
    detail = session.get(DocumentDetail, doc.id)
    detail.body_text += "正文发生变化。"
    session.commit()
    report = fields.extract_existing(session, document_ids=[doc.id])
    assert report.fields == 1
    session.refresh(old)
    assert old.status == "pending"
    assert session.get(DocumentField, old.id) is not None


@pytest.mark.parametrize("status", ["pending", "rejected", "stale"])
def test_unapproved_fields_never_public(session, client, status):
    doc = add_document(session)
    add_field(session, doc.id, status=status)
    assert "fields" not in client.get(f"/api/v1/documents/{doc.id}").json()
    assert client.get("/api/v1/reports/deadlines?support=贷款贴息").json()["total"] == 0


@pytest.mark.parametrize("rights", [0, 2, 3])
@pytest.mark.parametrize("public_is_master", [True, False])
def test_merged_fields_must_come_from_public_member(session, client, rights, public_is_master):
    master = add_document(session, 1, rights=1 if public_is_master else rights)
    twin = add_document(session, 2, rights=rights if public_is_master else 1)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="k")
    )
    session.commit()
    public, private = (master, twin) if public_is_master else (twin, master)
    add_field(session, private.id)
    add_field(session, private.id, field="audience", value="非公开来源摘录")
    url = f"/api/v1/documents/{master.id}"
    response = client.get(url)
    assert response.status_code == 200
    assert "fields" not in response.json()
    assert client.get("/api/v1/reports/deadlines?support=贷款贴息").json()["total"] == 0
    add_field(session, public.id)
    assert client.get(url).json()["fields"] == {
        "support": ["贷款贴息"],
        "support_evidence": {"贷款贴息": "企业可申请贷款贴息。"},
    }
    radar = client.get("/api/v1/reports/deadlines?support=贷款贴息").json()
    assert radar["total"] == 1
    assert radar["items"][0]["id"] == str(master.id)


def test_reextract_preserves_stale_review_and_is_idempotent(session):
    doc = add_document(session)
    row = add_field(session, doc.id)
    row.approved_value = "资金支持"
    session.commit()
    detail = session.get(DocumentDetail, doc.id)
    detail.body_text += "补充条件。"
    session.commit()
    report = fields.extract_existing(session)
    assert report.changed == 1
    session.refresh(row)
    assert row.status == "stale"
    assert row.body_hash == fields.body_fingerprint(detail.body_text)
    assert (row.approved_value, row.reviewed_by, row.reviewed_at) == ("资金支持", "创始人", STAMP)
    before = [(r.id, r.status, r.body_hash) for r in session.scalars(select(DocumentField))]
    assert fields.extract_existing(session).fields == 0
    assert [(r.id, r.status, r.body_hash) for r in session.scalars(select(DocumentField))] == before
    # 再次变更、甚至支持方式从正文消失，历史核对记录仍保留为 stale。
    detail.body_text = "申报对象：本省企业。条件另见附件。"
    session.commit()
    fields.extract_existing(session)
    session.refresh(row)
    assert row.status == "stale" and row.approved_value == "资金支持"
    assert session.scalar(select(DocumentField).where(DocumentField.id == row.id)) is not None


def test_concurrent_extraction_uses_upsert_without_duplicate_key(
    session, session_factory, monkeypatch
):
    doc = add_document(session)
    barrier = Barrier(2)
    original = fields.extract_rules

    def synchronize(body):
        result = original(body)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(fields, "extract_rules", synchronize)

    def run():
        with session_factory() as worker:
            return fields.extract_existing(worker, document_ids=[doc.id])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert [r.fields for r in results] == [1, 1]
    rows = session.scalars(select(DocumentField)).all()
    assert len(rows) == 1
    assert all(r.status == "pending" and r.body_hash == fields.body_fingerprint(BODY) for r in rows)


def test_manual_audience_edit_can_differ_from_original(session, client):
    doc = add_document(session)
    row = add_field(session, doc.id, field="audience", value="本省企业。", status="pending")
    edited = "创始人逐项核实后的申报对象"
    response = client.post(
        f"/api/admin/fields/{row.id}/approve",
        headers={"X-Admin-Token": "secret"},
        json={"reviewed_by": "创始人", "value": edited},
    )
    assert response.status_code == 200
    assert "fields" not in client.get(f"/api/v1/documents/{doc.id}").json()


def test_support_evidence_only_contains_approved_support_rows(session, client):
    doc = add_document(session)
    add_field(session, doc.id, value="贷款贴息")
    add_field(session, doc.id, value="资金支持", status="pending")
    payload = client.get(f"/api/v1/documents/{doc.id}").json()["fields"]
    assert payload["support"] == ["贷款贴息"]
    assert payload["support_evidence"] == {"贷款贴息": "企业可申请贷款贴息。"}


def test_queue_query_count_does_not_grow_with_documents(session, monkeypatch):
    monkeypatch.setattr(v1, "now", lambda: STAMP)
    doc = add_document(session, deadline_status="pending")
    add_field(session, doc.id, status="pending")
    add_field(session, doc.id, field="audience", value="本省企业。", status="pending")

    def measured_queue():
        statements = []

        def count(_conn, _cursor, statement, _params, _ctx, _many):
            statements.append(statement)

        event.listen(session.bind, "before_cursor_execute", count)
        try:
            cards = app_module._field_queue(session)
        finally:
            event.remove(session.bind, "before_cursor_execute", count)
        return cards, len(statements)

    cards, small_count = measured_queue()
    assert len(cards) == 1 and cards[0].deadline == date(2026, 10, 3)
    assert all(item.field == "support" for item in cards[0].fields)
    for number in range(2, 7):
        doc = add_document(session, number, deadline_status="pending")
        add_field(session, doc.id, status="pending")
    cards, large_count = measured_queue()
    assert len(cards) == 6
    assert large_count == small_count


def test_radar_support_is_filtered_in_sql_and_uses_reviewed_value(session, monkeypatch):
    monkeypatch.setattr(v1, "now", lambda: STAMP)
    wanted = add_document(session, 1)
    other = add_document(session, 2)
    row = add_field(session, wanted.id)
    row.approved_value = "资金支持"
    add_field(session, other.id)
    statements = []
    original = v1._approved

    def approved_for_selected(worker, ids):
        assert ids == [wanted.id]
        return original(worker, ids)

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    monkeypatch.setattr(v1, "_approved", approved_for_selected)
    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        result = v1._report_payload(session, 7, "", "", "资金支持", 1, 20)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert result["total"] == 1
    assert any("EXISTS" in sql and "document_fields" in sql and "sha2" in sql for sql in statements)
