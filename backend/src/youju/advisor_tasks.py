"""R1.5 工具任务队列、额度和结果文件处理。"""

from __future__ import annotations

import copy
import json
import logging
import tempfile
import time as monotonic_clock
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import httpx
from openpyxl import load_workbook
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from youju import access_log
from youju.ai import clause, interpret
from youju.ai.client import AIError, ChatClient
from youju.ai.skills import AI_KINDS, get_skill
from youju.ai.usage import record_usage
from youju.backup import BackupError, _open_api, upload
from youju.compare import CompareInputError, compare_lists, export_compare_excel
from youju.config import Settings, get_settings
from youju.models import AdvisorFileTrash, AdvisorTask, AdvisorUsage, Document, PayOrder, User
from youju.track import export_track_excel, track_clients

log = logging.getLogger("youju.advisor")
BEIJING = ZoneInfo("Asia/Shanghai")
MAX_ROWS = 50
MAX_BODY_BYTES = 100 * 1024
MAX_UPLOAD_BYTES = 60 * 1024
ADVISOR_AGREEMENT_VERSION = "advisor-2026-09-v1"
TASK_TIMEOUT = timedelta(minutes=5)
STALE_RUNNING = timedelta(minutes=10)
INPUT_RETENTION = timedelta(days=7)
FILE_RETENTION = timedelta(days=30)


def business_now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BEIJING)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, CompareInputError):
        return str(exc)
    if isinstance(exc, BackupError):
        return "结果文件暂时无法保存，请稍后重试"
    if isinstance(exc, TimeoutError):
        return "任务处理超时，请稍后重试"
    return "任务处理失败，请稍后重试"


def result_path(user: User, task_id: int, *, engine=None) -> str:
    """对象存储路径只使用程序生成的去标识用户编号。"""
    return f"advisor/out/{access_log.log_uid(user.openid, engine)}/{task_id}.xlsx"


def retained_input(task: AdvisorTask, stamp: datetime | None = None) -> dict | None:
    """清理任务尚未执行时也不展示或导出超过七天的企业输入。"""
    stamp = _utc(stamp or datetime.now(UTC))
    if task.created_at is not None and _utc(task.created_at) <= stamp - INPUT_RETENTION:
        return None
    return task.input_json


def task_dict(
    task: AdvisorTask, *, include_result: bool = True, stamp: datetime | None = None
) -> dict[str, Any]:
    """API 公共字段；不返回 input_rows，避免名单内容进入查询响应。"""
    result = {
        "id": task.id,
        "kind": task.kind,
        "status": task.status,
        "rowCount": task.row_count,
        "attempts": task.attempts,
        "createdAt": task.created_at,
        "startedAt": task.started_at,
        "finishedAt": task.finished_at,
        "expiresAt": task.expires_at,
        "error": task.error,
        # 付费任务（按次包）付款即执行，不能取消；前端据此隐藏取消按钮。
        "paid": task.pay_order_id is not None,
    }
    if include_result:
        result.update(
            {
                "resultRows": task.result_rows,
                "hasFile": bool(task.result_file_id),
            }
        )
    if task.kind in AI_KINDS and include_result:
        stored = (
            clause.with_company(task.result_json, retained_input(task, stamp))
            if task.kind == "clause" and task.result_json is not None
            else task.result_json
        )
        result["result"] = (
            {k: v for k, v in stored.items() if k != "dropped_reasons"}
            if task.result_json is not None
            else None
        )
    return result


def usage_day(value: datetime) -> date:
    """把任务时间转换为额度账本使用的北京时间日期。"""
    return _utc(value).astimezone(BEIJING).date()


def reserve_usage(session: Session, user_id: int, kind: str, day: date) -> bool:
    """在调用方已锁定用户行后占用一次额度。"""
    row = session.scalar(
        select(AdvisorUsage)
        .where(AdvisorUsage.user_id == user_id, AdvisorUsage.kind == kind, AdvisorUsage.day == day)
        .with_for_update()
    )
    if row is None:
        row = AdvisorUsage(user_id=user_id, kind=kind, day=day, used_count=0)
        session.add(row)
        session.flush()
    if kind in AI_KINDS:
        if row.used_count >= 5:
            return False
        row.used_count += 1
        return True
    if row.used_count >= 1:
        # 兼容 0010 上线前已被后台标成 failed 的旧任务：旧代码没有台账，
        # 首次遇到时补回一次；正常失败路径会在这里之前显式释放。
        old_failed = session.scalar(
            select(AdvisorTask.id)
            .where(
                AdvisorTask.user_id == user_id,
                AdvisorTask.kind == kind,
                AdvisorTask.pay_order_id.is_(None),
                AdvisorTask.status == "failed",
                AdvisorTask.created_at
                >= datetime.combine(day, datetime.min.time(), tzinfo=BEIJING).astimezone(UTC),
                AdvisorTask.created_at
                < datetime.combine(
                    day + timedelta(days=1), datetime.min.time(), tzinfo=BEIJING
                ).astimezone(UTC),
            )
            .limit(1)
        )
        active = session.scalar(
            select(AdvisorTask.id)
            .where(
                AdvisorTask.user_id == user_id,
                AdvisorTask.kind == kind,
                AdvisorTask.pay_order_id.is_(None),
                AdvisorTask.status.in_(("queued", "running", "done")),
                AdvisorTask.created_at
                >= datetime.combine(day, datetime.min.time(), tzinfo=BEIJING).astimezone(UTC),
                AdvisorTask.created_at
                < datetime.combine(
                    day + timedelta(days=1), datetime.min.time(), tzinfo=BEIJING
                ).astimezone(UTC),
            )
            .limit(1)
        )
        if old_failed is None or active is not None:
            return False
        row.used_count -= 1
    row.used_count += 1
    return True


def release_usage(session: Session, task: AdvisorTask) -> None:
    """释放一次已占用额度；重复释放安全。"""
    if task.pay_order_id is not None:
        return
    row = session.scalar(
        select(AdvisorUsage)
        .where(
            AdvisorUsage.user_id == task.user_id,
            AdvisorUsage.kind == task.kind,
            AdvisorUsage.day == usage_day(task.created_at),
        )
        .with_for_update()
    )
    if row is not None and row.used_count > 0:
        row.used_count -= 1


def _successful_ids(result: object, file_ids: list[str]) -> set[str]:
    """兼容旧测试替身返回数量；正式实现返回成功的文件 ID。"""
    if isinstance(result, (list, tuple, set, frozenset)):
        return {str(value) for value in result if value in file_ids}
    if isinstance(result, int):
        return set(file_ids[: max(0, min(result, len(file_ids)))])
    return set()


def _record_trash(session: Session, file_ids: list[str], user_hash: str | None = None) -> None:
    if not file_ids:
        return
    existing = set(
        session.scalars(
            select(AdvisorFileTrash.file_id).where(AdvisorFileTrash.file_id.in_(file_ids))
        )
    )
    for file_id in dict.fromkeys(file_ids):
        if file_id not in existing:
            session.add(AdvisorFileTrash(file_id=file_id, user_hash=user_hash))


def _delete_and_record(
    session: Session,
    file_ids: list[str],
    *,
    settings: Settings,
    client: httpx.Client | None,
    user_hash: str | None = None,
) -> set[str]:
    """删除对象存储文件；失败的 ID 留在待删表，调用方只清成功引用。"""
    if not file_ids:
        return set()
    cloud_env = getattr(settings, "cloud_env", None)
    successful: set[str] = set()
    if cloud_env:
        try:
            result = delete_result_files(client, cloud_env, file_ids)
            successful = _successful_ids(result, file_ids)
        except Exception:  # noqa: BLE001 - 存储删除失败必须留下待删记录
            successful = set()
    failed = [file_id for file_id in file_ids if file_id not in successful]
    _record_trash(session, failed, user_hash)
    return successful


def recover_stale(session: Session, stamp: datetime | None = None, condition=None) -> int:
    """实例重启后把运行超过十分钟的任务放回队列，不增加 attempts。"""
    stamp = _utc(stamp or datetime.now(UTC))
    stmt = update(AdvisorTask)
    if condition is not None:
        stmt = stmt.where(condition)
    count = session.execute(
        stmt.where(
            AdvisorTask.status == "running",
            AdvisorTask.started_at.is_not(None),
            AdvisorTask.started_at < stamp - STALE_RUNNING,
        ).values(status="queued", started_at=None, finished_at=None)
    ).rowcount
    session.commit()
    return count or 0


def _fail_revoked_interpret(session: Session, settings: Settings, stamp: datetime) -> None:
    """退回失去授权的任务额度；未超时的运行任务仍由领取它的进程负责。"""
    openids = (
        interpret.openids(settings) if getattr(settings, "ai_advisor_enabled", False) else set()
    )
    tasks = session.scalars(
        select(AdvisorTask)
        .where(
            AdvisorTask.kind.in_(AI_KINDS),
            AdvisorTask.user_id.not_in(select(User.id).where(User.openid.in_(openids))),
            (AdvisorTask.status == "queued")
            | (
                (AdvisorTask.status == "running")
                & AdvisorTask.started_at.is_not(None)
                & (AdvisorTask.started_at < stamp - STALE_RUNNING)
            ),
        )
        .order_by(AdvisorTask.id)
        .with_for_update(skip_locked=True)
    ).all()
    for task in tasks:
        task.status, task.error, task.finished_at = "failed", "功能未开放", stamp
        release_usage(session, task)
    session.commit()


def _run_engine(session: Session, task: AdvisorTask, output: Path) -> int:
    if not isinstance(task.input_rows, list):
        raise CompareInputError("任务输入已过期，请重新提交")
    if task.kind == "compare":
        report = compare_lists(session, task.input_rows)
        task.result_document_ids = sorted(
            {m.document_id for row in report.rows for m in row.matches}
        )
        return export_compare_excel(report, output)
    if task.kind == "track":
        report = track_clients(session, task.input_rows)
        task.result_document_ids = sorted({item.document_id for item in report.items})
        return export_track_excel(report, output)
    raise CompareInputError("不支持的工具任务类型")


def legacy_result_document_ids(session: Session, client: httpx.Client, url: str) -> list[int]:
    """旧结果没有依赖清单：只在存在下架文件时从既有 Excel 的原文链接补齐。

    不用当前输入重算，避免漏掉历史结果中的文件；只取文件引用，不记录客户内容。
    """
    data = bytearray()
    with client.stream("GET", url, timeout=30) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("结果文件过大")
    with ZipFile(BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024:
            raise ValueError("结果文件过大")
    book = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        rows = sheet.iter_rows(min_row=2, values_only=True)
        headers = next(rows)
        column = headers.index("原文链接")
        urls = {str(row[column]) for row in rows if len(row) > column and row[column]}
        return list(session.scalars(select(Document.id).where(Document.url.in_(urls))))
    finally:
        book.close()


def process_ai(
    session: Session,
    task: AdvisorTask,
    *,
    settings: Settings,
    stamp: datetime,
    timeout: timedelta = TASK_TIMEOUT,
    chat_client: ChatClient | None = None,
) -> bool:
    """拒答是有原因的完成结果；暂时性调用失败最多重排两次。"""
    if task.status not in ("running", "queued") or not interpret.enabled(
        settings, session.get(User, task.user_id)
    ):
        return False
    skill = get_skill(task.kind)
    started = monotonic_clock.monotonic()
    own_client = chat_client is None
    model = settings.ai_model
    task.model, task.prompt_version = model, skill.prompt_version
    try:
        if not task.input_json:
            raise interpret.InterpretRefusal("任务输入已过期，请重新提交")
        snapshot, verified, key = interpret.prepare(
            session, task.input_json["doc_id"], model, skill=skill
        )
        task.snapshot, task.cache_key = snapshot, key
        task.result_document_ids = snapshot["document_ids"]
        cached = session.scalar(
            select(AdvisorTask)
            .where(
                AdvisorTask.kind == skill.kind,
                AdvisorTask.id != task.id,
                AdvisorTask.prompt_version == skill.prompt_version,
                AdvisorTask.model == model,
                AdvisorTask.cache_key == key,
                AdvisorTask.status == "done",
                AdvisorTask.result_json.is_not(None),
                AdvisorTask.finished_at > stamp - FILE_RETENTION,
                AdvisorTask.expires_at > stamp,
            )
            .order_by(AdvisorTask.finished_at.desc())
            .limit(1)
        )
        reusable = False
        if cached is not None and cached.result_json and not cached.result_json.get("refusal"):
            generated = _utc(datetime.fromisoformat(cached.result_json["generated_at"]))
            # 复制结果不会刷新模型生成时间，避免连续命中把 30 天期限无限延长。
            if generated > stamp - FILE_RETENTION and cached.snapshot:
                try:
                    _, _, cached_current_key = interpret.prepare(
                        session, cached.snapshot["doc_id"], model, skill=skill
                    )
                    reusable = cached_current_key == key
                except interpret.InterpretRefusal:
                    pass
        if reusable:
            result = (
                clause.notification_result(cached.result_json)
                if skill.kind == "clause"
                else copy.deepcopy(cached.result_json)
            )
        else:
            if chat_client is None:
                if not settings.ai_api_key:
                    raise AIError("AI 服务尚未配置")
                chat_client = ChatClient(
                    settings.ai_base_url,
                    settings.ai_api_key.get_secret_value(),
                    model,
                    timeout=min(30, timeout.total_seconds()),
                    max_retries=0,
                )
            payload = chat_client.complete_json(
                skill.system_prompt,
                json.dumps({"paragraphs": snapshot["paragraphs"]}, ensure_ascii=False),
                max_tokens=12000,
            )
            if monotonic_clock.monotonic() - started > timeout.total_seconds():
                raise TimeoutError
            result = interpret.result_metadata(
                interpret.validate(payload, snapshot["paragraphs"], skill=skill),
                stamp=stamp,
                model=model,
                verified=verified,
                attachments=snapshot["attachments"],
                model_disclosure=interpret.disclosure(settings),
                skill=skill,
            )
            # 调用期间原文或核对状态可能变更；刷新后重新校验，不发布旧快照。
            session.flush()
            session.expire_all()
            _, _, current_key = interpret.prepare(
                session, task.input_json["doc_id"], model, skill=skill
            )
            if key != current_key:
                raise interpret.InterpretRefusal("正文或核对内容已变化，请重新提交")
        task.result_json = result
        task.result_rows = len(result[skill.result_field])
        task.status, task.finished_at, task.error = "done", stamp, None
        if result["refusal"]:
            release_usage(session, task)
        session.commit()
        return True
    except interpret.InterpretRefusal as exc:
        task.result_json = interpret.result_metadata(
            {
                skill.result_field: [],
                **({"missing": list(skill.types)} if skill.kind == "interpret" else {}),
                "dropped": 0,
                "refusal": str(exc),
            },
            stamp=stamp,
            model=model,
            verified=[],
            model_disclosure=interpret.disclosure(settings),
            skill=skill,
            attachments=(task.snapshot or {}).get("attachments", 0),
        )
        task.cache_key = None
        task.status, task.finished_at, task.error = "done", stamp, None
        task.result_rows = 0
        release_usage(session, task)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 不向用户暴露模型原始响应或配置
        session.rollback()
        task = session.get(AdvisorTask, task.id)
        if task is None:
            return False
        task.model, task.prompt_version = model, skill.prompt_version
        task.result_json = None
        task.cache_key = None
        retryable = (isinstance(exc, AIError) and exc.retryable) or isinstance(
            exc, (httpx.TransportError, TimeoutError)
        )
        task.error = (
            "AI 返回内容格式无效"
            if isinstance(exc, ValueError)
            else (
                "AI 条款对照失败，请稍后重试"
                if skill.kind == "clause"
                else "AI 解读失败，请稍后重试"
            )
        )
        if isinstance(exc, AIError) and not exc.retryable:
            task.error = "AI 返回内容无效或请求被拒绝，请稍后重试"
        if retryable and task.attempts <= 2:
            task.status, task.started_at, task.finished_at = "queued", None, None
        else:
            task.status, task.started_at, task.finished_at = "failed", None, stamp
            release_usage(session, task)
        session.commit()
        return False
    finally:
        if chat_client is not None:
            try:
                record_usage(session, chat_client, skill.kind, now=stamp)
            finally:
                if own_client:
                    chat_client.close()


def process_interpret(session: Session, task: AdvisorTask, **kwargs) -> bool:
    """兼容原有政策解读调用入口。"""
    return process_ai(session, task, **kwargs)


def process_clause(session: Session, task: AdvisorTask, **kwargs) -> bool:
    """条款对照共用同一处理流程。"""
    return process_ai(session, task, **kwargs)


REVOKED_ORDER_STATUS = ("refunding", "refunded")
REVOKED_RESULT = "订单已退款，结果不可用"


def _paid_order(session: Session, task: AdvisorTask, *, lock: bool = False) -> PayOrder | None:
    if task.pay_order_id is None:
        return None
    stmt = select(PayOrder).where(PayOrder.id == task.pay_order_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalar(stmt)


def order_revoked(order: PayOrder | None) -> bool:
    """付费任务的订单已退款、退款中或开始过退款：结果不能再执行、查看或下载。"""
    return order is not None and (
        order.status in REVOKED_ORDER_STATUS or order.refund_status is not None
    )


def _cancel_revoked(session: Session, task: AdvisorTask, stamp: datetime) -> bool:
    task.status, task.finished_at, task.error = "cancelled", stamp, REVOKED_RESULT
    task.result_file_id, task.result_rows = None, None
    session.commit()
    return False


def complete_paid_order(session: Session, task: AdvisorTask, stamp: datetime) -> None:
    """付费任务成功时，在同一事务里把订单置为已完成（审阅第 2 条）。"""
    order = _paid_order(session, task, lock=True)
    if order is not None and order.status == "processing" and order.task_id == task.id:
        order.status, order.task_completed_at, order.updated_at = (
            "completed",
            task.finished_at or stamp,
            stamp,
        )


def process_task(
    session: Session,
    task: AdvisorTask,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    stamp: datetime | None = None,
    timeout: timedelta = TASK_TIMEOUT,
) -> bool:
    """处理一条已锁定为 running 的任务；失败按最多两次重试回队列。"""
    settings = settings or get_settings()
    stamp = _utc(stamp or datetime.now(UTC))
    if task.kind in AI_KINDS:
        return process_ai(session, task, settings=settings, stamp=stamp, timeout=timeout)
    # 执行前不加锁检查：执行耗时长，持锁会挡住同时到达的退款推送；交付前再加锁复核。
    if task.pay_order_id is not None and order_revoked(_paid_order(session, task)):
        return _cancel_revoked(session, task, stamp)
    own_client = client is None
    client = client or httpx.Client(trust_env=False)
    started = monotonic_clock.monotonic()
    uploaded_file_id: str | None = None
    try:
        cloud_env = getattr(settings, "cloud_env", None)
        if not cloud_env:
            raise BackupError("未配置对象存储环境")
        user = session.get(User, task.user_id)
        if user is None:
            raise CompareInputError("用户不存在，任务无法处理")
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / f"{task.id}.xlsx"
            rows = _run_engine(session, task, local)
            if monotonic_clock.monotonic() - started > timeout.total_seconds():
                raise TimeoutError
            file_id = upload(
                client,
                cloud_env,
                result_path(user, task.id, engine=session.get_bind()),
                local,
                what="工具结果",
            )
            uploaded_file_id = file_id
        if monotonic_clock.monotonic() - started > timeout.total_seconds():
            raise TimeoutError
        if task.pay_order_id is not None and order_revoked(_paid_order(session, task, lock=True)):
            # 执行期间订单被退款：丢弃刚上传的结果，不交付。
            _delete_and_record(session, [file_id], settings=settings, client=client)
            uploaded_file_id = None
            return _cancel_revoked(session, task, stamp)
        task.status = "done"
        task.result_file_id = file_id
        task.result_rows = rows
        task.finished_at = stamp
        task.error = None
        complete_paid_order(session, task, stamp)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 用户只需看到稳定的中文原因
        session.rollback()
        if uploaded_file_id:
            _delete_and_record(
                session,
                [uploaded_file_id],
                settings=settings,
                client=client,
            )
        task = session.get(AdvisorTask, task.id)
        if task is None:
            session.commit()
            return False
        task.error = _friendly_error(exc)
        if task.attempts <= 2:
            task.status = "queued"
            task.started_at = None
            task.finished_at = None
        else:
            task.status = "failed"
            task.started_at = None
            task.finished_at = stamp
            release_usage(session, task)
        session.commit()
        log.warning("工具任务 #%s 失败（第 %s 次）：%s", task.id, task.attempts, task.error)
        return False
    finally:
        if own_client:
            client.close()


def run_advisor_tasks(
    session: Session,
    *,
    settings: Settings | None = None,
    stamp: datetime | None = None,
    timeout: timedelta = TASK_TIMEOUT,
    max_tasks: int | None = None,
) -> dict[str, int]:
    """定时任务入口；先终结失去授权的解读任务，再按开关领取任务。"""
    settings = settings or get_settings()
    stamp = _utc(stamp or datetime.now(UTC))
    _fail_revoked_interpret(session, settings, stamp)
    allowed_kinds = []
    if getattr(settings, "advisor_enabled", False):
        allowed_kinds.extend(("compare", "track"))
    allowed = AdvisorTask.kind.in_(allowed_kinds)
    if getattr(settings, "ai_advisor_enabled", False) and interpret.openids(settings):
        allowed = allowed | (
            (AdvisorTask.kind.in_(AI_KINDS))
            & AdvisorTask.user_id.in_(
                select(User.id).where(User.openid.in_(interpret.openids(settings)))
            )
        )
    if not allowed_kinds and not (
        getattr(settings, "ai_advisor_enabled", False) and interpret.openids(settings)
    ):
        return {"processed": 0, "done": 0, "failed": 0, "requeued": 0}
    if not getattr(settings, "pay_enabled", False):
        allowed = allowed & AdvisorTask.pay_order_id.is_(None)
    # 关联订单已退款或退款中的付费任务不再领取（审阅第 1 条）。
    allowed = allowed & (
        AdvisorTask.pay_order_id.is_(None)
        | AdvisorTask.pay_order_id.not_in(
            select(PayOrder.id).where(
                PayOrder.status.in_(REVOKED_ORDER_STATUS) | PayOrder.refund_status.is_not(None)
            )
        )
    )
    requeued = recover_stale(session, stamp, allowed)
    result = {"processed": 0, "done": 0, "failed": 0, "requeued": requeued}
    claimed: set[int] = set()
    while max_tasks is None or result["processed"] < max_tasks:
        stmt = (
            select(AdvisorTask)
            .where(AdvisorTask.status == "queued", allowed)
            .order_by(AdvisorTask.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if claimed:
            stmt = stmt.where(AdvisorTask.id.not_in(claimed))
        task = session.scalar(stmt)
        if task is None:
            break
        task.status = "running"
        task.started_at = stamp
        task.attempts += 1
        task.error = None
        session.commit()
        claimed.add(task.id)
        result["processed"] += 1
        if process_task(session, task, settings=settings, stamp=stamp, timeout=timeout):
            result["done"] += 1
        else:
            result["failed"] += 1
    return result


def download_url(client: httpx.Client, env: str, file_id: str) -> str | None:
    data = _open_api(client, "/tcb/batchdownloadfile", {"env": env, "file_list": [file_id]})
    items = data.get("download_list") or data.get("file_list") or []
    if not items:
        return None
    item = items[0]
    if item.get("status") not in (None, 0):
        return None
    return item.get("download_url") or item.get("url")


def delete_result_files(client: httpx.Client, env: str, file_ids: list[str]) -> list[str]:
    """返回对象存储确认删除成功的文件 ID。"""
    if not file_ids:
        return []
    data = _open_api(client, "/tcb/batchdeletefile", {"env": env, "fileid_list": file_ids})
    return [
        item["fileid"]
        for item in data.get("delete_list", [])
        if item.get("status") == 0 and item.get("fileid") in file_ids
    ]


def cleanup_advisor(
    session: Session,
    *,
    settings: Settings | None = None,
    stamp: datetime | None = None,
    client: httpx.Client | None = None,
) -> dict[str, int]:
    """清理七天输入和三十天导出；对象存储删除成功后才清空 file id。"""
    settings = settings or get_settings()
    stamp = _utc(stamp or datetime.now(UTC))
    input_count = (
        session.execute(
            update(AdvisorTask)
            .where(
                AdvisorTask.input_rows.is_not(None),
                AdvisorTask.created_at <= stamp - INPUT_RETENTION,
            )
            .values(input_rows=None)
        ).rowcount
        or 0
    )
    session.execute(
        update(AdvisorTask)
        .where(AdvisorTask.kind.in_(AI_KINDS), AdvisorTask.created_at <= stamp - INPUT_RETENTION)
        .values(input_json=None)
    )
    session.execute(
        update(AdvisorTask)
        .where(AdvisorTask.kind.in_(AI_KINDS), AdvisorTask.expires_at <= stamp)
        .values(
            result_json=None,
            snapshot=None,
            cache_key=None,
            model=None,
            prompt_version=None,
            result_document_ids=None,
        )
    )
    old = list(
        session.scalars(
            select(AdvisorTask).where(
                AdvisorTask.result_file_id.is_not(None),
                AdvisorTask.expires_at.is_not(None),
                AdvisorTask.expires_at <= stamp,
            )
        )
    )
    deleted_count = 0
    own_client = client is None
    trash = list(session.scalars(select(AdvisorFileTrash)))
    file_ids = list(
        dict.fromkeys(
            [task.result_file_id for task in old if task.result_file_id]
            + [row.file_id for row in trash]
        )
    )
    if file_ids:
        client = client or httpx.Client(trust_env=False)
        try:
            successful = _delete_and_record(
                session,
                file_ids,
                settings=settings,
                client=client,
            )
        finally:
            if own_client:
                client.close()
        for task in old:
            if task.result_file_id in successful:
                task.result_file_id = None
                deleted_count += 1
        for row in trash:
            if row.file_id in successful:
                session.delete(row)
    session.commit()
    return {"advisor_input_rows": input_count, "advisor_files": deleted_count}


def delete_user_tasks(
    session: Session,
    user_id: int,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> int:
    """注销或用户删除任务时删除结果文件，再删除数据库任务记录。"""
    settings = settings or get_settings()
    tasks = list(session.scalars(select(AdvisorTask).where(AdvisorTask.user_id == user_id)))
    file_ids = [task.result_file_id for task in tasks if task.result_file_id]
    own_client = client is None
    if file_ids:
        client = client or httpx.Client(trust_env=False)
        try:
            _delete_and_record(session, file_ids, settings=settings, client=client)
        finally:
            if own_client:
                client.close()
    # 没有任务就不发 DELETE：正式库给 youju_api 授权顾问表之前（功能关闭期间），
    # 注销不能因为缺这张表的 DELETE 权限而失败（MySQL 按语句检查权限，删 0 行也会报错）。
    if tasks:
        session.execute(delete(AdvisorTask).where(AdvisorTask.user_id == user_id))
    return len(tasks)
