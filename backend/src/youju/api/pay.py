"""默认关闭的批量工具支付接口（30 天、90 天两档，D-070）及云托管消息推送。"""

import asyncio
import json
import re
from datetime import timedelta
from typing import Annotated, Literal
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from youju import payments
from youju.api.advisor import SessionDep, _body_size, _stamp, _upload_consent
from youju.api.me import KnownUserDep, UserDep, request_openid
from youju.config import get_settings
from youju.models import InvoiceRequest, PayOrder, User
from youju.xpay import XPayClient, XPayError, encode, pay_signature, signature


def require_enabled(request: Request) -> None:
    # 在售才有商品和下单：总开关开着，且对全体开放或这个用户在测试名单里
    try:
        openid = request_openid(request)
    except HTTPException:
        openid = None
    if not payments.selling_to(get_settings(), openid):
        raise HTTPException(status_code=404, detail="没有找到接口")


# 开关只停卖：商品和下单受开关控制；订单列表、详情、自助退款不受——
# 关掉收费以后，已经付过款的用户还要能看到订单、退款。
router = APIRouter(prefix="/api/v1/pay", tags=["支付"])


def require_push_service() -> None:
    # 推送路径只在接收云托管推送的服务上存在；不受支付开关控制，配置检测和非支付消息要能应答。
    if not get_settings().trust_wx_headers:
        raise HTTPException(status_code=404, detail="没有找到接口")


push_router = APIRouter(dependencies=[Depends(require_push_service)])
PAY_EVENTS = {
    "xpay_goods_deliver_notify",
    "xpay_refund_notify",
    "xpay_complaint_notify",
    "xpay_subscribe_ios_refund_query_notify",
}


def wx_client():
    with httpx.Client(trust_env=False) as http:
        yield XPayClient(get_settings(), http)


WxDep = Annotated[XPayClient, Depends(wx_client)]


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=512, repr=False)
    # 用户勾选同意的《付费服务协议》版本，随订单保存，日后有争议时能说明当时同意的是哪一版
    terms_version: str = Field(min_length=1, max_length=40)


@router.get("/products", dependencies=[Depends(require_enabled)])
def list_products(request: Request, session: SessionDep) -> dict:
    """只读：已配置价格、可以下单的档位（批量工具 30 天、90 天，按天数从短到长）；
    小程序据此显示付费档，空列表时不显示。renewBonus：和价格同一屏写的续买规则，开关关着为 None。"""
    settings = get_settings()
    # 和下单条件一致（payments.orderable）：下不了单就不展示价格。
    if not payments.orderable(settings):
        return {"items": [], "sandbox": settings.xpay_env == 1, "renewBonus": None}
    # 看价格不要求登录；认得出是谁时才算"现在续买送不送"
    try:
        openid = request_openid(request)
    except HTTPException:
        openid = None
    user = session.scalar(select(User).where(User.openid == openid)) if openid else None
    return {
        "items": [
            {
                "productId": product,
                "name": payments.pass_name(days),
                "price": price,
                "days": days,
            }
            for product, (price, days) in payments.products(settings).items()
        ],
        # 沙箱（不扣真钱、只有测试名单看得到）：小程序在《付费服务协议》定稿前也放开付款按钮供联调
        "sandbox": settings.xpay_env == 1,
        "renewBonus": payments.renew_offer(session, settings, user, _stamp()),
    }


@router.post(
    "/orders", status_code=201, dependencies=[Depends(require_enabled), Depends(_body_size)]
)
def create_order(body: OrderIn, session: SessionDep, user: UserDep, client: WxDep) -> dict:
    settings = get_settings()
    _upload_consent(user)
    sku = payments.products(settings).get(body.product_id)
    if sku is None or not payments.orderable(settings):
        raise HTTPException(status_code=404, detail="没有找到商品")
    price, days = sku
    if settings.pay_terms_version and body.terms_version != settings.pay_terms_version:
        raise HTTPException(
            status_code=409, detail="《付费服务协议》已更新，请重新阅读并同意后再付款"
        )
    stamp = _stamp()
    # 和免费任务使用同一用户锁，防止并发下单绕过限制及与注销竞态。
    if session.scalar(select(User.id).where(User.id == user.id).with_for_update()) is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
    # 下新单前先问微信：10 分钟内的待支付单可能已经付了款，只是推送和补单还没到
    # （取消回调不准、付款中途小程序被关掉时，小程序本机没记下这一笔）。付了就先开通、不下新单。
    # 查不了不挡新单：没调起过支付的单（下单回应没送到手机）微信那边可能查不到，
    # 官方错误码里也没有「订单不存在」可以区分；这类单交给每分钟的补单处理。
    # 候选先普通查（不加范围锁：两个新用户同时下单时，范围锁加插入会互相等待甚至死锁），
    # 再按主键逐个加锁、复核状态；推送或补单刚开通的这里会看到不是待支付，跳过。
    recent = session.scalars(
        select(PayOrder.id)
        .where(
            PayOrder.user_id == user.id,
            PayOrder.status == "pending",
            PayOrder.created_at > stamp - timedelta(minutes=10),
        )
        .order_by(PayOrder.id)
    ).all()
    for old_id in recent:
        old = session.scalar(
            select(PayOrder)
            .where(PayOrder.id == old_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if old is None or old.status != "pending":
            continue
        try:
            data = payments.query(session, old, client)
        except XPayError:
            continue
        if data.get("status") == 6:
            old.status, old.updated_at = "closed", stamp
            continue
        payments.accept_payment(session, old, data, settings, stamp)
        if old.status != "pending":
            session.commit()
            raise HTTPException(
                status_code=409,
                detail="上一笔已经付款，正在为你开通，可到“我的订单”查看"
                if old.status == "completed"
                else "上一笔付款需要人工核对，请点“遇到问题”告诉我们",
            )
    count = session.scalar(
        select(func.count(PayOrder.id)).where(
            PayOrder.user_id == user.id,
            PayOrder.status == "pending",
            PayOrder.created_at > stamp - timedelta(minutes=10),
        )
    )
    if count >= 3:
        raise HTTPException(status_code=429, detail="待支付订单过多，请稍后再试")
    trade = uuid4().hex
    raw = encode(
        {
            "offerId": settings.xpay_offer_id,
            "buyQuantity": 1,
            "env": settings.xpay_env,
            "currencyType": "CNY",
            "productId": body.product_id,
            "goodsPrice": price,
            "outTradeNo": trade,
            "attach": trade,
        }
    )
    try:
        pay_sig = pay_signature(client.appkey(settings.xpay_env), "requestVirtualPayment", raw)
        # session_key 仅用于此表达式，不赋给订单、日志、异常或任何持久对象。
        user_sig = signature(client.session_key(body.code, user.openid), raw)
    except XPayError:
        raise HTTPException(status_code=503, detail="支付暂不可用，请稍后重试") from None
    order = PayOrder(
        user_id=user.id,
        out_trade_no=trade,
        product_id=body.product_id,
        price=price,
        days=days,
        # 下单时续买多送开着才算数（付款页这时写着这条规则）；付款时间符合才真的送
        bonus_days=payments.RENEW_BONUS_DAYS if payments.renew_bonus_enabled(settings) else 0,
        env=settings.xpay_env,
        terms_version=body.terms_version,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(order)
    session.commit()
    return {
        "orderId": order.id,
        "signData": raw,
        "paySig": pay_sig,
        "signature": user_sig,
        "mode": "short_series_goods",
    }


def order_out(session: Session, order: PayOrder) -> dict:
    from youju import rewards

    ticket = payments.order_pass(session, order)
    reason = payments.refund_reason(session, order, _stamp())
    invoice = payments.order_invoice(session, order)
    return {
        "orderId": order.id,
        "name": payments.pass_name(order.days),
        "days": order.days,
        "status": payments.STATUS_LABELS[order.status],
        "amount": order.price,
        "paidAt": order.paid_time,
        "passStartsAt": ticket.starts_at if ticket else None,
        "passEndsAt": ticket.ends_at if ticket else None,
        "passRevokedAt": ticket.revoked_at if ticket else None,
        # 随这笔送的天数，每次一笔（续买多送、加量体验折算）；收回的写着已收回
        "gifts": [
            {
                "kind": gift.kind,
                "name": rewards.LABELS[gift.kind],
                "days": gift.granted_days,
                "startsAt": pass_.starts_at if pass_ else None,
                "endsAt": pass_.ends_at if pass_ else None,
                "revoked": rewards.is_revoked(gift)
                or (pass_ is not None and pass_.revoked_at is not None),
            }
            for gift, pass_ in payments.order_gifts(session, order)
        ],
        # 退了多少（分）：按剩余天数退的少于订单金额；没退款的为 None
        "refundAmount": order.refund_fee if order.refund_status is not None else None,
        "canRefund": reason is None,
        "refundUnavailableReason": reason,
        # 只有已完成、没在退款的订单能申请发票；申请过的照原样带回（用户自己填的抬头和邮箱）
        "canInvoice": order.status == "completed" and order.refund_status is None,
        "invoice": payments.invoice_out(invoice),
    }


# 统一社会信用代码 18 位；老的税务登记号 15 或 20 位：只认数字和大写字母
_TAX_ID = re.compile(r"[0-9A-Z]{15}|[0-9A-Z]{18}|[0-9A-Z]{20}")
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_type: Literal["personal", "company"]
    title: str = Field(min_length=1, max_length=100)
    tax_id: str | None = Field(default=None, max_length=20)
    email: str = Field(min_length=3, max_length=254)


def _invoice_fields(body: InvoiceIn) -> tuple[str, str | None, str]:
    title, email = body.title.strip(), body.email.strip()
    tax_id = (body.tax_id or "").strip().upper() or None
    if not title or _CONTROL.search(title):
        raise HTTPException(status_code=422, detail="请填写发票抬头")
    if body.title_type == "company" and (tax_id is None or not _TAX_ID.fullmatch(tax_id)):
        raise HTTPException(status_code=422, detail="单位抬头要填税号（统一社会信用代码）")
    if body.title_type == "personal":
        tax_id = None
    if not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="请填写能收发票的邮箱")
    return title, tax_id, email


@router.put("/orders/{order_id}/invoice", dependencies=[Depends(_body_size)])
def request_invoice(
    order_id: int, body: InvoiceIn, session: SessionDep, user: KnownUserDep
) -> dict:
    """申请或修改发票：每笔已完成的订单一份，待开时可以改；不受支付开关控制（停卖后已付款的照常开）。"""
    title, tax_id, email = _invoice_fields(body)
    order = owned_order(session, user, order_id, lock=True)
    if order.status != "completed" or order.refund_status is not None:
        raise HTTPException(status_code=422, detail="只有已完成、没有退款的订单可以申请发票")
    stamp = _stamp()
    invoice = payments.order_invoice(session, order, lock=True)
    if invoice is not None and invoice.status != "pending":
        raise HTTPException(
            status_code=409, detail="这张订单的发票已处理，不能再改；有问题请点“遇到问题”告诉我们"
        )
    if invoice is None:
        invoice = InvoiceRequest(order_id=order.id, user_id=order.user_id, created_at=stamp)
        session.add(invoice)
    invoice.title_type, invoice.title, invoice.tax_id, invoice.email = (
        body.title_type,
        title,
        tax_id,
        email,
    )
    invoice.updated_at = stamp
    session.commit()
    return order_out(session, order)


@router.get("/orders")
def list_orders(session: SessionDep, user: KnownUserDep) -> dict:
    # 还没有用户记录的人没有订单：返回空列表，不用 404（小程序会把 404 说成「付费服务暂未开放」）
    if user is None:
        return {"items": []}
    orders = session.scalars(
        select(PayOrder)
        .where(PayOrder.user_id == user.id)
        .order_by(PayOrder.created_at.desc(), PayOrder.id.desc())
        .limit(20)
    )
    return {"items": [order_out(session, order) for order in orders]}


def owned_order(session: Session, user: User | None, order_id: int, *, lock=False) -> PayOrder:
    if user is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
    order = session.scalar(
        select(PayOrder).where(PayOrder.id == order_id, PayOrder.user_id == user.id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
    if lock:
        order = payments.lock_order(session, order.id)
        if order is None or order.user_id != user.id:
            raise HTTPException(status_code=404, detail="没有找到接口")
    return order


@router.get("/orders/{order_id}")
def get_order(order_id: int, session: SessionDep, user: KnownUserDep) -> dict:
    return order_out(session, owned_order(session, user, order_id))


@router.post("/orders/{order_id}/refund")
def refund_order(order_id: int, session: SessionDep, user: KnownUserDep, client: WxDep) -> dict:
    order = owned_order(session, user, order_id, lock=True)
    # 重复请求只返回同一个退款的状态，不重建退款单号。
    if order.platform == "android" and order.status in ("refunding", "refunded"):
        return order_out(session, order)
    stamp = _stamp()
    reason = payments.refund_reason(session, order, stamp, lock=True)
    if reason:
        raise HTTPException(status_code=422, detail=reason)
    # 自助退款退全额，收回这一笔的使用期和随它送的续买天数
    payments.begin_refund(session, order, stamp, fee=order.price, source=payments.SELF_REFUND)
    payments.start_refund(session, order, client, stamp)
    return order_out(session, order)


async def push_payload(request: Request) -> tuple[dict, bool]:
    settings = get_settings()
    # 云托管《消息推送》：服务未开公网时可信任所有推送；开了公网只认带 x-wx-source 的请求。
    appid = request.headers.get("X-WX-APPID")
    if (settings.wx_push_public and not request.headers.get("X-WX-SOURCE")) or (
        settings.wx_appid and appid is not None and appid != settings.wx_appid
    ):
        raise HTTPException(status_code=403, detail="推送来源无效")
    await _body_size(request)
    raw = await request.body()
    xml = raw.lstrip().startswith(b"<")
    try:
        if xml:
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError

            def unpack(node):
                return (
                    {child.tag: unpack(child) for child in node} if len(node) else (node.text or "")
                )

            data = unpack(ElementTree.fromstring(raw))
        else:
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data, xml
    except (ValueError, ElementTree.ParseError):
        raise HTTPException(status_code=400, detail="推送格式无效") from None


def reply(xml: bool, code: int = 0, extra: dict | None = None) -> Response:
    data = {"ErrCode": code, "ErrMsg": "success" if code == 0 else "retry", **(extra or {})}
    if not xml:
        return JSONResponse(data)
    root = ElementTree.Element("xml")
    for key, value in data.items():
        ElementTree.SubElement(root, key).text = str(value)
    return Response(ElementTree.tostring(root, encoding="utf-8"), media_type="application/xml")


def _ios_reply(bind, trade: str | None) -> dict:
    # 独立会话：响应超时返回后，后台查询仍可安全结束，不使用已经关闭的请求会话。
    try:
        with Session(bind, expire_on_commit=False) as session:
            order = (
                session.scalar(
                    select(PayOrder).where(
                        or_(PayOrder.out_trade_no == trade, PayOrder.wx_order_id == trade)
                    )
                )
                if trade
                else None
            )
            decision = payments.ios_decision(session, order)
            payments.event(
                session,
                "xpay_subscribe_ios_refund_query_notify",
                {"pay_order_id": trade, "result_code": decision["result_code"]},
            )
            session.commit()
            return decision
    except Exception:
        # 绝不把数据库连接或支付配置的异常文字放进退款凭据。
        return {"result_code": 0, "result_info": "未使用", "evidence": "本地任务状态暂不能核验"}


# 路径在服务启动时绑定，配置改变后须重新启动部署服务。
@push_router.post(get_settings().wx_push_path)
async def push(
    payload: Annotated[tuple[dict, bool], Depends(push_payload)], session: SessionDep, client: WxDep
) -> Response:
    data, xml = payload
    kind = data.get("Event", "")
    # 配置检测（action=CheckContainerPath）和非支付消息一律应答成功，避免微信重推。
    if kind not in PAY_EVENTS:
        return reply(xml)
    # 支付开关只停新下单：关闭后已有订单的推送照常处理（投诉、退款、晚到的付款）；
    # 本地没有的订单只记摘要并应答成功，不查单、不访问微信。
    if kind == "xpay_subscribe_ios_refund_query_notify":
        nested = data.get("WxaVirtualPayIosRefundQueryNotifyEvent", data)
        trade = nested.get("pay_order_id") if isinstance(nested, dict) else None
        trade = trade if isinstance(trade, str) and len(trade) <= 128 else None
        try:
            # 给平台三秒期限留出序列化及传输余量，数据库暂停/连接池等待也不阻塞问询。
            decision = await asyncio.wait_for(
                asyncio.to_thread(_ios_reply, session.get_bind(), trade), timeout=2.0
            )
        except TimeoutError:
            decision = {
                "result_code": 0,
                "result_info": "未使用",
                "evidence": "本地任务状态核验超时",
            }
        return reply(xml, extra=decision)
    return await run_in_threadpool(_transaction_push, data, xml, session, client)


def _transaction_push(data: dict, xml: bool, session: Session, client: XPayClient) -> Response:
    kind = data.get("Event", "")
    trade = (
        data.get("OutTradeNo") if kind == "xpay_goods_deliver_notify" else data.get("MchOrderId")
    )
    order_id = (
        session.scalar(select(PayOrder.id).where(PayOrder.out_trade_no == trade))
        if isinstance(trade, str) and trade
        else None
    )
    order = payments.lock_order(session, order_id) if order_id is not None else None
    if order is None:
        # 本地没有的订单重推也没用：只记摘要（事件表按白名单字段保存），应答成功。
        payments.event(session, kind, data, None)
        session.commit()
        return reply(xml)
    try:
        user = session.get(User, order.user_id) if order.user_id else None
        if (
            (user is not None and data.get("OpenId") != user.openid)
            or not isinstance(data.get("OpenId"), str)
            or not 1 <= len(data["OpenId"]) <= 128
            or (kind == "xpay_goods_deliver_notify" and int(data.get("Env", -1)) != order.env)
            or (kind != "xpay_goods_deliver_notify" and data.get("WxOrderId") != order.wx_order_id)
        ):
            return reply(xml, 1)
        payments.event(session, kind, data, order)
        if kind == "xpay_complaint_notify":
            order.status, order.error = "manual", "收到用户投诉，需人工处理"
            session.commit()
            return reply(xml)
        checked = payments.query(session, order, client, transient_openid=data["OpenId"])
        if kind == "xpay_goods_deliver_notify":
            accepted = payments.accept_payment(
                session, order, checked, get_settings(), _stamp(), data
            )
        else:
            accepted = payments.mark_refunded(session, order, checked, _stamp())
        session.commit()
        if (
            accepted
            and kind == "xpay_goods_deliver_notify"
            and order.status == "completed"
            and not order.goods_notified
        ):
            payments.provide_goods(session, order, client)
        return reply(xml, 0 if accepted else 1)
    except (XPayError, ValueError, TypeError, AttributeError, OverflowError):
        order.error = "推送暂未核验，将通过查单重试"
        session.commit()
        return reply(xml, 1)
