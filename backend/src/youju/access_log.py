"""网络日志：《网络安全法》要求网络运营者留存相关的网络日志不少于六个月。

- 记什么：每次请求一条（健康检查除外）：时间、服务、方法、路径（不含问号后面的查询参数）、状态码、
  耗时、来源 IP、小程序用户的去标识化编号；核对台操作另记做了什么、对哪条记录。
  不记查询参数和请求、响应内容：检索词、企业名称、纠错内容都不进日志。
- 怎么存：请求处理完写进数据库暂存表 access_logs，同时打一行到标准输出（云托管日志里能实时查）。
  写库失败的记录先留在内存里（每个实例最多 5000 条），下次写库时一起补上；实例在这期间重启会丢，
  但标准输出里每条都有。
  每天 0:30 把前一天（北京时间）写进库的归档成一个压缩文件，存到云托管对象存储
  logs/access-日期.jsonl.gz，上传成功才删暂存的记录；归档保留 190 天（多于六个月），到期删除。
  按写进库的时间（数据库的时钟）分日，不按请求时间：补写的记录归进补写那天，不会在某天归档之后
  又冒出这一天的记录（再归档一次会覆盖前一个文件）。所以某天的文件里可能有前一天的请求，
  查的时候按记录里的 at 字段看。暂存表不进数据库备份，恢复备份也不动它（见 youju/backup.py）。
- 归档格式：gzip 压缩的 JSON Lines，第一行说明（格式、写入日期），之后每行一条记录，最后一行结尾
  （条数和全部记录行的 SHA-256）；没有结尾说明文件不完整。
- 用户编号：openid 用程序自己的密钥（app_keys 表，第一次用到时随机生成）做 HMAC-SHA256，取前 16 位。
  只看日志得不到 openid；要查某个用户的日志时（如用户申请查阅），用 `youju log-uid` 算出编号再查。
- 写日志出错不影响请求本身；写库失败时提醒到群里（每个实例 10 分钟最多一条）。
"""

import gzip
import hashlib
import hmac
import json
import logging
import secrets
import shutil
import sys
import tempfile
import threading
import time as clock
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import IO, Any

import httpx
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from youju.alerts import send_alert
from youju.backup import BEIJING, BackupError, _open_api, _retrying, delete_files, upload
from youju.config import get_settings
from youju.db import get_engine
from youju.models import AccessDaily, AccessLog, AppKey

FORMAT = "youju-access-log"
VERSION = 1
KEEP_DAYS = 190
# 每次删掉这些天之前的归档（多删几天，漏跑或某天没删掉的也能清掉）
_PRUNE_DAYS = range(KEEP_DAYS + 1, KEEP_DAYS + 11)
# 一天结束后至少过这么久才归档这一天：写入时间取数据库执行插入时的时钟，提交在其后几毫秒内；
# 留出余量（也容下数据库和程序的时钟误差），免得归档之后还有这一天的记录写进来
_SETTLE = timedelta(minutes=10)
# 写库失败时每个实例在内存里最多留这么多条，等下次写库时补上；满了丢最早的
_PENDING_MAX = 5000
_FIELDS = ("service", "method", "path", "status", "ms", "ip", "detail")
KEY_NAME = "access_log"
# 每天汇总（AccessDaily）保留多久：只有计数，留一年多，够看同比
DAILY_KEEP_DAYS = 400
# 汇总只算小程序接口；超过这么久才答复算慢（D-061：99% 请求 3 秒内成功）
DAILY_PATH_PREFIX = "/api/v1/"
DAILY_SLOW_MS = 3000
_ALERT_INTERVAL = 600
_BATCH = 1000

log = logging.getLogger("youju.access_log")
# 每条访问日志在标准输出打一行 JSON（云托管日志里能实时检索）
out_log = logging.getLogger("youju.access")
if not out_log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    out_log.addHandler(_handler)
    out_log.setLevel(logging.INFO)
    out_log.propagate = False

_key_lock = threading.Lock()
_key: bytes | None = None
_fail_lock = threading.Lock()
_fail_state: dict = {"last_alert": None, "count": 0}
# 还没写进库的记录：（记录，openid）。openid 只在内存里，用来补算用户编号
_pending_lock = threading.Lock()
_pending: deque[tuple[dict[str, Any], str | None]] = deque()


def object_path(day: date) -> str:
    return f"logs/access-{day:%Y%m%d}.jsonl.gz"


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """北京时间这一天的起止（UTC）。"""
    start = datetime.combine(day, time(), tzinfo=BEIJING).astimezone(UTC)
    return start, start + timedelta(days=1)


def log_key(engine: Engine | None = None) -> bytes:
    """用户编号的去标识化密钥：库里没有就随机生成一个（几个实例同时生成时以先写进去的为准）。"""
    global _key
    with _key_lock:
        if _key is None:
            with (engine or get_engine()).begin() as conn:
                conn.execute(
                    mysql_insert(AppKey)
                    .prefix_with("IGNORE")
                    .values(name=KEY_NAME, value=secrets.token_hex(32))
                )
                _key = bytes.fromhex(
                    conn.scalar(select(AppKey.value).where(AppKey.name == KEY_NAME))
                )
        return _key


def log_uid(openid: str, engine: Engine | None = None) -> str:
    return hmac.new(log_key(engine), openid.encode(), hashlib.sha256).hexdigest()[:16]


def _send_in_background(message: str) -> None:
    threading.Thread(target=send_alert, args=(message,), daemon=True).start()


def _keep(batch: list[tuple[dict[str, Any], str | None]]) -> tuple[int, int]:
    """没写进库的放回内存，和这期间别的请求放回的合在一起按请求时间排，超出上限丢最早的。
    返回（现在内存里的条数，这次丢掉的条数）。"""
    with _pending_lock:
        merged = sorted([*batch, *_pending], key=lambda item: item[0]["at"])
        dropped = max(0, len(merged) - _PENDING_MAX)
        _pending.clear()
        _pending.extend(merged[dropped:])
        return len(_pending), dropped


def _note_failure(exc: Exception, waiting: int, dropped: int) -> None:
    """写库失败：记一行；距上次提醒超过 10 分钟就发一条提醒，带上这期间的次数。"""
    log.warning(
        "访问日志写入数据库失败：%s（内存里待补写 %d 条，丢掉 %d 条）",
        type(exc).__name__,
        waiting,
        dropped,
    )
    now = clock.monotonic()
    with _fail_lock:
        _fail_state["count"] += 1
        last = _fail_state["last_alert"]
        if last is not None and now - last < _ALERT_INTERVAL:
            return
        count = _fail_state["count"]
        _fail_state.update(last_alert=now, count=0)
    role = get_settings().role
    message = (
        f"访问日志写入数据库失败（{role} 服务，上次提醒以来 {count} 次）：{type(exc).__name__}。"
        f"没写进去的先留在内存里（现有 {waiting} 条，最多 {_PENDING_MAX} 条），数据库恢复后补写；"
        "实例重启会丢，云托管日志里仍有"
    )
    _send_in_background(message)


def write(entry: dict[str, Any], *, engine: Engine | None = None) -> None:
    """写一条访问日志：算出用户编号，连同内存里之前没写进去的一起写进暂存表，再打一行到标准输出。
    出错只提醒，不抛出。

    entry：service、method、path、status、ms、ip，可选 openid（只用来算编号，不保存）和
    detail（核对台操作）。
    """
    openid = entry.get("openid")
    row = {name: entry.get(name) for name in _FIELDS}
    # rid：这条记录的随机编号。提交其实成功了却报错时会补写第二次，编号相同就只留一条
    row.update(rid=secrets.token_hex(16), at=datetime.now(UTC), uid=None)
    with _pending_lock:
        batch = [*_pending, (row, openid)]
        _pending.clear()
    saved = False
    try:
        for item, item_openid in batch:
            if item_openid and item["uid"] is None:
                item["uid"] = log_uid(item_openid, engine)
        # INSERT IGNORE：编号相同的只留一条。线上 api 账号对这张表只有 INSERT（只能新增），
        # 不能用 ON DUPLICATE KEY UPDATE：它要 UPDATE 权限，会被拒（1142），
        # api 的日志一条都写不进库（9/29 上线起如此，10/16 发布核对时发现）。
        stmt = mysql_insert(AccessLog).prefix_with("IGNORE")
        with (engine or get_engine()).begin() as conn:
            conn.execute(stmt, [item for item, _ in batch])
        saved = True
    except Exception as exc:  # noqa: BLE001 - 写日志出错不能影响请求本身
        _note_failure(exc, *_keep(batch))
    try:
        line = {k: v for k, v in row.items() if k != "detail"}
        out_log.info(
            json.dumps(
                {"event": "access", **line, "saved": saved}, ensure_ascii=False, default=_encode
            )
        )
    except Exception:  # noqa: BLE001, S110 - 同上
        pass


@dataclass
class ArchiveReport:
    days: dict[date, int] = field(default_factory=dict)
    bytes: int = 0
    expired: int = 0
    pruned: int = 0


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"不支持的类型：{type(value).__name__}")


def export_day(
    engine: Engine, day: date, out: IO[bytes], counts: dict[str, list[int]] | None = None
) -> tuple[int, int | None]:
    """把这一天（北京时间）写进库的暂存记录按编号顺序写成 gzip JSON Lines，
    返回（条数，最大编号）。给了 counts 时，同一遍里按服务数小程序接口的请求、5xx、慢请求
    （{服务: [请求数, 5xx, 慢]}，见 count_request），和写进归档文件的记录逐条一致。"""
    start, end = _day_bounds(day)
    header = {"format": FORMAT, "version": VERSION, "day": day.isoformat(), "tz": "Asia/Shanghai"}
    digest = hashlib.sha256()
    count, last_id = 0, None
    table = AccessLog.__table__
    with (
        engine.connect() as conn,
        gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz,
    ):
        gz.write((json.dumps(header, ensure_ascii=False) + "\n").encode())
        result = conn.execution_options(stream_results=True, yield_per=_BATCH).execute(
            select(table)
            .where(table.c.logged_at >= start, table.c.logged_at < end)
            .order_by(table.c.id)
        )
        for row in result.mappings():
            line = (json.dumps(dict(row), ensure_ascii=False, default=_encode) + "\n").encode()
            digest.update(line)
            gz.write(line)
            count += 1
            last_id = row["id"]
            if counts is not None:
                count_request(counts, row["service"], row["path"], row["status"], row["ms"])
        gz.write(
            (json.dumps({"end": True, "rows": count, "sha256": digest.hexdigest()}) + "\n").encode()
        )
    return count, last_id


def count_request(
    counts: dict[str, list[int]], service: str | None, path: str | None, status: int, ms: int
) -> None:
    """按天汇总的口径：只算小程序接口（/api/v1/）；5xx 算失败；
    没返回 5xx、但 3 秒及以上才答复的算慢。"""
    if not (path or "").startswith(DAILY_PATH_PREFIX):
        return
    item = counts.setdefault(service or "", [0, 0, 0])
    item[0] += 1
    if (status or 0) >= 500:
        item[1] += 1
    elif (ms or 0) >= DAILY_SLOW_MS:
        item[2] += 1


def save_daily(engine: Engine, day: date, counts: dict[str, list[int]]) -> int:
    """把这一天按服务的汇总写进 access_daily（同一天重算覆盖），返回写了几行。
    在上传归档之前写：上传失败暂存记录留着，下次归档重新数，结果一样。"""
    values = [
        {"day": day, "service": service, "requests": n, "errors": errors, "slow": slow}
        for service, (n, errors, slow) in sorted(counts.items())
    ]
    if values:
        stmt = mysql_insert(AccessDaily).values(values)
        with engine.begin() as conn:
            conn.execute(
                stmt.on_duplicate_key_update(
                    requests=stmt.inserted.requests,
                    errors=stmt.inserted.errors,
                    slow=stmt.inserted.slow,
                )
            )
    return len(values)


def _pending_days(engine: Engine, now: datetime) -> list[date]:
    """要归档的日子（按写入时间）：暂存表里最早的一天到昨天（刚过零点不到 10 分钟时到前天）。"""
    last = now.astimezone(BEIJING).date() - timedelta(days=1)
    if now < _day_bounds(last)[1] + _SETTLE:
        last -= timedelta(days=1)
    with engine.connect() as conn:
        first_at = conn.scalar(
            select(func.min(AccessLog.logged_at)).where(AccessLog.logged_at < _day_bounds(last)[1])
        )
    if first_at is None:
        return []
    first = first_at.astimezone(BEIJING).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def prune(client: httpx.Client, env: str, now: datetime) -> int:
    """删掉 191–200 天前的归档，返回删掉的个数（本来就没有的不算错）。文件 ID 的前缀从
    "获取上传链接"接口返回的文件 ID 得到（只取链接，不上传）。"""
    today = now.astimezone(BEIJING).date()
    paths = [object_path(today - timedelta(days=d)) for d in _PRUNE_DAYS]
    link = _open_api(client, "/tcb/uploadfile", {"env": env, "path": paths[0]})
    file_id = link.get("file_id") or ""
    if not file_id.endswith(paths[0]):
        raise BackupError("文件 ID 的格式和预期不同，没有删除过期的访问日志归档")
    prefix = file_id[: -len(paths[0])]
    return delete_files(client, env, [prefix + p for p in paths], what="过期的访问日志归档")


def run_archive(
    engine: Engine,
    env: str | None,
    *,
    output: Path | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleep=clock.sleep,
) -> ArchiveReport:
    """删掉超过 190 天还没归档的暂存记录；把之前每天的暂存记录归档（上传到对象存储，
    给了 output 时只写到本机这个目录），成功一天删一天的暂存；最后删掉过期的归档。
    上传和删除遇到网络中断、服务繁忙都重试两次；失败抛 BackupError（由 job.sh 发提醒），
    没归档成功的暂存记录留着，下次再归档。"""
    if output is None and not env:
        raise BackupError("没有设置云托管环境 ID，无法上传访问日志归档")
    now = now or datetime.now(UTC)
    report = ArchiveReport()
    with engine.begin() as conn:
        report.expired = conn.execute(
            delete(AccessLog).where(AccessLog.logged_at < now - timedelta(days=KEEP_DAYS))
        ).rowcount
        conn.execute(
            delete(AccessDaily).where(
                AccessDaily.day < now.astimezone(BEIJING).date() - timedelta(days=DAILY_KEEP_DAYS)
            )
        )
    own = client is None and output is None
    if own:
        client = httpx.Client(trust_env=False)
    try:
        for day in _pending_days(engine, now):
            path = object_path(day)
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / Path(path).name
                counts: dict[str, list[int]] = {}
                with local.open("wb") as fh:
                    rows, last_id = export_day(engine, day, fh, counts)
                if not rows:
                    continue
                # 归档前先按天汇总（M3）：暂存记录归档成功就删，早上的日报只能从汇总里读。
                # 计数和写进归档文件的是同一遍读出来的同一批记录
                save_daily(engine, day, counts)
                size = local.stat().st_size
                if output is not None:
                    shutil.copyfile(local, output / local.name)
                else:
                    _retrying(partial(upload, client, env, path, local, what="访问日志归档"), sleep)
            start, end = _day_bounds(day)
            with engine.begin() as conn:
                conn.execute(
                    delete(AccessLog).where(
                        AccessLog.logged_at >= start,
                        AccessLog.logged_at < end,
                        AccessLog.id <= last_id,
                    )
                )
            report.days[day] = rows
            report.bytes += size
        if output is None:
            report.pruned = _retrying(partial(prune, client, env, now), sleep)
    finally:
        if own:
            client.close()
    return report
