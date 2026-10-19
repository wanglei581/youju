"""两个服务的 AI 设置心跳（D-075）：api 只在 admin 能处理时才收 AI 任务。

AI 的开关、开放范围、白名单、全站每天上限都是环境变量，api、admin 两个服务各填一份。
谁有权用、还剩几次以 api 为准；admin 的后台任务按自己那份设置领任务。两份不一样时，
用户提交以后任务会失败（admin 没开 AI 时一分钟内以"功能未开放"失败）。所以：

- admin 的后台任务每分钟写一行心跳（service=admin）：它这边能不能处理 AI 任务，加上设置指纹；
- api 的能力接口和提交接口看这行：说关着、超过 5 分钟没来、或者指纹和 api 自己算的对不上，
  就当 AI 暂时不可用，入口变灰、不收任务；
- api 最多每 5 分钟写一行自己的指纹（service=api），admin 拿来比对，对不上就往运维群发提醒，
  每小时最多一次。api 账号没有这张表的写权限时（mysql_accounts.sql 的"AI 设置心跳权限"没执行），
  api 照常工作，只是收不到这条提醒。

只有 api 服务（YOUJU_ROLE=api）才看心跳：本机开发和测试是同一个进程、同一份设置，不会对不上。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from youju.ai import interpret
from youju.models import AIHeartbeat

log = logging.getLogger(__name__)

STALE = timedelta(minutes=5)
API_EVERY = timedelta(minutes=5)
ALERT_EVERY = timedelta(hours=1)
UNAVAILABLE = "AI 暂时不可用，请稍后再试"

_api_lock = Lock()
_api_written_at: datetime | None = None
_api_denied_logged = False


def fingerprint(settings) -> str:
    """总开关、开放范围、白名单、全站每天上限的哈希；不含密钥、模型线路，也不存白名单明文。"""
    data = {
        "enabled": bool(getattr(settings, "ai_advisor_enabled", False)),
        "audience": getattr(settings, "ai_advisor_audience", "whitelist"),
        "openids": sorted(interpret.openids(settings)),
        "cap": int(getattr(settings, "ai_advisor_daily_cap", 0)),
    }
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def enforced(settings) -> bool:
    return getattr(settings, "role", "all") == "api"


def beat(session: Session, settings, service: str, stamp: datetime) -> AIHeartbeat:
    """写这个服务的一行心跳并提交。admin 写它能不能处理 AI 任务；api 写它的总开关。"""
    if service == "admin":
        enabled = interpret.worker_enabled(settings)
    else:
        enabled = bool(getattr(settings, "ai_advisor_enabled", False))
    row = session.get(AIHeartbeat, service)
    if row is None:
        row = AIHeartbeat(service=service, beat_at=stamp, ai_enabled=enabled)
        session.add(row)
    row.beat_at, row.ai_enabled, row.fingerprint = stamp, enabled, fingerprint(settings)
    session.commit()
    return row


def worker_ready(session: Session, settings, stamp: datetime) -> bool:
    """api 服务上：admin 能处理 AI 任务、心跳新鲜、设置指纹和 api 的一样。其他角色总是 True。"""
    if not enforced(settings):
        return True
    row = session.get(AIHeartbeat, "admin")
    return bool(
        row is not None
        and row.ai_enabled
        and stamp - row.beat_at <= STALE
        and row.fingerprint == fingerprint(settings)
    )


def note_api(session: Session, settings, stamp: datetime) -> None:
    """api 服务上最多每 5 分钟写一次自己的指纹。写不进（没有权限、库忙）只记一次日志，
    不影响请求。"""
    global _api_written_at, _api_denied_logged
    if not enforced(settings):
        return
    with _api_lock:
        if _api_written_at is not None and stamp - _api_written_at < API_EVERY:
            return
        _api_written_at = stamp
    try:
        beat(session, settings, "api", stamp)
    except SQLAlchemyError:
        session.rollback()
        if not _api_denied_logged:
            _api_denied_logged = True
            log.warning("api 写不进 AI 设置心跳（mysql_accounts.sql 的 AI 设置心跳权限没执行？）")


def check_and_alert(
    session: Session, settings, stamp: datetime, *, refunded: int = 0
) -> str | None:
    """admin 每分钟跑（advisor-run 之后）：两个服务的设置对不上，或者因为自己没开 AI 退回了排队的
    AI 任务，就往运维群发提醒，每小时最多一次。返回这次发出的提醒文字（没发为 None）。"""
    admin = session.get(AIHeartbeat, "admin")
    if admin is None:
        return None
    api = session.get(AIHeartbeat, "api")
    problem = None
    if (
        api is not None
        and stamp - api.beat_at <= 2 * API_EVERY
        and api.fingerprint != admin.fingerprint
    ):
        problem = (
            "api 和 admin 两个服务的 AI 设置不一致（总开关、开放范围、白名单或全站每天上限）。"
            "对不上期间小程序不收 AI 任务，请在云托管控制台把两个服务改成一样"
        )
    elif refunded:
        problem = (
            f"admin 服务没开 AI，退回了 {refunded} 个排队的 AI 任务（用户看到失败）。"
            "请检查 admin 服务的 YOUJU_AI_ADVISOR_ENABLED 等设置是否和 api 一样"
        )
    if problem is None:
        return None
    if admin.alerted_at is not None and stamp - admin.alerted_at < ALERT_EVERY:
        return None
    from youju.alerts import send_alert

    send_alert(problem)
    admin.alerted_at = stamp
    session.commit()
    return problem
