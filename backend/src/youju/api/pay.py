"""默认隐藏的按次支付接口及云托管消息推送。"""

import asyncio
import json
from datetime import timedelta
from typing import Annotated
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
from youju.api.advisor import SessionDep, TaskIn, _body_size, _file_rows, _stamp, _upload_consent
from youju.api.me import KnownUserDep, UserDep
from youju.config import get_settings
from youju.models import PayOrder, User
from youju.xpay import XPayClient, XPayError, encode, pay_signature, signature


def require_enabled() -> None:
    if not payments.enabled(get_settings()):
        raise HTTPException(status_code=404, detail="没有找到接口")


router = APIRouter(prefix="/api/v1/pay", tags=["支付"], dependencies=[Depends(require_enabled)])


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
    rows: list[str] = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=512, repr=False)


@router.get("/products")
def list_products() -> dict:
    """只读：已配置价格、可以下单的商品；小程序据此显示付费档，空列表时不显示。"""
    settings = get_settings()
    # 和下单条件一致：没有 offerId 时下单必然失败，不展示价格。
    if not settings.xpay_offer_id:
        return {"items": []}
    items = [
        {"productId": product, "price": price, "maxRows": limit}
        for product, (price, limit) in payments.products(settings).items()
    ]
    return {"items": sorted(items, key=lambda item: item["maxRows"])}


@router.post("/orders", status_code=201, dependencies=[Depends(_body_size)])
def create_order(body: OrderIn, session: SessionDep, user: UserDep, client: WxDep) -> dict:
    settings = get_settings()
    _upload_consent(user)
    sku = payments.products(settings).get(body.product_id)
    if sku is None or not settings.xpay_offer_id:
        raise HTTPException(status_code=404, detail="没有找到商品")
    rows = _file_rows(TaskIn(kind="compare", rows=body.rows))
    if not any(row.strip() for row in rows) or any(len(row) > 256 for row in rows):
        raise HTTPException(status_code=422, detail="名单为空或企业名称过长")
    price, limit = sku
    if len(rows) > limit:
        raise HTTPException(status_code=422, detail=f"该商品最多提交 {limit} 行")
    stamp = _stamp()
    # 和免费任务使用同一用户锁，防止并发下单绕过限制及与注销竞态。
    if session.scalar(select(User.id).where(User.id == user.id).with_for_update()) is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
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
        row_limit=limit,
        env=settings.xpay_env,
        input_rows=rows,
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


@router.get("/orders/{order_id}")
def get_order(order_id: int, session: SessionDep, user: KnownUserDep) -> dict:
    if user is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
    order = session.scalar(
        select(PayOrder)
        .where(PayOrder.id == order_id, PayOrder.user_id == user.id)
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="没有找到接口")
    payments.sync_task(session, order, _stamp())
    session.commit()
    return {
        "orderId": order.id,
        "status": payments.STATUS_LABELS[order.status],
        "taskId": order.task_id,
    }


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
        return {"result_code": 0, "result_info": "建议退款", "evidence": "本地任务状态暂不能核验"}


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
    if not payments.enabled(get_settings()):
        # 收费未开启：记录后应答成功，不查单、不发货。
        await run_in_threadpool(_record_only, data, kind, session)
        return reply(xml)
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
                "result_info": "建议退款",
                "evidence": "本地任务状态核验超时",
            }
        return reply(xml, extra=decision)
    return await run_in_threadpool(_transaction_push, data, xml, session, client)


def _record_only(data: dict, kind: str, session: Session) -> None:
    payments.event(session, kind, data, None)
    session.commit()


def _transaction_push(data: dict, xml: bool, session: Session, client: XPayClient) -> Response:
    kind = data.get("Event", "")
    trade = (
        data.get("OutTradeNo") if kind == "xpay_goods_deliver_notify" else data.get("MchOrderId")
    )
    order = (
        session.scalar(select(PayOrder).where(PayOrder.out_trade_no == trade).with_for_update())
        if isinstance(trade, str) and trade
        else None
    )
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
            if accepted:
                order.goods_notified = True
        else:
            accepted = payments.mark_refunded(session, order, checked, _stamp())
        session.commit()
        return reply(xml, 0 if accepted else 1)
    except (XPayError, ValueError, TypeError, AttributeError, OverflowError):
        order.error = "推送暂未核验，将通过查单重试"
        session.commit()
        return reply(xml, 1)
