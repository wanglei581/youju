"""第一批顾问技能包 API。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from youju import advisor_tasks as service
from youju.ai import interpret
from youju.ai.clause import Company
from youju.ai.skills import AI_KINDS, get_skill
from youju.api import v1
from youju.api.me import UserDep, request_openid
from youju.backup import BackupError
from youju.config import get_settings
from youju.db import get_session
from youju.models import AdvisorTask, AdvisorUsage, Document, Source, User
from youju.takedown import available_condition

router = APIRouter(prefix="/api/v1/advisor", tags=["顾问"])
SessionDep = Annotated[Session, Depends(get_session)]


async def _enabled(request: Request) -> None:
    settings = get_settings()
    if not settings.advisor_enabled and not getattr(settings, "ai_advisor_enabled", False):
        raise HTTPException(status_code=404, detail="没有找到接口")
    # 任务接口先由各自的归属检查确认身份，再按任务类型授权，避免泄露他人任务。
    if "task_id" in request.path_params:
        return
    kind = None
    if request.method == "POST" and request.url.path.endswith("/tasks"):
        _body_size(request)
        try:
            raw = await request.json()
            kind = raw.get("kind") if isinstance(raw, dict) else None
        except ValueError:
            pass  # 格式错误仍交给原有的请求校验。
        else:
            if kind not in ("compare", "track"):
                try:
                    openid = request_openid(request)
                except HTTPException:
                    openid = None
                if not getattr(
                    settings, "ai_advisor_enabled", False
                ) or openid not in interpret.openids(settings):
                    # 先于 TaskIn 的 Literal 校验，固定提示不含未开放类型或用户输入。
                    if settings.advisor_enabled:
                        raise HTTPException(status_code=422, detail="kind 只能是 compare 或 track")
                    raise HTTPException(status_code=404, detail="没有找到接口")
    if kind in ("compare", "track") and not settings.advisor_enabled:
        raise HTTPException(status_code=404, detail="没有找到接口")
    if kind in AI_KINDS or (kind is None and not settings.advisor_enabled):
        # 在协议校验之前隐藏 AI 接口，未同意协议的非白名单用户也只会收到 404。
        try:
            openid = request_openid(request)
        except HTTPException:
            raise HTTPException(status_code=404, detail="没有找到接口") from None
        if not getattr(settings, "ai_advisor_enabled", False) or openid not in interpret.openids(
            settings
        ):
            raise HTTPException(status_code=404, detail="没有找到接口")


def _body_size(request: Request) -> None:
    raw = request.headers.get("content-length")
    if raw:
        try:
            if int(raw) > service.MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="请求内容不能超过 100 KiB")
        except ValueError:
            raise HTTPException(status_code=413, detail="请求内容大小无效") from None


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["compare", "track", "interpret", "clause"]
    # 额度超限按 429 返回，不能由 Pydantic 的 422 max_length 提前截断。
    rows: list[Any] | None = Field(default=None, min_length=1)
    doc_id: int | None = Field(default=None, gt=0, strict=True)

    company: Company | None = None

    @model_validator(mode="after")
    def validate_input(self) -> TaskIn:
        if self.kind in AI_KINDS:
            if self.doc_id is None or "rows" in self.model_fields_set:
                raise ValueError(
                    "解读仅接受 doc_id"
                    if self.kind == "interpret"
                    else "条款对照必须提供 doc_id，不能提供 rows"
                )
            if self.kind == "interpret" and "company" in self.model_fields_set:
                raise ValueError("解读仅接受 doc_id")
        elif (
            self.rows is None
            or "doc_id" in self.model_fields_set
            or "company" in self.model_fields_set
        ):
            raise ValueError("数据工具必须提供 rows")
        return self

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, rows: list[Any] | None, info) -> list[Any] | None:
        if rows is None:
            return None
        kind = info.data.get("kind")
        if kind == "compare" and any(not isinstance(row, str) for row in rows):
            raise ValueError("名单比对的 rows 必须是企业名称或信用代码字符串")
        if kind == "track" and any(not isinstance(row, dict) for row in rows):
            raise ValueError("跟进表的 rows 必须是客户画像对象")
        return rows


def _stamp() -> datetime:
    return v1.now().astimezone(UTC)


def _authorize(kind: str, user: User) -> None:
    settings = get_settings()
    allowed = interpret.enabled(settings, user) if kind in AI_KINDS else settings.advisor_enabled
    if not allowed:
        raise HTTPException(status_code=404, detail="没有找到接口")


def _document_titles(session: Session, doc_ids: list[int]) -> dict[int, str]:
    if not doc_ids:
        return {}
    return dict(
        session.execute(
            select(Document.id, Document.title)
            .join(Source, Source.id == Document.source_id)
            .where(Document.id.in_(doc_ids), v1.public_condition())
        ).all()
    )


def _document_out(doc_id: int | None, titles: dict[int, str]) -> dict:
    # 编号与标题同时隐藏，不能暴露已下架或非公开通知的元数据。
    return {
        "docId": doc_id if doc_id in titles else None,
        "docTitle": titles.get(doc_id),
    }


def _out(task: AdvisorTask, session: Session, *, include_result: bool = True) -> dict:
    result = service.task_dict(
        task, include_result=include_result or task.kind not in AI_KINDS, stamp=_stamp()
    )
    if task.kind in AI_KINDS:
        doc_id = (task.snapshot or {}).get("doc_id") or (task.input_json or {}).get("doc_id")
        result.update(_document_out(doc_id, _document_titles(session, [doc_id] if doc_id else [])))
    if task.kind in AI_KINDS and not include_result:
        return {**result, "hasResult": bool(task.result_json)}
    if task.kind in AI_KINDS and task.result_json:
        if task.expires_at is not None and task.expires_at <= _stamp():
            result["result"] = None
            result["error"] = "结果已过期，请重新提交"
        elif task.cache_key:
            try:
                _, _, key = interpret.prepare(
                    session, task.snapshot["doc_id"], task.model, skill=get_skill(task.kind)
                )
                if key != task.cache_key:
                    raise interpret.InterpretRefusal("正文或核对内容已变化，请重新提交")
            except interpret.InterpretRefusal as exc:
                result["result"] = None
                result["error"] = str(exc)
    return result


def _owned(session: Session, user: User, task_id: int, *, lock: bool = False) -> AdvisorTask:
    stmt = select(AdvisorTask).where(AdvisorTask.id == task_id, AdvisorTask.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    task = session.scalar(stmt)
    if task is None:
        raise HTTPException(status_code=404, detail="没有找到顾问任务")
    _authorize(task.kind, user)
    return task


@router.get("/capabilities")
def capabilities(session: SessionDep, user: UserDep) -> dict:
    settings = get_settings()
    day = service.usage_day(_stamp())
    counts = dict(
        session.execute(
            select(AdvisorUsage.kind, AdvisorUsage.used_count).where(
                AdvisorUsage.user_id == user.id, AdvisorUsage.day == day
            )
        ).all()
    )
    data_enabled = settings.advisor_enabled
    ai_enabled = interpret.enabled(settings, user)
    return {
        "data_tools": {
            "enabled": data_enabled,
            "features": ["compare", "track"] if data_enabled else [],
            "remaining": {
                k: max(0, 1 - counts.get(k, 0)) if data_enabled else 0 for k in ("compare", "track")
            },
        },
        "ai_advisor": {
            "enabled": True,
            "features": list(AI_KINDS),
            "disclosure": interpret.disclosure(settings),
            "remaining": {k: max(0, 5 - counts.get(k, 0)) for k in AI_KINDS},
        }
        if ai_enabled
        else {"enabled": False, "features": [], "remaining": {}},
    }


@router.post(
    "/tasks",
    status_code=202,
    dependencies=[Depends(_enabled), Depends(_body_size)],
)
def create_task(
    body: TaskIn,
    request: Request,
    session: SessionDep,
    user: UserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    _authorize(body.kind, user)
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise HTTPException(
            status_code=422, detail="请提供 Idempotency-Key 请求头（不超过 128 字符）"
        )
    key = idempotency_key.strip()
    # 锁住用户，串行化额度检查和任务创建；重复 key 优先返回原任务，不再检查新 body。
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    input_json = {"doc_id": body.doc_id} if body.kind in AI_KINDS else None
    if body.kind == "clause":
        input_json["company"] = body.company.model_dump(mode="json") if body.company else {}
    existing = session.scalar(
        select(AdvisorTask).where(
            AdvisorTask.user_id == user.id, AdvisorTask.idempotency_key == key
        )
    )
    if existing is not None:
        _authorize(existing.kind, user)
        if (body.kind in AI_KINDS or existing.kind in AI_KINDS) and (
            existing.kind != body.kind or existing.input_json != input_json
        ):
            raise HTTPException(status_code=409, detail="同一幂等键不能用于不同输入")
        return _out(existing, session, include_result=False)
    if body.rows is not None and len(body.rows) > service.MAX_ROWS:
        label = "名单比对" if body.kind == "compare" else "跟进表"
        raise HTTPException(status_code=429, detail=f"{label}每次最多提交 50 行")
    stamp = _stamp()
    if not service.reserve_usage(session, user.id, body.kind, service.usage_day(stamp)):
        label = {
            "compare": "名单比对",
            "track": "跟进表",
            "interpret": "政策解读",
            "clause": "条款对照",
        }[body.kind]
        raise HTTPException(status_code=429, detail=f"{label}今天的免费额度已用完，请明天再试")
    task = AdvisorTask(
        user_id=user.id,
        kind=body.kind,
        status="queued",
        input_rows=body.rows,
        input_json=input_json,
        row_count=len(body.rows) if body.rows is not None else 0,
        idempotency_key=key,
        created_at=stamp,
        expires_at=stamp + service.FILE_RETENTION,
    )
    session.add(task)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(AdvisorTask).where(
                AdvisorTask.user_id == user.id, AdvisorTask.idempotency_key == key
            )
        )
        if existing is None:
            raise
        task = existing
    request.state.audit = {"action": "advisor_task_create", "task_id": task.id, "kind": task.kind}
    return _out(task, session, include_result=False)


@router.get("/tasks", dependencies=[Depends(_enabled)])
def list_tasks(session: SessionDep, user: UserDep) -> dict:
    settings = get_settings()
    kinds = ["compare", "track"] if settings.advisor_enabled else []
    if interpret.enabled(settings, user):
        kinds.extend(AI_KINDS)
    if not kinds:
        raise HTTPException(status_code=404, detail="没有找到接口")
    since = _stamp() - timedelta(days=30)
    rows = session.execute(
        select(
            AdvisorTask,
            (AdvisorTask.result_json.is_not(None) & (AdvisorTask.result_json != JSON.NULL)),
            # 只投影编号，避免加载整份正文快照及七天内的企业输入；快照在输入清理后仍保留。
            func.coalesce(
                AdvisorTask.snapshot["doc_id"].as_integer(),
                AdvisorTask.input_json["doc_id"].as_integer(),
            ),
        )
        .options(
            defer(AdvisorTask.result_json),
            defer(AdvisorTask.snapshot),
            defer(AdvisorTask.input_json),
        )
        .where(
            AdvisorTask.user_id == user.id,
            AdvisorTask.created_at >= since,
            AdvisorTask.kind.in_(kinds),
        )
        .order_by(AdvisorTask.created_at.desc(), AdvisorTask.id.desc())
    ).all()
    titles = _document_titles(
        session, list({doc_id for task, _, doc_id in rows if task.kind in AI_KINDS and doc_id})
    )
    return {
        "items": [
            {
                **service.task_dict(task, include_result=False),
                "hasResult": bool(has_result),
                **_document_out(doc_id, titles),
            }
            if task.kind in AI_KINDS
            else service.task_dict(task)
            for task, has_result, doc_id in rows
        ]
    }


@router.get("/tasks/{task_id}/download", dependencies=[Depends(_enabled)])
def download_task(task_id: int, session: SessionDep, user: UserDep) -> dict:
    task = _owned(session, user, task_id)
    if (
        task.status != "done"
        or not task.result_file_id
        or (task.expires_at is not None and task.expires_at <= _stamp())
    ):
        raise HTTPException(status_code=410, detail="结果文件已过期或已删除")
    # 已生成的 Excel 也要在每次取下载地址时复核。
    hidden = select(Document.id).where(~available_condition())
    legacy_check = task.result_document_ids is None and session.scalar(hidden.limit(1)) is not None
    if (
        task.result_document_ids is not None
        and session.scalar(hidden.where(Document.id.in_(task.result_document_ids)).limit(1))
        is not None
    ):
        raise HTTPException(status_code=410, detail="结果包含已下架文件，请重新生成")
    settings = get_settings()
    cloud_env = getattr(settings, "cloud_env", None)
    if not cloud_env:
        raise HTTPException(status_code=503, detail="对象存储暂不可用")
    try:
        with httpx.Client(trust_env=False) as client:
            url = service.download_url(client, cloud_env, task.result_file_id)
            if url and legacy_check:
                try:
                    ids = service.legacy_result_document_ids(session, client, url)
                except Exception:  # noqa: BLE001 - 旧文件损坏不能绕过核验
                    raise HTTPException(
                        status_code=503, detail="结果文件暂时无法核验，请重新生成"
                    ) from None
                task.result_document_ids = ids
                session.commit()
                if session.scalar(hidden.where(Document.id.in_(ids)).limit(1)) is not None:
                    raise HTTPException(status_code=410, detail="结果包含已下架文件，请重新生成")
    except (BackupError, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="下载地址暂时无法生成，请稍后重试") from None
    if not url:
        raise HTTPException(status_code=410, detail="结果文件已过期或已删除")
    # 统一个人接口的驼峰字段命名；不要同时保留 snake_case 别名。
    return {"url": url, "expiresIn": 600}


@router.get("/tasks/{task_id}", dependencies=[Depends(_enabled)])
def get_task(task_id: int, session: SessionDep, user: UserDep) -> dict:
    return _out(_owned(session, user, task_id), session)


@router.post("/tasks/{task_id}/cancel", dependencies=[Depends(_enabled)])
def cancel_task(task_id: int, request: Request, session: SessionDep, user: UserDep) -> dict:
    task = _owned(session, user, task_id, lock=True)
    if task.status == "queued":
        task.status = "cancelled"
        task.finished_at = _stamp()
        task.error = "用户已取消"
        service.release_usage(session, task)
        session.commit()
    elif task.status != "cancelled":
        raise HTTPException(status_code=409, detail="任务已开始处理，不能取消")
    request.state.audit = {"action": "advisor_task_cancel", "task_id": task.id}
    return _out(task, session, include_result=False)


@router.delete("/tasks/{task_id}", status_code=204, dependencies=[Depends(_enabled)])
def delete_task(task_id: int, request: Request, session: SessionDep, user: UserDep) -> None:
    task = _owned(session, user, task_id, lock=True)
    if task.status == "running":
        raise HTTPException(status_code=409, detail="任务正在处理，暂时不能删除")
    if task.status == "queued":
        # 排队中的任务先取消（退回额度）再删除；行已锁住，后台取不到它，也不会有结果文件
        service.release_usage(session, task)
    file_ids = [task.result_file_id] if task.result_file_id else []
    settings = get_settings()
    if file_ids:
        with httpx.Client(trust_env=False) as client:
            service._delete_and_record(session, file_ids, settings=settings, client=client)
    session.delete(task)
    session.commit()
    request.state.audit = {"action": "advisor_task_delete", "task_id": task_id}
