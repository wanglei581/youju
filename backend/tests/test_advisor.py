"""顾问任务接口、队列状态和名单保存期限。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju import advisor_tasks as service
from youju.api import advisor as advisor_api
from youju.api import me, v1
from youju.api.app import app
from youju.db import get_session
from youju.models import AdvisorFileTrash, AdvisorTask, AdvisorUsage, User

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 30, 23, 59, tzinfo=BJ)


@pytest.fixture
def advisor_settings(monkeypatch):
    settings = SimpleNamespace(
        advisor_enabled=True,
        cloud_env="test-env",
        dev_openid="advisor-user",
        trust_wx_headers=False,
        agreement_version="2026-09",
        retain_events_days=90,
        retain_feedback_days=180,
        retain_used_subscriptions_days=30,
        retain_unused_subscriptions_days=365,
    )
    monkeypatch.setattr(advisor_api, "get_settings", lambda: settings)
    monkeypatch.setattr(me, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    return settings


@pytest.fixture
def client(session_factory, advisor_settings):
    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def agree(client):
    response = client.post("/api/v1/me/agreement", json={"version": "2026-09"})
    assert response.status_code == 200


def create(client, key="k1", rows=None):
    return client.post(
        "/api/v1/advisor/tasks",
        headers={"Idempotency-Key": key},
        json={"kind": "compare", "rows": rows or ["甲公司"]},
    )


def task(session, user_id, **values):
    status = values.pop("status", "queued")
    row = AdvisorTask(
        user_id=user_id,
        kind="compare",
        status=status,
        input_rows=["客户秘密"],
        row_count=1,
        idempotency_key=values.pop("idempotency_key", f"direct-{user_id}"),
        created_at=values.pop("created_at", NOW.astimezone(UTC)),
        expires_at=values.pop("expires_at", NOW.astimezone(UTC) + timedelta(days=30)),
        **values,
    )
    session.add(row)
    session.commit()
    return row


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class FakeObjectStore:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/tcb/uploadfile"):
            return FakeResponse(
                {
                    "errcode": 0,
                    "url": "https://cos.test/upload",
                    "authorization": "sig",
                    "token": "token",
                    "cos_file_id": "cos-file",
                    "file_id": "cloud://advisor-result",
                }
            )
        if url.endswith("/tcb/batchdownloadfile"):
            return FakeResponse(
                {"errcode": 0, "download_list": [{"status": 0, "download_url": "https://download"}]}
            )
        return FakeResponse({"errcode": 0})


def test_switch_idempotency_quota_rollover_and_cancel(
    client, session, advisor_settings, monkeypatch
):
    advisor_settings.advisor_enabled = False
    assert client.get("/api/v1/advisor/tasks").status_code == 404
    advisor_settings.advisor_enabled = True
    agree(client)
    assert create(client, key="missing").status_code == 202
    duplicate = create(client, key="missing", rows=["不同内容"])
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == 1 and duplicate.json()["rowCount"] == 1
    assert (
        client.post("/api/v1/advisor/tasks", json={"kind": "compare", "rows": ["x"]}).status_code
        == 422
    )
    assert create(client, key="second").status_code == 429

    cancelled = client.post("/api/v1/advisor/tasks/1/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert create(client, key="after-cancel").status_code == 202

    # 处理失败的任务不算用掉额度。
    session.get(AdvisorTask, 2).status = "failed"
    session.commit()
    assert create(client, key="after-failed").status_code == 202

    # 北京时间跨日后恢复当天一次免费额度。
    monkeypatch.setattr(v1, "now", lambda: datetime(2026, 10, 1, 0, 1, tzinfo=BJ))
    assert create(client, key="next-day").status_code == 202


def test_row_and_body_limits_and_personal_data(client):
    agree(client)
    assert create(client, key="too-many", rows=["x"] * 51).status_code == 429
    huge = "x" * (service.MAX_BODY_BYTES + 1)
    response = create(client, key="huge", rows=[huge])
    assert response.status_code == 413
    data = client.get("/api/v1/me/data").json()
    assert data["advisorTasksTotal"] == 0
    assert data["advisorTasks"] == []
    created = create(client, key="private").json()
    data = client.get("/api/v1/me/data").json()
    assert data["advisorTasksTotal"] == 1
    assert "inputRows" not in str(data) and "客户秘密" not in str(data)
    assert data["advisorTasks"][0]["id"] == created["id"]


def test_task_ownership_for_read_cancel_delete_download(
    client, advisor_settings, session, monkeypatch
):
    agree(client)
    created = create(client, key="owned").json()
    task_id = created["id"]
    user = session.scalar(select(User).where(User.openid == "advisor-user"))
    row = session.get(AdvisorTask, task_id)
    row.status, row.result_file_id = "done", "cloud://owned"
    session.commit()
    deleted = []
    monkeypatch.setattr(
        service, "delete_result_files", lambda client, env, ids: deleted.extend(ids) or 1
    )

    advisor_settings.dev_openid = "other-user"
    agree(client)
    assert client.get(f"/api/v1/advisor/tasks/{task_id}").status_code == 404
    assert client.post(f"/api/v1/advisor/tasks/{task_id}/cancel").status_code == 404
    assert client.get(f"/api/v1/advisor/tasks/{task_id}/download").status_code == 404
    assert client.delete(f"/api/v1/advisor/tasks/{task_id}").status_code == 404

    advisor_settings.dev_openid = "advisor-user"
    monkeypatch.setattr(service, "download_url", lambda client, env, file_id: "https://download")
    row.expires_at = NOW.astimezone(UTC) + timedelta(minutes=1)
    session.commit()
    downloaded = client.get(f"/api/v1/advisor/tasks/{task_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.json() == {"url": "https://download", "expiresIn": 600}
    row.expires_at = NOW.astimezone(UTC)
    session.commit()
    assert client.get(f"/api/v1/advisor/tasks/{task_id}/download").status_code == 410
    assert client.delete(f"/api/v1/advisor/tasks/{task_id}").status_code == 204
    assert deleted == ["cloud://owned"]
    session.expire_all()
    assert session.get(AdvisorTask, task_id) is None
    assert user is not None


def test_queue_state_stale_recovery_and_retry(session, advisor_settings, monkeypatch):
    user = User(openid="queue-user", agreement_version="2026-09", agreement_at=NOW)
    session.add(user)
    session.commit()
    queued = task(session, user.id)
    monkeypatch.setattr(
        service, "_run_engine", lambda session, task, output: output.write_bytes(b"x") or 1
    )
    monkeypatch.setattr(service, "upload", lambda *args, **kwargs: "cloud://done")
    report = service.run_advisor_tasks(
        session, settings=advisor_settings, stamp=NOW.astimezone(UTC), max_tasks=1
    )
    session.refresh(queued)
    assert report["done"] == 1 and queued.status == "done" and queued.attempts == 1

    stale = task(
        session,
        user.id,
        idempotency_key="stale",
        status="running",
        started_at=(NOW - timedelta(minutes=11)).astimezone(UTC),
    )
    assert service.recover_stale(session, NOW.astimezone(UTC)) == 1
    session.refresh(stale)
    assert stale.status == "queued" and stale.started_at is None
    stale.status = "cancelled"
    session.commit()

    failing = task(session, user.id, idempotency_key="failing")
    monkeypatch.setattr(
        service, "_run_engine", lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    for attempts in (1, 2):
        service.run_advisor_tasks(
            session, settings=advisor_settings, stamp=NOW.astimezone(UTC), max_tasks=1
        )
        session.refresh(failing)
        assert failing.attempts == attempts and failing.status == "queued"
    service.run_advisor_tasks(
        session, settings=advisor_settings, stamp=NOW.astimezone(UTC), max_tasks=1
    )
    session.refresh(failing)
    assert failing.attempts == 3 and failing.status == "failed"


def test_fake_object_store_upload_download_and_expiry(session, advisor_settings, monkeypatch):
    user = User(openid="file-user", agreement_version="2026-09", agreement_at=NOW)
    session.add(user)
    session.commit()
    row = task(session, user.id, idempotency_key="file")
    monkeypatch.setattr(
        service, "_run_engine", lambda session, task, output: output.write_bytes(b"x") or 2
    )
    store = FakeObjectStore()
    assert (
        service.process_task(session, row, settings=advisor_settings, client=store, stamp=NOW)
        is True
    )
    session.refresh(row)
    assert row.status == "done" and row.result_file_id == "cloud://advisor-result"
    assert service.download_url(store, "test-env", row.result_file_id) == "https://download"

    row.expires_at = NOW.astimezone(UTC)
    session.commit()
    assert row.expires_at <= NOW.astimezone(UTC)


def test_cleanup_ignores_switch_and_deletes_expired_files(session, advisor_settings):
    user = User(openid="cleanup-user")
    session.add(user)
    session.commit()
    old = task(
        session,
        user.id,
        idempotency_key="old",
        created_at=(NOW - timedelta(days=30)).astimezone(UTC),
        expires_at=NOW.astimezone(UTC),
        result_file_id="cloud://old",
    )
    advisor_settings.advisor_enabled = False
    deleted = []
    client = SimpleNamespace()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        service, "delete_result_files", lambda client, env, ids: deleted.extend(ids) or len(ids)
    )
    result = service.cleanup_advisor(
        session,
        settings=advisor_settings,
        stamp=NOW.astimezone(UTC),
        client=client,
    )
    monkeypatch.undo()
    assert result["advisor_input_rows"] == 1 and result["advisor_files"] == 1
    session.refresh(old)
    assert old.input_rows is None and old.result_file_id is None and deleted == ["cloud://old"]


def test_delete_account_removes_advisor_tasks_and_files(
    client, advisor_settings, session, monkeypatch
):
    agree(client)
    created = create(client, key="logout").json()
    row = session.get(AdvisorTask, created["id"])
    row.status, row.result_file_id = "done", "cloud://logout"
    session.commit()
    deleted = []
    monkeypatch.setattr(
        service, "delete_result_files", lambda client, env, ids: deleted.extend(ids) or 1
    )
    assert client.delete("/api/v1/me").status_code == 204
    assert deleted == ["cloud://logout"]
    session.expire_all()
    assert session.get(AdvisorTask, created["id"]) is None


def test_delete_terminal_task_keeps_usage_and_queued_delete_refunds(
    client, advisor_settings, session
):
    agree(client)
    created = create(client, key="ledger").json()
    usage = session.scalar(select(AdvisorUsage))
    assert usage is not None and usage.used_count == 1
    queued_delete = client.delete(f"/api/v1/advisor/tasks/{created['id']}")
    # 排队中的任务一步取消并删除，额度退回
    assert queued_delete.status_code == 204
    session.expire_all()
    assert session.get(AdvisorTask, created["id"]) is None
    assert session.scalar(select(AdvisorUsage.used_count)) == 0

    created = create(client, key="ledger-done").json()
    row = session.get(AdvisorTask, created["id"])
    row.status = "done"
    session.commit()
    assert client.delete(f"/api/v1/advisor/tasks/{created['id']}").status_code == 204
    assert session.scalar(select(AdvisorUsage.used_count)) == 1
    assert create(client, key="ledger-blocked").status_code == 429


def test_storage_failure_is_queued_and_retried(session, advisor_settings, monkeypatch):
    user = User(openid="trash-user", agreement_version="2026-09", agreement_at=NOW)
    session.add(user)
    session.commit()
    row = task(
        session,
        user.id,
        idempotency_key="trash",
        expires_at=NOW.astimezone(UTC),
        result_file_id="cloud://trash",
    )
    attempts = iter([RuntimeError("暂时失败"), ["cloud://trash"]])
    monkeypatch.setattr(service, "delete_result_files", lambda *args: next(attempts))
    first = service.cleanup_advisor(
        session, settings=advisor_settings, stamp=NOW.astimezone(UTC), client=SimpleNamespace()
    )
    assert first["advisor_files"] == 0
    session.refresh(row)
    assert row.result_file_id == "cloud://trash"
    assert session.scalar(select(AdvisorFileTrash.file_id)) == "cloud://trash"

    second = service.cleanup_advisor(
        session,
        settings=advisor_settings,
        stamp=(NOW + timedelta(days=1)).astimezone(UTC),
        client=SimpleNamespace(),
    )
    assert second["advisor_files"] == 1
    session.refresh(row)
    assert row.result_file_id is None
    assert session.scalar(select(AdvisorFileTrash.file_id)) is None


def test_delete_and_logout_succeed_when_storage_delete_fails(
    client, advisor_settings, session, monkeypatch
):
    agree(client)
    created = create(client, key="api-trash").json()
    row = session.get(AdvisorTask, created["id"])
    row.status, row.result_file_id = "done", "cloud://api-trash"
    session.commit()
    monkeypatch.setattr(
        service, "delete_result_files", lambda *args: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert client.delete(f"/api/v1/advisor/tasks/{created['id']}").status_code == 204
    assert session.scalar(select(AdvisorFileTrash.file_id)) == "cloud://api-trash"

    created = client.post(
        "/api/v1/advisor/tasks",
        headers={"Idempotency-Key": "logout-trash"},
        json={"kind": "track", "rows": [{"name": "客户"}]},
    ).json()
    row = session.get(AdvisorTask, created["id"])
    row.status, row.result_file_id = "done", "cloud://logout-trash"
    session.commit()
    assert client.delete("/api/v1/me").status_code == 204
    assert (
        session.scalar(
            select(AdvisorFileTrash.file_id).where(
                AdvisorFileTrash.file_id == "cloud://logout-trash"
            )
        )
        == "cloud://logout-trash"
    )


def test_uploaded_file_is_deleted_or_queued_when_task_times_out(
    session, advisor_settings, monkeypatch
):
    user = User(openid="timeout-user", agreement_version="2026-09", agreement_at=NOW)
    session.add(user)
    session.commit()
    row = task(session, user.id, idempotency_key="timeout", status="running")
    monkeypatch.setattr(
        service, "_run_engine", lambda session, task, output: output.write_bytes(b"x") or 1
    )
    monkeypatch.setattr(service, "upload", lambda *args, **kwargs: "cloud://timeout")
    monkeypatch.setattr(
        service, "delete_result_files", lambda *args: (_ for _ in ()).throw(RuntimeError("down"))
    )
    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(service.monotonic_clock, "monotonic", lambda: next(clock))
    assert (
        service.process_task(
            session,
            row,
            settings=advisor_settings,
            client=SimpleNamespace(),
            stamp=NOW.astimezone(UTC),
            timeout=timedelta(seconds=1),
        )
        is False
    )
    assert session.scalar(select(AdvisorFileTrash.file_id)) == "cloud://timeout"
