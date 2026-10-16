"""付款开通使用期：事务内一次发货；补单、退款及对账。

未知平台不主动退款；任何原始用户内容或微信错误文字均不写入事件摘要。
"""

from __future__ import annotations

import contextlib
import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from youju import advisor_tasks
from youju.access_log import log_uid
from youju.config import Settings, get_settings
from youju.models import (
    AdvisorTask,
    InvoiceRequest,
    PayEvent,
    PayOrder,
    PayReconciliation,
    User,
    WorkbenchPass,
)
from youju.xpay import XPayClient, XPayError

STATUS_LABELS = {
    "pending": "待支付",
    "completed": "已完成",
    "refunding": "退款中",
    "refunded": "已退款",
    "manual": "需人工处理",
    "closed": "已关闭",
}
INVOICE_LABELS = {"pending": "待开", "issued": "已开", "rejected": "不开"}
INVOICE_VOID_NOTE = "订单已退款，不再开票"


# 通知发货最多尝试次数、账单下载/解析最多失败次数；到达后转人工 / failed 并告警。
PROVIDE_ATTEMPTS = 20
BILL_ATTEMPTS = 5
# 未完结订单：支付关闭后仍要处理到底，不能冻结。
OPEN_ORDER = (
    PayOrder.status.in_(("pending", "refunding"))
    | ((PayOrder.status == "manual") & PayOrder.alerted_at.is_(None))
    | ((PayOrder.status == "completed") & (PayOrder.goods_notified == False))  # noqa: E712
)


def enabled(settings) -> bool:
    """是否接受新订单（下单、商品接口）；不决定已有订单是否继续处理。"""
    return bool(getattr(settings, "pay_enabled", False) and settings.advisor_enabled)


def has_work(session: Session, settings) -> bool:
    """定时任务是否需要运行：支付开启，或库里还有未完结订单、当天有本地订单的待完成对账。

    支付关闭且没有这些记录时只做这两次本地查询，不创建 HTTP 客户端、不访问微信。
    """
    if enabled(settings):
        return True
    if session.scalar(select(PayOrder.id).where(OPEN_ORDER).limit(1)) is not None:
        return True
    return (
        session.scalar(
            select(PayReconciliation.id)
            .where(PayReconciliation.status == "pending", PayReconciliation.local_count > 0)
            .limit(1)
        )
        is not None
    )


def should_reconcile(session: Session, settings, day: date) -> bool:
    """支付关闭后，只有那天有本地已付款订单才下载账单。"""
    return enabled(settings) or bool(_local_orders(session, day))


PASS_DAYS = 30
PASS_NAME = "客户工作台 30 天"


def products(settings: Settings) -> dict[str, tuple[int, int]]:
    if settings.xpay_product_pass and settings.xpay_price_pass is not None:
        return {settings.xpay_product_pass: (settings.xpay_price_pass, PASS_DAYS)}
    return {}


def entry(session: Session, settings, user: User | None) -> dict:
    """小程序要不要显示购买入口和"我的订单"：正在售卖（开关开、配了 offerId 和价格），
    或这个用户有过订单（停卖后已付款的用户还要能看订单、退款、开票）。"""
    selling = (
        enabled(settings)
        and bool(getattr(settings, "xpay_offer_id", ""))
        and bool(products(settings))
    )
    has_orders = (
        user is not None
        and session.scalar(select(PayOrder.id).where(PayOrder.user_id == user.id).limit(1))
        is not None
    )
    return {"enabled": selling, "hasOrders": has_orders}


def lock_order(session: Session, order_id: int) -> PayOrder | None:
    # 与提交任务、注销统一先锁用户，再锁订单/使用期，串行化顺延与退款判断。
    # query() 待写事件的订单外键会取共享锁，必须在用户锁之后才 flush。
    with session.no_autoflush:
        owner = session.scalar(select(PayOrder.user_id).where(PayOrder.id == order_id))
        if owner is not None:
            session.scalar(select(User.id).where(User.id == owner).with_for_update())
        return session.scalar(
            select(PayOrder)
            .where(PayOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )


def order_pass(session: Session, order: PayOrder, *, lock=False) -> WorkbenchPass | None:
    stmt = select(WorkbenchPass).where(WorkbenchPass.order_id == order.id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalar(stmt)


def current_pass(
    session: Session, user_id: int, stamp: datetime, *, lock=False
) -> WorkbenchPass | None:
    stmt = (
        select(WorkbenchPass)
        .where(
            WorkbenchPass.user_id == user_id,
            WorkbenchPass.revoked_at.is_(None),
            WorkbenchPass.starts_at <= stamp,
            WorkbenchPass.ends_at > stamp,
        )
        .order_by(WorkbenchPass.ends_at.desc())
        .limit(1)
    )
    if lock:
        # 开通收费之前，小程序接口用的数据库账号对这张表只有读权限，
        # 而 MySQL 的 SELECT ... FOR UPDATE 要求账号有写权限：
        # 没有使用期的用户（开通收费前的所有人）提交任务不能因此报错
        # （10/10 线上实测：打开工具开关后提交名单返回 500）。先普通查询，查到了才加锁。
        # 不会漏：调用方已经先锁了用户行，开通、顺延、收回使用期也都先锁用户。
        if session.scalar(stmt) is None:
            return None
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalar(stmt)


def pass_status(session: Session, user_id: int, stamp: datetime) -> dict:
    ticket = current_pass(session, user_id, stamp)
    if ticket is None:
        return {"active": False, "expiresAt": None}
    expiry = ticket.ends_at
    for following in session.scalars(
        select(WorkbenchPass)
        .where(
            WorkbenchPass.user_id == user_id,
            WorkbenchPass.revoked_at.is_(None),
            WorkbenchPass.starts_at >= expiry,
        )
        .order_by(WorkbenchPass.starts_at)
    ):
        if following.starts_at != expiry:
            break
        expiry = following.ends_at
    return {"active": True, "expiresAt": expiry}


def paid_usage(session: Session, ticket: WorkbenchPass | None) -> bool:
    # last_used_at 是任务提交时的最少审计记录，删除、失败、取消均不抹去使用事实。
    if ticket is None or ticket.order_id is None:
        return False
    return (
        ticket.last_used_at is not None
        or session.scalar(
            select(AdvisorTask.id)
            .where(AdvisorTask.pass_id == ticket.id, AdvisorTask.usage_source == "pass")
            .limit(1)
        )
        is not None
    )


def refund_reason(session: Session, order: PayOrder, stamp: datetime, *, lock=False) -> str | None:
    if order.platform == "ios":
        return "苹果手机付款的订单请在 App Store 申请退款"
    if order.platform != "android":
        return "支付方式还没确认，可点“遇到问题”告诉我们"
    if order.status != "completed" or order.refund_status is not None:
        return "订单当前状态不能自助退款，可点“遇到问题”告诉我们"
    if order.paid_time is None or stamp > order.paid_time + timedelta(days=7):
        return "已超过 7 天"
    invoice = order_invoice(session, order)
    if invoice is not None and invoice.status == "issued":
        # 已开的发票要先冲红，不能自助退
        return "已开过发票，退款请点“遇到问题”告诉我们"
    ticket = order_pass(session, order, lock=lock)
    if ticket is None or ticket.revoked_at is not None:
        return "使用期还没开通或已取消，可点“遇到问题”告诉我们"
    if paid_usage(session, ticket):
        return "已提交过超过 50 家的名单或已超出每月免费次数"
    return None


def order_invoice(session: Session, order: PayOrder, *, lock=False) -> InvoiceRequest | None:
    stmt = select(InvoiceRequest).where(InvoiceRequest.order_id == order.id)
    return session.scalar(stmt.with_for_update() if lock else stmt)


def void_invoice(session: Session, order: PayOrder, stamp: datetime) -> None:
    """退款时还没开的发票作废；已开的留着，由创始人按退款冲红（不能自助退款，见 refund_reason）。"""
    invoice = order_invoice(session, order, lock=True)
    if invoice is not None and invoice.status == "pending":
        invoice.status, invoice.note, invoice.updated_at = "rejected", INVOICE_VOID_NOTE, stamp


def invoice_out(invoice: InvoiceRequest | None) -> dict | None:
    if invoice is None:
        return None
    return {
        "status": INVOICE_LABELS[invoice.status],
        "titleType": invoice.title_type,
        "title": invoice.title,
        "taxId": invoice.tax_id,
        "email": invoice.email,
        "note": invoice.note,
        "issuedAt": invoice.issued_at,
        "canEdit": invoice.status == "pending",
    }


def revoke_pass(session: Session, order: PayOrder, stamp: datetime) -> None:
    ticket = order_pass(session, order, lock=True)
    if ticket is not None:
        revoke_workbench_pass(session, ticket, stamp)


def unused_pass_time(ticket: WorkbenchPass | None, stamp: datetime) -> timedelta:
    if ticket is None or ticket.revoked_at is not None:
        return timedelta(0)
    return max(timedelta(0), ticket.ends_at - max(stamp, ticket.starts_at))


def revoke_workbench_pass(session: Session, ticket: WorkbenchPass, stamp: datetime) -> None:
    """调用方先锁用户和该段；退款和奖励收回共用后续使用期的前移逻辑。"""
    if ticket.revoked_at is not None:
        return
    unused = unused_pass_time(ticket, stamp)
    ticket.revoked_at = stamp
    if unused <= timedelta(0):
        return
    for later in session.scalars(
        select(WorkbenchPass)
        .where(
            WorkbenchPass.user_id == ticket.user_id,
            WorkbenchPass.revoked_at.is_(None),
            WorkbenchPass.starts_at >= ticket.ends_at,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        later.starts_at -= unused
        later.ends_at -= unused


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


def _paid_time(value) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def accept_payment(
    session: Session,
    order: PayOrder,
    data: dict,
    settings: Settings,
    stamp: datetime,
    push: dict | None = None,
) -> bool:
    # 调用方先锁用户、再锁订单；订单唯一使用期防止重复开通。
    # 返回 False 时推送应答失败（微信重推）；核验不过转人工的订单也返回 False，不算发货。
    if data.get("status") not in (2, 3, 4):
        return False
    accepted = order_pass(session, order, lock=True) is not None or order.status in (
        "refunded",
        "refunding",
    )
    paid_time = _paid_time(data.get("paid_time"))
    problem = (
        "支付金额与订单不符"
        if data.get("order_fee") != order.price or data.get("paid_fee") != order.price
        else "查单缺少有效支付时间"
        if paid_time is None
        else None
    )
    if problem is not None:
        if not accepted and order.status != "manual":
            # 微信已确认付款但核验不过：不能留在待支付里一直查，也不能发货；转人工并告警。
            order.wx_order_id = data.get("wx_order_id")
            if type(data.get("paid_fee")) is int:
                order.paid_fee = data["paid_fee"]
            order.paid_time = paid_time
            order.platform = platform(data, settings)
            order.status, order.alerted_at, order.updated_at = "manual", None, stamp
            order.error = f"微信已确认付款，但{problem}，未发货，需人工核对"
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
    if accepted:
        return True
    order.wx_order_id = data.get("wx_order_id")
    order.paid_fee = data["paid_fee"]
    order.paid_time = paid_time
    order.platform = platform(data, settings)
    order.status, order.updated_at, order.error = "completed", stamp, None
    if not order.user_id:
        order.status, order.error = "manual", "用户已注销，需处理已付款订单"
        return True
    # 在用户锁下读最新的所有未收回段；未来已排定的段也算入顺延。
    # 按付款时间比，不按确认时间：付款时旧的一段还没结束、确认却晚到（推送延迟、查单兜底）时，
    # 新的一段仍要接在旧的后面，否则两段重叠、少给天数（Codex 复审，10/10）。
    latest = session.scalar(
        select(WorkbenchPass.ends_at)
        .where(
            WorkbenchPass.user_id == order.user_id,
            WorkbenchPass.revoked_at.is_(None),
            WorkbenchPass.ends_at > paid_time,
        )
        .order_by(WorkbenchPass.ends_at.desc())
        .limit(1)
        .with_for_update()
    )
    starts = max(paid_time, latest) if latest else paid_time
    session.add(
        WorkbenchPass(
            user_id=order.user_id,
            order_id=order.id,
            starts_at=starts,
            ends_at=starts + timedelta(days=PASS_DAYS),
        )
    )
    session.flush()
    return True


def mark_refunded(session: Session, order: PayOrder, data: dict, stamp: datetime) -> bool:
    if data.get("status") not in (5, 8):
        return False
    if data.get("left_fee") != 0:
        order.status, order.refund_status, order.error = (
            "manual",
            "partial",
            "订单部分退款，需人工核对",
        )
        return True
    order.status, order.refund_status, order.error = "refunded", "completed", None
    order.updated_at = stamp
    revoke_pass(session, order, stamp)
    void_invoice(session, order, stamp)
    return True


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
    order.updated_at = stamp
    void_invoice(session, order, stamp)
    # 请求前持久化同一个退款单号和使用期收回；网络期间不持锁阻塞推送。
    session.commit()
    order_id = order.id
    order = lock_order(session, order_id)
    if order is None or order.status != "refunding":
        session.commit()
        return
    if order.refund_retry_at and order.refund_retry_at > stamp:
        session.commit()
        return
    order.refund_attempts += 1
    order.refund_retry_at = stamp + timedelta(minutes=min(60, 2 ** min(order.refund_attempts, 6)))
    session.commit()
    try:
        data = query(session, order, client)
        order = lock_order(session, order_id)
        if order is None or order.status != "refunding":
            session.commit()
            return
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
        if platform(data, client.settings) != "android":
            order.status, order.error = "manual", "退款时无法确认普通支付平台"
            session.commit()
            return
        user = session.get(User, order.user_id) if order.user_id else None
        if user is None:
            order.status, order.error = "manual", "用户已注销，退款需人工处理"
            session.commit()
            return
        request = {
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
        session.commit()
        client.refund(request)
        # 推送可能已在外部请求期间完成退款；只在仍退款中时写回，不能覆盖终态。
        order = lock_order(session, order_id)
        if order is not None and order.status == "refunding":
            order.refund_status, order.error, order.updated_at = "pending", None, stamp
    except XPayError:
        order = lock_order(session, order_id)
        if order is not None and order.status == "refunding":
            order.refund_status, order.error = "retry", "退款请求暂未确认，将重试"
            order.refund_retry_at = order.refund_retry_at or stamp + timedelta(minutes=2)
            order.updated_at = stamp
    session.commit()


def ios_decision(session: Session, order: PayOrder | None) -> dict:
    ticket = order_pass(session, order) if order else None
    if paid_usage(session, ticket):
        completed = ticket.last_task_completed_at
        return {
            "result_code": 1,
            "result_info": "已使用",
            "evidence": (
                f"任务 {ticket.last_task_id}，完成时间 "
                f"{completed.isoformat() if completed else '尚未完成'}，提交时间 "
                f"{ticket.last_used_at.isoformat() if ticket.last_used_at else '待核对'}"
            ),
        }
    return {"result_code": 0, "result_info": "未使用", "evidence": "没有使用付费的量"}


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
        if order_pass(session, order) is None and order.status not in ("refunding", "refunded"):
            order.status, order.error, order.alerted_at, order.updated_at = (
                "manual",
                "账单已支付但本地未发货，需人工核对",
                None,
                stamp,
            )
            differences.setdefault(
                order.out_trade_no,
                {"local": local.get(order.out_trade_no), "bill": bill[order.out_trade_no]},
            )
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


def _bill_failed(
    session: Session, row: PayReconciliation, stamp: datetime, alert, reason: str
) -> None:
    # 下载或解析失败计次；满 BILL_ATTEMPTS 次转 failed 并告警，不再占用每轮 3 天名额。
    session.rollback()
    row.attempts += 1
    row.differences, row.updated_at = {"error": reason}, stamp
    newly_failed = row.attempts >= BILL_ATTEMPTS and row.status != "failed"
    row.status = "failed" if row.attempts >= BILL_ATTEMPTS else "pending"
    session.commit()
    if newly_failed:
        from youju.alerts import send_alert

        (alert or send_alert)(
            f"支付对账 {row.day} 的账单下载或解析失败 {row.attempts} 次，已停止重试，需人工核对"
        )


def download_reconcile(
    session: Session, day: date, client: XPayClient, stamp: datetime, *, alert=None
) -> None:
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
    settings = client.settings
    if not settings.xpay_bill_order_column or not settings.xpay_bill_fee_column:
        # 没配账单列名就无法解析，下载也没用：直接标为失败，不访问微信。
        row.status, row.differences = "failed", {"error": "未配置账单字段"}
        session.commit()
        return
    session.commit()
    try:
        result = client.bill(day)
        url = result.get("url", "")
        if not url:
            return  # 下一分钟继续轮询，不阻塞 cron。
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in settings.xpay_bill_hosts
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
        reconcile(session, day, parse_bill(bytes(raw), settings), stamp)
    except XPayError as exc:
        # 268490011：账单生成中，不算失败，下一分钟继续轮询。
        if exc.code != 268490011:
            _bill_failed(session, row, stamp, alert, "账单未完成或无法核验")
    except Exception:
        _bill_failed(session, row, stamp, alert, "账单下载暂不可用")


def provide_goods(session: Session, order: PayOrder, client: XPayClient) -> None:
    order_id, trade, env = order.id, order.out_trade_no, order.env
    session.commit()
    success = True
    try:
        client.provide(trade, env)
    except XPayError:
        success = False
    order = lock_order(session, order_id)
    # 退款/投诉推送可能已经先到，不能让通知发货的回写覆盖订单终态。
    if order is not None and order.status == "completed" and not order.goods_notified:
        if success:
            order.goods_notified = True
        else:
            order.provide_attempts += 1
            if order.provide_attempts >= PROVIDE_ATTEMPTS:
                order.status, order.alerted_at = "manual", None
                order.error = f"通知发货失败 {order.provide_attempts} 次，已停止重试，需人工处理"
            else:
                order.error = "通知发货暂不可用，将重试"
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
    # 支付开关只停新下单；已有的未完结订单照常处理到底，没有任何待办时不访问微信。
    if not has_work(session, settings):
        return {"processed": 0}
    open_shop = enabled(settings)
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
    # 每轮最多 100 条：退款相关和待告警的排在最前，其次是已付款订单，待支付查单放最后。
    priority = case(
        (PayOrder.status.in_(("refunding", "manual")), 0),
        (PayOrder.status == "pending", 2),
        else_=1,
    )
    ids = list(
        session.scalars(
            select(PayOrder.id)
            .where(
                (
                    (PayOrder.status == "pending")
                    & (PayOrder.created_at <= stamp - timedelta(minutes=2))
                    & ((PayOrder.query_retry_at.is_(None)) | (PayOrder.query_retry_at <= stamp))
                )
                | PayOrder.status.in_(("refunding",))
                | ((PayOrder.status == "manual") & PayOrder.alerted_at.is_(None))
                | ((PayOrder.status == "completed") & (PayOrder.goods_notified == False))  # noqa: E712
            )
            .order_by(priority, PayOrder.updated_at, PayOrder.id)
            .limit(100)
        )
    )
    for order_id in ids:
        order = lock_order(session, order_id)
        if order is None:
            session.rollback()
            continue
        try:
            if order.status == "refunding":
                order.refund_started_at = order.refund_started_at or stamp
                if not order.user_id:
                    order.status, order.error = "manual", "用户已注销，退款需人工处理"
                elif refund_exhausted(order, stamp):
                    # 最后核验已成功的退款；查单也失败则人工接手，不能无限重试。
                    confirmed = False
                    with contextlib.suppress(XPayError):
                        confirmed = mark_refunded(
                            session, order, query(session, order, client), stamp
                        )
                    if not confirmed:
                        order.status, order.error, order.alerted_at = (
                            "manual",
                            "退款尝试达到上限或超过24小时，需人工核对",
                            None,
                        )
                elif order.refund_status == "pending":
                    if order.refund_retry_at is None or order.refund_retry_at <= stamp:
                        order.refund_retry_at = stamp + timedelta(minutes=10)
                        data = query(session, order, client)
                        if (
                            not mark_refunded(session, order, data, stamp)
                            and data.get("status") == 7
                        ):
                            order.refund_status = "retry"
                else:
                    start_refund(session, order, client, stamp)
            elif order.status == "pending":
                overdue = order.created_at <= stamp - timedelta(hours=48)
                order.query_retry_at = stamp + timedelta(
                    minutes=1 if order.created_at > stamp - timedelta(minutes=30) else 10
                )
                try:
                    data = query(session, order, client)
                except XPayError:
                    if not overdue:
                        raise
                    # 查单一直失败也不能永远轮询；真付了款的由推送或账单对账转人工。
                    data = None
                    order.status, order.error = "closed", "超过 48 小时无法确认支付，已关闭"
                if data is not None and not mark_refunded(session, order, data, stamp):
                    if data.get("status") == 6 or (overdue and data.get("status") in (0, 1)):
                        order.status = "closed"
                    else:
                        accept_payment(session, order, data, settings, stamp)
                session.commit()  # 先持久化使用期再发通知；通知失败由下一次补发。
            if order.status == "completed" and not order.goods_notified:
                provide_goods(session, order, client)
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
    # 9:30 的任务创建记录后，每分钟继续未完成的账单生成/下载；失败满 5 次的不再占名额。
    days_stmt = select(PayReconciliation.day).where(PayReconciliation.status == "pending")
    if not open_shop:
        days_stmt = days_stmt.where(PayReconciliation.local_count > 0)
    days = list(session.scalars(days_stmt.order_by(PayReconciliation.day).limit(3)))
    for day in days:
        download_reconcile(session, day, client, stamp, alert=alert)
    return {"processed": len(ids)}


def cleanup(session: Session, settings, stamp: datetime) -> dict:
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
    return {"pay_events": events}


def anonymize(session: Session, user: User) -> None:
    # 先用普通查询确认有没有订单：youju_api 在开通支付授权前只有 SELECT 权限，
    # 没有订单时不能发出 SELECT ... FOR UPDATE（MySQL 按语句检查权限），否则注销会失败。
    if session.scalar(select(PayOrder.id).where(PayOrder.user_id == user.id).limit(1)) is None:
        return
    session.scalar(select(User.id).where(User.id == user.id).with_for_update())
    orders = list(
        session.scalars(select(PayOrder).where(PayOrder.user_id == user.id).with_for_update())
    )
    if not orders:
        return
    # 完成/取消任务先写 task 再写 pass；注销必须同序，避免彼此等待死锁。
    list(
        session.scalars(
            select(AdvisorTask.id)
            .where(
                AdvisorTask.user_id == user.id,
            )
            .with_for_update()
        )
    )
    pseudonym = log_uid(user.openid, session.get_bind())
    for order in orders:
        revoke_pass(session, order, datetime.now(UTC))
        ticket = order_pass(session, order)
        if ticket:
            ticket.user_id = None
        order.user_id, order.pseudonym = None, pseudonym
        if order.status == "pending":
            order.status = "closed"
        elif order.status == "refunding":
            order.status, order.error = "manual", "用户已注销，需核对未完成订单"
    for row in session.scalars(
        select(PayEvent).where(PayEvent.order_id.in_([o.id for o in orders]))
    ):
        row.summary = {k: v for k, v in row.summary.items() if k not in ("OpenId", "openid")}
    # 开票申请是订单的会计凭证，和订单一样只断开用户、按订单保存期保留（隐私指引已写明）
    session.execute(
        update(InvoiceRequest).where(InvoiceRequest.user_id == user.id).values(user_id=None)
    )
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
        "reconciliation_failed": session.scalar(
            select(func.count(PayReconciliation.id)).where(PayReconciliation.status == "failed")
        )
        or 0,
        "differences": session.scalar(
            select(func.count(PayReconciliation.id)).where(PayReconciliation.status == "different")
        )
        or 0,
        "invoices_pending": session.scalar(
            select(func.count(InvoiceRequest.id)).where(InvoiceRequest.status == "pending")
        )
        or 0,
    }
