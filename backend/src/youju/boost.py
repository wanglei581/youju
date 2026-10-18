"""加量体验（D-070 ③）和申报季加量（D-070 ⑤）：各有单独开关，默认都关。

加量体验是单独的一种权益，不是批量工具的使用天数（不发 WorkbenchPass）：
- 每个微信身份一次；用户自己在确认页点"开始体验"才起算，连续 72 小时，不能暂停；
- 体验期内批量名单比对、客户相关通知表每次 500 家以内，两项合计每天（北京时间）最多 10 次，
  记在体验账本上（advisor_usage 的 trial 类型），不占每月免费次数；当天 10 次用完就等第二天，
  不改用每月免费次数（D-070 第二轮：先用完免费次数再开体验的人，体验结束不能只剩 0 次）；
- 到期按 ends_at 自动回到免费量，不需要定时任务；体验期的结果和其他任务一样保存 30 天；
- 已有付费使用期的不能开始（体验留到以后），万一重叠，先用体验次数、当天用完再用使用期。
开关关掉只是不能开始新的体验，已经开始的照常用满 72 小时（开始前承诺了连续 3 天）。
"每个微信一次"跨注销靠去标识凭据：注销时记一条 reward_claims（kind=boost_trial，HMAC 编号，
不含微信身份），清理任务 180 天后删除。

申报季加量：起止日期（北京时间，含首尾）内的那几天，所在月份的免费批量次数按每月 5 次算
（这个月已用的次数都算进去），每次仍 50 家以内；离开起止日期就回到 3 次，多用的不倒扣。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from youju import advisor_tasks as service
from youju import payments
from youju.models import BoostTrial, RewardClaim, User

TRIAL_HOURS = 72
TRIAL_DAILY = 10
TRIAL_MAX_ROWS = 500
SEASON_MONTHLY = 5
CLAIM_KIND = "boost_trial"
DATA_KINDS = ("compare", "track")
USED = "加量体验已经用过，每个微信只能体验一次"
HAS_PASS = "使用期内不限次数，不需要加量体验；体验留到以后再用"


class TrialError(Exception):
    """不能开始体验：code 给小程序区分情况，消息直接给用户看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def trial_enabled(settings) -> bool:
    """能不能开始新的体验：体验开关和批量工具开关都开着。"""
    return bool(
        getattr(settings, "boost_trial_enabled", False)
        and getattr(settings, "advisor_enabled", False)
    )


def season_window(settings) -> tuple[date, date] | None:
    if not getattr(settings, "season_boost_enabled", False):
        return None
    start = getattr(settings, "season_boost_start", None)
    end = getattr(settings, "season_boost_end", None)
    # 正式配置在启动时已校验；不经校验的配置（测试替身）缺日期或倒置时按没开处理
    if start is None or end is None or start > end:
        return None
    return start, end


def free_limit(settings, kind: str, day: date) -> int:
    """这一天的免费次数上限：申报季内的批量工具每月 5 次，其他照旧（AI 两项不受影响）。"""
    window = season_window(settings) if kind in DATA_KINDS else None
    if window is not None and window[0] <= day <= window[1]:
        return SEASON_MONTHLY
    return service.usage_limit(kind)


def season_status(settings, day: date) -> dict | None:
    """批量工具页显示的起止日期：还没开始的提前显示（D-070：提前公布），过了结束日不再显示。"""
    window = season_window(settings)
    if window is None or day > window[1]:
        return None
    return {
        "start": window[0].isoformat(),
        "end": window[1].isoformat(),
        "active": window[0] <= day,
        "limit": SEASON_MONTHLY,
        "normalLimit": service.usage_limit("compare"),
        "maxRows": service.MAX_ROWS,
    }


def trial_of(session: Session, user_id: int) -> BoostTrial | None:
    return session.scalar(select(BoostTrial).where(BoostTrial.user_id == user_id))


def is_active(trial: BoostTrial | None, stamp: datetime) -> bool:
    stamp = service._utc(stamp)
    return trial is not None and service._utc(trial.started_at) <= stamp < service._utc(
        trial.ends_at
    )


def active_trial(session: Session, user_id: int, stamp: datetime) -> BoostTrial | None:
    trial = trial_of(session, user_id)
    return trial if is_active(trial, stamp) else None


def claimed(session: Session, openid: str) -> bool:
    """这个微信以前的账号注销前用过体验（凭据还在保存期内）。"""
    from youju.rewards import claim_pseudonym

    return (
        session.scalar(
            select(RewardClaim.kind)
            .where(
                RewardClaim.pseudonym == claim_pseudonym(session, openid),
                RewardClaim.kind == CLAIM_KIND,
            )
            .limit(1)
        )
        is not None
    )


def start(session: Session, user: User, stamp: datetime) -> BoostTrial:
    """开始体验；调用方已检查开关、锁定用户行，并负责提交。

    已经在体验中就返回原记录（重复点"开始"不会重新起算）。同一用户并发开始由唯一约束兜底，
    调用方收到 IntegrityError 后重新读一次。"""
    stamp = service._utc(stamp)
    trial = trial_of(session, user.id)
    if trial is not None:
        if is_active(trial, stamp):
            return trial
        raise TrialError("TRIAL_USED", USED)
    if claimed(session, user.openid):
        raise TrialError("TRIAL_USED", USED)
    if payments.current_pass(session, user.id, stamp) is not None:
        raise TrialError("TRIAL_HAS_PASS", HAS_PASS)
    trial = BoostTrial(
        user_id=user.id, started_at=stamp, ends_at=stamp + timedelta(hours=TRIAL_HOURS)
    )
    session.add(trial)
    session.flush()
    return trial


def _iso(value: datetime | None) -> str | None:
    return service.business_now(value).isoformat() if value is not None else None


def trial_status(
    session: Session,
    settings,
    user: User | None,
    stamp: datetime,
    counts: dict[str, int] | None = None,
) -> dict | None:
    """批量工具页和确认页用的体验状态。开关关着、也没有进行中的体验时返回 None（页面不出现体验）。

    available：现在能开始（开关开着、没用过、没有使用期）；reason 说明为什么不能：
    used（用过了）、pass（使用期内）。还没同意协议的用户（user 为 None）也显示入口，
    点开始时先去同意。"""
    trial = trial_of(session, user.id) if user is not None else None
    active = is_active(trial, stamp)
    if not active and not trial_enabled(settings):
        return None
    result = {
        "available": False,
        "active": active,
        "reason": None,
        "hours": TRIAL_HOURS,
        "dailyLimit": TRIAL_DAILY,
        "maxRows": TRIAL_MAX_ROWS,
        "startedAt": _iso(trial.started_at) if trial else None,
        "endsAt": _iso(trial.ends_at) if trial else None,
        "usedToday": 0,
    }
    if active:
        if counts is None:
            counts = service.usage_counts(session, user.id, service.usage_day(stamp))
        result["usedToday"] = counts.get(service.TRIAL_USAGE, 0)
    elif trial is not None or (user is not None and claimed(session, user.openid)):
        result["reason"] = "used"
    elif user is not None and payments.current_pass(session, user.id, stamp) is not None:
        result["reason"] = "pass"
    else:
        result["available"] = True
    return result


def anonymize(session: Session, user: User, stamp: datetime) -> None:
    """注销：用过体验的，记一条去标识凭据（HMAC 编号，不含微信身份、不连用户，180 天后清理），
    体验记录随用户删除（外键级联）。从没用过的什么也不写，开关一直关着时不需要新的写权限。"""
    if trial_of(session, user.id) is None and not claimed(session, user.openid):
        return
    from youju.rewards import claim_pseudonym

    session.execute(
        insert(RewardClaim)
        .prefix_with("IGNORE")
        .values(
            pseudonym=claim_pseudonym(session, user.openid),
            kind=CLAIM_KIND,
            claimed_at=service._utc(stamp),
        )
    )
