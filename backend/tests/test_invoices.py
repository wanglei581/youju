"""开票申请（D-058 第一期）：用户在订单页申请、改抬头；退款作废未开的申请；已开的不能自助退款；
管理台标记已开、不开；注销只断开用户。微信接口全部用 test_payments 的 FakeWechat。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.test_payments import HEADERS, NOW, create, delivered, order, run, setup_pay  # noqa: F401
from youju import payments
from youju.models import InvoiceRequest, User

ADMIN = {"X-Admin-Token": "admin-secret"}
COMPANY = {
    "title_type": "company",
    "title": "有据验收虚构科技有限公司",
    "tax_id": "91370212mA3xxx0001",
    "email": "finance@example.com",
}


@pytest.fixture
def pay(setup_pay):  # noqa: F811 - 用 test_payments 的夹具
    setup_pay[1].admin_token = "admin-secret"
    return setup_pay


def ask(pay, row, **values):
    return pay[0].put(
        f"/api/v1/pay/orders/{row.id}/invoice", headers=HEADERS, json={**COMPANY, **values}
    )


def test_request_edit_and_show_in_order(pay, session):
    row = delivered(pay, session)
    response = ask(pay, row)
    assert response.status_code == 200
    body = response.json()
    assert body["canInvoice"] is True
    # 税号统一成大写，用户自己的抬头和邮箱照原样带回
    assert body["invoice"] == {
        "status": "待开",
        "titleType": "company",
        "title": COMPANY["title"],
        "taxId": "91370212MA3XXX0001",
        "email": "finance@example.com",
        "note": None,
        "issuedAt": None,
        "canEdit": True,
    }
    # 待开时可以改：改成个人抬头，税号去掉；仍只有一份申请
    response = ask(pay, row, title_type="personal", title="  王某某 ", tax_id="91370212MA3XXX0001")
    assert response.status_code == 200
    assert response.json()["invoice"]["title"] == "王某某"
    assert response.json()["invoice"]["taxId"] is None
    assert len(session.scalars(select(InvoiceRequest)).all()) == 1
    listed = pay[0].get("/api/v1/pay/orders", headers=HEADERS).json()["items"][0]
    assert listed["invoice"]["status"] == "待开"


@pytest.mark.parametrize(
    "values,detail",
    [
        ({"tax_id": None}, "税号"),
        ({"tax_id": "123"}, "税号"),
        ({"tax_id": "91370212MA3XXX000-"}, "税号"),
        ({"email": "not-an-email"}, "邮箱"),
        ({"email": "a@b"}, "邮箱"),
        ({"title": "   "}, "抬头"),
        ({"title": "有据\n公司"}, "抬头"),
    ],
)
def test_invalid_invoice_fields(pay, session, values, detail):
    row = delivered(pay, session)
    response = ask(pay, row, **values)
    assert response.status_code == 422 and detail in response.json()["detail"]
    assert session.scalar(select(InvoiceRequest)) is None


def test_unknown_fields_and_foreign_orders_rejected(pay, session):
    row = delivered(pay, session)
    assert ask(pay, row, extra="x").status_code == 422
    # 别人的订单、不存在的订单：一律 404，不暴露是否存在
    other = {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": "someone-else"}
    response = pay[0].put(f"/api/v1/pay/orders/{row.id}/invoice", headers=other, json=COMPANY)
    assert response.status_code == 404
    response = pay[0].put("/api/v1/pay/orders/999/invoice", headers=HEADERS, json=COMPANY)
    assert response.status_code == 404


def test_pending_order_cannot_request_invoice(pay, session):
    row = order(session, create(pay))
    response = ask(pay, row)
    assert response.status_code == 422 and "已完成" in response.json()["detail"]
    detail = pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert detail["canInvoice"] is False and detail["invoice"] is None


def test_admin_issue_locks_edit_and_self_refund(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    listed = pay[0].get("/api/admin/invoices", headers=ADMIN).json()
    assert [item["orderId"] for item in listed] == [row.id]
    assert listed[0]["paidFee"] == 100 and listed[0]["status"] == "待开"
    assert pay[0].get("/api/admin/invoices", headers={"X-Admin-Token": "wrong"}).status_code == 401
    invoice_id = listed[0]["id"]
    response = pay[0].post(f"/api/admin/invoices/{invoice_id}/issue", headers=ADMIN)
    assert response.status_code == 200 and response.json() == {"status": "issued"}
    # 已开：用户不能再改，也不能自助退款（要先冲红）
    response = ask(pay, row, email="other@example.com")
    assert response.status_code == 409
    detail = pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert detail["invoice"]["status"] == "已开" and detail["invoice"]["canEdit"] is False
    assert detail["canRefund"] is False and "发票" in detail["refundUnavailableReason"]
    response = pay[0].post(f"/api/v1/pay/orders/{row.id}/refund", headers=HEADERS)
    assert response.status_code == 422 and "发票" in response.json()["detail"]
    # 重复标记：409
    response = pay[0].post(f"/api/admin/invoices/{invoice_id}/issue", headers=ADMIN)
    assert response.status_code == 409
    assert pay[0].get("/api/admin/invoices", headers=ADMIN).json() == []
    assert len(pay[0].get("/api/admin/invoices?status=all", headers=ADMIN).json()) == 1


def test_admin_reject_needs_note_shown_to_user(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    invoice_id = pay[0].get("/api/admin/invoices", headers=ADMIN).json()[0]["id"]
    response = pay[0].post(f"/api/admin/invoices/{invoice_id}/reject", headers=ADMIN, json={})
    assert response.status_code == 422
    response = pay[0].post(
        f"/api/admin/invoices/{invoice_id}/reject", headers=ADMIN, json={"note": "税号与抬头不符"}
    )
    assert response.status_code == 200
    detail = pay[0].get(f"/api/v1/pay/orders/{row.id}", headers=HEADERS).json()
    assert detail["invoice"]["status"] == "不开" and detail["invoice"]["note"] == "税号与抬头不符"


def test_self_refund_voids_pending_invoice(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    response = pay[0].post(f"/api/v1/pay/orders/{row.id}/refund", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["invoice"]["status"] == "不开"
    assert body["invoice"]["note"] == payments.INVOICE_VOID_NOTE
    assert body["canInvoice"] is False
    # 退款中、已退款的订单不能再申请
    assert ask(pay, row).status_code == 422
    # 管理台也不能再开
    invoice_id = session.scalar(select(InvoiceRequest.id))
    assert pay[0].post(f"/api/admin/invoices/{invoice_id}/issue", headers=ADMIN).status_code == 409


def test_platform_refund_completion_voids_pending_invoice(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    session.expire_all()
    row = session.get(type(row), row.id)
    # 苹果手机等由平台完成的退款：查单结果显示已退款
    assert payments.mark_refunded(session, row, {"status": 5, "left_fee": 0}, NOW)
    session.commit()
    invoice = session.scalar(select(InvoiceRequest))
    assert invoice.status == "rejected" and invoice.note == payments.INVOICE_VOID_NOTE


def test_admin_cannot_issue_after_refund_started(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    invoice_id = session.scalar(select(InvoiceRequest.id))
    row.status, row.refund_status = "refunding", "retry"
    session.commit()
    response = pay[0].post(f"/api/admin/invoices/{invoice_id}/issue", headers=ADMIN)
    assert response.status_code == 409


def test_account_deletion_keeps_invoice_without_user(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    user = session.scalar(select(User))
    payments.anonymize(session, user)
    session.commit()
    invoice = session.scalar(select(InvoiceRequest))
    assert invoice is not None and invoice.user_id is None and invoice.order_id == row.id


def test_invoice_still_available_when_payment_closed(pay, session):
    row = delivered(pay, session)
    pay[1].pay_enabled = False
    response = ask(pay, row)
    assert response.status_code == 200 and response.json()["invoice"]["status"] == "待开"


def test_overview_counts_pending_invoices(pay, session):
    row = delivered(pay, session)
    assert payments.overview(session)["invoices_pending"] == 0
    ask(pay, row)
    assert payments.overview(session)["invoices_pending"] == 1
    todos = pay[0].get("/api/admin/overview", headers=ADMIN).json()["todos"]
    assert todos["invoices"] == 1


def test_expired_orders_cleanup_removes_their_invoices(pay, session):
    row = delivered(pay, session)
    ask(pay, row)
    row.created_at = NOW - timedelta(days=3700)
    session.commit()
    payments.cleanup(session, pay[1], NOW)
    session.commit()
    assert session.scalar(select(InvoiceRequest)) is None


def test_runs_ignore_invoices(pay, session):
    # 补单、退款、对账的定时任务不碰开票申请
    row = delivered(pay, session)
    ask(pay, row)
    run(session, pay, NOW + timedelta(minutes=5))
    assert session.scalar(select(InvoiceRequest)).status == "pending"


def test_pay_entry_flags_in_capabilities_and_me(pay, session):
    client, settings, _ = pay
    caps = client.get("/api/v1/advisor/capabilities", headers=HEADERS).json()
    assert caps["pay"] == {"enabled": True, "hasOrders": False}
    assert client.get("/api/v1/me", headers=HEADERS).json()["pay"] == caps["pay"]
    delivered(pay, session)
    # 停卖后：购买入口关掉，已付款的用户仍能进"我的订单"
    settings.pay_enabled = False
    caps = client.get("/api/v1/advisor/capabilities", headers=HEADERS).json()
    assert caps["pay"] == {"enabled": False, "hasOrders": True}
    assert client.get("/api/v1/me", headers=HEADERS).json()["pay"] == caps["pay"]
    # 没配价格或 offerId 时不算在售
    settings.pay_enabled, settings.xpay_price_pass = True, None
    assert client.get("/api/v1/me", headers=HEADERS).json()["pay"]["enabled"] is False
    stranger = {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": "nobody"}
    assert client.get("/api/v1/me", headers=stranger).json()["pay"] == {
        "enabled": False,
        "hasOrders": False,
    }


def test_pay_tester_whitelist_hides_store_from_everyone_else(pay, session):
    client, settings, _ = pay
    settings.pay_audience, settings.pay_openids = "whitelist", ""
    # 名单为空：开关开着也没人能看到商品、下单
    assert client.get("/api/v1/pay/products", headers=HEADERS).status_code == 404
    assert create(pay).status_code == 404
    caps = client.get("/api/v1/advisor/capabilities", headers=HEADERS).json()
    assert caps["pay"] == {"enabled": False, "hasOrders": False}
    # 进了名单的测试人员才能买；别人照样看不到
    settings.pay_openids = " other-tester , pay-user "
    assert client.get("/api/v1/pay/products", headers=HEADERS).json()["items"]
    assert create(pay).status_code == 201
    stranger = {"X-WX-SOURCE": "test-cloud", "X-WX-OPENID": "someone-else"}
    assert client.get("/api/v1/pay/products", headers=stranger).status_code == 404
    assert client.get("/api/v1/pay/products").status_code == 404
    assert client.get("/api/v1/me", headers=stranger).json()["pay"]["enabled"] is False
    assert client.get("/api/v1/me", headers=HEADERS).json()["pay"]["enabled"] is True
    # 定时任务只看总开关：名单之外不影响已有订单的补单、对账
    assert payments.has_work(session, settings) is True
    assert payments.selling_to(settings, None) is False
    settings.pay_audience = "all"
    assert payments.selling_to(settings, "anyone") is True
    settings.pay_enabled = False
    assert payments.selling_to(settings, "pay-user") is False
