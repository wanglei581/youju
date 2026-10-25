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
    # admin 没配对外线路：对所有人开放时不算能处理，不退回后台线路（D-093 上线门槛第 3 条）
    heartbeat.beat(session, make_settings(role="admin", ai_advisor_audience="all"), "admin", NOW)
    assert client.get("/api/v1/advisor/mode").json() == {"open": True, "available": False}
    line = {
        "ai_advisor_base_url": "https://cloud1-test.api.tcloudbasegateway.com/v1/ai/cloudbase",
        "ai_advisor_api_key": SecretStr("advisor-test-value"),
        "ai_advisor_model": "advisor-model",
    }
    heartbeat.beat(
        session, make_settings(role="admin", ai_advisor_audience="all", **line), "admin", NOW
    )
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
    [
        (service.AI_CAP_MESSAGE, "AI_DAILY_CAP"),
        (service.AI_OFF_MESSAGE, "AI_OFF"),
        (heartbeat.UNAVAILABLE, "AI_UNAVAILABLE"),
        (None, None),
    ],
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


def queue(session, user, doc, key="q"):
    """一个排队的 AI 解读任务（已扣次数）。"""
    assert service.reserve_usage(session, user.id, "interpret", service.usage_day(NOW))
    session.add(
        AdvisorTask(
            user_id=user.id,
            kind="interpret",
            status="queued",
            input_json={"doc_id": doc.id},
            row_count=0,
            idempotency_key=key,
            created_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
    )
    session.commit()


def pending(session):
    return session.get(AIHeartbeat, "admin", populate_existing=True).refunds_pending


def test_alerts_for_mismatch_and_refunds_at_most_hourly(session, monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    admin = make_settings(role="admin")
    assert heartbeat.check_and_alert(session, admin, NOW) is None  # 还没有 admin 心跳
    heartbeat.beat(session, admin, "admin", NOW)
    heartbeat.beat(session, make_settings(role="api", ai_advisor_daily_cap=300), "api", NOW)
    assert "设置不一致" in heartbeat.check_and_alert(session, admin, NOW)
    assert heartbeat.check_and_alert(session, admin, NOW + timedelta(minutes=30)) is None
    # admin 关了 AI、退回了 2 个：两边对不上，退回了几个写在同一条里，报了就清零
    later = NOW + heartbeat.ALERT_EVERY
    heartbeat.beat(session, make_settings(role="api"), "api", later)
    off = make_settings(role="admin", ai_advisor_enabled=False)
    heartbeat.beat(session, off, "admin", later)
    heartbeat.add_refunds(session, 2, later)
    session.commit()
    text = heartbeat.check_and_alert(session, off, later)
    assert "设置不一致" in text and "admin 服务处理不了 AI 任务" in text and "退回了 2 个" in text
    assert pending(session) == 0
    assert len(sent) == 2


def test_refund_count_is_kept_until_an_alert_goes_out(session, session_factory, monkeypatch):
    results = [False, True, True]
    sent = []

    def send(text):
        sent.append(text)
        if len(sent) == 2:
            # 正发着提醒，advisor-run 又退回了 1 个（另一个事务提交）：这次没报到，留到下次
            with session_factory() as other:
                heartbeat.add_refunds(other, 1, NOW)
                other.commit()
        return results[len(sent) - 1]

    monkeypatch.setattr("youju.alerts.send_alert", send)
    # api 的心跳没来（比如授权还没执行）：看不出两边对不上，只能靠退回的个数发现 admin 没开 AI。
    # admin 那一行还没有时，退回先占一行"处理不了"
    heartbeat.add_refunds(session, 2, NOW)
    session.commit()
    assert session.get(AIHeartbeat, "admin").ai_enabled is False and pending(session) == 2
    off = make_settings(role="admin", ai_advisor_enabled=False)
    heartbeat.beat(session, off, "admin", NOW)
    assert pending(session) == 2  # 心跳不动这个数
    # 第一次没发出去：不记时间，个数还在；下一分钟没有新的退回，照样报 2 个
    assert heartbeat.check_and_alert(session, off, NOW) is None
    assert session.get(AIHeartbeat, "admin").alerted_at is None and pending(session) == 2
    assert "退回了 2 个" in heartbeat.check_and_alert(session, off, NOW + timedelta(minutes=1))
    assert pending(session) == 1
    # 一小时内不再发；之后报发送期间新退回的那 1 个
    assert heartbeat.check_and_alert(session, off, NOW + timedelta(minutes=30)) is None
    later = NOW + timedelta(minutes=1) + heartbeat.ALERT_EVERY
    assert "退回了 1 个" in heartbeat.check_and_alert(session, off, later)
    assert pending(session) == 0
    assert heartbeat.check_and_alert(session, off, later + heartbeat.ALERT_EVERY) is None
    assert len(sent) == 3


def test_refunds_after_whitelist_removal_are_not_counted(session, doc, monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    user = add_user(session)
    queue(session, user, doc)
    # admin 开着 AI，只是这个人被移出了白名单：写"功能未开放"，不记退回
    removed = make_settings(role="admin", ai_advisor_openids="second")
    assert service._fail_revoked_interpret(session, removed, NOW) == 1
    task = session.scalar(select(AdvisorTask))
    assert task.error == service.AI_OFF_MESSAGE
    assert session.get(AIHeartbeat, "admin") is None
    # 之后 admin 关了 AI：先前那个不会被算成 admin 没开 AI 退回的
    off = make_settings(role="admin", ai_advisor_enabled=False)
    later = NOW + timedelta(minutes=5)
    heartbeat.beat(session, off, "admin", later)
    assert heartbeat.check_and_alert(session, off, later) is None
    # admin 没开 AI 时退回的才记：用户看到"AI 暂时不可用"，错误代码 AI_UNAVAILABLE
    queue(session, user, doc, key="second")
    assert service._fail_revoked_interpret(session, off, later) == 1
    session.expire_all()
    task = session.scalar(select(AdvisorTask).where(AdvisorTask.idempotency_key == "second"))
    assert task.error == heartbeat.UNAVAILABLE
    assert service.task_dict(task)["errorCode"] == "AI_UNAVAILABLE"
    assert "退回了 1 个" in heartbeat.check_and_alert(session, off, later + timedelta(minutes=1))
    assert len(sent) == 1


def test_queued_tasks_are_refunded_when_admin_loses_its_key(session, doc, monkeypatch):
    """任务排上队以后 admin 的对外 AI 密钥被撤掉：不领任务、不调用模型，按"AI 暂时不可用"退回并记个数。"""
    queue(session, add_user(session), doc)

    def forbidden(*args, **kwargs):
        pytest.fail("缺密钥时不得调用模型")

    monkeypatch.setattr(service, "ChatClient", forbidden)
    keyless = make_settings(role="admin", ai_api_key=None)
    result = service.run_advisor_tasks(session, settings=keyless, stamp=NOW)
    session.expire_all()
    task = session.scalar(select(AdvisorTask))
    assert result["revoked"] == 1 and task.status == "failed"
    assert task.error == heartbeat.UNAVAILABLE
    assert service.task_dict(task)["errorCode"] == "AI_UNAVAILABLE"
    assert session.scalar(select(AdvisorUsage.used_count)) == 0  # 次数已退
    assert pending(session) == 1


def test_alert_when_api_on_but_admin_cannot_work(session, monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    # 两边都关着（首发版）：不提醒
    closed = make_settings(ai_advisor_enabled=False)
    heartbeat.beat(session, closed, "admin", NOW)
    heartbeat.beat(session, closed, "api", NOW)
    assert heartbeat.check_and_alert(session, closed, NOW) is None
    # 设置一样、api 开着，admin 缺对外 AI 的密钥：api 一直不收任务，要提醒
    keyless = make_settings(role="admin", ai_api_key=None)
    heartbeat.beat(session, keyless, "admin", NOW)
    heartbeat.beat(session, make_settings(role="api"), "api", NOW)
    text = heartbeat.check_and_alert(session, keyless, NOW)
    assert "处理不了" in text and "设置不一致" not in text
    assert len(sent) == 1


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
    settings.ai_advisor_enabled = False
    assert cli.main(["ai-heartbeat"]) == 0
    assert "admin 不能处理 AI 任务" in capsys.readouterr().out
    session.expire_all()
    assert session.get(AIHeartbeat, "admin").ai_enabled is False
    queue(session, add_user(session), doc)
    assert cli.main(["advisor-run"]) == 0
    session.expire_all()
    task = session.scalar(select(AdvisorTask))
    assert task.status == "failed" and task.error == heartbeat.UNAVAILABLE
    assert sent == [] and pending(session) == 1  # advisor-run 只记个数，下一次心跳发提醒
    assert cli.main(["ai-heartbeat"]) == 0
    assert len(sent) == 1 and "退回了 1 个" in sent[0]
