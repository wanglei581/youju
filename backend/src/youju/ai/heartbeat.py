"""两个服务的 AI 设置心跳（D-075）：api 只在 admin 能处理时才收 AI 任务。

AI 的开关、开放范围、白名单、全站每天上限都是环境变量，api、admin 两个服务各填一份。
谁有权用、还剩几次以 api 为准；admin 的后台任务按自己那份设置领任务。两份不一样时，
用户提交以后任务会失败（admin 没开 AI 时一分钟内以"AI 暂时不可用"失败、退回次数）。所以：

- admin 每分钟单独跑一次 `youju ai-heartbeat`，写一行心跳（service=admin）：它这边能不能处理
  AI 任务（开关开着、对外 AI 的密钥配好了），加上设置指纹。不放在 advisor-run 里写，是因为一批
  任务可能处理好几分钟，心跳会断；
- api 的能力接口和提交接口看这行：说处理不了、超过 5 分钟没来、或者指纹和 api 自己算的对不上，
  就当 AI 暂时不可用，入口变灰、不收任务、不扣次数；
- api 最多每 5 分钟写一行自己的设置（service=api：总开关和指纹），admin 拿来比对。

提醒（往运维群，youju.alerts）。ai-heartbeat 每分钟查，下面三件有哪件就写在同一条里，每小时最多一次
（alerted_at）：
- 两个服务的设置对不上；
- 设置一样、api 开着 AI，admin 却处理不了（多半是 admin 的对外 AI 密钥没配）：这时 api 一直
  不收任务；
- admin 处理不了（没开 AI 或缺密钥）、退回了排队的任务。退回了几个不靠时间推算：advisor-run 退回时
  在同一个事务里把个数加进 admin 那一行（refunds_pending），提醒发出去以后才减掉这次报了的，没发出去
  下一分钟照样在。用户被移出白名单时的退回（admin 能处理）不加。
另外，admin 能处理 AI 任务、api 的心跳却超过 30 分钟没来：多半是 mysql_accounts.sql 的"AI 设置心跳
权限"没执行，这时设置对不上也发现不了，每天最多提醒一次（notice_at）。
发送失败或没配提醒机器人时不记时间，下一次照常再试。

只有 api 服务（YOUJU_ROLE=api）才看心跳：本机开发和测试是同一个进程、同一份设置，不会对不上。

问这份通知（问一问开发设计第六节第 3 条"只走单独线路"）另记一项 ask_ready：admin 能处理 AI 任务、
并且对外 AI 的单独线路三项都配好了（ask_capable）。api 的能力接口、发一问看它（ask_ready），
本机开发和测试按本进程的设置算。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from youju.ai import advisor_endpoint, advisor_route_ready, interpret
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


def ask_capable(settings) -> bool:
    """admin 这边能处理问这份通知：能处理 AI 任务，对外 AI 的单独线路三项都配好了（不退回后台线路，
    问一问开发设计第六节第 3 条），并且按流式接收（YOUJU_AI_ADVISOR_STREAM）：边收边查高危词、
    发现就中断，没开流式不开放、不留"整段收完再查"的退路（第九节第 6 条，Codex 第 8 轮）。"""
    return (
        worker_capable(settings)
        and advisor_route_ready(settings)
        and bool(getattr(settings, "ai_advisor_stream", False))
    )


def beat(session: Session, settings, service: str, stamp: datetime) -> AIHeartbeat:
    """写这个服务的一行心跳并提交。admin 写它能不能处理 AI 任务、问答能不能用；api 写它的总开关。"""
    ask = False
    if service == "admin":
        enabled = worker_capable(settings)
        ask = ask_capable(settings)
    else:
        enabled = bool(getattr(settings, "ai_advisor_enabled", False))
    row = session.get(AIHeartbeat, service)
    if row is None:
        row = AIHeartbeat(service=service, beat_at=stamp, ai_enabled=enabled)
        session.add(row)
    row.beat_at, row.ai_enabled, row.fingerprint = stamp, enabled, fingerprint(settings)
    row.ask_ready = ask
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


def ask_ready(session: Session, settings, stamp: datetime) -> bool:
    """问这份通知能不能收：api 服务上看 admin 的心跳（同 worker_ready，另要 ask_ready）；
    其他角色（本机开发、测试）按本进程的设置算。"""
    if not enforced(settings):
        return ask_capable(settings)
    row = session.get(AIHeartbeat, "admin")
    return bool(
        row is not None
        and row.ai_enabled
        and row.ask_ready
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


def add_refunds(session: Session, count: int, stamp: datetime) -> None:
    """advisor-run 因为 admin 处理不了退回了 count 个排队的 AI 任务：和退回放在同一个事务里，
    把个数加进 admin 那一行（由调用方提交）。admin 那一行还没有时先占一行"处理不了"，
    下一分钟的心跳会写全。"""
    if count <= 0:
        return
    table = AIHeartbeat.__table__
    stmt = mysql_insert(table).values(
        service="admin", beat_at=stamp, ai_enabled=False, fingerprint="", refunds_pending=count
    )
    session.execute(stmt.on_duplicate_key_update(refunds_pending=table.c.refunds_pending + count))


def check_and_alert(session: Session, settings, stamp: datetime) -> str | None:
    """admin 上每分钟跑（youju ai-heartbeat）：设置是否一致、api 开着时 admin 能不能处理、有没有退回
    任务、api 心跳在不在。要提醒就发，返回这次发出的提醒文字（没发为 None）。"""
    # 退回的个数是别的进程（advisor-run）加的：从库里重新读，不用会话里旧的那份
    admin = session.get(AIHeartbeat, "admin", populate_existing=True)
    if admin is None:
        return None
    api = session.get(AIHeartbeat, "api", populate_existing=True)
    fresh_api = api is not None and stamp - api.beat_at <= 2 * API_EVERY
    problems = []
    if fresh_api and api.fingerprint != admin.fingerprint:
        problems.append(
            "api 和 admin 两个服务的 AI 设置不一致（总开关、开放范围、白名单或全站每天上限）。"
            "对不上期间小程序不收 AI 任务，请在云托管控制台把两个服务改成一样"
        )
    elif fresh_api and api.ai_enabled and not admin.ai_enabled:
        problems.append(
            "api 开着 AI，admin 服务却处理不了 AI 任务（两边设置一样，多半是 admin 的"
            "对外 AI 密钥没配）。现在小程序不收 AI 任务，请检查 admin 服务的环境变量"
        )
    pending = admin.refunds_pending or 0
    if pending:
        problems.append(
            f"admin 服务处理不了 AI 任务（没开 AI 或对外 AI 密钥没配），退回了 {pending} 个"
            "排队的 AI 任务（用户看到的是 AI 暂时不可用，次数已退）。"
            "请检查 admin 服务的环境变量是否和 api 一样"
        )
    if problems:
        if admin.alerted_at is not None and stamp - admin.alerted_at < ALERT_EVERY:
            return None
        problem = "\n".join(problems)
        if not _send(problem):
            return None
        admin.alerted_at = stamp
        if pending:
            # 只减掉这次报了的：发送期间 advisor-run 又退回的留到下次
            admin.refunds_pending = AIHeartbeat.refunds_pending - pending
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
