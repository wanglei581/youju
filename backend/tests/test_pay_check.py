"""付款后主动核对订单（10/19 沙箱实测：付了款一直显示"待支付"）、付了款没开通的订单自助退款、
我的页的批量工具标识。微信接口全部用 test_payments 的 FakeWechat（MockTransport），不访问真实微信。"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.test_payments import (  # noqa: F401
    HEADERS,
    NOW,
    create,
    delivered,
    order,
    payload,
    push,
    run,
    setup_pay,
)
from youju import payments
from youju.api import app as app_module
from youju.api import pay
from youju.db import get_session
from youju.models import PayEvent, PayOrder, WorkbenchPass


def check(setup_pay, row, headers=HEADERS):  # noqa: F811
    return setup_pay[0].post(f"/api/v1/pay/orders/{row.id}/check", headers=headers)


def calls(setup_pay, path):  # noqa: F811
    return [call for call in setup_pay[2].calls if call[0] == path]


def again():
    """模拟过了 2 秒：清掉进程内的核对时间。"""
    pay._checked.clear()


def test_check_opens_a_paid_order_at_once(setup_pay, session):  # noqa: F811
    """推送没到、补单还没轮到：付款后小程序一核对就开通，并通知微信已发货。"""
    row = order(session, create(setup_pay))
    setup_pay[2].status = 2
    response = check(setup_pay, row)
    assert response.status_code == 200
    body = response.json()
    assert body["check"] == "paid"
    assert body["status"] == "已完成"
    assert body["passStartsAt"] and body["passEndsAt"]
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "completed" and row.goods_notified and row.platform == "android"
    ticket = payments.order_pass(session, row)
    assert ticket.starts_at == NOW and ticket.ends_at == NOW + timedelta(days=30)
    assert len(calls(setup_pay, "/xpay/notify_provide_goods")) == 1
    # 已开通的再核对：不再问微信，原样返回
    again()
    before = len(setup_pay[2].calls)
    body = check(setup_pay, row).json()
    assert body["check"] is None and body["status"] == "已完成"
    assert len(setup_pay[2].calls) == before
    # 推送晚到：照常应答成功，不会开通第二段
    assert push(setup_pay, payload(row)).json()["ErrCode"] == 0
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 1


@pytest.mark.parametrize(
    "status,result,label",
    [(1, "unpaid", "待确认"), (0, "unpaid", "待确认"), (6, "closed", "已关闭")],
)
def test_check_unpaid_and_closed(setup_pay, session, status, result, label):  # noqa: F811
    row = order(session, create(setup_pay))
    setup_pay[2].status = status
    body = check(setup_pay, row).json()
    assert body["check"] == result and body["status"] == label
    assert body["createdAt"]
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 0
    assert not calls(setup_pay, "/xpay/notify_provide_goods")


def test_check_unavailable_leaves_the_order_to_the_minute_job(setup_pay, session):  # noqa: F811
    """查单失败（这里是微信返回的单号对不上）：订单不动，交给每分钟的补单；补单能查时照常开通。"""
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status, fake.bad_order = 2, True
    body = check(setup_pay, row).json()
    assert body["check"] == "unavailable" and body["status"] == "待确认"
    session.expire_all()
    assert session.get(PayOrder, row.id).status == "pending"
    fake.bad_order = False
    run(session, setup_pay, NOW + timedelta(minutes=3))
    session.expire_all()
    assert session.get(PayOrder, row.id).status == "completed"


def test_check_is_throttled_per_order(setup_pay, session):  # noqa: F811
    """同一张订单 2 秒内只问一次微信：小程序轮询、连点都不会多查。"""
    row = order(session, create(setup_pay))
    setup_pay[2].status = 1
    assert check(setup_pay, row).json()["check"] == "unpaid"
    queried = len(calls(setup_pay, "/xpay/query_order"))
    body = check(setup_pay, row).json()
    assert body["check"] is None and body["status"] == "待确认"
    assert len(calls(setup_pay, "/xpay/query_order")) == queried
    again()
    setup_pay[2].status = 2
    assert check(setup_pay, row).json()["check"] == "paid"


def test_check_only_recent_own_pending_orders(setup_pay, session):  # noqa: F811
    client, _, fake = setup_pay
    first = order(session, create(setup_pay))
    # 别人的订单：查不到
    stranger = {**HEADERS, "X-WX-OPENID": "someone-else"}
    client.post("/api/v1/me/agreement", headers=stranger, json={"version": "2026-11"})
    assert check(setup_pay, first, stranger).status_code == 404
    # 超过 48 小时的待确认订单不由用户核对（交给补单，48 小时确认不了会关单）
    first.created_at = NOW - timedelta(hours=49)
    session.commit()
    fake.status = 2
    body = check(setup_pay, first).json()
    assert body["check"] is None and body["status"] == "待确认"
    assert not calls(setup_pay, "/xpay/query_order")
    # 只核对最近 3 张待确认订单：第 4 张（最早的）不查。
    # 下单时间挪到 10 分钟以前，免得下一单"先查上一笔""10 分钟内最多 3 单"
    fake.status = 1
    rows = []
    for minutes in (40, 30, 20, 10):
        made = order(session, create(setup_pay))
        made.created_at = NOW - timedelta(minutes=minutes)
        session.commit()
        rows.append(made)
    assert check(setup_pay, rows[0]).json()["check"] is None
    assert [check(setup_pay, made).json()["check"] for made in rows[1:]] == ["unpaid"] * 3


def test_check_flags_amount_mismatch_for_staff(setup_pay, session):  # noqa: F811
    """微信确认收了款，但金额和订单对不上：不开通，转人工并告警；用户不能自助退（金额要人工核对）。"""
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status, fake.fee = 2, 101
    body = check(setup_pay, row).json()
    assert body["check"] == "manual" and body["status"] == "需人工处理"
    assert body["canRefund"] is False
    assert "人工核对" in body["refundUnavailableReason"]
    assert pay_refund(setup_pay, row).status_code == 422
    alerts = []
    payments.run_payments(
        session,
        settings=setup_pay[1],
        client=fake.client,
        stamp=NOW + timedelta(minutes=3),
        alert=lambda text: alerts.append(text) or True,
    )
    assert alerts and "需人工处理" in alerts[0]


def test_check_sees_a_refund_done_on_wechat(setup_pay, session):  # noqa: F811
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status, fake.left_fee = 8, 0
    body = check(setup_pay, row).json()
    assert body["check"] == "refunded" and body["status"] == "已退款"


def pay_refund(setup_pay, row):  # noqa: F811
    return setup_pay[0].post(f"/api/v1/pay/orders/{row.id}/refund", headers=HEADERS)


def undelivered(setup_pay, session):  # noqa: F811
    """微信确认收了全款，但查单没有付款时间：核验不过、转人工，没开通使用期。"""
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status, fake.paid_time = 2, 0
    body = check(setup_pay, row).json()
    assert body["check"] == "manual" and body["status"] == "需人工处理"
    assert body["passStartsAt"] is None
    session.expire_all()
    return session.get(PayOrder, row.id)


def test_paid_but_not_opened_order_can_refund_itself(setup_pay, session):  # noqa: F811
    """付了款没开通（转了人工）：不用等人工，用户自己申请全额原路退回，不受 7 天限制。"""
    row = undelivered(setup_pay, session)
    row.alerted_at = NOW  # 负责人已收到过这张单的告警
    # 付款早在 7 天以前也能退：什么都没用上
    row.paid_time = NOW - timedelta(days=30)
    session.commit()
    listed = setup_pay[0].get("/api/v1/pay/orders", headers=HEADERS).json()["items"][0]
    assert listed["canRefund"] is True and listed["refundUnavailableReason"] is None
    response = pay_refund(setup_pay, row)
    assert response.status_code == 200
    assert response.json()["status"] == "退款中"
    refunds = calls(setup_pay, "/xpay/refund_order")
    assert len(refunds) == 1
    sent = refunds[0][1]
    assert sent["refund_fee"] == 100 and sent["left_fee"] == 100
    assert sent["req_from"] == "2" and sent["refund_reason"] == "3"
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.refund_source == payments.SELF_REFUND and row.refund_fee == 100
    # 重复点：返回同一笔退款，不重建退款单
    assert pay_refund(setup_pay, row).json()["status"] == "退款中"
    assert len(calls(setup_pay, "/xpay/refund_order")) == 1


def test_undelivered_refund_that_falls_back_to_staff_alerts_again(setup_pay, session):  # noqa: F811
    """申请退款时微信那边的余额对不上：退款不发起、转回人工，并重新告警（负责人要知道用户申请过退款）。"""
    row = undelivered(setup_pay, session)
    row.alerted_at = NOW
    session.commit()
    fake = setup_pay[2]
    fake.left_fee = 50
    assert pay_refund(setup_pay, row).status_code == 200
    assert not calls(setup_pay, "/xpay/refund_order")
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "manual" and row.alerted_at is None


def test_manual_order_that_was_opened_still_needs_staff(setup_pay, session):  # noqa: F811
    """开通过又出问题（通知发货一直失败转了人工）：用过没用过要人工看，不能自助退。"""
    row = delivered(setup_pay, session)
    row.status, row.error = "manual", "通知发货失败 20 次，已停止重试，需人工处理"
    session.commit()
    body = setup_pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert body["canRefund"] is False
    assert body["refundUnavailableReason"] == "订单当前状态不能自助退款，可点“遇到问题”告诉我们"
    assert pay_refund(setup_pay, row).status_code == 422


def test_me_carries_the_pass_for_the_badge(setup_pay, session):  # noqa: F811
    """我的页头的批量工具标识：/me 带上使用期（买的、送的都算，不看在不在卖）。"""
    client, settings, _ = setup_pay
    assert client.get("/api/v1/me", headers=HEADERS).json()["pass"] == {
        "active": False,
        "expiresAt": None,
    }
    delivered(setup_pay, session)
    settings.pay_enabled = False  # 停卖了，已买的使用期照样显示
    body = client.get("/api/v1/me", headers=HEADERS).json()
    assert body["pass"]["active"] is True
    assert body["pass"]["expiresAt"].startswith("2026-11-03")
    # 还没有用户记录的人：没有使用期
    assert (
        client.get("/api/v1/me", headers={**HEADERS, "X-WX-OPENID": "new"}).json()["pass"] is None
    )


def test_restricted_api_account_can_check_and_open(setup_pay, restricted_api_factory, monkeypatch):  # noqa: F811
    """主动核对跑在接口服务上：按部署文件授权的账号开通、记查单、通知发货都不缺权限。"""
    from youju import access_log

    with restricted_api_factory() as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        with limited() as session:
            row = order(session, create(setup_pay))
            setup_pay[2].status = 2
            assert check(setup_pay, row).json()["check"] == "paid"
            session.expire_all()
            assert session.get(PayOrder, row.id).goods_notified
            assert session.scalar(select(func.count(PayEvent.id))) >= 1
