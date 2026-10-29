"""全部微信请求使用 MockTransport；禁止访问真实微信。

签名向量来源：微信官方 virtual-payment.html 的 2.6 Python 示例，
2026-10-04 取回原文。这里的密钥和 session_key 均为文档公开示例。
"""

import json
import logging
import os
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event as sa_event
from sqlalchemy import func, select

from youju import advisor_tasks, payments, xpay
from youju.api import advisor, me, pay, v1
from youju.api import app as app_module
from youju.cli import main
from youju.config import Settings
from youju.db import get_session
from youju.models import (
    AdvisorTask,
    AdvisorUsage,
    PayEvent,
    PayOrder,
    PayReconciliation,
    User,
    WorkbenchPass,
)
from youju.xpay import XPayClient, XPayError, pay_signature, signature, wx_http

NOW = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
# 测试人员在体验版里（D-074：沙箱、测试名单只在开发版、体验版），代码包里的付费协议版本号是 pay-test
HEADERS = {
    "X-WX-SOURCE": "test-cloud",
    "X-WX-OPENID": "pay-user",
    "X-Youju-Env": "trial",
    "X-Youju-Pay-Terms": "pay-test",
}


class FakeWechat:
    def __init__(self, settings):
        self.settings = settings
        self.status = 1
        self.fee = 100
        self.order_type = 0
        self.left_fee = 100
        self.calls = []
        self.fail_refund = False
        self.wrong_owner = False
        self.bill_pending = False
        self.bad_order = False
        self.paid_time = int(NOW.timestamp())
        self.fail_provide = False
        self.bill_broken = None
        self.env_type = None
        # 查单返回的商户单号、交易单号（沙箱可能是空的）
        self.channel_ids = ("mch-test", "tx-test")
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.client = XPayClient(settings, self.http)

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body, str(request.url)))
        if path.startswith("/xpay/"):
            assert "access_token" not in request.url.params
            assert request.url.params["pay_sig"] == pay_signature(
                "12345", path, request.content.decode()
            )
        if path == "/sns/jscode2session":
            return httpx.Response(
                200,
                json={
                    "openid": "other" if self.wrong_owner else "pay-user",
                    "session_key": "9hAb/NEYUlkaMBEsmFgzig==",
                },
            )
        if path == "/xpay/query_order":
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "order": {
                        "order_id": "wrong" if self.bad_order else body["order_id"],
                        "openid": body["openid"],
                        "status": self.status,
                        # 官方文档：返回的 env_type 是 1 现网、2 沙箱（请求的 env 是 0、1）
                        "env_type": body["env"] + 1 if self.env_type is None else self.env_type,
                        "order_fee": self.fee,
                        "paid_fee": self.fee,
                        "left_fee": self.left_fee,
                        "paid_time": self.paid_time,
                        "wx_order_id": "wx-" + body["order_id"],
                        "order_type": self.order_type,
                        "channel_order_id": self.channel_ids[0],
                        "wxpay_order_id": self.channel_ids[1],
                    },
                },
            )
        if path == "/xpay/refund_order":
            return httpx.Response(200, json={"errcode": -1 if self.fail_refund else 0})
        if path == "/xpay/notify_provide_goods":
            return httpx.Response(200, json={"errcode": -1 if self.fail_provide else 0})
        if path == "/xpay/download_bill":
            if self.bill_pending:
                return httpx.Response(200, json={"errcode": 268490011})
            return httpx.Response(200, json={"errcode": 0, "url": "https://bill.example/file.csv"})
        if request.url.host == "bill.example":
            if self.bill_broken == "download":
                return httpx.Response(500)
            if self.bill_broken == "parse":
                return httpx.Response(200, content=b"unknown\nnot-a-bill\n")
            return httpx.Response(200, content=b"trade,fee\nextra,100\n")
        raise AssertionError("不得发起其他 HTTP 请求")


@pytest.fixture
def setup_pay(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        pay_enabled=True,
        # 这些用例测售卖本身：对全体开放；测试名单另见 test_invoices。
        pay_audience="all",
        pay_openids="pay-user",
        advisor_enabled=True,
        trust_wx_headers=True,
        xpay_offer_id="offer-test",
        # 新订单一律现网（沙箱已去掉，创始人 10/29）：现网 AppKey、定稿协议版本、发票类型都配了才卖
        xpay_appkey_prod="12345",
        # 去掉沙箱以前下的测试单（订单上记的 env 是 1）查单、退款用
        xpay_appkey_sandbox="12345",
        pay_terms_version="pay-test",
        pay_invoice_type="电子普通发票（测试）",
        xpay_price_pass=100,
        agreement_version="2026-11",
        xpay_bill_order_column="trade",
        xpay_bill_fee_column="fee",
        xpay_bill_hosts=["bill.example"],
    )
    fake = FakeWechat(settings)
    monkeypatch.setattr(pay, "get_settings", lambda: settings)
    monkeypatch.setattr(advisor, "get_settings", lambda: settings)
    monkeypatch.setattr(me, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "now", lambda: NOW)

    def override():
        with session_factory() as session:
            yield session

    app_module.app.dependency_overrides[get_session] = override
    app_module.app.dependency_overrides[pay.wx_client] = lambda: fake.client
    with TestClient(app_module.app) as client:
        client.post("/api/v1/me/agreement", headers=HEADERS, json={"version": "2026-11"})
        with session_factory() as session:
            user = session.scalar(select(User))
            user.advisor_agreement_version = advisor_tasks.ADVISOR_AGREEMENT_VERSION
            user.advisor_agreement_at = NOW
            session.commit()
        yield client, settings, fake
    app_module.app.dependency_overrides.clear()
    fake.http.close()


def create(setup_pay, **values):
    client, settings, _ = setup_pay
    return client.post(
        "/api/v1/pay/orders",
        headers=HEADERS,
        json={
            "product_id": "workbench_30d",
            "code": "test-login",
            "terms_version": "pay-test",
            # 购买页带上它显示的发票类型（现网要和配置一致，D-070）
            "invoice_type": payments.invoice_type(settings),
            **values,
        },
    )


def order(session, response):
    return session.get(PayOrder, response.json()["orderId"])


def payload(order):
    return {
        "Event": "xpay_goods_deliver_notify",
        "OpenId": "pay-user",
        "OutTradeNo": order.out_trade_no,
        "Env": 0,
        "WeChatPayInfo": {
            "MchOrderNo": "mch-test",
            "TransactionId": "tx-test",
            "PaidTime": int(NOW.timestamp()),
        },
        "GoodsInfo": {
            "ProductId": "workbench_30d",
            "Quantity": 1,
            "OrigPrice": 100,
            "ActualPrice": 100,
            "Attach": order.out_trade_no,
        },
    }


def push(setup_pay, data):
    return setup_pay[0].post("/api/wx/push", headers=HEADERS, json=data)


def collect(alerts):
    """告警记下来、当成发出去了（alert 返回 True 才算发出去；返回 None 是没配告警地址，下一轮接着发）。"""
    return lambda text: alerts.append(text) or True


def run(session, setup_pay, stamp=NOW):
    _, settings, fake = setup_pay
    return payments.run_payments(
        session, settings=settings, client=fake.client, stamp=stamp, alert=lambda _: True
    )


def delivered(setup_pay, session):
    response = create(setup_pay)
    assert response.status_code == 201
    row = order(session, response)
    setup_pay[2].status = 2
    assert push(setup_pay, payload(row)).json()["ErrCode"] == 0
    session.expire_all()
    return session.get(PayOrder, row.id)


def test_official_signature_vectors():
    raw = '{"openid": "xxx", "user_ip": "127.0.0.1", "env": 0}'
    assert pay_signature("12345", "/xpay/query_user_balance", raw) == (
        "c37809f27c6d7fd1837ad2500a04512b66b34fd793a39a385fade56dca89a4b5"
    )
    assert signature("9hAb/NEYUlkaMBEsmFgzig==", raw) == (
        "089d9e8dc5d308977360c4b79ec600a93d736802802a807d634192328032f6c7"
    )


def test_defaults_and_env_only_secrets(tmp_path, monkeypatch):
    config = Settings(_env_file=None)
    # 没有沙箱开关了（创始人 10/29）：线上还留着 YOUJU_XPAY_ENV 也照常启动，不起作用
    assert not config.pay_enabled and not hasattr(config, "xpay_env")
    monkeypatch.setenv("YOUJU_XPAY_ENV", "1")
    assert not hasattr(Settings(_env_file=None), "xpay_env")
    assert not payments.products(config)
    assert config.xpay_product_pass == "workbench_30d"
    monkeypatch.setenv("YOUJU_XPAY_PRICE_PASS", "")
    assert not payments.products(Settings(_env_file=None))
    path = tmp_path / "payment.env"
    path.write_text(
        "YOUJU_WX_APPSECRET=dotenv-test\nYOUJU_XPAY_APPKEY_SANDBOX=dotenv-test\n"
        "YOUJU_XPAY_APPKEY_PROD=dotenv-test\n"
    )
    assert Settings(_env_file=path).wx_appsecret is None
    assert Settings(_env_file=path).xpay_appkey_sandbox is None
    assert Settings(_env_file=path).xpay_appkey_prod is None
    monkeypatch.setenv("YOUJU_WX_APPSECRET", "environment-test")
    assert Settings(_env_file=path).wx_appsecret.get_secret_value() == "environment-test"


def test_create_signatures_snapshot_and_rate_limit(setup_pay, session):
    response = create(setup_pay)
    assert response.status_code == 201
    data = response.json()
    signed = json.loads(data["signData"])
    row = order(session, response)
    assert signed == {
        "offerId": "offer-test",
        "buyQuantity": 1,
        # 现网（沙箱已去掉，创始人 10/29）
        "env": 0,
        "currencyType": "CNY",
        "productId": "workbench_30d",
        "goodsPrice": 100,
        "outTradeNo": row.out_trade_no,
        "attach": row.out_trade_no,
    }
    assert data["paySig"] == pay_signature("12345", "requestVirtualPayment", data["signData"])
    assert data["signature"] == signature("9hAb/NEYUlkaMBEsmFgzig==", data["signData"])
    assert row.price == 100 and row.env == 0
    assert create(setup_pay).status_code == 201
    assert create(setup_pay).status_code == 201
    assert create(setup_pay).status_code == 429


@pytest.mark.parametrize(
    ("env", "env_type", "ok"),
    [
        (1, 2, True),  # 官方文档写法：沙箱 2
        (1, 1, True),  # 和请求一样的写法也认
        (1, 0, False),  # 0 只能是现网（请求写法）
        (0, 1, True),  # 官方文档写法：现网 1
        (0, 0, True),
        (0, 2, False),  # 2 只能是沙箱（官方写法）
    ],
)
def test_query_accepts_both_env_encodings_rejects_other_env(setup_pay, session, env, env_type, ok):
    """查单返回的 env_type 官方写的是 1 现网、2 沙箱，和请求的 env（0、1）不一样：两种都认，另一个环境的拒绝。
    新订单一律现网；env 1 是去掉沙箱以前下的测试单，照订单上记的环境查。"""
    _, _, fake = setup_pay
    row = order(session, create(setup_pay))
    row.env = env
    session.commit()
    fake.env_type = env_type
    if ok:
        assert payments.query(session, row, fake.client)["order_id"] == row.out_trade_no
    else:
        with pytest.raises(XPayError):
            payments.query(session, row, fake.client)


def test_new_order_first_checks_recent_pending_payment(setup_pay, session):
    """下新单前先问微信 10 分钟内的待支付单：已经付了款（推送和补单还没到；小程序取消回调不准、
    付款中途被关掉时本机没记下这一笔）就先开通、不下新单；没付照常下（agy 审阅，10/16）。
    查不了的挡新单（最多 10 分钟）：以前放行，旧单其实付了款时会重复扣款（Codex 10/29）；
    小程序报告过"付款前就被微信拒了"的单当场关掉，不再挡。"""
    _, _, fake = setup_pay
    first = order(session, create(setup_pay))
    fake.calls.clear()
    # 没付：照常下新单，只查了上一笔
    second = order(session, create(setup_pay))
    assert [c[0] for c in fake.calls].count("/xpay/query_order") == 1
    # 微信说已关闭：两笔都关掉，照常下新单
    fake.status = 6
    third = order(session, create(setup_pay))
    session.expire_all()
    assert session.get(PayOrder, first.id).status == "closed"
    assert session.get(PayOrder, second.id).status == "closed"
    # 已付：开通上一笔、不下新单
    fake.status = 2
    response = create(setup_pay)
    assert response.status_code == 409 and "上一笔已经付款" in response.json()["detail"]
    session.expire_all()
    assert session.get(PayOrder, third.id).status == "completed"
    assert session.scalar(select(WorkbenchPass).where(WorkbenchPass.order_id == third.id))
    assert session.scalar(select(func.count(PayOrder.id))) == 3
    # 付款核验不过（金额不符）：转人工，也不下新单
    fake.status = 1
    fourth = order(session, create(setup_pay))
    fake.status, fake.fee = 2, 99
    response = create(setup_pay)
    assert response.status_code == 409 and "人工核对" in response.json()["detail"]
    session.expire_all()
    assert session.get(PayOrder, fourth.id).status == "manual"
    # 微信查不了（比如下单回应没送到手机、这笔没调起过支付）：挡新单，上一笔留给补单
    fake.status, fake.fee = 1, 100
    fifth = order(session, create(setup_pay))
    fake.bad_order = True
    response = create(setup_pay)
    assert response.status_code == 409 and "查不到结果" in response.json()["detail"]
    session.expire_all()
    assert session.get(PayOrder, fifth.id).status == "pending"
    assert session.scalar(select(func.count(PayOrder.id))) == 5
    # 查单返回格式不对（unavailable）也一样挡
    fake.bad_order, fake.status = False, "x"
    response = create(setup_pay)
    assert response.status_code == 409 and "查不到结果" in response.json()["detail"]
    fake.bad_order, fake.status = True, 1
    # 小程序下一次付款时带上"上一笔付款前就被微信拒了（道具没发布到现网）"：那一单记一笔、照旧待确认，
    # 查不清时不再挡新单（服务端不凭这一句关单，补单照旧跟踪）
    sixth = order(session, create(setup_pay, failed_order={"order_id": fifth.id, "code": -15010}))
    session.expire_all()
    row = session.get(PayOrder, fifth.id)
    assert row.status == "pending" and row.error.startswith(payments.NOT_STARTED_PREFIX)
    # 超过 10 分钟的待支付单不在下单时查（交给每分钟的补单）
    fake.bad_order = False
    for row in (fifth, sixth):
        session.get(PayOrder, row.id).created_at = NOW - timedelta(minutes=11)
    session.commit()
    fake.calls.clear()
    assert create(setup_pay).status_code == 201
    assert "/xpay/query_order" not in [c[0] for c in fake.calls]


def test_failed_order_note_only_marks_own_recent_pending_orders_with_known_codes(
    setup_pay, session
):
    """下一次付款带上的"上一笔付款前就被微信拒了"，只认本人 10 分钟内的待确认单和官方表里下单阶段的错误码；
    记上以后订单照旧待确认、补单照旧查，查单出错不盖掉这一句，查到付了款照样开通（Codex 第 1 轮）。"""
    _, _, fake = setup_pay
    first = order(session, create(setup_pay))
    for code in (-1, -5, -15003, -15015, 0):
        assert not payments.mark_not_started(session, first.user_id, first.id, code, NOW), code
    assert not payments.mark_not_started(session, first.user_id + 999, first.id, -15010, NOW)
    first.status = "closed"
    session.commit()
    assert not payments.mark_not_started(session, first.user_id, first.id, -15010, NOW)
    first.status, first.created_at = "pending", NOW - timedelta(minutes=11)
    session.commit()
    assert not payments.mark_not_started(session, first.user_id, first.id, -15010, NOW)
    session.commit()
    assert session.get(PayOrder, first.id).error is None
    # 经下单接口带上：本人 10 分钟内的待确认单记上
    second = order(session, create(setup_pay))
    assert (
        create(setup_pay, failed_order={"order_id": second.id, "code": -15010}).status_code == 201
    )
    session.expire_all()
    row = session.get(PayOrder, second.id)
    assert row.status == "pending" and row.error.startswith(payments.NOT_STARTED_PREFIX)
    # 补单查单出错：这一句留着；查到付了款：照样开通
    row.created_at, row.query_retry_at = NOW - timedelta(minutes=5), None
    session.commit()
    fake.bad_order = True
    run(session, setup_pay)
    session.expire_all()
    row = session.get(PayOrder, second.id)
    assert row.status == "pending" and row.error.startswith(payments.NOT_STARTED_PREFIX)
    fake.bad_order, fake.status = False, 2
    row.query_retry_at = None
    session.commit()
    run(session, setup_pay)
    session.expire_all()
    assert session.get(PayOrder, second.id).status == "completed"
    # 48 小时都查不到的：照旧关单告警，告警里注明小程序报告过付款前就被拒（独立审阅 #402）
    fake.status = 1  # 前面那张待确认的单微信说还没付：不挡新单
    third = order(session, create(setup_pay))
    assert payments.mark_not_started(session, third.user_id, third.id, -15010, NOW)
    third.created_at, third.query_retry_at = NOW - timedelta(hours=49), None
    session.commit()
    fake.bad_order = True
    alerts = []
    payments.run_payments(
        session, settings=setup_pay[1], client=fake.client, stamp=NOW, alert=collect(alerts)
    )
    session.expire_all()
    assert session.get(PayOrder, third.id).status == "closed"
    assert any("小程序报告过付款前就被微信拒了" in a for a in alerts)


def test_orders_reported_as_never_started_do_not_count_toward_three_pending(setup_pay, session):
    """10 分钟内最多 3 笔待支付；小程序报告过付款前就被拒的单不算在这 3 笔里（连着被拒也能接着付），
    但连这些一起最多 10 笔（Codex #402 第 2 轮）。"""
    _, _, fake = setup_pay
    fake.bad_order = True  # 微信那边查不到这些单（付款前就被拒了）
    previous = order(session, create(setup_pay))
    for _ in range(8):
        response = create(setup_pay, failed_order={"order_id": previous.id, "code": -15010})
        assert response.status_code == 201
        previous = order(session, response)
    # 8 笔被标记 + 最新这一笔没标：第 10 笔还能下；10 笔都标上了，第 11 笔挡（连这些一起最多 10 笔）
    tenth = create(setup_pay, failed_order={"order_id": previous.id, "code": -15010})
    assert tenth.status_code == 201, tenth.json()
    tenth_id = order(session, tenth).id
    eleventh = create(setup_pay, failed_order={"order_id": tenth_id, "code": -15010})
    assert eleventh.status_code == 429, eleventh.json()
    # 被 429 挡回也存下了这一句（换台手机再付不会被第 10 笔挡，Codex #402 第 3 轮）
    session.expire_all()
    assert session.get(PayOrder, tenth_id).error.startswith(payments.NOT_STARTED_PREFIX)
    session.expire_all()
    assert session.scalar(select(func.count(PayOrder.id))) == 10


def test_failed_order_note_is_kept_when_the_new_order_fails(setup_pay, session):
    """带上来的"上一笔付款前就被拒"先单独存下：这一次下单失败（换会话密钥失败 503）回滚也不丢。"""
    _, _, fake = setup_pay
    first = order(session, create(setup_pay))
    fake.wrong_owner = True
    response = create(setup_pay, failed_order={"order_id": first.id, "code": -15014})
    assert response.status_code == 503
    session.expire_all()
    row = session.get(PayOrder, first.id)
    assert row.status == "pending" and row.error.startswith(payments.NOT_STARTED_PREFIX)
    assert session.scalar(select(func.count(PayOrder.id))) == 1


@pytest.mark.parametrize("status", ["refunding", "refunded", "closed"])
def test_complaint_on_refunding_refunded_or_closed_order_keeps_status_and_alerts_once(
    setup_pay, session, status
):
    """投诉时订单退款中、已退款、已关闭：不改状态（转人工会让退款中的单停掉自动退款、已退款的单倒退成
    "人工核对中"），这条投诉单独告警一次；没发出去（返回 False）、没配告警地址（None）的下一轮接着发；
    之后订单状态变了也照发（收到推送时就定好，Codex 第 1 轮）。"""
    _, settings, fake = setup_pay
    row = delivered(setup_pay, session)
    row.status = status
    if status != "closed":
        row.refund_status = "pending" if status == "refunding" else "completed"
    session.commit()
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    complaint = {**base, "Event": "xpay_complaint_notify", "ComplaintId": "c-1"}
    assert push(setup_pay, complaint).json()["ErrCode"] == 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == status
    # 补单发告警之前订单变成了已完成：照样发
    row.status = "completed"
    session.commit()
    settings.pay_enabled = False
    assert payments.has_work(session, settings) is True
    sent = []
    answers = iter([False, None, True])

    def flaky(message):
        sent.append(message)
        return next(answers)

    for minute in range(4):
        payments.complaint_alerts(session, NOW + timedelta(minutes=minute), flaky)
    assert len(sent) == 3 and sent[2].startswith(f"支付订单 #{row.id}（订单")
    assert payments.COMPLAINT_ERROR in sent[2]
    event = session.scalar(select(PayEvent).where(PayEvent.kind == "xpay_complaint_notify"))
    assert event.summary["alert"] == "sent"


@pytest.mark.parametrize("case", ["pending", "closed_unconfirmed", "unknown"])
def test_complaint_on_order_without_wechat_order_id_is_accepted_and_alerted(
    setup_pay, session, case
):
    """本地还没有微信单号的单（没开通的待确认单、48 小时查不到付款结果关掉的单）收到投诉：不比微信单号，
    照样记下、告警——这正是"扣了钱没开通"最该处理的投诉；本地查不到订单的投诉也告警（独立审阅 #402）。"""
    _, settings, fake = setup_pay
    row = order(session, create(setup_pay))
    if case == "closed_unconfirmed":
        row.status, row.error = "closed", payments.UNCONFIRMED_PAYMENT_ERROR
        session.commit()
    complaint = {
        "Event": "xpay_complaint_notify",
        "OpenId": "pay-user",
        "MchOrderId": "not-ours" if case == "unknown" else row.out_trade_no,
        "WxOrderId": "wx-from-wechat",
        "ComplaintId": "c-9",
    }
    assert push(setup_pay, complaint).json()["ErrCode"] == 0
    session.expire_all()
    current = session.get(PayOrder, row.id)
    alerts = []
    payments.run_payments(
        session, settings=settings, client=fake.client, stamp=NOW, alert=collect(alerts)
    )
    if case == "pending":
        # 待确认的单照旧转人工，由转人工的告警负责
        assert current.status == "manual" and payments.COMPLAINT_ERROR in current.error
        assert len(alerts) == 1 and "需人工处理" in alerts[0]
    elif case == "closed_unconfirmed":
        assert current.status == "closed"
        assert any(f"支付订单 #{row.id}（订单已关闭）" in a for a in alerts)
    else:
        assert current.status == "pending"
        assert any("商户单号 not-ours（本地没有这笔订单）" in a for a in alerts)
        # 同一个投诉重推：不再告警（Codex #402 第 4 轮）
        assert push(setup_pay, complaint).json()["ErrCode"] == 0
        more = []
        payments.run_payments(
            session,
            settings=settings,
            client=fake.client,
            stamp=NOW + timedelta(minutes=1),
            alert=collect(more),
        )
        assert not any("本地没有这笔订单" in a for a in more)
        kinds = [
            e.summary["alert"]
            for e in session.scalars(
                select(PayEvent)
                .where(PayEvent.kind == "xpay_complaint_notify", PayEvent.order_id.is_(None))
                .order_by(PayEvent.id)
            )
        ]
        assert kinds == ["sent", "duplicate"]


def test_same_complaint_arriving_twice_at_once_alerts_once(setup_pay, session):
    """本地没有订单的同一投诉两次推送同时到，收推送时都没看到对方、都记成 pending：
    发告警时按事件编号再判一次，只发最早那一条（Codex #402 第 5 轮）。"""
    summary = {"MchOrderId": "not-ours", "ComplaintId": "c-7", "alert": "pending"}
    session.add_all(
        [
            PayEvent(order_id=None, kind="xpay_complaint_notify", summary=dict(summary)),
            PayEvent(order_id=None, kind="xpay_complaint_notify", summary=dict(summary)),
            # 别的投诉照常发
            PayEvent(
                order_id=None,
                kind="xpay_complaint_notify",
                summary={**summary, "ComplaintId": "c-8"},
            ),
        ]
    )
    session.commit()
    alerts = []
    assert payments.complaint_alerts(session, NOW, collect(alerts)) == 2
    states = [e.summary["alert"] for e in session.scalars(select(PayEvent).order_by(PayEvent.id))]
    assert states == ["sent", "duplicate", "sent"]


def test_complaint_on_completed_order_still_goes_manual_without_a_second_alert(setup_pay, session):
    """已完成的单收到投诉照旧转人工、由转人工的告警负责（事件记 alert=manual，不单独发）；
    同一个投诉重推只记事件（duplicate）；人工单里已经有投诉原因时又来一个新投诉，单独告警一次。"""
    _, settings, fake = setup_pay
    row = delivered(setup_pay, session)
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    first = {**base, "Event": "xpay_complaint_notify", "ComplaintId": "c-1"}
    assert push(setup_pay, first).json()["ErrCode"] == 0
    assert push(setup_pay, first).json()["ErrCode"] == 0
    alerts = []
    for minute in range(2):
        payments.run_payments(
            session,
            settings=settings,
            client=fake.client,
            stamp=NOW + timedelta(minutes=minute),
            alert=collect(alerts),
        )
    assert len(alerts) == 1 and "需人工处理" in alerts[0]
    kinds = [
        e.summary["alert"]
        for e in session.scalars(
            select(PayEvent).where(PayEvent.kind == "xpay_complaint_notify").order_by(PayEvent.id)
        )
    ]
    assert kinds == ["manual", "duplicate"]
    second = {**base, "Event": "xpay_complaint_notify", "ComplaintId": "c-2"}
    assert push(setup_pay, second).json()["ErrCode"] == 0
    payments.run_payments(
        session,
        settings=settings,
        client=fake.client,
        stamp=NOW + timedelta(minutes=5),
        alert=collect(alerts),
    )
    assert len(alerts) == 2 and payments.COMPLAINT_ERROR in alerts[1]


def test_new_order_locks_previous_orders_by_primary_key_only(setup_pay, session):
    """下单时对订单表只按主键加锁，不做范围锁：范围锁加插入会让两个新用户同时下单时互相等待甚至死锁。"""
    first = order(session, create(setup_pay))
    statements = []

    def record(_conn, _cursor, statement, *_):
        statements.append(" ".join(statement.split()))

    engine = session.get_bind()
    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        assert create(setup_pay).status_code == 201
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)
    locked = [s for s in statements if "FROM pay_orders" in s and "FOR UPDATE" in s]
    assert locked, "上一笔待支付单应当按主键加锁后再查微信"
    assert all(" WHERE pay_orders.id = " in s and " AND " not in s for s in locked), locked
    assert first.id


@pytest.mark.parametrize(
    "values",
    [
        {"rows": []},
        {"rows": [" "]},
        {"rows": [3]},
        {"rows": ["公司"] * 101},
        {"product_id": "unknown"},
        {"code": ""},
    ],
)
def test_invalid_order_input(setup_pay, values):
    assert create(setup_pay, **values).status_code in (404, 422)


def test_consent_and_unconfigured_price(setup_pay, session):
    user = session.scalar(select(User))
    user.advisor_agreement_at = None
    session.commit()
    assert create(setup_pay).status_code == 403
    user.advisor_agreement_at = NOW
    session.commit()
    setup_pay[1].xpay_price_pass = None
    assert create(setup_pay).status_code == 404


def test_products_lists_only_priced_items(setup_pay, session):
    client, settings, fake = setup_pay
    assert client.get("/api/v1/pay/products", headers=HEADERS).json() == {
        "items": [
            {"productId": "workbench_30d", "name": "批量工具 30 天", "price": 100, "days": 30}
        ],
        # 续买多送开关默认关：付款页不提
        "renewBonus": None,
        "invoiceType": "电子普通发票（测试）",
    }
    assert session.scalar(select(func.count(PayOrder.id))) == 0
    # 没有沙箱了：商品接口不再带 sandbox（创始人 10/29）
    closed = {"items": [], "renewBonus": None, "invoiceType": None}
    settings.xpay_price_pass = None
    assert client.get("/api/v1/pay/products", headers=HEADERS).json() == closed
    settings.xpay_price_pass = 100
    settings.xpay_offer_id = ""
    assert client.get("/api/v1/pay/products", headers=HEADERS).json() == closed
    assert create(setup_pay).status_code == 404
    assert not fake.calls
    settings.xpay_offer_id = "offer-test"
    # 没配现网 AppKey（或只有空格）：下单签不了名，价格也不显示，不出现看得到价格却付不了款（Codex 10/29）
    for key in (None, SecretStr(""), SecretStr("  ")):
        settings.xpay_appkey_prod = key
        assert client.get("/api/v1/pay/products", headers=HEADERS).json() == closed
        assert create(setup_pay).status_code == 404
    settings.xpay_appkey_prod = SecretStr("12345")
    assert not fake.calls
    # 没配定稿的协议版本时谁也不卖（D-074 只对带定稿协议版本号的包开放）
    settings.pay_terms_version = ""
    assert client.get("/api/v1/pay/products", headers=HEADERS).status_code == 404
    assert create(setup_pay).status_code == 404
    assert not fake.calls
    settings.pay_terms_version = "pay-test"
    # 还要写明能开哪种发票（D-070 付款前要看得到）；写成占位也不算
    for invoice in ("", "【发票类型】", "待确认"):
        settings.pay_invoice_type = invoice
        assert client.get("/api/v1/pay/products", headers=HEADERS).json() == closed
        assert create(setup_pay).status_code == 404
    settings.pay_invoice_type = "电子普通发票（测试）"
    listed = client.get("/api/v1/pay/products", headers=HEADERS).json()
    assert "sandbox" not in listed and [i["productId"] for i in listed["items"]] == [
        "workbench_30d"
    ]
    assert listed["invoiceType"] == "电子普通发票（测试）"


def test_products_route_dependency_hides_when_closed(setup_pay):
    settings = setup_pay[1]
    settings.pay_enabled = False
    with pytest.raises(pay.HTTPException) as caught:
        # 只看请求头里的 openid（测试名单用），这里给一个最小的请求对象
        pay.require_enabled(SimpleNamespace(headers=HEADERS))
    assert caught.value.status_code == 404 and caught.value.detail == "没有找到接口"
    route = next(r for r in pay.router.routes if r.path == "/api/v1/pay/products")
    assert route.methods == {"GET"}
    assert any(d.dependency is pay.require_enabled for d in route.dependencies)


def test_login_identity_must_match(setup_pay, session):
    setup_pay[2].wrong_owner = True
    assert create(setup_pay).status_code == 503
    assert session.scalar(select(func.count(PayOrder.id))) == 0


def test_push_15_times_one_pass_no_task_or_quota(setup_pay, session):
    row = delivered(setup_pay, session)
    for _ in range(14):
        assert push(setup_pay, payload(row)).json()["ErrCode"] == 0
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 1
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    assert session.scalar(select(func.count(AdvisorUsage.id))) == 0
    ticket = payments.order_pass(session, row)
    assert ticket.starts_at == NOW and ticket.ends_at == NOW + timedelta(days=30)
    assert row.status == "completed" and row.goods_notified


@pytest.mark.parametrize("mismatch", ["price", "owner", "env", "trade", "unpaid"])
def test_push_requires_matching_query(setup_pay, session, mismatch):
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status = 2
    data = payload(row)
    if mismatch == "price":
        fake.fee = 101
    elif mismatch == "owner":
        data["OpenId"] = "wrong"
    elif mismatch == "env":
        # 新订单是现网（0）；推送说沙箱（1）就对不上
        data["Env"] = 1
    elif mismatch == "trade":
        fake.bad_order = True
    elif mismatch == "unpaid":
        fake.status = 1
    assert push(setup_pay, data).json()["ErrCode"] != 0
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 0


def pay_logs(caplog):
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("youju"))


@pytest.mark.parametrize(
    "change,field",
    [
        ("product", "GoodsInfo.ProductId（值不同）"),
        ("quantity", "GoodsInfo.Quantity（值不同）"),
        ("attach", "GoodsInfo.Attach（推送里是空的）"),
        ("no_goods", "GoodsInfo（推送里没有）"),
        ("wx", "WeChatPayInfo.TransactionId（值不同）"),
        ("mch_empty", "WeChatPayInfo.MchOrderNo（查单里是空的）"),
    ],
)
def test_push_fields_unlike_query_still_open_what_was_paid(
    setup_pay, session, caplog, change, field
):
    """开不开通以查单为准：推送里的道具、单号字段和查单对不上也不拒收——拒收只让微信重推，
    2 分钟后补单照样开通（10/19 沙箱实测）。照订单开通，只把对不上的字段名记进日志。"""
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status = 2
    data = payload(row)
    if change == "product":
        data["GoodsInfo"]["ProductId"] = "workbench_90d"
    elif change == "quantity":
        data["GoodsInfo"]["Quantity"] = 2
    elif change == "attach":
        data["GoodsInfo"]["Attach"] = ""
    elif change == "no_goods":
        del data["GoodsInfo"]
    elif change == "wx":
        data["WeChatPayInfo"]["TransactionId"] = "wrong"
    elif change == "mch_empty":
        fake.channel_ids = ("", "")
    caplog.set_level(logging.WARNING, logger="youju")
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    ticket = payments.order_pass(session, row)
    assert row.status == "completed" and row.goods_notified and row.error is None
    assert ticket.ends_at - ticket.starts_at == timedelta(days=30)
    text = pay_logs(caplog)
    assert f"订单 #{row.id} 推送和查单对不上（以查单为准）" in text and field in text
    for value in (row.out_trade_no, "workbench_90d", "wrong", "mch-test", "tx-test", "pay-user"):
        assert value not in text
    # 同一笔重推：照样应答成功，不重复开通
    assert push(setup_pay, data).json()["ErrCode"] == 0
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 1


def test_push_matching_query_logs_nothing(setup_pay, session, caplog):
    caplog.set_level(logging.WARNING, logger="youju")
    delivered(setup_pay, session)
    assert pay_logs(caplog) == ""


def test_push_not_handled_logs_reason_without_ids(setup_pay, session, caplog):
    """推送没处理成，原因记进日志：事件名、订单号、状态码，不记 openid、单号和金额。"""
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    data = payload(row)
    caplog.set_level(logging.WARNING, logger="youju")
    assert push(setup_pay, data).json()["ErrCode"] == 1  # 还没付款
    fake.status, fake.bad_order = 2, True
    assert push(setup_pay, data).json()["ErrCode"] == 1  # 查单返回的单号对不上
    fake.bad_order = False
    assert push(setup_pay, {**data, "OpenId": "someone-else"}).json()["ErrCode"] == 1
    text = pay_logs(caplog)
    tag = f"xpay_goods_deliver_notify 订单 #{row.id}"
    assert f"{tag} 这次没处理：查单状态 1" in text
    assert f"{tag} 核验失败：XPayError" in text
    assert f"{tag} 推送的用户、环境或微信单号和订单对不上，没处理" in text
    for value in (row.out_trade_no, "pay-user", "someone-else", "mch-test", "tx-test"):
        assert value not in text


def test_push_source_xml_and_role_gate(setup_pay, session):
    client, settings, fake = setup_pay
    row = order(session, create(setup_pay))
    assert client.post("/api/wx/push", json=payload(row)).status_code == 403
    root = ElementTree.Element("xml")

    def append(node, values):
        for key, value in values.items():
            child = ElementTree.SubElement(node, key)
            if isinstance(value, dict):
                append(child, value)
            else:
                child.text = str(value)

    append(root, payload(row))
    fake.status = 2
    settings.role = "api"
    response = client.post("/api/wx/push", headers=HEADERS, content=ElementTree.tostring(root))
    assert response.status_code == 200
    assert ElementTree.fromstring(response.content).findtext("ErrCode") == "0"


def test_poll_delivery_and_complete(setup_pay, session):
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(minutes=3)
    session.commit()
    setup_pay[2].status = 2
    run(session, setup_pay)
    assert payments.order_pass(session, row) and row.goods_notified
    response = setup_pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS)
    assert response.json()["status"] == "已完成"
    assert response.json()["canRefund"] is True


def test_refund_failure_retry_same_id_and_completion(setup_pay, session):
    row = delivered(setup_pay, session)
    fake = setup_pay[2]
    fake.fail_refund = True
    assert (
        setup_pay[0].post(f"/api/v1/pay/orders/{row.id}/refund", headers=HEADERS).status_code == 200
    )
    session.expire_all()
    assert row.refund_status == "retry"
    refund_id = row.refund_order_id
    fake.fail_refund = False
    run(session, setup_pay, NOW + timedelta(minutes=3))
    assert row.refund_status == "pending" and row.status == "refunding"
    assert row.refund_order_id == refund_id
    requests = [body for path, body, _ in fake.calls if path == "/xpay/refund_order"]
    assert len(requests) == 2 and requests[0] == requests[1]
    # 退款中的订单按 refund_retry_at 退避查单（审阅第 6 条），时间推进到到期点。
    fake.status, fake.left_fee = 7, 100
    run(session, setup_pay, advisor_tasks._utc(row.refund_retry_at))
    assert row.refund_status == "retry"
    fake.status = 2
    run(session, setup_pay, advisor_tasks._utc(row.refund_retry_at))
    assert row.refund_order_id == refund_id
    # 查单状态 5（商户侧扣款完成）：钱还没到用户那边，订单仍是退款中；8（用户侧收款完成）才算退完
    fake.status, fake.left_fee = 5, 0
    run(session, setup_pay, advisor_tasks._utc(row.refund_retry_at))
    assert row.status == "refunding" and row.refund_status == "pending"
    fake.status = 8
    run(session, setup_pay, advisor_tasks._utc(row.refund_retry_at))
    assert row.status == "refunded"


def test_reconciliation_per_order_diff_and_pending_download(setup_pay, session):
    row = delivered(setup_pay, session)
    result = payments.reconcile(session, NOW.date(), {"other": 100}, NOW)
    assert result.local_count == result.bill_count == 1
    assert result.local_fee == result.bill_fee == 100
    assert result.status == "different" and len(result.differences) == 2
    assert payments.reconcile(session, NOW.date(), {row.out_trade_no: 100}, NOW).status == "matched"
    fake = setup_pay[2]
    fake.bill_pending = True
    day = NOW.date() - timedelta(days=1)
    payments.download_reconcile(session, day, fake.client, NOW)
    report = session.scalar(select(PayReconciliation).where(PayReconciliation.day == day))
    assert report.status == "pending"
    assert payments.overview(session)["reconciliation_pending"] == 1
    fake.bill_pending = False
    run(session, setup_pay)
    assert report.status == "different" and report.bill_fee == 100
    assert payments.overview(session)["differences"] == 1


@pytest.mark.parametrize("toggle", ["pay_enabled", "advisor_enabled"])
def test_closed_all_endpoints_and_jobs(setup_pay, session, monkeypatch, toggle):
    client, settings, fake = setup_pay
    setattr(settings, toggle, False)
    for response in [
        create(setup_pay),
        client.get("/api/v1/pay/orders/1", headers=HEADERS),
        client.post("/api/v1/pay/orders", content=b"{"),
        client.get("/api/v1/pay/products", headers=HEADERS),
        client.get("/api/v1/pay/products"),
    ]:
        assert response.status_code == 404 and response.json()["detail"] == "没有找到接口"
    # 推送路径不受开关控制：应答成功但不查单、不发货（审阅 A）。
    response = push(setup_pay, {})
    assert response.status_code == 200 and response.json()["ErrCode"] == 0
    assert run(session, setup_pay) == {"processed": 0}
    monkeypatch.setattr("youju.config.get_settings", lambda: settings)
    assert main(["pay-run"]) == 0
    assert main(["pay-reconcile"]) == 0
    assert not fake.calls


def test_foreign_order_hidden(setup_pay):
    response = create(setup_pay)
    result = setup_pay[0].get(
        f"/api/v1/pay/orders/{response.json()['orderId']}",
        headers={**HEADERS, "X-WX-OPENID": "other"},
    )
    # 另一用户须已同意协议，才能验证归属 404。
    setup_pay[0].post(
        "/api/v1/me/agreement",
        json={"version": "2026-11"},
        headers={**HEADERS, "X-WX-OPENID": "other"},
    )
    result = setup_pay[0].get(
        f"/api/v1/pay/orders/{response.json()['orderId']}",
        headers={**HEADERS, "X-WX-OPENID": "other"},
    )
    assert result.status_code == 404


def test_http_errors_do_not_expose_secrets(caplog):
    config = Settings(
        _env_file=None, xpay_appkey_sandbox="12345", wx_appsecret="test-secret", wx_appid="test-app"
    )

    def handler(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = XPayClient(config, http)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(XPayError) as exc:
                client.session_key("test-code", "test-user")
            assert "test-secret" not in str(exc.value)
            with pytest.raises(XPayError):
                client.query("test-user", "test-trade", 1)
        assert "test-secret" not in caplog.text and "pay_sig=" not in caplog.text


def test_wx_http_verifies_with_system_trust_store(monkeypatch):
    # 云托管开放接口服务接管 api.weixin.qq.com 用的是系统证书库里的平台证书，httpx 默认那份
    # 不认（10/19 实测 self-signed certificate）；改用系统证书库，但证书和域名照样校验
    made = []
    real = ssl.create_default_context

    def record(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(xpay.ssl, "create_default_context", record)
    with wx_http() as http:
        assert http.trust_env is False
    assert len(made) == 1
    assert made[0].verify_mode == ssl.CERT_REQUIRED and made[0].check_hostname


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"errcode": 40029, "errmsg": "invalid code"}, "errcode=40029"),
        ({"openid": "someone-else", "session_key": "x"}, "openid 对不上"),
        ({"openid": "test-user"}, "没有会话密钥"),
        (["not", "a", "dict"], "返回格式不对"),
    ],
)
def test_session_key_failure_reason_logged_without_secrets(caplog, body, reason):
    # 失败原因要能在日志里看到（10/19 查了三轮终端才知道原因），但不能带密钥和登录凭证
    config = Settings(_env_file=None, wx_appsecret="test-secret", wx_appid="test-app")
    handler = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    with (
        httpx.Client(transport=handler) as http,
        caplog.at_level(logging.WARNING, logger="youju.xpay"),
        pytest.raises(XPayError),
    ):
        XPayClient(config, http).session_key("test-code", "test-user")
    assert f"换会话密钥失败：{reason}" in caplog.text
    assert "test-secret" not in caplog.text and "test-code" not in caplog.text


def test_session_key_transport_error_logs_only_the_type(caplog):
    config = Settings(_env_file=None, wx_appsecret="test-secret", wx_appid="test-app")

    def handler(request):
        raise httpx.ConnectError(f"certificate verify failed for {request.url}")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        caplog.at_level(logging.WARNING, logger="youju.xpay"),
        pytest.raises(XPayError),
    ):
        XPayClient(config, http).session_key("test-code", "test-user")
    assert "换会话密钥失败：ConnectError" in caplog.text
    assert "test-secret" not in caplog.text and "test-code" not in caplog.text


@pytest.mark.parametrize("kind", ["xpay_refund_notify", "xpay_complaint_notify"])
def test_official_refund_and_complaint_fields(setup_pay, session, kind):
    row = delivered(setup_pay, session)
    data = {
        "Event": kind,
        "OpenId": "pay-user",
        "MchOrderId": row.out_trade_no,
        "WxOrderId": row.wx_order_id,
        "ComplaintDetail": "个人电话 13800138000",
        "RefundFee": 100,
        "RetCode": 0,
    }
    if kind == "xpay_refund_notify":
        setup_pay[2].status, setup_pay[2].left_fee = 8, 0
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert row.status == ("refunded" if kind == "xpay_refund_notify" else "manual")
    summary = session.scalar(select(PayEvent).where(PayEvent.kind == kind)).summary
    assert "13800138000" not in str(summary) and "ComplaintDetail" not in summary


@pytest.mark.parametrize("problem", ["still_paid", "query_error"])
def test_refund_push_on_completed_order_not_confirmed_goes_manual(setup_pay, session, problem):
    """已完成的订单（苹果那边或小程序后台退的款）收到退款推送、查单没确认退款：转人工、告警一次，
    使用期先不动；重推时查单显示退完了（8），照常记成已退款、收回使用期（Codex 10/29）。
    以前只回"没处理"等微信重推，重推完、每分钟的补单又不查已完成的订单，就没人知道了。"""
    row = delivered(setup_pay, session)
    ticket = payments.order_pass(session, row)
    data = {
        "Event": "xpay_refund_notify",
        "OpenId": "pay-user",
        "MchOrderId": row.out_trade_no,
        "WxOrderId": row.wx_order_id,
        "RefundFee": 100,
        "RetCode": 0,
    }
    fake = setup_pay[2]
    # still_paid：查单还是已付款（2）；query_error：查单返回别的订单，核验失败
    fake.bad_order = problem == "query_error"
    assert push(setup_pay, data).json()["ErrCode"] != 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "manual" and row.error == payments.REFUND_PUSH_UNCONFIRMED_ERROR
    assert session.get(WorkbenchPass, ticket.id).revoked_at is None
    alerts = []
    payments.run_payments(
        session, settings=setup_pay[1], client=fake.client, stamp=NOW, alert=collect(alerts)
    )
    assert len(alerts) == 1 and f"支付订单 #{row.id} 需人工处理" in alerts[0]
    # 微信重推、这次还是没核实（查单又出错或还是已付款）：人工核对的原因不被盖掉（Codex 第 3 轮）
    assert push(setup_pay, data).json()["ErrCode"] != 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "manual" and row.error == payments.REFUND_PUSH_UNCONFIRMED_ERROR
    fake.bad_order, fake.status, fake.left_fee = False, 8, 0
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert session.get(PayOrder, row.id).status == "refunded"
    assert session.get(WorkbenchPass, ticket.id).revoked_at is not None


def test_unconfirmed_refund_push_on_order_already_manual_alerts_again(setup_pay, session):
    """已经因为投诉转了人工、也告警过的订单，又收到没核实的退款推送：原来的原因留着，前面加上退款要核对，
    再告警一次；微信重推时不重复加、不重复告警（Codex 第 4 轮）。"""
    row = delivered(setup_pay, session)
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    assert push(setup_pay, {**base, "Event": "xpay_complaint_notify"}).json()["ErrCode"] == 0
    alerts = []

    def run_alerts(stamp):
        payments.run_payments(
            session,
            settings=setup_pay[1],
            client=setup_pay[2].client,
            stamp=stamp,
            alert=collect(alerts),
        )

    run_alerts(NOW)
    session.expire_all()
    row = session.get(PayOrder, row.id)
    complaint = row.error
    assert row.status == "manual" and row.alerted_at is not None and len(alerts) == 1
    refund = {**base, "Event": "xpay_refund_notify", "RefundFee": 100, "RetCode": 0}
    for _ in range(2):  # 第二次是微信重推
        assert push(setup_pay, refund).json()["ErrCode"] != 0
        session.expire_all()
        row = session.get(PayOrder, row.id)
        assert row.error.startswith(payments.REFUND_PUSH_UNCONFIRMED_ERROR)
        assert row.error.endswith(complaint) and row.error.count("收到退款推送") == 1
        run_alerts(NOW + timedelta(minutes=1))
    assert len(alerts) == 2 and payments.REFUND_PUSH_UNCONFIRMED_ERROR in alerts[1]


@pytest.mark.parametrize("order_of_events", ["refund_then_complaint", "complaint_then_partial"])
def test_manual_reasons_are_added_not_overwritten_and_alert_again(
    setup_pay, session, order_of_events
):
    """转人工的原因只加不盖、每来一个新原因再告警一次（payments.escalate，Codex 第 5 轮）：
    没核实的退款转人工后又来投诉，退款核对那句不能被盖掉；投诉告警过以后查单显示部分退款，要再告警。"""
    row = delivered(setup_pay, session)
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    complaint = {**base, "Event": "xpay_complaint_notify"}
    refund = {**base, "Event": "xpay_refund_notify", "RefundFee": 100, "RetCode": 0}
    fake = setup_pay[2]
    alerts = []

    def run_alerts(minutes):
        payments.run_payments(
            session,
            settings=setup_pay[1],
            client=fake.client,
            stamp=NOW + timedelta(minutes=minutes),
            alert=collect(alerts),
        )
        session.expire_all()
        return session.get(PayOrder, row.id)

    if order_of_events == "refund_then_complaint":
        assert push(setup_pay, refund).json()["ErrCode"] != 0
        run_alerts(0)
        assert push(setup_pay, complaint).json()["ErrCode"] == 0
        current = run_alerts(1)
        assert current.error.startswith(f"{payments.COMPLAINT_ERROR}，需人工处理；原来的原因：")
        assert payments.REFUND_PUSH_UNCONFIRMED_ERROR in current.error
    else:
        assert push(setup_pay, complaint).json()["ErrCode"] == 0
        run_alerts(0)
        fake.status, fake.left_fee = 8, 50  # 查单：退完了，但剩的钱和预期不符
        assert push(setup_pay, refund).json()["ErrCode"] == 0
        current = run_alerts(1)
        assert current.error.startswith(
            "订单部分退款，退款金额和预期不符，需人工核对；原来的原因："
        )
        assert payments.COMPLAINT_ERROR in current.error and current.refund_status == "partial"
    assert current.status == "manual" and current.alerted_at == NOW + timedelta(minutes=1)
    assert len(alerts) == 2 and alerts[1] == f"支付订单 #{row.id} 需人工处理：{current.error}"
    # 同一个原因再来一次：不重复加、不重复告警
    assert not payments.escalate(current, f"{payments.COMPLAINT_ERROR}，需人工处理", NOW)
    assert run_alerts(2).alerted_at == NOW + timedelta(minutes=1) and len(alerts) == 2


def test_new_problem_on_manual_order_is_added_and_alerts_again(setup_pay, session):
    """已经转人工的单（这里是投诉），之后的推送查到付款但核验不过：付款信息不改，原因照样追加、
    再告警一次（Codex 第 6 轮：原来直接跳过，新问题没人知道）。"""
    row = order(session, create(setup_pay))
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    assert push(setup_pay, {**base, "Event": "xpay_complaint_notify"}).json()["ErrCode"] == 0
    alerts = []
    _, settings, fake = setup_pay
    payments.run_payments(
        session, settings=settings, client=fake.client, stamp=NOW, alert=collect(alerts)
    )
    fake.status, fake.fee = 2, 101
    assert push(setup_pay, payload(row)).json()["ErrCode"] != 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "manual" and row.error.startswith("微信已确认付款，但支付金额与订单不符")
    assert payments.COMPLAINT_ERROR in row.error and row.alerted_at is None
    later = NOW + timedelta(minutes=1)
    payments.run_payments(
        session, settings=settings, client=fake.client, stamp=later, alert=collect(alerts)
    )
    assert len(alerts) == 2 and "支付金额与订单不符" in alerts[1]
    assert payments.order_pass(session, row) is None


def test_manual_order_is_not_closed_out_by_a_push_before_its_alert_is_sent(setup_pay, session):
    """转了人工、告警还没发出去的单，推送先不自动开通：等补单把告警发出去再开通（微信会重推）——
    不然待发的告警跟着状态一起没了，投诉没人处理（Codex 第 7 轮）。"""
    row = order(session, create(setup_pay))
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    assert push(setup_pay, {**base, "Event": "xpay_complaint_notify"}).json()["ErrCode"] == 0
    _, settings, fake = setup_pay
    fake.status = 2
    assert push(setup_pay, payload(row)).json()["ErrCode"] != 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "manual" and payments.order_pass(session, row) is None
    alerts = []
    payments.run_payments(
        session, settings=settings, client=fake.client, stamp=NOW, alert=collect(alerts)
    )
    assert len(alerts) == 1 and payments.COMPLAINT_ERROR in alerts[0]
    assert push(setup_pay, payload(row)).json()["ErrCode"] == 0
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "completed" and payments.order_pass(session, row) is not None


def test_pay_recheck_command_opens_a_paid_order_closed_after_48_hours(
    setup_pay, session, session_factory, monkeypatch, capsys
):
    """48 小时查不到付款结果关了单、告警以后，负责人用 youju pay-recheck 向微信查一次：
    付了款且核验通过就开通、通知发货；先 --dry-run 只看查单结果（Codex 第 6 轮）。"""
    from youju import cli, xpay

    _, settings, fake = setup_pay
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr("youju.config.get_settings", lambda: settings)
    monkeypatch.setattr(
        xpay, "wx_http", lambda: httpx.Client(transport=httpx.MockTransport(fake.handle))
    )

    def recheck(*args):
        code = cli.main(["pay-recheck", *args])
        return code, capsys.readouterr().out.strip().splitlines()[-1]

    paid, unpaid = (order(session, create(setup_pay)) for _ in range(2))
    for row in (paid, unpaid):
        row.created_at = NOW - timedelta(hours=49)
    session.commit()
    fake.bad_order = True
    run(session, setup_pay)
    assert paid.status == unpaid.status == "closed"
    fake.bad_order, fake.status = False, 2
    assert recheck("--order-id", str(paid.id), "--dry-run") == (
        0,
        f"pay_recheck order={paid.id} result=dry_run status=closed wx_status=2 dry_run=1",
    )
    session.expire_all()
    assert session.get(PayOrder, paid.id).status == "closed"
    assert recheck("--order-id", str(paid.id)) == (
        0,
        f"pay_recheck order={paid.id} result=paid status=completed wx_status=- dry_run=0",
    )
    session.expire_all()
    row = session.get(PayOrder, paid.id)
    assert row.status == "completed" and row.goods_notified and payments.order_pass(session, row)
    # 已开通的不再动；微信说没付款的照旧关着；没有的单号报 missing
    assert recheck("--order-id", str(paid.id))[1].endswith(
        "result=skipped status=completed wx_status=- dry_run=0"
    )
    fake.status = 1
    assert recheck("--order-id", str(unpaid.id))[0] == 0
    session.expire_all()
    assert session.get(PayOrder, unpaid.id).status == "closed"
    assert recheck("--order-id", "999999")[0] == 1
    # 关了的单查到退款中（5）这类少见状态：不能当成已关闭了事，转人工、退出码 1（Codex 第 7 轮）
    fake.status = 5
    code, line = recheck("--order-id", str(unpaid.id))
    assert code == 1 and "result=manual status=manual" in line
    session.expire_all()
    row = session.get(PayOrder, unpaid.id)
    assert row.status == "manual" and row.error.startswith("核对时微信查单状态 5（")


def test_pay_recheck_keeps_the_pending_alert_of_a_manual_order_it_cannot_close(
    setup_pay, session, session_factory, monkeypatch, capsys
):
    """负责人核对一张告警还没发出去的人工单（这里是投诉），微信说还没付款：订单照旧是人工、
    待发的告警照旧由补单发，原因不丢；命令先打印订单上记的原因（Codex 第 8 轮）。"""
    from youju import cli, xpay

    _, settings, fake = setup_pay
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr("youju.config.get_settings", lambda: settings)
    monkeypatch.setattr(
        xpay, "wx_http", lambda: httpx.Client(transport=httpx.MockTransport(fake.handle))
    )
    row = order(session, create(setup_pay))
    base = {"OpenId": "pay-user", "MchOrderId": row.out_trade_no, "WxOrderId": row.wx_order_id}
    assert push(setup_pay, {**base, "Event": "xpay_complaint_notify"}).json()["ErrCode"] == 0
    fake.status = 1
    assert cli.main(["pay-recheck", "--order-id", str(row.id)]) == 0
    out = capsys.readouterr().out
    assert f"原因：{payments.COMPLAINT_ERROR}" in out and "result=unpaid status=manual" in out
    session.expire_all()
    row = session.get(PayOrder, row.id)
    assert row.status == "manual" and row.alerted_at is None
    alerts = []
    payments.run_payments(
        session, settings=settings, client=fake.client, stamp=NOW, alert=collect(alerts)
    )
    assert len(alerts) == 1 and payments.COMPLAINT_ERROR in alerts[0]


def test_pending_order_window_and_confirmed_close(setup_pay, session):
    row = order(session, create(setup_pay))
    assert run(session, setup_pay)["processed"] == 0
    # 超过 30 分钟仍继续查单（审阅第 3 条），只是间隔变为 10 分钟；未支付保持待支付。
    row.created_at = NOW - timedelta(minutes=31)
    session.commit()
    assert run(session, setup_pay)["processed"] == 1
    assert row.status == "pending"
    assert run(session, setup_pay, NOW + timedelta(minutes=5))["processed"] == 0
    row.created_at, row.query_retry_at = NOW - timedelta(minutes=3), None
    session.commit()
    setup_pay[2].status = 6
    run(session, setup_pay)
    assert row.status == "closed"


def test_bill_adapter_rejects_unknown_schema_and_fractional_fen(setup_pay):
    config = setup_pay[1]
    for raw in [
        b"",
        b"unknown,fee\nabc,100\n",
        b"trade,fee\na,0.5\n",
        b"trade,fee\na,NaN\n",
        b"trade,fee\na,100\na,100\n",
    ]:
        with pytest.raises(XPayError):
            payments.parse_bill(raw, config)
    config.xpay_bill_fee_unit = "yuan"
    assert payments.parse_bill(b"trade,fee\na,1.25\n", config) == {"a": 125}


def test_platform_can_be_replaced_with_configuration(setup_pay):
    config = setup_pay[1]
    config.xpay_platform_field = "confirmed_platform"
    config.xpay_ios_values = ["apple"]
    config.xpay_android_values = ["wechat"]
    assert payments.platform({"confirmed_platform": "apple"}, config) == "ios"
    assert payments.platform({"confirmed_platform": "wechat"}, config) == "android"
    assert payments.platform({"order_type": 0}, config) == "unknown"


def test_concurrent_delivery_transactions_are_idempotent(setup_pay, session, session_factory):
    from concurrent.futures import ThreadPoolExecutor

    row = order(session, create(setup_pay))
    setup_pay[2].status = 2

    def deliver(_):
        with session_factory() as tx:
            locked = payments.lock_order(tx, row.id)
            checked = payments.query(tx, locked, setup_pay[2].client)
            assert payments.accept_payment(tx, locked, checked, setup_pay[1], NOW)
            tx.commit()
            return payments.order_pass(tx, locked).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(deliver, range(15)))
    assert len(set(ids)) == 1
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 1
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0


def test_ios_deadline_when_database_is_slow(setup_pay, monkeypatch):
    import threading
    import time

    release, finished = threading.Event(), threading.Event()

    def slow_reply(bind, trade):
        release.wait(10)
        finished.set()
        return {"result_code": 1, "result_info": "任务已完成", "evidence": "任务测试凭据"}

    monkeypatch.setattr(pay, "_ios_reply", slow_reply)
    started = time.monotonic()
    try:
        response = push(
            setup_pay,
            {"Event": "xpay_subscribe_ios_refund_query_notify", "pay_order_id": "unknown"},
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        assert finished.wait(1)
    assert elapsed < 3
    # 核验超时也答建议退款，注明暂时无法核验（付费服务协议第二条第 7 项）
    assert response.json()["result_code"] == 0
    assert response.json()["result_info"] == "无法核验"
    assert response.json()["evidence"] == "暂时无法核验"


def test_payment_after_account_deletion_goes_to_manual_without_identity(setup_pay, session):
    row = order(session, create(setup_pay))
    data = payload(row)
    assert setup_pay[0].delete("/api/v1/me", headers=HEADERS).status_code == 204
    setup_pay[2].status = 2
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert row.status == "manual" and row.paid_fee == 100
    assert row.user_id is None
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    for e in session.scalars(select(PayEvent)):
        assert "OpenId" not in e.summary and "openid" not in e.summary


def test_review_3_old_pending_payment_recovers_with_backoff(setup_pay, session):
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(minutes=31)
    session.commit()
    fake = setup_pay[2]
    fake.bad_order = True
    assert run(session, setup_pay)["processed"] == 1
    count = len(fake.calls)
    assert run(session, setup_pay, NOW + timedelta(minutes=1))["processed"] == 0
    assert len(fake.calls) == count
    fake.bad_order, fake.status = False, 2
    run(session, setup_pay, NOW + timedelta(minutes=10))
    assert row.status == "completed" and payments.order_pass(session, row)


@pytest.mark.parametrize("wx_status", [0, 1, 6, 2])
def test_review_3_pending_after_48_hours_closes_or_is_manual(setup_pay, session, wx_status):
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(hours=49)
    session.commit()
    setup_pay[2].status = wx_status
    run(session, setup_pay)
    assert row.status == ("completed" if wx_status == 2 else "closed")
    assert bool(payments.order_pass(session, row)) == (wx_status == 2)


def test_review_3_bill_paid_but_not_delivered_goes_manual(setup_pay, session):
    row = order(session, create(setup_pay))
    payments.reconcile(session, NOW.date(), {row.out_trade_no: row.price}, NOW)
    assert row.status == "manual" and payments.order_pass(session, row) is None
    run(session, setup_pay)
    assert row.alerted_at and payments.overview(session)["manual"] == 1


@pytest.mark.parametrize("exhausted", ["attempts", "age"])
def test_review_6_exhausted_refund_moves_to_manual(setup_pay, session, exhausted):
    row = delivered(setup_pay, session)
    row.status, row.refund_status = "refunding", "retry"
    row.refund_attempts = 7 if exhausted == "attempts" else 1
    row.refund_started_at = NOW - timedelta(hours=25) if exhausted == "age" else NOW
    session.commit()
    setup_pay[2].fail_refund = True
    run(session, setup_pay)
    assert row.status == "manual" and row.alerted_at
    assert payments.overview(session)["manual"] == 1
    assert not any(path == "/xpay/refund_order" for path, _, _ in setup_pay[2].calls)


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/api/v1/pay/orders",
            {
                "product_id": "workbench_30d",
                "rows": ["私密企业"],
                "code": "private-code",
                "unexpected": "私密企业",
            },
        ),
        (
            "/api/v1/advisor/tasks",
            {
                "kind": "compare",
                "rows": ["私密企业"],
                "file": {"name": "secret.csv", "content_base64": "secret-file"},
            },
        ),
        ("/api/v1/advisor/tasks", {"kind": "track", "rows": ["私密企业"]}),
    ],
)
def test_review_8_validation_never_echoes_lists(setup_pay, path, body):
    response = setup_pay[0].post(
        path, headers={**HEADERS, "Idempotency-Key": "ordinary"}, json=body
    )
    assert response.status_code == 422
    assert "私密企业" not in response.text and "private-code" not in response.text
    assert "secret-file" not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"loc", "msg"}


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize("data", [{"action": "CheckContainerPath"}, {"Event": "subscribe"}])
def test_review_a_push_available_when_payment_disabled(setup_pay, xml, data):
    client, settings, fake = setup_pay
    settings.pay_enabled = False
    if xml:
        root = ElementTree.Element("xml")
        for key, value in data.items():
            ElementTree.SubElement(root, key).text = value
        response = client.post("/api/wx/push", headers=HEADERS, content=ElementTree.tostring(root))
        assert response.status_code == 200
        assert ElementTree.fromstring(response.content).findtext("ErrCode") == "0"
    else:
        response = push(setup_pay, data)
        assert response.status_code == 200
        assert response.json() == {"ErrCode": 0, "ErrMsg": "success"}
    assert not fake.calls


@pytest.mark.parametrize(
    "kind",
    [
        "xpay_goods_deliver_notify",
        "xpay_refund_notify",
        "xpay_complaint_notify",
        "xpay_subscribe_ios_refund_query_notify",
    ],
)
def test_review_a_disabled_payment_event_is_acknowledged_and_recorded(setup_pay, session, kind):
    setup_pay[1].pay_enabled = False
    response = push(setup_pay, {"Event": kind, "OutTradeNo": "unknown", "private": "不入库"})
    assert response.status_code == 200 and response.json()["ErrCode"] == 0
    event = session.scalar(select(PayEvent))
    assert event and event.kind == kind and "不入库" not in str(event.summary)
    assert not setup_pay[2].calls


def test_review_a_missing_appid_header_is_accepted(setup_pay):
    setup_pay[1].wx_appid = "configured-app"
    assert push(setup_pay, {"action": "CheckContainerPath"}).status_code == 200
    bad = setup_pay[0].post(
        "/api/wx/push",
        headers={**HEADERS, "X-WX-APPID": "other"},
        json={"action": "CheckContainerPath"},
    )
    assert bad.status_code == 403


def test_review_a_private_service_accepts_push_without_source(setup_pay):
    object.__setattr__(setup_pay[1], "wx_push_public", False)
    assert (
        setup_pay[0].post("/api/wx/push", json={"action": "CheckContainerPath"}).status_code == 200
    )


def test_review_a_untrusted_service_hides_push(setup_pay):
    setup_pay[1].trust_wx_headers = False
    assert push(setup_pay, {"action": "CheckContainerPath"}).status_code == 404


@pytest.mark.parametrize(
    "kind", ["xpay_goods_deliver_notify", "xpay_refund_notify", "xpay_complaint_notify"]
)
def test_review_b_unknown_order_is_acknowledged_with_safe_event(setup_pay, session, kind):
    response = push(
        setup_pay,
        {
            "Event": kind,
            "OutTradeNo": "unknown",
            "MchOrderId": "unknown",
            "ComplaintDetail": "个人电话 13800138000",
        },
    )
    assert response.json()["ErrCode"] == 0
    saved = session.scalar(select(PayEvent))
    assert saved and saved.order_id is None and saved.kind == kind
    assert "13800138000" not in str(saved.summary)
    assert not setup_pay[2].calls


def test_refunding_order_label(setup_pay, session):
    row = delivered(setup_pay, session)
    row.status = "refunding"
    session.commit()
    response = setup_pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS)
    assert response.json()["status"] == "退款中"


# ---- 第二轮审阅：9 个已确认问题（2026-10-04） ----


def _capture_sql(db_engine):
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()).upper())

    sa_event.listen(db_engine, "before_cursor_execute", capture)
    return statements, lambda: sa_event.remove(db_engine, "before_cursor_execute", capture)


def _calls(fake, path):
    return [body for called, body, _ in fake.calls if called == path]


def test_fix_1_anonymize_without_orders_never_locks_pay_orders(setup_pay, session, db_engine):
    statements, stop = _capture_sql(db_engine)
    try:
        assert setup_pay[0].delete("/api/v1/me", headers=HEADERS).status_code == 204
    finally:
        stop()
    pay_sql = [sql for sql in statements if "FROM PAY_ORDERS" in sql]
    # 仍然检查过有没有订单，但只用普通查询；youju_api 只有 SELECT 权限时注销不能 500。
    assert pay_sql and not any("FOR UPDATE" in sql for sql in pay_sql)
    assert session.scalar(select(func.count(User.id))) == 0


def test_fix_1_anonymize_with_orders_still_locks(setup_pay, session, db_engine):
    row = order(session, create(setup_pay))
    statements, stop = _capture_sql(db_engine)
    try:
        assert setup_pay[0].delete("/api/v1/me", headers=HEADERS).status_code == 204
    finally:
        stop()
    assert any("FROM PAY_ORDERS" in sql and "FOR UPDATE" in sql for sql in statements)
    session.expire_all()
    assert row.status == "closed" and row.user_id is None


@pytest.mark.parametrize("overdue", [False, True])
@pytest.mark.parametrize("problem", ["fee", "paid_time"])
def test_fix_2_paid_but_unverifiable_goes_manual(setup_pay, session, problem, overdue):
    row = order(session, create(setup_pay))
    row.created_at = NOW - (timedelta(hours=49) if overdue else timedelta(minutes=3))
    session.commit()
    fake = setup_pay[2]
    fake.status = 2
    if problem == "fee":
        fake.fee = 101
    else:
        fake.paid_time = 0
    run(session, setup_pay)
    assert row.status == "manual" and payments.order_pass(session, row) is None and row.alerted_at
    assert ("金额" if problem == "fee" else "支付时间") in row.error
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    assert not _calls(fake, "/xpay/notify_provide_goods")


def test_fix_2_push_amount_mismatch_manual_and_alerted_once(setup_pay, session):
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status, fake.fee = 2, 101
    assert push(setup_pay, payload(row)).json()["ErrCode"] != 0
    session.expire_all()
    assert (
        row.status == "manual"
        and row.alerted_at is None
        and payments.order_pass(session, row) is None
    )
    run(session, setup_pay)
    alerted = row.alerted_at
    assert alerted
    # 微信重推同一笔：仍不发货，也不重复告警。
    assert push(setup_pay, payload(row)).json()["ErrCode"] != 0
    session.expire_all()
    assert row.status == "manual" and row.alerted_at == alerted
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0


def test_fix_3_unconfirmable_pending_closes_and_alerts_after_48_hours(setup_pay, session):
    """查了 48 小时都没查成：关单并告警（现网真扣钱，万一其实付了款、推送和对账也没接上，有人去核对）；
    告警没发出去，下一轮接着发，发出去以后不再选中（Codex 10/29）。不转人工：没有能把这种单查清楚的工具，
    订单页"1 个工作日内处理"兑现不了（独立审阅 10/29）。"""
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(hours=47)
    session.commit()
    _, settings, fake = setup_pay
    fake.bad_order = True
    run(session, setup_pay)
    assert row.status == "pending"
    row.created_at, row.query_retry_at = NOW - timedelta(hours=49), None
    session.commit()
    sent = []

    def flaky(message):
        sent.append(message)
        return len(sent) > 1  # 第一次没发出去

    payments.run_payments(session, settings=settings, client=fake.client, stamp=NOW, alert=flaky)
    assert row.status == "closed" and row.error == payments.UNCONFIRMED_PAYMENT_ERROR
    assert row.alerted_at is None
    # 支付关着也照样补发（定时任务先看 has_work）
    settings.pay_enabled = False
    assert payments.has_work(session, settings) is True
    later = NOW + timedelta(minutes=1)
    payments.run_payments(session, settings=settings, client=fake.client, stamp=later, alert=flaky)
    assert row.status == "closed" and row.alerted_at == later
    assert (
        sent
        == [
            f"支付订单 #{row.id} {payments.UNCONFIRMED_PAYMENT_ERROR}；核实付了款的，"
            f"在 admin 里运行 youju pay-recheck --order-id {row.id}"
        ]
        * 2
    )
    assert payments.has_work(session, settings) is False
    assert run(session, setup_pay, NOW + timedelta(hours=1))["processed"] == 0


def test_fix_4_unconfigured_bill_columns_fail_without_download(setup_pay, session):
    settings, fake = setup_pay[1], setup_pay[2]
    settings.xpay_bill_fee_column = ""
    day = NOW.date() - timedelta(days=1)
    payments.download_reconcile(session, day, fake.client, NOW, alert=lambda _: True)
    report = session.scalar(select(PayReconciliation).where(PayReconciliation.day == day))
    assert report.status == "failed" and report.differences == {"error": "未配置账单字段"}
    assert not fake.calls
    assert run(session, setup_pay)["processed"] == 0 and not fake.calls


@pytest.mark.parametrize("broken", ["download", "parse"])
def test_fix_4_bill_failures_are_counted_then_failed(setup_pay, session, broken):
    settings, fake = setup_pay[1], setup_pay[2]
    fake.bill_broken = broken
    days = [NOW.date() - timedelta(days=offset) for offset in (4, 3, 2, 1)]
    session.add_all(
        PayReconciliation(day=day, differences={}, status="pending", created_at=NOW, updated_at=NOW)
        for day in days
    )
    session.commit()
    alerts = []

    def run_once(minute):
        payments.run_payments(
            session,
            settings=settings,
            client=fake.client,
            stamp=NOW + timedelta(minutes=minute),
            alert=collect(alerts),
        )
        session.expire_all()
        return {
            row.day: (row.status, row.attempts)
            for row in session.scalars(select(PayReconciliation))
        }

    for minute in range(4):
        assert run_once(minute)[days[0]] == ("pending", minute + 1)
    state = run_once(4)
    assert [state[day] for day in days[:3]] == [("failed", 5)] * 3
    assert state[days[3]] == ("pending", 0)
    assert len(alerts) == 3 and all("对账" in text for text in alerts)
    # 失败的日子不再占用每轮 3 天名额，排在后面的那天开始处理。
    state = run_once(5)
    assert state[days[3]] == ("pending", 1) and len(alerts) == 3
    report = session.scalar(select(PayReconciliation).where(PayReconciliation.day == days[0]))
    assert report.differences and "error" in report.differences


def test_fix_5_closed_switch_without_open_orders_never_calls_wechat(
    setup_pay, session, monkeypatch
):
    client, settings, fake = setup_pay
    row = delivered(setup_pay, session)
    # 支付时间远离“昨天”：pay-reconcile 按真实当前日期取前一天，不能因测试日期不同而下载账单。
    row.status, row.paid_time = "completed", datetime(2020, 1, 1, tzinfo=UTC)
    closed = order(session, create(setup_pay))
    closed.status = "closed"
    session.commit()
    settings.pay_enabled = settings.advisor_enabled = False
    fake.calls.clear()
    assert run(session, setup_pay) == {"processed": 0}
    assert payments.should_reconcile(session, settings, datetime(2020, 1, 1).date())
    assert not payments.should_reconcile(session, settings, datetime(2020, 1, 2).date())

    def no_http(*args, **kwargs):
        raise AssertionError("支付关闭且没有待办时不得创建 HTTP 客户端")

    # CLI 自己创建真实 HTTP 客户端；这里禁止创建，保证不会访问微信。
    monkeypatch.setattr(httpx, "Client", no_http)
    monkeypatch.setattr("youju.config.get_settings", lambda: settings)
    assert main(["pay-run"]) == 0
    assert main(["pay-reconcile"]) == 0
    assert not fake.calls


def test_fix_5_closed_switch_push_still_handles_local_order(setup_pay, session):
    row = delivered(setup_pay, session)
    setup_pay[1].pay_enabled = False
    data = {
        "Event": "xpay_complaint_notify",
        "OpenId": "pay-user",
        "MchOrderId": row.out_trade_no,
        "WxOrderId": row.wx_order_id,
    }
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert row.status == "manual"


@pytest.mark.parametrize("job", ["pay-run", "pay-reconcile", "advisor-run"])
def test_fix_7_pay_run_is_not_recorded(tmp_path, job):
    from youju import jobs

    assert "pay-run" not in jobs.OVERVIEW_JOBS and "pay-run" in jobs.JOB_NAMES
    assert "pay-reconcile" in jobs.OVERVIEW_JOBS
    for name, content in {
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        # advisor-run 等锁写成 -w 55 -E 75 <锁>，其他任务是 -n -E 75 <锁>
        "flock": '#!/bin/sh\ncase "$1" in -w) shift 5 ;; *) shift 4 ;; esac\nexec "$@"\n',
        "youju": '#!/bin/sh\necho "$*" >> "$CALLS"\n[ "$1" = job-start ] && echo 42\nexit 0\n',
    }.items():
        (tmp_path / name).write_text(content)
        (tmp_path / name).chmod(0o755)
    script = (Path(__file__).resolve().parents[1] / "deploy" / "job.sh").read_text()
    script = script.replace(". /app/backend/deploy/env.sh", ":").replace("cd /app/backend", ":")
    script = script.replace("/proc/1/fd/1", str(tmp_path / "output"))
    script = script.replace("/tmp/youju-", f"{tmp_path}/youju-")
    (tmp_path / "job.sh").write_text(script)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(tmp_path / "c")}
    result = subprocess.run(["sh", str(tmp_path / "job.sh"), job], env=env, timeout=30)
    assert result.returncode == 0
    calls = (tmp_path / "c").read_text()
    assert job in calls
    assert ("job-start" in calls) == (job == "pay-reconcile")


@pytest.mark.parametrize("secret", [False, True])
def test_fix_8_session_key_endpoint_per_mode(setup_pay, secret):
    settings, fake = setup_pay[1], setup_pay[2]
    if secret:
        settings.wx_appid, settings.wx_appsecret = "test-app", SecretStr("test-secret")
    assert fake.client.session_key("test-login", "pay-user") == "9hAb/NEYUlkaMBEsmFgzig=="
    url = fake.calls[-1][2]
    expected = "https" if secret else "http"
    assert url.startswith(f"{expected}://api.weixin.qq.com/sns/jscode2session?")
    assert ("secret=test-secret" in url) == secret


def test_fix_9_provide_goods_retry_limit(setup_pay, session):
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(minutes=3)
    session.commit()
    fake = setup_pay[2]
    fake.status, fake.fail_provide = 2, True
    for _ in range(19):
        run(session, setup_pay)
    assert row.status == "completed" and row.provide_attempts == 19
    run(session, setup_pay)
    assert len(_calls(fake, "/xpay/notify_provide_goods")) == 20
    assert row.provide_attempts == 20 and not row.goods_notified
    assert row.status == "manual" and row.alerted_at and "通知发货" in row.error
    run(session, setup_pay)
    assert len(_calls(fake, "/xpay/notify_provide_goods")) == 20


def submit(setup_pay, kind="compare", count=1, key="task"):
    rows = (
        ["示例科技有限公司"] * count
        if kind == "compare"
        else [{"name": "示例科技有限公司", "province": "北京"}] * count
    )
    return setup_pay[0].post(
        "/api/v1/advisor/tasks",
        headers={**HEADERS, "Idempotency-Key": key},
        json={"kind": kind, "rows": rows},
    )


def self_refund(setup_pay, row):
    return setup_pay[0].post(f"/api/v1/pay/orders/{row.id}/refund", headers=HEADERS)


def inquiry(setup_pay, row):
    return push(
        setup_pay,
        {"Event": "xpay_subscribe_ios_refund_query_notify", "pay_order_id": row.out_trade_no},
    ).json()


def test_pass_extends_and_expires(setup_pay, session, monkeypatch):
    first = delivered(setup_pay, session)
    second = delivered(setup_pay, session)
    one, two = payments.order_pass(session, first), payments.order_pass(session, second)
    assert one.starts_at == NOW and one.ends_at == two.starts_at
    assert two.ends_at == NOW + timedelta(days=60)
    assert payments.current_pass(session, first.user_id, NOW + timedelta(days=30)).id == two.id
    assert payments.current_pass(session, first.user_id, NOW + timedelta(days=60)) is None
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(days=60))
    status = setup_pay[0].get("/api/v1/advisor/status", headers=HEADERS).json()
    assert status["pass"] == {"active": False, "expiresAt": None}
    assert submit(setup_pay, count=51).json()["detail"]["code"] == "FREE_ROW_LIMIT"


def test_new_pass_after_expiry_starts_at_payment(setup_pay, session, monkeypatch):
    first = delivered(setup_pay, session)
    later = NOW + timedelta(days=31)
    monkeypatch.setattr(v1, "now", lambda: later)
    setup_pay[2].paid_time = int(later.timestamp())
    response = create(setup_pay)
    row = order(session, response)
    data = payload(row)
    data["WeChatPayInfo"]["PaidTime"] = int(later.timestamp())
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert payments.order_pass(session, row).starts_at == later
    assert payments.order_pass(session, first).ends_at < later


@pytest.mark.parametrize("kind", ["compare", "track"])
def test_free_month_boundaries_and_two_codes(setup_pay, session, monkeypatch, kind):
    stamp = datetime(2026, 9, 30, 15, 59, 59, tzinfo=UTC)
    monkeypatch.setattr(v1, "now", lambda: stamp)
    assert submit(setup_pay, kind, 51, "large").json()["detail"] == {
        "code": "FREE_ROW_LIMIT",
        "message": "每次最多提交 50 行",
        "maxRows": 50,
    }
    tasks = []
    for n in range(3):
        response = submit(setup_pay, kind, 50, f"free-{n}")
        assert response.status_code == 202
        tasks.append(response.json()["id"])
        assert response.json()["usageSource"] == "free"
    assert submit(setup_pay, kind, 50, "fourth").json()["detail"]["code"] == "FREE_MONTHLY_LIMIT"
    # Each kind has its own allowance.
    other = "track" if kind == "compare" else "compare"
    assert submit(setup_pay, other, 1, "other").status_code == 202
    stamp += timedelta(seconds=1)
    assert submit(setup_pay, kind, 50, "october").status_code == 202
    for _ in range(2):
        assert (
            setup_pay[0]
            .post(f"/api/v1/advisor/tasks/{tasks[0]}/cancel", headers=HEADERS)
            .status_code
            == 200
        )
    session.expire_all()
    user_id = session.scalar(select(User.id))
    assert advisor_tasks.usage_counts(session, user_id, datetime(2026, 9, 30).date())[kind] == 2
    assert advisor_tasks.usage_counts(session, user_id, datetime(2026, 10, 1).date())[kind] == 1


@pytest.mark.parametrize("kind", ["compare", "track"])
def test_pass_500_rows_and_unlimited_tasks(setup_pay, session, kind):
    row = delivered(setup_pay, session)
    for n in range(4):
        response = submit(setup_pay, kind, 500, f"paid-{n}")
        assert response.status_code == 202
        assert response.json()["usageSource"] == "pass"
    assert submit(setup_pay, kind, 501, "large").json()["detail"]["code"] == "PASS_ROW_LIMIT"
    assert session.scalar(select(func.count(AdvisorUsage.id))) == 0
    session.expire_all()
    assert payments.paid_usage(session, payments.order_pass(session, row))
    status = setup_pay[0].get("/api/v1/advisor/status", headers=HEADERS).json()
    assert status["pass"]["active"] and status["pass"]["expiresAt"]
    assert status["quota"][kind] == {"used": 0, "limit": 3, "maxRows": 500}


@pytest.mark.parametrize(
    "case,expected", [("unused", 200), ("old", 422), ("used", 422), ("ios", 422)]
)
def test_self_refund_rules(setup_pay, session, monkeypatch, case, expected):
    if case == "ios":
        setup_pay[2].order_type = 7
    row = delivered(setup_pay, session)
    if case == "old":
        # 付款后 24 小时多一点：已超过无理由退款时限（D-081，原为 7 天）
        monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(hours=24, microseconds=1))
    if case == "used":
        # 用过付费的量 = 付费任务出了结果（排队、处理中的另有一句，见 test_pay_sale_prep）
        task_id = submit(setup_pay, count=51).json()["id"]
        session.expire_all()
        task = session.get(AdvisorTask, task_id)
        task.status, task.finished_at = "done", NOW
        advisor_tasks.record_pass_completion(session, task)
        session.commit()
    result = self_refund(setup_pay, row)
    assert result.status_code == expected
    session.expire_all()
    ticket = payments.order_pass(session, row)
    if expected == 200:
        assert ticket.revoked_at == NOW and result.json()["status"] == "退款中"
        first_id = row.refund_order_id
        assert self_refund(setup_pay, row).status_code == 200
        session.expire_all()
        assert row.refund_order_id == first_id
        assert len(_calls(setup_pay[2], "/xpay/refund_order")) == 1
        assert (
            setup_pay[0].get("/api/v1/advisor/status", headers=HEADERS).json()["pass"]["active"]
            is False
        )
    else:
        assert ticket.revoked_at is None
        reason = result.json()["detail"]
        assert {"old": "超过 24 小时", "used": "已用过付费的量", "ios": "App Store"}[case] in reason
        assert not _calls(setup_pay[2], "/xpay/refund_order")


@pytest.mark.parametrize(
    "state,used",
    [
        ("queued", None),
        ("running", None),
        ("done", True),
        ("done_then_deleted", True),
        ("failed", False),
        ("cancelled", False),
        ("deleted_before_run", False),
    ],
)
def test_ios_inquiry_counts_only_paid_tasks_with_results(setup_pay, session, state, used):
    """苹果问询（付费服务协议第二条第 2、7 项，开卖那一版必做第 13 项）：付费任务出了结果（之后删了也算）
    才算用过；还在排队、处理中的建议退款并注明暂时无法核验（问询只有一次，没出结果的不能答已使用）；
    系统出错、取消、处理前删掉的不算用过。凭据不带任务编号和时间（D-081）。"""
    setup_pay[2].order_type = 7
    row = delivered(setup_pay, session)
    assert inquiry(setup_pay, row)["result_info"] == "未使用"
    task_id = submit(setup_pay, count=51).json()["id"]
    session.expire_all()
    task = session.get(AdvisorTask, task_id)
    if state == "running":
        task.status, task.started_at = "running", NOW
        session.commit()
    elif state in ("done", "done_then_deleted", "failed", "cancelled"):
        task.status = "done" if state == "done_then_deleted" else state
        task.finished_at = NOW + timedelta(minutes=1)
        advisor_tasks.record_pass_completion(session, task)
        session.commit()
    if state in ("done_then_deleted", "deleted_before_run"):
        assert (
            setup_pay[0].delete(f"/api/v1/advisor/tasks/{task_id}", headers=HEADERS).status_code
            == 204
        )
    setup_pay[2].calls.clear()
    result = inquiry(setup_pay, row)
    if used:
        assert result["result_code"] == 1 and result["result_info"] == "已使用"
        assert result["evidence"] == "付款后 24 小时内；用过付费的量"
    elif used is None:
        assert result["result_code"] == 0 and result["result_info"] == "无法核验"
        assert result["evidence"] == "付款后 24 小时内；暂时无法核验是否用过付费的量"
    else:
        assert result["result_code"] == 0 and result["result_info"] == "未使用"
        assert result["evidence"] == "付款后 24 小时内；没有用过付费的量"
    assert str(task_id) not in result["evidence"]
    assert not setup_pay[2].calls


def test_ios_inquiry_after_24_hours_declines_without_payment_time(setup_pay, session, monkeypatch):
    """苹果退款问询（D-081）：没用过、付款 24 小时内建议退款（含恰好 24 小时）；超过 24 小时拒绝，
    凭据只说超过 24 小时、没用过，不写付款时间。"""
    setup_pay[2].order_type = 7
    row = delivered(setup_pay, session)
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(hours=24))
    within = inquiry(setup_pay, row)
    assert within["result_code"] == 0 and within["result_info"] == "未使用"
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(hours=24, seconds=1))
    result = inquiry(setup_pay, row)
    assert result["result_code"] == 1 and result["result_info"] == "付款已超过 24 小时"
    assert result["evidence"] == "付款后已超过 24 小时；没有用过付费的量"
    assert within["evidence"] == "付款后 24 小时内；没有用过付费的量"


def test_ios_refund_notification_revokes_only_its_pass(setup_pay, session):
    setup_pay[2].order_type = 7
    first = delivered(setup_pay, session)
    second = delivered(setup_pay, session)
    assert submit(setup_pay, count=51).status_code == 202
    setup_pay[2].status, setup_pay[2].left_fee = 8, 0
    data = {
        "Event": "xpay_refund_notify",
        "OpenId": "pay-user",
        "MchOrderId": first.out_trade_no,
        "WxOrderId": first.wx_order_id,
    }
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert payments.order_pass(session, first).revoked_at == NOW
    assert payments.order_pass(session, second).revoked_at is None
    assert self_refund(setup_pay, first).status_code == 422
    assert session.scalar(select(AdvisorTask.status)) == "queued"


def test_task_failure_does_not_refund_and_payment_switch_does_not_pause_tasks(setup_pay, session):
    row = delivered(setup_pay, session)
    task_id = submit(setup_pay, count=51).json()["id"]
    setup_pay[1].pay_enabled = False
    for _ in range(3):
        advisor_tasks.run_advisor_tasks(session, settings=setup_pay[1], stamp=NOW, max_tasks=1)
    session.expire_all()
    assert session.get(AdvisorTask, task_id).status == "failed"
    # 系统出错、没出结果的付费任务不算用过（付费服务协议第二条第 2 项）：不自动退款，用户可以自己申请
    assert not payments.paid_usage(session, payments.order_pass(session, row))
    run(session, setup_pay)
    assert row.status == "completed" and row.refund_order_id is None
    assert not _calls(setup_pay[2], "/xpay/refund_order")
    assert payments.refund_reason(session, row, NOW) is None


def test_account_deletion_preserves_financial_and_usage_audit(setup_pay, session):
    row = delivered(setup_pay, session)
    task_id = submit(setup_pay, count=51).json()["id"]
    # 出了结果的付费任务：注销删掉任务以后，使用期上的记录还说明用过（苹果问询、争议时要用）
    session.expire_all()
    task = session.get(AdvisorTask, task_id)
    task.status, task.finished_at = "done", NOW
    advisor_tasks.record_pass_completion(session, task)
    session.commit()
    ticket = payments.order_pass(session, row)
    assert setup_pay[0].delete("/api/v1/me", headers=HEADERS).status_code == 204
    session.expire_all()
    assert row.user_id is None and row.pseudonym and row.price == 100
    assert (
        ticket.user_id is None and ticket.revoked_at is not None and ticket.last_task_id == task_id
    )
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    assert payments.paid_usage(session, ticket)
    for e in session.scalars(select(PayEvent)):
        assert "OpenId" not in e.summary and "openid" not in e.summary
    payments.cleanup(session, setup_pay[1], NOW + timedelta(days=3651))
    session.commit()
    assert session.get(PayOrder, row.id) is None
    assert session.scalar(select(WorkbenchPass.id).where(WorkbenchPass.id == ticket.id)) is None


def test_order_list_only_last_20_and_refund_ownership(setup_pay, session):
    row = delivered(setup_pay, session)
    session.add_all(
        PayOrder(
            user_id=row.user_id,
            out_trade_no=f"list-{n}",
            product_id="workbench_30d",
            price=100,
            days=30,
            env=0,
            terms_version="pay-test",
            created_at=NOW + timedelta(seconds=n),
        )
        for n in range(25)
    )
    session.commit()
    result = setup_pay[0].get("/api/v1/pay/orders", headers=HEADERS).json()["items"]
    assert len(result) == 20 and result[0]["orderId"] > result[-1]["orderId"]
    assert set(result[0]) == {
        "orderId",
        "name",
        "days",
        "status",
        "amount",
        "termsVersion",
        "createdAt",
        "paidAt",
        "paidAmount",
        "passStartsAt",
        "passEndsAt",
        "passRevokedAt",
        "gifts",
        "refundAmount",
        "canRefund",
        "refundUnavailableReason",
        "canInvoice",
        "invoice",
    }
    assert result[0]["canRefund"] is False and result[0]["refundUnavailableReason"]
    other = {**HEADERS, "X-WX-OPENID": "other"}
    setup_pay[0].post("/api/v1/me/agreement", headers=other, json={"version": "2026-11"})
    assert setup_pay[0].get("/api/v1/pay/orders", headers=other).json() == {"items": []}
    # 还没有用户记录的人（没同意过协议）：也是空列表，不是 404（小程序会把 404 说成付费服务未开放）
    unknown = {**HEADERS, "X-WX-OPENID": "never-agreed"}
    response = setup_pay[0].get("/api/v1/pay/orders", headers=unknown)
    assert response.status_code == 200 and response.json() == {"items": []}
    assert (
        setup_pay[0].post(f"/api/v1/pay/orders/{row.id}/refund", headers=other).status_code == 404
    )


def test_refund_partial_success_is_manual(setup_pay, session):
    row = delivered(setup_pay, session)
    assert self_refund(setup_pay, row).status_code == 200
    setup_pay[2].status, setup_pay[2].left_fee = 8, 50
    run(session, setup_pay, NOW + timedelta(minutes=10))
    session.expire_all()
    assert row.status == "manual" and row.refund_status == "partial"
    assert payments.order_pass(session, row).revoked_at is not None


def test_refund_reads_latest_usage_after_waiting_for_user_lock(setup_pay, session_factory):
    with session_factory() as session:
        row = delivered(setup_pay, session)
        session.commit()
        with session_factory() as stale:
            stale.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            seen = stale.get(PayOrder, row.id)
            assert not payments.paid_usage(stale, payments.order_pass(stale, seen))
            assert submit(setup_pay, count=51).status_code == 202
            locked = payments.lock_order(stale, row.id)
            # 等到用户锁以后读到了刚交的付费任务（还在排队：先不能退，处理完没出结果的再退）
            reason = payments.refund_reason(stale, locked, NOW, lock=True)
            assert reason == payments.PAID_TASK_RUNNING


def test_concurrent_orders_extend_without_overlapping(setup_pay, session_factory, session):
    from concurrent.futures import ThreadPoolExecutor

    orders = [order(session, create(setup_pay)).id for _ in range(3)]
    session.commit()
    setup_pay[2].status = 2

    def confirm(order_id):
        with session_factory() as tx:
            row = payments.lock_order(tx, order_id)
            assert payments.accept_payment(
                tx, row, payments.query(tx, row, setup_pay[2].client), setup_pay[1], NOW
            )
            tx.commit()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(confirm, orders))
    tickets = list(session.scalars(select(WorkbenchPass).order_by(WorkbenchPass.starts_at)))
    assert [(p.starts_at, p.ends_at) for p in tickets] == [
        (NOW + timedelta(days=n * 30), NOW + timedelta(days=(n + 1) * 30)) for n in range(3)
    ]
    status = setup_pay[0].get("/api/v1/advisor/status", headers=HEADERS).json()
    assert datetime.fromisoformat(
        status["pass"]["expiresAt"].replace("Z", "+00:00")
    ) == NOW + timedelta(days=90)


@pytest.mark.parametrize("kind", ["compare", "track"])
def test_free_failure_releases_only_once(setup_pay, session, kind):
    tasks = [submit(setup_pay, kind=kind, key=str(n)).json()["id"] for n in range(3)]
    session.expire_all()
    task = session.get(AdvisorTask, tasks[0])
    task.status, task.attempts = "failed", 3
    advisor_tasks.release_usage(session, task)
    advisor_tasks.release_usage(session, task)
    session.commit()
    assert session.scalar(select(AdvisorUsage.used_count)) == 2
    assert submit(setup_pay, kind=kind, key="replacement").status_code == 202
    assert (
        submit(setup_pay, kind=kind, key="extra").json()["detail"]["code"] == "FREE_MONTHLY_LIMIT"
    )


def test_latest_usage_evidence_is_not_overwritten_by_older_task(setup_pay, session):
    row = delivered(setup_pay, session)
    ids = [submit(setup_pay, count=51, key=str(n)).json()["id"] for n in range(2)]
    session.expire_all()
    older = session.get(AdvisorTask, ids[0])
    newer = session.get(AdvisorTask, ids[1])
    newer.finished_at = NOW + timedelta(minutes=1)
    advisor_tasks.record_pass_completion(session, newer)
    older.finished_at = NOW + timedelta(minutes=2)
    advisor_tasks.record_pass_completion(session, older)
    session.commit()
    assert payments.order_pass(session, row).last_task_id == ids[1]
    assert payments.order_pass(session, row).last_task_completed_at == newer.finished_at


def test_500_rows_execute_compare_engine_without_refund(setup_pay, session, monkeypatch):
    row = delivered(setup_pay, session)
    response = submit(setup_pay, count=500)
    task_id = response.json()["id"]
    setup_pay[1].cloud_env = "fake-cloud"
    monkeypatch.setattr(advisor_tasks, "upload", lambda *a, **kw: "fake-result-id")
    result = advisor_tasks.run_advisor_tasks(session, settings=setup_pay[1], stamp=NOW, max_tasks=1)
    assert result["done"] == 1
    session.expire_all()
    assert (
        session.get(AdvisorTask, task_id).result_rows == 1
    )  # duplicate enterprise names are merged
    assert payments.order_pass(session, row).last_task_completed_at == NOW
    # 出了结果才记"用过付费的量"的时间（提交时不记）
    assert payments.order_pass(session, row).last_used_at == NOW
    assert row.status == "completed" and row.refund_status is None


@pytest.mark.parametrize(
    "stage,failed_response", [("query", False), ("refund", False), ("refund", True)]
)
def test_refund_notification_before_network_response_keeps_terminal_status(
    setup_pay, session, monkeypatch, stage, failed_response
):
    row = delivered(setup_pay, session)
    fake = setup_pay[2]
    real_query, real_refund = fake.client.query, fake.client.refund
    notified = False

    def notify():
        nonlocal notified
        notified = True
        fake.status, fake.left_fee = 5, 0
        # 推送说退款成功（RetCode 0）：查单停在 5 也算退完
        result = push(
            setup_pay,
            {
                "Event": "xpay_refund_notify",
                "OpenId": "pay-user",
                "MchOrderId": row.out_trade_no,
                "WxOrderId": row.wx_order_id,
                "RetCode": 0,
            },
        )
        assert result.json()["ErrCode"] == 0

    def query_with_notice(*args):
        checked = real_query(*args)
        if stage == "query" and not notified:
            notify()
        return checked

    def refund_with_notice(body):
        real_refund(body)
        notify()
        if failed_response:
            raise XPayError

    monkeypatch.setattr(fake.client, "query", query_with_notice)
    monkeypatch.setattr(fake.client, "refund", refund_with_notice)
    result = self_refund(setup_pay, row)
    assert result.status_code == 200 and result.json()["status"] == "已退款"
    session.expire_all()
    assert row.status == "refunded" and row.refund_status == "completed"
    assert payments.order_pass(session, row).revoked_at is not None


def test_lock_order_takes_user_lock_before_flushing_payment_event(setup_pay, session, db_engine):
    row = delivered(setup_pay, session)
    session.commit()
    statements, stop = _capture_sql(db_engine)
    try:
        payments.event(session, "query_order", {"status": 2}, row)
        payments.lock_order(session, row.id)
        session.flush()
    finally:
        stop()
    user_lock = next(
        i for i, sql in enumerate(statements) if "FROM USERS" in sql and "FOR UPDATE" in sql
    )
    order_lock = next(
        i for i, sql in enumerate(statements) if "FROM PAY_ORDERS" in sql and "FOR UPDATE" in sql
    )
    event_insert = next(i for i, sql in enumerate(statements) if "INSERT INTO PAY_EVENTS" in sql)
    assert user_lock < order_lock < event_insert


def test_account_deletion_locks_tasks_before_passes(setup_pay, session, db_engine):
    delivered(setup_pay, session)
    assert submit(setup_pay, count=51).status_code == 202
    statements, stop = _capture_sql(db_engine)
    try:
        assert setup_pay[0].delete("/api/v1/me", headers=HEADERS).status_code == 204
    finally:
        stop()
    locks = [sql for sql in statements if "FOR UPDATE" in sql]
    task_lock = next(i for i, sql in enumerate(locks) if "FROM ADVISOR_TASKS" in sql)
    pass_lock = next(i for i, sql in enumerate(locks) if "FROM WORKBENCH_PASSES" in sql)
    assert task_lock < pass_lock


def test_provide_response_does_not_overwrite_refund_notification(setup_pay, session, monkeypatch):
    response = create(setup_pay)
    row = order(session, response)
    fake = setup_pay[2]
    fake.status = 2

    def provide(*args):
        fake.status, fake.left_fee = 5, 0
        session.expire_all()
        assert (
            push(
                setup_pay,
                {
                    "Event": "xpay_refund_notify",
                    "OpenId": "pay-user",
                    "MchOrderId": row.out_trade_no,
                    "WxOrderId": row.wx_order_id,
                    "RetCode": 0,
                },
            ).json()["ErrCode"]
            == 0
        )
        raise XPayError

    monkeypatch.setattr(fake.client, "provide", provide)
    assert push(setup_pay, payload(row)).json()["ErrCode"] == 0
    session.expire_all()
    assert row.status == "refunded" and row.refund_status == "completed"


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("count", [50, 51, 500, 501])
def test_csv_submission_has_same_row_limits_as_inline(setup_pay, paid, count):
    import base64

    if paid:
        create_response = create(setup_pay)
        row_id = create_response.json()["orderId"]
        # Resolve trade from the signed order snapshot without another DB session.
        signed = json.loads(create_response.json()["signData"])
        fake = setup_pay[2]
        fake.status = 2
        data = {
            "Event": "xpay_goods_deliver_notify",
            "OpenId": "pay-user",
            "OutTradeNo": signed["outTradeNo"],
            "Env": 0,
            "GoodsInfo": {
                "ProductId": "workbench_30d",
                "Quantity": 1,
                "OrigPrice": 100,
                "ActualPrice": 100,
                "Attach": signed["outTradeNo"],
            },
        }
        assert push(setup_pay, data).json()["ErrCode"] == 0
        assert row_id
    content = "企业名称\n" + "\n".join(f"示例企业{i}" for i in range(count))
    response = setup_pay[0].post(
        "/api/v1/advisor/tasks",
        headers={**HEADERS, "Idempotency-Key": "csv"},
        json={
            "kind": "compare",
            "file": {
                "name": "clients.csv",
                "content_base64": base64.b64encode(content.encode()).decode(),
            },
        },
    )
    assert response.status_code == (202 if count <= (500 if paid else 50) else 429)
    if response.status_code == 429:
        assert response.json()["detail"]["code"] == ("PASS_ROW_LIMIT" if paid else "FREE_ROW_LIMIT")


def test_pass_holder_uses_free_quota_first_and_stays_refundable(setup_pay, session):
    """有使用期也先用免费的量：50 行以内、本月免费次数没用完的记为免费，不影响自助退款；
    超过 50 行或免费次数用完才算用了付费的量（和付款页写的退款条件一致）。"""
    row = delivered(setup_pay, session)
    for n in range(3):
        body = submit(setup_pay, count=50, key=f"small-{n}").json()
        assert body["usageSource"] == "free"
    session.expire_all()
    assert payments.refund_reason(session, row, NOW) is None
    status = setup_pay[0].get("/api/v1/advisor/status", headers=HEADERS).json()
    assert status["quota"]["compare"]["used"] == 3
    # 第 4 次：免费次数用完，才用使用期
    fourth = submit(setup_pay, count=1, key="fourth").json()
    assert fourth["usageSource"] == "pass"
    session.expire_all()
    assert payments.refund_reason(session, row, NOW) == payments.PAID_TASK_RUNNING
    task = session.get(AdvisorTask, fourth["id"])
    task.status, task.finished_at = "done", NOW
    advisor_tasks.record_pass_completion(session, task)
    session.commit()
    assert "已用过付费的量" in payments.refund_reason(session, row, NOW)


def test_orders_and_refund_stay_available_after_sales_switched_off(setup_pay, session):
    """关掉收费只停卖：已经付款的用户照样能看订单、自助退款；商品和下单是 404。"""
    client, settings, fake = setup_pay
    row = delivered(setup_pay, session)
    settings.pay_enabled = False
    assert client.get("/api/v1/pay/products", headers=HEADERS).status_code == 404
    assert create(setup_pay).status_code == 404
    listed = client.get("/api/v1/pay/orders", headers=HEADERS).json()["items"]
    assert [item["orderId"] for item in listed] == [row.id] and listed[0]["canRefund"] is True
    assert client.get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).status_code == 200
    # 使用期照常有效
    status = client.get("/api/v1/advisor/status", headers=HEADERS).json()
    assert status["pass"]["active"] is True
    assert self_refund(setup_pay, row).json()["status"] == "退款中"
    assert len(_calls(fake, "/xpay/refund_order")) == 1


def test_refunding_first_of_two_passes_moves_the_second_forward(setup_pay, session):
    """一次买了两张、退掉前面那张：后面那张往前挪到现在开始，不留空档。"""
    first = delivered(setup_pay, session)
    second = delivered(setup_pay, session)
    a, b = payments.order_pass(session, first), payments.order_pass(session, second)
    assert b.starts_at == a.ends_at
    assert self_refund(setup_pay, first).status_code == 200
    session.expire_all()
    a, b = payments.order_pass(session, first), payments.order_pass(session, second)
    assert a.revoked_at == NOW
    assert b.starts_at == NOW and b.ends_at == NOW + timedelta(days=30)
    status = setup_pay[0].get("/api/v1/advisor/status", headers=HEADERS).json()
    assert status["pass"]["active"] is True


def test_late_confirmation_still_extends_from_the_pass_active_at_payment(
    setup_pay, session, monkeypatch
):
    """付款时旧的一段还没结束、确认却晚到（已过旧段的结束时间）：新的一段不能从付款时间算起和旧段重叠
    （否则少给天数）；D-070 起也不早于确认开通的时间（确认之前用不了，不能算进使用期）。"""
    first = delivered(setup_pay, session)
    old = payments.order_pass(session, first)
    response = create(setup_pay)
    row = order(session, response)
    # 第 20 天付款，旧段第 30 天结束；第 31 天才收到确认
    paid_at = NOW + timedelta(days=20)
    setup_pay[2].status = 2
    setup_pay[2].paid_time = int(paid_at.timestamp())
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(days=31))
    data = payload(row)
    data["WeChatPayInfo"]["PaidTime"] = int(paid_at.timestamp())
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    new = payments.order_pass(session, session.get(PayOrder, row.id))
    assert old.ends_at == NOW + timedelta(days=30)
    assert new.starts_at == NOW + timedelta(days=31)
    assert new.ends_at == new.starts_at + timedelta(days=30)


def test_order_keeps_agreed_terms_version_and_rejects_outdated(setup_pay, session):
    """订单记下用户同意的《付费服务协议》版本；和现行版本不一致的不让下单。
    没有沙箱了（创始人 10/29）：现行版本必须配（payments.orderable），不再有"不配只记录"。"""
    client, settings, _ = setup_pay
    response = create(setup_pay)
    assert response.status_code == 201
    assert order(session, response).terms_version == settings.pay_terms_version == "pay-test"
    missing = client.post(
        "/api/v1/pay/orders", headers=HEADERS, json={"product_id": "workbench_30d", "code": "c"}
    )
    assert missing.status_code == 422
    outdated = create(setup_pay, terms_version="pay-2026-10")
    assert outdated.status_code == 409 and "已更新" in outdated.json()["detail"]


def test_submitting_without_a_pass_never_locks_the_pass_table(setup_pay, session):
    """开通收费前，小程序接口的数据库账号对 workbench_passes 只有读权限，SELECT ... FOR UPDATE 会被拒绝：
    没有使用期的用户提交任务时，不能对这张表（和订单表）发出加锁查询。有使用期时才加锁。"""
    statements = []

    def record(_conn, _cursor, statement, *_):
        statements.append(" ".join(statement.split()))

    engine = session.get_bind()
    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        assert submit(setup_pay, count=5, key="no-pass").status_code == 202
        locked = [
            s
            for s in statements
            if "FOR UPDATE" in s and ("workbench_passes" in s or "pay_orders" in s)
        ]
        assert locked == []
        delivered(setup_pay, session)
        statements.clear()
        assert submit(setup_pay, count=51, key="with-pass").json()["usageSource"] == "pass"
        assert any("FOR UPDATE" in s and "workbench_passes" in s for s in statements)
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)
