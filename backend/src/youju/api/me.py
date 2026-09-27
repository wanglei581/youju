"""小程序身份和个人数据接口。仅可信云托管入口可启用微信请求头。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from youju.api import v1
from youju.config import get_settings
from youju.db import get_session
from youju.dedup import canonical_ids
from youju.models import Document, Event, Feedback, Follow, Subscription, User

router = APIRouter(prefix="/api/v1", tags=["用户"])
SessionDep = Annotated[Session, Depends(get_session)]
_TZ = ZoneInfo("Asia/Shanghai")
_DATA_BYTES_LIMIT = 1000 * 1024
# 手机号，可带 +86 / 0086 / 86 前缀，中间可有空格或短横线
_MOBILE = re.compile(
    r"(?<![0-9])(?:(?:\+|00)?86[ -]?)?1[3-9][0-9][ -]?[0-9]{4}[ -]?[0-9]{4}(?![0-9])"
)
_ID_CARD = re.compile(r"(?<![A-Za-z0-9])[1-9][0-9]{16}[0-9Xx](?![A-Za-z0-9])")
# 邮箱只认英文字符：\w 会匹配中文，会把邮箱前面的中文一起隐去
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9-])"
)
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECKS = "10X98765432"


def request_openid(request: Request) -> str:
    """当前请求的微信用户：云托管时取平台注入的请求头（且须有 X-WX-SOURCE），本机开发取配置。"""
    settings = get_settings()
    if settings.trust_wx_headers:
        openid = (
            (request.headers.get("X-WX-OPENID") or request.headers.get("X-WX-FROM-OPENID"))
            if "X-WX-SOURCE" in request.headers
            else None
        )
    else:
        openid = settings.dev_openid
    if not openid or not openid.strip() or len(openid) > 128:
        raise HTTPException(status_code=401, detail="请在微信小程序里使用")
    return openid


def known_user(request: Request, session: SessionDep) -> User | None:
    """已有的用户记录；没有就返回 None，不新建——同意用户协议和隐私保护指引之前不保存 openid。"""
    user = session.scalar(select(User).where(User.openid == request_openid(request)))
    if (
        user is not None
        and user.agreement_at is not None
        and user.agreement_version == get_settings().agreement_version
        and v1.now() - user.last_visited_at > timedelta(hours=1)
    ):
        user.last_visited_at = v1.now()  # 最近访问时间一小时记一次，不必每次请求都写库
        session.commit()
    return user


def current_user(user: Annotated[User | None, Depends(known_user)]) -> User:
    """关注、订阅、纠错、埋点：必须已同意当前版本的用户协议和隐私保护指引。"""
    if user is None or user.agreement_at is None:
        raise HTTPException(status_code=403, detail="请先阅读并同意用户协议和隐私保护指引")
    if user.agreement_version != get_settings().agreement_version:
        raise HTTPException(
            status_code=403, detail="用户协议或隐私保护指引已更新，请重新阅读并同意"
        )
    return user


KnownUserDep = Annotated[User | None, Depends(known_user)]
UserDep = Annotated[User, Depends(current_user)]


class AgreementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=20)


class Conditions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str | None = Field(default=None, max_length=50)
    kind: str = "all"
    regions: str | None = Field(default=None, max_length=400)
    types: str | None = Field(default=None, max_length=400)
    status: str | None = Field(default=None, max_length=400)
    time: str = "近 30 天"
    depts: str | None = Field(default=None, max_length=400)


class FollowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    conditions: Conditions


class FollowNameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["newDocs", "deadline"]
    templateId: str = Field(min_length=1, max_length=128)
    followId: int | None = Field(default=None, gt=0)
    docId: int | None = Field(default=None, gt=0)


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    docId: int | None = Field(default=None, gt=0)
    content: str = Field(min_length=1, max_length=500)


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal[
        "search", "open_detail", "copy_link", "export", "lookup", "follow", "subscribe", "feedback"
    ]
    props: dict = Field(default_factory=dict)


def _conditions_stmt(session: Session, conditions: dict):
    return v1.search_statement(session, **(Conditions().model_dump() | conditions))


def new_statement(session: Session, follow: Follow, since):
    return _conditions_stmt(session, follow.conditions).where(Document.first_seen_at > since)


def new_count(session: Session, follow: Follow, since) -> int:
    return v1._count(session, new_statement(session, follow, since))


def new_documents(
    session: Session, follow: Follow, since, limit: int, offset: int
) -> list[tuple[Document, object]]:
    stmt = new_statement(session, follow, since)
    return [
        tuple(row) for row in session.execute(v1._ordered(stmt).limit(limit).offset(offset)).all()
    ]


def _follow_out(session: Session, follow: Follow) -> dict:
    since = follow.last_seen_at or follow.created_at
    return {
        "id": follow.id,
        "name": follow.name,
        "conditions": follow.conditions,
        "newCount": new_count(session, follow, since),
    }


def _owned_follow(session: Session, user: User, follow_id: int) -> Follow:
    row = session.get(Follow, follow_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="没有找到关注")
    return row


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(_TZ).isoformat() if value is not None else None


def _valid_id_card(match: re.Match[str]) -> str:
    number = match.group()
    try:
        date(int(number[6:10]), int(number[10:12]), int(number[12:14]))
    except ValueError:
        return number
    check = _ID_CHECKS[
        sum(int(digit) * weight for digit, weight in zip(number[:17], _ID_WEIGHTS, strict=True))
        % 11
    ]
    return "［已隐去］" if number[-1].upper() == check else number


def _mask_feedback(content: str) -> str:
    content = _ID_CARD.sub(_valid_id_card, content)
    content = _MOBILE.sub("［已隐去］", content)
    return _EMAIL.sub("［已隐去］", content)


@router.get("/me")
def profile(session: SessionDep, user: KnownUserDep) -> dict:
    if user is None:
        return {"agreed": None, "follows": 0, "subscriptions": {"newDocs": 0, "deadline": 0}}
    counts = dict(
        session.execute(
            select(Subscription.kind, func.count(Subscription.id))
            .where(Subscription.user_id == user.id, Subscription.status == "unused")
            .group_by(Subscription.kind)
        ).all()
    )
    return {
        "agreed": {"version": user.agreement_version, "at": user.agreement_at.isoformat()}
        if user.agreement_at
        else None,
        "follows": session.scalar(select(func.count(Follow.id)).where(Follow.user_id == user.id))
        or 0,
        "subscriptions": {
            "newDocs": counts.get("newDocs", 0),
            "deadline": counts.get("deadline", 0),
        },
    }


@router.post("/me/agreement")
def agreement(body: AgreementIn, request: Request, session: SessionDep) -> dict:
    """同意用户协议和隐私保护指引：从这时起才建立用户记录。只接受当前版本。"""
    if body.version != get_settings().agreement_version:
        raise HTTPException(status_code=422, detail="协议已更新，请刷新后重新阅读并同意")
    stamp = v1.now()
    row_id = session.scalar(
        insert(User)
        .values(
            openid=request_openid(request),
            last_visited_at=stamp,
            agreement_version=body.version,
            agreement_at=stamp,
        )
        .on_conflict_do_update(
            index_elements=[User.openid],
            set_={
                "last_visited_at": stamp,
                "agreement_version": body.version,
                "agreement_at": stamp,
            },
        )
        .returning(User.id)
    )
    session.commit()
    user = session.get(User, row_id)
    return {"version": user.agreement_version, "at": user.agreement_at.isoformat()}


@router.delete("/me", status_code=204)
def delete_account(session: SessionDep, user: KnownUserDep) -> None:
    """注销：删除用户及其关注、订阅、纠错、埋点；没有记录也返回成功。"""
    if user is not None:
        session.delete(user)
        session.commit()


def _titles(session: Session, doc_ids: list[int | None]) -> dict[int, str]:
    """文件编号 → 主文件标题（合并过的用主文件；文件已删除的不在结果里）。"""
    ids = [doc_id for doc_id in doc_ids if doc_id is not None]
    masters = canonical_ids(session, ids)
    titles = dict(
        session.execute(
            select(Document.id, Document.title).where(
                Document.id.in_({masters.get(doc_id, doc_id) for doc_id in ids})
            )
        ).all()
    )
    return {
        doc_id: titles[masters.get(doc_id, doc_id)]
        for doc_id in ids
        if masters.get(doc_id, doc_id) in titles
    }


@router.get("/me/data")
def personal_data(session: SessionDep, user: KnownUserDep) -> JSONResponse:
    settings = get_settings()
    result = {
        "user": {
            "openid": user.openid,
            "createdAt": _iso(user.created_at),
            "lastVisitedAt": _iso(user.last_visited_at),
            "agreed": {"version": user.agreement_version, "at": _iso(user.agreement_at)}
            if user.agreement_at is not None
            else None,
        }
        if user is not None
        else None,
        "follows": [],
        "subscriptions": [],
        "subscriptionsTotal": 0,
        "feedback": [],
        "feedbackTotal": 0,
        "events": [],
        "eventsTotal": 0,
        "retention": {
            "events": settings.retain_events_days,
            "feedback": settings.retain_feedback_days,
            "usedSubscriptions": settings.retain_used_subscriptions_days,
            "unusedSubscriptions": settings.retain_unused_subscriptions_days,
        },
        "exportedAt": _iso(v1.now()),
    }
    if user is None:
        return JSONResponse(result)

    follows = session.scalars(
        select(Follow)
        .where(Follow.user_id == user.id)
        .order_by(Follow.created_at.desc(), Follow.id.desc())
    ).all()
    result["follows"] = [
        {
            "id": row.id,
            "name": row.name,
            "conditions": row.conditions,
            "createdAt": _iso(row.created_at),
            "lastSeenAt": _iso(row.last_seen_at),
            "lastNotifiedAt": _iso(row.last_notified_at),
        }
        for row in follows
    ]

    result["subscriptionsTotal"] = (
        session.scalar(select(func.count(Subscription.id)).where(Subscription.user_id == user.id))
        or 0
    )
    subscriptions = session.scalars(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .limit(500)
    ).all()
    sub_titles = _titles(session, [row.document_id for row in subscriptions])
    result["subscriptions"] = [
        {
            "id": row.id,
            "kind": row.kind,
            "followId": row.follow_id,
            "docId": row.document_id,
            "docTitle": sub_titles.get(row.document_id),
            "status": row.status,
            "createdAt": _iso(row.created_at),
            "attemptedAt": _iso(row.attempted_at),
        }
        for row in subscriptions
    ]

    result["feedbackTotal"] = (
        session.scalar(select(func.count(Feedback.id)).where(Feedback.user_id == user.id)) or 0
    )
    feedback = session.scalars(
        select(Feedback)
        .where(Feedback.user_id == user.id)
        .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .limit(200)
    ).all()
    titles = _titles(session, [row.document_id for row in feedback])
    result["feedback"] = [
        {
            "id": row.id,
            "docId": row.document_id,
            "docTitle": titles.get(row.document_id),
            "content": row.content,
            "checkResult": row.check_result,
            "createdAt": _iso(row.created_at),
        }
        for row in feedback
    ]

    result["eventsTotal"] = (
        session.scalar(select(func.count(Event.id)).where(Event.user_id == user.id)) or 0
    )
    events = session.scalars(
        select(Event)
        .where(Event.user_id == user.id)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(500)
    ).all()
    result["events"] = [
        {"name": row.name, "props": row.props, "createdAt": _iso(row.created_at)} for row in events
    ]

    response = JSONResponse(result)
    # 历史数据中的标题或埋点属性可能很长；按时间保留较新的条目。
    for key in ("events", "subscriptions", "feedback"):
        if len(response.body) <= _DATA_BYTES_LIMIT:
            break
        items = result[key]
        low, high = 0, len(items)
        while low < high:
            count = (low + high + 1) // 2
            result[key] = items[:count]
            if len(JSONResponse(result).body) <= _DATA_BYTES_LIMIT:
                low = count
            else:
                high = count - 1
        result[key] = items[:low]
        response = JSONResponse(result)
    if len(response.body) > _DATA_BYTES_LIMIT:
        raise HTTPException(status_code=413, detail="个人数据过大，请联系客服处理")
    return response


@router.get("/me/follows")
def list_follows(session: SessionDep, user: UserDep) -> dict:
    follows = session.scalars(
        select(Follow).where(Follow.user_id == user.id).order_by(Follow.id)
    ).all()
    return {"items": [_follow_out(session, row) for row in follows]}


@router.post("/me/follows", status_code=201)
def create_follow(body: FollowIn, session: SessionDep, user: UserDep) -> dict:
    # 锁住用户行，多个同时新增的请求也不能绕过 20 条限制。
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    if (session.scalar(select(func.count(Follow.id)).where(Follow.user_id == user.id)) or 0) >= 20:
        raise HTTPException(status_code=422, detail="最多关注 20 个条件")
    conditions = body.conditions.model_dump(exclude_none=True)
    _conditions_stmt(session, conditions)
    row = Follow(
        user_id=user.id, name=body.name.strip(), conditions=conditions, created_at=v1.now()
    )
    if not row.name:
        raise HTTPException(status_code=422, detail="关注名称不能为空")
    session.add(row)
    session.commit()
    return _follow_out(session, row)


# 小程序的 wx.request、callContainer 不支持 PATCH，改名用 PUT
@router.put("/me/follows/{follow_id}")
def rename_follow(follow_id: int, body: FollowNameIn, session: SessionDep, user: UserDep) -> dict:
    name = body.name.strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(status_code=422, detail="关注名称须为 1–40 字")
    row = _owned_follow(session, user, follow_id)
    row.name = name
    session.commit()
    return _follow_out(session, row)


# 删除不要求同意当前版本：协议更新后没重新同意的用户也能删除自己的数据
@router.delete("/me/follows/{follow_id}", status_code=204)
def delete_follow(follow_id: int, session: SessionDep, user: KnownUserDep) -> None:
    if user is None:
        raise HTTPException(status_code=404, detail="没有找到关注")
    session.delete(_owned_follow(session, user, follow_id))
    session.commit()


@router.post("/me/follows/{follow_id}/seen")
def follow_seen(follow_id: int, session: SessionDep, user: UserDep) -> dict:
    row = _owned_follow(session, user, follow_id)
    row.last_seen_at = v1.now()
    session.commit()
    return {"newCount": 0}


@router.get("/me/follows/{follow_id}/new")
def follow_new(
    follow_id: int,
    session: SessionDep,
    user: UserDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> dict:
    row = _owned_follow(session, user, follow_id)
    since = row.last_seen_at or row.created_at
    rows = new_documents(session, row, since, limit, offset)
    return {"total": new_count(session, row, since), "items": v1._items(session, rows)}


def _subscription_out(row: Subscription) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "templateId": row.template_id,
        "followId": row.follow_id,
        "docId": row.document_id,
    }


@router.get("/me/subscriptions")
def list_subscriptions(session: SessionDep, user: UserDep) -> dict:
    rows = session.scalars(
        select(Subscription)
        .where(Subscription.user_id == user.id, Subscription.status == "unused")
        .order_by(Subscription.id)
    ).all()
    return {"items": [_subscription_out(row) for row in rows]}


@router.post("/me/subscriptions", status_code=201)
def create_subscription(body: SubscriptionIn, session: SessionDep, user: UserDep) -> dict:
    settings = get_settings()
    template = settings.template_newdocs if body.kind == "newDocs" else settings.template_deadline
    if template and body.templateId != template.get("id"):
        raise HTTPException(status_code=422, detail="模板编号不匹配")
    follow_id = document_id = None
    if body.kind == "newDocs":
        if body.followId is None or body.docId is not None:
            raise HTTPException(status_code=422, detail="新文件提醒需要 followId")
        follow_id = _owned_follow(session, user, body.followId).id
    else:
        if body.docId is None or body.followId is not None:
            raise HTTPException(status_code=422, detail="截止提醒需要 docId")
        document_id = canonical_ids(session, [body.docId]).get(body.docId, body.docId)
        stamp = v1.now()
        if session.get(Document, document_id) is None or not any(
            v1.deadline_open(d, stamp)
            for d in v1._approved(session, [document_id]).get(document_id, [])
        ):
            raise HTTPException(status_code=422, detail="没有有效的人工核对截止")
        # 锁住这个用户，同时点两次也只建一条（下面按"已有未用授权"去重）
        session.execute(select(User.id).where(User.id == user.id).with_for_update())
        # 同一文件已有还没发出的截止提醒：返回已有的，不再新建（否则会提醒两次）
        existing = session.scalar(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.kind == "deadline",
                Subscription.document_id == document_id,
                Subscription.status == "unused",
            )
        )
        if existing is not None:
            return _subscription_out(existing)
    row = Subscription(
        user_id=user.id,
        kind=body.kind,
        template_id=body.templateId,
        follow_id=follow_id,
        document_id=document_id,
        status="unused",
    )
    session.add(row)
    session.commit()
    return _subscription_out(row)


@router.delete("/me/subscriptions/{subscription_id}", status_code=204)
def delete_subscription(subscription_id: int, session: SessionDep, user: KnownUserDep) -> None:
    row = session.get(Subscription, subscription_id)
    if row is None or user is None or row.user_id != user.id or row.status != "unused":
        raise HTTPException(status_code=404, detail="没有找到可退订的授权")
    session.delete(row)
    session.commit()


def check_feedback(openid: str, content: str) -> bool:
    response = httpx.post(
        "http://api.weixin.qq.com/wxa/msg_sec_check",
        json={"openid": openid, "version": 2, "scene": 2, "content": content},
        timeout=5,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("errcode") != 0:
        raise ValueError("微信内容安全接口未成功")
    return result.get("result", {}).get("suggest") == "pass"


@router.post("/feedback", status_code=201)
def create_feedback(body: FeedbackIn, session: SessionDep, user: UserDep) -> dict:
    doc_id = body.docId
    if doc_id is not None:
        doc_id = canonical_ids(session, [doc_id]).get(doc_id, doc_id)
        if session.get(Document, doc_id) is None:
            raise HTTPException(status_code=422, detail="文件不存在")
    content = _mask_feedback(body.content)
    check_result = "unchecked"
    if get_settings().trust_wx_headers:
        try:
            passed = check_feedback(user.openid, content)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=503, detail="内容安全校验暂不可用") from exc
        if not passed:
            raise HTTPException(status_code=422, detail="内容可能不合规，请修改后再提交")
        check_result = "pass"
    row = Feedback(user_id=user.id, document_id=doc_id, content=content, check_result=check_result)
    session.add(row)
    session.commit()
    return {"id": row.id, "masked": content != body.content}


@router.delete("/me/feedback/{feedback_id}", status_code=204)
def delete_feedback(feedback_id: int, session: SessionDep, user: KnownUserDep) -> None:
    row = session.get(Feedback, feedback_id)
    if row is None or user is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="没有找到纠错")
    session.delete(row)
    session.commit()


_KINDS = {"all", "申报通知", "名单", "政策", "其他", "newDocs", "deadline"}
_FORMATS = {"xlsx", "docx", "pptx"}


@router.post("/events", status_code=202)
def create_event(body: EventIn, session: SessionDep, user: UserDep) -> dict:
    props = body.props
    if set(props) - {"kind", "count", "format"}:
        raise HTTPException(status_code=422, detail="埋点参数不支持")
    if "kind" in props and (not isinstance(props["kind"], str) or props["kind"] not in _KINDS):
        raise HTTPException(status_code=422, detail="埋点类型不支持")
    if "format" in props and (
        not isinstance(props["format"], str) or props["format"] not in _FORMATS
    ):
        raise HTTPException(status_code=422, detail="埋点格式不支持")
    if "count" in props and (type(props["count"]) is not int or not 0 <= props["count"] <= 10000):
        raise HTTPException(status_code=422, detail="埋点数量不支持")
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    stamp = v1.now()
    count = (
        session.scalar(
            select(func.count(Event.id)).where(
                Event.user_id == user.id, Event.created_at > stamp - timedelta(minutes=1)
            )
        )
        or 0
    )
    if count >= 60:
        return {"accepted": False}
    session.add(Event(user_id=user.id, name=body.name, props=props, created_at=stamp))
    session.commit()
    return {"accepted": True}
