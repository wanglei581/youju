"""问一问的对话接口（D-078、D-093；开发设计 #328 第五节），挂在 /api/v1/advisor 下面。

- 发一问（POST /chat/turns）、按通知取对话（GET /chat）、找通知的示例（GET /chat/examples）：
  AI 对这个人开着、同意过含 AI 条款的协议才能用；本地高危词库定稿以前一律 404
  （chat_store.chat_ready）。发一问还要同意过当前版本的"问一问使用说明"（CHAT_CONSENT）。
- 看和删自己的对话（GET /chats、GET /chats/{id}、GET /chat/turns/{id}、DELETE /chats/{id}）
  只认本人，不看开关、白名单和协议版本（D-093 门槛第 8 条）；别人的、不存在的都是 404。
- 发一问先按请求编号找原来那一问，找到就原样返回，不再做下面的检查；没有才按顺序查：
  限频 → 长度 → 号码 → 本地高危词库 → 问某家企业在不在名单里 → 微信内容安全（第九节）。
  被拦下的都不建行、不扣次数；内容安全拦下的另记违规记录。
- 用户打的字只存在 ai_chat_turns.question（和找通知回答里的 keywords、docNumber）；
  日志、审计里只记编号、技能和错误代码，不记问题文字（第十节第 6 条）。
- 问这份通知（skill=ask）由 PR-3 接上；现在提交回 AI_UNAVAILABLE，能力接口也不给 ask。
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from threading import Lock
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from youju import access_log, chat_find, chat_guard, chat_store, dedup, safety_words, wx_security
from youju import advisor_tasks as service
from youju.ai import heartbeat, interpret, result_safety
from youju.api import advisor as advisor_api
from youju.api import v1
from youju.api.me import KnownUserDep, UserDep, request_openid
from youju.config import get_settings
from youju.db import get_session
from youju.models import AdvisorTask, AIChat, AIChatTurn, AISafetyHit, Document, Source, User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/advisor", tags=["问一问"])
SessionDep = Annotated[Session, Depends(get_session)]

PAGE_SIZE = 20  # 打开对话时给最近几问，before=<seq> 往前翻
LIST_LIMIT = 200  # "我的 AI 记录"最多列几条对话
MAX_TEXT = {"find": 100, "ask": 200}
RATE_LIMIT = 20  # 每人每分钟最多发几问（两个技能合计，被拦下的也算）
LAST_QUESTION_CHARS = 30
NOT_FOUND = "没有找到接口"
AI_TERMS_UPDATED = "AI 条款已更新，请更新小程序并重新阅读同意协议"
WHAT_LABELS = {
    "credit_code": "统一社会信用代码",
    "mobile": "手机号",
    "id_card": "身份证号",
    "bank": "银行卡号",
}
GO_LISTS = "要查企业在不在政府公开名单里，请用“查名单”，输入企业全称。"
GO_LISTS_FIND = "要查企业在不在政府公开名单里，可以用“查名单”，输入企业全称。"
UNSAFE = "内容可能不合规，请修改后再发。这次不算次数。"
CHECK_DOWN = "内容安全检测暂时不可用，请稍后再发。这次不算次数。"


def _stamp() -> datetime:
    return v1.now().astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(v1._TZ).isoformat() if value is not None else None


def _refuse(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _visible(request: Request) -> None:
    """先于协议校验隐藏：AI 对这个人没开、词库没定稿，都是 404，不说"未开放"（D-075）。"""
    try:
        openid = request_openid(request)
    except HTTPException:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    if not chat_store.chat_ready() or not interpret.enabled_for_openid(get_settings(), openid):
        raise HTTPException(status_code=404, detail=NOT_FOUND)


def _authorize(user: User) -> None:
    """同 api/advisor._authorize：白名单里、对所有人开放后没同意含 AI 条款的协议回 403。"""
    settings = get_settings()
    if interpret.enabled_for_openid(settings, user.openid) and not interpret.ai_agreed(
        settings, user
    ):
        raise HTTPException(status_code=403, detail=AI_TERMS_UPDATED)
    if not interpret.enabled(settings, user):
        raise HTTPException(status_code=404, detail=NOT_FOUND)


# 限频：按实例在内存里计（同 v1._limit_lookup），内存里最多记 1000 个人
_TIMES: OrderedDict[int, deque[float]] = OrderedDict()
_TIMES_LOCK = Lock()


def _monotonic() -> float:
    return time.monotonic()


def _limit(user_id: int) -> None:
    stamp = _monotonic()
    with _TIMES_LOCK:
        times = _TIMES.get(user_id, deque())
        while times and times[0] <= stamp - 60:
            times.popleft()
        if len(times) >= RATE_LIMIT:
            raise _refuse(429, "CHAT_TOO_FAST", "发得太快了，请稍等一会儿再发。")
        times.append(stamp)
        _TIMES[user_id] = times
        _TIMES.move_to_end(user_id)
        while len(_TIMES) > 1000:
            _TIMES.popitem(last=False)


class TurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill: Literal["find", "ask"]
    # 长度在接口里按技能另查（CHAT_TEXT）；这里只挡明显不对的大请求
    text: str = Field(max_length=1000)
    docId: int | None = Field(default=None, ge=1, le=2**31 - 1)


def _group_keys(session: Session, doc_id: int | None) -> list[int]:
    """按通知找对话用的编号：现在的合并组（第四节第 1 条）；不带通知的那条是 0。"""
    if doc_id is None:
        return [0]
    master = dedup.canonical_ids(session, [doc_id]).get(doc_id, doc_id)
    keys = dedup.group_ids(session, master)
    return keys if doc_id in keys else [*keys, doc_id]


def _chat_for(
    session: Session, user_id: int, doc_id: int | None, *, lock: bool = False
) -> AIChat | None:
    """这份通知（或不带通知）的那条对话：组里有几条时取最近一问更新的那条。"""
    stmt = (
        select(AIChat)
        .where(AIChat.user_id == user_id, AIChat.doc_key.in_(_group_keys(session, doc_id)))
        .order_by(AIChat.updated_at.desc(), AIChat.id.desc())
        .limit(1)
    )
    if lock:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def _turn_by_key(session: Session, user_id: int, key: str, stamp: datetime):
    """这个人所有对话里用过这个请求编号的那一问（到期的不算）。"""
    return session.execute(
        select(AIChatTurn, AIChat)
        .join(AIChat, AIChat.id == AIChatTurn.chat_id)
        .where(
            AIChat.user_id == user_id,
            AIChatTurn.idempotency_key == key,
            chat_store.live_turns(stamp),
        )
    ).first()


def _same_request(session: Session, turn: AIChatTurn, chat: AIChat, body: TurnIn) -> bool:
    if turn.skill != body.skill or turn.question != body.text.strip():
        return False
    if turn.skill == "ask":
        return turn.doc_id == body.docId
    return chat.doc_key in _group_keys(session, body.docId)


def _tasks_of(session: Session, turns: list[AIChatTurn]) -> dict[int, AdvisorTask]:
    ids = [turn.task_id for turn in turns if turn.task_id]
    if not ids:
        return {}
    return {
        task.id: task
        for task in session.scalars(select(AdvisorTask).where(AdvisorTask.id.in_(ids)))
    }


def _turn_out(
    session: Session, turn: AIChatTurn, task: AdvisorTask | None, *, stamp: datetime
) -> dict:
    status = chat_store.turn_status(turn, task)
    out = {
        "id": turn.id,
        "seq": turn.seq,
        "skill": turn.skill,
        "question": turn.question,
        "createdAt": _iso(turn.created_at),
        "status": status,
        "queuePosition": 0,
        "etaSeconds": 0,
        "counted": chat_store.counted(turn, task),
        "answer": None,
        "error": None,
        "errorCode": None,
    }
    if turn.skill == "find":
        out["answer"] = chat_find.render(
            session, turn.answer_json or {}, today=service.usage_day(stamp)
        )
    elif turn.answer_json is not None:
        # 规则当场答的（要问清楚、范围外、不能代写），PR-3 接上
        out["answer"] = turn.answer_json
    elif task is not None and task.status == "queued":
        out.update(
            service.queue_info(session, task, getattr(get_settings(), "ai_advisor_concurrency", 3))
        )
    return out


def _chat_out(
    session: Session, chat: AIChat, *, stamp: datetime, before: int | None = None
) -> dict | None:
    """一条对话：最近 PAGE_SIZE 问（按先后），before 往前翻。一问都没了（都到期）返回 None。"""
    conditions = [AIChatTurn.chat_id == chat.id, chat_store.live_turns(stamp)]
    # 一问都没了（都到期）就当没有这条对话，带不带 before 都一样
    if session.scalar(select(func.count(AIChatTurn.id)).where(*conditions)) == 0:
        return None
    if before is not None:
        conditions.append(AIChatTurn.seq < before)
    turns = list(
        session.scalars(
            select(AIChatTurn)
            .where(*conditions)
            .order_by(AIChatTurn.seq.desc())
            .limit(PAGE_SIZE + 1)
        )
    )
    more = len(turns) > PAGE_SIZE
    turns = sorted(turns[:PAGE_SIZE], key=lambda turn: turn.seq)
    tasks = _tasks_of(session, turns)
    return {
        "id": chat.id,
        **_doc_out(session, chat),
        "createdAt": _iso(chat.created_at),
        "updatedAt": _iso(chat.updated_at),
        "turns": [_turn_out(session, turn, tasks.get(turn.task_id), stamp=stamp) for turn in turns],
        "hasMore": more,
    }


def _doc_out(session: Session, chat: AIChat, titles: dict | None = None) -> dict:
    """对话的通知：hasDoc 是带不带通知；下架的不给编号和标题（同 advisor._document_out）。"""
    if chat.doc_key == 0:
        return {"hasDoc": False, "docId": None, "docTitle": None, "linkGone": None}
    if titles is None:
        titles = advisor_api._document_titles(session, [chat.doc_key])
    return {"hasDoc": True, **advisor_api._document_out(chat.doc_key, titles)}


def _record_hit(
    session: Session,
    user: User,
    *,
    skill: str,
    layer: str,
    reason: dict,
    text: str,
    stamp: datetime,
) -> None:
    """提问没过内容安全：记一条违规记录（第九节第 7 条）。写不进去也照样拦下，日志不带那句话。"""
    try:
        session.add(
            AISafetyHit(
                created_at=stamp,
                uid=access_log.log_uid(user.openid, session.get_bind()),
                kind=skill,
                stage="question",
                layer=layer,
                reason_json=reason,
                text=text[:200],
                expires_at=stamp + result_safety.HIT_RETENTION,
            )
        )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        log.warning("违规记录写入失败：%s（技能 %s，%s）", type(exc).__name__, skill, layer)


def _check_local(session: Session, user: User, skill: str, text: str, stamp: datetime) -> None:
    """提问在本机的检查（第九节第 2–4 条）：号码 → 本地高危词库 → 问某家企业在不在名单里。
    都在本机，不发给任何人；前一步拦下就不往下走。"""
    what = chat_guard.sensitive(text)
    if what:
        label = WHAT_LABELS[what]
        if skill == "ask":
            message = (
                f"你输入的内容里像有{label}。这一项会把你打的字发给 AI 模型，请删掉后再发。"
                "这次不算次数。"
            )
        elif what == "credit_code":
            message = f"你输入的像是统一社会信用代码，请删掉后再发。{GO_LISTS_FIND}"
        else:
            message = f"你输入的内容里像有{label}，请删掉后再发。"
        raise _refuse(422, "CHAT_SENSITIVE", message, what=what)
    hit = safety_words.check(text)
    if hit is not None:
        reason = {"category": hit.category, "entry": hit.entry_id, "version": hit.version}
        _record_hit(
            session, user, skill=skill, layer="local", reason=reason, text=text, stamp=stamp
        )
        raise _refuse(422, "CHAT_UNSAFE", UNSAFE)
    if chat_guard.goes_to_lists(text):
        raise _refuse(422, "CHAT_GO_LISTS", GO_LISTS)


def _check_wechat(session: Session, user: User, skill: str, text: str, stamp: datetime) -> None:
    """提问交微信内容安全复核（第九节第 5 条）。网络请求在拿用户锁之前做
    （同 create_deadline_clue）；本机开发和测试里不调（wx_security）。"""
    try:
        verdict = wx_security.check_text(user.openid, text)
    except wx_security.CheckDown:
        raise _refuse(503, "CHAT_CHECK_DOWN", CHECK_DOWN) from None
    if not verdict.passed:
        reason = {"suggest": verdict.suggest, "label": verdict.label}
        if verdict.trace_id:
            reason["traceId"] = verdict.trace_id  # 事后向微信核查用
        _record_hit(
            session, user, skill=skill, layer="wechat", reason=reason, text=text, stamp=stamp
        )
        raise _refuse(422, "CHAT_UNSAFE", UNSAFE)


def _public_document(session: Session, doc_id: int) -> bool:
    return (
        session.scalar(
            select(Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(Document.id == doc_id, v1.public_condition())
        )
        is not None
    )


def _replay(session: Session, turn: AIChatTurn, chat: AIChat, body: TurnIn, response, stamp):
    """同一个请求编号再发：一样就返回原来那一问，不一样回 409（同 create_task）。"""
    if not _same_request(session, turn, chat, body):
        raise _refuse(409, "CHAT_KEY_REUSED", "同一个请求编号不能用于不同的问题，请重新发送")
    response.status_code = 202 if turn.skill == "ask" else 200
    task = _tasks_of(session, [turn]).get(turn.task_id)
    return {"chatId": chat.id, "turn": _turn_out(session, turn, task, stamp=stamp)}


@router.post("/chat/turns", dependencies=[Depends(_visible)])
def create_turn(
    body: TurnIn,
    request: Request,
    response: Response,
    session: SessionDep,
    user: UserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """发一问。找通知当场答完（200）；问这份通知建任务（202，PR-3）。"""
    _authorize(user)
    if not chat_store.consented(user):
        raise _refuse(403, "CHAT_CONSENT", "请先阅读并同意问一问使用说明")
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise _refuse(422, "CHAT_KEY", "请提供 Idempotency-Key 请求头（不超过 128 字符）")
    key = idempotency_key.strip()
    stamp = _stamp()
    # 先查请求编号，再做检查：断线重发不会撞上限频或内容安全出错，也不会把同一句话再发给微信
    found = _turn_by_key(session, user.id, key, stamp)
    if found is not None:
        return _replay(session, *found, body, response, stamp)
    _limit(user.id)
    text = body.text.strip()
    if not text:
        raise _refuse(422, "CHAT_TEXT", "请先输入要问的内容。")
    if len(text) > MAX_TEXT[body.skill]:
        raise _refuse(422, "CHAT_TEXT", "最多 200 字（找通知最多 100 字）。")
    # 本机的检查两个技能都做（号码、高危词、问名单）；问这份通知的后台由 PR-3 接上，
    # 在这之前查完本机的就不收，也不把字发给微信或模型
    _check_local(session, user, body.skill, text, stamp)
    if body.skill == "ask":
        raise _refuse(429, "AI_UNAVAILABLE", heartbeat.UNAVAILABLE)
    # 带通知新建对话时这份要是公开的；已有对话的照常能发（通知下架了，找通知照常）
    if (
        body.docId is not None
        and _chat_for(session, user.id, body.docId) is None
        and not _public_document(session, body.docId)
    ):
        # 带代码的 422：小程序把不带代码的 404 当成"AI 没开"，会整页回首页
        raise _refuse(
            422, "CHAT_DOC_UNAVAILABLE", "这份通知现在看不了，换一份通知，或用“找通知”找别的。"
        )
    _check_wechat(session, user, body.skill, text, stamp)
    try:
        answer = chat_find.run(session, text, today=service.usage_day(stamp))
    except Exception as exc:  # noqa: BLE001 - 出错只记类型，异常信息里可能带用户打的字
        session.rollback()
        log.warning("找通知出错：%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="服务出错了，请稍后再试") from None
    # 先锁用户、再锁对话（同删对话的锁顺序）；锁里再按请求编号找一次，两个一样的请求同时到时
    # 后到的拿到先建成的那一问
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    found = _turn_by_key(session, user.id, key, stamp)
    if found is not None:
        session.rollback()  # 放开用户锁再拼回答
        return _replay(session, *found, body, response, stamp)
    chat = _chat_for(session, user.id, body.docId, lock=True)
    if chat is None:
        chat = AIChat(user_id=user.id, doc_key=body.docId or 0, created_at=stamp, updated_at=stamp)
        session.add(chat)
        session.flush()
    seq = (
        session.scalar(select(func.max(AIChatTurn.seq)).where(AIChatTurn.chat_id == chat.id)) or 0
    ) + 1
    turn = AIChatTurn(
        chat_id=chat.id,
        seq=seq,
        skill=body.skill,
        question=text,
        answer_json=answer,
        idempotency_key=key,
        created_at=stamp,
        expires_at=stamp + chat_store.TURN_RETENTION,
    )
    chat.updated_at = stamp
    session.add(turn)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        found = _turn_by_key(session, user.id, key, stamp)
        if found is None:
            raise _refuse(409, "CHAT_KEY_REUSED", "请求编号冲突，请重新发送") from None
        return _replay(session, *found, body, response, stamp)
    request.state.audit = {"action": "chat_turn_create", "turn_id": turn.id, "skill": turn.skill}
    return {"chatId": chat.id, "turn": _turn_out(session, turn, None, stamp=stamp)}


@router.get("/chat", dependencies=[Depends(_visible)])
def get_chat(
    session: SessionDep,
    user: UserDep,
    doc_id: Annotated[int | None, Query(alias="docId", ge=1, le=2**31 - 1)] = None,
    before: Annotated[int | None, Query(ge=1, le=10**9)] = None,
) -> dict:
    """这份通知的对话（按现在的合并组找）；不带 docId 是不带通知的那条。没有时 chat 为 null。"""
    _authorize(user)
    stamp = _stamp()
    chat = _chat_for(session, user.id, doc_id)
    return {"chat": _chat_out(session, chat, stamp=stamp, before=before) if chat else None}


@router.get("/chat/examples", dependencies=[Depends(_visible)])
def chat_examples(session: SessionDep, user: UserDep) -> dict:
    """找通知的可点示例：按收录现取，每句都真查得到；一句都没有就是空的。"""
    _authorize(user)
    return {"examples": chat_find.examples(session, now=_stamp())}


def _owned_chat(session: Session, user: User | None, chat_id: int, *, lock: bool = False):
    if user is None:
        raise HTTPException(status_code=404, detail="没有找到对话")
    stmt = select(AIChat).where(AIChat.id == chat_id, AIChat.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    chat = session.scalar(stmt)
    if chat is None:
        raise HTTPException(status_code=404, detail="没有找到对话")
    return chat


@router.get("/chat/turns/{turn_id}")
def get_turn(turn_id: int, session: SessionDep, user: KnownUserDep) -> dict:
    """一问的现状（小程序轮询问这份通知的进度）。只认本人。"""
    stamp = _stamp()
    row = (
        session.execute(
            select(AIChatTurn, AIChat)
            .join(AIChat, AIChat.id == AIChatTurn.chat_id)
            .where(
                AIChatTurn.id == turn_id,
                AIChat.user_id == user.id,
                chat_store.live_turns(stamp),
            )
        ).first()
        if user is not None
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到这一问")
    turn, chat = row
    task = _tasks_of(session, [turn]).get(turn.task_id)
    return {"chatId": chat.id, "turn": _turn_out(session, turn, task, stamp=stamp)}


@router.get("/chats")
def list_chats(session: SessionDep, user: KnownUserDep) -> dict:
    """我的对话（"我的 AI 记录"用）：现取现算，不另存。只认本人，不看开关和协议版本。"""
    if user is None:
        return {"items": [], "total": 0}
    stamp = _stamp()
    busy = case((AdvisorTask.status.in_(chat_store.IN_FLIGHT), 1), else_=0)
    rows = session.execute(
        select(
            AIChat,
            func.count(AIChatTurn.id),
            func.max(AIChatTurn.created_at),
            func.coalesce(func.sum(busy), 0),
        )
        .join(AIChatTurn, AIChatTurn.chat_id == AIChat.id)
        .outerjoin(AdvisorTask, AdvisorTask.id == AIChatTurn.task_id)
        .where(AIChat.user_id == user.id, chat_store.live_turns(stamp))
        .group_by(AIChat.id)
        .order_by(func.max(AIChatTurn.created_at).desc(), AIChat.id.desc())
    ).all()
    shown = rows[:LIST_LIMIT]
    titles = advisor_api._document_titles(
        session, [chat.doc_key for chat, *_ in shown if chat.doc_key]
    )
    items = []
    for chat, count, last_at, in_flight in shown:
        item = {
            "id": chat.id,
            **_doc_out(session, chat, titles),
            "turns": int(count),
            "lastAt": _iso(last_at),
            "busy": bool(in_flight),
        }
        if chat.doc_key == 0:
            last = session.scalar(
                select(AIChatTurn.question)
                .where(AIChatTurn.chat_id == chat.id, chat_store.live_turns(stamp))
                .order_by(AIChatTurn.seq.desc())
                .limit(1)
            )
            if last is not None and len(last) > LAST_QUESTION_CHARS:
                last = last[:LAST_QUESTION_CHARS] + "…"
            item["lastQuestion"] = last
        items.append(item)
    return {"items": items, "total": len(rows)}


@router.get("/chats/{chat_id}")
def open_chat(
    chat_id: int,
    session: SessionDep,
    user: KnownUserDep,
    before: Annotated[int | None, Query(ge=1, le=10**9)] = None,
) -> dict:
    """按对话编号打开（"我的记录"、AI 首页"我的 AI 记录"用），只能看自己的。"""
    chat = _owned_chat(session, user, chat_id)
    out = _chat_out(session, chat, stamp=_stamp(), before=before)
    if out is None:
        raise HTTPException(status_code=404, detail="没有找到对话")
    return {"chat": out}


@router.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: int, request: Request, session: SessionDep, user: KnownUserDep) -> None:
    """删整条对话，当场删完：处理中的那一问也删，占的次数退回（第五节）。只认本人。"""
    if user is None:
        raise HTTPException(status_code=404, detail="没有找到对话")
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    chat = _owned_chat(session, user, chat_id, lock=True)
    chat_store.delete_chats(session, [chat.id])
    session.commit()
    request.state.audit = {"action": "chat_delete", "chat_id": chat_id}
