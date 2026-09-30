"""R1.5 顾问任务队列、额度和结果文件处理。"""

from __future__ import annotations

import logging
import tempfile
import time as monotonic_clock
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from youju import access_log
from youju.backup import BackupError, _open_api, delete_files, upload
from youju.compare import CompareInputError, compare_lists, export_compare_excel
from youju.config import Settings, get_settings
from youju.models import AdvisorTask, User
from youju.track import export_track_excel, track_clients

log = logging.getLogger("youju.advisor")
BEIJING = ZoneInfo("Asia/Shanghai")
MAX_ROWS = 50
MAX_BODY_BYTES = 100 * 1024
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
    raise CompareInputError("不支持的顾问任务类型")


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
                what="顾问结果",
            )
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
        task = session.get(AdvisorTask, task.id)
        if task is None:
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
        session.commit()
        log.warning("顾问任务 #%s 失败（第 %s 次）：%s", task.id, task.attempts, task.error)
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


def delete_result_files(client: httpx.Client, env: str, file_ids: list[str]) -> int:
    return delete_files(client, env, file_ids, what="顾问结果") if file_ids else 0


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
    cloud_env = getattr(settings, "cloud_env", None)
    if old and cloud_env:
        client = client or httpx.Client(trust_env=False)
        try:
            deleted_count = delete_result_files(
                client,
                cloud_env,
                [task.result_file_id for task in old if task.result_file_id],
            )
            for task in old:
                task.result_file_id = None
        finally:
            if own_client:
                client.close()
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
    cloud_env = getattr(settings, "cloud_env", None)
    if file_ids and cloud_env:
        client = client or httpx.Client(trust_env=False)
        try:
            delete_result_files(client, cloud_env, file_ids)
        finally:
            if own_client:
                client.close()
    # 没有任务就不发 DELETE：正式库给 youju_api 授权顾问表之前（功能关闭期间），
    # 注销不能因为缺这张表的 DELETE 权限而失败（MySQL 按语句检查权限，删 0 行也会报错）。
    if tasks:
        session.execute(delete(AdvisorTask).where(AdvisorTask.user_id == user_id))
    return len(tasks)
