"""付款开通批量工具使用期：事务内一次发货；补单、退款及对账。

批量工具两档（D-070）：30 天、90 天，一次付清、到期不扣钱。逐笔记账：每笔订单一段使用期，
每次赠送（续买多送、加量体验折算）一笔流水（pass_rewards）加一段使用期；退哪笔只退那笔，
随那笔订单送的续买天数一起收回。

未知平台不主动退款；任何原始用户内容或微信错误文字均不写入事件摘要。
"""

from __future__ import annotations

import contextlib
import csv
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import case, delete, func, or_, select, true, update
from sqlalchemy.orm import Session

from youju import advisor_tasks
from youju.access_log import log_uid
from youju.config import Settings, get_settings
from youju.models import (
    AdvisorTask,
    InvoiceRequest,
    PassReward,
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


PASS_NAME = "批量工具"
DAY = timedelta(days=1)
# 续买多送（D-070 ⑥）："到期前后 7 天内续买，每次多送 3 天"，资格按付款时间算
RENEW_BONUS_DAYS = 3
RENEW_WINDOW = timedelta(days=7)
# 随订单发的赠送：流水的 ref_type（ref_id 是订单编号）。续买多送随订单退款一起收回；
# 加量体验折算的是用户自己没用完的体验时间，退款不收回（D-070 ③"不浪费"）
RENEWAL_REF = "order_renewal"
TRIAL_REF = "order_trial"
ORDER_GIFT_REFS = (RENEWAL_REF, TRIAL_REF)
# 退款发起方：self 用户自助（7 天内没用过付费的量）；staff 负责人按规则发起（系统故障、停止服务）
SELF_REFUND, STAFF_REFUND = "self", "staff"


def pass_name(days: int) -> str:
    return f"{PASS_NAME} {days} 天"


def products(settings: Settings) -> dict[str, tuple[int, int]]:
    """在售的档位：商品编号 →（价格分，天数），按天数从短到长。没配价格的那一档不卖。"""
    tiers = (
        (settings.xpay_product_pass, settings.xpay_price_pass, 30),
        (
            getattr(settings, "xpay_product_pass_90", ""),
            getattr(settings, "xpay_price_pass_90", None),
            90,
        ),
    )
    return {product: (price, days) for product, price, days in tiers if product and price}


def renew_bonus_enabled(settings) -> bool:
    return bool(getattr(settings, "renew_bonus_enabled", False))


def renewal_due(
    session: Session, user_id: int, paid_time: datetime, *, order_id: int | None = None
) -> bool:
    """这次付款算不算"到期前后 7 天内续买"（D-070 ⑥）。资格按付款时间算：

    - 以前买过：有一笔付款早于这次、使用期没收回（没退款）的订单；加量体验、赠送的天数不算买过；
    - 到期：这个用户现在所有没收回的使用期里最晚的结束时间（已经续上的、送的天数都算），
      付款时间在它前后 7 天以内（含两端）。提前好几次续买的，只有落在最后到期前后 7 天的那次送。
    调用方已锁用户行，这次订单自己的使用期还没建（order_id 只是保险，排除它）。"""
    others = (
        or_(WorkbenchPass.order_id.is_(None), WorkbenchPass.order_id != order_id)
        if order_id is not None
        else true()
    )
    bought = session.scalar(
        select(WorkbenchPass.id)
        .join(PayOrder, PayOrder.id == WorkbenchPass.order_id)
        .where(
            WorkbenchPass.user_id == user_id,
            WorkbenchPass.revoked_at.is_(None),
            PayOrder.paid_time < paid_time,
            others,
        )
        .limit(1)
    )
    if bought is None:
        return False
    expiry = session.scalar(
        select(func.max(WorkbenchPass.ends_at)).where(
            WorkbenchPass.user_id == user_id, WorkbenchPass.revoked_at.is_(None), others
        )
    )
    return expiry is not None and expiry - RENEW_WINDOW <= paid_time <= expiry + RENEW_WINDOW


def renew_offer(session: Session, settings, user: User | None, stamp: datetime) -> dict | None:
    """付款页和价格同一屏写的续买规则（开关关着时为 None，页面不提）；
    eligibleNow：现在付款符合"到期前后 7 天内续买"（实际按付款时间再算一次）。"""
    if not renew_bonus_enabled(settings):
        return None
    return {
        "days": RENEW_BONUS_DAYS,
        "windowDays": RENEW_WINDOW.days,
        "eligibleNow": user is not None and renewal_due(session, user.id, stamp),
    }


def selling_to(settings, openid: str | None) -> bool:
    """这个用户能不能看到购买入口、下新单：总开关开着，并且对全体开放或在测试名单里。
    补单、退款、对账等定时任务只看总开关（enabled），已有订单照常处理到底。"""
    if not enabled(settings):
        return False
    if getattr(settings, "pay_audience", "whitelist") == "all":
        return True
    testers = {s.strip() for s in getattr(settings, "pay_openids", "").split(",") if s.strip()}
    return bool(openid) and openid in testers


def invoice_type(settings) -> str:
    """购买页写的"能开哪种发票"（YOUJU_PAY_INVOICE_TYPE）；没填或还是占位时为空串。"""
    value = getattr(settings, "pay_invoice_type", "").strip()
    return "" if not value or "【" in value or "待" in value else value


def orderable(settings) -> bool:
    """能不能真的下单：配了 offerId 和价格；现网（真扣钱）还必须配了定稿的协议版本
    （YOUJU_PAY_TERMS_VERSION）和能开哪种发票（YOUJU_PAY_INVOICE_TYPE，D-070 付款前要看得到）。
    沙箱不扣真钱，协议定稿前也能联调。商品列表、下单、购买入口都按这一条判断，
    不出现看得到价格却付不了款的入口，也不能绕过购买页直接下单。"""
    if not getattr(settings, "xpay_offer_id", "") or not products(settings):
        return False
    return settings.xpay_env == 1 or bool(
        getattr(settings, "pay_terms_version", "") and invoice_type(settings)
    )


def entry(session: Session, settings, user: User | None) -> dict:
    """小程序要不要显示购买入口和"我的订单"：正在售卖（开关开、对这个用户开放、能下单），
    或这个用户有过订单（停卖后已付款的用户还要能看订单、退款、开票）。"""
    selling = selling_to(settings, user.openid if user else None) and orderable(settings)
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
    """自助全额退款的条件（D-070 统一退款口径）：安卓、鸿蒙、电脑版微信付款（微信的普通支付），
    付款 7 天内（含恰好 7 天），这一笔的使用期没用过付费的量（没超出每月免费次数、
    没有一次超过 50 家），也没开过发票。只看这一笔自己：提前续买、一次买了几笔的，每笔分开算。"""
    if order.platform == "ios":
        return "苹果手机付款的订单请在 App Store 申请退款"
    if order.platform != "android":
        return "支付方式还没确认，可点“遇到问题”告诉我们"
    if order.status != "completed" or order.refund_status is not None:
        return "订单当前状态不能自助退款，可点“遇到问题”告诉我们"
    if order.paid_time is None or stamp > order.paid_time + timedelta(days=7):
        return "已超过 7 天"
    # 退款时加锁读发票（锁顺序：用户 → 订单 → 发票，和管理台开票一致）：否则查完发票、
    # 作废之前，管理台可能刚把它标成已开，已开发票的订单就被退了款（Grok 红队，10/16）
    invoice = order_invoice(session, order, lock=lock)
    if invoice is not None and invoice.status == "issued":
        # 已开的发票要先冲红，不能自助退
        return "已开过发票，退款请点“遇到问题”告诉我们"
    ticket = order_pass(session, order, lock=lock)
    if ticket is None or ticket.revoked_at is not None:
        return "使用期还没开通或已取消，可点“遇到问题”告诉我们"
    if paid_usage(session, ticket):
        return "这笔的使用期已用过付费的量（超出了每月免费次数，或一次超过 50 家）"
    return None


def remaining_days(
    order: PayOrder, ticket: WorkbenchPass | None, stamp: datetime, extra_days: int = 0
) -> int:
    """这笔订单没提供的天数（系统故障、停止服务按剩余天数退）：使用期剩下的时间，不足一天按一天，
    还没开始的（提前续买排在后面的）按整笔天数，已收回的为 0；extra_days 是此前因系统故障不能用、
    又没有延长补上的天数（负责人核实后填，付费服务协议 v2 第二节）。最多整笔天数。
    送的天数不算钱，不在这里。"""
    left = math.ceil(unused_pass_time(ticket, stamp) / DAY)
    return min(order.days, left + max(0, extra_days))


def remaining_fee(
    order: PayOrder, ticket: WorkbenchPass | None, stamp: datetime, extra_days: int = 0
) -> int:
    """按没提供的天数退的金额（分）：订单金额 × 天数 ÷ 这一档的天数，不足一分按一分。"""
    return -(-order.price * remaining_days(order, ticket, stamp, extra_days) // order.days)


def staff_refund_problem(
    session: Session, order: PayOrder, *, invoice_reversed: bool = False
) -> str | None:
    """负责人按规则发起退款（系统故障、停止服务，youju pay-refund）之前的检查；None 表示可以退。
    调用方已按 用户 → 订单 的顺序加锁。"""
    if order.platform == "ios":
        return "苹果手机付款的订单只能由用户在 App Store 申请退款，是否退款由苹果公司决定"
    if order.platform != "android":
        return "支付平台没有确认，不能主动退款，请核对后人工处理"
    if order.status != "completed" or order.refund_status is not None:
        return f"订单当前状态（{STATUS_LABELS[order.status]}）不能退款"
    if order.user_id is None:
        return "用户已注销，退款需要人工核对"
    invoice = order_invoice(session, order, lock=True)
    if invoice is not None and invoice.status == "issued" and not invoice_reversed:
        return "这笔已开过发票：先按退款金额开红字发票，再加 --invoice-reversed 重新运行"
    return None


def begin_refund(
    session: Session,
    order: PayOrder,
    stamp: datetime,
    *,
    fee: int,
    source: str,
    note: str | None = None,
) -> None:
    """受理一笔退款：记下退多少、谁发起，收回这一笔的使用期和随它送的续买天数，作废没开的发票。
    之后由 start_refund 向微信发起（同一个退款单号，网络失败按原单号重试）。
    调用方已锁 用户 → 订单。"""
    order.refund_fee, order.refund_source, order.refund_note = fee, source, note
    revoke_pass(session, order, stamp)
    void_invoice(session, order, stamp)


def order_invoice(session: Session, order: PayOrder, *, lock=False) -> InvoiceRequest | None:
    stmt = select(InvoiceRequest).where(InvoiceRequest.order_id == order.id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalar(stmt)


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
    """退款（自助、人工、苹果、平台）：收回这一笔的使用期，随它送的续买天数一起收回；
    加量体验折算的天数是用户自己的体验时间，不收回（跟着往前挪）。调用方已锁 用户 → 订单。"""
    ticket = order_pass(session, order, lock=True)
    if ticket is not None:
        revoke_workbench_pass(session, ticket, stamp)
    from youju import rewards

    for gift, _ in order_gifts(session, order, lock=True):
        if gift.ref_type == RENEWAL_REF and gift.user_id is not None:
            rewards.revoke(session, gift.id, "随订单退款", stamp)


def order_gifts(
    session: Session, order: PayOrder, *, lock=False
) -> list[tuple[PassReward, WorkbenchPass | None]]:
    """随这笔订单送的天数（续买多送、加量体验折算）：每次一笔流水和对应的一段使用期。"""
    stmt = (
        select(PassReward)
        .where(PassReward.ref_type.in_(ORDER_GIFT_REFS), PassReward.ref_id == order.id)
        .order_by(PassReward.id)
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    gifts = list(session.scalars(stmt))
    if not gifts:
        return []
    tickets = select(WorkbenchPass).where(WorkbenchPass.reward_id.in_([g.id for g in gifts]))
    if lock:
        tickets = tickets.with_for_update().execution_options(populate_existing=True)
    by_reward = {t.reward_id: t for t in session.scalars(tickets)}
    return [(gift, by_reward.get(gift.id)) for gift in gifts]


def _append_gift(
    session: Session,
    order: PayOrder,
    kind: str,
    ref_type: str,
    days: int,
    stamp: datetime,
    note: str | None = None,
) -> PassReward:
    """记一笔赠送，并接在这个用户所有没收回的使用期最后面（这时就是刚开通的这一笔后面）。"""
    gift = PassReward(
        user_id=order.user_id,
        kind=kind,
        requested_days=days,
        granted_days=days,
        ref_type=ref_type,
        ref_id=order.id,
        granted_at=stamp,
        note=note,
    )
    session.add(gift)
    session.flush()
    latest = session.scalar(
        select(func.max(WorkbenchPass.ends_at)).where(
            WorkbenchPass.user_id == order.user_id, WorkbenchPass.revoked_at.is_(None)
        )
    )
    starts = max(stamp, latest) if latest else stamp
    session.add(
        WorkbenchPass(
            user_id=order.user_id,
            reward_id=gift.id,
            starts_at=starts,
            ends_at=starts + timedelta(days=days),
        )
    )
    session.flush()
    return gift


def _convert_trial(session: Session, order: PayOrder, stamp: datetime) -> None:
    """体验期内买了批量工具（D-070 ③）：使用期立即生效，体验就此结束；
    没用完的体验时间折成天数（不足一天按一天）接在这一笔后面，不浪费。
    看的是开通（确认）时体验还剩多少：使用期也从确认时起算，确认之前用户一直在用体验，不算浪费。
    付款后、确认前才开始的体验同样处理——使用期和体验不同时进行。"""
    from youju import boost

    trial = boost.trial_of(session, order.user_id)
    if not boost.is_active(trial, stamp):
        return
    left = trial.ends_at - stamp
    days = math.ceil(left / DAY)
    hours = math.ceil(left / timedelta(hours=1))
    note = f"加量体验还剩约 {hours} 小时（原定 {_beijing(trial.ends_at)} 结束），折成 {days} 天"
    # 体验到现在为止：之后提交按使用期算，不再记体验次数（结束时间要晚于开始时间，表上有约束）
    trial.ends_at = max(stamp, trial.started_at + timedelta(microseconds=1))
    _append_gift(session, order, "trial", TRIAL_REF, days, stamp, note)


def _beijing(value: datetime) -> str:
    return advisor_tasks.business_now(value).strftime("%m-%d %H:%M")


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
    # 环境：请求的 env 是 0 现网、1 沙箱，官方 query_order 返回的 env_type 却是 1 现网、2 沙箱
    # （10/16 核对官方文档；原来直接比较，按文档每次查单都会判不一致）。两种写法都认，
    # 只拒绝明确是另一个环境的值；查单本身按 env 查，不会查到另一个环境的单。
    if (
        data.get("order_id") != order.out_trade_no
        or data.get("openid", openid) != openid
        or data.get("env_type", order.env + 1) not in (order.env, order.env + 1)
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
    # 最早从确认开通时起算：推送晚到、补单兜底时，确认之前用不了付费的量，
    # 不能让这段时间白白算进使用期（D-070 开卖前 Codex 审阅；正常情况下确认就在付款那一刻）。
    # 体验折算也就只需看确认时还剩多少
    starts = max(paid_time, stamp, latest) if latest else max(paid_time, stamp)
    # 续买资格要在这一笔的使用期建好之前判断（"到期"是以前那些使用期的到期）
    renewal = order.bonus_days > 0 and renewal_due(
        session, order.user_id, paid_time, order_id=order.id
    )
    session.add(
        WorkbenchPass(
            user_id=order.user_id,
            order_id=order.id,
            starts_at=starts,
            ends_at=starts + timedelta(days=order.days),
        )
    )
    session.flush()
    # 每次赠送单独记一笔，接在这一笔后面：先续买多送，再体验折算
    if renewal:
        _append_gift(session, order, "renewal", RENEWAL_REF, order.bonus_days, stamp)
    _convert_trial(session, order, stamp)
    return True


def mark_refunded(session: Session, order: PayOrder, data: dict, stamp: datetime) -> bool:
    if data.get("status") not in (5, 8):
        return False
    left = data.get("left_fee")
    # 按剩余天数退的（refund_fee 小于订单金额），退完还剩"订单金额 − 退款金额"；
    # 全额退的（自助、苹果、平台）剩 0。都不是就转人工核对
    planned = order.price - order.refund_fee if order.refund_fee is not None else 0
    if type(left) is not int or left not in (0, planned):
        order.status, order.refund_status, order.error = (
            "manual",
            "partial",
            "订单部分退款，退款金额和预期不符，需人工核对",
        )
        return True
    order.status, order.refund_status, order.error = "refunded", "completed", None
    order.refund_fee = order.price - left
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
        fee = order.refund_fee if order.refund_fee is not None else order.price
        staff = order.refund_source == STAFF_REFUND
        request = {
            "openid": user.openid,
            "order_id": order.out_trade_no,
            "refund_order_id": order.refund_order_id,
            "left_fee": data["left_fee"],
            "refund_fee": fee,
            "biz_meta": order.out_trade_no,
            # 官方 refund_order 的枚举（10/18 核对原文）：退款原因 1 产品问题、
            # 3 意愿问题（用户主动退款）；来源 1 人工客服、2 用户自己发起。
            # 负责人按规则退的是系统故障或停止服务
            "refund_reason": "1" if staff else "3",
            "req_from": "1" if staff else "2",
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
        # 只收回订单自己的使用期：送的天数（续买多送、体验折算）由 rewards.anonymize 统一作废，
        # 那不是退款，流水备注不写"随订单退款"
        ticket = order_pass(session, order, lock=True)
        if ticket is not None:
            revoke_workbench_pass(session, ticket, datetime.now(UTC))
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
