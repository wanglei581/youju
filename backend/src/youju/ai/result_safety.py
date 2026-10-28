"""AI 解读、条款对照的结果过两层内容安全（D-093 门槛第 4 条；带 AI 提审补齐清单第 4 道）。

admin 处理任务时（advisor_tasks.process_ai），结果存进去之前先查：
1. 本地高危词库（safety_words，在本机比，不发给任何人）：一句一句查，免得前一句的结尾和
   后一句的开头拼出一个词；
2. 微信内容安全接口（wx_security.check_text，带提交任务的人的 openid；只有 pass 算通过）：
   原句合起来发（超过 2500 字分段）。

查的是结果里显示给用户的文字：每一条的原句（条目的 text 和 refs 里的 quote，都是政府原文
逐字摘的；模型自己写的话不显示，见 interpret.validate）。类型名、提醒、模型名称是我们写的
固定文字，不查；没有条目的（拒答）不查。复用缓存的结果也照样查：开关打开以前缓存的从没查过。

- 通过：结果里记上标记 safety（词库版本、查没查微信）。读的时候没有这个标记的不显示，在云托管里
  读的（线上）标记里还要写着查过微信（mark_ok）；词库版本变了的先用本地词库再查一遍
  （visible_after_update）；每天的清理把命中的撤下、写处置记录（advisor_tasks.sweep_results）。
- 没通过：不显示、不存，任务记失败、退回次数（ResultBlocked，由调用方处理），另写一条处置记录
  （ai_safety_hits，190 天）：通知编号、任务编号、哪一层、原因、正文指纹和原句的段号（正文重抓
  以后也能对回当时是哪几句）；不存结果文字，不记用户编号。同一份内容 1 天内再提交，直接按没通过
  处理，不再调模型和微信（advisor_tasks.BLOCK_CACHE）：解读、对照的记录里另记缓存键、提示词版本
  和模型，按这几项认，原来的任务删了、提交的人注销了也认得出。
- 接口出错、超时：暂时性的（网络、超时、5xx、返回看不懂、errcode -1 系统繁忙、44991 频率限制）
  原地重试，最多查 3 次；还不行，或者是重试也没用的错误（权限、用户两小时内没访问过、当天次数
  用完等），抛 wx_security.CheckDown：任务直接失败、退回次数，不重排（重排要重新调模型）。

问这份通知（ai/ask.py，问一问开发设计第九节第 6 条）的回答也用 check：查的是逐字核对、扩成整句
以后要显示的原句（条目的 text）。模型的输出在流式接收时已经边收边过了一遍本地词库（ask._Guard），
这里查的是扩成整句以后的原句。问这份通知的处置记录另记这一问的编号（turn_id）和提问的人的
去标识化用户编号（uid，同访问日志、同提问被拦的记录；第九节第 7 条、问一问使用说明第 3 条）；
解读、对照的结果不是用户写的，照旧不记用户编号。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, cast, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from youju import access_log, safety_words, wx_security
from youju.models import AdvisorTask, AIChatTurn, AISafetyHit, AppKey, User

log = logging.getLogger(__name__)

UNSAFE_MESSAGE = "这次的结果没有通过内容安全检查，不显示；这次没有扣你的次数。"
# 已经显示过的结果后来没通过（复用缓存时、词库更新以后查出来的）：撤下，原来的任务也不再显示
REMOVED_MESSAGE = "这条结果没有通过内容安全检查，已撤下，不再显示。"
CHECK_DOWN_MESSAGE = "内容安全检测暂时不可用，请稍后重试；这次没有扣你的次数。"
# 内容安全检查上线以前生成的结果：没查过，不显示
UNCHECKED_MESSAGE = "这条结果生成时还没有内容安全检查，不再显示；请重新提交。"
HIT_RETENTION = timedelta(days=190)
# 微信接口暂时性的错误码：系统繁忙（-1）、频率限制（44991）。其余错误码重试也没用
TRANSIENT_ERRCODES = frozenset({-1, 44991})
CHECK_ATTEMPTS = 3
CHECK_PAUSE = 1.5  # 两次之间等几秒


_ALERT_EVERY = timedelta(hours=1)  # 检测接口出错的群提醒，所有进程合起来每小时最多一条
_ALERT_KEY = "ai_check_down_alert"  # app_keys 里记上次提醒的时间（秒）


def alert_check_down(session: Session, exc: wx_security.CheckDown, stamp: datetime) -> None:
    """AI 任务因为内容安全检测出错没做成：往运维群提醒，只写错误码。admin 调不通微信接口时每个
    AI 任务都会失败，要尽早知道。advisor-run 每分钟起一个新进程，节流记在库里（app_keys 一行），
    抢到这一小时的才发。61010（这个用户两小时内没访问过小程序）是单个用户的事，不提醒。"""
    if exc.errcode == 61010:
        return
    now = int(stamp.timestamp())
    try:
        session.execute(
            mysql_insert(AppKey).prefix_with("IGNORE").values(name=_ALERT_KEY, value="0")
        )
        claimed = session.execute(
            update(AppKey)
            .where(
                AppKey.name == _ALERT_KEY,
                cast(AppKey.value, BigInteger) <= now - int(_ALERT_EVERY.total_seconds()),
            )
            .values(value=str(now))
        ).rowcount
        session.commit()
    except Exception as error:  # noqa: BLE001 - 节流记不上就不发，免得一分钟一条；只记类型
        session.rollback()
        log.warning("内容安全检测出错的提醒没记上：%s", type(error).__name__)
        return
    if not claimed:
        return
    from youju.alerts import send_alert

    hint = {45009: "当天的调用次数用完了（没上架的小程序每天 100 次）"}.get(
        exc.errcode,
        "可能是 admin 调不通微信接口：看云托管的开放接口服务、"
        "令牌权限里有没有登记 wxa/msg_sec_check",
    )
    try:
        send_alert(f"AI 结果的内容安全检测出错（errcode {exc.errcode}），AI 任务没做成。{hint}")
    except Exception as error:  # noqa: BLE001 - 提醒发不出去不能影响任务收尾；只记类型
        log.warning("内容安全检测出错的提醒没发出去：%s", type(error).__name__)


class ResultBlocked(Exception):
    """结果没通过内容安全：不显示、不存。处置记录已经加进会话，随任务失败一起提交。"""


class RecentlyBlocked(ResultBlocked):
    """同一份内容 1 天内真查过、没通过：这次不再调模型和微信，直接按没通过处理，也不另写记录。"""


class UserGone(LookupError):
    """处理途中提交任务的人注销了：不放行；任务随用户删掉，调用方丢掉结果。"""


def quotes(result: dict | None, field: str) -> list[tuple[str | None, str]]:
    """结果里显示给用户的原句（段号, 原句），去重后按出现的先后。问这份通知的原句没有 refs，
    段号就是条目自己的 sid（ai/ask.py）。"""
    seen: dict[str, str | None] = {}
    for item in (result or {}).get(field) or []:
        if not isinstance(item, dict):
            continue
        refs = [ref for ref in item.get("refs") or [] if isinstance(ref, dict)]
        first = refs[0].get("pid") if refs else item.get("sid")
        for sid, value in (
            (first, item.get("text")),
            *((r.get("pid"), r.get("quote")) for r in refs),
        ):
            if isinstance(value, str) and value.strip() and value not in seen:
                seen[value] = sid if isinstance(sid, str) else None
    return [(sid, value) for value, sid in seen.items()]


def result_text(result: dict | None, field: str) -> str:
    """交给微信的文字：原句去重后按出现的先后接起来，一行一句。"""
    return "\n".join(value for _, value in quotes(result, field))


def local_hit(pairs: list[tuple[str | None, str]], library=None):
    """一句一句过本地词库：返回（命中, 段号）或 None。"""
    for sid, value in pairs:
        hit = safety_words.check(value, library)
        if hit is not None:
            return hit, sid
    return None


def mark(*, wechat: bool) -> dict:
    """通过检查的标记：词库版本、查没查微信（本机开发和测试不调微信）。"""
    return {"words": safety_words.LIBRARY.version, "wechat": wechat}


def mark_ok(stamp) -> bool:
    """标记算不算数：要有词库版本，还要写着查过微信。只有本机开发和测试（YOUJU_ROLE=all、不在
    云托管里）不调微信，wechat 是 false 也认；线上的 api、admin 一律要查过（Codex 10/28：不靠
    配置推断在不在线上，admin 配错、没认出自己在云托管里时，没交微信查的结果照样不显示）。"""
    if not isinstance(stamp, dict) or not isinstance(stamp.get("words"), str):
        return False
    if stamp.get("wechat") is True:
        return True
    settings = wx_security.get_settings()
    return getattr(settings, "role", "all") == "all" and not wx_security.in_cloud(settings)


def _pause(seconds: float) -> None:
    time.sleep(seconds)  # 测试里换掉，不真等


def _transient(exc: wx_security.CheckDown) -> bool:
    return exc.errcode is None or exc.errcode in TRANSIENT_ERRCODES


def _wechat(openid: str, text: str, task: AdvisorTask) -> wx_security.Verdict:
    for attempt in range(1, CHECK_ATTEMPTS + 1):
        try:
            return wx_security.check_text(openid, text)
        except wx_security.CheckDown as exc:
            # 只记任务编号和错误码，不记文字和 openid
            log.warning(
                "AI 结果内容安全检测出错：任务 %s，第 %d 次，errcode %s",
                task.id,
                attempt,
                exc.errcode,
            )
            if attempt == CHECK_ATTEMPTS or not _transient(exc):
                raise
            _pause(CHECK_PAUSE)
    raise AssertionError("不会走到这里")


def _record(
    session: Session, task: AdvisorTask, *, layer: str, reason: dict, stamp: datetime
) -> None:
    snapshot = task.snapshot or {}
    doc_id = snapshot.get("doc_id") or (task.input_json or {}).get("doc_id")
    if snapshot.get("body_hash"):
        reason = {**reason, "bodyHash": snapshot["body_hash"]}
    if task.kind != "ask" and task.cache_key:
        # 解读、对照：1 天内同一份内容再提交按这三项认（advisor_tasks._recently_blocked），
        # 任务删了、提交的人注销了也认得出。缓存键是正文和核对情况的指纹，不含用户的数据
        reason = {
            **reason,
            "cacheKey": task.cache_key,
            "promptVersion": task.prompt_version,
            "model": task.model,
        }
    session.add(
        AISafetyHit(
            created_at=stamp,
            uid=_uid(session, task),
            kind=task.kind,
            stage="answer",
            layer=layer,
            reason_json=reason,
            text=None,
            document_id=doc_id,
            task_id=task.id,
            turn_id=_turn_id(session, task),
            expires_at=stamp + HIT_RETENTION,
        )
    )


def _uid(session: Session, task: AdvisorTask) -> str | None:
    """问这份通知的回答被拦时记提问的人（去标识化，同访问日志）；人已经注销了为 None。
    解读、对照的结果不是用户写的，不记。"""
    if task.kind != "ask":
        return None
    openid = session.scalar(select(User.openid).where(User.id == task.user_id))
    return access_log.log_uid(openid, session.get_bind()) if openid is not None else None


def _turn_id(session: Session, task: AdvisorTask) -> int | None:
    """问这份通知的回答被拦时记是哪一问（只记编号）：任务输入里有（7 天后清空），
    没有就按任务编号找。"""
    if task.kind != "ask":
        return None
    turn_id = (task.input_json or {}).get("turn_id")
    if isinstance(turn_id, int):
        return turn_id
    return session.scalar(select(AIChatTurn.id).where(AIChatTurn.task_id == task.id))


def record_local(
    session: Session, task: AdvisorTask, found, *, stamp: datetime, extra: dict | None = None
) -> None:
    hit, sid = found
    reason = {"category": hit.category, "entry": hit.entry_id, "version": hit.version}
    if sid:
        reason["sid"] = sid
    _record(session, task, layer="local", reason={**reason, **(extra or {})}, stamp=stamp)


def check(
    session: Session,
    task: AdvisorTask,
    result: dict,
    *,
    field: str,
    stamp: datetime,
    cached_from: int | None = None,
) -> dict | None:
    """两层都过了，返回要记进结果的标记（没有原句的返回 None）；没过抛 ResultBlocked，
    接口出错抛 wx_security.CheckDown。cached_from：这份结果是从哪个任务的缓存复制来的。"""
    items = (result or {}).get(field) or []
    pairs = quotes(result, field)
    if not pairs:
        if items:
            # 有条目却取不出要查的文字：结构不对，不放行
            raise ValueError("结果里有条目，但没有可以查的原句")
        return None
    source = {"cachedFrom": cached_from} if cached_from else {}
    found = local_hit(pairs)
    if found is not None:
        record_local(session, task, found, stamp=stamp, extra=source)
        raise ResultBlocked
    # 现读一次：领任务的会话提交时不让对象过期，get 会拿到处理开始时读的那个人，注销了也看不出来
    openid = session.scalar(select(User.openid).where(User.id == task.user_id))
    if openid is None:
        # 处理途中注销了：不放行，也不把他的 openid 交给微信（任务随用户删除，调用方回滚）
        raise UserGone("提交任务的人已经注销")
    in_cloud = wx_security.in_cloud(wx_security.get_settings())
    verdict = _wechat(openid, "\n".join(value for _, value in pairs), task)
    if not verdict.passed:
        reason = {"suggest": verdict.suggest, "label": verdict.label}
        if verdict.trace_id:
            reason["traceId"] = verdict.trace_id
        _record(session, task, layer="wechat", reason={**reason, **source}, stamp=stamp)
        raise ResultBlocked
    return mark(wechat=in_cloud)


def visible_after_update(result: dict | None, field: str) -> str | None:
    """读的时候用：结果能不能照常显示。不能就返回给用户看的原因。

    - 没有标记：内容安全检查上线以前生成的，没查过，不显示；线上读到没查过微信的标记也一样
      （mark_ok）；
    - 标记的词库版本和现在的不一样：先用现在的本地词库再查一遍（在本机比，不调微信），命中的不显示。
      撤下和写处置记录由 admin 每天的清理做（api 不写处置记录）。"""
    if not result or not (result.get(field) or []):
        return None  # 拒答、没有条目：只有我们写的固定文字
    stamp = result.get("safety")
    if not mark_ok(stamp):
        return UNCHECKED_MESSAGE
    if stamp.get("words") != safety_words.LIBRARY.version and local_hit(quotes(result, field)):
        return REMOVED_MESSAGE
    return None
