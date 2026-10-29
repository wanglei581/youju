"""卡里的 AI 次数（D-109 甲，youju/ai_credits.py，迁移 0046）：给次数、扣的先后、占/扣/退、确认提示、
钱包的状态、并发争最后一次、付费全站名额、告警、对账统计、收回和保存期。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from youju import ai_credits
from youju.ai_credits import CreditConfirm, CreditError, NoCredits
from youju.models import (
    AICreditGrant,
    AICreditUse,
    BoostTrial,
    PassReward,
    PayOrder,
    User,
    WorkbenchPass,
)

NOW = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)  # 北京时间 11/10 10:00
_seq = iter(range(1, 10_000))


@pytest.fixture(autouse=True)
def _fresh_cache():
    # 在用卡数按分钟缓存、告警按天记在进程里：每个测试换了数据，先清掉
    ai_credits.reset_cache()
    yield
    ai_credits.reset_cache()


def user(session, openid="credit-user") -> User:
    row = User(openid=openid, agreement_version="2026-11", agreement_at=NOW)
    session.add(row)
    session.flush()
    return row


def card(
    session, owner, *, days=30, paid=NOW - timedelta(hours=1), starts=None, platform="android"
):
    """一张买来的卡：完成的订单和它那一段使用期，再按天数 × 10 给次数。"""
    n = next(_seq)
    order = PayOrder(
        user_id=owner.id,
        out_trade_no=f"credit-{n}",
        product_id=f"workbench_{days}d",
        price=2900 if days == 30 else 7900,
        days=days,
        env=0,
        terms_version="pay-test",
        status="completed",
        wx_order_id=f"wx-{n}",
        paid_fee=2900,
        paid_time=paid,
        platform=platform,
        created_at=paid,
        updated_at=paid,
    )
    session.add(order)
    session.flush()
    begin = starts or paid
    ticket = WorkbenchPass(
        user_id=owner.id, order_id=order.id, starts_at=begin, ends_at=begin + timedelta(days=days)
    )
    session.add(ticket)
    session.flush()
    grant = ai_credits.grant_pass(
        session, ticket, ai_credits.PURCHASE, ai_credits.credits_for_days(days), paid
    )
    return order, ticket, grant


def trial(session, owner, *, started=NOW - timedelta(hours=1), hours=72):
    row = BoostTrial(user_id=owner.id, started_at=started, ends_at=started + timedelta(hours=hours))
    session.add(row)
    session.flush()
    return row, ai_credits.grant_trial(session, row, started)


def locked(session, owner, stamp=NOW):
    session.scalar(select(User).where(User.id == owner.id).with_for_update())
    return ai_credits.lock_wallet(session, owner.id, stamp)


def take(session, owner, key, *, stamp=NOW, reason="free_used", confirmed=("first_purchase",)):
    return ai_credits.reserve(
        session,
        locked(session, owner, stamp),
        key,
        "interpret",
        stamp,
        reason=reason,
        confirmed=frozenset(confirmed),
    )


# ---- 给次数、口径 -----------------------------------------------------------------------


def test_days_times_ten_is_the_whole_card_total_without_daily_cap(session):
    owner = user(session)
    _, _, grant = card(session, owner, days=30)
    _, _, grant90 = card(session, user(session, "other"), days=90)
    assert (grant.amount, grant90.amount) == (300, 900)
    for n in range(12):  # 同一天连着用十几次也行：没有每天上限
        take(session, owner, f"task:{n}")
    session.commit()
    assert ai_credits.balance(session, owner.id, NOW)["left"] == 288
    assert (
        ai_credits.grant_pass(session, card(session, owner)[1], "purchase", 300, NOW).amount == 300
    )


def test_gifts_first_then_bought_and_earliest_expiry_first(session):
    owner = user(session)
    order, ticket, bought = card(session, owner)
    # 续买多送的那 3 天：一笔赠送流水和它那一段使用期，接在买来的那段后面
    reward = PassReward(
        user_id=owner.id,
        kind="renewal",
        requested_days=3,
        granted_days=3,
        ref_type="order_renewal",
        ref_id=order.id,
        granted_at=NOW,
    )
    session.add(reward)
    session.flush()
    gift_segment = WorkbenchPass(
        user_id=owner.id,
        reward_id=reward.id,
        starts_at=ticket.ends_at,
        ends_at=ticket.ends_at + timedelta(days=3),
    )
    session.add(gift_segment)
    session.flush()
    renewal = ai_credits.grant_pass(session, gift_segment, "renewal", ai_credits.RENEW_CREDITS, NOW)
    _, trial_gift = trial(session, owner)
    wallet = locked(session, owner)
    assert [slot.grant.id for slot in wallet.slots] == [trial_gift.id, renewal.id, bought.id]
    sources = [take(session, owner, f"task:{n}")["source"] for n in range(31)]
    assert sources[:30] == ["gift"] * 30  # 体验送的 30 次先用完（最早到期）
    view = ai_credits.balance(session, owner.id, NOW)
    assert (view["gift"], view["bought"], view["left"]) == (29, 300, 329)


def test_future_segment_is_usable_and_status_tells_the_cases_apart(session):
    owner = user(session)
    assert ai_credits.balance(session, owner.id, NOW)["status"] == "NO_CARD"
    _, ticket, grant = card(session, owner, paid=NOW - timedelta(days=40))
    view = ai_credits.balance(session, owner.id, NOW)
    assert (view["status"], view["hasCard"], view["left"]) == ("EXPIRED", False, 0)
    # 提前续买、还没开始的那段也能用
    _, future, _ = card(
        session, owner, starts=NOW + timedelta(days=5), paid=NOW - timedelta(days=1)
    )
    view = ai_credits.balance(session, owner.id, NOW)
    assert (view["status"], view["hasCard"], view["left"]) == ("OK", True, 300)
    assert view["expiresAt"] == future.ends_at
    session.execute(text("UPDATE ai_credit_grants SET used = amount"))
    session.expire_all()
    view = ai_credits.balance(session, owner.id, NOW)
    assert (view["status"], view["hasCard"], view["left"]) == ("USED_UP", True, 0)
    # 退了的卡算"没卡"，不是"过期"
    other = user(session, "refunded")
    _, refunded, _ = card(session, other)
    refunded.revoked_at = NOW
    session.flush()
    assert ai_credits.balance(session, other.id, NOW)["status"] == "NO_CARD"


# ---- 占、扣、退 -------------------------------------------------------------------------


def test_reserve_charge_release_are_idempotent_and_move_the_counters(session):
    owner = user(session)
    _, _, grant = card(session, owner)
    first = take(session, owner, "task:1")
    assert first == {"useKey": "task:1", "grantId": grant.id, "source": "bought", "left": 299}
    assert take(session, owner, "task:1")["useKey"] == "task:1"  # 同一个编号不重复占
    session.refresh(grant)
    assert (grant.held, grant.used) == (1, 0)
    assert ai_credits.charge(session, "task:1", NOW) == "charged"
    assert ai_credits.charge(session, "task:1", NOW) == "charged"
    assert ai_credits.release(session, "task:1", NOW, "failed") == "charged"  # 扣了的不退
    session.refresh(grant)
    assert (grant.held, grant.used) == (0, 1)
    take(session, owner, "task:2")
    assert ai_credits.release(session, "task:2", NOW, "refusal") == "released"
    assert ai_credits.release(session, "task:2", NOW, "refusal") == "released"
    session.refresh(grant)
    assert (grant.held, grant.used) == (0, 1)
    with pytest.raises(CreditError) as err:
        take(session, owner, "task:2")
    assert err.value.code == "RELEASED"
    assert ai_credits.charge(session, "task:404", NOW) == "none"
    use = session.scalar(select(AICreditUse).where(AICreditUse.use_key == "task:2"))
    assert (use.state, use.release_reason, use.reason) == ("released", "refusal", "free_used")


def test_no_credits_codes_match_the_wallet_status(session):
    owner = user(session)
    with pytest.raises(NoCredits) as err:
        take(session, owner, "task:1")
    assert err.value.code == "NO_CARD"
    _, ticket, grant = card(session, owner, paid=NOW - timedelta(days=31))
    with pytest.raises(NoCredits) as err:
        take(session, owner, "task:2")
    assert err.value.code == "EXPIRED"
    _, _, fresh = card(session, owner)
    fresh.used = fresh.amount
    session.flush()
    with pytest.raises(NoCredits) as err:
        take(session, owner, "task:3")
    assert err.value.code == "USED_UP"


def test_release_goes_back_to_the_same_grant_even_after_it_expired(session):
    owner = user(session)
    _, _, old = card(session, owner, paid=NOW - timedelta(days=30) + timedelta(minutes=5))
    _, _, later = card(session, owner, starts=NOW + timedelta(minutes=5))
    used = take(session, owner, "task:1", stamp=NOW)
    assert used["grantId"] == old.id  # 最早到期的先扣
    after = NOW + timedelta(minutes=10)  # 那一段过期了
    assert ai_credits.release(session, "task:1", after, "failed") == "released"
    session.refresh(later)
    assert later.held == later.used == 0  # 不挪到别的卡
    assert ai_credits.balance(session, owner.id, after)["left"] == 300


def test_charge_still_counts_after_the_grant_was_revoked(session):
    owner = user(session)
    _, ticket, grant = card(session, owner)
    take(session, owner, "task:1")
    assert ai_credits.revoke_pass_grants(session, [ticket.id], NOW) == 1
    assert ai_credits.charge(session, "task:1", NOW) == "charged"
    session.refresh(grant)
    assert (grant.used, grant.held, grant.revoked_at is not None) == (1, 0, True)
    assert ai_credits.balance(session, owner.id, NOW)["status"] == "NO_CARD"


# ---- 确认提示 ---------------------------------------------------------------------------


def test_site_full_and_first_purchase_are_asked_together(session):
    owner = user(session)
    card(session, owner)
    with pytest.raises(CreditConfirm) as err:
        take(session, owner, "task:1", reason="site_full", confirmed=())
    assert err.value.codes == ["site_full", "first_purchase"]
    assert session.scalar(select(func.count(AICreditUse.id))) == 0
    with pytest.raises(CreditConfirm) as err:
        take(session, owner, "task:1", reason="site_full", confirmed=("site_full",))
    assert err.value.codes == ["site_full", "first_purchase"]
    take(session, owner, "task:1", reason="site_full", confirmed=("site_full", "first_purchase"))
    # 这一段买来的次数已经扣过（或占着）：之后不再提示"不能无理由退款"
    assert take(session, owner, "task:2", confirmed=())["source"] == "bought"
    assert ai_credits.balance(session, owner.id, NOW)["nextConfirm"] is None


def test_first_purchase_prompt_only_while_the_order_is_refundable(session):
    owner = user(session)
    _, _, grant = card(session, owner, paid=NOW - timedelta(hours=25))  # 过了 24 小时
    assert ai_credits.balance(session, owner.id, NOW)["nextConfirm"] is None
    take(session, owner, "task:1", confirmed=())
    apple = user(session, "apple")
    card(session, apple, platform="ios")
    assert ai_credits.balance(session, apple.id, NOW)["nextConfirm"] == "first_purchase"
    with pytest.raises(CreditConfirm):
        take(session, apple, "task:2", confirmed=())
    gift_only = user(session, "gift-only")
    trial(session, gift_only)
    assert take(session, gift_only, "task:3", confirmed=())["source"] == "gift"  # 赠送的不提示


# ---- 并发（照 #404：先各建旧快照，再依次争） ----------------------------------------------


def test_two_connections_race_for_the_last_credit(session, repeatable_read_factory):
    owner = user(session)
    _, _, grant = card(session, owner, paid=NOW - timedelta(days=2))
    grant.used = grant.amount - 1
    session.commit()
    with repeatable_read_factory() as first, repeatable_read_factory() as second:
        for s in (first, second):  # 各自先普通读一次，定下快照
            assert s.scalar(select(AICreditGrant.used).where(AICreditGrant.id == grant.id)) == 299
        assert take(first, owner, "task:a")["left"] == 0
        first.commit()
        with pytest.raises(NoCredits) as err:
            take(second, owner, "task:b")
        assert err.value.code == "USED_UP"
        second.rollback()
    session.expire_all()
    assert session.scalar(select(func.count(AICreditUse.id))) == 1


def test_charge_and_release_race_settles_only_once(session, repeatable_read_factory):
    owner = user(session)
    _, _, grant = card(session, owner, paid=NOW - timedelta(days=2))
    take(session, owner, "task:1")
    session.commit()
    with repeatable_read_factory() as first, repeatable_read_factory() as second:
        for s in (first, second):
            assert s.scalar(select(AICreditUse.state).where(AICreditUse.use_key == "task:1")) == (
                "reserved"
            )
        assert ai_credits.charge(first, "task:1", NOW) == "charged"
        first.commit()
        assert ai_credits.release(second, "task:1", NOW, "failed") == "charged"
        second.commit()
    session.expire_all()
    session.refresh(grant)
    assert (grant.used, grant.held) == (1, 0)


def test_same_use_key_from_an_old_snapshot_returns_the_existing_one(
    session, repeatable_read_factory
):
    owner = user(session)
    _, ticket, grant = card(session, owner, paid=NOW - timedelta(days=2))
    session.commit()
    with repeatable_read_factory() as first, repeatable_read_factory() as second:
        for s in (first, second):  # 各自先定下快照
            assert s.scalar(select(func.count(AICreditUse.id))) == 0
        take(first, owner, "task:dup")
        first.commit()
        again = take(second, owner, "task:dup")  # 普通读看不到，插入撞了唯一键：取回已有的那行
        assert again["useKey"] == "task:dup" and again["grantId"] == grant.id
        second.commit()
    session.expire_all()
    session.refresh(grant)
    assert grant.held == 1
    assert ai_credits.paid_pool_used(session, date(2026, 11, 10)) == 1


def test_granting_twice_from_old_snapshots_gives_one_grant(session, repeatable_read_factory):
    owner = user(session)
    order, ticket, _ = card(session, owner)
    row, _ = trial(session, owner)
    session.commit()
    with repeatable_read_factory() as first, repeatable_read_factory() as second:
        for s in (first, second):
            assert s.scalar(select(func.count(AICreditGrant.id))) == 2
        a = ai_credits.grant_pass(first, first.get(WorkbenchPass, ticket.id), "backfill", 50, NOW)
        first.commit()
        b = ai_credits.grant_pass(second, second.get(WorkbenchPass, ticket.id), "backfill", 50, NOW)
        assert a.id == b.id
        second.commit()
    session.expire_all()
    assert session.scalar(select(func.count(AICreditGrant.id))) == 3
    assert ai_credits.grant_trial(session, row, NOW).kind == "trial"


def test_revoke_user_grants_on_account_deletion(session):
    owner = user(session)
    card(session, owner)
    trial(session, owner)
    take(session, owner, "task:1")
    assert ai_credits.revoke_user_grants(session, owner.id, NOW) == 2
    assert ai_credits.balance(session, owner.id, NOW)["status"] == "NO_CARD"
    assert ai_credits.charge(session, "task:1", NOW) == "charged"  # 占着的照常走完


# ---- 付费全站名额、告警、对账 -------------------------------------------------------------


def test_paid_pool_counts_every_reservation_and_scales_with_live_cards(session, db_engine):
    owner = user(session)
    card(session, owner)
    take(session, owner, "task:1")
    take(session, owner, "task:2")
    ai_credits.release(session, "task:2", NOW, "failed")  # 退了也算：调过模型
    take(session, owner, "task:3", stamp=NOW + timedelta(days=1))  # 第二天的不算今天
    for n in range(40):
        card(session, user(session, f"buyer-{n}"))
    expired = user(session, "expired")
    card(session, expired, paid=NOW - timedelta(days=40))
    trial(session, user(session, "trial"))
    # 按张数：一个人两张卡算两张，体验加买卡也算两张（Codex 10/29）
    both = user(session, "both")
    card(session, both)
    card(session, both, starts=NOW + timedelta(days=30))
    trial(session, both)
    session.commit()
    with db_engine.connect() as conn:  # 在"全站名额那条连接"上数
        assert ai_credits.paid_pool_used(conn, date(2026, 11, 10)) == 2
        assert ai_credits.paid_pool_limit(conn, NOW) == max(1000, (41 + 1 + 3) * 30)
    ai_credits.reset_cache()
    assert ai_credits.paid_pool_limit(session, NOW + timedelta(days=90)) == 1000


def test_paid_pool_count_does_not_drop_when_a_trial_user_is_deleted(session):
    owner = user(session, "trial-only")
    trial(session, owner)
    take(session, owner, "task:1")
    take(session, owner, "task:2")
    session.commit()
    session.delete(session.get(User, owner.id))  # 注销：体验和它送的次数一起删
    session.commit()
    assert session.scalar(select(func.count(AICreditGrant.id))) == 0
    assert ai_credits.paid_pool_used(session, date(2026, 11, 10)) == 2


def test_pool_limit_is_cached_per_minute(session):
    owner = user(session)
    card(session, owner)
    session.commit()
    first = ai_credits.paid_pool_limit(session, NOW)
    for n in range(40):
        card(session, user(session, f"later-{n}"))
    session.commit()
    assert ai_credits.paid_pool_limit(session, NOW + timedelta(seconds=30)) == first
    assert ai_credits.paid_pool_limit(session, NOW + timedelta(minutes=1)) == 41 * 30


def test_pool_alert_once_per_level_per_day():
    sent = []
    day = date(2026, 11, 10)
    assert ai_credits.pool_alert(799, 1000, day, alert=sent.append) is None
    ai_credits.pool_alert(800, 1000, day, alert=sent.append)
    ai_credits.pool_alert(900, 1000, day, alert=sent.append)
    ai_credits.pool_alert(1000, 1000, day, alert=sent.append)
    ai_credits.pool_alert(1001, 1000, day, alert=sent.append)
    ai_credits.pool_alert(800, 1000, day + timedelta(days=1), alert=sent.append)
    assert len(sent) == 3
    assert "到了 80%" in sent[0] and "满了" in sent[1]
    assert "openid" not in "".join(sent)


def test_daily_stats_by_kind_source_and_state(session):
    owner = user(session)
    card(session, owner)
    trial(session, owner)
    take(session, owner, "task:1")  # 体验送的
    ai_credits.charge(session, "task:1", NOW)
    session.scalar(select(User).where(User.id == owner.id).with_for_update())
    wallet = ai_credits.lock_wallet(session, owner.id, NOW)
    for n in range(30):
        if wallet.next_slot().source == "bought":
            break
        ai_credits.reserve(session, wallet, f"gift:{n}", "ask", NOW, reason="free_used")
    ai_credits.reserve(
        session,
        wallet,
        "buy:1",
        "clause",
        NOW,
        reason="free_used",
        confirmed=frozenset({"first_purchase"}),
    )
    ai_credits.release(session, "buy:1", NOW, "failed")
    stats = ai_credits.daily_stats(session, date(2026, 11, 10))
    counted = {(i["kind"], i["source"], i["state"]): i["count"] for i in stats["items"]}
    assert counted[("interpret", "gift", "charged")] == 1
    assert counted[("ask", "gift", "reserved")] == 29
    assert counted[("clause", "bought", "released")] == 1
    assert stats["total"] == 31


# ---- 给"用过付费的量"用、保存期 -----------------------------------------------------------


def test_paid_ai_usage_counts_only_bought_credits(session):
    owner = user(session)
    _, ticket, _ = card(session, owner)
    _, _ = trial(session, owner)
    assert ai_credits.paid_ai_usage(session, ticket) is None
    take(session, owner, "task:1")  # 体验送的
    ai_credits.charge(session, "task:1", NOW)
    assert ai_credits.paid_ai_usage(session, ticket) is None
    session.execute(text("UPDATE ai_credit_grants SET used = amount WHERE kind = 'trial'"))
    session.expire_all()
    take(session, owner, "task:2")  # 买来的，结果还没出来
    assert ai_credits.paid_ai_usage(session, ticket, lock=True) == "running"
    ai_credits.charge(session, "task:2", NOW)
    assert ai_credits.paid_ai_usage(session, ticket) == "used"
    assert ai_credits.paid_ai_usage(session, None) is None


def test_grants_and_uses_follow_the_segment_and_the_user(session):
    owner = user(session)
    order, ticket, grant = card(session, owner)
    take(session, owner, "task:1")
    ai_credits.charge(session, "task:1", NOW)
    session.commit()
    session.delete(session.get(User, owner.id))  # 删用户：凭据断开用户、照留
    session.commit()
    session.expire_all()
    assert session.scalar(select(AICreditGrant.user_id)) is None
    assert session.scalar(select(AICreditUse.user_id)) is None
    session.delete(session.get(PayOrder, order.id))  # 订单按保存期删：段、次数、凭据一起删
    session.commit()
    assert session.scalar(select(func.count(AICreditGrant.id))) == 0
    assert session.scalar(select(func.count(AICreditUse.id))) == 0
    assert ai_credits.grant_trial(session, trial(session, user(session, "t"))[0], NOW).amount == 30
