"""默认关闭的批量工具支付接口（30 天、90 天两档，D-070）及云托管消息推送。"""

import asyncio
import json
import logging
import re
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Annotated, Literal
from uuid import uuid4
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from youju import payments
from youju.api.advisor import SessionDep, _body_size, _stamp, _upload_consent
from youju.api.contact import transfer_to_kf
from youju.api.me import KnownUserDep, UserDep, request_openid
from youju.config import get_settings
from youju.models import InvoiceRequest, PayOrder, User
from youju.xpay import XPAY_ENV, XPayClient, XPayError, encode, pay_signature, signature, wx_http

logger = logging.getLogger(__name__)


def require_enabled(request: Request) -> None:
    # 在售才有商品和下单：总开关开着，且对全体开放或这个用户在测试名单里
    try:
        openid = request_openid(request)
    except HTTPException:
        openid = None
    if not payments.selling_to(get_settings(), openid, payments.client_of(request.headers)):
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
    with wx_http() as http:
        yield XPayClient(get_settings(), http)


WxDep = Annotated[XPayClient, Depends(wx_client)]


# 下新单前查不清上一笔付款结果时的说法（挡新单，最多 10 分钟）
UNVERIFIED_PREVIOUS = (
    "上一笔付款微信暂时还查不到结果，请过几分钟再试；已扣款的会自动开通，可到“我的订单”查看"
)


class FailedOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int
    code: int


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=512, repr=False)
    # 用户勾选同意的《付费服务协议》版本，随订单保存，日后有争议时能说明当时同意的是哪一版
    terms_version: str = Field(min_length=1, max_length=40)
    # 购买页上写的"能开哪种发票"（商品接口带回的那句）：和现在的配置不一致就不下单，
    # 订单记下的就是用户付款前看到的那句（Codex 第四轮）
    invoice_type: str = Field(default="", max_length=100)
    # 上一笔付款前就被微信拒了（小程序拿到官方错误码表里下单阶段的错误码）：带上那一单和错误码，
    # 服务端在那一单上记一笔，下新单前查它查不清时不挡（payments.mark_not_started）
    failed_order: FailedOrderIn | None = None


@router.get("/products", dependencies=[Depends(require_enabled)])
def list_products(request: Request, session: SessionDep) -> dict:
    """只读：已配置价格、可以下单的档位（批量工具 30 天、90 天，按天数从短到长）；
    小程序据此显示付费档，空列表时不显示。renewBonus：和价格同一屏写的续买规则，开关关着为 None。"""
    settings = get_settings()
    # 和下单条件一致（payments.orderable）：下不了单就不展示价格。
    if not payments.orderable(settings):
        return {"items": [], "renewBonus": None, "invoiceType": None}
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
        "renewBonus": payments.renew_offer(session, settings, user, _stamp()),
        # 能开哪种发票（付款前要看得到）；没填就不卖，走到这里一定有
        "invoiceType": payments.invoice_type(settings) or None,
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
    invoice = payments.invoice_type(settings)
    if invoice and body.invoice_type.strip() != invoice:
        raise HTTPException(
            status_code=409, detail="能开的发票类型有更新，请重新看一下购买页再付款"
        )
    stamp = _stamp()
    # 和免费任务使用同一用户锁，防止并发下单绕过限制及与注销竞态。
    if session.scalar(select(User.id).where(User.id == user.id).with_for_update()) is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
    # 下新单前先问微信：10 分钟内的待支付单可能已经付了款，只是推送和补单还没到
    # （取消回调不准、付款中途小程序被关掉时，小程序本机没记下这一笔）。付了就先开通、不下新单。
    # 查不了的旧单挡新单（409，最多挡 10 分钟）：以前放行，
    # 旧单其实付了款时会重复扣款（Codex 10/29）。
    # 付款前就被微信拒了的单，小程序下一次付款时会带上（failed_order），记过的不在这里挡人；
    # 下单回应没送到手机这类没带上、微信那边也查不到的单，最多挡 10 分钟。
    if body.failed_order is not None:
        payments.mark_not_started(
            session, user.id, body.failed_order.order_id, body.failed_order.code, stamp
        )
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
            if payments.reported_not_started(old):
                continue
            session.commit()
            raise HTTPException(status_code=409, detail=UNVERIFIED_PREVIOUS) from None
        # 和主动核对同一套处理（payments.settle_pending）：已关单、已退完的不挡新单；
        # 付了款的开通、转人工的（金额不符、5/7 等少见状态）都返回 409，不再下新单；
        # 查单返回格式不对（unavailable）和查不了一样，挡新单（Codex 第 1 轮）
        result = payments.settle_pending(session, old, data, settings, stamp)
        if result == "unavailable" and not payments.reported_not_started(old):
            session.commit()
            raise HTTPException(status_code=409, detail=UNVERIFIED_PREVIOUS)
        if result in ("paid", "manual"):
            session.commit()
            raise HTTPException(
                status_code=409,
                detail="上一笔已经付款，正在为你开通，可到“我的订单”查看"
                if result == "paid"
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
            "env": XPAY_ENV,
            "currencyType": "CNY",
            "productId": body.product_id,
            "goodsPrice": price,
            "outTradeNo": trade,
            "attach": trade,
        }
    )
    try:
        pay_sig = pay_signature(client.appkey(XPAY_ENV), "requestVirtualPayment", raw)
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
        # 购买页上写的发票类型（上面已核对和现在的配置一致）
        invoice_type=invoice or None,
        env=XPAY_ENV,
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
        # 下单时同意的《付费服务协议》版本：订单页按它打开协议
        # （付费服务协议第七条，开卖那一版必做第 7 项）
        "termsVersion": order.terms_version,
        # 下单时间：待确认的订单写"几点下的单"，用户好对上自己是哪一次付的
        "createdAt": order.created_at,
        "paidAt": order.paid_time,
        # 微信确认的实付金额（分）：有它才算"付款已收到"；人工核对中的订单据此写说明
        # （没核实付款的不写"收到"）
        "paidAmount": order.paid_fee,
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
    # 付了款没开通、转了人工的订单（payments.undelivered_refund_reason）：负责人可能已收到过告警；
    # 退款万一又转回人工，要重新告警，负责人才知道用户已经申请退款
    if order.status == "manual":
        order.alerted_at = None
    # 自助退款退全额，收回这一笔的使用期和随它送的续买天数
    payments.begin_refund(session, order, stamp, fee=order.price, source=payments.SELF_REFUND)
    payments.start_refund(session, order, client, stamp)
    return order_out(session, order)


# 主动核对的频率：同一张订单在一个服务实例里 2 秒内只问一次微信；一个实例每秒最多替用户问 10 次
# （超出的这次不查，原样返回订单，小程序 2 秒后再核对）。小程序付款后每 2 秒核对一次，订单页一次
# 最多核对 3 张；连点、脚本刷不会把查单接口的频率打满（官方有频率限制错误码 268490015）。
# 补单、推送不走这里，不受影响
CHECK_GAP = 2.0
CHECK_RATE = 10
_checked: dict[int, float] = {}
_recent: deque[float] = deque()
_checked_lock = threading.Lock()


def _check_due(order_id: int) -> bool:
    now = time.monotonic()
    with _checked_lock:
        last = _checked.get(order_id)
        if last is not None and now - last < CHECK_GAP:
            return False
        while _recent and now - _recent[0] >= 1.0:
            _recent.popleft()
        if len(_recent) >= CHECK_RATE:
            return False
        _recent.append(now)
        if len(_checked) >= 1000:
            for key in [key for key, at in _checked.items() if now - at >= CHECK_GAP]:
                del _checked[key]
        _checked[order_id] = now
        return True


@router.post("/orders/{order_id}/check")
def check_order(order_id: int, session: SessionDep, user: KnownUserDep, client: WxDep) -> dict:
    """核对一张待确认的订单（payments.check_order）：付款后小程序立刻调用、之后每 2 秒一次，
    打开订单页时对待确认的订单各调用一次。付了款当场开通，不等推送和每分钟的补单。
    和订单列表、退款一样不受支付开关控制（停卖前付的款照样核对）。只核对本人 48 小时内、
    最近 3 张待确认的订单，同一张 2 秒内只问一次微信；其余情况不查，原样返回订单。
    返回订单，加上 check：这次核对的结果（见 payments.check_order），没有向微信查时为 None。"""
    order = owned_order(session, user, order_id)
    stamp = _stamp()
    result = None
    if payments.checkable(session, order, stamp) and _check_due(order.id):
        order = owned_order(session, user, order_id, lock=True)
        # 加锁后再看一次：推送、补单可能刚处理完
        if payments.checkable(session, order, stamp):
            result = payments.check_order(session, order, client, get_settings(), stamp)
    return {**order_out(session, order), "check": result}


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
            decision = payments.ios_decision(session, order, _stamp(), get_settings())
            payments.event(
                session,
                "xpay_subscribe_ios_refund_query_notify",
                {"pay_order_id": trade, "result_code": decision["result_code"]},
            )
            session.commit()
            return decision
    except Exception:
        # 绝不把数据库连接或支付配置的异常文字放进退款凭据；
        # 核验不了也答建议退款（付费服务协议第二条第 7 项）
        return dict(payments.IOS_UNVERIFIABLE)


# 路径在服务启动时绑定，配置改变后须重新启动部署服务。
@push_router.post(get_settings().wx_push_path)
async def push(
    payload: Annotated[tuple[dict, bool], Depends(push_payload)], session: SessionDep, client: WxDep
) -> Response:
    data, xml = payload
    kind = data.get("Event", "")
    # 用户在客服会话里发的消息回"转客服"，客服人员才收得到（contact.py）；
    # 配置检测（action=CheckContainerPath）和其他非支付事件一律应答成功，避免微信重推。
    if kind not in PAY_EVENTS:
        return transfer_to_kf(data, xml) or reply(xml)
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
            decision = dict(payments.IOS_UNVERIFIABLE)
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
    # 推送没处理成的原因记进日志（微信会重推，补单兜底）：只记事件名、订单号和状态码，
    # 不记 openid、单号和金额；事件名是英文，云托管日志能按它搜
    order_id = order.id
    try:
        user = session.get(User, order.user_id) if order.user_id else None
        if (
            (user is not None and data.get("OpenId") != user.openid)
            or not isinstance(data.get("OpenId"), str)
            or not 1 <= len(data["OpenId"]) <= 128
            or (kind == "xpay_goods_deliver_notify" and int(data.get("Env", -1)) != order.env)
            or (kind != "xpay_goods_deliver_notify" and data.get("WxOrderId") != order.wx_order_id)
        ):
            logger.warning(
                "%s 订单 #%s 推送的用户、环境或微信单号和订单对不上，没处理", kind, order_id
            )
            return reply(xml, 1)
        row = payments.event(session, kind, data, order)
        if kind == "xpay_complaint_notify":
            # 每条投诉收到时就定好由谁告警（payments.note_complaint）：
            # 已完成、待确认、人工单转人工；
            # 退款中、已退款、已关闭的不改状态，补单单独告警；同一个投诉重推的不再告警
            session.flush()
            payments.note_complaint(session, order, row, _stamp())
            session.commit()
            return reply(xml)
        # 退款推送和发货推送一样只是"去查单"的信号，按查单结果走和每分钟补单同一套处理
        # （Codex 复核：迟到的、别的退款的推送，按推送本身改订单都会改错）：
        # - RetCode 0（退款成功）且退款单号是我们这次发起的：查单停在 5 也算退完；
        # - RetCode 非 0（失败）：只记下来；订单在退款中时，查单 8 记退完、7 照补单的做法重新发起
        #   （同一退款单号），5 和没变化的接着等；一直退不成的，从发起退款满 24 小时转人工、
        #   告警一次（run_payments）。推送不直接把订单改成人工，已退完、已完成的订单不会倒退。
        ret = data.get("RetCode")
        refund_id = data.get("MchRefundId")
        ours = not refund_id or not order.refund_order_id or refund_id == order.refund_order_id
        failed = kind == "xpay_refund_notify" and ret is not None and str(ret) != "0"
        if failed:
            logger.warning(
                "%s 订单 #%s 微信说退款失败：RetCode=%s，订单状态 %s%s",
                kind,
                order_id,
                str(ret)[:16],
                order.status,
                "" if ours else "，不是这笔订单正在办的退款单",
            )
            if order.status == "refunding" and ours:
                checked = payments.query(session, order, client, transient_openid=data["OpenId"])
                if not payments.mark_refunded(session, order, checked, _stamp()) and (
                    checked.get("status") == 7
                ):
                    order.refund_status = "retry"
            session.commit()
            return reply(xml)
        checked = payments.query(session, order, client, transient_openid=data["OpenId"])
        if kind == "xpay_goods_deliver_notify":
            accepted = payments.accept_payment(
                session, order, checked, get_settings(), _stamp(), data
            )
        else:
            # 推送说我们这次发起的退款成功（RetCode 0）时，查单停在 5（商户侧扣款完成）也算退完
            accepted = payments.mark_refunded(
                session,
                order,
                checked,
                _stamp(),
                confirmed=ours and ret is not None and str(ret) == "0",
            )
            if not accepted:
                # 已完成的订单（苹果那边或小程序后台退的款）没核实退款：转人工，重推完也有人知道
                payments.flag_unconfirmed_refund(order, _stamp())
        session.commit()
        if (
            accepted
            and kind == "xpay_goods_deliver_notify"
            and order.status == "completed"
            and not order.goods_notified
        ):
            payments.provide_goods(session, order, client)
        if not accepted:
            logger.warning(
                "%s 订单 #%s 这次没处理：查单状态 %s", kind, order_id, checked.get("status")
            )
        return reply(xml, 0 if accepted else 1)
    except (XPayError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        code = exc.code if isinstance(exc, XPayError) else None
        reason = f"errcode={code}" if code is not None else type(exc).__name__
        logger.warning("%s 订单 #%s 核验失败：%s", kind, order_id, reason)
        # 每分钟的补单不查已完成的订单：退款推送没核验成就转人工（Codex 10/29）。
        # 已经转人工的不改原因：重推时查单又出错，不能把要人工核对什么盖成"将通过查单重试"
        # （补单不查人工单，Codex 第 3 轮）
        flagged = kind == "xpay_refund_notify" and payments.flag_unconfirmed_refund(order, _stamp())
        if not flagged and order.status != "manual":
            order.error = "推送暂未核验，将通过查单重试"
        session.commit()
        return reply(xml, 1)
