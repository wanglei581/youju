"""全部微信请求使用 MockTransport；禁止访问真实微信。

签名向量来源：微信官方 virtual-payment.html 的 2.6 Python 示例，
2026-10-04 取回原文。这里的密钥和 session_key 均为文档公开示例。
"""

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event as sa_event
from sqlalchemy import func, select

from youju import advisor_tasks, payments
from youju.api import advisor, me, pay, v1
from youju.api import app as app_module
from youju.cli import main
from youju.config import Settings
from youju.db import get_session
from youju.models import AdvisorTask, AdvisorUsage, PayEvent, PayOrder, PayReconciliation, User
from youju.xpay import XPayClient, XPayError, pay_signature, signature

NOW = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
HEADERS = {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": "pay-user"}


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
                        "env_type": body["env"],
                        "order_fee": self.fee,
                        "paid_fee": self.fee,
                        "left_fee": self.left_fee,
                        "paid_time": self.paid_time,
                        "wx_order_id": "wx-" + body["order_id"],
                        "order_type": self.order_type,
                        "channel_order_id": "mch-test",
                        "wxpay_order_id": "tx-test",
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
        advisor_enabled=True,
        trust_wx_headers=True,
        xpay_offer_id="offer-test",
        xpay_appkey_sandbox="12345",
        xpay_price_100=100,
        xpay_price_200=200,
        agreement_version="2026-10",
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
        client.post("/api/v1/me/agreement", headers=HEADERS, json={"version": "2026-10"})
        with session_factory() as session:
            user = session.scalar(select(User))
            user.advisor_agreement_version = advisor_tasks.ADVISOR_AGREEMENT_VERSION
            user.advisor_agreement_at = NOW
            session.commit()
        yield client, settings, fake
    app_module.app.dependency_overrides.clear()
    fake.http.close()


def create(setup_pay, **values):
    client, _, _ = setup_pay
    return client.post(
        "/api/v1/pay/orders",
        headers=HEADERS,
        json={
            "product_id": "compare_100",
            "rows": ["示例科技有限公司"],
            "code": "test-login",
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
        "Env": 1,
        "WeChatPayInfo": {
            "MchOrderNo": "mch-test",
            "TransactionId": "tx-test",
            "PaidTime": int(NOW.timestamp()),
        },
        "GoodsInfo": {
            "ProductId": "compare_100",
            "Quantity": 1,
            "OrigPrice": 100,
            "ActualPrice": 100,
            "Attach": order.out_trade_no,
        },
    }


def push(setup_pay, data):
    return setup_pay[0].post("/api/wx/push", headers=HEADERS, json=data)


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
    assert not config.pay_enabled and config.xpay_env == 1
    assert not payments.products(config)
    path = tmp_path / "payment.env"
    path.write_text("YOUJU_WX_APPSECRET=dotenv-test\nYOUJU_XPAY_APPKEY_SANDBOX=dotenv-test\n")
    assert Settings(_env_file=path).wx_appsecret is None
    assert Settings(_env_file=path).xpay_appkey_sandbox is None
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
        "env": 1,
        "currencyType": "CNY",
        "productId": "compare_100",
        "goodsPrice": 100,
        "outTradeNo": row.out_trade_no,
        "attach": row.out_trade_no,
    }
    assert data["paySig"] == pay_signature("12345", "requestVirtualPayment", data["signData"])
    assert data["signature"] == signature("9hAb/NEYUlkaMBEsmFgzig==", data["signData"])
    assert row.price == 100 and row.row_limit == 100
    assert create(setup_pay).status_code == 201
    assert create(setup_pay).status_code == 201
    assert create(setup_pay).status_code == 429


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
    setup_pay[1].xpay_price_100 = None
    assert create(setup_pay).status_code == 404


def test_products_lists_only_priced_items(setup_pay, session):
    client, settings, fake = setup_pay
    response = client.get("/api/v1/pay/products", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"productId": "compare_100", "price": 100, "maxRows": 100},
            {"productId": "compare_200", "price": 200, "maxRows": 200},
        ]
    }
    # 只读：不建用户、不建订单、不访问微信。
    assert session.scalar(select(func.count(PayOrder.id))) == 0
    settings.xpay_product_200 = "wx_goods_200"
    settings.xpay_price_100 = None
    assert client.get("/api/v1/pay/products").json() == {
        "items": [{"productId": "wx_goods_200", "price": 200, "maxRows": 200}]
    }
    settings.xpay_price_200 = None
    assert client.get("/api/v1/pay/products").json() == {"items": []}
    settings.xpay_price_100 = 100
    settings.xpay_offer_id = ""
    # 没有 offerId 时下单必然 404，商品列表也为空，小程序不显示价格。
    assert client.get("/api/v1/pay/products").json() == {"items": []}
    assert create(setup_pay).status_code == 404
    assert not fake.calls


def test_products_route_dependency_hides_when_closed(setup_pay):
    settings = setup_pay[1]
    settings.pay_enabled = False
    with pytest.raises(pay.HTTPException) as caught:
        pay.require_enabled()
    assert caught.value.status_code == 404 and caught.value.detail == "没有找到接口"
    route = next(r for r in pay.router.routes if r.path == "/api/v1/pay/products")
    assert route.methods == {"GET"}
    assert any(d.dependency is pay.require_enabled for d in route.dependencies)


def test_login_identity_must_match(setup_pay, session):
    setup_pay[2].wrong_owner = True
    assert create(setup_pay).status_code == 503
    assert session.scalar(select(func.count(PayOrder.id))) == 0


def test_push_15_times_one_task_no_free_quota(setup_pay, session):
    row = delivered(setup_pay, session)
    for _ in range(14):
        assert push(setup_pay, payload(row)).json() == {"ErrCode": 0, "ErrMsg": "success"}
    assert session.scalar(select(func.count(AdvisorTask.id))) == 1
    assert session.scalar(select(func.count(AdvisorUsage.id))) == 0
    task = session.get(AdvisorTask, row.task_id)
    assert task.pay_order_id == row.id
    usage = AdvisorUsage(user_id=row.user_id, kind="compare", day=NOW.date(), used_count=1)
    session.add(usage)
    session.commit()
    advisor_tasks.release_usage(session, task)
    assert usage.used_count == 1


@pytest.mark.parametrize(
    "mismatch", ["price", "product", "owner", "env", "trade", "wx", "quantity", "unpaid"]
)
def test_push_requires_matching_query(setup_pay, session, mismatch):
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status = 2
    data = payload(row)
    if mismatch == "price":
        fake.fee = 101
    elif mismatch == "product":
        data["GoodsInfo"]["ProductId"] = "wrong"
    elif mismatch == "owner":
        data["OpenId"] = "wrong"
    elif mismatch == "env":
        data["Env"] = 0
    elif mismatch == "trade":
        fake.bad_order = True
    elif mismatch == "wx":
        data["WeChatPayInfo"]["TransactionId"] = "wrong"
    elif mismatch == "quantity":
        data["GoodsInfo"]["Quantity"] = 2
    elif mismatch == "unpaid":
        fake.status = 1
    assert push(setup_pay, data).json()["ErrCode"] != 0
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0


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
    assert row.task_id and row.goods_notified
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.finished_at = "done", NOW
    session.commit()
    run(session, setup_pay)
    assert row.status == "completed"
    response = setup_pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS)
    assert response.json()["status"] == "已完成"


def test_refund_failure_retry_same_id_and_completion(setup_pay, session):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.attempts = "failed", 3
    session.commit()
    fake = setup_pay[2]
    fake.fail_refund = True
    run(session, setup_pay)
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
    fake.status, fake.left_fee = 5, 0
    run(session, setup_pay, advisor_tasks._utc(row.refund_retry_at))
    assert row.status == "refunded"


def test_ios_rerun_once_and_manual_alert(setup_pay, session):
    setup_pay[2].order_type = 7
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    original_rows = list(task.input_rows)
    task.status = "failed"
    session.commit()
    run(session, setup_pay)
    assert row.rerun_count == 1
    rerun = session.get(AdvisorTask, row.task_id)
    assert rerun.id != task.id and rerun.input_rows == original_rows
    rerun.status = "failed"
    session.commit()
    run(session, setup_pay)
    assert row.status == "manual" and row.alerted_at
    run(session, setup_pay)
    assert row.rerun_count == 1
    assert not any(path == "/xpay/refund_order" for path, _, _ in setup_pay[2].calls)


def test_ios_refund_inquiry_no_network_and_evidence(setup_pay, session):
    setup_pay[2].order_type = 7
    row = delivered(setup_pay, session)
    before = len(setup_pay[2].calls)
    data = {"Event": "xpay_subscribe_ios_refund_query_notify", "pay_order_id": row.wx_order_id}
    assert push(setup_pay, data).json()["result_code"] == 0
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.finished_at = "done", NOW
    session.commit()
    response = push(setup_pay, data).json()
    assert response["result_code"] == 1
    assert str(task.id) in response["evidence"] and NOW.isoformat() in response["evidence"]
    assert len(setup_pay[2].calls) == before


def test_unknown_platform_no_refund(setup_pay, session):
    setup_pay[2].order_type = 99
    row = delivered(setup_pay, session)
    session.get(AdvisorTask, row.task_id).status = "failed"
    session.commit()
    run(session, setup_pay)
    assert row.status == "manual"
    assert not any(path == "/xpay/refund_order" for path, _, _ in setup_pay[2].calls)


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
        json={"version": "2026-10"},
        headers={**HEADERS, "X-WX-OPENID": "other"},
    )
    result = setup_pay[0].get(
        f"/api/v1/pay/orders/{response.json()['orderId']}",
        headers={**HEADERS, "X-WX-OPENID": "other"},
    )
    assert result.status_code == 404


def test_paid_task_attempts_before_refund(setup_pay, session):
    row = delivered(setup_pay, session)
    settings = setup_pay[1]
    # 未配置存储是稳定失败；现有规则仍为初次 + 两次重试。
    for attempt in range(1, 4):
        advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)
        task = session.get(AdvisorTask, row.task_id)
        assert task.attempts == attempt
        assert task.status == ("failed" if attempt == 3 else "queued")
        assert session.scalar(select(func.count(AdvisorUsage.id))) == 0
    run(session, setup_pay)
    assert row.status == "refunding"


def test_cleanup_and_account_anonymization(setup_pay, session):
    row = delivered(setup_pay, session)
    user_id = row.user_id
    response = setup_pay[0].delete("/api/v1/me", headers=HEADERS)
    assert response.status_code == 204
    session.expire_all()
    assert not session.get(User, user_id)
    assert row.user_id is None and row.input_rows is None and row.pseudonym
    assert row.price == 100 and row.out_trade_no
    for e in session.scalars(select(PayEvent)):
        assert "OpenId" not in e.summary and "openid" not in e.summary
    old = PayOrder(
        out_trade_no="old",
        product_id="compare_100",
        price=100,
        row_limit=100,
        env=1,
        input_rows=["过期企业"],
        created_at=NOW - timedelta(days=8),
    )
    session.add(old)
    session.add(PayEvent(kind="old", summary={}, created_at=NOW - timedelta(days=181)))
    session.commit()
    result = payments.cleanup(session, setup_pay[1], NOW)
    session.commit()
    assert old.input_rows is None and result["pay_events"] == 1


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


def test_ios_rerun_keeps_original_retention(setup_pay, session):
    setup_pay[2].order_type = 7
    row = delivered(setup_pay, session)
    created = row.created_at
    session.get(AdvisorTask, row.task_id).status = "failed"
    session.commit()
    run(session, setup_pay, NOW + timedelta(days=6))
    rerun = session.get(AdvisorTask, row.task_id)
    assert rerun.created_at == created
    advisor_tasks.cleanup_advisor(session, settings=setup_pay[1], stamp=NOW + timedelta(days=8))
    payments.cleanup(session, setup_pay[1], NOW + timedelta(days=8))
    session.commit()
    assert row.input_rows is None and rerun.input_rows is None


def test_paid_queue_disabled_preserves_free_queue(setup_pay, session):
    row = delivered(setup_pay, session)
    free = AdvisorTask(
        user_id=row.user_id,
        kind="compare",
        input_rows=["免费公司"],
        row_count=1,
        idempotency_key="free-example",
        created_at=NOW,
    )
    session.add(free)
    session.commit()
    setup_pay[1].pay_enabled = False
    advisor_tasks.run_advisor_tasks(session, settings=setup_pay[1], stamp=NOW, max_tasks=1)
    assert session.get(AdvisorTask, row.task_id).attempts == 0
    assert free.attempts == 1


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


def test_anonymous_account_financial_retention(setup_pay, session):
    row = delivered(setup_pay, session)
    row.status, row.task_completed_at = "completed", NOW
    session.commit()
    setup_pay[0].delete("/api/v1/me", headers=HEADERS)
    session.expire_all()
    assert row.task_completed_at == NOW
    payments.cleanup(session, setup_pay[1], NOW + timedelta(days=3651))
    session.commit()
    assert session.get(PayOrder, row.id) is None


def test_concurrent_delivery_transactions_are_idempotent(setup_pay, session, session_factory):
    from concurrent.futures import ThreadPoolExecutor

    row = order(session, create(setup_pay))
    row_id = row.id
    setup_pay[2].status = 2

    def deliver(_):
        with session_factory() as tx:
            locked = tx.scalar(select(PayOrder).where(PayOrder.id == row_id).with_for_update())
            checked = payments.query(tx, locked, setup_pay[2].client)
            assert payments.accept_payment(tx, locked, checked, setup_pay[1], NOW)
            tx.commit()
            return locked.task_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(deliver, range(15)))
    assert len(set(ids)) == 1
    assert session.scalar(select(func.count(AdvisorTask.id))) == 1


def test_200_row_paid_task_executes_existing_compare(setup_pay, session, monkeypatch):
    rows = [f"示例科技{i}有限公司" for i in range(200)]
    response = create(setup_pay, product_id="compare_200", rows=rows)
    assert response.status_code == 201
    row = order(session, response)
    setup_pay[2].status, setup_pay[2].fee, setup_pay[2].left_fee = 2, 200, 200
    data = payload(row)
    data["GoodsInfo"].update(ProductId="compare_200", OrigPrice=200, ActualPrice=200)
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    setup_pay[1].cloud_env = "fake-cloud"
    monkeypatch.setattr(advisor_tasks, "upload", lambda *a, **kw: "fake-result-id")
    report = advisor_tasks.run_advisor_tasks(session, settings=setup_pay[1], stamp=NOW, max_tasks=1)
    assert report["done"] == 1
    task = session.get(AdvisorTask, row.task_id)
    assert task.row_count == task.result_rows == 200
    assert session.scalar(select(func.count(AdvisorUsage.id))) == 0


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
    assert response.json()["result_code"] == 0
    assert "超时" in response.json()["evidence"]


def test_payment_after_account_deletion_goes_to_manual_without_identity(setup_pay, session):
    row = order(session, create(setup_pay))
    data = payload(row)
    assert setup_pay[0].delete("/api/v1/me", headers=HEADERS).status_code == 204
    setup_pay[2].status = 2
    assert push(setup_pay, data).json()["ErrCode"] == 0
    session.expire_all()
    assert row.status == "manual" and row.paid_fee == 100
    assert row.user_id is None and row.input_rows is None
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    for e in session.scalars(select(PayEvent)):
        assert "OpenId" not in e.summary and "openid" not in e.summary


@pytest.mark.parametrize("state", ["queued", "running", "done"])
def test_review_1_refund_revokes_every_task_state(setup_pay, session, state):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.result_file_id = state, "fake-result"
    session.commit()
    assert payments.mark_refunded(session, row, {"status": 8, "left_fee": 0}, NOW)
    session.commit()
    assert task.status == ("done" if state == "done" else "cancelled")
    client = setup_pay[0]
    for suffix in ("", "/download"):
        response = client.get(f"/api/v1/advisor/tasks/{task.id}{suffix}", headers=HEADERS)
        assert response.status_code == 410
        assert response.json()["detail"] == "订单已退款，结果不可用"
    item = client.get("/api/v1/advisor/tasks", headers=HEADERS).json()["items"][0]
    assert item["error"] == "订单已退款，结果不可用"
    assert not item.get("hasFile") and not item.get("resultRows")


@pytest.mark.parametrize("state", ["refunding", "refunded"])
def test_review_1_refunded_order_cannot_be_claimed(setup_pay, session, state):
    row = delivered(setup_pay, session)
    row.status = state
    session.commit()
    advisor_tasks.run_advisor_tasks(session, settings=setup_pay[1], stamp=NOW, max_tasks=1)
    assert session.get(AdvisorTask, row.task_id).attempts == 0


@pytest.mark.parametrize("state", ["refunding", "refunded"])
def test_review_1_execution_checks_order_before_engine(setup_pay, session, monkeypatch, state):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.attempts, row.status = "running", 1, state
    session.commit()
    setup_pay[1].cloud_env = "fake-cloud"
    engines = []
    monkeypatch.setattr(advisor_tasks, "_run_engine", lambda *a: engines.append(True))
    assert not advisor_tasks.process_task(session, task, settings=setup_pay[1], stamp=NOW)
    assert not engines and task.status == "cancelled"


def test_review_1_refund_during_upload_discards_result(
    setup_pay, session, session_factory, monkeypatch
):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.attempts = "running", 1
    session.commit()
    setup_pay[1].cloud_env = "fake-cloud"
    deleted = []

    def refund_during_upload(*args, **kwargs):
        with session_factory() as other:
            refunded = other.scalar(select(PayOrder).where(PayOrder.id == row.id).with_for_update())
            payments.mark_refunded(other, refunded, {"status": 5, "left_fee": 0}, NOW)
            other.commit()
        return "unusable-upload"

    monkeypatch.setattr(advisor_tasks, "upload", refund_during_upload)
    monkeypatch.setattr(
        advisor_tasks, "delete_result_files", lambda c, e, ids: deleted.extend(ids) or ids
    )
    assert not advisor_tasks.process_task(session, task, settings=setup_pay[1], stamp=NOW)
    session.expire_all()
    assert task.status == "cancelled" and task.result_file_id is None
    assert row.status == "refunded" and deleted == ["unusable-upload"]


def test_review_2_success_completes_order_in_same_commit(setup_pay, session, monkeypatch):
    from sqlalchemy import event

    row = delivered(setup_pay, session)
    setup_pay[1].cloud_env = "fake-cloud"
    monkeypatch.setattr(advisor_tasks, "upload", lambda *a, **kw: "fake-result")
    checked = []

    def before_commit(tx):
        task = tx.get(AdvisorTask, row.task_id)
        if task.status == "done":
            assert row.status == "completed" and row.task_completed_at == task.finished_at
            checked.append(True)

    event.listen(session, "before_commit", before_commit)
    try:
        assert (
            advisor_tasks.run_advisor_tasks(session, settings=setup_pay[1], stamp=NOW, max_tasks=1)[
                "done"
            ]
            == 1
        )
    finally:
        event.remove(session, "before_commit", before_commit)
    assert checked and row.status == "completed"


def test_review_2_deleting_done_task_syncs_order_first(setup_pay, session):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.finished_at = "done", NOW
    session.commit()
    assert (
        setup_pay[0].delete(f"/api/v1/advisor/tasks/{task.id}", headers=HEADERS).status_code == 204
    )
    session.expire_all()
    assert row.status == "completed" and row.task_completed_at == NOW
    run(session, setup_pay)
    assert not any(path == "/xpay/refund_order" for path, _, _ in setup_pay[2].calls)


def test_review_2_missing_task_is_manual_and_alerted(setup_pay, session):
    row = delivered(setup_pay, session)
    session.delete(session.get(AdvisorTask, row.task_id))
    session.commit()
    run(session, setup_pay)
    assert row.status == "manual" and row.alerted_at
    assert payments.overview(session)["manual"] == 1
    assert not any(path == "/xpay/refund_order" for path, _, _ in setup_pay[2].calls)


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
    assert row.status == "processing" and row.task_id


@pytest.mark.parametrize("wx_status", [0, 1, 6, 2])
def test_review_3_pending_after_48_hours_closes_or_is_manual(setup_pay, session, wx_status):
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(hours=49)
    session.commit()
    setup_pay[2].status = wx_status
    run(session, setup_pay)
    assert row.status == ("manual" if wx_status == 2 else "closed")
    assert row.task_id is None
    if wx_status == 2:
        assert row.alerted_at and row.paid_fee == row.price


def test_review_3_bill_paid_but_not_delivered_goes_manual(setup_pay, session):
    row = order(session, create(setup_pay))
    payments.reconcile(session, NOW.date(), {row.out_trade_no: row.price}, NOW)
    assert row.status == "manual" and not row.task_id
    run(session, setup_pay)
    assert row.alerted_at and payments.overview(session)["manual"] == 1


@pytest.mark.parametrize("key", ["pay:guessed:0", " pay:guessed:0 "])
def test_review_4_user_cannot_reserve_payment_keys(setup_pay, session, key):
    response = setup_pay[0].post(
        "/api/v1/advisor/tasks",
        headers={**HEADERS, "Idempotency-Key": key},
        json={"kind": "compare", "rows": ["测试公司"]},
    )
    assert response.status_code == 422
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    assert session.scalar(select(func.count(AdvisorUsage.id))) == 0


@pytest.mark.parametrize("wx_status", [5, 8])
def test_review_5_processing_polls_lost_refund_notification(setup_pay, session, wx_status):
    row = delivered(setup_pay, session)
    setup_pay[2].status, setup_pay[2].left_fee = wx_status, 0
    run(session, setup_pay, NOW + timedelta(minutes=11))
    assert row.status == "refunded"
    assert session.get(AdvisorTask, row.task_id).status == "cancelled"


@pytest.mark.parametrize("state", ["processing", "completed", "closed", "manual", "refunding"])
def test_review_5_refund_push_handles_all_local_states(setup_pay, session, state):
    row = delivered(setup_pay, session)
    row.status = state
    session.commit()
    setup_pay[2].status, setup_pay[2].left_fee = 8, 0
    response = push(
        setup_pay,
        {
            "Event": "xpay_refund_notify",
            "OpenId": "pay-user",
            "MchOrderId": row.out_trade_no,
            "WxOrderId": row.wx_order_id,
        },
    )
    assert response.json()["ErrCode"] == 0
    session.expire_all()
    assert row.status == "refunded"


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
                "product_id": "compare_100",
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


def test_paid_task_cannot_be_cancelled_and_is_marked_paid(setup_pay, session):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    assert task.status == "queued"
    client = setup_pay[0]
    response = client.post(f"/api/v1/advisor/tasks/{task.id}/cancel", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "付费任务不能取消，失败会自动退款"
    session.expire_all()
    assert session.get(AdvisorTask, task.id).status == "queued"
    item = client.get("/api/v1/advisor/tasks", headers=HEADERS).json()["items"][0]
    assert item["paid"] is True


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
    assert row.status == "manual" and row.task_id is None and row.alerted_at
    assert ("金额" if problem == "fee" else "支付时间") in row.error
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0
    assert not _calls(fake, "/xpay/notify_provide_goods")


def test_fix_2_push_amount_mismatch_manual_and_alerted_once(setup_pay, session):
    row = order(session, create(setup_pay))
    fake = setup_pay[2]
    fake.status, fake.fee = 2, 101
    assert push(setup_pay, payload(row)).json()["ErrCode"] != 0
    session.expire_all()
    assert row.status == "manual" and row.alerted_at is None and row.task_id is None
    run(session, setup_pay)
    alerted = row.alerted_at
    assert alerted
    # 微信重推同一笔：仍不发货，也不重复告警。
    assert push(setup_pay, payload(row)).json()["ErrCode"] != 0
    session.expire_all()
    assert row.status == "manual" and row.alerted_at == alerted
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0


def test_fix_3_unconfirmable_pending_closes_after_48_hours(setup_pay, session):
    row = order(session, create(setup_pay))
    row.created_at = NOW - timedelta(hours=47)
    session.commit()
    setup_pay[2].bad_order = True
    run(session, setup_pay)
    assert row.status == "pending"
    row.created_at, row.query_retry_at = NOW - timedelta(hours=49), None
    session.commit()
    run(session, setup_pay)
    assert row.status == "closed" and row.error == "超过 48 小时无法确认支付，已关闭"
    assert run(session, setup_pay, NOW + timedelta(hours=1))["processed"] == 0


def test_fix_3_refund_and_alert_orders_come_before_pending(setup_pay, session):
    failed = delivered(setup_pay, session)
    session.get(AdvisorTask, failed.task_id).status = "failed"
    user = session.scalar(select(User))
    common = {
        "user_id": user.id,
        "product_id": "compare_100",
        "price": 100,
        "row_limit": 100,
        "env": 1,
        "input_rows": ["示例科技有限公司"],
        "created_at": NOW - timedelta(hours=1),
    }
    refunding = PayOrder(
        out_trade_no="refunding",
        status="refunding",
        refund_status="retry",
        platform="android",
        updated_at=NOW,
        **common,
    )
    manual = PayOrder(
        out_trade_no="manual", status="manual", error="测试", updated_at=NOW, **common
    )
    pending = [
        PayOrder(out_trade_no=f"pending{i}", updated_at=NOW - timedelta(days=1), **common)
        for i in range(100)
    ]
    session.add_all([refunding, manual, *pending])
    failed.updated_at = NOW
    session.commit()
    assert run(session, setup_pay)["processed"] == 100
    assert refunding.refund_attempts == 1 and failed.refund_attempts == 1
    assert manual.alerted_at is not None


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
            alert=alerts.append,
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


@pytest.mark.parametrize("toggle", ["pay_enabled", "advisor_enabled"])
def test_fix_5_closed_switch_still_finishes_open_orders(setup_pay, session, toggle):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.attempts = "failed", 3
    session.commit()
    setattr(setup_pay[1], toggle, False)
    assert create(setup_pay).status_code == 404
    assert setup_pay[0].get("/api/v1/pay/products", headers=HEADERS).status_code == 404
    assert run(session, setup_pay)["processed"] == 1
    assert row.status == "refunding" and row.refund_status == "pending"
    assert len(_calls(setup_pay[2], "/xpay/refund_order")) == 1


def test_fix_5_closed_switch_paid_task_waits_then_manual(setup_pay, session):
    row = delivered(setup_pay, session)
    settings = setup_pay[1]
    settings.pay_enabled = False
    advisor_tasks.run_advisor_tasks(session, settings=settings, stamp=NOW, max_tasks=1)
    assert session.get(AdvisorTask, row.task_id).attempts == 0
    run(session, setup_pay, NOW + timedelta(minutes=9))
    assert row.status == "processing" and row.alerted_at is None
    run(session, setup_pay, NOW + timedelta(minutes=11))
    assert row.status == "manual" and row.alerted_at and "未执行" in row.error
    assert not _calls(setup_pay[2], "/xpay/refund_order")
    assert session.get(AdvisorTask, row.task_id).status == "queued"


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


def test_fix_6_delete_paid_task_locks_order_before_task(setup_pay, session, db_engine):
    row = delivered(setup_pay, session)
    task = session.get(AdvisorTask, row.task_id)
    task.status, task.finished_at = "done", NOW
    session.commit()
    statements, stop = _capture_sql(db_engine)
    try:
        response = setup_pay[0].delete(f"/api/v1/advisor/tasks/{task.id}", headers=HEADERS)
    finally:
        stop()
    assert response.status_code == 204
    locks = [sql for sql in statements if "FOR UPDATE" in sql]
    order_lock = next(i for i, sql in enumerate(locks) if "FROM PAY_ORDERS" in sql)
    task_lock = next(i for i, sql in enumerate(locks) if "FROM ADVISOR_TASKS" in sql)
    assert order_lock < task_lock
    session.expire_all()
    assert row.status == "completed"


@pytest.mark.parametrize("job", ["pay-run", "pay-reconcile", "advisor-run"])
def test_fix_7_pay_run_is_not_recorded(tmp_path, job):
    from youju import jobs

    assert "pay-run" not in jobs.OVERVIEW_JOBS and "pay-run" in jobs.JOB_NAMES
    assert "pay-reconcile" in jobs.OVERVIEW_JOBS
    for name, content in {
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "flock": '#!/bin/sh\nshift 4\nexec "$@"\n',
        "youju": '#!/bin/sh\necho "$*" >> "$CALLS"\n[ "$1" = job-start ] && echo 42\nexit 0\n',
    }.items():
        (tmp_path / name).write_text(content)
        (tmp_path / name).chmod(0o755)
    script = (Path(__file__).resolve().parents[1] / "deploy" / "job.sh").read_text()
    script = script.replace(". /app/backend/deploy/env.sh", ":").replace("cd /app/backend", ":")
    script = script.replace("/proc/1/fd/1", str(tmp_path / "output"))
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
    assert row.status == "processing" and row.provide_attempts == 19
    run(session, setup_pay)
    assert len(_calls(fake, "/xpay/notify_provide_goods")) == 20
    assert row.provide_attempts == 20 and not row.goods_notified
    assert row.status == "manual" and row.alerted_at and "通知发货" in row.error
    run(session, setup_pay)
    assert len(_calls(fake, "/xpay/notify_provide_goods")) == 20
