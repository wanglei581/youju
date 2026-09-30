"""本人核对撤回：时间边界、归并副本、质检保护与审计。"""

import json
import logging
import shutil
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.api import app as admin
from youju.db import get_session
from youju.dedup import sync_reviewed_twins
from youju.models import AccessLog, Document, DocumentDeadline

from .test_audit import add_doc, merge

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
HEADERS = {"X-Admin-Token": "undo-test-token"}


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


def candidate(session, doc=None, n=0):
    doc = doc or add_doc(session)
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=str(n).ljust(64, "x"),
        kind="企业申报截止",
        candidate_date=date(2026, 10, 15),
        candidate_time=time(17, 0),
        evidence="请于2026年10月15日前申报",
        confidence="高",
        status="pending",
    )
    session.add(row)
    session.commit()
    return row


def post(client, row, action, body=None, headers=HEADERS):
    body = (
        body
        if body is not None
        else ({"reviewer": "创始人"} if action == "undo" else {"reviewed_by": "创始人"})
    )
    return client.post(f"/api/admin/deadlines/{row.id}/{action}", json=body, headers=headers)


def assert_pending(session, row):
    session.refresh(row)
    assert row.status == "pending"
    assert row.reviewed_by is row.reviewed_at is row.approved_date is row.approved_time is None
    assert row.audit_state is row.audit_note is row.audit_at is row.audit_by is None
    assert row.candidate_date == date(2026, 10, 15)
    assert row.candidate_time == time(17, 0)


@pytest.mark.parametrize("action", ["approve", "edit", "reject"])
def test_undo_review_and_audit(client, session, caplog, monkeypatch, action):
    row = candidate(session)
    body = {"reviewed_by": "创始人"}
    if action == "edit":
        body.update(date="2026-10-20", time="12:30")
    assert post(client, row, "approve" if action == "edit" else action, body).status_code == 200
    session.refresh(row)
    before = row.status
    approved_date = row.approved_date
    monkeypatch.setattr(admin.audit_log, "propagate", True)
    caplog.set_level(logging.INFO, logger="youju.audit")
    response = post(client, row, "undo")
    assert response.status_code == 200 and response.json() == {"status": "pending"}
    assert_pending(session, row)
    records = [json.loads(record.getMessage()) for record in caplog.records]
    assert any(
        record.get("action") == "undo" and record["reviewer"] == "创始人" for record in records
    )
    log = session.scalar(select(AccessLog).where(AccessLog.path.endswith("/undo")))
    assert log.detail["action"] == "undo" and log.detail["count"] == 1
    assert log.detail["before"] == before and log.detail["reviewer"] == "创始人"
    assert log.detail["date"] == (str(approved_date) if approved_date else None)
    assert log.detail["reviewed_at"] and log.detail["deadline_id"] == row.id
    # 撤回不会留下质检退回标记，可以照常再次核对。
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
    response = post(client, row, "undo")
    assert response.status_code == (200 if allowed else 409)
    if allowed:
        assert_pending(session, row)
    else:
        assert "30 分钟" in response.json()["detail"]
        session.refresh(row)
        assert row.status == "approved"


@pytest.mark.parametrize(
    "state", ["other", "changed", "cleared", "reopened", "pending", "missing_time"]
)
def test_undo_conflicts(client, session, state):
    row = candidate(session)
    assert post(client, row, "approve").status_code == 200
    session.refresh(row)
    if state == "other":
        row.reviewed_by = "另一人"
    elif state == "changed":
        target = add_doc(session, 2)
        row.status = "changed"
        row.change_reason = "延期"
        row.changed_to_document_id = target.id
    elif state == "cleared":
        row.audit_state = "flagged"
        session.commit()
        assert post(client, row, "audit-ok").status_code == 200
        session.refresh(row)
    elif state == "reopened":
        assert post(client, row, "reopen").status_code == 200
        session.refresh(row)
    elif state == "pending":
        row.status = "pending"
    else:
        row.reviewed_at = None
    session.commit()
    before = (row.status, row.reviewed_by, row.reviewed_at, row.audit_state)
    response = post(client, row, "undo")
    assert response.status_code == 409
    assert any(
        word in response.json()["detail"] for word in ("核对人", "已变更", "质检", "已通过", "时间")
    )
    session.refresh(row)
    assert (row.status, row.reviewed_by, row.reviewed_at, row.audit_state) == before


def test_undo_auth_validation_and_missing(client, session):
    row = candidate(session)
    assert post(client, row, "undo", headers={}).status_code == 401
    assert post(client, row, "undo", {"reviewer": ""}).status_code == 422
    assert (
        client.post(
            "/api/admin/deadlines/9999/undo", headers=HEADERS, json={"reviewer": "创始人"}
        ).status_code
        == 404
    )


def twins(session):
    master = add_doc(session)
    copy = add_doc(session, 2)
    merge(session, master, copy)
    return candidate(session, master), candidate(session, copy)


@pytest.mark.parametrize("action", ["approve", "edit", "reject"])
def test_undo_merged_batch(client, session, action):
    row, twin = twins(session)
    body = {"reviewed_by": "创始人"}
    if action == "edit":
        body.update(date="2026-10-20", time=None)
    assert post(client, row, "approve" if action == "edit" else action, body).status_code == 200
    # 可以从同次操作的副本发起撤回。
    assert post(client, twin, "undo").status_code == 200
    assert_pending(session, row)
    assert_pending(session, twin)
    log = session.scalar(select(AccessLog).where(AccessLog.path.endswith("/undo")))
    assert log.detail["count"] == 2


@pytest.mark.parametrize("state", ["changed", "cleared"])
def test_protected_twin_blocks_whole_undo(client, session, state):
    row, twin = twins(session)
    assert post(client, row, "approve").status_code == 200
    session.refresh(twin)
    if state == "changed":
        target = add_doc(session, 3)
        twin.status = "changed"
        twin.change_reason = "延期"
        twin.changed_to_document_id = target.id
    else:
        twin.audit_state = state
    session.commit()
    assert post(client, row, "undo").status_code == 409
    session.refresh(row)
    assert row.status == "approved"


def test_reopened_group_cannot_be_undone(client, session):
    row, twin = twins(session)
    assert post(client, row, "approve").status_code == 200
    assert post(client, twin, "reopen").status_code == 200
    assert post(client, row, "undo").status_code == 409
    for item in (row, twin):
        session.refresh(item)
        assert item.status == "pending" and item.audit_state == "reopened"


def test_independent_reopened_twin_does_not_block_undo(client, session):
    row, twin = candidate(session), candidate(session, add_doc(session, 2))
    assert (
        post(client, row, "approve", {"reviewed_by": "创始人", "date": "2026-10-20"}).status_code
        == 200
    )
    assert (
        post(client, twin, "approve", {"reviewed_by": "另一人", "date": "2026-10-22"}).status_code
        == 200
    )
    merge(session, session.get(Document, row.document_id), session.get(Document, twin.document_id))
    assert post(client, twin, "reopen").status_code == 200
    assert post(client, row, "undo").status_code == 200
    assert_pending(session, row)
    session.refresh(twin)
    assert twin.status == "pending" and twin.audit_state == "reopened"


@pytest.mark.parametrize("same_reviewer", [False, True])
def test_independently_reviewed_twin_is_not_undone(client, session, same_reviewer):
    row, twin = twins(session)
    reviewer = "创始人" if same_reviewer else "另一人"
    assert post(client, twin, "approve", {"reviewed_by": reviewer}).status_code == 200
    session.refresh(twin)
    if same_reviewer:
        twin.reviewed_at = NOW - timedelta(minutes=1)
    # 模拟其中一条在归并前仍待核对，另一条已经独立核对。
    session.refresh(row)
    row.status = "pending"
    row.reviewed_by = row.reviewed_at = row.approved_date = row.approved_time = None
    session.commit()
    assert post(client, row, "approve").status_code == 200
    assert post(client, row, "undo").status_code == 200
    assert_pending(session, row)
    session.refresh(twin)
    assert twin.status == "approved" and twin.reviewed_by == reviewer


def test_same_timestamp_different_deadline_is_not_undone(client, session):
    row, twin = twins(session)
    twin.candidate_date = date(2026, 10, 20)
    twin.evidence = "另一项申报于2026年10月20日截止"
    session.commit()
    assert post(client, row, "approve").status_code == 200
    assert post(client, twin, "approve").status_code == 200
    assert post(client, row, "undo").status_code == 200
    assert_pending(session, row)
    session.refresh(twin)
    assert twin.status == "approved" and twin.approved_date == date(2026, 10, 20)


def test_undo_covers_later_synced_twin_with_different_evidence(client, session):
    row, twin = twins(session)
    twin.evidence = "申报系统将于2026年10月15日关闭"
    session.commit()
    assert post(client, row, "approve").status_code == 200
    session.refresh(twin)
    assert twin.status == "pending"
    master = session.get(Document, row.document_id)
    assert sync_reviewed_twins(session, {master.id}) == 1
    session.commit()
    assert post(client, row, "undo").status_code == 200
    assert_pending(session, row)
    assert_pending(session, twin)


def test_undo_flagged_but_not_confirmed_is_allowed(client, session):
    row = candidate(session)
    assert post(client, row, "approve").status_code == 200
    session.refresh(row)
    row.audit_state = "flagged"
    row.audit_note = "等待人工复核"
    row.audit_at = NOW
    session.commit()
    assert post(client, row, "undo").status_code == 200
    assert_pending(session, row)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_review_shortcuts_js():
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "js" / "review_shortcuts.cjs")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
