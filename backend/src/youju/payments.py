"""付款即执行：事务内一次发货；补单、退款、iOS 重跑及对账。

未知平台不主动退款；任何原始用户内容或微信错误文字均不写入事件摘要。
"""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from youju import advisor_tasks
from youju.access_log import log_uid
from youju.config import Settings, get_settings
from youju.models import AdvisorTask, PayEvent, PayOrder, PayReconciliation, User
from youju.xpay import XPayClient, XPayError

STATUS_LABELS = {
    "pending": "待支付",
    "processing": "处理中",
    "completed": "已完成",
    "refunding": "处理中",
    "refunded": "已退款",
    "manual": "需人工处理",
    "closed": "已关闭",
}


def enabled(settings) -> bool:
    return bool(getattr(settings, "pay_enabled", False) and settings.advisor_enabled)


def products(settings: Settings) -> dict[str, tuple[int, int]]:
    return {
        product: (price, limit)
        for product, price, limit in (
            (settings.xpay_product_100, settings.xpay_price_100, 100),
            (settings.xpay_product_200, settings.xpay_price_200, 200),
        )
        if product and price is not None and price > 0
    }


def platform(order: dict, settings: Settings) -> str:
    if settings.xpay_platform_field:
        value = str(order.get(settings.xpay_platform_field, ""))
        if value in settings.xpay_ios_values:
            return "ios"
        if value in settings.xpay_android_values:
            return "android"
        return "unknown"
    # 官方 query_order 的 order_type：7/8 苹果支付/退款，0/1 普通支付/退款。
    value = order.get("order_type")
    return "ios" if value in (7, 8) else "android" if value in (0, 1) else "unknown"


def event(session: Session, kind: str, data: dict, order: PayOrder | None = None) -> None:
    allowed = {
        "OpenId",
        "OutTradeNo",
        "Env",
        "Event",
        "openid",
        "order_id",
        "status",
        "order_fee",
        "paid_fee",
        "left_fee",
        "paid_time",
        "wx_order_id",
        "order_type",
        "MchOrderId",
        "WxOrderId",
        "MchRefundId",
        "WxRefundId",
        "RefundFee",
        "RetCode",
        "ComplaintId",
        "ComplaintTime",
        "pay_order_id",
        "result_code",
    }
    summary = {k: v for k, v in data.items() if k in allowed and type(v) in (str, int)}
    summary = {k: v[:128] if isinstance(v, str) else v for k, v in summary.items()}
    # 已注销用户的后续事件不得重新写入 openid。
    if order is not None and order.user_id is None:
        summary.pop("OpenId", None)
        summary.pop("openid", None)
    session.add(PayEvent(order_id=order.id if order else None, kind=kind[:64], summary=summary))


def query(
    session: Session, order: PayOrder, client: XPayClient, *, transient_openid: str | None = None
) -> dict:
    user = session.get(User, order.user_id) if order.user_id else None
    openid = user.openid if user else transient_openid
    if not openid:
        raise XPayError
    # 注销后的晚到推送仅临时使用身份查单，不重新保存用户关联。
    data = client.query(openid, order.out_trade_no, order.env)
    event(session, "query_order", data, order)
    # 返回未包含 openid 时，微信已在请求中按此 openid 查询；返回有身份字段则逐项核验。
    if (
        data.get("order_id") != order.out_trade_no
        or data.get("openid", openid) != openid
        or data.get("env_type", order.env) != order.env
    ):
        raise XPayError
    if order.wx_order_id and data.get("wx_order_id") != order.wx_order_id:
        raise XPayError
    return data


def new_task(session: Session, order: PayOrder, stamp: datetime) -> AdvisorTask:
    task = AdvisorTask(
        user_id=order.user_id,
        pay_order_id=order.id,
        kind="compare",
        status="queued",
        input_rows=list(order.input_rows),
        row_count=len(order.input_rows),
        idempotency_key=f"pay:{order.out_trade_no}:{order.rerun_count}",
        # 重跑不能延长原始名单的七天留存期限。
        created_at=order.created_at,
        expires_at=stamp + advisor_tasks.FILE_RETENTION,
    )
    session.add(task)
    session.flush()
    order.task_id = task.id
    return task


def accept_payment(
    session: Session,
    order: PayOrder,
    data: dict,
    settings: Settings,
    stamp: datetime,
    push: dict | None = None,
) -> bool:
    # 调用方锁定订单行；唯一业务单号 + 用户任务幂等键作为数据库双重保护。
    if data.get("status") not in (2, 3, 4):
        return False
    if data.get("order_fee") != order.price or data.get("paid_fee") != order.price:
        order.error = "支付金额与订单不符，未发货"
        return False
    if push is not None:
        goods = push.get("GoodsInfo", {})
        wx = push.get("WeChatPayInfo", {})
        if (
            goods.get("ProductId") != order.product_id
            or int(goods.get("Quantity", -1)) != 1
            or int(goods.get("ActualPrice", -1)) != order.price
            or int(goods.get("OrigPrice", -1)) != order.price
            or goods.get("Attach") != order.out_trade_no
            or (
                wx
                and (
                    wx.get("MchOrderNo") != data.get("channel_order_id")
                    or wx.get("TransactionId") != data.get("wxpay_order_id")
                    or int(wx.get("PaidTime", -1)) != data.get("paid_time")
                )
            )
        ):
            order.error = "推送与查单不一致，未发货"
            return False
    if order.task_id is not None or order.status in ("refunded", "refunding", "completed"):
        return True
    order.wx_order_id = data.get("wx_order_id")
    order.paid_fee = data["paid_fee"]
    paid_time = data.get("paid_time")
    if not isinstance(paid_time, int) or paid_time <= 0:
        order.error = "查单缺少有效支付时间，未发货"
        return False
    try:
        order.paid_time = datetime.fromtimestamp(paid_time, UTC)
    except (OverflowError, OSError, ValueError):
        order.error = "查单支付时间无效，未发货"
        return False
    order.platform = platform(data, settings)
    order.status, order.updated_at, order.error = "processing", stamp, None
    if not order.user_id or not order.input_rows or order.created_at <= stamp - timedelta(days=7):
        order.status, order.error = "manual", "输入已清理，需处理已付款订单"
        return True
    new_task(session, order, stamp)
    return True


def mark_refunded(session: Session, order: PayOrder, data: dict, stamp: datetime) -> bool:
    if data.get("status") not in (5, 8):
        return False
    if data.get("left_fee") != 0:
        order.status, order.refund_status, order.error = (
            "manual", "partial", "订单部分退款，需人工核对"
        )
        return True
    order.status, order.refund_status, order.error = "refunded", "completed", None
    order.updated_at = stamp
    # 也取消 iOS 重跑前的运行任务；完成结果由读取接口按订单状态撤销访问。
    session.execute(
        update(AdvisorTask)
        .where(AdvisorTask.pay_order_id == order.id, AdvisorTask.status.in_(("queued", "running")))
        .values(status="cancelled", finished_at=stamp, error="订单已退款，结果不可用")
    )
    return True


def result_error(order: PayOrder | None) -> str | None:
    if order is None:
        return "订单状态无法核验，结果不可用"
    if order.status in ("refunding", "refunded") or order.refund_status is not None:
        return "订单已退款，结果不可用"
    return None


def refund_exhausted(order: PayOrder, stamp: datetime) -> bool:
    return order.refund_attempts >= 6 or (
        order.refund_started_at is not None
        and order.refund_started_at <= stamp - timedelta(hours=24)
    )


def start_refund(session: Session, order: PayOrder, client: XPayClient, stamp: datetime) -> None:
    if order.platform != "android":
        order.status, order.error = "manual", "无法确认可主动退款的平台"
        session.commit()
        return
    order.status, order.refund_status = "refunding", "retry"
    order.refund_started_at = order.refund_started_at or stamp
    order.refund_order_id = order.refund_order_id or uuid4().hex
    # 请求前持久化同一个退款单号；超时、重启只会重试同一退款，不重复扣款。
    session.commit()
    if order.refund_retry_at and order.refund_retry_at > stamp:
        return
    # 包括查单失败的尝试也记数，避免请求一直发不出去时永久停留在退款中。
    order.refund_attempts += 1
    order.refund_retry_at = stamp + timedelta(minutes=min(60, 2 ** min(order.refund_attempts, 6)))
    session.commit()
    try:
        data = query(session, order, client)
        if mark_refunded(session, order, data, stamp):
            session.commit()
            return
        if (
            data.get("status") not in (2, 3, 4, 7)
            or data.get("left_fee") != order.price
            or data.get("order_fee") != order.price
            or data.get("paid_fee") != order.price
        ):
            order.status, order.error = "manual", "退款余额或订单状态不符，需核对"
            session.commit()
            return
        # 再次依据权威查单判断平台，禁止苹果订单主动退款。
        if platform(data, client.settings) != "android":
            order.status, order.error = "manual", "退款时无法确认普通支付平台"
            session.commit()
            return
        user = session.get(User, order.user_id)
        client.refund(
            {
                "openid": user.openid,
                "order_id": order.out_trade_no,
                "refund_order_id": order.refund_order_id,
                "left_fee": data["left_fee"],
                "refund_fee": order.price,
                "biz_meta": order.out_trade_no,
                "refund_reason": "1",
                "req_from": "3",
                "env": order.env,
            }
        )
        order.refund_status, order.error = "pending", None
    except XPayError:
        order.refund_status, order.error = "retry", "退款请求暂未确认，将重试"
        order.refund_retry_at = order.refund_retry_at or stamp + timedelta(minutes=2)
    order.updated_at = stamp
    session.commit()


def sync_task(session: Session, order: PayOrder, stamp: datetime) -> None:
    if order.status != "processing":
        return
    task = session.get(AdvisorTask, order.task_id) if order.task_id else None
    if task and task.status == "done":
        order.status, order.task_completed_at, order.updated_at = (
            "completed",
            task.finished_at,
            stamp,
        )
    elif task is None:
        order.status, order.error, order.alerted_at = (
            "manual", "关联任务缺失，需人工核对，未自动退款", None
        )
    elif task.status in ("failed", "cancelled"):
        if order.platform == "ios":
            if (
                order.rerun_count == 0
                and order.input_rows
                and order.user_id
                and order.created_at > stamp - timedelta(days=7)
            ):
                order.rerun_count += 1
                new_task(session, order, stamp)
            else:
                order.status, order.error = "manual", "iOS 重跑失败或输入不可用，需人工处理"
        elif order.platform == "android":
            order.status, order.refund_status = "refunding", "retry"
            order.refund_started_at = order.refund_started_at or stamp
        else:
            order.status, order.error = "manual", "支付平台未知，需人工处理"
    session.flush()


def ios_decision(session: Session, order: PayOrder | None) -> dict:
    task = session.get(AdvisorTask, order.task_id) if order and order.task_id else None
    finished = order.task_completed_at if order else None
    if not finished and task and task.status == "done":
        finished = task.finished_at
    # code/reason 为推送问询的决策字段；ErrCode 是协议应答，不混用。
    if finished:
        return {
            "result_code": 1,
            "result_info": "任务已成功完成",
            "evidence": f"任务 {order.task_id} 已完成，时间 {finished.isoformat()}",
        }
    return {"result_code": 0, "result_info": "建议退款", "evidence": "任务尚未成功完成"}


def _local_orders(session: Session, day: date) -> dict[str, int]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=advisor_tasks.BEIJING).astimezone(UTC)
    return dict(
        session.execute(
            select(PayOrder.out_trade_no, PayOrder.paid_fee).where(
                PayOrder.paid_time >= start, PayOrder.paid_time < start + timedelta(days=1)
            )
        ).all()
    )


def reconcile(
    session: Session, day: date, bill: dict[str, int], stamp: datetime
) -> PayReconciliation:
    local = _local_orders(session, day)
    differences = {
        key: {"local": local.get(key), "bill": bill.get(key)}
        for key in local.keys() | bill.keys()
        if local.get(key) != bill.get(key)
    }
    # 支付日可能尚未被本地记录；按业务单号找漏发订单，绝不依据账单直接发货。
    for order in session.scalars(
        select(PayOrder).where(PayOrder.out_trade_no.in_(bill)).with_for_update()
    ):
        if order.task_id is None and order.status not in ("refunding", "refunded"):
            order.status, order.error, order.alerted_at, order.updated_at = (
                "manual", "账单已支付但本地未发货，需人工核对", None, stamp
            )
            differences.setdefault(order.out_trade_no, {"local": local.get(order.out_trade_no), "bill": bill[order.out_trade_no]})
    row = session.scalar(
        select(PayReconciliation).where(PayReconciliation.day == day).with_for_update()
    )
    if row is None:
        row = PayReconciliation(day=day, created_at=stamp)
        session.add(row)
    row.local_count, row.local_fee = len(local), sum(local.values())
    row.bill_count, row.bill_fee = len(bill), sum(bill.values())
    row.differences, row.status, row.updated_at = (
        differences,
        "different" if differences else "matched",
        stamp,
    )
    session.commit()
    return row


def parse_bill(raw: bytes, settings: Settings) -> dict[str, int]:
    # 官方未给账单内容 schema，显式配置列名和单位，不能把空文件当零交易对账成功。
    if not settings.xpay_bill_order_column or not settings.xpay_bill_fee_column:
        raise XPayError
    try:
        reader = csv.DictReader(StringIO(raw.decode("utf-8-sig")))
        if not {settings.xpay_bill_order_column, settings.xpay_bill_fee_column} <= set(
            reader.fieldnames or []
        ):
            raise XPayError
        rows: dict[str, int] = {}
        for item in reader:
            key = item[settings.xpay_bill_order_column].strip().lstrip("`")
            amount = Decimal(item[settings.xpay_bill_fee_column].strip().lstrip("`"))
            amount *= 100 if settings.xpay_bill_fee_unit == "yuan" else 1
            if (
                not key
                or key in rows
                or not amount.is_finite()
                or amount < 0
                or amount != int(amount)
            ):
                raise XPayError
            rows[key] = int(amount)
        return rows
    except (UnicodeError, ValueError, TypeError, KeyError, InvalidOperation, csv.Error):
        raise XPayError from None


def download_reconcile(session: Session, day: date, client: XPayClient, stamp: datetime) -> None:
    row = session.scalar(select(PayReconciliation).where(PayReconciliation.day == day))
    if row is None:
        row = PayReconciliation(
            day=day, differences={}, status="pending", created_at=stamp, updated_at=stamp
        )
        session.add(row)
        session.commit()
    local = _local_orders(session, day)
    row.local_count, row.local_fee = len(local), sum(local.values())
    row.updated_at = stamp
    session.commit()
    try:
        result = client.bill(day)
        url = result.get("url", "")
        if not url:
            return  # 下一分钟继续轮询，不阻塞 cron。
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in client.settings.xpay_bill_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise XPayError
        raw = bytearray()
        with client.http.stream("GET", url, timeout=10) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 10 * 1024 * 1024:
                    raise XPayError
        reconcile(session, day, parse_bill(bytes(raw), client.settings), stamp)
    except XPayError as exc:
        if exc.code != 268490011:
            row.status, row.differences, row.updated_at = (
                "pending",
                {"error": "账单未完成或无法核验"},
                stamp,
            )
            session.commit()
    except Exception:
        row.status, row.differences = "pending", {"error": "账单下载暂不可用"}
        session.commit()


def run_payments(
    session: Session,
    *,
    settings: Settings | None = None,
    client: XPayClient | None = None,
    stamp: datetime | None = None,
    alert=None,
) -> dict:
    settings = settings or get_settings()
    if not enabled(settings):
        return {"processed": 0}
    stamp = advisor_tasks._utc(stamp or datetime.now(UTC))
    if client is None:
        with httpx.Client(trust_env=False) as http:
            return run_payments(
                session,
                settings=settings,
                client=XPayClient(settings, http),
                stamp=stamp,
                alert=alert,
            )
    from youju.alerts import send_alert

    alert = alert or send_alert
    ids = list(
        session.scalars(
            select(PayOrder.id)
            .where(
                (
                    (PayOrder.status == "pending")
                    & (PayOrder.created_at <= stamp - timedelta(minutes=2))
                    & ((PayOrder.query_retry_at.is_(None)) | (PayOrder.query_retry_at <= stamp))
                )
                | PayOrder.status.in_(("processing", "refunding"))
                | ((PayOrder.status == "manual") & PayOrder.alerted_at.is_(None))
                | ((PayOrder.status == "completed") & (PayOrder.goods_notified == False))  # noqa: E712
            )
            .order_by(PayOrder.updated_at, PayOrder.id)
            .limit(100)
        )
    )
    for order_id in ids:
        order = session.scalar(
            select(PayOrder).where(PayOrder.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            session.rollback()
            continue
        try:
            # 先查平台退款，再同步失败任务，避免把丢失的退款推送当成任务失败重跑。
            if (
                order.status == "processing"
                and (order.paid_time or order.created_at) <= stamp - timedelta(minutes=10)
                and (order.query_retry_at is None or order.query_retry_at <= stamp)
            ):
                order.query_retry_at = stamp + timedelta(minutes=10)
                data = query(session, order, client)
                mark_refunded(session, order, data, stamp)
            sync_task(session, order, stamp)
            if order.status == "refunding":
                order.refund_started_at = order.refund_started_at or stamp
                if not order.user_id:
                    order.status, order.error = "manual", "用户已注销，退款需人工处理"
                elif refund_exhausted(order, stamp):
                    # 最后核验已成功的退款；查单也失败则人工接手，不能无限重试。
                    confirmed = False
                    try:
                        confirmed = mark_refunded(session, order, query(session, order, client), stamp)
                    except XPayError:
                        pass
                    if not confirmed:
                        order.status, order.error, order.alerted_at = (
                            "manual", "退款尝试达到上限或超过24小时，需人工核对", None
                        )
                elif order.refund_status == "pending":
                    if order.refund_retry_at is None or order.refund_retry_at <= stamp:
                        order.refund_retry_at = stamp + timedelta(minutes=10)
                        data = query(session, order, client)
                        if not mark_refunded(session, order, data, stamp) and data.get("status") == 7:
                            order.refund_status = "retry"
                else:
                    start_refund(session, order, client, stamp)
            elif order.status == "pending":
                overdue = order.created_at <= stamp - timedelta(hours=48)
                order.query_retry_at = stamp + timedelta(
                    minutes=1 if order.created_at > stamp - timedelta(minutes=30) else 10
                )
                data = query(session, order, client)
                if not mark_refunded(session, order, data, stamp):
                    if data.get("status") == 6 or (overdue and data.get("status") in (0, 1)):
                        order.status = "closed"
                    elif overdue and data.get("status") in (2, 3, 4):
                        # 记录核验过的支付证据，但超出自动履约窗口不能再建任务。
                        accept_payment(session, order, data, settings, stamp, deliver=False)
                    else:
                        accept_payment(session, order, data, settings, stamp)
                session.commit()  # 先持久化任务再发通知；通知失败由下一次补发。
            if order.status in ("processing", "completed") and not order.goods_notified:
                client.provide(order.out_trade_no, order.env)
                order.goods_notified = True
            if (
                order.status == "manual"
                and not order.alerted_at
                and alert(f"支付订单 #{order.id} 需人工处理：{order.error}") is not False
            ):
                order.alerted_at = stamp
            order.updated_at = stamp
            session.commit()
        except XPayError:
            order.error = "微信查单或通知暂不可用，将重试"
            order.updated_at = stamp
            session.commit()
    # 9:30 的任务创建记录后，每分钟继续未完成的账单生成/下载。
    days = list(
        session.scalars(
            select(PayReconciliation.day)
            .where(PayReconciliation.status == "pending")
            .order_by(PayReconciliation.day)
            .limit(3)
        )
    )
    for day in days:
        download_reconcile(session, day, client, stamp)
    return {"processed": len(ids)}


def cleanup(session: Session, settings, stamp: datetime) -> dict:
    count = session.execute(
        update(PayOrder)
        .where(PayOrder.created_at <= stamp - timedelta(days=7))
        .values(input_rows=None)
    ).rowcount
    events = session.execute(
        delete(PayEvent).where(PayEvent.created_at <= stamp - timedelta(days=180))
    ).rowcount
    session.execute(
        delete(PayOrder).where(
            PayOrder.created_at
            <= stamp - timedelta(days=getattr(settings, "retain_pay_orders_days", 3650)),
            PayOrder.status.in_(("completed", "closed", "refunded")),
        )
    )
    return {"pay_inputs": count, "pay_events": events}


def anonymize(session: Session, user: User) -> None:
    orders = list(
        session.scalars(select(PayOrder).where(PayOrder.user_id == user.id).with_for_update())
    )
    if not orders:
        return
    pseudonym = log_uid(user.openid, session.get_bind())
    for order in orders:
        order.input_rows, order.user_id, order.pseudonym = None, None, pseudonym
        if order.status == "pending":
            order.status = "closed"
        elif order.status in ("processing", "refunding"):
            task = session.get(AdvisorTask, order.task_id) if order.task_id else None
            if task and task.status == "done":
                order.status, order.task_completed_at = "completed", task.finished_at
            else:
                order.status, order.error = "manual", "用户已注销，需核对未完成订单"
    for row in session.scalars(
        select(PayEvent).where(PayEvent.order_id.in_([o.id for o in orders]))
    ):
        row.summary = {k: v for k, v in row.summary.items() if k not in ("OpenId", "openid")}
    session.flush()


def overview(session: Session) -> dict:
    day = advisor_tasks.usage_day(datetime.now(UTC))
    start = datetime.combine(day, datetime.min.time(), tzinfo=advisor_tasks.BEIJING).astimezone(UTC)
    return {
        "today_orders": session.scalar(
            select(func.count(PayOrder.id)).where(PayOrder.created_at >= start)
        )
        or 0,
        "today_fee": session.scalar(
            select(func.sum(PayOrder.paid_fee)).where(PayOrder.paid_time >= start)
        )
        or 0,
        "manual": session.scalar(select(func.count(PayOrder.id)).where(PayOrder.status == "manual"))
        or 0,
        "reconciliation_pending": session.scalar(
            select(func.count(PayReconciliation.id)).where(PayReconciliation.status == "pending")
        )
        or 0,
        "differences": session.scalar(
            select(func.count(PayReconciliation.id)).where(PayReconciliation.status == "different")
        )
        or 0,
    }
