"""批量工具开卖（D-070）：两档商品、续买多送、逐笔记账的退款和赠送收回、体验期内购买、
按剩余天数退、纠错被采纳送天数、最小权限账号。

微信请求全部走 test_payments 的假客户端（MockTransport），不访问真实微信；只连接指定测试库。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text

from tests.test_payments import FakeWechat
from youju import advisor_tasks, boost, cli, payments, rewards
from youju.api import advisor, me, pay, v1
from youju.api import app as app_module
from youju.config import Settings
from youju.db import get_session
from youju.models import (
    BoostTrial,
    Feedback,
    InvoiceRequest,
    PassReward,
    PayOrder,
    User,
    WorkbenchPass,
)

# 当前的用户协议版本：取后台默认值，升版本时不用改测试（同 #286 对 test_boost、test_rewards 的改法）
AGREEMENT = Settings.model_fields["agreement_version"].default

# 北京时间 11 月 2 日 10:00
NOW = datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
DAY = timedelta(days=1)
# 测试人员在体验版里（D-074），代码包里的付费协议版本号是 pay-test
HEADERS = {
    "X-WX-SOURCE": "test-cloud",
    "X-WX-OPENID": "pay-user",
    "X-Youju-Env": "trial",
    "X-Youju-Pay-Terms": "pay-test",
}
ADMIN = {"X-Admin-Token": "test-admin"}
PRICES = {"workbench_30d": 2900, "workbench_90d": 7900}


class Clock:
    def __init__(self):
        self.at = NOW

    def __call__(self):
        return self.at


@pytest.fixture
def shop(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        pay_enabled=True,
        pay_audience="all",
        # 沙箱只给测试名单（D-074）
        pay_openids="pay-user",
        advisor_enabled=True,
        boost_trial_enabled=True,
        renew_bonus_enabled=True,
        trust_wx_headers=True,
        xpay_offer_id="offer-test",
        xpay_appkey_sandbox="12345",
        xpay_price_pass=2900,
        xpay_price_pass_90=7900,
        agreement_version=AGREEMENT,
        admin_token="test-admin",
        cloud_env="test-env",
    )
    fake = FakeWechat(settings)
    clock = Clock()
    for module in (pay, advisor, me, app_module, rewards):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "now", clock)

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    app_module.app.dependency_overrides[pay.wx_client] = lambda: fake.client
    with TestClient(app_module.app) as client:
        join(client, HEADERS)
        yield SimpleNamespace(client=client, settings=settings, fake=fake, clock=clock)
    app_module.app.dependency_overrides.clear()
    fake.http.close()


def join(client, identity):
    assert (
        client.post(
            "/api/v1/me/agreement", headers=identity, json={"version": AGREEMENT}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/me/agreement",
            headers=identity,
            json={"scope": "advisor", "version": advisor_tasks.ADVISOR_AGREEMENT_VERSION},
        ).status_code
        == 200
    )


def create(shop, product="workbench_30d", invoice_type=None):
    # 微信那边待支付：下新单前查上一笔时，不会把还没付的单当成已付款
    shop.fake.status = 1
    body = {"product_id": product, "code": "test-login", "terms_version": "pay-test"}
    # 购买页带上它显示的发票类型（默认就是现在配置的那句）
    body["invoice_type"] = (
        payments.invoice_type(shop.settings) if invoice_type is None else invoice_type
    )
    return shop.client.post("/api/v1/pay/orders", headers=HEADERS, json=body)


def deliver(shop, out_trade_no, product, paid_at):
    """微信的付款推送：金额、商品、付款时间和查单一致。"""
    fake, price = shop.fake, PRICES[product]
    fake.status, fake.fee, fake.left_fee = 2, price, price
    fake.paid_time = int(paid_at.timestamp())
    return shop.client.post(
        "/api/wx/push",
        headers=HEADERS,
        json={
            "Event": "xpay_goods_deliver_notify",
            "OpenId": "pay-user",
            "OutTradeNo": out_trade_no,
            "Env": 1,
            "WeChatPayInfo": {
                "MchOrderNo": "mch-test",
                "TransactionId": "tx-test",
                "PaidTime": fake.paid_time,
            },
            "GoodsInfo": {
                "ProductId": product,
                "Quantity": 1,
                "OrigPrice": price,
                "ActualPrice": price,
                "Attach": out_trade_no,
            },
        },
    )


def buy(shop, session, product="workbench_30d", *, at=None, confirmed=None, invoice_type=None):
    """at 下单并付款；confirmed 收到推送的时间（默认就是付款时间）。"""
    at = at or shop.clock.at
    shop.clock.at = at
    response = create(shop, product, invoice_type)
    assert response.status_code == 201, response.text
    row = session.get(PayOrder, response.json()["orderId"])
    shop.clock.at = confirmed or at
    assert deliver(shop, row.out_trade_no, product, at).json()["ErrCode"] == 0
    session.expire_all()
    return session.get(PayOrder, row.id)


def segments(session, user_id=None):
    stmt = select(WorkbenchPass).order_by(WorkbenchPass.starts_at, WorkbenchPass.id)
    if user_id is not None:
        stmt = stmt.where(WorkbenchPass.user_id == user_id)
    return [
        (p.order_id is not None, p.starts_at, p.ends_at, p.revoked_at is not None)
        for p in session.scalars(stmt)
    ]


def gifts(session, order):
    return [
        (gift.kind, gift.granted_days, rewards.is_revoked(gift), ticket)
        for gift, ticket in payments.order_gifts(session, order)
    ]


def expiry(shop):
    raw = shop.client.get("/api/v1/advisor/status", headers=HEADERS).json()["pass"]
    return (
        datetime.fromisoformat(raw["expiresAt"].replace("Z", "+00:00")) if raw["active"] else None
    )


def refund(shop, row):
    return shop.client.post(f"/api/v1/pay/orders/{row.id}/refund", headers=HEADERS)


def submit(shop, count, key):
    return shop.client.post(
        "/api/v1/advisor/tasks",
        headers={**HEADERS, "Idempotency-Key": key},
        json={"kind": "compare", "rows": [f"合成企业{n}" for n in range(count)]},
    )


def refunds(shop):
    return [body for path, body, _ in shop.fake.calls if path == "/xpay/refund_order"]


# ---- 两档商品 -------------------------------------------------------------------------


def test_two_tiers_listed_short_first_and_each_order_keeps_its_days(shop, session):
    listed = shop.client.get("/api/v1/pay/products", headers=HEADERS).json()
    assert listed["items"] == [
        {"productId": "workbench_30d", "name": "批量工具 30 天", "price": 2900, "days": 30},
        {"productId": "workbench_90d", "name": "批量工具 90 天", "price": 7900, "days": 90},
    ]
    # 第一次买：不是续买，eligibleNow 为 False
    assert listed["renewBonus"] == {"days": 3, "windowDays": 7, "eligibleNow": False}
    assert listed["invoiceType"] is None
    row = buy(shop, session, "workbench_90d")
    assert (row.days, row.price, row.bonus_days) == (90, 7900, 3)
    assert segments(session) == [(True, NOW, NOW + 90 * DAY, False)]
    out = shop.client.get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert out["name"] == "批量工具 90 天" and out["days"] == 90 and out["gifts"] == []
    # 配置改了也不影响已下的单：天数按订单快照
    shop.settings.xpay_price_pass_90 = None
    listed = shop.client.get("/api/v1/pay/products", headers=HEADERS).json()["items"]
    assert [i["days"] for i in listed] == [30]
    assert create(shop, "workbench_90d").status_code == 404


def test_order_keeps_the_invoice_type_shown_when_it_was_placed(shop, session):
    """订单记下用户付款前看到的发票类型：页面带来的那句和现在的配置不一致（看页面后改了配置）就不下单；
    日后改了配置，历史订单仍查得到当时写的是什么（Codex 第三、四轮）。"""
    shop.settings.pay_invoice_type = "电子普通发票（测试）"
    for seen in ("", "旧的一句（测试）"):
        stale = create(shop, invoice_type=seen)
        assert stale.status_code == 409 and "发票类型有更新" in stale.json()["detail"]
    row = buy(shop, session, invoice_type="电子普通发票（测试）")
    shop.settings.pay_invoice_type = "另一种发票（测试）"
    session.expire_all()
    assert session.get(PayOrder, row.id).invoice_type == "电子普通发票（测试）"
    # 沙箱联调时可以还没填：记空
    shop.settings.pay_invoice_type = ""
    assert buy(shop, session, at=NOW + DAY).invoice_type is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, pay_invoice_type="字" * 101)


def test_draft_terms_version_cannot_be_used_for_live_sales():
    """现网只认定稿协议：服务端填成草稿版本号就不启动（定稿的协议必须写明发票类型，小程序测试检查）。"""
    with pytest.raises(ValidationError, match="不能填草稿版本"):
        Settings(_env_file=None, pay_terms_version="pay-2026-10-draft")
    assert Settings(_env_file=None, pay_terms_version="pay-2026-11").pay_terms_version


def test_tier_without_price_is_not_sold_and_same_product_id_refuses_to_start():
    only_30 = Settings(_env_file=None, xpay_price_pass=2900)
    assert payments.products(only_30) == {"workbench_30d": (2900, 30)}
    assert Settings(_env_file=None, xpay_price_pass_90="").xpay_price_pass_90 is None
    with pytest.raises(ValidationError, match="不能是同一个商品编号"):
        Settings(
            _env_file=None,
            xpay_price_pass=2900,
            xpay_price_pass_90=7900,
            xpay_product_pass="same",
            xpay_product_pass_90="same",
        )
    # 开关默认都关
    defaults = Settings(_env_file=None)
    assert not defaults.renew_bonus_enabled and not defaults.correction_reward_enabled


def test_push_naming_the_other_tier_opens_what_was_paid(shop, session):
    """推送里的商品和订单不一致（买的是 30 天，推送说 90 天）：以订单和查单为准，开通 30 天。"""
    response = create(shop, "workbench_30d")
    row = session.get(PayOrder, response.json()["orderId"])
    shop.fake.status, shop.fake.fee = 2, 2900
    data = {
        "Event": "xpay_goods_deliver_notify",
        "OpenId": "pay-user",
        "OutTradeNo": row.out_trade_no,
        "Env": 1,
        "GoodsInfo": {
            "ProductId": "workbench_90d",
            "Quantity": 1,
            "OrigPrice": 2900,
            "ActualPrice": 2900,
            "Attach": row.out_trade_no,
        },
    }
    assert shop.client.post("/api/wx/push", headers=HEADERS, json=data).json()["ErrCode"] == 0
    ticket = session.scalar(select(WorkbenchPass))
    assert ticket.ends_at - ticket.starts_at == timedelta(days=30)


# ---- 续买多送 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paid_at,bonus",
    [
        (NOW + 23 * DAY - timedelta(seconds=1), False),  # 到期前 7 天之外
        (NOW + 23 * DAY, True),  # 到期前正好 7 天
        (NOW + 29 * DAY, True),
        (NOW + 37 * DAY, True),  # 到期后正好 7 天
        (NOW + 37 * DAY + timedelta(seconds=1), False),
    ],
)
def test_renewal_window_is_seven_days_either_side_of_expiry(shop, session, paid_at, bonus):
    buy(shop, session)
    row = buy(shop, session, at=paid_at)
    start = max(paid_at, NOW + 30 * DAY)
    expected = [(True, NOW, NOW + 30 * DAY, False), (True, start, start + 30 * DAY, False)]
    if bonus:
        expected.append((False, start + 30 * DAY, start + 33 * DAY, False))
    assert segments(session) == expected
    assert [(k, d) for k, d, _, _ in gifts(session, row)] == ([("renewal", 3)] if bonus else [])


def test_first_purchase_and_refunded_purchase_are_not_renewals(shop, session):
    first = buy(shop, session)
    assert gifts(session, first) == []
    # 第一笔 7 天内退了：再买不算续买（以前买过的那笔已经收回）
    shop.clock.at = NOW + 2 * DAY
    assert refund(shop, first).status_code == 200
    again = buy(shop, session, at=NOW + 3 * DAY)
    assert gifts(session, again) == []


def test_buying_twice_early_only_the_one_near_expiry_gets_the_bonus(shop, session):
    buy(shop, session)
    near = buy(shop, session, at=NOW + 25 * DAY)
    # 刚续过：最晚到期变成第 63 天，第 26 天再买不在它前后 7 天内
    far = buy(shop, session, at=NOW + 26 * DAY)
    assert [(k, d) for k, d, _, _ in gifts(session, near)] == [("renewal", 3)]
    assert gifts(session, far) == []
    shop.clock.at = NOW + 26 * DAY
    assert expiry(shop) == NOW + 93 * DAY


def test_eligibility_uses_payment_time_not_confirmation_time(shop, session):
    """第 25 天付的款，推送第 40 天才到：资格仍按第 25 天算，送；新的一段从确认开通时（第 40 天）起算，
    确认之前用不了的那几天不算进使用期。"""
    buy(shop, session)
    late = buy(shop, session, at=NOW + 25 * DAY, confirmed=NOW + 40 * DAY)
    assert [(k, d) for k, d, _, _ in gifts(session, late)] == [("renewal", 3)]
    assert segments(session)[1:] == [
        (True, NOW + 40 * DAY, NOW + 70 * DAY, False),
        (False, NOW + 70 * DAY, NOW + 73 * DAY, False),
    ]


def test_late_confirmation_of_a_first_purchase_starts_when_it_is_confirmed(shop, session):
    """推送晚到两小时：使用期从确认开通时起算，30 天一天不少；7 天自助退款仍从付款时间算。"""
    row = buy(shop, session, at=NOW, confirmed=NOW + timedelta(hours=2))
    assert segments(session) == [
        (True, NOW + timedelta(hours=2), NOW + timedelta(hours=2) + 30 * DAY, False)
    ]
    shop.clock.at = NOW + 7 * DAY + timedelta(hours=1)
    assert refund(shop, row).json()["detail"] == "已超过 7 天"


def test_bonus_follows_the_switch_at_order_time(shop, session):
    """下单时开关开着就算数（付款页当时写着这条规则）；下单时关着，付款时打开也不送。"""
    buy(shop, session)
    shop.clock.at = NOW + 25 * DAY
    promised = create(shop).json()["orderId"]
    shop.settings.renew_bonus_enabled = False
    assert shop.client.get("/api/v1/pay/products", headers=HEADERS).json()["renewBonus"] is None
    unpromised = create(shop).json()["orderId"]
    shop.settings.renew_bonus_enabled = True
    for order_id in (promised, unpromised):
        row = session.get(PayOrder, order_id)
        assert (
            deliver(shop, row.out_trade_no, "workbench_30d", NOW + 25 * DAY).json()["ErrCode"] == 0
        )
    session.expire_all()
    assert session.get(PayOrder, unpromised).bonus_days == 0
    assert [k for k, *_ in gifts(session, session.get(PayOrder, promised))] == ["renewal"]
    assert gifts(session, session.get(PayOrder, unpromised)) == []


def test_products_say_whether_renewing_now_gets_the_bonus(shop, session):
    buy(shop, session)
    for at, eligible in ((NOW + 20 * DAY, False), (NOW + 24 * DAY, True), (NOW + 38 * DAY, False)):
        shop.clock.at = at
        offer = shop.client.get("/api/v1/pay/products", headers=HEADERS).json()["renewBonus"]
        assert offer["eligibleNow"] is eligible
    # 现网对所有人开卖后，看价格不要求登录：认不出是谁就是 False（沙箱只给测试名单，所以这里切到现网）
    shop.clock.at = NOW + 24 * DAY
    shop.settings.xpay_env, shop.settings.pay_terms_version = 0, "pay-test"
    shop.settings.pay_invoice_type = "电子普通发票（测试）"
    anonymous = {"X-Youju-Env": "release", "X-Youju-Pay-Terms": "pay-test"}
    offer = shop.client.get("/api/v1/pay/products", headers=anonymous).json()["renewBonus"]
    assert offer["eligibleNow"] is False


def test_renewal_bonus_does_not_count_toward_reward_limits(shop, session):
    buy(shop, session)
    buy(shop, session, at=NOW + 25 * DAY)
    user_id = session.scalar(select(User.id))
    assert list(session.scalars(rewards.monthly(session, user_id, NOW + 25 * DAY))) == []
    with pytest.raises(ValueError):
        rewards.grant(session, user_id, "renewal", 3, "x", 1, NOW)


# ---- 逐笔记账的退款和赠送收回 ------------------------------------------------------------


def test_early_renewal_refund_takes_back_only_that_order_and_its_bonus(shop, session):
    """提前续买的那笔（还没开始）7 天内没用过：可以自助全额退，只收回它自己和随它送的 3 天。"""
    first = buy(shop, session)
    renewal = buy(shop, session, at=NOW + 25 * DAY)
    shop.clock.at = NOW + 26 * DAY
    # 这期间用的是第一笔（一次 51 家，记在第一笔的使用期上）；第二笔还没开始，没用过
    assert submit(shop, 51, "big").json()["usageSource"] == "pass"
    session.expire_all()
    assert refund(shop, first).json()["detail"] == "已超过 7 天"
    response = refund(shop, renewal)
    assert response.status_code == 200 and response.json()["status"] == "退款中"
    assert [(b["refund_fee"], b["refund_reason"], b["req_from"]) for b in refunds(shop)] == [
        (2900, "3", "2")
    ]
    session.expire_all()
    # 收回这一笔时，后面的段（送的 3 天）先往前挪到它的开始时间，再一起收回
    assert segments(session) == [
        (True, NOW, NOW + 30 * DAY, False),
        (True, NOW + 30 * DAY, NOW + 60 * DAY, True),
        (False, NOW + 30 * DAY, NOW + 33 * DAY, True),
    ]
    bonus = gifts(session, renewal)
    assert bonus[0][:3] == ("renewal", 3, True)
    assert session.get(PassReward, session.scalar(select(PassReward.id))).note == (
        "已收回：随订单退款"
    )
    out = response.json()
    assert out["gifts"][0]["revoked"] is True and out["refundAmount"] == 2900
    assert expiry(shop) == NOW + 30 * DAY


def test_refunding_the_current_order_moves_the_later_order_and_its_bonus_forward(shop, session):
    """一笔正在用（没用过付费的量）、一笔排在后面：退掉正在用的那笔，后面那笔整段往前挪到现在。"""
    first = buy(shop, session, at=NOW)
    # 7 天内续买：到期是第 30 天，第 5 天付款不在窗口里，不送
    second = buy(shop, session, at=NOW + 5 * DAY)
    assert gifts(session, second) == []
    shop.clock.at = NOW + 6 * DAY
    assert refund(shop, first).status_code == 200
    session.expire_all()
    assert segments(session) == [
        (True, NOW, NOW + 30 * DAY, True),
        (True, NOW + 6 * DAY, NOW + 36 * DAY, False),
    ]
    assert expiry(shop) == NOW + 36 * DAY


def test_ios_refund_notification_also_takes_back_the_bonus(shop, session):
    shop.fake.order_type = 7
    buy(shop, session)
    renewal = buy(shop, session, at=NOW + 25 * DAY)
    shop.fake.status, shop.fake.left_fee = 8, 0
    data = {
        "Event": "xpay_refund_notify",
        "OpenId": "pay-user",
        "MchOrderId": renewal.out_trade_no,
        "WxOrderId": renewal.wx_order_id,
    }
    assert shop.client.post("/api/wx/push", headers=HEADERS, json=data).json()["ErrCode"] == 0
    session.expire_all()
    row = session.get(PayOrder, renewal.id)
    assert row.status == "refunded" and row.refund_fee == 2900
    assert [revoked for _, _, revoked, _ in gifts(session, row)] == [True]


def test_order_list_shows_each_gift_and_refund_amount(shop, session):
    buy(shop, session)
    renewal = buy(shop, session, at=NOW + 25 * DAY)
    items = shop.client.get("/api/v1/pay/orders", headers=HEADERS).json()["items"]
    assert [i["orderId"] for i in items] == [renewal.id, renewal.id - 1]
    top = items[0]
    parse = lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00"))  # noqa: E731
    assert [
        (g["kind"], g["name"], g["days"], parse(g["startsAt"]), parse(g["endsAt"]), g["revoked"])
        for g in top["gifts"]
    ] == [("renewal", "续买多送", 3, NOW + 60 * DAY, NOW + 63 * DAY, False)]
    assert top["refundAmount"] is None and items[1]["gifts"] == []


# ---- 体验期内购买 -----------------------------------------------------------------------


def start_trial(shop):
    response = shop.client.post("/api/v1/advisor/trial", headers=HEADERS, json={})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "bought_after,days", [(timedelta(hours=23), 3), (timedelta(hours=50), 1), (DAY, 2)]
)
def test_buying_during_the_trial_starts_now_and_adds_the_rest_of_the_trial(
    shop, session, bought_after, days
):
    start_trial(shop)
    at = NOW + bought_after
    row = buy(shop, session, at=at)
    trial = session.scalar(select(BoostTrial))
    assert trial.ends_at == at  # 体验到付款确认时结束
    assert segments(session) == [
        (True, at, at + 30 * DAY, False),
        (False, at + 30 * DAY, at + (30 + days) * DAY, False),
    ]
    kind, granted, revoked, _ = gifts(session, row)[0]
    assert (kind, granted, revoked) == ("trial", days, False)
    shop.clock.at = at + timedelta(minutes=1)
    status = shop.client.get("/api/v1/advisor/status", headers=HEADERS).json()
    assert status["trial"]["active"] is False and status["pass"]["active"] is True
    assert submit(shop, 120, "after-buy").json()["usageSource"] == "pass"
    note = session.get(PassReward, session.scalar(select(PassReward.id))).note
    assert note.startswith("加量体验还剩约") and f"折成 {days} 天" in note


def test_trial_days_stay_when_the_order_is_refunded(shop, session):
    """体验折算的是用户自己没用完的体验时间：退款不收回，跟着往前挪到现在。"""
    start_trial(shop)
    row = buy(shop, session, at=NOW + timedelta(hours=10))
    shop.clock.at = NOW + DAY
    assert refund(shop, row).status_code == 200
    session.expire_all()
    assert segments(session) == [
        (True, NOW + timedelta(hours=10), NOW + timedelta(hours=10) + 30 * DAY, True),
        (False, NOW + DAY, NOW + 4 * DAY, False),
    ]
    assert expiry(shop) == NOW + 4 * DAY


def test_trial_started_after_paying_but_before_confirmation_also_ends(shop, session):
    """付款后、确认前才开始的体验：开通时同样结束体验、剩下的折成天数（使用期和体验不同时进行）。"""
    response = create(shop)
    row = session.get(PayOrder, response.json()["orderId"])
    shop.clock.at = NOW + timedelta(hours=1)
    start_trial(shop)
    shop.clock.at = NOW + timedelta(hours=5)
    assert deliver(shop, row.out_trade_no, "workbench_30d", NOW).json()["ErrCode"] == 0
    session.expire_all()
    assert session.scalar(select(BoostTrial)).ends_at == NOW + timedelta(hours=5)
    # 体验到 73 小时结束，确认时还剩 68 小时：折成 3 天
    assert [(k, d) for k, d, _, _ in gifts(session, row)] == [("trial", 3)]
    start = NOW + timedelta(hours=5)
    assert segments(session) == [
        (True, start, start + 30 * DAY, False),
        (False, start + 30 * DAY, start + 33 * DAY, False),
    ]


def test_trial_that_already_ended_is_not_converted(shop, session):
    start_trial(shop)
    # 第 2 天付的款，推送第 4 天才到：体验已经结束，没有剩下的
    row = buy(shop, session, at=NOW + DAY, confirmed=NOW + 4 * DAY)
    assert gifts(session, row) == []
    assert session.scalar(select(BoostTrial)).ends_at == NOW + timedelta(hours=72)


# ---- 负责人按规则退款（系统故障、停止服务） ---------------------------------------------


@pytest.fixture
def run_cli(shop, session_factory, monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            return shop.clock.at

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr("youju.config.get_settings", lambda: shop.settings)
    monkeypatch.setattr("youju.xpay.XPayClient", lambda settings, http: shop.fake.client)

    def run(*args):
        return cli.main(["pay-refund", *args])

    return run


def complete_refund(shop, session, row, left):
    """微信那边退款完成：状态 5，剩余 left 分；下一轮补单查单后写成已退款。"""
    shop.fake.status, shop.fake.left_fee = 5, left
    payments.run_payments(
        session,
        settings=shop.settings,
        client=shop.fake.client,
        stamp=shop.clock.at + timedelta(minutes=30),
        alert=lambda _: True,
    )
    session.expire_all()
    return session.get(PayOrder, row.id)


def test_remaining_days_refund_counts_part_days_as_whole_and_rounds_up(
    shop, session, run_cli, capsys
):
    row = buy(shop, session)
    shop.clock.at = NOW + timedelta(days=10, hours=12)
    reason = ["--order-id", str(row.id), "--remaining", "--reason", "系统故障三天无法使用"]
    assert run_cli(*reason, "--dry-run") == 0
    out = capsys.readouterr().out
    # 还剩 19.5 天按 20 天：2900 × 20 ÷ 30 = 1933.3，按 1934 分
    assert "按剩余 20 天退：19.34 元" in out and "演练完成，未写入" in out
    session.expire_all()
    assert session.get(PayOrder, row.id).refund_status is None and not refunds(shop)
    assert run_cli(*reason) == 0
    assert [(b["refund_fee"], b["refund_reason"], b["req_from"]) for b in refunds(shop)] == [
        (1934, "1", "1")
    ]
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert (row.refund_source, row.refund_note, row.refund_fee) == (
        "staff",
        "系统故障三天无法使用",
        1934,
    )
    assert payments.order_pass(session, row).revoked_at == shop.clock.at
    row = complete_refund(shop, session, row, 2900 - 1934)
    assert (row.status, row.refund_status, row.refund_fee) == ("refunded", "completed", 1934)
    out = shop.client.get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert out["status"] == "已退款" and out["refundAmount"] == 1934


def test_extra_days_that_were_not_made_up_are_added_and_capped(shop, session, run_cli, capsys):
    """此前因系统故障不能用、又没补上的天数，核实后一起退；最多整笔。"""
    row = buy(shop, session)
    shop.clock.at = NOW + 20 * DAY
    base = ["--order-id", str(row.id), "--remaining", "--reason", "系统故障两天没补上"]
    assert run_cli(*base, "--extra-days", "2", "--dry-run") == 0
    # 剩 10 天 + 没补上的 2 天 = 12 天：2900 × 12 ÷ 30 = 1160
    assert "按剩余 12 天退：11.60 元（含此前没补上的 2 天）" in capsys.readouterr().out
    assert run_cli(*base, "--extra-days", "90", "--dry-run") == 0
    assert "按剩余 30 天退：29.00 元" in capsys.readouterr().out
    assert (
        run_cli("--order-id", str(row.id), "--full", "--extra-days", "2", "--reason", "x" * 8) == 2
    )
    assert run_cli("--all-active", "--remaining", "--extra-days", "1", "--reason", "x" * 8) == 2
    with pytest.raises(SystemExit):
        run_cli(*base, "--extra-days", "91")
    assert not refunds(shop)


def test_remaining_refund_of_a_future_renewal_is_the_full_price_and_takes_its_bonus(
    shop, session, run_cli
):
    buy(shop, session)
    renewal = buy(shop, session, at=NOW + 25 * DAY)
    shop.clock.at = NOW + 26 * DAY
    reason = ["--order-id", str(renewal.id), "--remaining", "--reason", "停止服务，按剩余天数退"]
    assert run_cli(*reason) == 0
    assert refunds(shop)[0]["refund_fee"] == 2900
    session.expire_all()
    assert [revoked for _, _, revoked, _ in gifts(session, renewal)] == [True]


def test_unexpected_refund_amount_goes_to_manual_and_full_refund_is_accepted(
    shop, session, run_cli
):
    row = buy(shop, session)
    shop.clock.at = NOW + 10 * DAY
    assert run_cli("--order-id", str(row.id), "--remaining", "--reason", "系统故障按剩余退") == 0
    session.expire_all()
    fee = session.get(PayOrder, row.id).refund_fee
    assert fee == 1934
    # 微信说只剩 100 分：既不是全额也不是按剩余天数退的结果
    manual = complete_refund(shop, session, session.get(PayOrder, row.id), 100)
    assert manual.status == "manual" and "和预期不符" in manual.error
    # 另一笔：按剩余天数发起，微信那边却是全额退了（例如商户后台退的）：按实际退的记
    other = buy(shop, session, at=NOW + 10 * DAY)
    shop.clock.at = NOW + 20 * DAY
    assert run_cli("--order-id", str(other.id), "--remaining", "--reason", "系统故障按剩余退") == 0
    session.expire_all()
    assert session.get(PayOrder, other.id).refund_fee == 1934
    full = complete_refund(shop, session, session.get(PayOrder, other.id), 0)
    assert full.status == "refunded" and full.refund_fee == 2900


def test_full_refund_and_cases_that_are_not_refunded(shop, session, run_cli, capsys):
    row = buy(shop, session)
    session.add(
        InvoiceRequest(
            order_id=row.id,
            user_id=row.user_id,
            title_type="personal",
            title="合成抬头",
            email="a@example.com",
            status="issued",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()
    full = ["--order-id", str(row.id), "--full", "--reason", "重复扣款，全额退回"]
    # 退款不等冲红（D-074）：已开过发票的照常退，只提醒退完按退款金额开红字发票
    assert run_cli(*full) == 0
    assert "退款后请按退款金额开红字发票" in capsys.readouterr().out
    assert refunds(shop)[0]["refund_fee"] == 2900
    # 管理台开票页"要冲红"列出它（带退款金额和进度）；"待开"里没有
    reverse = shop.client.get("/api/admin/invoices?status=reverse", headers=ADMIN).json()
    assert [(i["orderId"], i["needsReversal"], i["refundFee"]) for i in reverse] == [
        (row.id, True, 2900)
    ]
    assert reverse[0]["refundState"] == "退款处理中，确认退款后再冲红"
    assert reverse[0]["canMarkReversed"] is False
    assert shop.client.get("/api/admin/invoices", headers=ADMIN).json() == []
    invoice_id = reverse[0]["id"]
    # 退款还没确认：不能标已冲红
    early = shop.client.post(f"/api/admin/invoices/{invoice_id}/reversed", headers=ADMIN)
    assert early.status_code == 409
    assert complete_refund(shop, session, row, 0).status == "refunded"
    reverse = shop.client.get("/api/admin/invoices?status=reverse", headers=ADMIN).json()
    assert reverse[0]["refundState"] == "已退款，按退款金额开红字发票"
    assert reverse[0]["canMarkReversed"] is True
    done = shop.client.post(f"/api/admin/invoices/{invoice_id}/reversed", headers=ADMIN)
    assert done.status_code == 200
    again = shop.client.post(f"/api/admin/invoices/{invoice_id}/reversed", headers=ADMIN)
    assert again.status_code == 409
    # 标过以后"要冲红"里不再列出；"全部"里写已冲红；用户订单页的发票写"已冲红"
    assert shop.client.get("/api/admin/invoices?status=reverse", headers=ADMIN).json() == []
    everything = shop.client.get("/api/admin/invoices?status=all", headers=ADMIN).json()
    assert everything[0]["reversedAt"] and everything[0]["needsReversal"] is False
    mine = shop.client.get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert mine["invoice"]["status"] == "已冲红"
    with pytest.raises(SystemExit):
        run_cli(*full, "--invoice-reversed")
    # 苹果手机付款的不主动退
    shop.fake.order_type = 7
    apple = buy(shop, session, at=NOW + DAY)
    capsys.readouterr()
    assert run_cli("--order-id", str(apple.id), "--full", "--reason", "重复扣款，全额退回") == 0
    assert "App Store" in capsys.readouterr().out and len(refunds(shop)) == 1
    assert run_cli("--order-id", "999", "--full", "--reason", "重复扣款，全额退回") == 1
    with pytest.raises(SystemExit):
        run_cli("--order-id", str(row.id), "--full", "--reason", "太短")


def test_stop_service_refunds_every_active_order_by_remaining_days(shop, session, run_cli, capsys):
    first = buy(shop, session)
    renewal = buy(shop, session, at=NOW + 25 * DAY)
    # 另一位用苹果手机付款的用户：只列出来，不主动退
    other = User(openid="apple-user", agreement_version=AGREEMENT, agreement_at=NOW)
    session.add(other)
    session.flush()
    apple = PayOrder(
        user_id=other.id,
        out_trade_no="apple-order",
        product_id="workbench_30d",
        price=2900,
        days=30,
        env=1,
        terms_version="pay-test",
        status="completed",
        platform="ios",
        paid_time=NOW + 20 * DAY,
        created_at=NOW + 20 * DAY,
        updated_at=NOW + 20 * DAY,
    )
    session.add(apple)
    session.flush()
    session.add(
        WorkbenchPass(
            user_id=other.id, order_id=apple.id, starts_at=apple.paid_time, ends_at=NOW + 50 * DAY
        )
    )
    session.commit()
    shop.clock.at = NOW + 29 * DAY + timedelta(hours=12)
    stop = ["--all-active", "--remaining", "--reason", "2026-12-31 起停止服务"]
    assert run_cli("--all-active", "--full", "--reason", "停止服务全额退回") == 2
    assert run_cli(*stop, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "使用期还没用完的订单 3 笔" in out and "可退 2 笔，不退 1 笔" in out
    assert run_cli(*stop) == 0
    # 第一笔剩半天按 1 天（97 分）；续买那笔整段没开始，全额
    assert sorted(b["refund_fee"] for b in refunds(shop)) == [97, 2900]
    session.expire_all()
    assert {o.id: o.refund_source for o in session.scalars(select(PayOrder))} == {
        first.id: "staff",
        renewal.id: "staff",
        apple.id: None,
    }


# ---- 纠错被采纳送 1 天（可选，单独开关） --------------------------------------------------


def feedback(session, content="截止日期和原文不一致"):
    row = Feedback(user_id=session.scalar(select(User.id)), content=content, check_result="pass")
    session.add(row)
    session.commit()
    return row


def test_correction_reward_switch_off_by_default(shop, session):
    row = feedback(session)
    assert (
        shop.client.post(
            f"/api/admin/feedback/{row.id}/reward/correction", headers=ADMIN
        ).status_code
        == 404
    )
    item = shop.client.get("/api/admin/feedback", headers=ADMIN).json()[0]
    assert item["correctionRewardEnabled"] is False and item["rewardsEnabled"] is False
    assert shop.client.get("/api/v1/me", headers=HEADERS).json()["correctionReward"] is None


def test_correction_reward_one_day_each_three_days_a_month(shop, session):
    shop.settings.correction_reward_enabled = True
    rows = [feedback(session, f"第 {n} 处日期有误") for n in range(4)]
    granted = [
        shop.client.post(f"/api/admin/feedback/{r.id}/reward/correction", headers=ADMIN).json()
        for r in rows
    ]
    assert [g["days"] for g in granted] == [1, 1, 1, 0]
    assert granted[3]["message"] == "已记录，这次没有送天数"
    assert granted[3]["note"] == "本月纠错奖励已到上限（每月最多 3 天）"
    # 下个月（北京时间 12 月 1 日）重新算
    shop.clock.at = datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
    nxt = feedback(session, "下个月的纠错")
    assert (
        shop.client.post(f"/api/admin/feedback/{nxt.id}/reward/correction", headers=ADMIN).json()[
            "days"
        ]
        == 1
    )
    listed = {
        i["id"]: i for i in shop.client.get("/api/admin/feedback?status=all", headers=ADMIN).json()
    }
    assert listed[rows[0].id]["correctionRewardEnabled"] is True
    assert listed[rows[3].id]["rewardNote"] == "本月纠错奖励已到上限（每月最多 3 天）"
    assert all(listed[r.id]["handledAt"] for r in rows)
    profile = shop.client.get("/api/v1/me", headers=HEADERS).json()
    assert profile["correctionReward"] == {
        "days": 1,
        "monthLimit": 3,
        "rule": rewards.CORRECTION_RULE,
    }
    data = shop.client.get("/api/v1/me/data", headers=HEADERS).json()["passGifts"]
    assert [(g["name"], g["days"]) for g in data] == [("纠错被采纳", 1)] + [
        ("纠错被采纳", d) for d in (0, 1, 1, 1)
    ]


def test_legacy_reward_switch_does_not_turn_on_correction_reward(shop, session):
    """旧的奖励总开关开着、纠错送天数的开关关着：纠错不送天数（D-070 ⑦ 单独开关，Codex 审阅）。"""
    shop.settings.rewards_enabled = True
    row = feedback(session)
    assert (
        shop.client.post(
            f"/api/admin/feedback/{row.id}/reward/correction", headers=ADMIN
        ).status_code
        == 404
    )
    item = shop.client.get("/api/admin/feedback", headers=ADMIN).json()[0]
    assert item["correctionRewardEnabled"] is False and item["rewardsEnabled"] is True
    assert shop.client.get("/api/v1/me", headers=HEADERS).json()["correctionReward"] is None


def test_correction_switch_alone_turns_on_nothing_else(shop, session):
    """只开纠错送天数：提的栏目已接入、支付问题不发；第一次批量功能、引荐这些旧奖励不会跟着开。"""
    shop.settings.correction_reward_enabled = True
    source = feedback(session, "申请覆盖：某市科技局")
    payment = feedback(session, "支付问题（订单 12）：没开通")
    assert (
        shop.client.post(
            f"/api/admin/feedback/{source.id}/reward/source", headers=ADMIN
        ).status_code
        == 404
    )
    assert (
        shop.client.post(
            f"/api/admin/feedback/{source.id}/reward/correction", headers=ADMIN
        ).status_code
        == 422
    )
    assert (
        shop.client.post(
            f"/api/admin/feedback/{payment.id}/reward/correction", headers=ADMIN
        ).status_code
        == 422
    )
    kinds = {
        i["id"]: i["kind"] for i in shop.client.get("/api/admin/feedback", headers=ADMIN).json()
    }
    assert kinds == {source.id: "申请覆盖", payment.id: "支付问题"}
    # 第一次批量功能、引荐两项奖励已经删了（rewards.RETIRED_KINDS），补发也不会发
    assert not hasattr(rewards, "first_completion")
    assert rewards.reconcile(session, settings=shop.settings, stamp=NOW) == {
        "granted": 0,
        "failed": 0,
    }
    assert shop.client.get("/api/v1/me/rewards", headers=HEADERS).status_code == 404
    page = shop.client.get("/admin/feedback").text
    assert "采纳标准：第一个指出、经核对属实的事实错误" in page


# ---- 提示语、我的数据 --------------------------------------------------------------------


def test_free_limit_message_only_mentions_buying_while_on_sale(shop, session):
    for n in range(3):
        assert submit(shop, 1, f"free-{n}").status_code == 202
    detail = submit(shop, 1, "fourth").json()["detail"]
    assert detail == {
        "code": "FREE_MONTHLY_LIMIT",
        "canBuy": True,
        "message": "本月免费次数已用完，下月 1 日恢复；也可以买批量工具，不限次数、每次 500 家以内，到期不扣钱",
    }
    shop.settings.pay_enabled = False
    detail = submit(shop, 1, "fifth").json()["detail"]
    assert detail["message"] == "本月免费次数已用完，下月 1 日恢复"
    assert detail["canBuy"] is False
    assert "客户工作台" not in json.dumps(detail, ensure_ascii=False)


def test_personal_data_lists_every_gift(shop, session):
    start_trial(shop)
    buy(shop, session, at=NOW + timedelta(hours=1))
    gifts_out = shop.client.get("/api/v1/me/data", headers=HEADERS).json()["passGifts"]
    assert [(g["kind"], g["name"], g["days"], g["revoked"]) for g in gifts_out] == [
        ("trial", "加量体验折算", 3, False)
    ]


# ---- 最小权限账号 ------------------------------------------------------------------------


def test_restricted_api_account_runs_the_whole_sale(shop, restricted_api_factory, monkeypatch):
    """按部署文件授权（含"批量工具两档开卖权限"）：体验期内买、续买多送、自助退款收回赠送、注销都不缺权限。"""
    from youju import access_log

    with restricted_api_factory() as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        start_trial(shop)
        with limited() as session:
            first = buy(shop, session, at=NOW + timedelta(hours=5))
            assert [k for k, *_ in gifts(session, first)] == ["trial"]
            renewal = buy(shop, session, at=NOW + 30 * DAY)
            assert [k for k, *_ in gifts(session, renewal)] == ["renewal"]
            shop.clock.at = NOW + 31 * DAY
            assert refund(shop, renewal).status_code == 200
            session.expire_all()
            assert [r for _, _, r, _ in gifts(session, renewal)] == [True]
        assert shop.client.get("/api/v1/pay/orders", headers=HEADERS).status_code == 200
        assert shop.client.get("/api/v1/me/data", headers=HEADERS).status_code == 200
        assert shop.client.delete("/api/v1/me", headers=HEADERS).status_code == 204


def test_boost_trial_has_no_effect_on_pass_when_nothing_bought(shop, session):
    """没买的体验照旧：不发使用期，到期自动回到免费量（加量体验本身的规则在 test_boost）。"""
    start_trial(shop)
    shop.clock.at = NOW + timedelta(hours=73)
    assert boost.active_trial(session, session.scalar(select(User.id)), shop.clock.at) is None
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 0


# ---- 迁移 0032 回退 ----------------------------------------------------------------------


def test_downgrade_refuses_while_gift_days_or_partial_refunds_are_open(shop, session, db_engine):
    """还有没到期的赠送天数、或按剩余天数退款没办完：不能自动回退（旧代码不认赠送、只会全额退）；
    都处理完了才回退，回退后再升级，结构恢复。"""
    from alembic import command
    from sqlalchemy import inspect

    from youju.migrations import alembic_config

    buy(shop, session)
    renewal = buy(shop, session, at=NOW + 25 * DAY)
    gift_pass = gifts(session, renewal)[0][3]
    session.commit()
    session.close()
    config = alembic_config(db_engine.url.render_as_string(hide_password=False))
    try:
        with pytest.raises(RuntimeError, match="不能自动回退 0032"):
            command.downgrade(config, "0031")
        with db_engine.begin() as conn:
            conn.execute(
                text("UPDATE workbench_passes SET revoked_at = :at WHERE id = :id"),
                {"at": NOW, "id": gift_pass.id},
            )
            conn.execute(
                text(
                    "UPDATE pay_orders SET status = 'refunding', refund_fee = 1934 WHERE id = :id"
                ),
                {"id": renewal.id},
            )
        with pytest.raises(RuntimeError, match="1 笔按剩余天数退款"):
            command.downgrade(config, "0031")
        with db_engine.begin() as conn:
            conn.execute(
                text("UPDATE pay_orders SET status = 'refunded' WHERE id = :id"), {"id": renewal.id}
            )
        command.downgrade(config, "0031")
        columns = {c["name"] for c in inspect(db_engine).get_columns("pay_orders")}
        assert not {"days", "bonus_days", "refund_fee"} & columns
        with db_engine.connect() as conn:
            assert conn.scalar(text("SELECT COUNT(*) FROM pass_rewards")) == 0
    finally:
        command.upgrade(config, "head")
    with db_engine.connect() as conn:
        assert conn.scalar(text("SELECT MIN(days) FROM pay_orders")) == 30
