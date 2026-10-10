"""奖励使用期、一次成功触发、同行引荐及后台操作；只连接指定测试库。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from youju import advisor_tasks, cli, payments, rewards
from youju.api import advisor, me, pay, v1
from youju.api import app as app_module
from youju.backup import dump, restore
from youju.config import Settings
from youju.db import get_session
from youju.models import (
    AdvisorTask,
    Feedback,
    PassReward,
    PayOrder,
    RewardClaim,
    User,
    WorkbenchPass,
)

NOW = datetime(2026, 10, 10, 4, tzinfo=UTC)
NEXT_MONTH = datetime(2026, 10, 31, 16, tzinfo=UTC)
ADMIN = {"X-Admin-Token": "test-admin"}


def headers(openid):
    return {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": openid}


@pytest.fixture
def context(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        rewards_enabled=True,
        referral_enabled=True,
        advisor_enabled=True,
        trust_wx_headers=True,
        cloud_env="test-env",
        admin_token="test-admin",
    )
    for module in (me, advisor, pay, rewards, app_module):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(
        advisor_tasks,
        "_run_engine",
        lambda session, task, output: output.write_bytes(b"test") and 1,
    )
    monkeypatch.setattr(advisor_tasks, "upload", lambda *args, **kwargs: "cloud://test-result")
    monkeypatch.setattr(advisor_tasks, "delete_result_files", lambda client, env, ids: ids)

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    with TestClient(app_module.app) as client:
        yield client, settings
    app_module.app.dependency_overrides.clear()


def user(session, name="reward-user", **values):
    row = User(
        openid=name,
        agreement_version="2026-10",
        agreement_at=NOW,
        advisor_agreement_version=advisor_tasks.ADVISOR_AGREEMENT_VERSION,
        advisor_agreement_at=NOW,
        **values,
    )
    session.add(row)
    session.commit()
    return row


def task(session, owner, *, kind="compare", status="queued", key=None):
    row = AdvisorTask(
        user_id=owner.id,
        kind=kind,
        status=status,
        input_rows=["单位"],
        row_count=1,
        idempotency_key=key or f"task-{session.scalar(select(func.count(AdvisorTask.id)))}",
        created_at=NOW,
        finished_at=NOW if status == "done" else None,
        expires_at=NOW + timedelta(days=30),
    )
    session.add(row)
    session.commit()
    return row


def award(session, owner, kind="correction", days=1, ref=1, stamp=NOW, ref_type=None):
    row = rewards.grant(session, owner.id, kind, days, ref_type or kind, ref, stamp)
    session.commit()
    return row


def ticket(session, reward):
    return session.scalar(select(WorkbenchPass).where(WorkbenchPass.reward_id == reward.id))


def paid(session, owner, start=NOW):
    order = PayOrder(
        user_id=owner.id,
        out_trade_no=f"order-{owner.id}",
        product_id="workbench_30d",
        price=100,
        env=1,
        terms_version="test",
        status="completed",
        platform="android",
        paid_time=NOW,
    )
    session.add(order)
    session.flush()
    row = WorkbenchPass(
        user_id=owner.id, order_id=order.id, starts_at=start, ends_at=start + timedelta(days=30)
    )
    session.add(row)
    session.commit()
    return order, row


@pytest.mark.parametrize(
    "kind,days",
    [("first_batch", 3), ("correction", 1), ("source", 3), ("referral", 7), ("deadline_clue", 1)],
)
def test_grant_types_and_dedup(session, kind, days):
    owner = user(session)
    row = award(session, owner, kind, days)
    segment = ticket(session, row)
    assert row.requested_days == row.granted_days == days
    assert (segment.starts_at, segment.ends_at, segment.order_id) == (
        NOW,
        NOW + timedelta(days=days),
        None,
    )
    assert award(session, owner, kind, days, stamp=NEXT_MONTH).id == row.id
    assert session.scalar(select(func.count(PassReward.id))) == 1
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 1


def test_first_batch_once_even_different_reference_and_deleted_task(session):
    owner = user(session)
    original = award(session, owner, "first_batch", 3)
    assert award(session, owner, "first_batch", 3, ref=2).id == original.id


def test_month_limit_partial_zero_and_beijing_boundary(session):
    owner = user(session)
    before = NEXT_MONTH - timedelta(microseconds=1)
    for i in range(29):
        award(session, owner, ref=i, stamp=before)
    partial = award(session, owner, "referral", 7, ref=101, stamp=before)
    zero = award(session, owner, "source", 3, ref=102, stamp=before)
    assert partial.granted_days == 1 and partial.requested_days == 7
    assert partial.note == zero.note == "本月奖励已到上限"
    assert zero.granted_days == 0 and ticket(session, zero) is None
    assert award(session, owner, "referral", 7, ref=103, stamp=NEXT_MONTH).granted_days == 7
    assert award(session, owner, "source", 3, ref=102, stamp=NEXT_MONTH).id == zero.id
    assert (
        sum(r.granted_days for r in session.scalars(rewards.monthly(session, owner.id, before)))
        == 30
    )
    assert (
        sum(r.granted_days for r in session.scalars(rewards.monthly(session, owner.id, NEXT_MONTH)))
        == 7
    )


@pytest.mark.parametrize(
    "kind,days,limit", [("source", 3, 2), ("referral", 7, 4), ("deadline_clue", 1, 10)]
)
def test_kind_month_limits_and_cross_month(session, kind, days, limit):
    owner = user(session)
    assert [award(session, owner, kind, days, ref=i).granted_days for i in range(limit)] == [
        days
    ] * limit
    denied = award(session, owner, kind, days, ref=limit)
    assert denied.granted_days == 0 and "上限" in denied.note
    assert ticket(session, denied) is None
    assert award(session, owner, kind, days, ref=limit, stamp=NEXT_MONTH).id == denied.id
    assert award(session, owner, kind, days, ref=limit + 1, stamp=NEXT_MONTH).granted_days == days


@pytest.mark.parametrize("state", ["none", "active", "future", "expired", "revoked"])
def test_start_and_extension(session, state):
    owner = user(session)
    expected = NOW
    if state != "none":
        _, segment = paid(
            session,
            owner,
            NOW - timedelta(days=31)
            if state == "expired"
            else NOW + timedelta(days=1)
            if state == "future"
            else NOW,
        )
        if state == "revoked":
            segment.revoked_at = NOW
            session.commit()
        if state in ("active", "future"):
            expected = segment.ends_at
    segment = ticket(session, award(session, owner, "first_batch", 3))
    assert segment.starts_at == expected and segment.ends_at == expected + timedelta(days=3)


def test_pass_origin_database_constraint(session):
    owner = user(session)
    reward = award(session, owner)
    order, _ = paid(session, owner)
    for order_id, reward_id in [(None, None), (order.id, reward.id)]:
        with pytest.raises(DBAPIError), session.begin_nested():
            session.add(
                WorkbenchPass(
                    user_id=owner.id,
                    order_id=order_id,
                    reward_id=reward_id,
                    starts_at=NOW,
                    ends_at=NOW + timedelta(days=1),
                )
            )
            session.flush()


def test_concurrent_grants_same_user_cap_and_reference(session_factory):
    with session_factory() as session:
        owner_id = user(session).id

    def issue(ref):
        with session_factory() as s:
            row = rewards.grant(s, owner_id, "correction", 1, "feedback", ref, NOW)
            s.commit()
            return row.id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(issue, [1] * 8 + list(range(2, 35))))
    with session_factory() as session:
        assert len(set(ids[:8])) == 1
        assert session.scalar(select(func.sum(PassReward.granted_days))) == 30
        assert session.scalar(select(func.count(PassReward.id))) == 34


def test_invite_code_fixed_private_and_collision(context, session, monkeypatch):
    client, _ = context
    owner = user(session, "private-openid")
    other = user(session, "other", invite_code="existingcode")
    codes = iter([other.invite_code, "random-code"])
    monkeypatch.setattr(rewards.secrets, "token_urlsafe", lambda _: next(codes))
    body = client.get("/api/v1/me/invite", headers=headers(owner.openid)).json()
    assert (
        body["inviteCode"] == "random-code" and body["monthCount"] == 0 and body["monthLimit"] == 4
    )
    assert client.get("/api/v1/me/invite", headers=headers(owner.openid)).json() == body
    assert set(body) == {"inviteCode", "monthCount", "monthLimit", "rules"}
    assert "private-openid" not in str(body) and "other" not in str(body)


@pytest.mark.parametrize("case", ["new", "old", "self", "invalid", "missing", "advisor_scope"])
def test_registration_invite_cases(context, session, case):
    client, _ = context
    inviter = user(session, "inviter", invite_code="fixed-invite")
    openid = "inviter" if case == "self" else "new-user"
    old = user(session, openid) if case in ("old", "advisor_scope") else None
    code = "invalid" if case == "invalid" else "fixed-invite"
    body = {"version": "2026-10", **({"invite_code": code} if case != "missing" else {})}
    if case == "advisor_scope":
        body.update(version=advisor_tasks.ADVISOR_AGREEMENT_VERSION, scope="advisor")
    assert (
        client.post("/api/v1/me/agreement", headers=headers(openid), json=body).status_code == 200
    )
    session.expire_all()
    registered = session.scalar(select(User).where(User.openid == openid))
    assert registered.invited_by_user_id == (inviter.id if case == "new" else None)
    assert (registered.invited_at is not None) == (case == "new")
    if old:
        assert registered.id == old.id
    if case == "new":
        user(session, "second-inviter", invite_code="second-code")
        assert (
            client.post(
                "/api/v1/me/agreement",
                headers=headers(openid),
                json={"version": "2026-10", "invite_code": "second-code"},
            ).status_code
            == 200
        )
        session.refresh(registered)
        assert registered.invited_by_user_id == inviter.id and registered.invited_at == NOW


@pytest.mark.parametrize("kind", ["compare", "track"])
def test_first_success_auto_referral_once_and_one_level(context, session, kind):
    _, settings = context
    grandparent = user(session, "grandparent")
    inviter = user(session, "inviter", invited_by_user_id=grandparent.id, invited_at=NOW)
    newcomer = user(session, "newcomer", invited_by_user_id=inviter.id, invited_at=NOW)
    row = task(session, newcomer, kind=kind)
    assert (
        advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)["done"]
        == 1
    )
    session.refresh(newcomer)
    assert newcomer.batch_completed_at == NOW
    history = session.scalars(select(PassReward).order_by(PassReward.id)).all()
    assert [(r.user_id, r.kind, r.granted_days) for r in history] == [
        (newcomer.id, "first_batch", 3),
        (inviter.id, "referral", 7),
    ]
    assert advisor_tasks.process_task(session, row, settings=settings, stamp=NOW)
    task(session, newcomer, kind=kind, key="second")
    advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)
    assert session.scalar(select(func.count(PassReward.id))) == 2
    assert (
        context[0].get("/api/v1/me/invite", headers=headers(inviter.openid)).json()["monthCount"]
        == 1
    )
    assert "newcomer" not in str(
        context[0].get("/api/v1/me/rewards", headers=headers(inviter.openid)).json()
    )
    session.delete(row)
    session.commit()
    task(session, newcomer, key="third")
    advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)
    assert session.scalar(select(func.count(PassReward.id))) == 2


def test_failed_task_no_award_then_success(context, session, monkeypatch):
    _, settings = context
    inviter = user(session, "inviter")
    newcomer = user(session, invited_by_user_id=inviter.id)
    row = task(session, newcomer)
    row.attempts = 3
    row.status = "running"
    session.commit()
    monkeypatch.setattr(
        advisor_tasks, "_run_engine", lambda *args: (_ for _ in ()).throw(RuntimeError())
    )
    assert advisor_tasks.process_task(session, row, settings=settings, stamp=NOW) is False
    session.refresh(row)
    session.refresh(newcomer)
    assert row.status == "failed" and newcomer.batch_completed_at is None
    assert session.scalar(select(func.count(PassReward.id))) == 0


def test_fifth_referral_never_deferred(context, session):
    _, settings = context
    inviter = user(session, "inviter")
    for i in range(5):
        newcomer = user(session, f"new-{i}", invited_by_user_id=inviter.id, invited_at=NOW)
        task(session, newcomer)
        advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)
    rows = session.scalars(
        select(PassReward).where(PassReward.kind == "referral").order_by(PassReward.id)
    ).all()
    assert [r.granted_days for r in rows] == [7, 7, 7, 7, 0]
    assert rewards.reconcile(session, settings=settings, stamp=NEXT_MONTH) == {
        "granted": 0,
        "failed": 0,
    }
    assert (
        context[0].get("/api/v1/me/invite", headers=headers(inviter.openid)).json()["monthCount"]
        == 4
    )


@pytest.mark.parametrize("broken_kind", ["first_batch", "referral", "both"])
def test_award_failure_no_task_failure_or_next_task_retry_reconcile(
    context, session, monkeypatch, broken_kind
):
    _, settings = context
    inviter = user(session, "inviter")
    newcomer = user(session, invited_by_user_id=inviter.id, invited_at=NOW)
    first = task(session, newcomer)
    original = rewards.grant

    def broken(s, user_id, kind, *args):
        if broken_kind in (kind, "both"):
            original(s, user_id, kind, *args)
            raise RuntimeError("must never enter log or task error")
        return original(s, user_id, kind, *args)

    monkeypatch.setattr(rewards, "grant", broken)
    assert (
        advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)["done"]
        == 1
    )
    session.refresh(first)
    assert first.status == "done" and first.error is None and first.result_file_id
    count = session.scalar(select(func.count(PassReward.id)))
    assert count == (0 if broken_kind == "both" else 1)
    monkeypatch.setattr(rewards, "grant", original)
    task(session, newcomer, key="next")
    advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)
    assert session.scalar(select(func.count(PassReward.id))) == count
    session.delete(first)
    session.commit()
    report = rewards.reconcile(session, settings=settings, stamp=NOW)
    assert report == {"granted": 2 - count, "failed": 0}
    assert session.scalar(select(func.count(PassReward.id))) == 2
    assert rewards.reconcile(session, settings=settings, stamp=NOW)["granted"] == 0


def test_reconcile_legacy_done_only_and_cli(context, session):
    completed = user(session, "done")
    failed = user(session, "failed")
    task(session, completed, status="done")
    task(session, failed, status="failed")
    assert cli.main(["rewards-reconcile"]) == 0
    assert [(r.user_id, r.kind) for r in session.scalars(select(PassReward))] == [
        (completed.id, "first_batch")
    ]
    session.refresh(completed)
    assert completed.batch_completed_at == NOW


@pytest.mark.parametrize("switch", ["rewards_enabled", "advisor_enabled"])
def test_switch_disabled_api_registration_admin_auto_and_cli(context, session, switch):
    client, settings = context
    inviter = user(session, "inviter", invite_code="fixed-code")
    setattr(settings, switch, False)
    for path in ("/api/v1/me/rewards", "/api/v1/me/invite"):
        assert client.get(path).status_code == 404
        assert client.get(path, headers=headers(inviter.openid)).status_code == 404
    assert (
        client.post(
            "/api/v1/me/agreement",
            headers=headers("new"),
            json={"version": "2026-10", "invite_code": "fixed-code"},
        ).status_code
        == 200
    )
    newcomer = session.scalar(select(User).where(User.openid == "new"))
    assert newcomer.invited_by_user_id is None
    row = task(session, newcomer)
    assert advisor_tasks.process_task(session, row, settings=settings, stamp=NOW)
    assert session.scalar(select(func.count(PassReward.id))) == 0
    assert cli.main(["rewards-reconcile"]) == 0
    fb = Feedback(user_id=inviter.id, content="标题错误", check_result="pass")
    session.add(fb)
    session.commit()
    item = client.get("/api/admin/feedback", headers=ADMIN).json()[0]
    assert item["rewardsEnabled"] is False
    assert (
        client.post(f"/api/admin/feedback/{fb.id}/reward/correction", headers=ADMIN).status_code
        == 404
    )
    assert Settings(_env_file=None).rewards_enabled is False


@pytest.mark.parametrize("kind", ["compare", "track"])
def test_reward_pass_quota_500_unlimited_and_status(context, session, kind):
    client, _ = context
    owner = user(session)
    reward = award(session, owner, "first_batch", 3)
    for i in range(6):
        rows = (
            [f"单位 {j}" for j in range(500)]
            if kind == "compare"
            else [{"name": f"单位 {j}"} for j in range(500)]
        )
        response = client.post(
            "/api/v1/advisor/tasks",
            headers={**headers(owner.openid), "Idempotency-Key": str(i)},
            json={"kind": kind, "rows": rows},
        )
        assert response.status_code == 202, response.text
        assert response.json()["usageSource"] == "pass"
    body = client.get("/api/v1/advisor/status", headers=headers(owner.openid)).json()
    assert body["quota"][kind]["maxRows"] == 500
    assert body["quota"][kind]["used"] == 0 and body["pass"]["active"]
    rows.append(rows[-1])
    response = client.post(
        "/api/v1/advisor/tasks",
        headers={**headers(owner.openid), "Idempotency-Key": "over"},
        json={"kind": kind, "rows": rows},
    )
    assert response.status_code == 429 and response.json()["detail"]["code"] == "PASS_ROW_LIMIT"
    assert not payments.paid_usage(session, ticket(session, reward))


def test_reward_usage_does_not_block_order_refund_and_reward_moves(context, session):
    client, _ = context
    owner = user(session)
    prior_reward = award(session, owner, "first_batch", 3)
    reward_segment = ticket(session, prior_reward)
    order, bought = paid(session, owner, reward_segment.ends_at)
    used = task(session, owner)
    used.pass_id, used.usage_source = reward_segment.id, "pass"
    reward_segment.last_used_at = NOW
    session.commit()
    assert payments.refund_reason(session, order, NOW) is None
    assert not payments.paid_usage(session, reward_segment)
    assert client.get("/api/v1/pay/orders", headers=headers(owner.openid)).json()["items"][0][
        "canRefund"
    ]
    appended = ticket(session, award(session, owner, "source", 3))
    assert appended.starts_at == bought.ends_at
    payments.lock_order(session, order.id)
    payments.revoke_pass(session, order, NOW)
    session.commit()
    assert appended.starts_at == reward_segment.ends_at
    assert appended.ends_at == reward_segment.ends_at + timedelta(days=3)
    assert payments.pass_status(session, owner.id, NOW)["expiresAt"] == appended.ends_at
    assert len(client.get("/api/v1/pay/orders", headers=headers(owner.openid)).json()["items"]) == 1


@pytest.mark.parametrize("who", ["inviter", "newcomer"])
def test_delete_account_clears_relations_preserves_others_rewards(context, session, who):
    client, settings = context
    inviter = user(session, "inviter")
    newcomer = user(
        session, "newcomer", invited_by_user_id=inviter.id, invited_at=NOW, batch_completed_at=NOW
    )
    rewards.first_completion(session, newcomer.id, NOW, settings=settings)
    deleted, survivor = (inviter, newcomer) if who == "inviter" else (newcomer, inviter)
    survivor_id, deleted_id = survivor.id, deleted.id
    survivor_pass_id = session.scalar(
        select(WorkbenchPass.id).where(WorkbenchPass.user_id == survivor_id)
    )
    settings.rewards_enabled = False
    assert client.delete("/api/v1/me", headers=headers(deleted.openid)).status_code == 204
    session.expire_all()
    assert session.get(User, deleted_id) is None
    surviving_pass = session.get(WorkbenchPass, survivor_pass_id)
    assert surviving_pass.user_id == survivor_id and surviving_pass.revoked_at is None
    erased = session.scalar(select(PassReward).where(PassReward.user_id.is_(None)))
    assert erased is not None and ticket(session, erased).revoked_at is not None
    assert ticket(session, erased).user_id is None
    assert (
        session.scalar(
            select(PassReward.id).where(
                PassReward.ref_type.in_(("user_referral", "user_first_batch")),
                PassReward.ref_id == deleted_id,
            )
        )
        is None
    )
    if who == "inviter":
        assert session.get(User, survivor_id).invited_by_user_id is None
        assert session.get(User, survivor_id).invited_at is None


@pytest.mark.parametrize(
    "kind,content,days", [("correction", "截止有误", 1), ("source", "申请覆盖：科技厅", 3)]
)
def test_admin_buttons_authorization_repeat_state_and_audit(
    context, session, caplog, kind, content, days
):
    client, _ = context
    owner = user(session)
    fb = Feedback(user_id=owner.id, content=content, check_result="pass")
    session.add(fb)
    session.commit()
    url = f"/api/admin/feedback/{fb.id}/reward/{kind}"
    assert client.post(url).status_code == 401
    app_module.audit_log.addHandler(caplog.handler)
    try:
        response = client.post(url, headers=ADMIN)
        assert response.status_code == 200 and response.json()["days"] == days
        duplicate = client.post(url, headers=ADMIN).json()
        assert duplicate == {"alreadyGranted": True, "days": days, "message": "已经发过"}
    finally:
        app_module.audit_log.removeHandler(caplog.handler)
    assert "feedback_reward" in caplog.text
    session.refresh(fb)
    assert fb.handled_at is not None
    item = client.get("/api/admin/feedback?status=all", headers=ADMIN).json()[0]
    assert item["rewardKind"] == kind and item["rewardDays"] == days and item["rewardsEnabled"]
    other = "source" if kind == "correction" else "correction"
    assert client.post(url.rsplit("/", 1)[0] + "/" + other, headers=ADMIN).json()["alreadyGranted"]
    assert session.scalar(select(func.count(PassReward.id))) == 1
    page = client.get("/admin/feedback").text
    assert (
        "采纳并送 1 天" in page and "已接入并送 3 天" in page and "if (item.rewardsEnabled)" in page
    )


def test_admin_wrong_type_and_missing(context, session):
    client, _ = context
    owner = user(session)
    fb = Feedback(user_id=owner.id, content="申请覆盖：科技厅", check_result="pass")
    session.add(fb)
    session.commit()
    assert (
        client.post(f"/api/admin/feedback/{fb.id}/reward/correction", headers=ADMIN).status_code
        == 422
    )
    assert client.post("/api/admin/feedback/999/reward/source", headers=ADMIN).status_code == 404
    assert session.scalar(select(func.count(PassReward.id))) == 0


def test_history_latest_50_total_actual_and_labels(context, session):
    client, _ = context
    owner = user(session)
    for i in range(55):
        award(session, owner, ref=i, stamp=NOW + timedelta(seconds=i))
    body = client.get("/api/v1/me/rewards", headers=headers(owner.openid)).json()
    assert body["monthDays"] == 30 and body["monthLimit"] == 30
    assert len(body["items"]) == 50 and body["items"][0]["days"] == 0
    assert body["items"][0]["name"] == "纠错被采纳"
    assert body["items"][0]["note"] == "本月奖励已到上限"
    forbidden = (
        "试用",
        "订阅",
        "自动续费",
        "充值",
        "余额",
        "积分",
        "返利",
        "佣金",
        "分销",
        "AI",
        "智能",
        "推荐",
    )
    visible = (
        str(body)
        + str(client.get("/api/v1/me/invite", headers=headers(owner.openid)).json())
        + str(rewards.LABELS)
    )
    assert not any(word in visible for word in forbidden)


@pytest.mark.parametrize("code", ["x" * 1000, 123, [], {}, "", None])
def test_invalid_code_does_not_block_registration(context, session, code):
    client, _ = context
    response = client.post(
        "/api/v1/me/agreement",
        headers=headers("new"),
        json={"version": "2026-10", "invite_code": code},
    )
    assert response.status_code == 200
    assert session.scalar(select(User).where(User.openid == "new")).invited_by_user_id is None


def test_success_before_upgrade_no_auto_catchup(context, session):
    _, settings = context
    owner = user(session)
    task(session, owner, status="done", key="old")
    task(session, owner, key="new")
    assert (
        advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)["done"]
        == 1
    )
    assert session.scalar(select(func.count(PassReward.id))) == 0
    assert rewards.reconcile(session, settings=settings, stamp=NOW)["granted"] == 1


def test_concurrent_referral_first_completion(session_factory, context):
    _, settings = context
    with session_factory() as session:
        inviter = user(session, "inviter")
        newcomer_id = user(
            session, "new", invited_by_user_id=inviter.id, invited_at=NOW, batch_completed_at=NOW
        ).id

    def complete(_):
        with session_factory() as session:
            return rewards.first_completion(session, newcomer_id, NOW, settings=settings)

    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(complete, range(8)))
    assert sum(r["granted"] for r in reports) == 2
    assert sum(r["failed"] for r in reports) == 0
    with session_factory() as session:
        assert session.scalar(select(func.count(PassReward.id))) == 2


def test_refund_api_after_reward_usage_advances_reward_segment(context, session):
    client, settings = context
    owner = user(session)
    first = ticket(session, award(session, owner, "first_batch", 3))
    first.last_used_at = NOW
    order, bought = paid(session, owner, first.ends_at)
    following = ticket(session, award(session, owner, "source", 3))
    session.commit()

    class RefundClient:
        calls = []

        def query(self, openid, trade, env):
            return {
                "order_id": trade,
                "openid": openid,
                "env_type": env,
                "status": 2,
                "order_fee": 100,
                "paid_fee": 100,
                "left_fee": 100,
                "order_type": 0,
            }

        def refund(self, body):
            self.calls.append(body)

    fake = RefundClient()
    fake.settings = settings
    app_module.app.dependency_overrides[pay.wx_client] = lambda: fake
    response = client.post(f"/api/v1/pay/orders/{order.id}/refund", headers=headers(owner.openid))
    assert response.status_code == 200 and response.json()["status"] == "退款中"
    session.refresh(bought)
    session.refresh(following)
    assert bought.revoked_at == NOW
    assert following.starts_at == first.ends_at and following.ends_at == first.ends_at + timedelta(
        days=3
    )
    assert (
        client.post(
            f"/api/v1/pay/orders/{order.id}/refund", headers=headers(owner.openid)
        ).status_code
        == 200
    )
    assert len(fake.calls) == 1


def test_delete_with_bought_and_reward_segments(context, session):
    client, _ = context
    owner = user(session)
    order, bought = paid(session, owner)
    reward = award(session, owner, "source", 3)
    reward_segment = ticket(session, reward)
    task(session, owner)
    assert client.delete("/api/v1/me", headers=headers(owner.openid)).status_code == 204
    for row in (order, bought, reward, reward_segment):
        session.refresh(row)
        assert row.user_id is None
    assert bought.revoked_at and reward_segment.revoked_at
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0


def test_admin_award_zero_and_repeat(context, session):
    client, _ = context
    owner = user(session)
    for i in range(30):
        award(session, owner, ref=i)
    feedback = Feedback(user_id=owner.id, content="日期错误", check_result="pass")
    session.add(feedback)
    session.commit()
    url = f"/api/admin/feedback/{feedback.id}/reward/correction"
    body = client.post(url, headers=ADMIN).json()
    assert body["days"] == 0 and body["note"] == "本月奖励已到上限"
    assert client.post(url, headers=ADMIN).json()["alreadyGranted"]
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 30


def test_backup_restore_invitations_rewards_and_passes(session, db_engine):
    from .test_backup import clear_restore_target

    inviter = user(session, "inviter", invite_code="fixed-code")
    newcomer = user(
        session, "new", invited_by_user_id=inviter.id, invited_at=NOW, batch_completed_at=NOW
    )
    reward = award(session, inviter, "referral", 7, ref=newcomer.id, ref_type="user_referral")
    reward_id, inviter_id, newcomer_id = reward.id, inviter.id, newcomer.id
    output = BytesIO()
    counts = dump(db_engine, output, now=NOW)
    clear_restore_target(session, db_engine)
    assert restore(db_engine, BytesIO(output.getvalue())) == counts
    session.expire_all()
    assert session.get(User, newcomer_id).invited_by_user_id == inviter_id
    assert session.get(User, inviter_id).invite_code == "fixed-code"
    restored = session.get(PassReward, reward_id)
    assert restored.user_id == inviter_id and restored.ref_id == newcomer_id
    assert ticket(session, restored).ends_at == NOW + timedelta(days=7)


def test_migration_down_up_preserves_order_segment_and_unlinks_reward_task(session, db_engine):
    from alembic import command
    from sqlalchemy import inspect, text

    from youju.migrations import alembic_config

    owner = user(session)
    order, bought = paid(session, owner)
    reward = award(session, owner, "source", 3)
    pending = task(session, owner)
    pending.pass_id = ticket(session, reward).id
    session.commit()
    order_id, task_id, bought_id = order.id, pending.id, bought.id
    config = alembic_config(str(db_engine.url))
    try:
        command.downgrade(config, "0026")
        assert "pass_rewards" not in inspect(db_engine).get_table_names()
        with db_engine.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT order_id FROM workbench_passes WHERE id=:id"), {"id": bought_id}
                )
                == order_id
            )
            assert (
                conn.scalar(text("SELECT pass_id FROM advisor_tasks WHERE id=:id"), {"id": task_id})
                is None
            )
    finally:
        command.upgrade(config, "head")
        session.expire_all()
    assert session.get(WorkbenchPass, bought_id).order_id == order_id


@pytest.mark.parametrize(
    "reward_on,referral_on", [(False, False), (False, True), (True, False), (True, True)]
)
def test_referral_separate_switch(context, session, reward_on, referral_on):
    client, settings = context
    settings.rewards_enabled, settings.referral_enabled = reward_on, referral_on
    inviter = user(session, "inviter", invite_code="fixed-code")
    active = reward_on and referral_on
    assert client.get("/api/v1/me/invite", headers=headers(inviter.openid)).status_code == (
        200 if active else 404
    )
    assert client.get("/api/v1/me/invite").status_code == (401 if active else 404)
    client.post(
        "/api/v1/me/agreement",
        headers=headers("new"),
        json={"version": "2026-10", "invite_code": "fixed-code"},
    )
    newcomer = session.scalar(select(User).where(User.openid == "new"))
    assert newcomer.invited_by_user_id == (inviter.id if active else None)
    # 已经存在的关系也不能绕过单独开关；补发同样受控。
    newcomer.invited_by_user_id, newcomer.batch_completed_at = inviter.id, NOW
    session.commit()
    rewards.first_completion(session, newcomer.id, NOW, settings=settings)
    rewards.reconcile(session, settings=settings, stamp=NOW)
    assert session.scalar(
        select(func.count(PassReward.id)).where(PassReward.kind == "first_batch")
    ) == int(reward_on)
    assert session.scalar(
        select(func.count(PassReward.id)).where(PassReward.kind == "referral")
    ) == int(active)
    assert Settings(_env_file=None).referral_enabled is False


@pytest.mark.parametrize("complete", [False, True])
def test_reregister_not_new_and_claim_retention(context, session, complete):
    from youju.notify import run_cleanup

    client, settings = context
    inviter = user(session, "inviter", invite_code="fixed-code")

    def register():
        assert (
            client.post(
                "/api/v1/me/agreement",
                headers=headers("repeat"),
                json={"version": "2026-10", "invite_code": "fixed-code"},
            ).status_code
            == 200
        )
        return session.scalar(select(User).where(User.openid == "repeat"))

    newcomer = register()
    assert newcomer.invited_by_user_id == inviter.id
    if complete:
        newcomer.batch_completed_at = NOW
        session.commit()
        assert (
            rewards.first_completion(session, newcomer.id, NOW, settings=settings)["granted"] == 2
        )
    assert client.delete("/api/v1/me", headers=headers("repeat")).status_code == 204
    session.expire_all()
    newcomer = register()
    assert newcomer.invited_by_user_id is None
    newcomer.batch_completed_at = NOW
    session.commit()
    assert rewards.first_completion(session, newcomer.id, NOW, settings=settings)["granted"] == int(
        not complete
    )
    claims = session.scalars(select(RewardClaim)).all()
    assert len(claims) == (3 if complete else 2)
    assert all(len(c.pseudonym) == 64 and "repeat" not in c.pseudonym for c in claims)
    assert (
        run_cleanup(
            session, settings=settings, stamp=NOW + timedelta(days=180) - timedelta(microseconds=1)
        )["reward_claims"]
        == 0
    )
    assert run_cleanup(session, settings=settings, stamp=NOW + timedelta(days=180))[
        "reward_claims"
    ] == len(claims)
