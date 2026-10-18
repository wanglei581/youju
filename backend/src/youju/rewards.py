"""奖励与购买共用使用期；grant 由调用方提交，自动发放与补发独立提交并隔离失败。

首次批量奖励和同行引荐已删（D-061、D-070 不再做），防重复领取的凭据（reward_claims）随之不再写入：
注销即删，和加量体验一个口径（D-071 第②条）。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from youju.advisor_tasks import BEIJING, _utc
from youju.config import get_settings
from youju.models import (
    AdvisorTask,
    DeadlineClue,
    PassReward,
    User,
    WorkbenchPass,
)

MONTH_LIMIT = 30
# 纠错被采纳（D-070 ⑦）：每条送 1 天，每人每月最多 3 天
CORRECTION_DAYS = 1
CORRECTION_MONTH_DAYS = 3
# 采纳标准（D-070 ⑦，开关开着时纠错页公开写出来）
CORRECTION_RULE = (
    "第一个指出、经核对属实的事实错误（日期、链接、名单状态等）才算；重复的、核对不了的不算"
)
LABELS = {
    "first_batch": "第一次用批量功能",
    "correction": "纠错被采纳",
    "source": "提的栏目已接入",
    "referral": "请来一位同行",
    "deadline_clue": "补截止日期被采纳",
    # 随订单发的赠送（payments.py 发放和收回），不是奖励：不走 grant，不占每月奖励上限
    "renewal": "续买多送",
    "trial": "加量体验折算",
}
ORDER_KINDS = ("renewal", "trial")
REVOKED_PREFIX = "已收回："
# 首次批量、同行引荐不再发放（D-061、D-070）；名称留着，旧流水照常显示
RETIRED_KINDS = ("first_batch", "referral")


def is_revoked(reward: PassReward) -> bool:
    # 零天、已注销奖励没有可用的使用期，统一用保留在流水中的前缀判重。
    return bool(reward.note and reward.note.startswith(REVOKED_PREFIX))


def lock_reward(session: Session, reward_id: int) -> tuple[PassReward, WorkbenchPass | None]:
    with session.no_autoflush:
        owner = session.scalar(select(PassReward.user_id).where(PassReward.id == reward_id))
        if owner is not None:
            session.scalar(select(User.id).where(User.id == owner).with_for_update())
        reward = session.scalar(
            select(PassReward)
            .where(PassReward.id == reward_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reward is None:
            raise ValueError("没有找到这笔奖励")
        ticket = session.scalar(
            select(WorkbenchPass)
            .where(WorkbenchPass.reward_id == reward_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    return reward, ticket


def revoke(session: Session, reward_id: int, reason: str, stamp: datetime) -> str:
    """只收回未用时间，不退月额度；与 grant 一样由调用方提交事务。"""
    from youju.payments import revoke_workbench_pass

    reward, ticket = lock_reward(session, reward_id)
    if is_revoked(reward):
        return "已经收回过"
    if reward.user_id is not None and ticket is not None:
        revoke_workbench_pass(session, ticket, _utc(stamp))
    # 为原备注预留位置；原备注本身很长时也须留至少一个字记收回原因。
    suffix = ("；" + reward.note)[:195] if reward.note else ""
    reward.note = (
        REVOKED_PREFIX + reason.strip()[: 200 - len(REVOKED_PREFIX) - len(suffix)] + suffix
    )
    return "已收回"


def enabled(settings) -> bool:
    return bool(
        getattr(settings, "rewards_enabled", False) and getattr(settings, "advisor_enabled", False)
    )


def correction_enabled(settings) -> bool:
    """纠错被采纳送天数（D-070 ⑦）：只看单独开关 correction_reward_enabled
    （旧的奖励总开关开着也不算，Codex 审阅），还要批量工具开关开着（天数是批量工具的使用期）。"""
    return bool(
        getattr(settings, "correction_reward_enabled", False)
        and getattr(settings, "advisor_enabled", False)
    )


def correction_info(settings) -> dict | None:
    """纠错页公开写的规则（开关关着时为 None，页面不提送天数）。"""
    if not correction_enabled(settings):
        return None
    return {"days": CORRECTION_DAYS, "monthLimit": CORRECTION_MONTH_DAYS, "rule": CORRECTION_RULE}


def month_bounds(stamp: datetime) -> tuple[datetime, datetime]:
    local = _utc(stamp).astimezone(BEIJING)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def monthly(session: Session, user_id: int, stamp: datetime):
    """这个月发的奖励（不含随订单送的续买多送、体验折算）。"""
    start, end = month_bounds(stamp)
    return select(PassReward).where(
        PassReward.user_id == user_id,
        PassReward.granted_at >= start,
        PassReward.granted_at < end,
        PassReward.kind.not_in(ORDER_KINDS),
    )


def grant(
    session: Session,
    user_id: int,
    kind: str,
    days: int,
    ref_type: str,
    ref_id: int,
    stamp: datetime,
) -> PassReward | None:
    if kind not in LABELS or kind in ORDER_KINDS or type(days) is not int or days <= 0:
        raise ValueError("奖励类型或天数不支持")
    if kind in RETIRED_KINDS:
        raise ValueError("这种奖励已不再发放")
    stamp = _utc(stamp)
    # 和下单、提交任务、注销同序；不让待写外键在用户锁之前自动刷新。
    with session.no_autoflush:
        if session.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
            raise ValueError("用户已注销")
    existing = session.scalar(
        select(PassReward).where(PassReward.ref_type == ref_type, PassReward.ref_id == ref_id)
    )
    if existing is not None:
        return existing
    rows = session.scalars(monthly(session, user_id, stamp)).all()
    actual = min(days, max(0, MONTH_LIMIT - sum(r.granted_days for r in rows)))
    note = None
    if actual < days:
        note = "本月奖励已到上限"
    same = [r for r in rows if r.kind == kind and r.granted_days > 0]
    if kind == "source" and len(same) >= 2:
        actual, note = 0, "本月该项奖励已到上限"
    if kind == "deadline_clue":
        actual = min(actual, max(0, 10 - sum(r.granted_days for r in same)))
        if actual < days:
            note = note or "本月该项奖励已到上限"
    if kind == "correction":
        # D-070 ⑦：纠错被采纳每人每月最多 3 天（超出的记零天流水，备注写明）
        actual = min(actual, max(0, CORRECTION_MONTH_DAYS - sum(r.granted_days for r in same)))
        if actual < days:
            note = note or f"本月纠错奖励已到上限（每月最多 {CORRECTION_MONTH_DAYS} 天）"
    reward = PassReward(
        user_id=user_id,
        kind=kind,
        requested_days=days,
        granted_days=actual,
        ref_type=ref_type,
        ref_id=ref_id,
        granted_at=stamp,
        note=note,
    )
    # 跨用户对同一关联对象的并发由唯一约束兜底，只回滚本次插入。
    try:
        with session.begin_nested():
            session.add(reward)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(PassReward).where(PassReward.ref_type == ref_type, PassReward.ref_id == ref_id)
        )
        if existing is None:
            raise
        return existing
    if actual:
        latest = session.scalar(
            select(func.max(WorkbenchPass.ends_at)).where(
                WorkbenchPass.user_id == user_id, WorkbenchPass.revoked_at.is_(None)
            )
        )
        start = max(stamp, latest) if latest else stamp
        session.add(
            WorkbenchPass(
                user_id=user_id,
                reward_id=reward.id,
                starts_at=start,
                ends_at=start + timedelta(days=actual),
            )
        )
        session.flush()
    return reward


def reconcile(session: Session, *, settings=None, stamp: datetime | None = None) -> dict[str, int]:
    """补发被采纳的截止线索奖励：发奖和核对通过不在同一个事务里，发奖出错只记日志，靠这里补上。"""
    settings = settings or get_settings()
    result = {"granted": 0, "failed": 0}
    if not enabled(settings):
        return result
    stamp = _utc(stamp or datetime.now(UTC))
    from youju.deadline_clues import award_accepted

    report = award_accepted(session, settings, stamp)
    for key in result:
        result[key] += report[key]
    return result


def anonymize(session: Session, user: User, stamp: datetime) -> None:
    """注销：待核对的线索删掉，奖励流水去掉和这个账号的关联，没用完的奖励天数作废；
    不留防重复领取的凭据（注销即删）。"""
    # 引荐已删，库里可能还有以前留下的引荐关系：按编号锁关系中的用户，再断开关系。
    related = set(session.scalars(select(User.id).where(User.invited_by_user_id == user.id))) | {
        user.id
    }
    if user.invited_by_user_id is not None:
        related.add(user.invited_by_user_id)
    list(
        session.scalars(
            select(User.id).where(User.id.in_(sorted(related))).order_by(User.id).with_for_update()
        )
    )
    if (
        session.scalar(
            select(DeadlineClue.id)
            .where(DeadlineClue.user_id == user.id, DeadlineClue.status == "pending")
            .limit(1)
        )
        is not None
    ):
        session.execute(
            delete(DeadlineClue).where(
                DeadlineClue.user_id == user.id, DeadlineClue.status == "pending"
            )
        )
    if (
        session.scalar(select(DeadlineClue.id).where(DeadlineClue.user_id == user.id).limit(1))
        is not None
    ):
        session.execute(
            update(DeadlineClue).where(DeadlineClue.user_id == user.id).values(user_id=None)
        )
    # 引荐关系断开，不收回另一方已经拿到的使用天数。
    session.execute(
        update(User)
        .where(User.invited_by_user_id == user.id)
        .values(invited_by_user_id=None, invited_at=None)
    )
    # 以前的首次批量、引荐流水的 ref_id 记的是首次完成者的编号，一并清掉
    linked = (PassReward.ref_type.in_(("user_first_batch", "user_referral"))) & (
        PassReward.ref_id == user.id
    )
    if session.scalar(select(PassReward.id).where(linked).limit(1)) is not None:
        session.execute(update(PassReward).where(linked).values(ref_id=None))
    if session.scalar(select(PassReward.id).where(PassReward.user_id == user.id).limit(1)) is None:
        return
    list(
        session.scalars(
            select(AdvisorTask.id).where(AdvisorTask.user_id == user.id).with_for_update()
        )
    )
    if (
        session.scalar(
            select(WorkbenchPass.id)
            .where(WorkbenchPass.user_id == user.id, WorkbenchPass.reward_id.is_not(None))
            .limit(1)
        )
        is not None
    ):
        session.execute(
            update(WorkbenchPass)
            .where(WorkbenchPass.user_id == user.id, WorkbenchPass.reward_id.is_not(None))
            .values(user_id=None, revoked_at=stamp)
        )
    session.execute(update(PassReward).where(PassReward.user_id == user.id).values(user_id=None))
