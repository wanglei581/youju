"""卡里的 AI 次数（D-109 甲：AI 次数加进使用期卡，迁移 0046）。

给次数（买卡天数 × 10、续买多送 30、加量体验 30、上线补给）、占、扣、退、查剩余都在这里；
调用的地方（解读、对照、问一问的提交和处理）在 AI 那边。双方定稿的接口说明在协调会话的共享笔记
《卡里AI次数_接口说明》（10/29）；口径照《AI 功能和付费套餐方案》（PR #405）第三节第 3、4 条。

规矩：
- 函数都在调用方的事务里跑，不提交，不拿命名锁。付费全站名额的锁在 AI 那边的
  ``ai_cap_slot(pool="paid")``，这里只给在那条连接上数数的 paid_pool_used、paid_pool_limit。
- 写之前调用方先锁这个用户的行。锁的先后：用户行 → lock_wallet（这个用户的卡）→ 任务、对话 →
  全站名额的命名锁 → reserve 插占用 → 提交 → 放锁。charge、release 前也先锁用户行。
- 每笔已扣（used）、占着（held）记在那一笔自己的行上：锁住那一行就读到最新的数，读已提交、
  可重复读都一样；每次占用另记一行 ai_credit_uses（凭据，任务删了也还在）。
- 先扣赠送的（续买多送、体验送的、上线补给），再扣买来的；同类里最早到期的先扣；后面还没开始的
  那段也能提前用。每天的免费次数不在这里（各功能各算各的，在 AI 那边）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from youju.advisor_tasks import usage_day
from youju.models import AICreditGrant, AICreditUse, PayOrder, WorkbenchPass

log = logging.getLogger(__name__)

# 卡里的次数 = 买的天数 × 10（30 天卡 300 次、90 天卡 900 次）：整张卡的总数，
# 有效期内随时用，没有每天上限
CREDITS_PER_DAY = 10
# 续买多送的 3 天给 30 次；加量体验送 30 次（3 天内有效）
RENEW_CREDITS = 30
TRIAL_CREDITS = 30
# 付费全站名额 = max(1000, 在用的卡数 × 30)（方案第四节第 2 条）
PAID_POOL_MIN = 1000
PAID_POOL_PER_CARD = 30

PURCHASE = "purchase"
GIFT_KINDS = ("renewal", "trial", "backfill")
USE_KINDS = ("interpret", "clause", "ask", "photo", "compile")
REASONS = ("free_used", "site_full")

CONFIRM_TEXT = {
    "site_full": "今天全站免费名额用完了，这次扣卡里的 1 次吗？",
    "first_purchase": "扣了以后，这张卡就不能无理由退款了",
}
# 页面的说法由 AI 那边按 status 写（要创始人看，D-076）；这里只是兜底
NO_CREDIT_TEXT = {
    "NO_CARD": "没有可用的卡里次数",
    "EXPIRED": "卡已过期",
    "USED_UP": "卡里的次数用完了",
}


class CreditError(Exception):
    """卡里次数的业务错误：code 给调用方判断，message 是兜底说法。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class NoCredits(CreditError):
    """没有能扣的次数：code 是 NO_CARD（从没有过没收回的卡或体验赠送）、EXPIRED（有过，都到期了）、
    USED_UP（还在有效期里，剩 0），和钱包的 status 一致。"""

    def __init__(self, code: str) -> None:
        super().__init__(code, NO_CREDIT_TEXT[code])


class CreditConfirm(CreditError):
    """要用户先确认才能扣：codes 里是要确认的全部（site_full、first_purchase），页面一个弹窗写全，
    确认后带上 confirmed 重发。没有占，调用方回滚。"""

    def __init__(self, codes: list[str]) -> None:
        super().__init__("CREDIT_CONFIRM", "；".join(CONFIRM_TEXT[c] for c in codes))
        self.codes = codes
        self.messages = {c: CONFIRM_TEXT[c] for c in codes}


class PaidPoolFull(CreditError):
    """付费全站名额满了：AI 那边在 ai_cap_slot(pool="paid") 里数满了时抛，不占。被挡住的不扣次数，
    按"因我们的原因不能使用"处理（方案第四节第 2 条）。"""

    def __init__(self) -> None:
        super().__init__("PAID_POOL_FULL", "今天卡里次数的全站名额满了，这次没有扣次数，请稍后再试")


@dataclass
class Slot:
    """钱包里的一笔：还能用（没收回、没到期）。"""

    grant: AICreditGrant
    ticket: WorkbenchPass | None
    expires_at: datetime

    @property
    def left(self) -> int:
        return self.grant.amount - self.grant.used - self.grant.held

    @property
    def source(self) -> str:
        return "bought" if self.grant.kind == PURCHASE else "gift"


@dataclass
class Wallet:
    """一个用户卡里的 AI 次数。slots 按扣的先后排好：先赠送的再买来的，同类里最早到期的先。
    had_any：有没有过没收回的卡或体验赠送（分 NO_CARD 和 EXPIRED）。"""

    user_id: int
    stamp: datetime
    slots: list[Slot] = field(default_factory=list)
    had_any: bool = False

    @property
    def left(self) -> int:
        return sum(max(slot.left, 0) for slot in self.slots)

    @property
    def status(self) -> str:
        if self.slots:
            return "OK" if self.left > 0 else "USED_UP"
        return "EXPIRED" if self.had_any else "NO_CARD"

    def next_slot(self) -> Slot | None:
        return next((slot for slot in self.slots if slot.left > 0), None)

    def view(self, session: Session) -> dict:
        """能力接口和页面用。status：OK 能用；NO_CARD 没卡，给购买入口；
        EXPIRED 卡已过期，给购买入口；
        USED_UP 有效期内剩 0，写"有效期内可以再买一张"。nextConfirm：下一次要是扣到买来的次数、而且
        这一笔现在还能无理由退款，先弹"扣了以后，这张卡就不能无理由退款了"。"""
        usable = [slot for slot in self.slots if slot.left > 0]
        nxt = self.next_slot()
        return {
            "status": self.status,
            "hasCard": self.status in ("OK", "USED_UP"),
            "left": self.left,
            "gift": sum(slot.left for slot in usable if slot.source == "gift"),
            "bought": sum(slot.left for slot in usable if slot.source == "bought"),
            "expiresAt": min((slot.expires_at for slot in usable), default=None),
            "nextConfirm": "first_purchase"
            if nxt is not None and _first_purchase_prompt(session, nxt, self.stamp)
            else None,
        }


def credits_for_days(days: int) -> int:
    return days * CREDITS_PER_DAY


def _wallet(session: Session, user_id: int, stamp: datetime, *, lock: bool) -> Wallet:
    stmt = (
        select(AICreditGrant, WorkbenchPass)
        .outerjoin(WorkbenchPass, WorkbenchPass.id == AICreditGrant.pass_id)
        .where(AICreditGrant.user_id == user_id, AICreditGrant.revoked_at.is_(None))
    )
    if lock:
        # 卡的行和它挂的那一段一起锁：锁定读拿到的是最新提交的（段被退款往前挪、收回都看得到）
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    wallet = Wallet(user_id=user_id, stamp=stamp)
    for grant, ticket in session.execute(stmt).all():
        if ticket is not None and ticket.revoked_at is not None:
            continue  # 那一段已经收回（退款、注销）：不算"有过"，退了的卡是"没卡"
        wallet.had_any = True
        expires = ticket.ends_at if ticket is not None else grant.expires_at
        if expires is not None and expires > stamp:
            wallet.slots.append(Slot(grant=grant, ticket=ticket, expires_at=expires))
    wallet.slots.sort(key=lambda s: (s.source == "bought", s.expires_at, s.grant.id))
    return wallet


def lock_wallet(session: Session, user_id: int, stamp: datetime) -> Wallet:
    """提交口锁完用户行以后、进付费名额的命名锁以前调：锁住这个用户的卡，算好每笔剩几次。
    没有卡也返回空钱包，不抛。"""
    return _wallet(session, user_id, stamp, lock=True)


def balance(session: Session, user_id: int, stamp: datetime) -> dict:
    """只读的剩余（能力接口用），不锁行；返回和 Wallet.view 一样的字段。"""
    return _wallet(session, user_id, stamp, lock=False).view(session)


def _first_purchase_prompt(session: Session, slot: Slot, stamp: datetime) -> bool:
    """要扣的是买来的次数、这一段买来的次数还没扣过也没占着、而且这一笔现在还能无理由退款
    （安卓等：照自助退款的条件；苹果：付款 24 小时内、没用过付费的量），才先提示。"""
    from youju import payments

    grant, ticket = slot.grant, slot.ticket
    if grant.kind != PURCHASE or grant.used or grant.held or ticket is None or not ticket.order_id:
        return False
    order = session.get(PayOrder, ticket.order_id)
    if order is None or order.paid_time is None:
        return False
    if stamp > order.paid_time + payments.SELF_REFUND_WINDOW:
        return False
    if order.platform == "ios":
        return not payments.paid_usage(session, ticket)
    return payments.refund_reason(session, order, stamp) is None


def _out(use: AICreditUse, slot_source: str, left: int) -> dict:
    return {"useKey": use.use_key, "grantId": use.grant_id, "source": slot_source, "left": left}


def reserve(
    session: Session,
    wallet: Wallet,
    use_key: str,
    kind: str,
    stamp: datetime,
    *,
    reason: str,
    confirmed: frozenset[str] | set[str] = frozenset(),
) -> dict:
    """提交时占 1 次（在调用方的 ai_cap_slot(pool="paid") 里调；钱包是 lock_wallet 锁过的）。

    reason：free_used（自己这一项今天的免费次数用完了）或 site_full（自己还有免费次数，
    但全站免费名额满了）。要确认的没带上抛 CreditConfirm（不占）；没有能扣的抛 NoCredits。
    同一个 use_key 已经占着或扣了：原样返回，不重复占；已经退回的抛 CreditError("RELEASED")。
    返回 {useKey, grantId, source, left}。"""
    if kind not in USE_KINDS or reason not in REASONS:
        raise ValueError(f"不认识的 kind 或 reason：{kind} {reason}")
    existing = session.scalar(select(AICreditUse).where(AICreditUse.use_key == use_key))
    if existing is not None:
        if existing.state == "released":
            raise CreditError("RELEASED", "这次请求的次数已经退回，不能再占")
        grant = session.get(AICreditGrant, existing.grant_id)
        source = "bought" if grant is not None and grant.kind == PURCHASE else "gift"
        return _out(existing, source, wallet.left)
    slot = wallet.next_slot()
    if slot is None:
        raise NoCredits(wallet.status)
    needed = ["site_full"] if reason == "site_full" else []
    if _first_purchase_prompt(session, slot, stamp):
        needed.append("first_purchase")
    if needed and not set(needed) <= set(confirmed):
        raise CreditConfirm(needed)
    use = AICreditUse(
        grant_id=slot.grant.id,
        user_id=wallet.user_id,
        use_key=use_key,
        kind=kind,
        state="reserved",
        reason=reason,
        reserved_at=stamp,
        pool_day=usage_day(stamp),
    )
    session.add(use)
    # 这一行 lock_wallet 已经锁着、读的是最新的：直接加 1，钱包里的数跟着变（一个事务里占几次也对）
    slot.grant.held += 1
    session.flush()
    return _out(use, slot.source, wallet.left)


def _settle(session: Session, use_key: str, stamp: datetime, state: str, **values) -> str:
    """把一次占用从 reserved 改成 charged 或 released：锁住这一行再改，重复调用、两个连接同时调
    都只改成功一次。返回改完（或本来）的状态，没有这一行返回 "none"。"""
    use = session.scalar(
        select(AICreditUse)
        .where(AICreditUse.use_key == use_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if use is None:
        log.warning("卡里次数：没有这次占用（%s），%s 时跳过", use_key, state)
        return "none"
    if use.state != "reserved":
        return use.state
    use.state, use.settled_at = state, stamp
    for key, value in values.items():
        setattr(use, key, value)
    counters = {"held": AICreditGrant.held - 1}
    if state == "charged":
        counters["used"] = AICreditGrant.used + 1
    session.execute(
        update(AICreditGrant)
        .where(AICreditGrant.id == use.grant_id)
        .values(**counters)
        .execution_options(synchronize_session="fetch")
    )
    session.flush()
    return state


def charge(session: Session, use_key: str, stamp: datetime) -> str:
    """结果给到用户时扣（解读、对照：done、不是拒答、条数大于 0；问一问：status 是 answer 且过了内容
    安全）。占着期间那一笔到期或被收回，照样扣（发起时有效）。已经扣了的返回 "charged"，已经退回的
    返回 "released"，都不再改。"""
    return _settle(session, use_key, stamp, "charged")


def release(session: Session, use_key: str, stamp: datetime, reason: str) -> str:
    """没出结果时退回（失败、拒答、0 条、没过内容安全、问一问没给出原句、回答被拦、取消或删任务、
    删对话、AI 关了或退回排队、处理时全站付费名额满了、出错兜底、卡住清理、注销）。退回原来那一笔；
    那一笔过期或收回了，只算没用过，不挪到别的卡。已经扣了的不退（结果给过以后又撤下的不退），
    返回 "charged"。"""
    return _settle(session, use_key, stamp, "released", release_reason=reason[:32])


def paid_pool_used(bind, day: date) -> int:
    """当天从卡里发起的占用数（全站合计）：提交时占上的都算，后来扣了、退了都不减（失败、拒答也调了
    模型，方案第三节第 4 条第 11 项）。在 ai_cap_slot(pool="paid") 那条连接上调。"""
    return int(
        bind.scalar(select(func.count(AICreditUse.id)).where(AICreditUse.pool_day == day)) or 0
    )


def paid_pool_limit(bind, stamp: datetime) -> int:
    """付费全站名额 = max(1000, 在用的卡数 × 30)。在用的卡按人数：现在有没收回、没到期的卡或体验
    赠送的用户（后面还没开始的段也算）。"""
    cards = int(
        bind.scalar(
            select(func.count(func.distinct(AICreditGrant.user_id)))
            .select_from(AICreditGrant)
            .outerjoin(WorkbenchPass, WorkbenchPass.id == AICreditGrant.pass_id)
            .where(
                AICreditGrant.revoked_at.is_(None),
                AICreditGrant.user_id.is_not(None),
                (
                    AICreditGrant.pass_id.is_not(None)
                    & WorkbenchPass.revoked_at.is_(None)
                    & (WorkbenchPass.ends_at > stamp)
                )
                | (AICreditGrant.trial_id.is_not(None) & (AICreditGrant.expires_at > stamp)),
            )
        )
        or 0
    )
    return max(PAID_POOL_MIN, cards * PAID_POOL_PER_CARD)


# 付费全站名额的告警：用到 80%、满了各一次，每个进程每天记一下（几个实例可能各发一次，量很小）
_alerted: set[tuple[date, str]] = set()


def pool_alert(used: int, limit: int, day: date, *, alert=None) -> bool | None:
    """在 ai_cap_slot(pool="paid") 放锁以后调。用到 80% 或满了才发；同一天同一档只发一次。
    返回发送结果（没到档、发过了返回 None）。只写数，不写用户。"""
    level = "full" if used >= limit else "80" if used * 5 >= limit * 4 else None
    if level is None or (day, level) in _alerted:
        return None
    from youju.alerts import send_alert

    text = f"卡里 AI 次数的全站付费名额 {day}：已用 {used}/{limit}，" + (
        "满了，新的提交会被挡住、不扣次数，请查原因" if level == "full" else "到了 80%"
    )
    sent = (alert or send_alert)(text)
    if sent is not False:
        _alerted.add((day, level))
    return sent


def daily_stats(session: Session, day: date) -> dict:
    """日报、对账：当天占用按功能、来源（赠送/买来）、状态数。"""
    rows = session.execute(
        select(AICreditUse.kind, AICreditGrant.kind, AICreditUse.state, func.count(AICreditUse.id))
        .join(AICreditGrant, AICreditGrant.id == AICreditUse.grant_id)
        .where(AICreditUse.pool_day == day)
        .group_by(AICreditUse.kind, AICreditGrant.kind, AICreditUse.state)
    ).all()
    items = [
        {
            "kind": kind,
            "source": "bought" if grant_kind == PURCHASE else "gift",
            "state": state,
            "count": int(count),
        }
        for kind, grant_kind, state, count in rows
    ]
    return {"day": day, "items": items, "total": sum(item["count"] for item in items)}


# ---- 给次数、收回（支付这边调；接进付费流程在第二个 PR） ----------------------------


def grant_pass(
    session: Session, ticket: WorkbenchPass, kind: str, amount: int, stamp: datetime
) -> AICreditGrant:
    """给一段使用期发次数：买卡 purchase（天数 × 10）、续买多送 renewal（30）、上线补给 backfill
    （剩下的整天数 × 10）。同一段同一种只发一次，再调返回已有的那笔。"""
    if kind not in (PURCHASE, "renewal", "backfill") or amount <= 0:
        raise ValueError(f"不能给使用期发这种次数：{kind} {amount}")
    existing = session.scalar(
        select(AICreditGrant).where(AICreditGrant.pass_id == ticket.id, AICreditGrant.kind == kind)
    )
    if existing is not None:
        return existing
    grant = AICreditGrant(
        user_id=ticket.user_id, pass_id=ticket.id, kind=kind, amount=amount, granted_at=stamp
    )
    session.add(grant)
    session.flush()
    return grant


def grant_trial(session: Session, trial, stamp: datetime) -> AICreditGrant:
    """开加量体验时送 30 次，到体验原定的结束时间作废（体验期里买卡、体验提前结束也不变）。
    一个体验只送一次。"""
    existing = session.scalar(select(AICreditGrant).where(AICreditGrant.trial_id == trial.id))
    if existing is not None:
        return existing
    grant = AICreditGrant(
        user_id=trial.user_id,
        trial_id=trial.id,
        kind="trial",
        amount=TRIAL_CREDITS,
        expires_at=trial.ends_at,
        granted_at=stamp,
    )
    session.add(grant)
    session.flush()
    return grant


def revoke_pass_grants(session: Session, pass_ids: list[int], stamp: datetime) -> int:
    """退款、收回赠送、注销时：这些段上的次数作废。已经扣的照留；正在占着的照常走完（扣或退）。"""
    if not pass_ids:
        return 0
    return (
        session.execute(
            update(AICreditGrant)
            .where(AICreditGrant.pass_id.in_(pass_ids), AICreditGrant.revoked_at.is_(None))
            .values(revoked_at=stamp)
            .execution_options(synchronize_session="fetch")
        ).rowcount
        or 0
    )


def paid_ai_usage(
    session: Session, ticket: WorkbenchPass | None, *, lock: bool = False
) -> str | None:
    """这一段买来的 AI 次数用没用过（"用过付费的量"的 AI 部分，方案第五节第 3 条）：
    扣过返回 "used"，
    还占着（结果没出来）返回 "running"，都没有返回 None。赠送的次数怎么用都不算。lock=True（退款时）
    锁这一段的卡的行，锁的先后在任务之后、使用期之前。"""
    if ticket is None:
        return None
    stmt = select(AICreditGrant).where(
        AICreditGrant.pass_id == ticket.id, AICreditGrant.kind == PURCHASE
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    grants = list(session.scalars(stmt))
    if any(grant.used for grant in grants):
        return "used"
    if any(grant.held for grant in grants):
        return "running"
    return None
