"""第一批顾问技能包 API。"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from youju import advisor_tasks as service
from youju import boost, payments
from youju.agreements import TOOLS_AGREEMENT_VERSION, agreement_at_least
from youju.ai import interpret
from youju.ai.clause import Company
from youju.ai.skills import AI_KINDS, get_skill
from youju.api import v1
from youju.api.me import OUTDATED, KnownUserDep, UserDep, request_openid
from youju.backup import BackupError
from youju.compare import CompareInputError, read_customer_file, validate_xlsx
from youju.config import get_settings
from youju.db import get_session
from youju.models import AdvisorTask, Document, Source, User
from youju.takedown import available_condition
from youju.track import read_customer_profiles, validate_customer_rows

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
        await _body_size(request)
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
                if not interpret.enabled_for_openid(settings, openid):
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
        if not interpret.enabled_for_openid(settings, openid):
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

    kind: Literal["compare", "track", "interpret", "clause"]
    # 额度超限按 429 返回，不能由 Pydantic 的 422 max_length 提前截断。
    rows: list[Any] | None = Field(default=None, min_length=1)
    file: FileIn | None = None
    doc_id: int | None = Field(default=None, gt=0, strict=True)

    company: Company | None = None

    @model_validator(mode="after")
    def validate_input(self) -> TaskIn:
        if self.kind in AI_KINDS:
            if self.doc_id is None or {"rows", "file"} & self.model_fields_set:
                raise ValueError(
                    "解读仅接受 doc_id"
                    if self.kind == "interpret"
                    else "条款对照必须提供 doc_id，不能提供 rows 或 file"
                )
            if self.kind == "interpret" and "company" in self.model_fields_set:
                raise ValueError("解读仅接受 doc_id")
        elif {"doc_id", "company"} & self.model_fields_set:
            raise ValueError("数据工具不接受 doc_id 或 company")
        elif (self.rows is None) == (self.file is None):
            raise ValueError("请只提供 rows 或 file 中的一项")
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


def _file_rows(body: TaskIn) -> list[Any]:
    upload = body.file
    if upload is None:
        if body.kind == "track":
            try:
                validate_customer_rows(body.rows)
            except CompareInputError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
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
                    "city": row.city,
                    **({"district": row.district} if row.district else {}),
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
    # 同意工具使用说明时已核对过；同意记录只会往新版本走，这里再查一次，不靠那一处
    if not agreement_at_least(user.agreement_version, TOOLS_AGREEMENT_VERSION):
        raise HTTPException(status_code=403, detail=OUTDATED)


@router.get("/status", dependencies=[Depends(_enabled)])
def status(session: SessionDep, user: KnownUserDep) -> dict:
    settings = get_settings()
    if not settings.advisor_enabled:
        raise HTTPException(status_code=404, detail="没有找到接口")
    stamp = _stamp()
    day = service.usage_day(stamp)
    usage = service.usage_counts(session, user.id, day) if user else {}
    ticket = payments.current_pass(session, user.id, stamp) if user else None
    # quota 仍是每月免费次数（体验期间不占用，确认页要写本月还剩几次）；
    # 体验期自己的每天次数在 trial 里，maxRows 按现在能提交的上限
    trial = boost.trial_status(session, settings, user, stamp, usage)
    wide = ticket is not None or bool(trial and trial["active"])
    return {
        "enabled": True,
        "pass": payments.pass_status(session, user.id, stamp)
        if user
        else {"active": False, "expiresAt": None},
        "quota": {
            kind: {
                "used": usage.get(kind, 0),
                "limit": boost.free_limit(settings, kind, day),
                "maxRows": 500 if wide else service.MAX_ROWS,
            }
            for kind in ("compare", "track")
        },
        "trial": trial,
        "season": boost.season_status(settings, day),
    }


@router.post("/trial", dependencies=[Depends(_enabled)])
def start_trial(request: Request, session: SessionDep, user: UserDep) -> dict:
    """开始加量体验：用户在确认页看完规则点"开始体验"。重复点返回同一段体验，不会重新起算。

    开始体验不提交客户信息，不要求先同意工具使用说明；要求同意过写进体验记录的那一版用户协议和
    隐私保护指引（TOOLS_AGREEMENT_VERSION）：过渡期里只同意过旧版的返回 403，小程序清掉本机的
    同意记录，下次用需要登录的功能时请他重新同意。"""
    settings = get_settings()
    if not boost.trial_enabled(settings):
        raise HTTPException(status_code=404, detail="没有找到接口")
    if not agreement_at_least(user.agreement_version, TOOLS_AGREEMENT_VERSION):
        raise HTTPException(status_code=403, detail=OUTDATED)
    # 和提交任务、注销同序：先锁用户行，再查体验记录和使用期（连接是 READ COMMITTED，
    # 锁后的查询读得到等锁期间别的请求刚提交的体验和使用期）。开始时间在锁后取：
    # 连点两次时，后拿到锁的请求不会早于先建成的那段，返回同一段而不是"已经用过"
    session.execute(select(User.id).where(User.id == user.id).with_for_update()).one()
    stamp = _stamp()
    try:
        boost.start(session, user, stamp)
        session.commit()
    except boost.TrialError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from None
    except IntegrityError:
        # 唯一约束兜底（另一个请求已经建成）：返回建成的那段
        session.rollback()
        stamp = _stamp()
        if boost.active_trial(session, user.id, stamp) is None:
            raise
    request.state.audit = {"action": "boost_trial_start"}
    return boost.trial_status(session, settings, user, stamp)


@router.post("/preview", dependencies=[Depends(_enabled), Depends(_body_size)])
def preview(body: TaskIn, user: UserDep) -> dict:
    _authorize(body.kind, user)
    if body.kind in AI_KINDS:
        raise HTTPException(status_code=422, detail="预览只支持 compare 或 track")
    _upload_consent(user)
    return {"rowCount": len(_file_rows(body))}


def _stamp() -> datetime:
    return v1.now().astimezone(UTC)


def _authorize(kind: str, user: User) -> None:
    settings = get_settings()
    if (
        kind in AI_KINDS
        and interpret.enabled_for_openid(settings, user.openid)
        and not interpret.ai_agreed(settings, user)
    ):
        raise HTTPException(status_code=403, detail="AI 条款已更新，请更新小程序并重新阅读同意协议")
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
        if task.status == "queued":
            result.update(
                service.queue_info(
                    session, task, getattr(get_settings(), "ai_advisor_concurrency", 3)
                )
            )
    if task.kind in AI_KINDS and not include_result:
        return {**result, "hasResult": bool(task.result_json)}
    if task.kind in AI_KINDS and task.result_json:
        if task.expires_at is not None and task.expires_at <= _stamp():
            result["result"] = None
            result["error"] = "结果已过期，请重新提交"
        elif task.cache_key:
            try:
                if not doc_id:
                    raise interpret.InterpretRefusal("结果已失效，请重新提交")
                current, verified, key = interpret.prepare(
                    session, doc_id, task.model, skill=get_skill(task.kind)
                )
                same_key = key == task.cache_key
                # D-073：只在正文变了时作废（原文或发给模型的脱敏后段落变了都算）
                if interpret.body_changed(task.snapshot, current, same_key=same_key):
                    raise interpret.InterpretRefusal(interpret.BODY_CHANGED)
                if not same_key:
                    # 核对、附件、模型或提示词版本变了：照常显示，"已核对"和附件提醒换成现在的
                    # （撤回的不再显示）；核对或附件变了才告诉用户解读之后有更新
                    result["result"] = interpret.refresh_verification(
                        result["result"], verified, current["attachments"]
                    )
                    result["outdated"] = interpret.verification_changed(task.snapshot, current)
            except interpret.InterpretRefusal as exc:
                result["result"] = None
                result["error"] = str(exc)
    return result


@contextmanager
def _submission_lock(session: Session, kind: str):
    try:
        if kind in AI_KINDS:
            with service.ai_cap_lock(session):
                yield
        else:
            yield
    except service.AdvisorBusy:
        raise HTTPException(status_code=503, detail="系统繁忙，请稍后重试") from None


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
def capabilities(request: Request, session: SessionDep, user: UserDep) -> dict:
    settings = get_settings()
    day = service.usage_day(_stamp())
    counts = service.usage_counts(session, user.id, day)
    data_enabled = settings.advisor_enabled
    ai_enabled = interpret.enabled(settings, user)
    return {
        "data_tools": {
            "enabled": data_enabled,
            "features": ["compare", "track"] if data_enabled else [],
            "remaining": {
                k: max(0, boost.free_limit(settings, k, day) - counts.get(k, 0))
                if data_enabled
                else 0
                for k in ("compare", "track")
            },
        },
        "ai_advisor": {
            "enabled": True,
            "features": list(AI_KINDS),
            "disclosure": interpret.disclosure(settings),
            "remaining": {k: max(0, 5 - counts.get(k, 0)) for k in AI_KINDS},
        }
        if ai_enabled
        else {
            "enabled": False,
            "features": [],
            "remaining": {},
            **(
                {"reason": "consent"}
                if interpret.enabled_for_openid(settings, user.openid)
                and not interpret.ai_agreed(settings, user)
                else {}
            ),
        },
        # 超出免费量时要不要提示买批量工具（D-058、D-070）；没开放支付时不出现任何购买字样
        "pay": payments.entry(session, settings, user, payments.client_of(request.headers)),
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
        # 不填城市时保留旧任务输入合同，部署后同一幂等键仍可返回原任务。
        if not input_json["company"].get("city"):
            input_json["company"].pop("city", None)
        if not input_json["company"].get("district"):
            input_json["company"].pop("district", None)
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
    if body.kind not in AI_KINDS:
        _upload_consent(user)
    stamp = _stamp()
    settings = get_settings()
    day = service.usage_day(stamp)
    data_kind = body.kind not in AI_KINDS
    ticket = payments.current_pass(session, user.id, stamp, lock=True) if data_kind else None
    # 加量体验期内每次 500 家以内，记在体验账本上，不占每月免费次数（boost.py）
    trial = boost.active_trial(session, user.id, stamp) if data_kind else None
    row_limit = 500 if ticket or trial else service.MAX_ROWS
    row_code = "PASS_ROW_LIMIT" if ticket else "TRIAL_ROW_LIMIT" if trial else "FREE_ROW_LIMIT"
    try:
        rows = _file_rows(body) if data_kind else None
    except HTTPException as exc:
        if exc.status_code == 422 and isinstance(exc.detail, str) and "最多 500 行" in exc.detail:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": row_code,
                    "message": f"每次最多提交 {row_limit} 行",
                    "maxRows": row_limit,
                },
            ) from None
        raise
    if rows is not None and len(rows) > row_limit:
        raise HTTPException(
            status_code=429,
            detail={
                "code": row_code,
                "message": f"每次最多提交 {row_limit} 行",
                "maxRows": row_limit,
            },
        )
    with _submission_lock(session, body.kind):
        if (
            body.kind in AI_KINDS
            and max(
                service.global_ai_usage(session, day),
                service.global_ai_calls(session, day),
            )
            >= settings.ai_advisor_daily_cap
        ):
            raise HTTPException(
                status_code=429,
                detail={"code": "AI_DAILY_CAP", "message": "今天的 AI 使用次数已满，请明天再试"},
            )
        if trial is not None:
            # 体验期先用体验当天的次数（两项合计）；用完了有使用期的用使用期，
            # 否则等第二天——不改用每月免费次数，体验结束后它们照常可用
            if service.reserve_usage(
                session, user.id, service.TRIAL_USAGE, day, limit=boost.TRIAL_DAILY
            ):
                ticket = None
            elif ticket is None:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "TRIAL_DAILY_LIMIT",
                        "message": f"今天的 {boost.TRIAL_DAILY} 次加量体验已用完，明天 0 点恢复；"
                        "本月免费次数不受影响，体验结束后照常可用",
                        "dailyLimit": boost.TRIAL_DAILY,
                    },
                )
            source = "pass" if ticket else "trial"
        else:
            # 有使用期也先用免费的量：50 行以内、本月免费次数还没用完的，记为免费，
            # 不算用过付费的量。付款页写的自助退款条件就是
            # "没有提交过超过 50 家的名单、也没有超出每月免费次数"。
            fits_free = not data_kind or len(rows) <= service.MAX_ROWS
            reserved = fits_free and service.reserve_usage(
                session, user.id, body.kind, day, limit=boost.free_limit(settings, body.kind, day)
            )
            if reserved:
                ticket = None
            if ticket is None and not reserved:
                if data_kind:
                    # 不在售（开关关着、不对这个用户开放、下不了单）时不提购买（D-058、D-068）
                    selling = payments.selling_to(
                        settings, user.openid, payments.client_of(request.headers)
                    ) and payments.orderable(settings)
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "code": "FREE_MONTHLY_LIMIT",
                            # 这个小程序包现在能不能买（D-074）：
                            # 页面据此丢掉之前缓存的价格，不再提购买
                            "canBuy": selling,
                            "message": "本月免费次数已用完，下月 1 日恢复"
                            + (
                                "；也可以买批量工具，不限次数、每次 500 家以内，到期不扣钱"
                                if selling
                                else ""
                            ),
                        },
                    )
                label = "政策解读" if body.kind == "interpret" else "条款对照"
                raise HTTPException(
                    status_code=429, detail=f"{label}今天的免费额度已用完，请明天再试"
                )
            source = "pass" if ticket else "free"
        task = AdvisorTask(
            user_id=user.id,
            pass_id=ticket.id if ticket else None,
            usage_source=source,
            kind=body.kind,
            status="queued",
            input_rows=rows,
            input_json=input_json,
            row_count=len(rows) if rows is not None else 0,
            idempotency_key=key,
            created_at=stamp,
            expires_at=stamp + service.FILE_RETENTION,
        )
        session.add(task)
        try:
            session.flush()
            if ticket:
                ticket.last_task_id, ticket.last_used_at, ticket.last_task_completed_at = (
                    task.id,
                    stamp,
                    None,
                )
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
                **(
                    service.queue_info(
                        session, task, getattr(settings, "ai_advisor_concurrency", 3)
                    )
                    if task.status == "queued"
                    else {}
                ),
            }
            if task.kind in AI_KINDS
            else service.task_dict(task)
            for task, has_result, doc_id in rows
        ]
    }


def _result_link(session: Session, user: User, task_id: int) -> tuple[httpx.Client, str]:
    """核对这份结果还能不能取，返回（HTTP 客户端，对象存储的临时链接）。

    链接只在服务端用，不发给小程序。调用方负责关闭客户端。"""
    task = _owned(session, user, task_id)
    if (
        task.status != "done"
        or not task.result_file_id
        or (task.expires_at is not None and task.expires_at <= _stamp())
    ):
        raise HTTPException(status_code=410, detail="结果文件已过期或已删除")
    # 已生成的 Excel 也要在每次取文件时复核。
    hidden = select(Document.id).where(~available_condition())
    legacy_check = task.result_document_ids is None and session.scalar(hidden.limit(1)) is not None
    if (
        task.result_document_ids is not None
        and session.scalar(hidden.where(Document.id.in_(task.result_document_ids)).limit(1))
        is not None
    ):
        raise HTTPException(status_code=410, detail="结果包含已下架文件，请重新生成")
    cloud_env = service.result_env(get_settings(), task.result_file_id)
    if not cloud_env:
        raise HTTPException(status_code=503, detail="对象存储暂不可用")
    client = httpx.Client(trust_env=False)
    try:
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
        client.close()
        raise HTTPException(status_code=503, detail="结果文件暂时取不到，请稍后重试") from None
    except HTTPException:
        client.close()
        raise
    if not url:
        client.close()
        raise HTTPException(status_code=410, detail="结果文件已过期或已删除")
    return client, url


@router.get("/tasks/{task_id}/download", dependencies=[Depends(_enabled)])
def download_task(task_id: int, session: SessionDep, user: UserDep) -> dict:
    """结果文件有多大、分几段。小程序随后逐段取 /tasks/{id}/file?part=N，拼起来就是 Excel。"""
    client, url = _result_link(session, user, task_id)
    try:
        size = service.result_size(client, url)
    except (BackupError, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="结果文件暂时取不到，请稍后重试") from None
    finally:
        client.close()
    # 统一个人接口的驼峰字段命名；不要同时保留 snake_case 别名。
    return {
        "size": size,
        "parts": max(1, -(-size // service.RESULT_PART_SIZE)),
        "partSize": service.RESULT_PART_SIZE,
    }


@router.get("/tasks/{task_id}/file", dependencies=[Depends(_enabled)])
def task_file(
    task_id: int,
    session: SessionDep,
    user: UserDep,
    part: Annotated[int, Query(ge=0, le=64)] = 0,
) -> Response:
    """结果文件的一段（原始字节）。每次都重新核对归属、有效期和下架情况。"""
    client, url = _result_link(session, user, task_id)
    try:
        size = service.result_size(client, url)
        data = service.read_result_part(client, url, part, size)
    except ValueError:
        raise HTTPException(status_code=404, detail="没有这一段") from None
    except (BackupError, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="结果文件暂时取不到，请稍后重试") from None
    finally:
        client.close()
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}", dependencies=[Depends(_enabled)])
def get_task(task_id: int, session: SessionDep, user: UserDep) -> dict:
    task = _owned(session, user, task_id)
    return _out(task, session)


@router.post("/tasks/{task_id}/cancel", dependencies=[Depends(_enabled)])
def cancel_task(task_id: int, request: Request, session: SessionDep, user: UserDep) -> dict:
    task = _owned(session, user, task_id, lock=True)
    if task.status == "queued":
        task.status = "cancelled"
        task.finished_at = _stamp()
        task.error = "用户已取消"
        service.record_pass_completion(session, task)
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
