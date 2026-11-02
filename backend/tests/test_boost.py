"""加量体验（D-070 ③）和申报季加量（⑤）：开关、规则、退次数、注销后仍算用过、最小权限账号。

只连接指定测试库。"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from youju import advisor_tasks, boost, rewards
from youju.agreements import TOOLS_AGREEMENT_VERSION
from youju.api import advisor, me, pay, v1
from youju.api import app as app_module
from youju.config import Settings
from youju.db import get_session
from youju.models import AdvisorTask, AdvisorUsage, BoostTrial, User

# 当前的用户协议版本：取后台默认值，升版本时不用改测试
AGREEMENT = Settings.model_fields["agreement_version"].default


BJ = ZoneInfo("Asia/Shanghai")
# 星期二上午；体验 72 小时到 10 月 23 日 10:00 结束
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=BJ)


def headers(openid):
    return {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": openid}


class Clock:
    def __init__(self):
        self.at = NOW

    def __call__(self):
        return self.at


@pytest.fixture
def ctx(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        advisor_enabled=True,
        boost_trial_enabled=True,
        trust_wx_headers=True,
        cloud_env="test-env",
        admin_token="test-admin",
    )
    for module in (me, advisor, pay, app_module):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    clock = Clock()
    monkeypatch.setattr(v1, "now", clock)

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    with TestClient(app_module.app) as client:
        yield SimpleNamespace(client=client, settings=settings, clock=clock)
    app_module.app.dependency_overrides.clear()


def join(client, openid="trial-user", *, tools=True):
    """同意用户协议（和工具使用说明）；返回这个用户的请求头。"""
    identity = headers(openid)
    assert (
        client.post(
            "/api/v1/me/agreement", headers=identity, json={"version": AGREEMENT}
        ).status_code
        == 200
    )
    if tools:
        assert (
            client.post(
                "/api/v1/me/agreement",
                headers=identity,
                json={"scope": "advisor", "version": advisor_tasks.ADVISOR_AGREEMENT_VERSION},
            ).status_code
            == 200
        )
    return identity


_keys = iter(range(1, 10**6))


def submit(client, identity, count=1, *, kind="compare"):
    rows = (
        [f"合成企业{n}" for n in range(count)]
        if kind == "compare"
        else [{"name": f"合成企业{n}", "province": "山东"} for n in range(count)]
    )
    return client.post(
        "/api/v1/advisor/tasks",
        headers={**identity, "Idempotency-Key": f"boost-{next(_keys)}"},
        json={"kind": kind, "rows": rows},
    )


def status(client, identity):
    response = client.get("/api/v1/advisor/status", headers=identity)
    assert response.status_code == 200, response.text
    return response.json()


def start(client, identity):
    return client.post("/api/v1/advisor/trial", headers=identity, json={})


def test_switches_default_off(session_factory, monkeypatch):
    settings = Settings(_env_file=None, advisor_enabled=True, trust_wx_headers=True)
    assert settings.boost_trial_enabled is False and settings.season_boost_enabled is False
    for module in (me, advisor, app_module):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "now", lambda: NOW)

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    try:
        with TestClient(app_module.app) as client:
            identity = join(client)
            assert start(client, identity).status_code == 404
            body = status(client, identity)
            assert body["trial"] is None and body["season"] is None
            assert body["quota"]["compare"] == {"used": 0, "limit": 3, "maxRows": 50}
            # 批量工具关着时体验接口也不在
            settings.advisor_enabled = False
            settings.boost_trial_enabled = True
            assert start(client, identity).status_code == 404
    finally:
        app_module.app.dependency_overrides.clear()


def test_start_needs_agreement_runs_72_hours_and_only_once(ctx, session):
    client, clock = ctx.client, ctx.clock
    stranger = headers("not-agreed")
    assert start(client, stranger).status_code == 403
    # 开始体验不提交客户信息：只同意了用户协议、没同意工具使用说明也能开始
    identity = join(client, tools=False)
    before = status(client, identity)["trial"]
    assert before["available"] is True and before["active"] is False
    assert (before["hours"], before["dailyLimit"], before["maxRows"]) == (72, 10, 500)

    response = start(client, identity)
    assert response.status_code == 200, response.text
    trial = response.json()
    assert trial["active"] is True and trial["available"] is False
    assert trial["startedAt"] == NOW.isoformat()
    assert trial["endsAt"] == (NOW + timedelta(hours=72)).isoformat()
    # 重复点"开始"不会重新起算
    clock.at = NOW + timedelta(hours=5)
    again = start(client, identity)
    assert again.status_code == 200 and again.json()["startedAt"] == NOW.isoformat()
    assert session.scalar(select(func.count(BoostTrial.id))) == 1
    assert status(client, identity)["quota"]["compare"]["maxRows"] == 500

    # 第 72 小时整结束，自动回到免费量
    clock.at = NOW + timedelta(hours=72)
    after = status(client, identity)
    assert after["trial"]["active"] is False and after["trial"]["reason"] == "used"
    assert after["quota"]["compare"]["maxRows"] == 50
    used = start(client, identity)
    assert used.status_code == 409 and used.json()["detail"]["code"] == "TRIAL_USED"


def test_old_agreement_kept_for_old_features_cannot_start_a_trial(ctx, session):
    """协议升版过渡期（最早版本填 2026-10）：只同意过 2026-10 的照旧算登录，但那一版没写体验记录和
    注销后的去标识编号，开始体验要先重新同意（403 后小程序清掉本机同意、请他重新同意）。"""
    client = ctx.client
    assert TOOLS_AGREEMENT_VERSION <= AGREEMENT
    ctx.settings.agreement_min_version = "2026-10"
    identity = headers("old-consent")
    old = client.post("/api/v1/me/agreement", headers=identity, json={"version": "2026-10"})
    assert old.status_code == 200 and old.json()["valid"] is True
    assert status(client, identity)["trial"]["available"] is True
    response = start(client, identity)
    assert response.status_code == 403 and response.json()["detail"] == me.OUTDATED
    assert session.scalar(select(func.count(BoostTrial.id))) == 0
    new = client.post("/api/v1/me/agreement", headers=identity, json={"version": AGREEMENT})
    assert new.status_code == 200
    assert start(client, identity).status_code == 200


def test_first_release_ai_agreement_cannot_start_a_trial(ctx, session):
    """D-098：只同意过过审版加 AI 的 2026-11.1（版本排在 2026-11 后面，正文没写加量体验）开始不了体验；
    同意含体验条款的新版（如 2026-12）以后才行。"""
    client = ctx.client
    ctx.settings.agreement_version = "2026-12"
    ctx.settings.agreement_min_version = "2026-10.2"
    identity = headers("ai-pack-consent")
    ai_pack = client.post("/api/v1/me/agreement", headers=identity, json={"version": "2026-11.1"})
    assert ai_pack.status_code == 200 and ai_pack.json()["valid"] is True
    response = start(client, identity)
    assert response.status_code == 403 and response.json()["detail"] == me.OUTDATED
    assert session.scalar(select(func.count(BoostTrial.id))) == 0
    new = client.post("/api/v1/me/agreement", headers=identity, json={"version": "2026-12"})
    assert new.status_code == 200
    assert start(client, identity).status_code == 200


def test_old_agreement_cannot_accept_the_new_tool_terms(ctx):
    """批量工具的任务记录和次数从 2026-11 版指引起才写明（一·13）：过渡期只同意过旧版的，
    先重新同意新版指引，才能同意工具使用说明和提交名单。"""
    client = ctx.client
    ctx.settings.agreement_min_version = "2026-10"
    identity = headers("old-consent-tools")
    old = client.post("/api/v1/me/agreement", headers=identity, json={"version": "2026-10"})
    assert old.status_code == 200 and old.json()["valid"] is True
    terms = {"scope": "advisor", "version": advisor_tasks.ADVISOR_AGREEMENT_VERSION}
    refused = client.post("/api/v1/me/agreement", headers=identity, json=terms)
    assert refused.status_code == 403 and refused.json()["detail"] == me.OUTDATED
    assert submit(client, identity).status_code == 403
    assert (
        client.post(
            "/api/v1/me/agreement", headers=identity, json={"version": AGREEMENT}
        ).status_code
        == 200
    )
    assert client.post("/api/v1/me/agreement", headers=identity, json=terms).status_code == 200
    assert submit(client, identity).status_code == 202


def test_old_agreement_with_tool_terms_still_cannot_preview_or_submit(ctx, session):
    """过渡期（当前 2026-11、最早 2026-10）只同意过 2026-10 的，即使记着同意过新版工具使用说明
    （比如回退配置时留下的），预览、提交也拦下（付费下单走同一个 advisor._upload_consent）；
    经接口重新同意新版指引后放行。"""
    client = ctx.client
    ctx.settings.agreement_min_version = "2026-10"
    identity = headers("old-consent-preview")
    old = client.post("/api/v1/me/agreement", headers=identity, json={"version": "2026-10"})
    assert old.status_code == 200 and old.json()["valid"] is True
    user = session.scalar(select(User).where(User.openid == "old-consent-preview"))
    user.advisor_agreement_version = advisor_tasks.ADVISOR_AGREEMENT_VERSION
    user.advisor_agreement_at = NOW
    session.commit()
    body = {"kind": "compare", "rows": ["合成企业"]}
    preview = client.post("/api/v1/advisor/preview", headers=identity, json=body)
    assert preview.status_code == 403 and preview.json()["detail"] == me.OUTDATED
    blocked = submit(client, identity)
    assert blocked.status_code == 403 and blocked.json()["detail"] == me.OUTDATED
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    new = client.post("/api/v1/me/agreement", headers=identity, json={"version": AGREEMENT})
    assert new.status_code == 200
    assert client.post("/api/v1/advisor/preview", headers=identity, json=body).status_code == 200
    assert submit(client, identity).status_code == 202


def test_double_tap_that_waited_for_the_lock_gets_the_same_trial(ctx, db_engine):
    """连点两次：后拿到锁的那个请求可能比先建成的那段更早到达，开始时间要在锁后取，
    返回同一段，不报"已经用过"。"""
    from sqlalchemy import event

    client, clock = ctx.client, ctx.clock
    identity = join(client)
    assert start(client, identity).status_code == 200
    clock.at = NOW - timedelta(seconds=1)

    def waited(conn, cursor, statement, *args):
        sql = statement.lower()
        if sql.startswith("select users.id") and "for update" in sql:
            clock.at = NOW + timedelta(seconds=1)

    event.listen(db_engine, "before_cursor_execute", waited)
    try:
        again = start(client, identity)
    finally:
        event.remove(db_engine, "before_cursor_execute", waited)
    assert again.status_code == 200, again.text
    assert again.json()["startedAt"] == NOW.isoformat()


def test_unique_constraint_returns_the_trial_already_started(ctx, session, monkeypatch):
    """没读到已建成的那段（并发）时，插入撞唯一约束：返回同一段，不是 500。"""
    client = ctx.client
    identity = join(client)
    assert start(client, identity).status_code == 200
    real, calls = boost.trial_of, []

    def missed_once(db, user_id):
        calls.append(user_id)
        return None if len(calls) == 1 else real(db, user_id)

    monkeypatch.setattr(boost, "trial_of", missed_once)
    again = start(client, identity)
    assert again.status_code == 200, again.text
    assert again.json()["startedAt"] == NOW.isoformat()
    assert session.scalar(select(func.count(BoostTrial.id))) == 1


def test_start_sees_a_pass_granted_while_waiting_for_the_user_lock(
    ctx, session_factory, monkeypatch
):
    """读用户之后、锁用户之前别处刚发了使用期：锁后读得到（连接是 READ COMMITTED），不能开始体验。"""
    client = ctx.client
    identity = join(client)
    real = boost.trial_enabled

    def granted_meanwhile(settings):
        with session_factory() as other:
            owner = other.scalar(select(User).where(User.openid == "trial-user"))
            rewards.grant(other, owner.id, "correction", 30, "correction", 9, NOW)
            other.commit()
        return real(settings)

    monkeypatch.setattr(boost, "trial_enabled", granted_meanwhile)
    blocked = start(client, identity)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "TRIAL_HAS_PASS"


def test_trial_does_not_touch_monthly_free_quota_and_caps_ten_a_day(ctx, session):
    client, clock = ctx.client, ctx.clock
    identity = join(client)
    for _ in range(3):
        assert submit(client, identity).status_code == 202
    limit = submit(client, identity)
    assert limit.status_code == 429 and limit.json()["detail"]["code"] == "FREE_MONTHLY_LIMIT"
    wide = submit(client, identity, 51)
    assert wide.status_code == 429 and wide.json()["detail"]["code"] == "FREE_ROW_LIMIT"
    assert status(client, identity)["trial"]["available"] is True

    assert start(client, identity).status_code == 200
    first = submit(client, identity, 500)
    assert first.status_code == 202, first.text
    assert first.json()["usageSource"] == "trial"
    over = submit(client, identity, 501)
    assert over.status_code == 429
    assert over.json()["detail"] == {
        "code": "TRIAL_ROW_LIMIT",
        "message": "每次最多提交 500 行",
        "maxRows": 500,
    }
    # 两项合计每天 10 次；50 家以内的也记在体验上
    for n in range(9):
        response = submit(client, identity, 3, kind="track" if n % 2 else "compare")
        assert response.status_code == 202 and response.json()["usageSource"] == "trial"
    capped = submit(client, identity)
    assert capped.status_code == 429
    assert capped.json()["detail"]["code"] == "TRIAL_DAILY_LIMIT"
    body = status(client, identity)
    assert body["trial"]["usedToday"] == 10
    assert body["quota"]["compare"]["used"] == 3 and body["quota"]["track"]["used"] == 0

    # 北京时间 0 点重新计数；体验账本按天、每月免费次数一次没少
    clock.at = datetime(2026, 10, 21, 0, 0, tzinfo=BJ)
    assert status(client, identity)["trial"]["usedToday"] == 0
    assert submit(client, identity, 200).status_code == 202
    user_id = session.scalar(select(User.id))
    ledger = dict(
        session.execute(
            select(AdvisorUsage.kind, func.sum(AdvisorUsage.used_count))
            .where(AdvisorUsage.user_id == user_id)
            .group_by(AdvisorUsage.kind)
        ).all()
    )
    assert ledger == {"compare": 3, "trial": 11}

    # 结束后回到免费版：本月免费已用完，下个月照常
    clock.at = NOW + timedelta(hours=72)
    assert submit(client, identity, 51).json()["detail"]["code"] == "FREE_ROW_LIMIT"
    assert submit(client, identity).json()["detail"]["code"] == "FREE_MONTHLY_LIMIT"
    assert submit(client, identity, kind="track").status_code == 202
    clock.at = datetime(2026, 11, 1, 0, 0, tzinfo=BJ)
    assert submit(client, identity).status_code == 202


def test_failed_cancelled_and_deleted_trial_tasks_give_back_the_day(ctx, session, monkeypatch):
    client = ctx.client
    identity = join(client)
    assert start(client, identity).status_code == 200
    cancelled = submit(client, identity, 80).json()["id"]
    deleted = submit(client, identity, 80).json()["id"]
    failing = submit(client, identity, 80).json()["id"]
    assert status(client, identity)["trial"]["usedToday"] == 3
    assert client.post(f"/api/v1/advisor/tasks/{cancelled}/cancel", headers=identity).json()[
        "status"
    ] == ("cancelled")
    assert client.delete(f"/api/v1/advisor/tasks/{deleted}", headers=identity).status_code == 204
    assert status(client, identity)["trial"]["usedToday"] == 1

    monkeypatch.setattr(
        advisor_tasks, "_run_engine", lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    for _ in range(3):
        advisor_tasks.run_advisor_tasks(
            session, settings=ctx.settings, stamp=NOW.astimezone(UTC), max_tasks=1
        )
    row = session.get(AdvisorTask, failing)
    session.refresh(row)
    assert row.status == "failed" and row.usage_released is True
    body = status(client, identity)
    # 失败退回的是体验当天的次数，不碰每月免费次数
    assert body["trial"]["usedToday"] == 0 and body["quota"]["compare"]["used"] == 0
    # 重复退回安全
    advisor_tasks.release_usage(session, row)
    session.commit()
    assert status(client, identity)["trial"]["usedToday"] == 0


def test_trial_results_still_open_after_the_trial_ends(ctx, session, monkeypatch):
    client, clock = ctx.client, ctx.clock
    identity = join(client)
    assert start(client, identity).status_code == 200
    task_id = submit(client, identity, 300).json()["id"]
    row = session.get(AdvisorTask, task_id)
    row.status, row.finished_at = "done", NOW.astimezone(UTC)
    row.result_file_id, row.result_rows, row.result_document_ids = "cloud://trial", 300, []
    session.commit()
    monkeypatch.setattr(advisor_tasks, "download_url", lambda *args: "https://download")
    monkeypatch.setattr(advisor_tasks, "result_size", lambda *args: 4)
    monkeypatch.setattr(advisor_tasks, "read_result_part", lambda *args: b"xlsx")

    clock.at = NOW + timedelta(days=10)
    assert status(client, identity)["trial"]["active"] is False
    detail = client.get(f"/api/v1/advisor/tasks/{task_id}", headers=identity).json()
    assert detail["hasFile"] is True and detail["usageSource"] == "trial"
    assert client.get(f"/api/v1/advisor/tasks/{task_id}/download", headers=identity).json()[
        "size"
    ] == (4)
    part = client.get(f"/api/v1/advisor/tasks/{task_id}/file?part=0", headers=identity)
    assert part.status_code == 200 and part.content == b"xlsx"


def test_pass_holder_cannot_start_and_overlap_uses_trial_first(ctx, session):
    client = ctx.client
    holder = join(client, "pass-holder")
    owner = session.scalar(select(User).where(User.openid == "pass-holder"))
    rewards.grant(session, owner.id, "correction", 30, "correction", 1, NOW)
    session.commit()
    body = status(client, holder)["trial"]
    assert body["available"] is False and body["reason"] == "pass"
    blocked = start(client, holder)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "TRIAL_HAS_PASS"

    # 先开始体验、后来才有使用期：体验当天的次数先用，用完再用使用期
    identity = join(client)
    assert start(client, identity).status_code == 200
    user = session.scalar(select(User).where(User.openid == "trial-user"))
    rewards.grant(session, user.id, "correction", 5, "correction", 2, NOW)
    session.commit()
    sources = [submit(client, identity, 60).json()["usageSource"] for _ in range(11)]
    assert sources == ["trial"] * 10 + ["pass"]


def test_switch_off_keeps_started_trials_but_allows_no_new_ones(ctx):
    client, settings = ctx.client, ctx.settings
    identity = join(client)
    assert start(client, identity).status_code == 200
    settings.boost_trial_enabled = False
    assert status(client, identity)["trial"]["active"] is True
    assert submit(client, identity, 100).json()["usageSource"] == "trial"
    newcomer = join(client, "newcomer")
    assert status(client, newcomer)["trial"] is None
    assert start(client, newcomer).status_code == 404


def test_account_deletion_deletes_the_trial_and_keeps_no_credential(ctx, session):
    """注销即删（D-071 第②条 10/18 改）：体验记录随账号删除，不留任何凭据；
    同一个微信重新同意算新账号，可以再体验一次。没用过的注销同样什么也不留。"""
    client = ctx.client
    plain = join(client, "never-tried")
    assert client.delete("/api/v1/me", headers=plain).status_code == 204

    identity = join(client)
    assert start(client, identity).status_code == 200
    assert client.get("/api/v1/me/data", headers=identity).json()["boostTrial"] == {
        "startedAt": NOW.isoformat(),
        "endsAt": (NOW + timedelta(hours=72)).isoformat(),
    }
    assert client.delete("/api/v1/me", headers=identity).status_code == 204
    session.expire_all()
    assert session.scalar(select(func.count(BoostTrial.id))) == 0

    identity = join(client)
    assert status(client, identity)["trial"]["available"] is True
    assert start(client, identity).status_code == 200


@pytest.fixture
def season(ctx):
    ctx.settings.season_boost_enabled = True
    ctx.settings.season_boost_start = date(2026, 10, 20)
    ctx.settings.season_boost_end = date(2026, 10, 25)
    return ctx


def test_season_boost_raises_monthly_free_quota_between_dates(season):
    client, clock = season.client, season.clock
    clock.at = datetime(2026, 10, 13, 9, 0, tzinfo=BJ)
    identity = join(client)
    early = status(client, identity)
    # 提前公布：还没开始时显示起止日期，次数仍是 3 次
    assert early["season"] == {
        "start": "2026-10-20",
        "end": "2026-10-25",
        "active": False,
        "limit": 5,
        "normalLimit": 3,
        "maxRows": 50,
    }
    assert early["quota"]["compare"]["limit"] == 3

    clock.at = NOW
    body = status(client, identity)
    assert body["season"]["active"] is True and body["quota"]["track"]["limit"] == 5
    for _ in range(5):
        assert submit(client, identity).status_code == 202
    assert submit(client, identity).json()["detail"]["code"] == "FREE_MONTHLY_LIMIT"
    # 每次仍是 50 家以内
    assert submit(client, identity, 51, kind="track").json()["detail"]["code"] == "FREE_ROW_LIMIT"
    caps = client.get("/api/v1/advisor/capabilities", headers=identity).json()
    assert caps["data_tools"]["remaining"] == {"compare": 0, "track": 5}

    # 结束日当天仍算；过了结束日回到 3 次，多用的不倒扣，起止日期不再显示
    clock.at = datetime(2026, 10, 25, 23, 59, tzinfo=BJ)
    assert status(client, identity)["quota"]["compare"]["limit"] == 5
    clock.at = datetime(2026, 10, 26, 0, 0, tzinfo=BJ)
    after = status(client, identity)
    assert after["season"] is None
    assert after["quota"]["compare"] == {"used": 5, "limit": 3, "maxRows": 50}
    caps = client.get("/api/v1/advisor/capabilities", headers=identity).json()
    assert caps["data_tools"]["remaining"] == {"compare": 0, "track": 3}


def test_season_boost_needs_both_dates_in_order():
    with pytest.raises(ValidationError, match="SEASON_BOOST"):
        Settings(_env_file=None, season_boost_enabled=True)
    with pytest.raises(ValidationError, match="SEASON_BOOST"):
        Settings(
            _env_file=None,
            season_boost_enabled=True,
            season_boost_start="2026-11-30",
            season_boost_end="2026-11-01",
        )
    ready = Settings(
        _env_file=None,
        season_boost_enabled=True,
        season_boost_start="2026-11-01",
        season_boost_end="2026-11-30",
    )
    assert (ready.season_boost_start, ready.season_boost_end) == (
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    # 云托管环境变量留空等于没填
    assert Settings(_env_file=None, season_boost_start="").season_boost_start is None
    assert boost.season_window(SimpleNamespace(season_boost_enabled=True)) is None


def test_restricted_api_account_runs_the_trial(ctx, restricted_api_factory, monkeypatch):
    """按部署文件授权（含加量体验权限）：开始、提交、取消退次数、注销都不缺权限。"""
    from youju import access_log

    client = ctx.client
    ctx.settings.season_boost_enabled = True
    ctx.settings.season_boost_start = date(2026, 10, 1)
    ctx.settings.season_boost_end = date(2026, 10, 31)
    with restricted_api_factory() as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        identity = join(client, "limited-trial")
        assert status(client, identity)["trial"]["available"] is True
        assert start(client, identity).status_code == 200
        task = submit(client, identity, 120)
        assert task.status_code == 202 and task.json()["usageSource"] == "trial"
        cancel = client.post(f"/api/v1/advisor/tasks/{task.json()['id']}/cancel", headers=identity)
        assert cancel.status_code == 200
        assert status(client, identity)["trial"]["usedToday"] == 0
        assert client.get("/api/v1/me/data", headers=identity).status_code == 200
        assert client.delete("/api/v1/me", headers=identity).status_code == 204
        # 注销即删：重新同意算新账号，受限账号也能再开始
        identity = join(client, "limited-trial")
        assert start(client, identity).status_code == 200


def test_restricted_api_without_trial_grants_while_switch_is_off(
    ctx, restricted_api_factory, monkeypatch
):
    """迁移后还没执行新授权、体验开关关着：申报季加量照常，注销照常。"""
    from youju import access_log

    client = ctx.client
    ctx.settings.boost_trial_enabled = False
    ctx.settings.season_boost_enabled = True
    ctx.settings.season_boost_start = date(2026, 10, 1)
    ctx.settings.season_boost_end = date(2026, 10, 31)
    with restricted_api_factory(new_grants=False) as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        identity = join(client, "limited-off")
        assert start(client, identity).status_code == 404
        for _ in range(5):
            assert submit(client, identity).status_code == 202
        assert status(client, identity)["quota"]["compare"]["used"] == 5
        assert client.delete("/api/v1/me", headers=identity).status_code == 204
