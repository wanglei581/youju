"""付款开通批量工具使用期：事务内一次发货；补单、退款及对账。

批量工具两档（D-070）：30 天、90 天，一次付清、到期不扣钱。逐笔记账：每笔订单一段使用期，
每次赠送（续买多送、加量体验折算）一笔流水（pass_rewards）加一段使用期；退哪笔只退那笔，
随那笔订单送的续买天数一起收回。

未知平台不主动退款；任何原始用户内容或微信错误文字均不写入事件摘要。
"""

from __future__ import annotations

import contextlib
import csv
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import case, delete, exists, func, or_, select, true, update
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
from youju.xpay import XPayClient, XPayError, wx_http

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    # 还没确认付款结果：付了款的会自动开通，没付的不扣钱、稍后关单。原来写"待支付"，
    # 付了款、推送和补单还没到的那一两分钟里，用户看到的是"待支付"，以为钱白扣了（10/19 沙箱实测）
    "pending": "待确认",
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
# 待确认的订单查了 48 小时都没查成（查单一直出错）：关单，并告警一次——现网真扣钱，
# 万一用户其实付了款、推送和对账也没接上，有人去核对；告警没发出去下一轮接着发（Codex 10/29）。
# 付款前就被微信拒了的单（道具没发布到现网等，微信那边可能没有这一单）也会走到这里
UNCONFIRMED_PAYMENT_ERROR = (
    "超过 48 小时查不到付款结果，已关闭；用户说付了款的，到小程序后台虚拟支付的交易订单里核对"
)
# 这类关单里还没发出告警的
UNCONFIRMED_CLOSE_UNALERTED = (
    (PayOrder.status == "closed")
    & PayOrder.alerted_at.is_(None)
    & (PayOrder.error == UNCONFIRMED_PAYMENT_ERROR)
)
# 未完结订单：支付关闭后仍要处理到底，不能冻结。
OPEN_ORDER = (
    PayOrder.status.in_(("pending", "refunding"))
    | ((PayOrder.status == "manual") & PayOrder.alerted_at.is_(None))
    | ((PayOrder.status == "completed") & (PayOrder.goods_notified == False))  # noqa: E712
    | UNCONFIRMED_CLOSE_UNALERTED
)


def enabled(settings) -> bool:
    """是否接受新订单（下单、商品接口）；不决定已有订单是否继续处理。"""
    return bool(getattr(settings, "pay_enabled", False) and settings.advisor_enabled)


def has_work(session: Session, settings) -> bool:
    """定时任务是否需要运行：支付开启，或库里还有未完结订单、当天有本地订单的待完成对账，
    或近 7 天有投诉（不转人工的投诉要单独告警，见 complaint_alerts）。

    支付关闭且没有这些记录时只做这几次本地查询，不创建 HTTP 客户端、不访问微信。
    """
    if enabled(settings):
        return True
    if session.scalar(select(PayOrder.id).where(OPEN_ORDER).limit(1)) is not None:
        return True
    if (
        session.scalar(
            select(PayEvent.id)
            .where(
                PayEvent.kind == COMPLAINT_EVENT,
                PayEvent.created_at > datetime.now(UTC) - COMPLAINT_ALERT_WINDOW,
            )
            .limit(1)
        )
        is not None
    ):
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
# 投诉推送写在订单上的说明（api/pay.py 收到 xpay_complaint_notify 时）：自助退款据此认出投诉中的订单
COMPLAINT_ERROR = "收到用户投诉"
COMPLAINT_EVENT = "xpay_complaint_notify"
# 不转人工的投诉（退款中、已退款、已关闭的单）按投诉事件单独告警，只看最近 7 天的事件
COMPLAINT_ALERT_WINDOW = timedelta(days=7)
# 投诉时订单是这几种状态就不改状态：转人工会让退款中的单停掉自动退款、已退款的单倒退成"人工核对中"
COMPLAINT_KEEPS_STATUS = frozenset({"refunding", "refunded", "closed"})
# 付款前就被微信拒了的错误码（官方 wx.requestVirtualPayment 错误码表里下单阶段的错误和风控拦截，
# 10/29 核对；
# 和小程序 utils/pay.js 的 NOT_STARTED 一致）。-15003"系统错误"说不清发生在哪一步，不在里面
NOT_STARTED_CODES = frozenset(
    {1001, -4, -15001, -15002, -15004, -15005, -15006, -15007, -15008, -15009, -15010}
    | {-15011, -15012, -15013, -15014, -15016, -15017, -15018, -15019, -15020, -15021}
)
# 小程序报告"付款没发起"只认 10 分钟内下的单（和下新单前查旧单的窗口一样）
ABANDON_WINDOW = timedelta(minutes=10)
# 退款发起方：self 用户自助（付款 24 小时内没用过付费的量，D-081）；
# staff 负责人按规则发起（系统故障、停止服务）
SELF_REFUND_WINDOW = timedelta(hours=24)
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


# 开卖按小程序版本放开（D-074）：小程序在商品、下单、能力、我的、提交任务这几个请求的头里带上
# 代码包里的付费协议版本号和小程序环境（开发版、体验版、正式版）。
PAY_TERMS_HEADER = "X-Youju-Pay-Terms"
CLIENT_ENV_HEADER = "X-Youju-Env"
TEST_BUILDS = frozenset({"develop", "trial"})


@dataclass(frozen=True)
class Client:
    """请求来自哪个小程序包。terms：代码包里《付费服务协议》的版本号；
    env：develop 开发版 / trial 体验版 / release 正式版。
    老版本不带这两个头：terms 为空，env 按正式版（release）算——老版本永远看不到购买入口。"""

    terms: str = ""
    env: str = "release"


def client_of(headers: Mapping[str, str]) -> Client:
    """从请求头读出小程序包；认不出的环境一律按正式版算（宁可看不到，不能多放）。

    这两个头由小程序自己填，只用来决定"谁看得到购买入口"（审核和合规上的展示），不是安全边界：
    改了头也绕不过钱相关的检查——测试阶段只给测试名单（openid 由微信云托管注入，伪造不了），
    下单时还核对请求体里用户同意的协议版本（老版本带的是草稿号，409），老版本的付款按钮本来就不可用。"""
    env = (headers.get(CLIENT_ENV_HEADER) or "").strip().lower()
    terms = (headers.get(PAY_TERMS_HEADER) or "").strip()[:40]
    return Client(terms=terms, env=env if env in TEST_BUILDS else "release")


def testers(settings) -> set[str]:
    return {s.strip() for s in getattr(settings, "pay_openids", "").split(",") if s.strip()}


def selling_to(settings, openid: str | None, client: Client) -> bool:
    """这个用户、这个小程序包能不能看到购买入口、下新单（D-058、D-074）：

    - 总开关开着；
    - 测试阶段（YOUJU_PAY_AUDIENCE 还是 whitelist）：只给测试名单里的人，并且只在开发版、体验版——
      正式版不出现白名单购买（运营规范 5.19 人群对抗）；
      测试也是现网真付（沙箱已去掉，创始人 10/29）；
    - 只给代码包里带着定稿付费协议版本号（YOUJU_PAY_TERMS_VERSION）的小程序——
      开卖要单独提审一版，老版本永远看不到价格。
    补单、退款、对账等定时任务只看总开关（enabled），已有订单照常处理到底。"""
    if not enabled(settings) or shutdown_notice(settings):
        return False
    testing = getattr(settings, "pay_audience", "whitelist") != "all"
    tester = bool(openid) and openid in testers(settings) and client.env in TEST_BUILDS
    if testing and not tester:
        return False
    terms = getattr(settings, "pay_terms_version", "")
    return bool(terms) and client.terms == terms


def invoice_type(settings) -> str:
    """购买页写的"能开哪种发票"（YOUJU_PAY_INVOICE_TYPE）；没填或还是占位时为空串。"""
    value = getattr(settings, "pay_invoice_type", "").strip()
    return "" if not value or "【" in value or "待" in value else value


def orderable(settings) -> bool:
    """能不能真的下单（新订单一律现网、真扣钱）：配了 offerId、价格、现网 AppKey
    （没配下单签不了名，只会 503，Codex 10/29），定稿的协议版本（YOUJU_PAY_TERMS_VERSION）
    和能开哪种发票（YOUJU_PAY_INVOICE_TYPE，D-070 付款前要看得到）。
    商品列表、下单、购买入口都按这一条判断，不出现看得到价格却付不了款的入口，
    也不能绕过购买页直接下单。"""
    if not getattr(settings, "xpay_offer_id", "") or not products(settings):
        return False
    key = getattr(settings, "xpay_appkey_prod", None)
    return bool(
        key is not None
        and key.get_secret_value().strip()
        and getattr(settings, "pay_terms_version", "")
        and invoice_type(settings)
    )


def entry(session: Session, settings, user: User | None, client: Client) -> dict:
    """小程序要不要显示购买入口和"我的订单"：正在售卖（开关开、对这个用户和这个小程序包开放、能下单），
    或这个用户有过订单（停卖后已付款的用户还要能看订单、退款、开票）。"""
    selling = selling_to(settings, user.openid if user else None, client) and orderable(settings)
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


def pass_summary(session: Session, user_id: int, stamp: datetime) -> dict:
    """我的页用的使用期：现在有没有、到哪天（pass_status），没有时再说以前有没有过、已经到期
    （退款收回的不算）。我的页头的"批量工具"标识只看 active；"批量工具"一行的小字，到期了的写
    "已到期，可再买一次"，从没开通过的（比如只有一张待确认的订单）写价格，不写"已到期"。"""
    status = pass_status(session, user_id, stamp)
    status["expired"] = not status["active"] and (
        session.scalar(
            select(WorkbenchPass.id)
            .where(
                WorkbenchPass.user_id == user_id,
                WorkbenchPass.revoked_at.is_(None),
                WorkbenchPass.ends_at <= stamp,
            )
            .limit(1)
        )
        is not None
    )
    return status


# 付费任务没出结果的状态（付费服务协议第二条第 2 项"系统出错、没出结果的不算用过"，
# 开卖那一版必做第 13 项）
NO_RESULT_STATES = ("failed", "cancelled")


def paid_usage(
    session: Session, ticket: WorkbenchPass | None, states: set[str] | None = None
) -> bool:
    """这段使用期用没用过付费的量（D-081）：记为付费的任务出了结果（完成时记在 last_used_at，
    任务删了、注销了也还在），或者还在排队、处理中（结果可能马上就出来），算用过；系统出错、
    排队时取消、处理前删掉的不算。认不出的状态按用过算：宁可核实后人工退，也不让没核实的自助退走。
    states：调用方已经读到的这段使用期付费任务的状态（paid_task_states），不传就现读。"""
    if ticket is None or ticket.order_id is None:
        return False
    if ticket.last_used_at is not None:
        return True
    if states is None:
        states = paid_task_states(session, ticket.id)
    return bool(states - set(NO_RESULT_STATES))


def paid_task_states(session: Session, pass_id: int | None, *, lock=False) -> set[str]:
    """这段使用期下记为付费的任务现在是什么状态。lock=True（自助退款）：要在锁使用期之前调用，
    锁顺序是用户 → 任务 → 使用期，和任务完成、取消、注销一致；锁定读读得到别的会话刚交的任务，
    会话是可重复读也一样（普通查询会停在事务开始时的快照上）。"""
    if pass_id is None:
        return set()
    stmt = select(AdvisorTask.status).where(
        AdvisorTask.pass_id == pass_id, AdvisorTask.usage_source == "pass"
    )
    if lock:
        stmt = stmt.with_for_update()
    return set(session.scalars(stmt))


def refund_reason(session: Session, order: PayOrder, stamp: datetime, *, lock=False) -> str | None:
    """自助全额退款（无理由退款）的条件（D-070 统一退款口径；时限 D-081 由 7 天改为 24 小时，
    创始人 10/19："24 小时内没用过付费的量才能退，用过就不能退"）：安卓、鸿蒙、电脑版微信付款
    （微信的普通支付），付款 24 小时内（含恰好 24 小时），这一笔的使用期没用过付费的量（没超出
    每月免费次数、没有一次超过 50 家），也没开过发票。只看这一笔自己：提前续买、一次买了几笔的，
    每笔分开算；每个账号每个自然月只能申请 1 次（D-083，见 monthly_self_refunds）。
    付了款却没开通的人工单另走 undelivered_refund_reason，不受这个时限和次数。"""
    if order.platform == "ios":
        return "苹果手机付款的订单请在 App Store 申请退款"
    if order.platform != "android":
        return "支付方式还没确认，可点“遇到问题”告诉我们"
    if order.status == "manual" and order.refund_status is None:
        return undelivered_refund_reason(session, order, lock=lock)
    if order.status != "completed" or order.refund_status is not None:
        return "订单当前状态不能自助退款，可点“遇到问题”告诉我们"
    if order.paid_time is None or stamp > order.paid_time + SELF_REFUND_WINDOW:
        return "已超过 24 小时"
    # 退款时加锁读发票（锁顺序：用户 → 订单 → 发票，和管理台开票一致）：否则查完发票、
    # 作废之前，管理台可能刚把它标成已开，已开发票的订单就被退了款（Grok 红队，10/16）
    invoice = order_invoice(session, order, lock=lock)
    if invoice is not None and invoice.status == "issued":
        # 已开的发票要先冲红，不能自助退
        return "已开过发票，退款请点“遇到问题”告诉我们"
    # 先锁这段使用期的付费任务、再锁使用期（paid_task_states 的锁顺序）
    states = paid_task_states(
        session,
        session.scalar(select(WorkbenchPass.id).where(WorkbenchPass.order_id == order.id)),
        lock=lock,
    )
    ticket = order_pass(session, order, lock=lock)
    if ticket is None or ticket.revoked_at is not None:
        return "使用期还没开通或已取消，可点“遇到问题”告诉我们"
    if paid_usage(session, ticket, states):
        if ticket.last_used_at is None and not states - {*NO_RESULT_STATES, "queued", "running"}:
            return PAID_TASK_RUNNING
        return "这笔的使用期已用过付费的量（超出了每月免费次数，或一次超过 50 家）"
    refunds = monthly_self_refunds(session, order.user_id, stamp, exclude=order.id)
    if refunds >= SELF_REFUNDS_PER_MONTH:
        # 这笔的 24 小时到下月 1 日还没过，才说"下月 1 日起可以再申请"；
        # 多数订单 24 小时在月内就过了，直接说这笔不能再无理由退款，不让用户白等（审阅意见，10/20）
        if order.paid_time + SELF_REFUND_WINDOW >= next_month_start(stamp):
            return MONTHLY_REFUND_REASON
        return MONTHLY_REFUND_FINAL
    return None


# 无理由退款每个账号每月 1 次（D-083，创始人 10/20："加一个限制，每个号一个月只能退款一次"）：
# 按北京时间自然月算（每月 1 日 0 点起，和每月免费次数同一口径）。只限无理由退款；
# 付了款没开通的自助退款、系统故障或停止服务的人工退款、苹果退款不受限，也不占次数。
# 注销后订单只留由微信标识算出的编号，只用于对账和争议、不用来认人；重新同意协议后按新账号算
# （D-083 ④）
SELF_REFUNDS_PER_MONTH = 1
# 只有还在排队、处理中的付费任务（还没出结果）时自助退款的原因：处理完没出结果的不算用过
PAID_TASK_RUNNING = (
    "这笔的使用期有一份超出免费范围的名单还在处理；处理完没出结果的不算用过，付款 24 小时内"
    "可以再申请，超过 24 小时请点“遇到问题”告诉我们"
)
MONTHLY_REFUND_REASON = "每个账号每月只能申请 1 次无理由退款，本月已经申请过；下月 1 日起可以再申请"
MONTHLY_REFUND_FINAL = (
    "每个账号每月只能申请 1 次无理由退款，本月已经申请过，这笔不能再无理由退款；"
    "有问题可点“遇到问题”告诉我们"
)


def month_start(stamp: datetime) -> datetime:
    """stamp 所在北京时间自然月的 1 日 0 点（UTC）。"""
    first = advisor_tasks.usage_day(stamp).replace(day=1)
    local = datetime.combine(first, datetime.min.time(), tzinfo=advisor_tasks.BEIJING)
    return local.astimezone(UTC)


def next_month_start(stamp: datetime) -> datetime:
    """stamp 所在北京时间自然月的下个月 1 日 0 点（UTC）。"""
    return month_start(month_start(stamp) + timedelta(days=32))


def monthly_self_refunds(
    session: Session, user_id: int | None, stamp: datetime, *, exclude: int | None = None
) -> int:
    """这个用户本月（北京时间自然月）发起过几次无理由退款：自助退款（refund_source=self）、
    这笔开通过使用期（付了款没开通的自助退款是我们的原因，不算），不论退成没退成；
    exclude 是正在看的这一笔（同一笔重复点不算第二次）。调用方退款时已锁用户行，
    连接是 READ COMMITTED，读得到先申请的那一笔。"""
    if user_id is None:
        return 0
    stmt = select(func.count(PayOrder.id)).where(
        PayOrder.user_id == user_id,
        PayOrder.refund_source == SELF_REFUND,
        PayOrder.refund_started_at >= month_start(stamp),
        exists().where(WorkbenchPass.order_id == PayOrder.id),
    )
    if exclude is not None:
        stmt = stmt.where(PayOrder.id != exclude)
    return session.scalar(stmt) or 0


def undelivered_refund_reason(session: Session, order: PayOrder, *, lock=False) -> str | None:
    """付了款却没开通的订单（微信确认收到全款，开通前的核验没过、转了人工）：用户什么都没用上，
    不用等人工，随时可以自助申请全额原路退回，不受 24 小时限制（创始人 10/19："扣款成功但订单
    没同步过来，可以对这笔订单申请自助退款"）。只认能核对清楚的：安卓等普通支付、付款金额等于
    订单金额、这笔从没开通过使用期；其余的（金额对不上、开通过又出问题的、投诉中的）仍由人工处理。
    调用方已按 用户 → 订单 加锁（lock=True 时）。发起退款前 start_refund 还会再向微信查一次单。"""
    if order.paid_fee != order.price or not order.wx_order_id:
        return "这笔付款需要人工核对，可点“遇到问题”告诉我们"
    if order_pass(session, order, lock=lock) is not None:
        return "订单当前状态不能自助退款，可点“遇到问题”告诉我们"
    # 用户向微信支付投诉过这笔：投诉要负责人按流程回复处理，不能绕过去自助退（Codex 复核，10/19）。
    # 投诉推送把订单的 error 写成"收到用户投诉……"（人工单之后不会被别的流程改掉）；
    # 事件表只留 180 天，两处都看
    complained = (
        COMPLAINT_ERROR in (order.error or "")
        or session.scalar(
            select(PayEvent.id)
            .where(PayEvent.order_id == order.id, PayEvent.kind == "xpay_complaint_notify")
            .limit(1)
        )
        is not None
    )
    if complained:
        return "这笔订单的投诉正在处理，可点“遇到问题”告诉我们"
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


def staff_refund_problem(session: Session, order: PayOrder) -> str | None:
    """负责人按规则发起退款（系统故障、停止服务，youju pay-refund）之前的检查；None 表示可以退。
    已开过发票的照常退：退款不等冲红（D-074），退完再按退款金额开红字发票（invoice_issued 提醒）。
    调用方已按 用户 → 订单 的顺序加锁。"""
    if order.platform == "ios":
        return "苹果手机付款的订单只能由用户在 App Store 申请退款，是否退款由苹果公司决定"
    if order.platform != "android":
        return "支付平台没有确认，不能主动退款，请核对后人工处理"
    if order.status != "completed" or order.refund_status is not None:
        return f"订单当前状态（{STATUS_LABELS[order.status]}）不能退款"
    if order.user_id is None:
        return "用户已注销，退款需要人工核对"
    return None


def invoice_issued(session: Session, order: PayOrder) -> bool:
    """这笔订单的发票是不是已经开了：退款后要按退款金额开红字发票（管理台开票页"要冲红"里列出）。"""
    invoice = order_invoice(session, order, lock=True)
    return invoice is not None and invoice.status == "issued"


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
    """退款时还没开的发票作废；已开的留着，退款后由创始人按退款金额开红字发票（管理台"要冲红"）。
    已开过发票的不能自助退款（见 refund_reason），由负责人核实后先退款、再冲红（D-074）。"""
    invoice = order_invoice(session, order, lock=True)
    if invoice is not None and invoice.status == "pending":
        invoice.status, invoice.note, invoice.updated_at = "rejected", INVOICE_VOID_NOTE, stamp


def invoice_out(invoice: InvoiceRequest | None) -> dict | None:
    if invoice is None:
        return None
    return {
        # 订单退款后已按退款金额开了红字发票的，写"已冲红"（D-074）
        "status": "已冲红" if invoice.reversed_at is not None else INVOICE_LABELS[invoice.status],
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


def _blank(value) -> bool:
    return value is None or value == "" or value == 0


def _same(got, want) -> bool:
    # 推送里的数字可能写成文字（"1"）：数字按数值比，文字原样比
    if isinstance(want, int) and not isinstance(want, bool):
        try:
            return int(got) == want
        except (TypeError, ValueError, OverflowError):
            return False
    return got == want


def push_mismatch(order: PayOrder, data: dict, push: dict) -> list[str]:
    """发货推送里和订单、查单对不上的字段：只给字段名和哪边是空的，不给单号和金额。"""
    goods = push.get("GoodsInfo")
    goods = goods if isinstance(goods, dict) else {}
    wx = push.get("WeChatPayInfo")
    wx = wx if isinstance(wx, dict) else {}
    out = [] if goods else ["GoodsInfo（推送里没有）"]
    pairs = (
        [
            ("GoodsInfo.ProductId", goods.get("ProductId"), order.product_id),
            ("GoodsInfo.Quantity", goods.get("Quantity"), 1),
            ("GoodsInfo.OrigPrice", goods.get("OrigPrice"), order.price),
            ("GoodsInfo.ActualPrice", goods.get("ActualPrice"), order.price),
            ("GoodsInfo.Attach", goods.get("Attach"), order.out_trade_no),
        ]
        if goods
        else []
    )
    # 官方文档：非微信支付渠道可能没有 WeChatPayInfo
    if wx:
        pairs += [
            ("WeChatPayInfo.MchOrderNo", wx.get("MchOrderNo"), data.get("channel_order_id")),
            ("WeChatPayInfo.TransactionId", wx.get("TransactionId"), data.get("wxpay_order_id")),
            ("WeChatPayInfo.PaidTime", wx.get("PaidTime"), data.get("paid_time")),
        ]
    for name, got, want in pairs:
        if not _same(got, want):
            side = "推送里是空的" if _blank(got) else "查单里是空的" if _blank(want) else "值不同"
            out.append(f"{name}（{side}）")
    return out


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
        if not accepted:
            # 微信已确认付款但核验不过：不能留在待支付里一直查，也不能发货；转人工并告警。
            # 已经是人工的单：付款信息不改，原因照样追加、再告警（escalate，Codex 第 6 轮）
            if order.status != "manual":
                order.wx_order_id = data.get("wx_order_id")
                if type(data.get("paid_fee")) is int:
                    order.paid_fee = data["paid_fee"]
                order.paid_time = paid_time
                order.platform = platform(data, settings)
            escalate(order, f"微信已确认付款，但{problem}，未发货，需人工核对", stamp)
        return False
    if not accepted and awaiting_alert(order):
        return False
    if push is not None:
        # 推送只是"去查单"的信号，开不开通以查单为准（定时补单本来就只看查单）。
        # 原来推送字段和查单对不上就拒收：10/19 沙箱实测被拒、微信连推 6 次，
        # 2 分钟后补单照样开通了，拒收只让用户多等。现在对不上只记字段名，不记单号和金额。
        mismatch = push_mismatch(order, data, push)
        if mismatch:
            logger.warning(
                "xpay_goods_deliver_notify 订单 #%s 推送和查单对不上（以查单为准）：%s",
                order.id,
                "、".join(mismatch),
            )
    if accepted:
        return True
    order.wx_order_id = data.get("wx_order_id")
    order.paid_fee = data["paid_fee"]
    order.paid_time = paid_time
    order.platform = platform(data, settings)
    order.status, order.updated_at, order.error = "completed", stamp, None
    if not order.user_id:
        escalate(order, "用户已注销，需处理已付款订单", stamp)
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


def mark_refunded(
    session: Session, order: PayOrder, data: dict, stamp: datetime, *, confirmed: bool = False
) -> bool:
    """查单显示退款已经完成就记成已退款（收回使用期、作废没开的发票），返回 True。
    官方 query_order：5"订单退款：商户侧扣款完成"、8"订单退款：用户侧收款完成"、7"订单退款失败"；
    refund_order："启动后需要调用query_order接口来查询退款单状态，等状态变成退款完成后即为最终成功"。
    所以查单时 8 才算退完，5 只是钱从我们这边扣走了、还没到用户那边，继续查（Codex 复核，10/19；
    原来 5 也算完成，之后要是退款失败就没人知道了）。confirmed：退款推送（"退款完成后"发）说
    退款成功（RetCode 0），这时 5 也算完成。"""
    status = data.get("status")
    if not (status == 8 or (status == 5 and confirmed)):
        return False
    if awaiting_alert(order):
        return False
    left = data.get("left_fee")
    # 按剩余天数退的（refund_fee 小于订单金额），退完还剩"订单金额 − 退款金额"；
    # 全额退的（自助、苹果、平台）剩 0。都不是就转人工核对
    planned = order.price - order.refund_fee if order.refund_fee is not None else 0
    if type(left) is not int or left not in (0, planned):
        order.refund_status = "partial"
        escalate(order, "订单部分退款，退款金额和预期不符，需人工核对", stamp)
        return True
    order.status, order.refund_status, order.error = "refunded", "completed", None
    order.refund_fee = order.price - left
    order.updated_at = stamp
    revoke_pass(session, order, stamp)
    void_invoice(session, order, stamp)
    return True


# 发起退款满 24 小时、查单还停在 5（钱从我们这边扣走了、还没到用户那边）：转人工时写的说明
STUCK_REFUND_ERROR = "退款超过 24 小时还没到用户那边（查单状态 5：商户侧扣款完成），需人工核对"


# 发起退款后最多等多久：满 24 小时还没退完（没到查单 8、也没收到退款成功推送）就转人工
REFUND_WAIT = timedelta(hours=24)


def refund_exhausted(order: PayOrder, stamp: datetime) -> bool:
    return order.refund_attempts >= 6 or (
        order.refund_started_at is not None and order.refund_started_at <= stamp - REFUND_WAIT
    )


def start_refund(session: Session, order: PayOrder, client: XPayClient, stamp: datetime) -> None:
    if order.platform != "android":
        escalate(order, "无法确认可主动退款的平台", stamp)
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
        if data.get("status") == 5:
            # 上一次发起的退款其实已经受理（商户侧扣款完成，比如请求超时但微信那边办了）：
            # 不再发起，等它到用户那边（查单变 8 或收到退款推送）
            order.refund_status, order.error, order.updated_at = "pending", None, stamp
            session.commit()
            return
        if (
            data.get("status") not in (2, 3, 4, 7)
            or data.get("left_fee") != order.price
            or data.get("order_fee") != order.price
            or data.get("paid_fee") != order.price
        ):
            escalate(order, "退款余额或订单状态不符，需核对", stamp)
            session.commit()
            return
        if platform(data, client.settings) != "android":
            escalate(order, "退款时无法确认普通支付平台", stamp)
            session.commit()
            return
        user = session.get(User, order.user_id) if order.user_id else None
        if user is None:
            escalate(order, "用户已注销，退款需人工处理", stamp)
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


# 赠送流水的保存期（隐私指引付费部分）
REWARD_RETENTION = timedelta(days=3650)
# 苹果问询查不到这笔订单、本地核验出错或超时：也答建议退款，注明暂时无法核验
# （付费服务协议第二条第 7 项）
IOS_UNVERIFIABLE = {"result_code": 0, "result_info": "无法核验", "evidence": "暂时无法核验"}


def shutdown_notice(settings) -> bool:
    """停止服务公示期间（YOUJU_PAY_SHUTDOWN_NOTICE）：不再卖新的使用期，苹果问询一律建议退款。"""
    return bool(getattr(settings, "pay_shutdown_notice", False))


def ios_decision(session: Session, order: PayOrder | None, stamp: datetime, settings=None) -> dict:
    """苹果退款问询的答复（D-081，和安卓的无理由退款同一口径；付费服务协议第二条第 7 项）：
    - 已核实应当退款的建议退款：负责人用 `youju pay-refund-due` 标记过的订单（因我们的原因不能使用、
      未成年人未经监护人同意购买等），和停止服务公示期间还有剩余使用期的订单，用没用过都一样；
    - 其余的，用过付费的量（出了结果）拒绝；付款超过 24 小时拒绝；付费的名单还在排队、处理中、
      没出结果的，建议退款并注明暂时无法核验（没出结果的不算用过，Codex 10/28）；都没有的建议退款；
    - 查不到订单的建议退款，注明暂时无法核验（IOS_UNVERIFIABLE，_ios_reply 出错、超时也是）。
    凭据只写"付款后 24 小时内 / 付款后已超过 24 小时"和"用过付费的量 / 没有用过付费的量"，
    已核实应当退款的只写"已核实应当退款"；不带任务编号、具体时间和应当退款的原因：答复经微信
    给到境外的 Apple，只给必要的（开卖那一版必做第 8 项、隐私指引付费部分）。"""
    if order is None:
        return dict(IOS_UNVERIFIABLE)
    ticket = order_pass(session, order)
    states = paid_task_states(session, ticket.id if ticket else None)
    # 自助退款把排队、处理中的也先算用过（等它出结果）；苹果问询只问一次，没出结果的不能答已使用
    used = paid_usage(session, ticket, states - {"queued", "running"})
    pending = not used and bool(states & {"queued", "running"})
    paid = order.paid_time
    late = paid is not None and stamp > paid + SELF_REFUND_WINDOW
    timing = "付款后已超过 24 小时" if late else "付款后 24 小时内" if paid is not None else ""
    usage = (
        "用过付费的量"
        if used
        else "暂时无法核验是否用过付费的量"
        if pending
        else "没有用过付费的量"
    )
    evidence = f"{timing}；{usage}" if timing else usage
    # 停止服务只退剩余天数（付费服务协议第二条第 5 项）：公示期间只给这一笔还有没用完、没收回的
    # 使用期的订单答应当退款；已经用完、退过的照常答（独立审阅 10/28）
    remaining = ticket is not None and ticket.revoked_at is None and ticket.ends_at > stamp
    if order.refund_due_at is not None or (
        shutdown_notice(settings or get_settings()) and remaining
    ):
        # 应当退的和用没用过、过没过 24 小时无关：凭据只写这一句，不再带前面两句（agy 10/28）
        return {"result_code": 0, "result_info": "应当退款", "evidence": "已核实应当退款"}
    if used:
        return {"result_code": 1, "result_info": "已使用", "evidence": evidence}
    if late:
        return {"result_code": 1, "result_info": "付款已超过 24 小时", "evidence": evidence}
    if pending:
        return {"result_code": 0, "result_info": "无法核验", "evidence": evidence}
    return {"result_code": 0, "result_info": "未使用", "evidence": evidence}


def mark_refund_due(
    session: Session, order_id: int, note: str | None, stamp: datetime, *, dry_run: bool = False
) -> tuple[str, PayOrder | None]:
    """负责人核实一笔苹果订单应当退款（因我们的原因不能使用、未成年人未经监护人同意购买等），
    或清除这个标记（note 为 None）：苹果来问时照它答"建议退款"（ios_decision）。
    只给苹果手机付款、付过款的订单标：安卓的直接用 youju pay-refund 退。返回（结果, 订单）：
    marked / cleared / unchanged / not_ios / missing。改动记一笔支付事件，事件里不写原因。"""
    order = lock_order(session, order_id)
    if order is None:
        return "missing", None
    if order.platform != "ios" or order.paid_time is None:
        session.rollback()
        return "not_ios", order
    if (note is None) == (order.refund_due_at is None):
        session.rollback()
        return "unchanged", order
    if dry_run:
        session.rollback()
        return ("cleared" if note is None else "marked"), order
    order.refund_due_at = None if note is None else stamp
    order.refund_due_note, order.updated_at = note, stamp
    event(
        session,
        "refund_due_cleared" if note is None else "refund_due_marked",
        {"pay_order_id": order.id},
        order,
    )
    session.commit()
    return ("cleared" if note is None else "marked"), order


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
    session: Session, day: date, bill: dict[str, int], stamp: datetime, *, alert=None
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
            escalate(order, "账单已支付但本地未发货，需人工核对", stamp)
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
    newly_different = bool(differences) and row.status != "different"
    row.differences, row.status, row.updated_at = (
        differences,
        "different" if differences else "matched",
        stamp,
    )
    session.commit()
    if newly_different:
        # 对不上的要人工核对（含账单已付款、本地没开通的，已转人工）：同一天只在第一次对不上时告警
        _alert(
            alert,
            f"支付对账 {day}：本地 {row.local_count} 笔、账单 {row.bill_count} 笔，"
            f"有 {len(differences)} 笔对不上，需人工核对（管理台订单页）",
        )
    return row


def _alert(alert, text: str) -> None:
    from youju.alerts import send_alert

    (alert or send_alert)(text)


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
        _alert(
            alert,
            f"支付对账 {row.day} 的账单下载或解析失败 {row.attempts} 次，已停止重试，需人工核对",
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
        # 开卖以后每天都在卖，对不了账要有人知道：同一天只告警一次
        newly_failed = row.status != "failed"
        row.status, row.differences = "failed", {"error": "未配置账单字段"}
        session.commit()
        if newly_failed and local:
            _alert(
                alert, f"支付对账 {day}：没配账单字段（YOUJU_XPAY_BILL_*），对不了账，需人工核对"
            )
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
        reconcile(session, day, parse_bill(bytes(raw), settings), stamp, alert=alert)
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


# 用户主动核对（付款后、打开订单页）：只核对 48 小时内下的待确认订单（更早的交给每分钟的补单，
# 超过 48 小时还确认不了会关单），并且只核对这个用户最近的 3 张待确认订单
# （下单限制同样是 10 分钟内 3 张）
CHECK_WINDOW = timedelta(hours=48)
CHECK_RECENT = 3


def checkable(session: Session, order: PayOrder, stamp: datetime) -> bool:
    """这张订单能不能由用户主动核对：待确认、48 小时内下的、这个用户最近的 3 张待确认订单之一。"""
    if order.status != "pending" or order.user_id is None:
        return False
    if order.created_at <= stamp - CHECK_WINDOW:
        return False
    recent = session.scalars(
        select(PayOrder.id)
        .where(
            PayOrder.user_id == order.user_id,
            PayOrder.status == "pending",
            PayOrder.created_at > stamp - CHECK_WINDOW,
        )
        .order_by(PayOrder.created_at.desc(), PayOrder.id.desc())
        .limit(CHECK_RECENT)
    ).all()
    return order.id in recent


# 查单状态里待确认订单"能自己处理"的：0/1 没付、2/3/4 付了、8 退完了、6 关了。
# 其余（5 退款还在进行、7 退款失败、9 回收广告金完成、10 分账回退完成，以及以后新加的）说明钱
# 在微信那边走过别的流程，既不能当没付款关单、也不能照常开通，转人工核对并告警；原来补单会
# 一直把它当待支付查下去（Codex 复核，10/19）
HANDLED_STATUSES = frozenset({0, 1, 2, 3, 4, 6, 8})
ODD_STATUS_NAMES = {
    5: "退款中：商户侧扣款完成",
    7: "退款失败",
    9: "回收广告金完成",
    10: "分账回退完成",
}


# 已完成的订单收到退款推送、查单却没确认退款（多半是苹果那边或小程序后台退的款）：推送只重推有限次，
# 每分钟的补单也不查已完成的订单，不转人工就没人知道（Codex 10/29）。使用期先不动，核实退了再收回
REFUND_PUSH_UNCONFIRMED_ERROR = (
    "收到退款推送，查单没确认退款，需人工核对（小程序后台 → 虚拟支付 → 交易订单）；"
    "核实退了再收回使用期"
)


def escalate(order: PayOrder, reason: str, stamp: datetime) -> bool:
    """转人工：订单改成"需人工处理"，写上原因，清空 alerted_at，
    每分钟的补单发告警（发不出去下一轮接着发）。
    已经是人工的订单再来一个新原因：原因只加不盖（新的放前面，超长截到 500 字时截掉的是旧的），
    再告警一次——不然人工只按原来的原因处理，不知道又出了什么事（Codex 第 4、5 轮：投诉盖掉退款核对、
    部分退款不再告警）。同一个原因已经在里面的不重复加、不重复告警。返回这次有没有改。"""
    current = (order.error or "") if order.status == "manual" else ""
    if current and reason in current:
        return False
    order.error = (f"{reason}；原来的原因：{current}" if current else reason)[:500]
    order.status, order.alerted_at, order.updated_at = "manual", None, stamp
    return True


def awaiting_alert(order: PayOrder) -> bool:
    """转了人工、告警还没发出去的单：推送和查单先不自动结案（开通、记已退款），等补单把告警发出去
    （一般一分钟内）再说——不然待发的告警跟着状态一起没了，投诉之类没人处理（Codex 第 7 轮）。
    微信会重推；负责人用 youju pay-recheck 核对时不受这条限制（recheck 先记 alerted_at）。"""
    return order.status == "manual" and order.alerted_at is None


def flag_unconfirmed_refund(order: PayOrder, stamp: datetime) -> bool:
    """已完成（或已转人工）的订单收到退款推送、这次没核实退款：转人工、告警，返回 True。
    使用期先不动；推送重推时查单显示退完了（8），mark_refunded 照常把它记成已退款、收回使用期。"""
    if order.status not in ("completed", "manual"):
        return False
    return escalate(order, REFUND_PUSH_UNCONFIRMED_ERROR, stamp)


def flag_odd_pending(order: PayOrder, data: dict, stamp: datetime) -> bool:
    """待确认的订单查到上面说的少见状态：转人工（每分钟的补单会发告警），返回 True。
    状态不是整数（返回格式不对）的不算，留给下一次查单。"""
    status = data.get("status")
    if type(status) is not int or status in HANDLED_STATUSES:
        return False
    name = ODD_STATUS_NAMES.get(status, "未知状态")
    escalate(order, f"微信查单状态 {status}（{name}），待确认的订单不能自动处理，需人工核对", stamp)
    return True


def check_order(
    session: Session, order: PayOrder, client: XPayClient, settings: Settings, stamp: datetime
) -> str:
    """用户付款后、打开订单页时主动核对一张待确认的订单：向微信查单，付了款就当场开通、通知发货，
    不等推送和每分钟的补单（10/19 沙箱实测：推送没处理成，补单 2 分钟后才开通，小程序只等了
    1 分钟，用户一直看到"待支付"）。核验规则和补单完全一样（accept_payment）。
    调用方已按 用户 → 订单 加锁，订单是待确认（checkable）。返回这次核对的结果：
    paid 已开通；unpaid 微信还没有这笔付款；closed 微信已关单；refunded 微信那边已经退款；
    manual 微信确认收到付款，但核验不过、已转人工；unavailable 这次没查成（交给每分钟的补单）。"""
    try:
        data = query(session, order, client)
    except XPayError:
        session.commit()  # 查单记录（若有）照常保存；订单不动，交给补单
        return "unavailable"
    result = settle_pending(session, order, data, settings, stamp)
    # 先持久化使用期再通知发货；通知失败由每分钟的补单补发（和补单同一顺序）
    session.commit()
    if result == "paid" and not order.goods_notified:
        provide_goods(session, order, client)
    return result


def settle_pending(
    session: Session, order: PayOrder, data: dict, settings: Settings, stamp: datetime
) -> str:
    """按查单结果处理一张待确认的订单（不提交、不通知发货）：主动核对和下新单前查上一笔共用。
    返回 paid 已开通；unpaid 微信还没有这笔付款（0/1）；closed 微信已关单（6）；
    refunded 已退完（8）；manual 转了人工（金额等核验不过、5/7/9/10 等少见状态）；
    unavailable 这次处理不了（返回格式不对）。"""
    if mark_refunded(session, order, data, stamp):
        return "refunded" if order.status == "refunded" else "manual"
    status = data.get("status")
    if status == 6:
        order.status, order.updated_at = "closed", stamp
        return "closed"
    if status in (0, 1):
        return "unpaid"
    if flag_odd_pending(order, data, stamp):
        return "manual"
    accept_payment(session, order, data, settings, stamp)
    if order.status == "completed":
        return "paid"
    return "manual" if order.status == "manual" else "unavailable"


def note_complaint(order: PayOrder, stamp: datetime) -> bool:
    """投诉推送：投诉一律要人工跟进（微信支付投诉有答复时限）。已完成、待确认、已转人工的单照旧转人工
    （escalate，补单告警）；退款中、已退款、已关闭的单不改状态——转人工会让退款中的单停掉自动退款、
    已退款的单倒退成"人工核对中"（Codex 第 6 轮）——由补单按投诉事件单独告警一次
    （complaint_alerts）。
    返回这次有没有转人工。"""
    if order.status in COMPLAINT_KEEPS_STATUS:
        return False
    return escalate(order, f"{COMPLAINT_ERROR}，需人工处理", stamp)


def complaint_alerts(session: Session, stamp: datetime, alert) -> int:
    """没转人工的投诉（投诉时订单退款中、已退款、已关闭）单独告警一次：
    在投诉事件的摘要里记 alerted，
    告警没发出去下一轮接着发。转了人工的投诉由转人工的告警负责，这里只记上 alerted、不重复发。
    只看最近 7 天的投诉事件。返回这次发出的告警条数。"""
    sent = 0
    for row in session.scalars(
        select(PayEvent)
        .where(
            PayEvent.kind == COMPLAINT_EVENT,
            PayEvent.order_id.is_not(None),
            PayEvent.created_at > stamp - COMPLAINT_ALERT_WINDOW,
        )
        .order_by(PayEvent.id)
    ):
        if (row.summary or {}).get("alerted"):
            continue
        order = session.get(PayOrder, row.order_id)
        if order is not None and order.status not in COMPLAINT_KEEPS_STATUS:
            done = True
        else:
            label = STATUS_LABELS.get(order.status, order.status) if order else "已删除"
            done = (
                alert(
                    f"支付订单 #{row.order_id} {COMPLAINT_ERROR}（订单{label}，没有转人工），"
                    "需人工处理：到小程序后台虚拟支付的投诉里答复"
                )
                is not False
            )
            sent += done
        if done:
            row.summary = {**(row.summary or {}), "alerted": True}
    session.commit()
    return sent


def abandonable(order: PayOrder, stamp: datetime) -> bool:
    """小程序能不能报告这一单"付款没发起"：待确认、没注销、10 分钟内下的。"""
    return (
        order.status == "pending"
        and order.user_id is not None
        and order.created_at > stamp - ABANDON_WINDOW
    )


def abandon(
    session: Session,
    order: PayOrder,
    code: int,
    client: XPayClient,
    settings: Settings,
    stamp: datetime,
) -> str:
    """小程序报告这一单付款前就被微信拒了（NOT_STARTED_CODES 里的错误码）：向微信查一次——
    付了款的照常开通（万一），退完的记已退款，核验不过的转人工；没付款（0/1）、已关单（6）、
    查不到（微信那边可能根本没有这一单）或返回格式不对的，当场关单、不告警。关了以后要是其实付了款，
    发货推送照样会把它开通（accept_payment 不看关没关单）。这样下新单前就不会被它挡住
    （create_order 只挡查不清的旧单）。调用方已锁 用户 → 订单，
    订单可报告（abandonable）。返回 closed / paid / refunded / manual。"""
    try:
        data = query(session, order, client)
    except XPayError:
        data = None
    if data is not None:
        result = settle_pending(session, order, data, settings, stamp)
        if result in ("paid", "refunded", "manual", "closed"):
            return result
    order.status, order.updated_at = "closed", stamp
    order.error = f"付款前就被微信拒了（错误码 {code}），已关闭"
    return "closed"


def recheck(
    session: Session, order: PayOrder, client: XPayClient, settings: Settings, stamp: datetime
) -> str:
    """负责人核对一张订单（youju pay-recheck）：48 小时查不到付款结果已关单的、转了人工的、
    还在待确认的，向微信查一次，按和补单同一套规则处理——付了款且核验通过就开通（再通知发货），
    退完了记已退款，微信已关单记关单，没付款不动（Codex 第 6 轮：关单以后要有核实、处理的路径）。
    调用方已锁 用户 → 订单。返回 paid / refunded / closed / unpaid / manual / skipped
    （其他状态的订单不动）；查单出错抛 XPayError。"""
    if order.status not in ("closed", "manual", "pending"):
        return "skipped"
    data = query(session, order, client)
    # 负责人在核对：推送那边"告警发出去之前不结案"的限制不挡这里（先记 alerted_at）；
    # 最后还是人工单（没付款、核验不过、少见状态）的，告警照旧等补单发——人工原因可能是投诉，
    # 不能因为核对过一次就把待发的告警吞了（Codex 第 8 轮）
    unalerted = awaiting_alert(order)
    if unalerted:
        order.alerted_at = stamp
    result = _recheck(session, order, data, settings, stamp)
    if unalerted and order.status == "manual":
        order.alerted_at = None
    return result


def _recheck(
    session: Session, order: PayOrder, data: dict, settings: Settings, stamp: datetime
) -> str:
    if mark_refunded(session, order, data, stamp):
        return "refunded" if order.status == "refunded" else "manual"
    status = data.get("status")
    if status == 6:
        if order.status != "closed":
            order.status, order.updated_at = "closed", stamp
        return "closed"
    if status in (0, 1):
        return "unpaid"
    if status in (2, 3, 4):
        accept_payment(session, order, data, settings, stamp)
        return "paid" if order.status == "completed" else "manual"
    # 退款中、退款失败等少见状态（或返回格式不对）：不能当成已关闭了事，转人工（Codex 第 7 轮）
    name = ODD_STATUS_NAMES.get(status, "未知状态") if type(status) is int else "返回格式不对"
    escalate(order, f"核对时微信查单状态 {status}（{name}），需人工核对", stamp)
    return "manual"


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
        with wx_http() as http:
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
                | UNCONFIRMED_CLOSE_UNALERTED
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
                    escalate(order, "用户已注销，退款需人工处理", stamp)
                elif refund_exhausted(order, stamp):
                    # 最后核验已成功的退款；查单也失败则人工接手，不能无限重试。
                    # 24 小时从发起退款（refund_started_at）算：停在 5（钱从我们这边扣走了、
                    # 还没到用户那边）满 24 小时也转人工，告警只发一次（alerted_at）。
                    # 发起次数先到上限、还没满 24 小时就查到 5 的（几次请求超时、其实微信受理了）：
                    # 不再发起，按 10 分钟一次接着等，满 24 小时再定（Codex 复核）
                    waiting = order.refund_started_at > stamp - REFUND_WAIT
                    if not (waiting and order.refund_retry_at and order.refund_retry_at > stamp):
                        confirmed, status = False, None
                        with contextlib.suppress(XPayError):
                            data = query(session, order, client)
                            status = data.get("status")
                            confirmed = mark_refunded(session, order, data, stamp)
                        if confirmed:
                            pass
                        elif status == 5 and waiting:
                            order.refund_status, order.error = "pending", None
                            order.refund_retry_at = stamp + timedelta(minutes=10)
                        else:
                            escalate(
                                order,
                                STUCK_REFUND_ERROR
                                if status == 5
                                else "退款尝试达到上限或超过24小时，需人工核对",
                                stamp,
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
                    # 查单一直失败也不能永远轮询：关单，下面告警（见 UNCONFIRMED_PAYMENT_ERROR）。
                    # 不转人工：没有能把这种单查清楚再处理的工具，订单页"1 个工作日内处理"兑现不了
                    # （独立审阅 10/29）；关单后订单页写"如果其实扣了款，点'遇到问题'告诉我们"
                    data = None
                    order.status, order.error = "closed", UNCONFIRMED_PAYMENT_ERROR
                    order.alerted_at = None
                if data is not None and not mark_refunded(session, order, data, stamp):
                    if data.get("status") == 6 or (overdue and data.get("status") in (0, 1)):
                        order.status = "closed"
                    elif not flag_odd_pending(order, data, stamp):
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
            if (
                order.status == "closed"
                and order.error == UNCONFIRMED_PAYMENT_ERROR
                and not order.alerted_at
                and alert(
                    f"支付订单 #{order.id} {UNCONFIRMED_PAYMENT_ERROR}；核实付了款的，"
                    f"在 admin 里运行 youju pay-recheck --order-id {order.id}"
                )
                is not False
            ):
                order.alerted_at = stamp
            order.updated_at = stamp
            session.commit()
        except XPayError:
            order.error = "微信查单或通知暂不可用，将重试"
            order.updated_at = stamp
            session.commit()
    complaint_alerts(session, stamp, alert)
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
    # 赠送天数的流水（续买多送、体验折算、奖励）自发放之日起保存 3650 天（隐私指引付费部分，
    # 开卖那一版必做第 9 项）：到期连同它那一段使用期一起删（使用期引用流水，先删使用期；
    # 任务上的引用会置空）
    # 被顺延到很久以后、还没用完也没收回的那段（Codex 10/28）不删，等它用完再删
    unused = (
        select(WorkbenchPass.id)
        .where(
            WorkbenchPass.reward_id == PassReward.id,
            WorkbenchPass.revoked_at.is_(None),
            WorkbenchPass.ends_at > stamp,
        )
        .exists()
    )
    old = list(
        session.scalars(
            select(PassReward.id).where(PassReward.granted_at <= stamp - REWARD_RETENTION, ~unused)
        )
    )
    rewards = 0
    if old:
        session.execute(delete(WorkbenchPass).where(WorkbenchPass.reward_id.in_(old)))
        rewards = session.execute(delete(PassReward).where(PassReward.id.in_(old))).rowcount
    return {"pay_events": events, "pass_rewards": rewards or 0}


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
            escalate(order, "用户已注销，需核对未完成订单", datetime.now(UTC))
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
