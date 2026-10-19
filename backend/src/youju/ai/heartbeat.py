"""两个服务的 AI 设置心跳（D-075）：api 只在 admin 能处理时才收 AI 任务。

AI 的开关、开放范围、白名单、全站每天上限都是环境变量，api、admin 两个服务各填一份。
谁有权用、还剩几次以 api 为准；admin 的后台任务按自己那份设置领任务。两份不一样时，
用户提交以后任务会失败（admin 没开 AI 时一分钟内以"功能未开放"失败）。所以：

- admin 每分钟单独跑一次 `youju ai-heartbeat`，写一行心跳（service=admin）：它这边能不能处理
  AI 任务（开关开着、对外 AI 的密钥配好了），加上设置指纹。不放在 advisor-run 里写，是因为一批
  任务可能处理好几分钟，心跳会断；
- api 的能力接口和提交接口看这行：说处理不了、超过 5 分钟没来、或者指纹和 api 自己算的对不上，
  就当 AI 暂时不可用，入口变灰、不收任务、不扣次数；
- api 最多每 5 分钟写一行自己的指纹（service=api），admin 拿来比对。

提醒（往运维群，youju.alerts）：
- 两个服务的设置对不上，或者 admin 因为自己没开 AI 退回了排队的任务：每小时最多一次（alerted_at）；
- admin 能处理 AI 任务、api 的心跳却超过 30 分钟没来：多半是 mysql_accounts.sql 的"AI 设置心跳
  权限"没执行，这时设置对不上也发现不了，每天最多提醒一次（notice_at）。
发送失败或没配提醒机器人时不记时间，下一次照常再试。

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

from youju.ai import advisor_endpoint, interpret
from youju.models import AIHeartbeat

log = logging.getLogger(__name__)

STALE = timedelta(minutes=5)
API_EVERY = timedelta(minutes=5)
API_MISSING = timedelta(minutes=30)
ALERT_EVERY = timedelta(hours=1)
NOTICE_EVERY = timedelta(days=1)
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


def worker_capable(settings) -> bool:
    """admin 这边真能处理 AI 任务：开关开着、有人能用，而且对外 AI 的密钥配好了。"""
    return interpret.worker_enabled(settings) and bool(advisor_endpoint(settings)[1])


def beat(session: Session, settings, service: str, stamp: datetime) -> AIHeartbeat:
    """写这个服务的一行心跳并提交。admin 写它能不能处理 AI 任务；api 写它的总开关。"""
    if service == "admin":
        enabled = worker_capable(settings)
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


def _send(text: str) -> bool:
    from youju.alerts import send_alert

    return send_alert(text) is True


def check_and_alert(
    session: Session, settings, stamp: datetime, *, refunded: int = 0
) -> str | None:
    """admin 上跑：ai-heartbeat 每分钟查设置是否一致、api 心跳在不在；advisor-run 因为自己没开 AI
    退回了任务时带上 refunded。要提醒就发，返回这次发出的提醒文字（没发为 None）。"""
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
    if problem is not None:
        if admin.alerted_at is not None and stamp - admin.alerted_at < ALERT_EVERY:
            return None
        if not _send(problem):
            return None
        admin.alerted_at = stamp
        session.commit()
        return problem
    missing = api is None or stamp - api.beat_at > API_MISSING
    if not (admin.ai_enabled and missing):
        return None
    if admin.notice_at is not None and stamp - admin.notice_at < NOTICE_EVERY:
        return None
    notice = (
        "admin 能处理 AI 任务，但 api 服务超过 30 分钟没有写 AI 设置心跳。多半是"
        'mysql_accounts.sql 里"AI 设置心跳权限"那一行还没执行；没执行时两个服务设置对不上也发现不了'
    )
    if not _send(notice):
        return None
    admin.notice_at = stamp
    session.commit()
    return notice
