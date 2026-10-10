"""奖励与购买共用使用期；grant 由调用方提交，自动发放与补发独立提交并隔离失败。"""

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from youju.advisor_tasks import BEIJING, _utc
from youju.config import get_settings
from youju.models import AdvisorTask, PassReward, User, WorkbenchPass

log = logging.getLogger("youju.rewards")
MONTH_LIMIT = 30
LABELS = {
    "first_batch": "第一次用批量功能",
    "correction": "纠错被采纳",
    "source": "提的栏目已接入",
    "referral": "请来一位同行",
    "deadline_clue": "补截止日期被采纳",
}


def enabled(settings) -> bool:
    return bool(
        getattr(settings, "rewards_enabled", False) and getattr(settings, "advisor_enabled", False)
    )


def month_bounds(stamp: datetime) -> tuple[datetime, datetime]:
    local = _utc(stamp).astimezone(BEIJING)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def monthly(session: Session, user_id: int, stamp: datetime):
    start, end = month_bounds(stamp)
    return select(PassReward).where(
        PassReward.user_id == user_id, PassReward.granted_at >= start, PassReward.granted_at < end
    )


def grant(
    session: Session,
    user_id: int,
    kind: str,
    days: int,
    ref_type: str,
    ref_id: int,
    stamp: datetime,
) -> PassReward:
    if kind not in LABELS or type(days) is not int or days <= 0:
        raise ValueError("奖励类型或天数不支持")
    stamp = _utc(stamp)
    # 和下单、提交任务、注销同序；不让待写外键在用户锁之前自动刷新。
    with session.no_autoflush:
        if session.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
            raise ValueError("用户已注销")
    existing = session.scalar(
        select(PassReward).where(PassReward.ref_type == ref_type, PassReward.ref_id == ref_id)
    )
    if existing is None and kind == "first_batch":
        existing = session.scalar(
            select(PassReward).where(PassReward.user_id == user_id, PassReward.kind == kind)
        )
    if existing is not None:
        return existing
    rows = session.scalars(monthly(session, user_id, stamp)).all()
    actual = min(days, max(0, MONTH_LIMIT - sum(r.granted_days for r in rows)))
    note = None
    if actual < days:
        note = "本月奖励已到上限"
    same = [r for r in rows if r.kind == kind and r.granted_days > 0]
    if kind in ("source", "referral") and len(same) >= {"source": 2, "referral": 4}[kind]:
        actual, note = 0, "本月该项奖励已到上限"
    if kind == "deadline_clue":
        actual = min(actual, max(0, 10 - sum(r.granted_days for r in same)))
        if actual < days:
            note = note or "本月该项奖励已到上限"
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


def invite_code(session: Session, user_id: int) -> str:
    user = session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ValueError("用户已注销")
    if user.invite_code:
        return user.invite_code
    # 随机 72 位，不编码用户身份；碰撞仅回滚保存点并换码。
    while True:
        try:
            with session.begin_nested():
                user.invite_code = secrets.token_urlsafe(9)
                session.flush()
            return user.invite_code
        except IntegrityError:
            session.refresh(user)


def first_completion(
    session: Session, user_id: int, stamp: datetime, *, settings=None
) -> dict[str, int]:
    """已持久化首次完成后单独发奖；两个奖励各自隔离失败，日志不含异常参数。"""
    result = {"granted": 0, "failed": 0}
    if not enabled(settings or get_settings()):
        return result
    # 全程按编号锁双方用户，避免注销引荐人时自关联外键与另一方互相等待。
    user = session.get(User, user_id)
    if user is None or user.batch_completed_at is None:
        return result
    inviter_id = user.invited_by_user_id
    session.rollback()
    for owner, kind, days, ref_type in (
        (user_id, "first_batch", 3, "user_first_batch"),
        (inviter_id, "referral", 7, "user_referral"),
    ):
        if owner is None:
            continue
        try:
            locked = {
                u.id: u
                for u in session.scalars(
                    select(User)
                    .where(User.id.in_(sorted({user_id, owner})))
                    .order_by(User.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            recipient = locked.get(user_id)
            if recipient is None or owner not in locked:
                session.rollback()
                continue
            if kind == "referral" and recipient.invited_by_user_id != owner:
                session.rollback()
                continue
            existing = session.scalar(
                select(PassReward.id).where(
                    PassReward.ref_type == ref_type, PassReward.ref_id == user_id
                )
            )
            if existing is None:
                grant(session, owner, kind, days, ref_type, user_id, stamp)
                result["granted"] += 1
            session.commit()
        except Exception:
            session.rollback()
            result["failed"] += 1
            log.warning("首次批量任务奖励未发出（%s），请运行 rewards-reconcile", kind)
    return result


def reconcile(session: Session, *, settings=None, stamp: datetime | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    result = {"granted": 0, "failed": 0}
    if not enabled(settings):
        return result
    stamp = _utc(stamp or datetime.now(UTC))
    # 兼容升级前已完成的任务；新完成标记不受任务删除或过期清理影响。
    ids = list(
        session.scalars(
            select(User.id)
            .where(
                User.batch_completed_at.is_not(None)
                | User.id.in_(
                    select(AdvisorTask.user_id).where(
                        AdvisorTask.kind.in_(("compare", "track")), AdvisorTask.status == "done"
                    )
                )
            )
            .order_by(User.id)
        )
    )
    session.rollback()
    for user_id in ids:
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            session.rollback()
            continue
        if user.batch_completed_at is None:
            user.batch_completed_at = (
                session.scalar(
                    select(func.min(AdvisorTask.finished_at)).where(
                        AdvisorTask.user_id == user_id,
                        AdvisorTask.kind.in_(("compare", "track")),
                        AdvisorTask.status == "done",
                    )
                )
                or stamp
            )
        session.commit()
        report = first_completion(session, user_id, stamp, settings=settings)
        for key in result:
            result[key] += report[key]
    return result


def anonymize(session: Session, user: User, stamp: datetime) -> None:
    # 发奖需要双方用户；注销也按编号锁关系中的用户，避免外键更新与发奖互锁。
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
    # 引荐关系断开，不收回另一方已经拿到的使用天数。
    session.execute(
        update(User)
        .where(User.invited_by_user_id == user.id)
        .values(invited_by_user_id=None, invited_at=None)
    )
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
    session.execute(
        update(WorkbenchPass)
        .where(WorkbenchPass.user_id == user.id, WorkbenchPass.reward_id.is_not(None))
        .values(user_id=None, revoked_at=stamp)
    )
    session.execute(update(PassReward).where(PassReward.user_id == user.id).values(user_id=None))
