"""问一问的对话怎么存、怎么删（开发设计 #328 第四节、第十节）。接口在 api/chat.py。

- 每一问建的时候加 30 天到期（TURN_RETENTION）；读的时候先排除到期的，每天的清理再物理删除，
  一问都不剩的对话一起删。内容安全的违规记录（ai_safety_hits）190 天，由 cleanup_advisor 删，
  删对话、注销不提前删。
- 删对话（用户删、注销）先退回还在排队或处理中的问答占的那 1 次（只退 ask 那本，ask_try 不退），
  再删问答任务、问答和对话。外键级联只当兜底。
- 本地高危词库定稿以前问一问不开放（chat_ready，第九节第 3 条）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from youju import advisor_tasks as service
from youju import safety_words
from youju.agreements import CHAT_AGREEMENT_VERSION
from youju.ai import result_safety
from youju.models import AdvisorTask, AIChat, AIChatTurn, User

TURN_RETENTION = timedelta(days=30)
# 每人每天交给模型的问这份通知最多 20 问（第八节）：单独一本账，永不退回
ASK_TRY = "ask_try"
ASK_TRY_LIMIT = 20
IN_FLIGHT = ("queued", "running")


# 《问一问使用说明》按 D-065 定稿以后改成 True（草稿 docs/合规/隐私指引补充_问一问_v1.md）
CHAT_TERMS_FINAL = False


def chat_ready() -> bool:
    """问一问能不能开：本地高危词库为问一问定稿（final）、《问一问使用说明》定稿以前都不开放
    （第九节第 3 条、第十三节第 1 条）。两样各管各的：词库定稿不会顺带把问一问打开。"""
    return bool(safety_words.LIBRARY.final) and CHAT_TERMS_FINAL


def consented(user: User | None) -> bool:
    """同意过当前版本的"问一问使用说明"（第十三节第 1 条）；版本升了要重新同意。"""
    return bool(
        user is not None
        and user.chat_agreement_at is not None
        and user.chat_agreement_version == CHAT_AGREEMENT_VERSION
    )


def turn_status(turn: AIChatTurn, task: AdvisorTask | None) -> str:
    """一问的状态：找通知建好就是 done；问这份通知照任务的状态，规则当场答的是 done。"""
    if turn.skill == "find" or turn.answer_json is not None:
        return "done"
    if task is None:
        return "failed"
    return task.status if task.status in ("queued", "running", "done") else "failed"


def counted(turn: AIChatTurn, task: AdvisorTask | None) -> bool:
    """这一问算没算次数：只有问这份通知找到了原句才算（没退回占的那 1 次）。找到以后又被每天的
    清理按新词库撤下的（advisor_tasks.sweep_results）没有退次数，也算。"""
    if turn.skill != "ask" or task is None or task.usage_released or task.usage_source != "free":
        return False
    return task.status == "done" or (
        task.status == "failed" and task.error == result_safety.REMOVED_MESSAGE
    )


def live_turns(stamp: datetime):
    """还没到期的问（读的时候先排除到期的，第十节第 1 条）。"""
    return AIChatTurn.expires_at > stamp


def delete_chats(session: Session, chat_ids: list[int]) -> None:
    """删整条对话：先退回排队中、处理中的问答占的次数，再删任务、问答和对话。

    调用方要先锁住用户行（同发一问的锁顺序：先用户、再对话）。处理中的那一问由 admin 收尾时
    发现任务和这一问都没了，丢掉结果（第六节第 3 条"处理时"第 7 步）。"""
    if not chat_ids:
        return
    task_ids = select(AIChatTurn.task_id).where(
        AIChatTurn.chat_id.in_(chat_ids), AIChatTurn.task_id.is_not(None)
    )
    tasks = list(
        session.scalars(
            select(AdvisorTask)
            .where(AdvisorTask.id.in_(task_ids))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for task in tasks:
        if task.status in IN_FLIGHT:
            service.release_usage(session, task)
    if tasks:
        session.execute(delete(AdvisorTask).where(AdvisorTask.id.in_([t.id for t in tasks])))
    session.execute(delete(AIChatTurn).where(AIChatTurn.chat_id.in_(chat_ids)))
    session.execute(delete(AIChat).where(AIChat.id.in_(chat_ids)))


def delete_user_chats(session: Session, user_id: int) -> int:
    """注销：在删任务的同一个事务里先显式删这个人的对话和问答（第十节第 3 条）。

    先不加锁查有没有对话，没有就什么都不发：正式库给 api 账号授权对话表之前，注销不能因为缺权限
    失败（MySQL 按语句检查权限：删 0 行的 DELETE、加锁的 SELECT … FOR UPDATE 都要写权限；
    同 advisor_tasks.delete_user_tasks）。"""
    if session.scalar(select(AIChat.id).where(AIChat.user_id == user_id).limit(1)) is None:
        return 0
    # 先锁用户、再锁对话（同发一问、删对话的锁顺序）
    session.execute(select(User.id).where(User.id == user_id).with_for_update()).one_or_none()
    chat_ids = list(
        session.scalars(
            select(AIChat.id)
            .where(AIChat.user_id == user_id)
            .order_by(AIChat.id)  # 和每天的清理同一个加锁顺序
            .with_for_update()
        )
    )
    delete_chats(session, chat_ids)
    return len(chat_ids)


def cleanup(session: Session, stamp: datetime) -> dict[str, int]:
    """每天 03:30 的清理（cleanup_advisor 里调，单独提交）：到期的问、一问都不剩的对话。
    先锁对话、再删问（同删对话的锁顺序），免得和用户删对话互相等。问答任务照 AI 任务的规矩
    30 天删；违规记录的到期删除也在 cleanup_advisor。不提交，由调用方提交。"""
    chat_ids = list(
        session.scalars(select(AIChatTurn.chat_id).where(AIChatTurn.expires_at <= stamp).distinct())
    )
    if not chat_ids:
        return {"ai_chat_turns": 0, "ai_chats": 0}
    locked = list(
        session.scalars(
            select(AIChat.id).where(AIChat.id.in_(chat_ids)).order_by(AIChat.id).with_for_update()
        )
    )
    if not locked:
        return {"ai_chat_turns": 0, "ai_chats": 0}  # 这期间对话被用户删了
    turns = (
        session.execute(
            delete(AIChatTurn).where(AIChatTurn.chat_id.in_(locked), AIChatTurn.expires_at <= stamp)
        ).rowcount
        or 0
    )
    empty = ~exists(select(AIChatTurn.id).where(AIChatTurn.chat_id == AIChat.id))
    chats = session.execute(delete(AIChat).where(AIChat.id.in_(locked), empty)).rowcount or 0
    return {"ai_chat_turns": turns, "ai_chats": chats}
