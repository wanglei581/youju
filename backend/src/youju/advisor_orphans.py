"""批量工具结果文件巡检（youju advisor-orphans）：对象存储里有、但没有任何记录指向的结果文件。

- 路径规则：advisor/out/<去标识用户编号>/<任务编号>.xlsx（advisor_tasks.result_path；用户编号是
  access_log.log_uid，openid 的 HMAC 前 16 位十六进制）。任务完成时文件 ID（cloud://环境.存储桶/
  路径）记进 advisor_tasks.result_file_id；删不掉的记进 advisor_file_trash，每天 3:30 的清理重试。
- 为什么会有：worker（process_task）把结果上传之后、提交任务之前进程退出，之后这个任务一直没人处理
  （比如批量工具开关一直关着），任务过期被清理，或者用户注销、删除任务时，就没有任何记录指向这份
  文件，清理永远删不到。文件里是用户的客户名单比对结果，隐私保护指引写的是保存 30 天、到期删除。
- 怎么找：按前缀列出对象存储里的 advisor/out/（youju/storage_list.py），逐个对照，下面这些不动，
  其余是孤儿：
  不是结果文件命名的；任务表 result_file_id 指向的；待删表里的（清理会删）；最后修改不到 31 天的
  （30 天保存期加 1 天余量，刚上传、任务还没提交的也在里面）；任务表里这条任务还在排队或处理中的
  （重新处理会写回同一个路径，删了可能删到刚生成的结果）。
  按路径比，不按整个文件 ID 比：记录里的文件 ID 换过环境前缀也算有记录（宁可少删）。
- 先读任务表和待删表、再列对象存储：列的时候刚被清理删掉记录的文件，读的时候还有记录，不算孤儿。
- 默认只报个数；delete 才删。删除前再读一次任务表和待删表，期间有了记录的不删；删除走
  advisor_tasks._delete_and_record（删不掉的进待删表，每天的清理会重试），每次最多 50 个。
  记录里有认不出格式的文件 ID 时不删（不知道它指向哪里）。
- 只输出个数和文件编号前缀，不输出用户编号、任务编号。
"""

import re
import time as clock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from youju import storage_list
from youju.advisor_tasks import FILE_RETENTION, _delete_and_record
from youju.backup import BackupError
from youju.config import Settings
from youju.models import AdvisorFileTrash, AdvisorTask

RESULT_PREFIX = "advisor/out/"
MIN_AGE = FILE_RETENTION + timedelta(days=1)
DELETE_BATCH = 50
_RESULT_KEY = re.compile(r"advisor/out/[0-9a-f]{16}/([0-9]+)\.xlsx")
_FILE_KEY = re.compile(r"cloud://[^/]+/(.+)")


@dataclass
class OrphanReport:
    prefix: str = ""
    # advisor/out/ 下的文件数，以及其中不动的各类
    listed: int = 0
    referenced: int = 0
    trash: int = 0
    recent: int = 0
    pending: int = 0
    other: int = 0
    # 没有任何记录指向、超过 31 天的
    orphans: int = 0
    # 任务表、待删表里认不出格式的文件 ID：有就不删
    unparsed: int = 0
    deleting: bool = False
    # 以下只在删除时有：删除前复查有了记录、没删的；删掉的；没删掉、记进待删表的
    rechecked: int = 0
    deleted: int = 0
    failed: int = 0

    @property
    def blocked(self) -> bool:
        """要删、有孤儿，但记录里有认不出的文件 ID，所以没删。"""
        return self.deleting and bool(self.orphans and self.unparsed)

    def text(self) -> str:
        days = MIN_AGE.days
        kept = (
            f"任务表有记录 {self.referenced} 个，待删表里 {self.trash} 个，"
            f"最后修改不到 {days} 天 {self.recent} 个，任务还在排队或处理中 {self.pending} 个，"
            f"不是结果文件命名的 {self.other} 个"
        )
        lines = [
            f"结果文件巡检（{self.prefix}）：对象存储里共 {self.listed} 个文件。",
            f"不动的：{kept}。",
        ]
        orphans = f"没有任何记录指向、超过 {days} 天的：{self.orphans} 个"
        if self.unparsed:
            lines.append(f"{orphans}。")
            lines.append(
                f"任务表或待删表里有 {self.unparsed} 个文件 ID 认不出格式，不知道指向哪个文件；"
                "为免误删，有这种记录时加 --delete 也不删除，要先查清。"
            )
        elif not self.deleting:
            hint = "（只列出，没有删除；确认后加 --delete 删除）" if self.orphans else ""
            lines.append(f"{orphans}{hint}。")
        elif not self.orphans:
            lines.append(f"{orphans}，没有要删的。")
        else:
            line = f"{orphans}，删掉 {self.deleted} 个"
            if self.failed:
                line += f"，没删掉 {self.failed} 个（已记进待删表，每天 3:30 的清理会重试）"
            if self.rechecked:
                line += f"；删除前复查时有了记录、没删的 {self.rechecked} 个"
            lines.append(f"{line}。")
        return "\n".join(lines)

    def alert_text(self) -> str:
        return (
            f"结果文件巡检：对象存储里有 {self.orphans} 个批量工具结果文件没有任何记录指向，"
            f"最后修改已超过 {MIN_AGE.days} 天（保存期是 {FILE_RETENTION.days} 天）。"
            "确认后在 admin 服务的容器里运行 youju advisor-orphans --delete 删除。"
        )


@dataclass(frozen=True)
class _Known:
    referenced: frozenset[str]
    trash: frozenset[str]
    pending: frozenset[int]
    unparsed: int


def _key(file_id: str) -> str | None:
    match = _FILE_KEY.fullmatch(file_id)
    return match.group(1) if match else None


def _known(session: Session) -> _Known:
    """任务表 result_file_id 和待删表里的路径，还在排队或处理中的任务编号。读完就结束事务，
    下一次读到的是那时已经提交的。"""
    referenced = list(
        session.scalars(
            select(AdvisorTask.result_file_id).where(AdvisorTask.result_file_id.is_not(None))
        )
    )
    trash = list(session.scalars(select(AdvisorFileTrash.file_id)))
    pending = frozenset(
        session.scalars(select(AdvisorTask.id).where(AdvisorTask.status.in_(("queued", "running"))))
    )
    session.commit()
    keys = {file_id: _key(file_id) for file_id in [*referenced, *trash]}
    return _Known(
        referenced=frozenset(keys[i] for i in referenced if keys[i]),
        trash=frozenset(keys[i] for i in trash if keys[i]),
        pending=pending,
        unparsed=sum(1 for key in keys.values() if key is None),
    )


def _kept(key: str, known: _Known) -> bool:
    match = _RESULT_KEY.fullmatch(key)
    return (
        key in known.referenced
        or key in known.trash
        or (match is not None and int(match.group(1)) in known.pending)
    )


def scan(
    session: Session,
    settings: Settings,
    *,
    delete: bool = False,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = clock.sleep,
) -> OrphanReport:
    """巡检对象存储 advisor/out/ 下的结果文件，delete 时删掉孤儿。对象存储出错抛 BackupError，
    这时什么都没删（删除在列完、对照完之后才开始）。"""
    env = getattr(settings, "cloud_env", None)
    if not env:
        raise BackupError("没有设置云托管环境 ID")
    now = now or datetime.now(UTC)
    cutoff = now - MIN_AGE
    report = OrphanReport(deleting=delete)
    known = _known(session)
    report.unparsed = known.unparsed
    own_client = client is None
    client = client or httpx.Client(trust_env=False)
    try:
        creds = storage_list.credentials(client, sleep=sleep)
        prefix, bucket = storage_list.bucket_of(client, env, sleep=sleep)
        report.prefix = prefix + RESULT_PREFIX
        objects = storage_list.list_objects(client, creds, bucket, RESULT_PREFIX, sleep=sleep)
        report.listed = len(objects)
        orphans = []
        for item in objects:
            match = _RESULT_KEY.fullmatch(item.key)
            if match is None:
                report.other += 1
            elif item.key in known.referenced:
                report.referenced += 1
            elif item.key in known.trash:
                report.trash += 1
            elif item.last_modified >= cutoff:
                report.recent += 1
            elif int(match.group(1)) in known.pending:
                report.pending += 1
            else:
                orphans.append(item.key)
        report.orphans = len(orphans)
        if not delete or not orphans or report.unparsed:
            return report
        fresh = _known(session)
        if fresh.unparsed:
            report.unparsed = fresh.unparsed
            return report
        targets = [prefix + key for key in orphans if not _kept(key, fresh)]
        report.rechecked = len(orphans) - len(targets)
        for start in range(0, len(targets), DELETE_BATCH):
            batch = targets[start : start + DELETE_BATCH]
            done = _delete_and_record(session, batch, settings=settings, client=client)
            session.commit()
            report.deleted += len(done)
            report.failed += len(batch) - len(done)
        return report
    finally:
        if own_client:
            client.close()
