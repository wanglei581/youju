"""R1.5 工具任务队列、额度和结果文件处理。"""

from __future__ import annotations

import logging
import tempfile
import time as monotonic_clock
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from youju import access_log
from youju.backup import BackupError, _open_api, upload
from youju.compare import CompareInputError, compare_lists, export_compare_excel
from youju.config import Settings, get_settings
from youju.models import AdvisorFileTrash, AdvisorTask, AdvisorUsage, User
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


def task_dict(task: AdvisorTask, *, include_result: bool = True) -> dict[str, Any]:
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
    }
    if include_result:
        result.update(
            {
                "resultRows": task.result_rows,
                "hasFile": bool(task.result_file_id),
            }
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
    if row.used_count >= 1:
        # 兼容 0010 上线前已被后台标成 failed 的旧任务：旧代码没有台账，
        # 首次遇到时补回一次；正常失败路径会在这里之前显式释放。
        old_failed = session.scalar(
            select(AdvisorTask.id)
            .where(
                AdvisorTask.user_id == user_id,
                AdvisorTask.kind == kind,
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


def recover_stale(session: Session, stamp: datetime | None = None) -> int:
    """实例重启后把运行超过十分钟的任务放回队列，不增加 attempts。"""
    stamp = _utc(stamp or datetime.now(UTC))
    count = session.execute(
        update(AdvisorTask)
        .where(
            AdvisorTask.status == "running",
            AdvisorTask.started_at.is_not(None),
            AdvisorTask.started_at < stamp - STALE_RUNNING,
        )
        .values(status="queued", started_at=None, finished_at=None)
    ).rowcount
    session.commit()
    return count or 0


def _run_engine(session: Session, task: AdvisorTask, output: Path) -> int:
    if not isinstance(task.input_rows, list):
        raise CompareInputError("任务输入已过期，请重新提交")
    if task.kind == "compare":
        report = compare_lists(session, task.input_rows)
        return export_compare_excel(report, output)
    if task.kind == "track":
        report = track_clients(session, task.input_rows)
        return export_track_excel(report, output)
    raise CompareInputError("不支持的工具任务类型")


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
        task.status = "done"
        task.result_file_id = file_id
        task.result_rows = rows
        task.finished_at = stamp
        task.error = None
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
    """定时任务入口；总开关关闭时完全跳过。"""
    settings = settings or get_settings()
    if not getattr(settings, "advisor_enabled", False):
        return {"processed": 0, "done": 0, "failed": 0, "requeued": 0}
    stamp = _utc(stamp or datetime.now(UTC))
    requeued = recover_stale(session, stamp)
    result = {"processed": 0, "done": 0, "failed": 0, "requeued": requeued}
    claimed: set[int] = set()
    while max_tasks is None or result["processed"] < max_tasks:
        stmt = (
            select(AdvisorTask)
            .where(AdvisorTask.status == "queued")
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
