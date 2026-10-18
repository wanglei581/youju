"""奖励使用期和后台发奖；首次批量、同行引荐已删，注销不留防重复领取的凭据；只连接指定测试库。"""

import re
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
    Base,
    Feedback,
    PassReward,
    PayOrder,
    User,
    WorkbenchPass,
)

# 当前的用户协议版本：取后台默认值，升版本时不用改测试
AGREEMENT = Settings.model_fields["agreement_version"].default

NOW = datetime(2026, 10, 10, 4, tzinfo=UTC)
NEXT_MONTH = datetime(2026, 10, 31, 16, tzinfo=UTC)
ADMIN = {"X-Admin-Token": "test-admin"}


def headers(openid):
    return {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": openid}


@pytest.fixture(autouse=True)
def _legacy_correction_cap(monkeypatch):
    """这个文件测的是旧的奖励规则（各项合计每月 30 天），借"纠错被采纳"当没有单项上限的奖励。
    D-070 起纠错每人每月最多 3 天，在 test_sale.py 里测。"""
    monkeypatch.setattr(rewards, "CORRECTION_MONTH_DAYS", 1000)


@pytest.fixture
def context(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        rewards_enabled=True,
        # D-070 起纠错送天数只看自己的开关（rewards.correction_enabled）
        correction_reward_enabled=True,
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
        agreement_version=AGREEMENT,
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


def legacy_referral(session, inviter, newcomer):
    """以前（D-059 时期）留下的两笔奖励流水：首次批量给新用户、引荐给引荐人，ref_id 都是新用户的编号。
    两项已删，程序不再写这些数据，这里直接写库，模拟库里的旧数据。"""
    rows = {}
    for owner, kind, days, ref_type in (
        (newcomer, "first_batch", 3, "user_first_batch"),
        (inviter, "referral", 7, "user_referral"),
    ):
        reward = PassReward(
            user_id=owner.id,
            kind=kind,
            requested_days=days,
            granted_days=days,
            ref_type=ref_type,
            ref_id=newcomer.id,
            granted_at=NOW,
        )
        session.add(reward)
        session.flush()
        session.add(
            WorkbenchPass(
                user_id=owner.id,
                reward_id=reward.id,
                starts_at=NOW,
                ends_at=NOW + timedelta(days=days),
            )
        )
        rows[kind] = reward
    session.commit()
    return rows


@pytest.mark.parametrize("elapsed", [-2, 1.5, 3, 4])
def test_revoke_reward_advances_paid_and_reward_segments(session, elapsed):
    owner = user(session)
    other = user(session, "other")
    row = award(session, owner, "source", 3)
    segment = ticket(session, row)
    if elapsed < 0:
        segment.starts_at += timedelta(days=2)
        segment.ends_at += timedelta(days=2)
        session.commit()
    order, bought = paid(session, owner, segment.ends_at)
    following = ticket(session, award(session, owner, ref=2))
    untouched = ticket(session, award(session, other, ref=3))
    original = (segment.starts_at, segment.ends_at, bought.starts_at, following.starts_at)
    stamp = segment.starts_at + timedelta(days=elapsed)
    unused = timedelta(days=3 - max(0, min(3, elapsed)))

    assert rewards.revoke(session, row.id, "核实为虚假账号领奖", stamp) == "已收回"
    session.commit()
    assert segment.revoked_at == stamp
    assert (segment.starts_at, segment.ends_at) == original[:2]
    assert bought.starts_at == original[2] - unused
    assert bought.ends_at == bought.starts_at + timedelta(days=30)
    assert following.starts_at == original[3] - unused == bought.ends_at
    assert following.ends_at == following.starts_at + timedelta(days=1)
    assert untouched.starts_at == NOW and untouched.revoked_at is None
    assert row.granted_days == 3 and row.note == "已收回：核实为虚假账号领奖"
    assert order.status == "completed"

    assert (
        rewards.revoke(session, row.id, "再次核实账号违规", stamp + timedelta(hours=1))
        == "已经收回过"
    )
    session.commit()
    assert segment.revoked_at == stamp and row.note == "已收回：核实为虚假账号领奖"
    assert bought.starts_at == original[2] - unused
    assert following.starts_at == original[3] - unused


def test_revoke_preserves_prior_and_already_revoked_segments(session):
    owner = user(session)
    prior = ticket(session, award(session, owner, "source", 3))
    row = award(session, owner, ref=2)
    segment = ticket(session, row)
    removed = ticket(session, award(session, owner, ref=3))
    removed.revoked_at = NOW
    session.commit()
    after = ticket(session, award(session, owner, ref=4))
    before = (prior.starts_at, prior.ends_at, removed.starts_at, removed.ends_at)
    rewards.revoke(session, row.id, "核实为虚假账号领奖", NOW)
    session.commit()
    assert segment.revoked_at == NOW
    assert (prior.starts_at, prior.ends_at, removed.starts_at, removed.ends_at) == before
    assert after.starts_at == prior.ends_at and after.ends_at == prior.ends_at + timedelta(days=1)


@pytest.mark.parametrize(
    "old,reason",
    [
        (None, "原因" * 150),
        ("原来的备注", "原因" * 150),
        ("备" * 200, "核实为虚假账号领奖"),
        ("原来的备注", "核实为虚假账号领奖"),
    ],
)
def test_revoke_note_truncation_preserves_old_note(session, old, reason):
    row = award(session, user(session))
    row.note = old
    session.commit()
    rewards.revoke(session, row.id, reason, NOW)
    session.commit()
    session.refresh(row)
    assert row.note.startswith("已收回：" + reason[0]) and len(row.note) <= 200
    if old:
        assert row.note.split("；", 1)[1] == old[:194]
    else:
        assert row.note == "已收回：" + reason[:196]


def test_revoke_does_not_restore_monthly_allowance(session):
    owner = user(session)
    row = award(session, owner, days=30)
    rewards.revoke(session, row.id, "核实为虚假账号领奖", NOW)
    session.commit()
    denied = award(session, owner, ref=2)
    assert row.granted_days == 30 and denied.granted_days == 0
    assert (
        sum(r.granted_days for r in session.scalars(rewards.monthly(session, owner.id, NOW))) == 30
    )


def test_revoke_zero_day_reward_note_only(session):
    owner = user(session)
    first = award(session, owner, days=30)
    row = award(session, owner, ref=2)
    assert row.granted_days == 0 and ticket(session, row) is None
    rewards.revoke(session, row.id, "核实为虚假账号领奖", NOW)
    session.commit()
    assert row.note == "已收回：核实为虚假账号领奖；本月奖励已到上限"
    assert ticket(session, first).revoked_at is None
    assert rewards.revoke(session, row.id, "再次核实账号违规", NOW) == "已经收回过"
    assert row.note == "已收回：核实为虚假账号领奖；本月奖励已到上限"


def test_revoke_deleted_user_note_only(context, session):
    client, _ = context
    owner = user(session)
    row = award(session, owner)
    segment = ticket(session, row)
    assert client.delete("/api/v1/me", headers=headers(owner.openid)).status_code == 204
    session.refresh(row)
    session.refresh(segment)
    before = (segment.user_id, segment.starts_at, segment.ends_at, segment.revoked_at)
    assert row.user_id is None
    assert rewards.revoke(session, row.id, "核实为虚假账号领奖", NOW) == "已收回"
    session.commit()
    assert row.note == "已收回：核实为虚假账号领奖" and row.granted_days == 1
    assert (segment.user_id, segment.starts_at, segment.ends_at, segment.revoked_at) == before
    assert rewards.revoke(session, row.id, "再次核实账号违规", NOW) == "已经收回过"


def test_revoke_locks_user_before_reward_pass_and_pending_write(session, db_engine):
    from sqlalchemy import event

    owner = user(session)
    row = award(session, owner)
    pending = task(session, owner)
    pending.result_document_ids = [123]
    statements = []

    def capture(conn, cursor, statement, params, ctx, many):
        statements.append(statement.lower())

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        rewards.revoke(session, row.id, "核实为虚假账号领奖", NOW)
        session.commit()
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
    locks = [sql for sql in statements if "for update" in sql]
    assert "from users" in locks[0]
    assert "from pass_rewards" in locks[1]
    assert "from workbench_passes" in locks[2]
    first_lock = next(i for i, sql in enumerate(statements) if "for update" in sql)
    first_write = next(i for i, sql in enumerate(statements) if sql.startswith("update "))
    assert first_lock < first_write


def test_revoke_refreshes_stale_rows_and_serializes_duplicates(session_factory, session):
    owner = user(session)
    row = award(session, owner, "source", 3)
    following = ticket(session, award(session, owner, ref=2))
    reward_id = row.id
    session.commit()

    def run():
        with session_factory() as s:
            result = rewards.revoke(s, reward_id, "核实为虚假账号领奖", NOW + timedelta(days=1))
            s.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == sorted(["已收回", "已经收回过"])
    assert (
        rewards.revoke(session, reward_id, "再次核实账号违规", NOW + timedelta(days=2))
        == "已经收回过"
    )
    session.commit()
    session.refresh(following)
    assert following.starts_at == NOW + timedelta(days=1)


def test_reward_history_returns_revoked_and_keeps_existing_fields(context, session):
    client, _ = context
    owner = user(session)
    first = award(session, owner, days=30)
    zero = award(session, owner, ref=2)
    third = award(session, owner, ref=3)
    for row in (first, zero):
        rewards.revoke(session, row.id, "核实为虚假账号领奖", NOW)
    session.commit()
    response = client.get("/api/v1/me/rewards", headers=headers(owner.openid))
    assert response.status_code == 200
    body = response.json()
    assert body["monthDays"] == body["monthLimit"] == 30
    assert [item["revoked"] for item in body["items"]] == [False, True, True]
    for item, row in zip(body["items"], (third, zero, first), strict=True):
        assert item == {
            "kind": row.kind,
            "name": rewards.LABELS[row.kind],
            "requestedDays": row.requested_days,
            "days": row.granted_days,
            "grantedAt": row.granted_at.astimezone(advisor_tasks.BEIJING).isoformat(),
            "note": row.note,
            "revoked": rewards.is_revoked(row),
        }
    assert owner.openid not in str(body)


@pytest.mark.parametrize("state", ["future", "half", "expired", "zero", "deleted", "revoked"])
def test_rewards_revoke_cli_preview_and_execute(context, session, monkeypatch, capsys, state):
    client, settings = context
    owner = user(session)
    if state == "zero":
        award(session, owner, days=30)
    row = award(session, owner, "source", 3, ref=2)
    segment = ticket(session, row)
    stamp = NOW
    if state == "future":
        segment.starts_at += timedelta(days=2)
        segment.ends_at += timedelta(days=2)
        session.commit()
    elif state == "half":
        stamp += timedelta(days=1.5)
    elif state == "expired":
        stamp += timedelta(days=4)
    elif state == "deleted":
        assert client.delete("/api/v1/me", headers=headers(owner.openid)).status_code == 204
        session.refresh(row)
        session.refresh(segment)
    elif state == "revoked":
        rewards.revoke(session, row.id, "核实为虚假账号领奖", stamp)
        session.commit()
    following = None
    if state not in ("zero", "deleted"):
        following = ticket(session, award(session, owner, ref=3))

    class Clock:
        @staticmethod
        def now(tz):
            return stamp

    monkeypatch.setattr(cli, "datetime", Clock)
    settings.rewards_enabled = settings.advisor_enabled = False
    snapshots = {
        p.id: (p.starts_at, p.ends_at, p.revoked_at) for p in session.scalars(select(WorkbenchPass))
    }
    note = row.note
    reward_id = row.id
    unused = payments.unused_pass_time(segment, stamp) if row.user_id is not None else timedelta(0)
    args = ["rewards-revoke", "--reward-id", str(reward_id), "--reason", "核实为虚假账号领奖"]
    session.rollback()
    assert cli.main([*args, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "未写入" in output and "source（提的栏目已接入）" in output
    assert "发放天数：" in output and NOW.isoformat() in output
    assert f"将要收回时间：{unused}（{unused.total_seconds():g} 秒）" in output
    assert "用户内部编号：" in output and "openid" not in output and "reward-user" not in output
    if segment is not None:
        assert segment.starts_at.isoformat() in output and segment.ends_at.isoformat() in output
    else:
        assert "使用期：无" in output
    session.expire_all()
    assert session.get(PassReward, reward_id).note == note
    assert {
        p.id: (p.starts_at, p.ends_at, p.revoked_at) for p in session.scalars(select(WorkbenchPass))
    } == snapshots
    session.rollback()
    assert cli.main(args) == 0
    assert ("已经收回过" if state == "revoked" else "已收回") in capsys.readouterr().out
    session.expire_all()
    assert session.get(PassReward, reward_id).note.startswith("已收回：")
    if following is not None:
        assert following.starts_at == snapshots[following.id][0] - unused
        assert following.ends_at == snapshots[following.id][1] - unused
    session.rollback()
    assert cli.main(args) == 0
    assert "已经收回过" in capsys.readouterr().out


@pytest.mark.parametrize("reason", [None, "太短", " " * 6, "字" * 101])
def test_rewards_revoke_cli_rejects_invalid_reason(monkeypatch, capsys, reason):
    def no_database():
        pytest.fail("无效原因不应连接数据库")

    monkeypatch.setattr(cli, "session_factory", no_database)
    args = ["rewards-revoke", "--reward-id", "1"]
    if reason is not None:
        args += ["--reason", reason]
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    assert "--reason" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_rewards_revoke_cli_missing_reward(context, session, capsys, dry_run):
    args = ["rewards-revoke", "--reward-id", "999", "--reason", "核实为虚假账号领奖"]
    assert cli.main(args + (["--dry-run"] if dry_run else [])) == 1
    assert "没有找到这笔奖励" in capsys.readouterr().out
    assert session.scalar(select(func.count(PassReward.id))) == 0


def paid(session, owner, start=NOW):
    order = PayOrder(
        user_id=owner.id,
        out_trade_no=f"order-{owner.id}",
        product_id="workbench_30d",
        price=100,
        days=30,
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


@pytest.mark.parametrize("kind,days", [("correction", 1), ("source", 3), ("deadline_clue", 1)])
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


@pytest.mark.parametrize("kind", ["first_batch", "referral"])
def test_retired_kinds_are_not_granted(session, kind):
    """首次批量、同行引荐不再发放（D-061、D-070）；名称留着，旧流水照常显示。"""
    owner = user(session)
    with pytest.raises(ValueError, match="不再发放"):
        rewards.grant(session, owner.id, kind, 3, f"user_{kind}", owner.id, NOW)
    session.rollback()
    assert session.scalar(select(func.count(PassReward.id))) == 0
    assert kind in rewards.LABELS


def test_month_limit_partial_zero_and_beijing_boundary(session):
    owner = user(session)
    before = NEXT_MONTH - timedelta(microseconds=1)
    for i in range(29):
        award(session, owner, ref=i, stamp=before)
    partial = award(session, owner, "source", 3, ref=101, stamp=before)
    zero = award(session, owner, "source", 3, ref=102, stamp=before)
    assert partial.granted_days == 1 and partial.requested_days == 3
    assert partial.note == zero.note == "本月奖励已到上限"
    assert zero.granted_days == 0 and ticket(session, zero) is None
    assert award(session, owner, "source", 3, ref=103, stamp=NEXT_MONTH).granted_days == 3
    assert award(session, owner, "source", 3, ref=102, stamp=NEXT_MONTH).id == zero.id
    assert (
        sum(r.granted_days for r in session.scalars(rewards.monthly(session, owner.id, before)))
        == 30
    )
    assert (
        sum(r.granted_days for r in session.scalars(rewards.monthly(session, owner.id, NEXT_MONTH)))
        == 3
    )


@pytest.mark.parametrize("kind,days,limit", [("source", 3, 2), ("deadline_clue", 1, 10)])
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
    segment = ticket(session, award(session, owner, "source", 3))
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


def test_referral_is_gone(context, session):
    """同行引荐已删（D-061、D-070）：引荐接口没有了，同意协议不再收引荐码，开关也删了。"""
    client, _ = context
    owner = user(session, "owner")
    assert client.get("/api/v1/me/invite", headers=headers(owner.openid)).status_code == 404
    with_code = {"version": AGREEMENT, "invite_code": "fixed-code"}
    response = client.post("/api/v1/me/agreement", headers=headers("new"), json=with_code)
    assert response.status_code == 422
    assert session.scalar(select(User).where(User.openid == "new")) is None
    response = client.post(
        "/api/v1/me/agreement", headers=headers("new"), json={"version": AGREEMENT}
    )
    assert response.status_code == 200
    assert session.scalar(select(User).where(User.openid == "new")) is not None
    assert "referral_enabled" not in Settings.model_fields
    assert not hasattr(rewards, "invite_code") and not hasattr(rewards, "referral_enabled")


@pytest.mark.parametrize("kind", ["compare", "track"])
def test_first_success_grants_nothing(context, session, kind):
    """首次批量奖励已删：奖励开关开着，第一次完成批量任务也不发天数，补发也不补。"""
    _, settings = context
    newcomer = user(session, "newcomer")
    done = task(session, newcomer, kind=kind)
    assert (
        advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)["done"]
        == 1
    )
    session.refresh(done)
    assert done.status == "done" and done.error is None and done.result_file_id
    assert rewards.reconcile(session, settings=settings, stamp=NOW) == {"granted": 0, "failed": 0}
    assert cli.main(["rewards-reconcile"]) == 0
    assert session.scalar(select(func.count(PassReward.id))) == 0
    assert not hasattr(rewards, "first_completion")


@pytest.mark.parametrize("switch", ["rewards_enabled", "advisor_enabled"])
def test_switch_disabled_api_registration_admin_auto_and_cli(context, session, switch):
    client, settings = context
    inviter = user(session, "inviter")
    setattr(settings, switch, False)
    # 纠错送天数有自己的开关（D-070 ⑦，开着时的情况在 test_sale.py）；这里测全部关掉
    settings.correction_reward_enabled = False
    for path in ("/api/v1/me/rewards", "/api/v1/me/invite"):
        assert client.get(path).status_code == 404
        assert client.get(path, headers=headers(inviter.openid)).status_code == 404
    assert (
        client.post(
            "/api/v1/me/agreement", headers=headers("new"), json={"version": AGREEMENT}
        ).status_code
        == 200
    )
    newcomer = session.scalar(select(User).where(User.openid == "new"))
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
    reward = award(session, owner, "source", 3)
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
    prior_reward = award(session, owner, "source", 3, ref=2)
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


@pytest.mark.parametrize("rewards_on", [False, True])
@pytest.mark.parametrize("who", ["inviter", "newcomer"])
def test_delete_account_unlinks_legacy_rewards_preserves_others(context, session, who, rewards_on):
    """以前留下的首次批量、引荐流水：注销时本人的流水去关联、没用完的天数作废，别人流水里指向本人的
    ref_id 清掉（奖励开关关着也一样），另一方已经拿到的天数不收回。"""
    client, settings = context
    inviter = user(session, "inviter")
    newcomer = user(session, "newcomer")
    legacy_referral(session, inviter, newcomer)
    deleted, survivor = (inviter, newcomer) if who == "inviter" else (newcomer, inviter)
    survivor_id, deleted_id = survivor.id, deleted.id
    survivor_pass_id = session.scalar(
        select(WorkbenchPass.id).where(WorkbenchPass.user_id == survivor_id)
    )
    settings.rewards_enabled = rewards_on
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
    assert "采纳并送 1 天" in page and "已接入并送 3 天" in page
    assert "item.correctionRewardEnabled && item.kind === '纠错'" in page
    assert "item.rewardsEnabled && item.kind === '申请覆盖'" in page


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
    assert not any(word in str(body) + str(rewards.LABELS) for word in forbidden)


def test_refund_api_after_reward_usage_advances_reward_segment(context, session):
    client, settings = context
    owner = user(session)
    first = ticket(session, award(session, owner, "source", 3, ref=2))
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


def test_backup_restore_legacy_rewards_and_passes(session, db_engine):
    from .test_backup import clear_restore_target

    inviter = user(session, "inviter")
    newcomer = user(session, "new")
    reward = legacy_referral(session, inviter, newcomer)["referral"]
    reward_id, inviter_id, newcomer_id = reward.id, inviter.id, newcomer.id
    output = BytesIO()
    counts = dump(db_engine, output, now=NOW)
    clear_restore_target(session, db_engine)
    assert restore(db_engine, BytesIO(output.getvalue())) == counts
    session.expire_all()
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
    config = alembic_config(db_engine.url.render_as_string(hide_password=False))
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


LEGACY_USER_COLUMNS = ("invite_code", "invited_by_user_id", "invited_at", "batch_completed_at")


def _before_0035(config):
    """0035 接在哪个版本后面（合并时随 main 改 down_revision，测试不用跟着改）。"""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(config).get_revision("0035").down_revision


def _show_create(conn, table):
    """建表语句，去掉随数据变的自增起点。"""
    from sqlalchemy import text

    return re.sub(
        r" AUTO_INCREMENT=\d+", "", conn.execute(text(f"SHOW CREATE TABLE `{table}`")).one()[1]
    )


def test_migration_0035_drops_legacy_structures_and_downgrade_rebuilds_them(
    context, session, db_engine
):
    """0035 删凭据表、users 的四列和排队行，别的数据不动；回退重建的结构和 0027、0031 建的一模一样，
    数据不恢复，再往下回退也走得通。新代码在迁移前的表上照样能注册、读取、注销（先发 api 靠这一点）。"""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect, text

    from youju.migrations import alembic_config

    client, _ = context
    session.close()
    config = alembic_config(db_engine.url.render_as_string(hide_password=False))
    before = _before_0035(config)

    def register(openid):
        response = client.post(
            "/api/v1/me/agreement", headers=headers(openid), json={"version": AGREEMENT}
        )
        assert response.status_code == 200
        with db_engine.connect() as conn:
            return conn.scalar(text("SELECT id FROM users WHERE openid = :o"), {"o": openid})

    try:
        # 先降到 0027 之前（0027、0031 的回退要按名字删 0035 重建的约束和索引），再升回 0035 的上一版，
        # 拿到 0027、0031 原样建出来的结构
        command.downgrade(config, "0026")
        command.upgrade(config, before)
        with db_engine.connect() as conn:
            original = {name: _show_create(conn, name) for name in ("users", "reward_claims")}
        command.upgrade(config, "head")
        assert "reward_claims" not in inspect(db_engine).get_table_names()
        command.downgrade(config, before)
        with db_engine.connect() as conn:
            assert {
                name: _show_create(conn, name) for name in ("users", "reward_claims")
            } == original

        # 在迁移前的表上跑新代码：注册不写这四列；注销被引用的用户时，外键把引用置空
        inviter_id, newcomer_id = register("old-inviter"), register("old-newcomer")
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET invite_code = 'legacy-code', invited_by_user_id = :inviter,"
                    " invited_at = :at, batch_completed_at = :at WHERE id = :id"
                ),
                {"inviter": inviter_id, "at": NOW.replace(tzinfo=None), "id": newcomer_id},
            )
            conn.execute(
                text(
                    "INSERT INTO reward_claims (pseudonym, kind, claimed_at)"
                    " VALUES (:a, 'referred', :at), (:b, 'boost_trial', :at)"
                ),
                {"a": "a" * 64, "b": "b" * 64, "at": NOW.replace(tzinfo=None)},
            )
            conn.execute(
                text(
                    "INSERT INTO app_keys (name, value) VALUES ('reward_claim_queue', '1'),"
                    " ('deadline_clue_queue', '1')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO pass_rewards (user_id, kind, requested_days, granted_days, ref_type,"
                    " ref_id, granted_at) VALUES (:id, 'first_batch', 3, 3, 'user_first_batch', :id, :at)"
                ),
                {"id": newcomer_id, "at": NOW.replace(tzinfo=None)},
            )
        assert client.delete("/api/v1/me", headers=headers("old-inviter")).status_code == 204
        assert client.get("/api/v1/me", headers=headers("old-newcomer")).status_code == 200
        with db_engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT invite_code, invited_by_user_id, batch_completed_at IS NOT NULL"
                    " FROM users WHERE id = :id"
                ),
                {"id": newcomer_id},
            ).one() == ("legacy-code", None, 1)

        command.upgrade(config, "head")
        assert "reward_claims" not in inspect(db_engine).get_table_names()
        assert not set(LEGACY_USER_COLUMNS) & {
            c["name"] for c in inspect(db_engine).get_columns("users")
        }
        assert not inspect(db_engine).get_foreign_keys("users")
        assert {i["name"] for i in inspect(db_engine).get_indexes("users")}.isdisjoint(
            {"ix_users_invited_by_user_id", "uq_users_invite_code"}
        )
        with db_engine.connect() as conn:
            names = {r[0] for r in conn.execute(text("SELECT name FROM app_keys"))}
            assert "reward_claim_queue" not in names and "deadline_clue_queue" in names
            assert conn.execute(text("SELECT id, openid FROM users")).all() == [
                (newcomer_id, "old-newcomer")
            ]
            assert conn.execute(text("SELECT kind, ref_id FROM pass_rewards")).all() == [
                ("first_batch", newcomer_id)
            ]

        # 回退只重建空表、空列
        command.downgrade(config, before)
        with db_engine.connect() as conn:
            assert conn.scalar(text("SELECT COUNT(*) FROM reward_claims")) == 0
            counts = ", ".join(f"COUNT({name})" for name in LEGACY_USER_COLUMNS)
            assert conn.execute(text(f"SELECT {counts} FROM users")).one() == (0, 0, 0, 0)
    finally:
        command.upgrade(config, "head")
    with db_engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_migration_0035_gives_up_on_a_held_metadata_lock_and_resumes(session, db_engine):
    """有事务读过 users 还没结束（如批量任务正在处理）：迁移最多等 5 秒就失败，不让后面读 users 的
    接口一直排队；已经做完的步骤不再做，事务结束后重跑接着做完。升级、回退都一样。
    改完 users、只差写版本号时失败（版本还是上一版），重跑也能做完。"""
    import threading
    import time

    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect, text

    from youju.migrations import alembic_config

    session.close()
    config = alembic_config(db_engine.url.render_as_string(hide_password=False))
    before = _before_0035(config)

    def blocked(target, step):
        with db_engine.connect() as holder:
            holder_id = holder.scalar(text("SELECT CONNECTION_ID()"))
            holder.execute(text("SELECT id FROM users LIMIT 1"))

            def release():
                with db_engine.connect() as killer:
                    killer.execute(text(f"KILL {holder_id}"))

            # 迁移要是没设短超时会一直等：30 秒后断开占锁的连接，让测试失败而不是卡住
            guard = threading.Timer(30, release)
            guard.start()
            started = time.monotonic()
            try:
                with pytest.raises(DBAPIError, match="1205"):
                    step(config, target)
            finally:
                guard.cancel()
            assert time.monotonic() - started < 20
            holder.rollback()

    def revision():
        with db_engine.connect() as conn:
            return conn.scalar(text("SELECT version_num FROM alembic_version"))

    try:
        command.downgrade(config, before)
        with db_engine.connect() as conn:
            rebuilt = {name: _show_create(conn, name) for name in ("users", "reward_claims")}

        blocked("head", command.upgrade)
        assert revision() == before
        assert "reward_claims" not in inspect(db_engine).get_table_names()
        assert set(LEGACY_USER_COLUMNS) <= {
            c["name"] for c in inspect(db_engine).get_columns("users")
        }
        command.upgrade(config, "head")
        assert revision() == "0035"

        blocked(before, command.downgrade)
        assert revision() == "0035"
        assert "reward_claims" in inspect(db_engine).get_table_names()
        command.downgrade(config, before)
        with db_engine.connect() as conn:
            assert {
                name: _show_create(conn, name) for name in ("users", "reward_claims")
            } == rebuilt

        # 迁移的活都做完了、写版本号时失败：库里是新结构，版本还是上一版
        with db_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO app_keys (name, value) VALUES ('reward_claim_queue', '1')")
            )
            conn.execute(text("DROP TABLE reward_claims"))
            conn.execute(
                text(
                    "ALTER TABLE users DROP FOREIGN KEY fk_users_invited_by,"
                    " DROP INDEX ix_users_invited_by_user_id, DROP INDEX uq_users_invite_code, "
                    + ", ".join(f"DROP COLUMN {name}" for name in LEGACY_USER_COLUMNS)
                )
            )
        assert revision() == before
        command.upgrade(config, "head")
        assert revision() == "0035"
        with db_engine.connect() as conn:
            assert not conn.scalar(
                text("SELECT COUNT(*) FROM app_keys WHERE name = 'reward_claim_queue'")
            )
    finally:
        command.upgrade(config, "head")
    with db_engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_reregister_after_deletion_is_a_new_account(context, session):
    """注销即删：有过奖励的账号注销后，同一个微信重新同意建的是新账号，以前的奖励不跟过来。"""
    client, _ = context

    def register():
        assert (
            client.post(
                "/api/v1/me/agreement", headers=headers("repeat"), json={"version": AGREEMENT}
            ).status_code
            == 200
        )
        return session.scalar(select(User).where(User.openid == "repeat"))

    first = register()
    first_id = first.id
    award(session, first, "source", 3)
    assert client.delete("/api/v1/me", headers=headers("repeat")).status_code == 204
    session.expire_all()
    assert session.get(User, first_id) is None
    again = register()
    assert again.id != first_id
    assert (
        session.scalar(select(func.count(PassReward.id)).where(PassReward.user_id == again.id)) == 0
    )


@pytest.mark.parametrize("switch", ["rewards_enabled", "advisor_enabled"])
def test_delete_empty_account_without_new_table_writes(context, session, db_engine, switch):
    from sqlalchemy import event

    client, settings = context
    owner = user(session)
    setattr(settings, switch, False)
    statements = []

    def capture(conn, cursor, statement, params, ctx, many):
        statements.append(statement.lower())

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        assert client.delete("/api/v1/me", headers=headers(owner.openid)).status_code == 204
        assert (
            client.delete(
                "/api/v1/me/deadline-clues/999", headers=headers(owner.openid)
            ).status_code
            == 404
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
    for sql in statements:
        if any(
            table in sql
            for table in (
                "deadline_clues",
                "pass_rewards",
                "workbench_passes",
                "pay_orders",
                "pay_events",
            )
        ):
            assert sql.lstrip().startswith("select") and "for update" not in sql


@pytest.mark.parametrize("enabled", [False, True])
def test_completion_locks_user_before_flushing_task(
    context, session, db_engine, monkeypatch, enabled
):
    from sqlalchemy import event

    _, settings = context
    settings.rewards_enabled = enabled
    owner = user(session)
    row = task(session, owner)

    def engine(s, task, output):
        task.result_document_ids = [123]
        output.write_bytes(b"test")
        return 1

    monkeypatch.setattr(advisor_tasks, "_run_engine", engine)
    statements = []

    def capture(conn, cursor, statement, params, ctx, many):
        statements.append(statement.lower())

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        assert (
            advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)[
                "done"
            ]
            == 1
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
    lock = next(
        i for i, sql in enumerate(statements) if "from users" in sql and "for update" in sql
    )
    finish = next(
        i
        for i, sql in enumerate(statements)
        if sql.startswith("update advisor_tasks") and "result_document_ids" in sql
    )
    assert lock < finish
    session.refresh(row)
    assert row.status == "done" and row.result_document_ids == [123]


@pytest.mark.parametrize("advisor_enabled", [False, True])
def test_restricted_api_without_new_grants(
    context, restricted_api_factory, monkeypatch, advisor_enabled
):
    from youju import access_log

    client, settings = context
    settings.rewards_enabled = settings.pay_enabled = False
    settings.advisor_enabled = advisor_enabled
    with restricted_api_factory(new_grants=False) as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        identity = headers("limited-off")
        assert (
            client.post(
                "/api/v1/me/agreement",
                headers=identity,
                json={"version": settings.agreement_version},
            ).status_code
            == 200
        )
        if advisor_enabled:
            assert (
                client.post(
                    "/api/v1/me/agreement",
                    headers=identity,
                    json={"scope": "advisor", "version": advisor_tasks.ADVISOR_AGREEMENT_VERSION},
                ).status_code
                == 200
            )
        submitted = client.post(
            "/api/v1/advisor/tasks",
            headers={**identity, "Idempotency-Key": "limited-task"},
            json={"kind": "compare", "rows": ["甲公司"]},
        )
        assert submitted.status_code == (202 if advisor_enabled else 404), submitted.text
        task_id = submitted.json()["id"] if advisor_enabled else 999
        assert client.get(f"/api/v1/advisor/tasks/{task_id}", headers=identity).status_code == (
            200 if advisor_enabled else 404
        )
        assert client.get("/api/v1/advisor/tasks", headers=identity).status_code == (
            200 if advisor_enabled else 404
        )
        for path in ("/api/v1/me", "/api/v1/me/data"):
            assert client.get(path, headers=identity).status_code == 200
        assert client.delete("/api/v1/me/deadline-clues/999", headers=identity).status_code == 404
        assert client.delete("/api/v1/me", headers=identity).status_code == 204


def test_reconcile_is_scheduled_hourly_and_recorded():
    """发奖失败只记日志，补发靠定时任务：crontab 里要有且只有一行 rewards-reconcile，job.sh 认得这个任务名。"""
    from pathlib import Path

    from youju import jobs

    crontab = (Path(__file__).resolve().parents[1] / "deploy" / "crontab").read_text()
    lines = [line for line in crontab.splitlines() if "job.sh rewards-reconcile" in line]
    assert lines == ["20 * * * * /app/backend/deploy/job.sh rewards-reconcile"]
    assert "rewards-reconcile" in jobs.JOB_NAMES and "rewards-reconcile" not in jobs.OVERVIEW_JOBS
