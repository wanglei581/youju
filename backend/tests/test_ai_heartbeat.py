"""两个服务的 AI 设置心跳、开放状态、全站额度标记和任务错误代码（D-075）。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from youju import advisor_tasks as service
from youju import cli
from youju.ai import heartbeat
from youju.api import advisor as api
from youju.api import app as app_module
from youju.api import me, v1
from youju.db import get_session
from youju.models import (
    AdvisorTask,
    AdvisorUsage,
    AIHeartbeat,
    AIUsage,
    Document,
    DocumentDetail,
    Source,
    User,
)

NOW = datetime(2026, 10, 19, 2, tzinfo=UTC)
BODY = "申报对象为科技企业。\n支持方式为资金支持。\n请提交申请书及附件。"


def make_settings(**changes):
    value = SimpleNamespace(
        advisor_enabled=False,
        ai_advisor_enabled=True,
        ai_advisor_openids="internal,second",
        ai_advisor_audience="whitelist",
        ai_advisor_daily_cap=200,
        ai_model="fake-model",
        ai_api_key=SecretStr("fake-test-value"),
        ai_base_url="https://fake.invalid",
        dev_openid="internal",
        trust_wx_headers=False,
        agreement_version="2026-10",
        retain_events_days=90,
        retain_feedback_days=180,
        retain_used_subscriptions_days=30,
        retain_unused_subscriptions_days=365,
        cloud_env=None,
        role="all",
    )
    for key, item in changes.items():
        setattr(value, key, item)
    return value


@pytest.fixture
def settings(monkeypatch):
    value = make_settings()
    for module in (api, me, service, app_module, v1, cli):
        monkeypatch.setattr(module, "get_settings", lambda: value)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(heartbeat, "_api_written_at", None)
    monkeypatch.setattr(heartbeat, "_api_denied_logged", False)
    return value


@pytest.fixture
def client(session_factory, settings):
    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    with TestClient(app_module.app) as c:
        assert c.post("/api/v1/me/agreement", json={"version": "2026-10"}).status_code == 200
        yield c
    app_module.app.dependency_overrides.clear()


@pytest.fixture
def doc(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="科技厅",
        column_name="政策",
        url="https://official.test/policy/",
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    row = Document(
        source_id=source.id,
        title="申报通知",
        kind_override="申报通知",
        url="https://official.test/1",
        url_key="official.test/1",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(row)
    session.flush()
    session.add(DocumentDetail(document_id=row.id, body_text=BODY, fetched_at=NOW, status="ok"))
    session.commit()
    return row


def submit(client, doc_id, key="first"):
    return client.post(
        "/api/v1/advisor/tasks",
        json={"kind": "interpret", "doc_id": doc_id},
        headers={"Idempotency-Key": key},
    )


def test_fingerprint_changes_with_each_setting_and_hides_openids():
    base = heartbeat.fingerprint(make_settings())
    assert len(base) == 64 and "internal" not in base
    # 白名单顺序、多余的空格不影响
    assert heartbeat.fingerprint(make_settings(ai_advisor_openids=" second , internal")) == base
    for change in (
        {"ai_advisor_enabled": False},
        {"ai_advisor_audience": "all"},
        {"ai_advisor_openids": "internal"},
        {"ai_advisor_daily_cap": 300},
    ):
        assert heartbeat.fingerprint(make_settings(**change)) != base
    # 模型线路、密钥不算进去
    assert heartbeat.fingerprint(make_settings(ai_model="other")) == base


def test_worker_ready_only_enforced_on_api_role(session):
    local = make_settings()
    assert heartbeat.worker_ready(session, local, NOW) is True  # 本机开发和测试不看心跳
    remote = make_settings(role="api")
    assert heartbeat.worker_ready(session, remote, NOW) is False  # 还没有心跳
    heartbeat.beat(session, make_settings(role="admin"), "admin", NOW)
    assert heartbeat.worker_ready(session, remote, NOW) is True
    assert heartbeat.worker_ready(session, remote, NOW + heartbeat.STALE) is True
    assert heartbeat.worker_ready(
        session, remote, NOW + heartbeat.STALE + timedelta(seconds=1)
    ) is (False)
    # admin 没开 AI
    heartbeat.beat(session, make_settings(role="admin", ai_advisor_enabled=False), "admin", NOW)
    assert heartbeat.worker_ready(session, remote, NOW) is False
    # 两边设置对不上（admin 的白名单少一个人）
    heartbeat.beat(
        session, make_settings(role="admin", ai_advisor_openids="internal"), "admin", NOW
    )
    assert heartbeat.worker_ready(session, remote, NOW) is False


def test_api_refuses_ai_tasks_until_admin_is_ready(client, session, settings, doc):
    settings.role = "api"
    refused = submit(client, doc.id)
    assert refused.status_code == 429
    assert refused.json()["detail"] == {"code": "AI_UNAVAILABLE", "message": heartbeat.UNAVAILABLE}
    assert session.scalar(select(AdvisorUsage.used_count)) is None  # 不扣次数
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["enabled"] is True and caps["available"] is False
    heartbeat.beat(session, make_settings(role="admin"), "admin", NOW)
    assert client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["available"] is True
    assert submit(client, doc.id).status_code == 202
    # 同一幂等键重复提交：后台又处理不了时，仍返回原任务（不重复建）
    session.get(AIHeartbeat, "admin").ai_enabled = False
    session.commit()
    assert submit(client, doc.id).status_code == 202
    assert len(session.scalars(select(AdvisorTask)).all()) == 1


def test_submission_refuses_ineligible_document_without_charging(client, session, doc):
    doc.kind_override = "名单"
    session.commit()
    refused = submit(client, doc.id)
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "AI_NOT_ELIGIBLE"
    assert refused.json()["detail"]["reason"] == "list"
    assert session.scalar(select(AdvisorTask)) is None
    assert session.scalar(select(AdvisorUsage.used_count)) is None


def test_task_list_carries_error_code(client, session, doc):
    user = session.scalar(select(User).where(User.openid == "internal"))
    session.add(
        AdvisorTask(
            user_id=user.id,
            kind="interpret",
            status="failed",
            input_json={"doc_id": doc.id},
            row_count=0,
            idempotency_key="k",
            created_at=NOW,
            error=service.AI_CAP_MESSAGE,
            expires_at=NOW + timedelta(days=30),
        )
    )
    session.commit()
    items = client.get("/api/v1/advisor/tasks").json()["items"]
    assert items[0]["errorCode"] == "AI_DAILY_CAP"


def test_local_role_does_not_need_heartbeat(client, settings, doc):
    assert submit(client, doc.id).status_code == 202
    assert client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["available"] is True


def test_mode_is_public_and_follows_audience(client, session, settings):
    assert client.get("/api/v1/advisor/mode").json() == {"open": False, "available": False}
    settings.ai_advisor_audience = "all"
    assert client.get("/api/v1/advisor/mode").json() == {"open": True, "available": True}
    settings.role = "api"
    assert client.get("/api/v1/advisor/mode").json() == {"open": True, "available": False}
    heartbeat.beat(session, make_settings(role="admin", ai_advisor_audience="all"), "admin", NOW)
    assert client.get("/api/v1/advisor/mode").json() == {"open": True, "available": True}
    settings.ai_advisor_enabled = False
    assert client.get("/api/v1/advisor/mode").json() == {"open": False, "available": False}


def test_site_full_flag_uses_submission_rule(client, session, settings):
    settings.ai_advisor_daily_cap = 2
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["siteFull"] is False
    day = service.usage_day(NOW)
    session.add(
        AIUsage(
            day=day,
            task="interpret",
            model="m",
            calls=2,
            errors=0,
            prompt_tokens=0,
            completion_tokens=0,
        )
    )
    session.commit()
    assert client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["siteFull"] is True


@pytest.mark.parametrize(
    ("error", "code"),
    [(service.AI_CAP_MESSAGE, "AI_DAILY_CAP"), (service.AI_OFF_MESSAGE, "AI_OFF"), (None, None)],
)
def test_task_output_carries_error_code(client, session, doc, error, code):
    user = session.scalar(select(User).where(User.openid == "internal"))
    task = AdvisorTask(
        user_id=user.id,
        kind="interpret",
        status="failed" if error else "queued",
        input_json={"doc_id": doc.id},
        row_count=0,
        idempotency_key="k",
        created_at=NOW,
        error=error,
        expires_at=NOW + timedelta(days=30),
    )
    session.add(task)
    session.commit()
    body = client.get(f"/api/v1/advisor/tasks/{task.id}").json()
    assert body["errorCode"] == code


def test_note_api_throttles_and_tolerates_missing_permission(session, monkeypatch):
    monkeypatch.setattr(heartbeat, "_api_written_at", None)
    monkeypatch.setattr(heartbeat, "_api_denied_logged", False)
    remote = make_settings(role="api")
    heartbeat.note_api(session, remote, NOW)
    assert session.get(AIHeartbeat, "api").beat_at == NOW
    heartbeat.note_api(session, remote, NOW + timedelta(minutes=4))
    assert session.get(AIHeartbeat, "api").beat_at == NOW  # 5 分钟内不再写
    heartbeat.note_api(session, remote, NOW + heartbeat.API_EVERY)
    assert session.get(AIHeartbeat, "api").beat_at == NOW + heartbeat.API_EVERY
    # 本机角色不写
    heartbeat.note_api(session, make_settings(), NOW + timedelta(hours=2))
    assert session.get(AIHeartbeat, "api").beat_at == NOW + heartbeat.API_EVERY

    def denied(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("denied"))

    monkeypatch.setattr(heartbeat, "beat", denied)
    heartbeat.note_api(session, remote, NOW + timedelta(hours=1))  # 没有写权限也不抛错
    assert heartbeat._api_denied_logged is True


def add_user(session, openid="internal"):
    user = User(openid=openid, agreement_version="2026-10", agreement_at=NOW)
    session.add(user)
    session.commit()
    return user


def add_refunded(session, user, finished_at):
    """一个以"功能未开放"失败的 AI 任务（admin 退回的，次数已退）。"""
    session.add(
        AdvisorTask(
            user_id=user.id,
            kind="interpret",
            status="failed",
            error=service.AI_OFF_MESSAGE,
            input_json={"doc_id": 1},
            row_count=0,
            idempotency_key=f"r{finished_at.timestamp()}",
            created_at=finished_at,
            finished_at=finished_at,
            expires_at=finished_at + timedelta(days=30),
        )
    )
    session.commit()


def test_alerts_for_mismatch_and_refunds_at_most_hourly(session, monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    admin = make_settings(role="admin")
    assert heartbeat.check_and_alert(session, admin, NOW) is None  # 还没有 admin 心跳
    heartbeat.beat(session, admin, "admin", NOW)
    heartbeat.beat(session, make_settings(role="api", ai_advisor_daily_cap=300), "api", NOW)
    assert "设置不一致" in heartbeat.check_and_alert(session, admin, NOW)
    assert heartbeat.check_and_alert(session, admin, NOW + timedelta(minutes=30)) is None
    # admin 开着 AI 时以"功能未开放"失败的是用户被移出白名单，不算退回、不提醒
    add_refunded(session, add_user(session), NOW + timedelta(minutes=40))
    later = NOW + heartbeat.ALERT_EVERY
    heartbeat.beat(session, make_settings(role="api"), "api", later)
    assert heartbeat.check_and_alert(session, admin, later) is None
    # admin 关了 AI：两边对不上，退回了几个写在同一条里
    off = make_settings(role="admin", ai_advisor_enabled=False)
    heartbeat.beat(session, off, "admin", later)
    text = heartbeat.check_and_alert(session, off, later)
    assert "设置不一致" in text and "admin 服务没开 AI，退回了 1 个" in text
    assert len(sent) == 2


def test_refund_alert_counts_from_tasks_and_survives_failed_send(session, monkeypatch):
    results = [False, True, True]
    sent = []
    monkeypatch.setattr(
        "youju.alerts.send_alert", lambda text: sent.append(text) or results[len(sent) - 1]
    )
    # api 的心跳没来（比如授权还没执行）：看不出两边对不上，只能靠退回的任务发现 admin 没开 AI
    off = make_settings(role="admin", ai_advisor_enabled=False)
    heartbeat.beat(session, off, "admin", NOW)
    user = add_user(session)
    add_refunded(session, user, NOW)
    add_refunded(session, user, NOW + timedelta(seconds=30))
    # 不到一分钟的先不数：同一分钟 advisor-run 可能还在退回、没提交
    assert heartbeat.check_and_alert(session, off, NOW + timedelta(seconds=50)) is None
    assert sent == []
    # 第一次没发出去：不记时间；下一分钟没有新的退回，照样数到 2 个、再发
    assert heartbeat.check_and_alert(session, off, NOW + timedelta(minutes=2)) is None
    assert session.get(AIHeartbeat, "admin").alerted_at is None
    first = heartbeat.check_and_alert(session, off, NOW + timedelta(minutes=3))
    assert "退回了 2 个" in first
    # 发出去以后一小时内不再发；之后只数上次没数到的：上次只数到一分钟以前（2:30 那个留到这次），
    # 已经数过的两个不再算
    add_refunded(session, user, NOW + timedelta(minutes=2, seconds=30))
    add_refunded(session, user, NOW + timedelta(minutes=10))
    assert heartbeat.check_and_alert(session, off, NOW + timedelta(minutes=30)) is None
    second = heartbeat.check_and_alert(
        session, off, NOW + timedelta(minutes=3) + heartbeat.ALERT_EVERY
    )
    assert "退回了 2 个" in second
    # 没有新的退回：不再提醒
    assert heartbeat.check_and_alert(session, off, NOW + 3 * heartbeat.ALERT_EVERY) is None
    assert len(sent) == 3


def test_failed_send_is_retried_next_time(session, monkeypatch):
    results = [None, False, True]
    sent = []
    monkeypatch.setattr(
        "youju.alerts.send_alert", lambda text: sent.append(text) or results[len(sent) - 1]
    )
    admin = make_settings(role="admin")
    heartbeat.beat(session, admin, "admin", NOW)
    heartbeat.beat(session, make_settings(role="api", ai_advisor_daily_cap=300), "api", NOW)
    # 没配机器人（None）、发送失败（False）都不记时间，下一分钟再试；发出去以后才开始算一小时
    assert heartbeat.check_and_alert(session, admin, NOW) is None
    assert heartbeat.check_and_alert(session, admin, NOW + timedelta(minutes=1)) is None
    assert heartbeat.check_and_alert(session, admin, NOW + timedelta(minutes=2))
    assert session.get(AIHeartbeat, "admin").alerted_at == NOW + timedelta(minutes=2)
    assert len(sent) == 3


def test_notice_when_api_heartbeat_missing_at_most_daily(session, monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    # admin 不能处理 AI 任务时不提醒（首发版两边都关着）
    heartbeat.beat(session, make_settings(role="admin", ai_advisor_enabled=False), "admin", NOW)
    assert heartbeat.check_and_alert(session, make_settings(role="admin"), NOW) is None
    admin = make_settings(role="admin")
    heartbeat.beat(session, admin, "admin", NOW)
    assert "AI 设置心跳权限" in heartbeat.check_and_alert(session, admin, NOW)
    assert heartbeat.check_and_alert(session, admin, NOW + timedelta(hours=23)) is None
    # api 心跳来了、而且新鲜：不再提醒
    heartbeat.beat(session, make_settings(role="api"), "api", NOW + timedelta(days=1))
    assert heartbeat.check_and_alert(session, admin, NOW + timedelta(days=1)) is None
    # 超过 30 分钟没来，又过了一天：再提醒一次
    assert "AI 设置心跳权限" in heartbeat.check_and_alert(
        session, admin, NOW + timedelta(days=1, minutes=31)
    )
    assert len(sent) == 2


def test_admin_without_ai_key_is_not_ready(session):
    remote = make_settings(role="api")
    heartbeat.beat(session, make_settings(role="admin", ai_api_key=None), "admin", NOW)
    assert session.get(AIHeartbeat, "admin").ai_enabled is False
    assert heartbeat.worker_ready(session, remote, NOW) is False


def test_heartbeat_command_alerts_refunds_from_advisor_run(
    session, session_factory, settings, doc, monkeypatch, capsys
):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(heartbeat, "REFUND_SETTLE", timedelta(0))
    settings.ai_advisor_enabled = False
    assert cli.main(["ai-heartbeat"]) == 0
    assert "admin 不能处理 AI 任务" in capsys.readouterr().out
    session.expire_all()
    assert session.get(AIHeartbeat, "admin").ai_enabled is False
    user = add_user(session)
    assert service.reserve_usage(session, user.id, "interpret", service.usage_day(NOW))
    session.add(
        AdvisorTask(
            user_id=user.id,
            kind="interpret",
            status="queued",
            input_json={"doc_id": doc.id},
            row_count=0,
            idempotency_key="q",
            created_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
    )
    session.commit()
    assert cli.main(["advisor-run"]) == 0
    session.expire_all()
    task = session.scalar(select(AdvisorTask))
    assert task.status == "failed" and task.error == service.AI_OFF_MESSAGE
    assert sent == []  # advisor-run 自己不发，下一次心跳按任务表数
    assert cli.main(["ai-heartbeat"]) == 0
    assert len(sent) == 1 and "退回了 1 个" in sent[0]
