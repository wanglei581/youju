"""任务记账与任务本身分开：数据库不可用时任务继续运行。"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from youju.db import session_factory
from youju.models import JobRun

log = logging.getLogger("youju.jobs")
OVERVIEW_JOBS = (
    "round",
    "backup",
    "archive-logs",
    "cleanup",
    "audit-deadlines",
    "daily-report",
    "weekly-digest",
    "pay-reconcile",
)
# advisor-run、pay-run 每分钟一次，job.sh 不给它们记运行记录（情况看工具任务表、支付订单）
JOB_NAMES = OVERVIEW_JOBS + (
    "advisor-run",
    "pay-run",
    "crawl",
    "check-sources",
    "fetch-details",
    "repost-groups",
    "extract-deadlines",
    "extract-fields",
    "ai-check",
    "extract-conditions",
    "triage-feedback",
    "parse-lists",
    "cache-attachments",
    "notify",
    "alert",
)


def start_run(job: str) -> int | None:
    try:
        if job not in JOB_NAMES:
            raise ValueError("未知任务")
        with session_factory()() as session:
            row = JobRun(job=job, started_at=datetime.now(UTC), summary="任务开始")
            session.add(row)
            session.commit()
            return row.id
    except Exception:  # noqa: BLE001 - 记账失败不能阻断任务；异常详情可能包含连接凭据
        log.warning("任务开始记录未写入")
        return None


def finish_run(run_id: int | None, code: int) -> None:
    if run_id is None:
        return
    try:
        with session_factory()() as session:
            row = session.scalar(select(JobRun).where(JobRun.id == run_id).with_for_update())
            if row is None or row.status != "running":
                return
            row.finished_at = datetime.now(UTC)
            row.status = "ok" if code == 0 else "failed"
            # 不复制 stdout、异常或命令参数：其中可能有密钥、管理口令或带凭据的网址。
            row.summary = {
                0: "任务执行完成",
                20: "来源检查未达标，详细提醒已发送",
                75: "上一次还没跑完，本次重叠跳过",
                124: "运行超过 3 小时，已超时",
                130: "任务被中断",
                143: "任务被终止",
            }.get(code, f"任务执行失败（退出码 {int(code)}）")[:500]
            session.commit()
    except Exception:  # noqa: BLE001 - 同上；不输出数据库异常正文
        log.warning("任务结束记录未写入")


def run_out(row: JobRun) -> dict:
    return {
        "id": row.id,
        "job": row.job,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "status": row.status,
        "summary": row.summary,
    }
