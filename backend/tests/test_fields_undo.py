"""字段本人撤回：时间边界、原文变更保护、审计及页面交互。"""

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.api import app as admin
from youju.db import get_session
from youju.models import AccessLog, DocumentDetail

from .test_fields_integration import add_document, add_field

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
HEADERS = {"X-Admin-Token": "field-undo-test-token"}


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    admin.app.dependency_overrides[get_session] = override
    monkeypatch.setattr(admin, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        admin, "get_settings", lambda: SimpleNamespace(admin_token=HEADERS["X-Admin-Token"])
    )
    try:
        with TestClient(admin.app) as client:
            yield client
    finally:
        admin.app.dependency_overrides.clear()


def candidate(session):
    return add_field(session, add_document(session).id, status="pending")


def post(client, row, action="undo", body=None, headers=HEADERS):
    if body is None:
        body = {"reviewer" if action == "undo" else "reviewed_by": "创始人"}
    return client.post(f"/api/admin/fields/{row.id}/{action}", json=body, headers=headers)


@pytest.mark.parametrize("action", ["approve", "edit", "reject"])
def test_undo_review_and_audit(client, session, caplog, monkeypatch, action):
    row = candidate(session)
    original = (row.value, row.evidence, row.evidence_offset, row.body_hash, row.source)
    sibling = add_field(session, row.document_id, value="资金支持")
    body = {"reviewed_by": "创始人"}
    if action == "edit":
        body["value"] = "资金支持"
    assert post(client, row, "approve" if action == "edit" else action, body).status_code == 200
    session.refresh(row)
    before, approved_value, reviewed_at = row.status, row.approved_value, row.reviewed_at
    monkeypatch.setattr(admin.audit_log, "propagate", True)
    caplog.set_level(logging.INFO, logger="youju.audit")
    response = post(client, row)
    assert response.status_code == 200 and response.json() == {"status": "pending"}
    session.refresh(row)
    assert row.status == "pending"
    assert row.reviewed_by is row.reviewed_at is row.approved_value is None
    assert (row.value, row.evidence, row.evidence_offset, row.body_hash, row.source) == original
    session.refresh(sibling)
    assert sibling.status == "approved"
    records = [json.loads(record.getMessage()) for record in caplog.records]
    assert any(r.get("action") == "field_undo" and r["reviewer"] == "创始人" for r in records)
    log = session.scalar(select(AccessLog).where(AccessLog.path.endswith("/undo")))
    assert log.detail == {
        "action": "field_undo",
        "field_id": row.id,
        "document_id": row.document_id,
        "reviewer": "创始人",
        "before": before,
        "value": row.value,
        "approved_value": approved_value,
        "reviewed_at": str(reviewed_at),
    }
    assert post(client, row).status_code == 409
    assert post(client, row, "approve").status_code == 200


@pytest.mark.parametrize(
    "age,allowed",
    [
        (timedelta(minutes=29, seconds=59), True),
        (timedelta(minutes=30), True),
        (timedelta(minutes=30, microseconds=1), False),
        (timedelta(seconds=-1), False),
    ],
)
def test_undo_time_boundary(client, session, age, allowed):
    row = candidate(session)
    assert post(client, row, "approve").status_code == 200
    session.refresh(row)
    row.reviewed_at = NOW - age
    session.commit()
    response = post(client, row)
    assert response.status_code == (200 if allowed else 409)
    session.refresh(row)
    assert row.status == ("pending" if allowed else "approved")
    if not allowed:
        assert "30 分钟" in response.json()["detail"]


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("state", ["other", "pending", "stale", "missing_time", "body_changed"])
def test_undo_conflicts_preserve_record(client, session, action, state):
    row = candidate(session)
    assert post(client, row, action).status_code == 200
    session.refresh(row)
    if state == "other":
        row.reviewed_by = "另一人"
    elif state in ("pending", "stale"):
        row.status = state
    elif state == "missing_time":
        row.reviewed_at = None
    else:
        session.get(DocumentDetail, row.document_id).body_text += "原文新增条件。"
    session.commit()
    before = (row.status, row.reviewed_by, row.reviewed_at, row.approved_value)
    response = post(client, row)
    assert response.status_code == 409
    assert any(
        word in response.json()["detail"] for word in ("核对人", "已通过", "原文已变化", "时间")
    )
    session.refresh(row)
    assert (row.status, row.reviewed_by, row.reviewed_at, row.approved_value) == before
    assert (
        session.scalar(
            select(AccessLog).where(AccessLog.detail["action"].as_string() == "field_undo")
        )
        is None
    )


def test_undo_auth_validation_and_missing(client, session):
    row = candidate(session)
    assert post(client, row, "approve").status_code == 200
    assert post(client, row, headers={}).status_code == 401
    assert post(client, row, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert post(client, row, body={"reviewer": ""}).status_code == 422
    assert post(client, row, body={}).status_code == 422
    assert post(client, row, body={"reviewer": "x" * 81}).status_code == 422
    assert (
        client.post(
            "/api/admin/fields/9999/undo", headers=HEADERS, json={"reviewer": "创始人"}
        ).status_code
        == 404
    )
    session.refresh(row)
    assert row.status == "approved"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_fields_shortcuts_js():
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "js" / "fields_shortcuts.cjs")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
