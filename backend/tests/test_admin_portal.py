"""管理台鉴权、统计口径、嵌入调用与定时任务留痕。"""

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju import jobs, source_health
from youju.api import app as admin
from youju.db import get_session
from youju.models import DocumentField, JobRun, ListEntry, Subscription
from youju.notify import run_cleanup

from .test_digest import NOW
from .test_digest import world as digest_world

HEADERS = {"X-Admin-Token": "portal-test-token"}
BACKEND = Path(__file__).resolve().parents[1]
world = digest_world


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(
            role="all", admin_token=HEADERS["X-Admin-Token"], advisor_enabled=False
        ),
    )
    admin.app.dependency_overrides[get_session] = override
    with TestClient(admin.app) as client:
        yield client
    admin.app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/api/admin/overview", "/api/admin/job-runs"])
def test_portal_data_requires_admin_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get(path, headers=HEADERS).status_code == 200


def test_portal_page_opens_without_token_and_has_no_data(client):
    # 页面本身和 /admin/review 一样是静态的：不带口令也返回 200，数据全靠带口令的接口
    page = client.get("/admin")
    assert page.status_code == 200
    assert 'id="token"' in page.text
    assert page.headers["cache-control"] == "no-store"
    for _ in range(10):
        assert client.get("/admin").status_code == 200
    assert client.get("/api/admin/overview", headers=HEADERS).status_code == 200


def test_overview_matches_database_and_consoles(client, session, world, monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(source_health, "datetime", Clock)
    field = DocumentField(
        document_id=world["apply"].id,
        field="support",
        value="贷款贴息",
        evidence="正文",
        evidence_offset=0,
        body_hash="f" * 64,
    )
    session.add(field)
    for n in (1, 2):
        session.add(
            ListEntry(
                document_id=world["list"].id,
                source_url="https://example.gov.cn/list",
                name=f"机构{n}",
                normalized_name=f"机构{n}",
                sequence=str(n),
                category="机构",
            )
        )
        session.add(
            Subscription(
                user_id=1,
                kind="deadline",
                template_id="test-template",
                document_id=world["apply"].id,
            )
        )
    started = datetime.now(UTC)
    session.add_all(
        [
            JobRun(
                job="round",
                started_at=started - timedelta(days=1),
                status="failed",
                summary="旧记录",
            ),
            JobRun(
                job="round",
                started_at=started,
                finished_at=started + timedelta(seconds=1),
                status="ok",
                summary="任务执行完成",
            ),
        ]
    )
    session.commit()
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    monkeypatch.setattr(admin, "CRON_HEARTBEAT", heartbeat)
    response = client.get("/api/admin/overview", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["todos"] == {
        "deadlines": 3,
        "deadlines_30": 1,
        "fields": 1,
        "kinds": 1,
        "audit": 1,
        "feedback": 1,
        "sources": 1,
        "takedown": 0,
    }
    assert data["data"] == {
        "documents": 4,
        "applications": 2,
        "lists": 1,
        "list_entries": 2,
        "approved_deadlines": 1,
        "users": 2,
        "follows": 1,
        "pending_reminders": 2,
    }
    for key, path, count_key in [
        ("deadlines", "deadlines/count?due=all", "pending"),
        ("deadlines_30", "deadlines/count?due=30", "pending"),
        ("fields", "fields/count", "pending"),
        ("kinds", "kinds/count", "pending"),
        ("audit", "audit/count", "flagged"),
        ("feedback", "feedback/count", "pending"),
    ]:
        assert (
            data["todos"][key]
            == client.get(f"/api/admin/{path}", headers=HEADERS).json()[count_key]
        )
    assert data["jobs"][0]["latest"]["summary"] == "任务执行完成"
    assert data["jobs"][1]["latest"] is None
    assert data["heartbeat_age_seconds"] < 10 and data["advisor_enabled"] is False
    assert "o-fresh" not in response.text and "日期不对" not in response.text
    assert response.headers["cache-control"] == "no-store"
    heartbeat.unlink()
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(
            role="all", admin_token=HEADERS["X-Admin-Token"], advisor_enabled=True
        ),
    )
    data = client.get("/api/admin/overview", headers=HEADERS).json()
    assert data["heartbeat_age_seconds"] is None and data["advisor_enabled"] is True


@pytest.mark.parametrize(
    "page,endpoint",
    [
        ("review", "deadlines"),
        ("fields", "fields"),
        ("kinds", "kinds"),
        ("audit", "audit"),
        ("feedback", "feedback"),
        ("sources", "sources/trial"),
    ],
)
def test_embedded_page_and_interface_auth(client, page, endpoint):
    for query in ("", "?embed=1"):
        response = client.get(f"/admin/{page}{query}")
        assert response.status_code == 200
        assert "/admin/embed.js" in response.text and "youjuAdminToken()" in response.text
    if page == "sources":
        # 无效网址在鉴权后被校验拦下，不触发真实抓取。
        def call(headers):
            return client.post(
                f"/api/admin/{endpoint}?embed=1", headers=headers, json={"url": "invalid"}
            )

        assert call({}).status_code == 401
        assert call(HEADERS).status_code == 422
    else:
        assert client.get(f"/api/admin/{endpoint}?embed=1").status_code == 401
        assert client.get(f"/api/admin/{endpoint}?embed=1", headers=HEADERS).status_code == 200


def test_run_records_finish_once_and_cleanup(session, session_factory, monkeypatch):
    monkeypatch.setattr(jobs, "session_factory", lambda: session_factory)
    first = jobs.start_run("backup")
    row = session.get(JobRun, first)
    assert row.status == "running" and row.finished_at is None
    jobs.finish_run(first, 0)
    jobs.finish_run(first, 1)  # 重复通知不能改写首次结束的结果
    session.refresh(row)
    assert row.status == "ok" and row.finished_at >= row.started_at
    second = jobs.start_run("cleanup")
    jobs.finish_run(second, 124)
    failed = session.get(JobRun, second)
    assert failed.status == "failed" and failed.summary == "运行超过 3 小时，已超时"
    now = datetime.now(UTC)
    session.add_all(
        [
            JobRun(job="round", started_at=now - timedelta(days=90, seconds=1)),
            JobRun(job="round", started_at=now - timedelta(days=90)),
        ]
    )
    session.commit()
    assert run_cleanup(session, stamp=now)["job_runs"] == 1
    assert len(session.scalars(select(JobRun)).all()) == 3


def test_record_failure_is_nonfatal_and_does_not_log_credentials(monkeypatch, caplog, capsys):
    from youju import cli

    def broken():
        raise RuntimeError("mysql://name:private-password@host/db token=private-token")

    monkeypatch.setattr(jobs, "session_factory", broken)
    assert jobs.start_run("backup") is None
    jobs.finish_run(1, 1)
    assert cli.main(["job-start", "backup"]) == 0
    assert cli.main(["job-finish", "1", "1"]) == 0
    assert capsys.readouterr().out == ""
    assert "private-password" not in caplog.text and "private-token" not in caplog.text


def test_job_records_filter_and_paginate_last_seven_days(client, session):
    now = datetime.now(UTC)
    session.add_all(
        [
            JobRun(job="backup", started_at=now - timedelta(days=8)),
            JobRun(job="round", started_at=now - timedelta(days=1)),
            JobRun(job="backup", started_at=now - timedelta(hours=2)),
            JobRun(job="backup", started_at=now - timedelta(hours=1)),
        ]
    )
    session.commit()
    first = client.get("/api/admin/job-runs?job=backup&limit=1", headers=HEADERS).json()
    assert len(first["items"]) == 1 and first["items"][0]["id"] == 4
    second = client.get(
        f"/api/admin/job-runs?job=backup&before_id={first['next_before_id']}", headers=HEADERS
    ).json()
    assert [row["id"] for row in second["items"]] == [3]
    assert second["next_before_id"] is None
    assert len(client.get("/api/admin/job-runs", headers=HEADERS).json()["items"]) == 3
    assert client.get("/api/admin/job-runs?job=unknown", headers=HEADERS).status_code == 422


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_embed_uses_only_same_origin_parent():
    script = BACKEND / "tests" / "js" / "admin_embed.cjs"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("record_fails", ["none", "start", "finish"])
@pytest.mark.parametrize("task_code", [0, 1, 20, 75, 124])
def test_job_wrapper_keeps_task_exit_status(tmp_path, record_fails, task_code):
    # 用替身命令执行真实包装器，不连接数据库、不操作真实任务或 /proc。
    for name, content in {
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "flock": '#!/bin/sh\nshift 4\nexec "$@"\n',
        "youju": """#!/bin/sh
echo "$*" >> "$CALLS"
case "$1" in
  job-start) [ "$RECORD_FAILS" = start ] && exit 1; echo 42; exit 0 ;;
  job-finish) [ "$RECORD_FAILS" = finish ] && exit 1; exit 0 ;;
  alert) exit 0 ;;
  *) exit "$TASK_CODE" ;;
esac
""",
    }.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o755)
    script = (BACKEND / "deploy" / "job.sh").read_text()
    script = script.replace(". /app/backend/deploy/env.sh", ":").replace("cd /app/backend", ":")
    script = script.replace("/proc/1/fd/1", str(tmp_path / "output"))
    wrapper = tmp_path / "job.sh"
    wrapper.write_text(script)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "RECORD_FAILS": str(record_fails),
        "TASK_CODE": str(task_code),
    }
    result = subprocess.run(
        ["sh", str(wrapper), "check-sources"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == (1 if task_code == 20 else task_code), result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "check-sources --job" in calls
    if record_fails != "start":
        assert f"job-finish 42 {task_code}" in calls


@pytest.mark.parametrize("failure", ["none", "crawl", "fetch-details", "lock", "record"])
def test_round_records_aggregate_result_and_continues(tmp_path, failure):
    for name, content in {
        "env.sh": "",
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "flock": '#!/bin/sh\n[ "$FAILURE" = lock ] && exit 1\nexit 0\n',
        "job.sh": """#!/bin/sh
echo "step $1" >> "$CALLS"
[ "$1" = "$FAILURE" ] && exit 1
exit 0
""",
        "youju": """#!/bin/sh
echo "$*" >> "$CALLS"
[ "$FAILURE" = record ] && exit 1
[ "$1" = job-start ] && echo 42
exit 0
""",
    }.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o755)
    script = (BACKEND / "deploy" / "round.sh").read_text()
    script = script.replace("D=/app/backend/deploy", f"D={tmp_path}")
    script = script.replace("cd /app/backend", f"cd {tmp_path}")
    script = script.replace("/proc/1/fd/1", str(tmp_path / "output"))
    script = script.replace("/tmp/youju-round.lock", str(tmp_path / "round.lock"))
    wrapper = tmp_path / "round.sh"
    wrapper.write_text(script)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "FAILURE": failure,
    }
    result = subprocess.run(
        ["sh", str(wrapper), "--notify"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "job-start round" in calls and "step notify" in calls
    if failure == "lock":
        assert "job-finish 42 75" in calls and "step crawl" not in calls
    else:
        assert "step crawl" in calls and "step parse-lists" in calls
        if failure == "crawl":
            assert "step check-sources" not in calls
        if failure != "record":
            assert f"job-finish 42 {0 if failure == 'none' else 1}" in calls
