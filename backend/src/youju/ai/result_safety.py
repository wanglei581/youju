"""AI 解读、条款对照的结果过两层内容安全（D-093 门槛第 4 条；带 AI 提审补齐清单第 4 道）。

admin 处理任务时（advisor_tasks.process_ai），结果存进去之前先查：
1. 本地高危词库（safety_words，在本机比，不发给任何人）；
2. 微信内容安全接口（wx_security.check_text，带提交任务的人的 openid；只有 pass 算通过）。

查的是结果里显示给用户的文字：每一条的原句（条目的 text 和 refs 里的 quote，都是政府原文
逐字摘的；模型自己写的话不显示，见 interpret.validate）。类型名、提醒、模型名称是我们写的
固定文字，不查；没有条目的（拒答）不查。复用缓存的结果也照样查：开关打开以前缓存的从没查过。

- 没通过：不显示、不存，任务记失败、退回次数（ResultBlocked，由调用方处理），另写一条处置记录
  （ai_safety_hits，190 天）：只记通知编号、任务编号、哪一层、原因，不存结果文字，不记用户编号。
- 接口出错、超时：不放行，抛 wx_security.CheckDown，由调用方按可重试的失败处理。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from youju import safety_words, wx_security
from youju.models import AdvisorTask, AISafetyHit, User

UNSAFE_MESSAGE = "这次的结果没有通过内容安全检查，不显示；这次没有扣你的次数。"
# 已经显示过的结果后来没通过（复用缓存时查出来的）：撤下，原来的任务也不再显示
REMOVED_MESSAGE = "这条结果没有通过内容安全检查，已撤下，不再显示。"
CHECK_DOWN_MESSAGE = "内容安全检测暂时不可用，请稍后重试；这次没有扣你的次数。"
HIT_RETENTION = timedelta(days=190)
# 微信返回这两种错误时重试也没用：用户两小时内没访问过小程序（61010）、当天调用次数用完（45009）
FINAL_ERRCODES = frozenset({61010, 45009})


class ResultBlocked(Exception):
    """结果没通过内容安全：不显示、不存。处置记录已经加进会话，随任务失败一起提交。"""


def result_text(result: dict | None, field: str) -> str:
    """结果里显示给用户的原句，去重后按出现的先后接起来，一行一句。"""
    seen: list[str] = []
    for item in (result or {}).get(field) or []:
        if not isinstance(item, dict):
            continue
        refs = item.get("refs") if isinstance(item.get("refs"), list) else []
        for value in (
            item.get("text"),
            *(ref.get("quote") for ref in refs if isinstance(ref, dict)),
        ):
            if isinstance(value, str) and value.strip() and value not in seen:
                seen.append(value)
    return "\n".join(seen)


def retryable(exc: wx_security.CheckDown) -> bool:
    """接口出错要不要重排：网络、超时、临时错误重排；重试也没用的错误码直接失败。"""
    return exc.errcode not in FINAL_ERRCODES


def _record(
    session: Session, task: AdvisorTask, *, layer: str, reason: dict, stamp: datetime
) -> None:
    doc_id = (task.snapshot or {}).get("doc_id") or (task.input_json or {}).get("doc_id")
    session.add(
        AISafetyHit(
            created_at=stamp,
            uid=None,
            kind=task.kind,
            stage="answer",
            layer=layer,
            reason_json=reason,
            text=None,
            document_id=doc_id,
            task_id=task.id,
            expires_at=stamp + HIT_RETENTION,
        )
    )


def check(
    session: Session,
    task: AdvisorTask,
    result: dict,
    *,
    field: str,
    stamp: datetime,
    cached_from: int | None = None,
) -> None:
    """两层都过了才返回；没过抛 ResultBlocked，接口出错抛 wx_security.CheckDown。
    cached_from：这份结果是从哪个任务的缓存复制来的，写进处置记录的原因里。"""
    items = (result or {}).get(field) or []
    text = result_text(result, field)
    if not text:
        if items:
            # 有条目却取不出要查的文字：结构不对，不放行
            raise ValueError("结果里有条目，但没有可以查的原句")
        return
    source = {"cachedFrom": cached_from} if cached_from else {}
    hit = safety_words.check(text)
    if hit is not None:
        reason = {"category": hit.category, "entry": hit.entry_id, "version": hit.version}
        _record(session, task, layer="local", reason={**reason, **source}, stamp=stamp)
        raise ResultBlocked
    user = session.get(User, task.user_id)
    if user is None:
        # 处理途中注销了：不放行（任务随用户删除，调用方回滚）
        raise LookupError("提交任务的人已经注销")
    verdict = wx_security.check_text(user.openid, text)
    if not verdict.passed:
        reason = {"suggest": verdict.suggest, "label": verdict.label}
        _record(session, task, layer="wechat", reason={**reason, **source}, stamp=stamp)
        raise ResultBlocked
