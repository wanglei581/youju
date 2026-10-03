"""第一批顾问技能包 API。"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from youju import advisor_tasks as service
from youju.api import v1
from youju.api.me import KnownUserDep, UserDep
from youju.backup import BackupError
from youju.compare import CompareInputError, read_customer_file, validate_xlsx
from youju.config import get_settings
from youju.db import get_session
from youju.models import AdvisorTask, AdvisorUsage, User
from youju.takedown import available_condition
from youju.track import read_customer_profiles

router = APIRouter(prefix="/api/v1/advisor", tags=["顾问"])
SessionDep = Annotated[Session, Depends(get_session)]


def _enabled() -> None:
    if not get_settings().advisor_enabled:
        raise HTTPException(status_code=404, detail="没有找到接口")


async def _body_size(request: Request) -> None:
    raw = request.headers.get("content-length")
    if raw:
        try:
            if int(raw) > service.MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="请求内容不能超过 100 KiB")
        except ValueError:
            raise HTTPException(status_code=413, detail="请求内容大小无效") from None
    if len(await request.body()) > service.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="请求内容不能超过 100 KiB")


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    content_base64: str


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["compare", "track"]
    # 额度超限按 429 返回，不能由 Pydantic 的 422 max_length 提前截断。
    rows: list[Any] | None = Field(default=None, min_length=1)
    file: FileIn | None = None

    @model_validator(mode="after")
    def one_input(self):
        if (self.rows is None) == (self.file is None):
            raise ValueError("请只提供 rows 或 file 中的一项")
        return self

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, rows: list[Any], info) -> list[Any]:
        if rows is None:
            return rows
        kind = info.data.get("kind")
        if kind == "compare" and any(not isinstance(row, str) for row in rows):
            raise ValueError("名单比对的 rows 必须是企业名称或信用代码字符串")
        if kind == "track" and any(not isinstance(row, dict) for row in rows):
            raise ValueError("跟进表的 rows 必须是客户画像对象")
        return rows


def _file_rows(body: TaskIn) -> list[Any]:
    upload = body.file
    if upload is None:
        return body.rows
    if Path(upload.name).suffix.lower() not in (".xlsx", ".csv"):
        raise HTTPException(status_code=422, detail="只支持 .xlsx 和 .csv 文件")
    try:
        content = base64.b64decode(upload.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="文件编码无效，请重新选择文件") from None
    if len(content) > service.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="文件不能超过 60 KB")
    try:
        # 完整 ZIP 签名和工作簿结构都成立才按 XLSX 读；PK 开头的文本仍是 CSV。
        suffix = ".xlsx" if validate_xlsx(content) else ".csv"
        if suffix == ".csv" and (
            b"\x00" in content or any(value < 32 and value not in (9, 10, 13) for value in content)
        ):
            raise CompareInputError("文件内容不是 CSV 或 XLSX 表格")
        if body.kind == "compare":
            rows = read_customer_file("upload" + suffix, content=content)
        else:
            profiles = read_customer_profiles("upload" + suffix, content=content)
            rows = [
                {
                    "name": row.name,
                    "province": row.province,
                    "keywords": list(row.keywords),
                    "supports": list(row.supports),
                }
                for row in profiles
            ]
        if not rows or (body.kind == "compare" and not any(rows)):
            raise CompareInputError("文件里没有可提交的客户行")
        return rows
    except Exception as exc:  # noqa: BLE001 - 文件解析库的异常统一返回中文
        # CSV/XLSX 库的技术异常不暴露给用户；业务异常保留中文原因。
        detail = str(exc) if isinstance(exc, CompareInputError) else "无法读取表格，请检查文件内容"
        raise HTTPException(status_code=422, detail=detail) from None


def _upload_consent(user: User) -> None:
    if (
        user.advisor_agreement_at is None
        or user.advisor_agreement_version != service.ADVISOR_AGREEMENT_VERSION
    ):
        raise HTTPException(status_code=403, detail="请先阅读并同意工具使用说明")


@router.get("/status", dependencies=[Depends(_enabled)])
def status(session: SessionDep, user: KnownUserDep) -> dict:
    day = service.usage_day(_stamp())
    usage = (
        dict(
            session.execute(
                select(AdvisorUsage.kind, AdvisorUsage.used_count).where(
                    AdvisorUsage.user_id == user.id, AdvisorUsage.day == day
                )
            ).all()
        )
        if user
        else {}
    )
    return {
        "enabled": True,
        "quota": {
            kind: {"used": usage.get(kind, 0), "limit": 1, "maxRows": service.MAX_ROWS}
            for kind in ("compare", "track")
        },
    }


@router.post("/preview", dependencies=[Depends(_enabled), Depends(_body_size)])
def preview(body: TaskIn, user: UserDep) -> dict:
    _upload_consent(user)
    return {"rowCount": len(_file_rows(body))}


def _stamp() -> datetime:
    return v1.now().astimezone(UTC)


def _out(task: AdvisorTask) -> dict:
    return service.task_dict(task)


def _owned(session: Session, user: User, task_id: int) -> AdvisorTask:
    task = session.get(AdvisorTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="没有找到工具任务")
    return task


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
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise HTTPException(
            status_code=422, detail="请提供 Idempotency-Key 请求头（不超过 128 字符）"
        )
    key = idempotency_key.strip()
    # 锁住用户，串行化额度检查和任务创建；重复 key 优先返回原任务，不再检查新 body。
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    existing = session.scalar(
        select(AdvisorTask).where(
            AdvisorTask.user_id == user.id, AdvisorTask.idempotency_key == key
        )
    )
    if existing is not None:
        return _out(existing)
    if body.file is not None:
        _upload_consent(user)
    rows = _file_rows(body)
    if len(rows) > service.MAX_ROWS:
        label = "名单比对" if body.kind == "compare" else "跟进表"
        raise HTTPException(status_code=429, detail=f"{label}每次最多提交 50 行")
    stamp = _stamp()
    if not service.reserve_usage(session, user.id, body.kind, service.usage_day(stamp)):
        label = "名单比对" if body.kind == "compare" else "跟进表"
        raise HTTPException(status_code=429, detail=f"{label}今天的免费额度已用完，请明天再试")
    task = AdvisorTask(
        user_id=user.id,
        kind=body.kind,
        status="queued",
        input_rows=rows,
        row_count=len(rows),
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
    return _out(task)


@router.get("/tasks", dependencies=[Depends(_enabled)])
def list_tasks(session: SessionDep, user: UserDep) -> dict:
    since = _stamp() - timedelta(days=30)
    rows = session.scalars(
        select(AdvisorTask)
        .where(AdvisorTask.user_id == user.id, AdvisorTask.created_at >= since)
        .order_by(AdvisorTask.created_at.desc(), AdvisorTask.id.desc())
    ).all()
    return {"items": [_out(task) for task in rows]}


@router.get("/tasks/{task_id}/download", dependencies=[Depends(_enabled)])
def download_task(task_id: int, session: SessionDep, user: UserDep) -> dict:
    task = _owned(session, user, task_id)
    if (
        task.status != "done"
        or not task.result_file_id
        or (task.expires_at is not None and task.expires_at <= _stamp())
    ):
        raise HTTPException(status_code=410, detail="结果文件已过期或已删除")
    from youju.models import Document

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
    return _out(_owned(session, user, task_id))


@router.post("/tasks/{task_id}/cancel", dependencies=[Depends(_enabled)])
def cancel_task(task_id: int, request: Request, session: SessionDep, user: UserDep) -> dict:
    task = session.scalar(
        select(AdvisorTask)
        .where(AdvisorTask.id == task_id, AdvisorTask.user_id == user.id)
        .with_for_update()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="没有找到工具任务")
    if task.status == "queued":
        task.status = "cancelled"
        task.finished_at = _stamp()
        task.error = "用户已取消"
        service.release_usage(session, task)
        session.commit()
    elif task.status != "cancelled":
        raise HTTPException(status_code=409, detail="任务已开始处理，不能取消")
    request.state.audit = {"action": "advisor_task_cancel", "task_id": task.id}
    return _out(task)


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, request: Request, session: SessionDep, user: KnownUserDep) -> None:
    if user is None:
        raise HTTPException(status_code=404, detail="没有找到工具任务")
    task = session.scalar(
        select(AdvisorTask)
        .where(AdvisorTask.id == task_id, AdvisorTask.user_id == user.id)
        .with_for_update()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="没有找到工具任务")
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
