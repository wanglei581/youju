"""有据首发前搬家（D-127）：云托管导出。在云托管 admin 服务的"登录实例"终端里运行。

线上镜像（迁移 0046）没有搬家用的命令，这个脚本只用镜像里已有的命令和函数：
youju archive-logs / backup / backup-verify，backup.upload、backup._open_api，
storage_list.bucket_of、list_objects，takedown.available_condition，和那一版的表结构。

按顺序做（不删、不改任何数据；只往对象存储写当天的备份、日志归档和 exports/ 下的几个文件）：
1. youju archive-logs → 记下用户数据各表的整表校验值（CHECKSUM TABLE）→ youju backup →
   youju backup-verify（和每天定时任务做的一样）。--links-only 跳过这一步（清单里的链接过期了、
   只要重出一份清单时用），用上次记下的那份核对
2. 列出 backups/（只要最新一份）、logs/、attachments/、advisor/out/ 下的文件和大小
3. 下架清单 exports/takedowns-<时间>.jsonl：当前对用户不可见的文件（整组下架的每一份）的规范网址键、
   来源编号、下架时间、原因类别。不带备注、操作人、标题
4. 交易记录 exports/pay-records-<时间>.jsonl.gz：订单、退款（在订单里）、支付流水、开票申请、
   赠送天数、使用期、每日对账。隐私保护指引写的是这些记录"与账号断开，去标识化、限制访问"，
   保存 3650 天（D-127；订单、退款、开票自下单起，赠送自发放之日起）；这里照注销账号时
   （payments.anonymize、rewards.anonymize）的口径对所有人做一遍，另外去掉开票的联系邮箱和个人抬头（就是姓名）
5. 给每个文件取 2 小时有效的下载链接，连同汇总数（只有数字）和不涉密的配置写成清单
   exports/manifest-<时间>.json
6. 访问日志暂存表里还没归档的（archive-logs 只归档到昨天）按天导出 exports/access-staging-<日期>-…
7. 再记一次用户数据各表：和备份前那次对不上，说明导出期间有新写入（D-127：停掉新的写入，或在导出
   前后核对没有新写入），停下、不出清单
8. 打印一行给服务器网页终端用的命令（带清单的 SHA-256）、清单链接、各前缀的文件数和大小、汇总数

--cleanup（手册第 9 步，服务器上 IMPORT OK 以后）：删掉对象存储里的用户数据（exports/、backups/、
logs/、advisor/，见 CLEANUP_PREFIXES），删完再列一遍核对删干净。admin 调成 0 以后云托管按期删
它们的定时任务都停了，留着会超过各自的保存期；服务器上都已加密存档。attachments/ 是公开文件的
附件副本，不删。

清单里有整库备份的下载链接：那一行命令只贴到服务器的网页终端，不发到聊天里。
不打印任何单个文件的链接、密钥、openid。要复制的几行只有英文和数字（容器终端复制中文会乱码）。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import re
import subprocess
import sys
import tempfile
import time as clock
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, aliased

from youju import access_log, backup, storage_list
from youju.config import get_settings
from youju.db import create_mysql_engine
from youju.models import (
    AppKey,
    Document,
    DocumentMerge,
    DocumentTakedownEvent,
    InvoiceRequest,
    PassReward,
    PayEvent,
    PayOrder,
    PayReconciliation,
    User,
    WorkbenchPass,
)
from youju.takedown import available_condition

PREFIXES = ("backups/", "logs/", "attachments/", "advisor/out/")
# 用户数据：带 user_id、uid、user_hash 的表和 users，跟着它们的 pay_events、ai_chat_turns、
# follow_repost_notifications，交易记录里一起导出的 pay_reconciliations，访问日志暂存表（测试里核对
# 这一版的每张表都归了类）。导出前后各记一次整表校验值（MySQL 的 CHECKSUM TABLE，按每一行的内容算：
# 增、删、改都会变），对不上就是导出期间有新写入。admin 的定时任务不停：停了 cron，健康检查要的心跳
# 也停了，实例会被判不健康、重启
WATCHED = (
    "users",
    "follows",
    "subscriptions",
    "feedback",
    "events",
    "advisor_tasks",
    "advisor_usage",
    "ai_chats",
    "ai_chat_turns",
    "pay_orders",
    "pay_events",
    "invoice_requests",
    "deadline_clues",
    "pass_rewards",
    "boost_trials",
    "workbench_passes",
    "ai_credit_grants",
    "ai_credit_uses",
    "access_logs",
    "ai_safety_hits",
    "advisor_file_trash",
    "follow_repost_notifications",
    "pay_reconciliations",
)
# --cleanup 删的前缀（见文件开头）
CLEANUP_PREFIXES = ("exports/", "backups/", "logs/", "advisor/")
# 云托管 admin 每天往这几个前缀写文件的时间（北京时间，线上镜像的 crontab）：0:30 归档访问日志
# （logs/），4:00 整库备份（backups/）。从前 CLEANUP_BEFORE 分钟到后 CLEANUP_AFTER 分钟（任务可能
# 还在跑）都不清；清完要在下一次之前把 admin 调成 0（之后就没有定时任务了）
SCHEDULED_WRITES = ((0, 30), (4, 0))
CLEANUP_BEFORE = 20
CLEANUP_AFTER = 30
# 备份前记下的那份：--links-only 重出清单时拿来核对（同一个实例的 /tmp；实例换了就要整个重跑）
FINGERPRINT = Path("/tmp/youju-export-fingerprint.json")
MAX_AGE = 7200  # 下载链接 2 小时有效（微信文档的示例值）；过期了带 --links-only 重出一份清单
BATCH = 50  # 每次取多少个文件的下载链接
FORMAT = "youju-cloudrun-export"
# 不涉密的配置（服务器上照着填）：模型名、备案号、价格、商品编号、协议版本、模板、开关和保留天数。
# 不含任何密钥、数据库地址、openid 名单（ai_advisor_openids、pay_openids 不在这里）
CONFIG_KEYS = (
    "wx_appid",
    "wx_push_public",
    "template_deadline",
    "template_newdocs",
    "send_wx_messages",
    "agreement_version",
    "agreement_min_version",
    "retain_events_days",
    "retain_feedback_days",
    "retain_used_subscriptions_days",
    "retain_unused_subscriptions_days",
    "retain_pay_orders_days",
    "ai_base_url",
    "ai_model",
    "ai_request_extra",
    "ai_background_model",
    "ai_advisor_base_url",
    "ai_advisor_model",
    "ai_advisor_request_extra",
    "ai_advisor_stream",
    "ai_advisor_timeout_seconds",
    "ai_model_label",
    "ai_model_filing",
    "rewards_enabled",
    "advisor_enabled",
    "advisor_compare_enabled",
    "ai_advisor_enabled",
    "ai_advisor_audience",
    "ai_advisor_daily_cap",
    "ai_advisor_concurrency",
    "boost_trial_enabled",
    "season_boost_enabled",
    "season_boost_start",
    "season_boost_end",
    "pay_enabled",
    "pay_audience",
    "pay_terms_version",
    "pay_invoice_type",
    "pay_shutdown_notice",
    "renew_bonus_enabled",
    "correction_reward_enabled",
    "xpay_offer_id",
    "xpay_price_pass",
    "xpay_product_pass",
    "xpay_price_pass_90",
    "xpay_product_pass_90",
    "exports_per_10min",
    "xpay_platform_field",
    "xpay_ios_values",
    "xpay_android_values",
    "xpay_bill_order_column",
    "xpay_bill_fee_column",
    "xpay_bill_fee_unit",
    "xpay_bill_hosts",
    "wx_push_path",
)


# 故意不导的：密钥和口令、openid 名单、数据库地址和环境，和只在云托管或本机有意义的
# （抓取参数在本机，服务器的登录方式另配）。白名单和这里都没有、又和默认值不同的，导出时只列名字
CONFIG_SKIPPED = (
    "database_url",
    "admin_token",
    "alert_webhook",
    "ai_api_key",
    "ai_advisor_api_key",
    "xpay_appkey_prod",
    "xpay_appkey_sandbox",
    "wx_appsecret",
    "ai_advisor_openids",
    "pay_openids",
    "dev_openid",
    "cloud_env",
    "role",
    "trust_wx_headers",
    "user_agent",
    "request_timeout",
    "per_host_interval",
    "max_workers",
    "failure_alert_threshold",
    "sources_csv",
)
# 整库备份的文件名：和服务器上认的一样（youju/cloudrun_fetch.py 的 _BACKUP）
_BACKUP = re.compile(r"backups/youju-\d{8}\.jsonl\.gz")
# 下载链接的域名：和服务器上 youju export-fetch 认的一样（youju/cloudrun_fetch.py 的 URL）
LINK = re.compile(r"https://[A-Za-z0-9.-]+\.(?:tcb\.qcloud\.la|myqcloud\.com)/[!-~]*")


class ExportError(RuntimeError):
    pass


def run_command(args: list[str], runner=subprocess.run) -> None:
    print(f"== {' '.join(args)}", flush=True)
    done = runner(args)
    if done.returncode != 0:
        raise ExportError(f"STOP: {' '.join(args)} exit {done.returncode}")


def fingerprint(engine) -> dict:
    """用户数据各表的整表校验值（CHECKSUM TABLE）。哪张表这一版里没有、取不到校验值，都停下。"""
    from youju.models import Base

    missing = [name for name in WATCHED if name not in Base.metadata.tables]
    if missing:
        raise ExportError("这一版的表结构里没有：" + "、".join(missing))
    with engine.connect() as conn:
        rows = conn.execute(text("CHECKSUM TABLE " + ", ".join(f"`{n}`" for n in WATCHED))).all()
    marks = {str(table).split(".")[-1].strip("`"): checksum for table, checksum in rows}
    bad = [name for name in WATCHED if marks.get(name) is None]
    if bad:
        raise ExportError("这些表取不到整表校验值：" + "、".join(bad))
    return {name: marks[name] for name in WATCHED}


def changed_tables(before: dict, after: dict) -> list[str]:
    return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))


def list_with_sizes(
    client: httpx.Client, bucket: storage_list.Bucket, prefix: str
) -> list[tuple[str, int, str, str]]:
    """storage_list.list_objects 列文件（翻页、校验、临时密钥都用它的），顺带记下每个文件的大小和
    ETag：线上镜像的 list_objects 只返回名字和修改时间，这里在它解析每一页时把 Size、ETag 也读出来。
    ETag 文件内容一变就变，服务器重跑时拿它判断下好的是不是同一份。
    返回（路径，大小，修改时间，ETag）。"""
    sizes: dict[str, int] = {}
    etags: dict[str, str] = {}
    original = storage_list._parse

    def parse(content: bytes):
        result = original(content)  # 先让原来的解析校验这一页
        for item in ElementTree.fromstring(content):
            if storage_list._local(item.tag) != "Contents":
                continue
            key = storage_list._text(item, "Key") or ""
            size = (storage_list._text(item, "Size") or "").strip()
            if not size.isdigit():
                raise ExportError(f"文件列表里 {key} 的大小读不出来")
            sizes[key] = int(size)
            etags[key] = (storage_list._text(item, "ETag") or "").strip().strip('"')
        return result

    storage_list._parse = parse
    try:
        objects = storage_list.list_objects(client, bucket, prefix)
    finally:
        storage_list._parse = original
    return [
        (item.key, sizes[item.key], _utc(item.last_modified).isoformat(), etags[item.key])
        for item in objects
    ]


def pick_files(listed: dict[str, list[tuple]]) -> list[tuple]:
    """backups/ 只要最新一份整库备份（文件名里的日期最大的）；别的前缀全要。"""
    files: list[tuple] = []
    for prefix in PREFIXES:
        items = listed.get(prefix, [])
        if prefix == "backups/":
            dumps = sorted(item for item in items if _BACKUP.fullmatch(item[0]))
            if not dumps:
                raise ExportError("backups/ 下没有整库备份")
            items = dumps[-1:]
        files.extend(items)
    return files


def download_links(
    client: httpx.Client, env: str, file_ids: list[str], *, sleep=clock.sleep
) -> dict[str, str]:
    links: dict[str, str] = {}
    for start in range(0, len(file_ids), BATCH):
        part = file_ids[start : start + BATCH]
        payload = {"env": env, "file_list": [{"fileid": f, "max_age": MAX_AGE} for f in part]}
        data = backup._retrying(
            lambda payload=payload: backup._open_api(client, "/tcb/batchdownloadfile", payload),
            sleep,
        )
        items = data.get("file_list") or data.get("download_list") or []
        wanted = set(part)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("fileid") not in wanted:
                continue
            url = item.get("download_url") or item.get("url")
            if (
                item.get("status") in (None, 0)
                and isinstance(url, str)
                and url.startswith("https://")
            ):
                links[item["fileid"]] = url
        missing = [f for f in part if f not in links]
        if missing:
            raise ExportError(f"{len(missing)} 个文件没拿到下载链接（第一个：{missing[0]}）")
    return links


def _utc(value: datetime) -> datetime:
    """库里读出来的时间按 UTC 算（UTCDateTime 读出来本来就带时区；万一没带，不按容器时区换算）。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def takedown_rows(session: Session) -> list[dict]:
    """当前对用户不可见的文件，每份一行。下架按归并组算（takedown.available_condition）：
    组里有一份下架，整组都不可见，组里每一份的网址都带上（本机收进来的可能是其中任何一份）；
    下架时间、原因取组里最近一次下架的那一份。"""
    merge = aliased(DocumentMerge)
    master = func.coalesce(merge.canonical_id, Document.id)
    rows = session.execute(
        select(
            Document.url_key,
            Document.source_id,
            Document.taken_down_at,
            Document.takedown_reason,
            master.label("master"),
        )
        .outerjoin(merge, (merge.duplicate_id == Document.id) & merge.active.is_(True))
        .where(~available_condition())
        .order_by(Document.id)
    ).all()
    latest: dict[int, tuple[datetime, str | None]] = {}
    for row in rows:
        if row.taken_down_at is not None and (
            row.master not in latest or row.taken_down_at > latest[row.master][0]
        ):
            latest[row.master] = (row.taken_down_at, row.takedown_reason)
    now = datetime.now(UTC)
    result = []
    for row in rows:
        stamp, reason = latest.get(row.master, (now, None))
        result.append(
            {
                "url_key": row.url_key,
                "origin_source_id": row.source_id,
                "taken_down_at": _utc(stamp).isoformat(),
                "reason": reason or "其他",
            }
        )
    return result


def summary(session: Session, takedown_docs: int) -> dict:
    """只有数字：核对 D-127 的前提（1.3.1 没提审、没有真实用户），不打印任何人的信息。"""

    def count(model) -> int:
        return session.scalar(select(func.count()).select_from(model)) or 0

    last_visit = session.scalar(select(func.max(User.last_visited_at)))
    orders = Counter(
        {
            f"env{env}:{status}": n
            for env, status, n in session.execute(
                select(PayOrder.env, PayOrder.status, func.count()).group_by(
                    PayOrder.env, PayOrder.status
                )
            )
        }
    )
    refunds = {
        status: n
        for status, n in session.execute(
            select(PayOrder.refund_status, func.count())
            .where(PayOrder.refund_status.is_not(None))
            .group_by(PayOrder.refund_status)
        )
    }
    return {
        "users": count(User),
        "last_visit": _utc(last_visit).date().isoformat() if last_visit else None,
        "orders": dict(sorted(orders.items())),
        "refunds": dict(sorted(refunds.items())),
        "invoices": count(InvoiceRequest),
        "takedown_docs": takedown_docs,
        "takedown_events": count(DocumentTakedownEvent),
    }


def _plain(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _changed(settings, name: str) -> bool:
    field = type(settings).model_fields[name]
    default = field.default_factory() if field.default_factory else field.default
    return getattr(settings, name) != default


def config_values(settings) -> dict:
    """白名单里、和默认值不一样的配置（服务器的 .env 照着填）。"""
    fields = type(settings).model_fields
    return {
        f"YOUJU_{name.upper()}": _plain(getattr(settings, name))
        for name in CONFIG_KEYS
        if name in fields and _changed(settings, name)
    }


def config_unlisted(settings) -> list[str]:
    """白名单、不导的单子里都没有、又和默认值不同的配置：只列名字（值可能是密钥），人工看。"""
    listed = set(CONFIG_KEYS) | set(CONFIG_SKIPPED)
    return sorted(
        f"YOUJU_{name.upper()}"
        for name in type(settings).model_fields
        if name not in listed and _changed(settings, name)
    )


def _ascii(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# 交易记录的表（顺序就是文件里的顺序）
PAY_TABLES = (PayOrder, PayEvent, InvoiceRequest, PassReward, WorkbenchPass, PayReconciliation)
_IDENTITY_WORDS = ("openid", "unionid")
_NOTES = {
    "pay_orders": ("refund_note", "refund_due_note"),
    "invoice_requests": ("note",),
    "pass_rewards": ("note",),
}


def _pseudonym(key: str | None, openid: str | None) -> str | None:
    """和 access_log.log_uid 算法一样（访问日志里的去标识化编号）。密钥直接读，不调 log_key：
    那个函数库里没有密钥时会新建一个，这个脚本不往库里写。"""
    if not key or not openid:
        return None
    return hmac.new(bytes.fromhex(key), openid.encode(), hashlib.sha256).hexdigest()[:16]


def _deidentify(table: str, row: dict, pseudonyms: dict[int, str | None]) -> dict:
    user_id = row.pop("user_id", None)
    if table == "pay_orders" and not row.get("pseudonym"):
        row["pseudonym"] = pseudonyms.get(user_id) if user_id is not None else None
    elif table == "pay_events" and isinstance(row.get("summary"), dict):
        row["summary"] = {
            k: v
            for k, v in row["summary"].items()
            if not any(word in str(k).lower() for word in _IDENTITY_WORDS)
        }
    elif table == "invoice_requests":
        row.pop("email", None)  # 开出以后用不着联系邮箱
        if row.get("title_type") != "company":
            row["title"] = None  # 个人抬头就是姓名；单位抬头和税号留着对得上开出的发票
    elif table == "pass_rewards" and row.get("ref_type") in ("user_first_batch", "user_referral"):
        row["ref_id"] = None  # 记的是另一个人的用户编号
    elif table == "workbench_passes":
        row.pop("last_task_id", None)  # 指向这个人的任务
    # 人工填的备注可能写到个人信息：清空（退款原因、开票情况看结构化的字段）
    for column in _NOTES.get(table, ()):
        if column in row:
            row[column] = None
    return row


# 交易记录存多久按 D-127（现行隐私保护指引写的 3650 天）定，不看云托管上的 retain_pay_orders_days
PAY_RETAIN_DAYS = 3650


def pay_records(session: Session, retain_days: int) -> tuple[list[str], dict]:
    """交易记录（去标识化）：第一行是说明（格式、保存天数、最早一笔订单的下单时间、各表条数），
    后面每行 {"table": 表名, "row": 一行}。"""
    key = session.scalar(select(AppKey.value).where(AppKey.name == access_log.KEY_NAME))
    pseudonyms = {
        user_id: _pseudonym(key, openid)
        for user_id, openid in session.execute(select(User.id, User.openid))
    }
    lines: list[str] = []
    counts: dict[str, int] = {}
    for model in PAY_TABLES:
        table = model.__table__
        counts[table.name] = 0
        for row in session.execute(select(table).order_by(table.c.id)).mappings():
            data = {column: _plain(value) for column, value in row.items()}
            lines.append(
                _ascii({"table": table.name, "row": _deidentify(table.name, data, pseudonyms)})
            )
            counts[table.name] += 1
    # 保存期起算（隐私保护指引）：订单、退款、开票自下单起，赠送自发放之日起
    earliest = session.scalar(select(func.min(PayOrder.created_at)))
    earliest_gift = session.scalar(select(func.min(PassReward.granted_at)))
    header = {
        "format": "youju-pay-records",
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "retain_days": retain_days,
        "earliest_order_at": _utc(earliest).isoformat() if earliest else None,
        "earliest_gift_at": _utc(earliest_gift).isoformat() if earliest_gift else None,
        "counts": counts,
    }
    return [_ascii(header), *lines], header


def staging_logs(engine, workdir: Path, stamp: str) -> list[tuple[date, Path, int]]:
    """访问日志暂存表里还没归档的（archive-logs 只归档到昨天），按天导出成和归档一样的格式
    （access_log.export_day）。返回（日期，文件，条数），没有记录的日子不出文件。"""
    with engine.connect() as conn:
        first = conn.scalar(select(func.min(access_log.AccessLog.logged_at)))
    if first is None:
        return []
    today = datetime.now(UTC).astimezone(backup.BEIJING).date()
    day = _utc(first).astimezone(backup.BEIJING).date()
    out = []
    while day <= today:
        path = workdir / f"access-staging-{day:%Y%m%d}-{stamp}.jsonl.gz"
        with path.open("wb") as fh:
            rows, _ = access_log.export_day(engine, day, fh)
        if rows:
            out.append((day, path, rows))
        else:
            path.unlink()
        day += timedelta(days=1)
    return out


def _check_links(links) -> None:
    """服务器只认这两种域名的链接：域名不对现在就停下（不用等贴到服务器上才发现）。"""
    for link in links:
        if not LINK.fullmatch(link):
            host = link.split("/")[2] if link.count("/") >= 2 else "?"
            raise ExportError(f"下载链接的域名是 {host}，服务器脚本不认：先别往下做，告诉团队")


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return str(size)


def export(
    env: str,
    database_url: str,
    *,
    client: httpx.Client,
    settings,
    workdir: Path,
    before: dict | None = None,
) -> dict:
    """before：备份前记下的用户数据各表（见 fingerprint）；给了就在最后再记一次，对不上就停下。"""
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    bucket = storage_list.bucket_of(client, env)
    listed = {prefix: list_with_sizes(client, bucket, prefix) for prefix in PREFIXES}
    files = pick_files(listed)
    engine = create_mysql_engine(database_url)
    try:
        with engine.connect() as conn:
            revision = backup._revision(conn)
        with Session(engine) as session:
            rows = takedown_rows(session)
            numbers = summary(session, len(rows))
            pay_lines, pay_header = pay_records(session, PAY_RETAIN_DAYS)
        staged = staging_logs(engine, workdir, stamp)
        if before is not None:
            changed = changed_tables(before, fingerprint(engine))
            if changed:
                raise ExportError(
                    "备份以后用户数据有新写入（" + "、".join(changed) + "）：清单不出。"
                    "停掉新的写入（api 调成 0、admin 别有人在用），再不带 --links-only 重跑整个导出"
                )
    finally:
        engine.dispose()
    takedowns = workdir / f"takedowns-{stamp}.jsonl"
    takedowns.write_text("".join(_ascii(row) + "\n" for row in rows), encoding="ascii")
    takedowns_path = f"exports/{takedowns.name}"
    takedowns_id = backup.upload(client, env, takedowns_path, takedowns, what="下架清单")
    pay = workdir / f"pay-records-{stamp}.jsonl.gz"
    pay.write_bytes(gzip.compress("".join(line + "\n" for line in pay_lines).encode(), mtime=0))
    pay_path = f"exports/{pay.name}"
    pay_id = backup.upload(client, env, pay_path, pay, what="交易记录")
    staged_ids = [
        backup.upload(client, env, f"exports/{path.name}", path, what="访问日志暂存")
        for _, path, _ in staged
    ]
    ids = [bucket.prefix + item[0] for item in files]
    links = download_links(client, env, [*ids, takedowns_id, pay_id, *staged_ids])
    _check_links(links.values())
    manifest = {
        "format": FORMAT,
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "env": env,
        "revision": revision,
        "max_age": MAX_AGE,
        "files": [
            {
                "path": key,
                "size": size,
                "modified": modified,
                "etag": etag,
                "url": links[file_id],
            }
            for (key, size, modified, etag), file_id in zip(files, ids, strict=True)
        ],
        "takedowns": {
            "path": takedowns_path,
            "size": takedowns.stat().st_size,
            "sha256": hashlib.sha256(takedowns.read_bytes()).hexdigest(),
            "count": len(rows),
            "url": links[takedowns_id],
        },
        "pay_records": {
            "path": pay_path,
            "size": pay.stat().st_size,
            "sha256": hashlib.sha256(pay.read_bytes()).hexdigest(),
            "counts": pay_header["counts"],
            "earliest_order_at": pay_header["earliest_order_at"],
            "earliest_gift_at": pay_header["earliest_gift_at"],
            "retain_days": pay_header["retain_days"],
            "url": links[pay_id],
        },
        "staging_logs": [
            {
                "day": day.isoformat(),
                "path": f"exports/{path.name}",
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": rows,
                "url": links[file_id],
            }
            for (day, path, rows), file_id in zip(staged, staged_ids, strict=True)
        ],
        "summary": numbers,
        "config": config_values(settings),
        "config_unlisted": config_unlisted(settings),
    }
    local = workdir / f"manifest-{stamp}.json"
    local.write_text(_ascii(manifest) + "\n", encoding="ascii")
    manifest_id = backup.upload(client, env, f"exports/{local.name}", local, what="导出清单")
    url = download_links(client, env, [manifest_id])[manifest_id]
    _check_links([url])
    return {
        "manifest": manifest,
        "url": url,
        "sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
    }


def report(result: dict) -> None:
    manifest = result["manifest"]
    per_prefix: dict[str, list[int]] = {prefix: [0, 0] for prefix in PREFIXES}
    for item in manifest["files"]:
        prefix = next(p for p in PREFIXES if item["path"].startswith(p))
        per_prefix[prefix][0] += 1
        per_prefix[prefix][1] += item["size"]
    print("== 导出清单（各前缀的文件数和大小）")
    for prefix, (n, size) in per_prefix.items():
        print(f"  {prefix} {n} 个 {_human(size)}")
    backups = [i["path"] for i in manifest["files"] if i["path"].startswith("backups/")]
    print(f"  整库备份：{backups[0]}；迁移版本 {manifest['revision']}")
    print(f"  下架清单：{manifest['takedowns']['count']} 个网址")
    counts = manifest["pay_records"]["counts"]
    print(f"  交易记录（去标识化）：{'，'.join(f'{k} {v} 条' for k, v in counts.items())}")
    staged = manifest.get("staging_logs", [])
    print(
        f"  还没归档的访问日志：{'，'.join(f'{i["day"]} {i["rows"]} 条' for i in staged) or '没有'}"
    )
    print(f"  不涉密的配置 {len(manifest['config'])} 项（在清单里，服务器上照着填）")
    if manifest.get("config_unlisted"):
        names = ", ".join(manifest["config_unlisted"])
        print(f"  和默认值不同、没导出的配置（只列名字，人工看）：{names}")
    total = sum(size for _, size in per_prefix.values())
    files = ",".join(f"{p.rstrip('/')}:{n}/{size}" for p, (n, size) in per_prefix.items())
    print("== 以下几行只有英文和数字，复制以它们为准")
    print(f"EXPORT OK files={files} total={total} revision={manifest['revision']}")
    print(f"SUMMARY {_ascii(manifest['summary'])}")
    # 链接不写进命令：命令行参数别的用户看得到，命令还会进 shell 历史；运行后在提示里粘贴（不显示）
    print("== 在服务器的网页终端里先运行这一行：")
    print(f"sudo /opt/youju/bin/youju-shared.sh fetch-export {result['sha256']}")
    print(
        "== 提示'粘贴清单链接'时贴下面这一行（清单里有整库备份的下载链接，2 小时内有效；"
        "只贴到那里，不发到聊天里）："
    )
    print(result["url"])


def next_scheduled_write(now: datetime) -> datetime:
    """下一次往这几个前缀写文件的时间（北京时间，带时区）。"""
    local = now.astimezone(backup.BEIJING)
    upcoming = []
    for hour, minute in SCHEDULED_WRITES:
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
        upcoming.append(at)
    return min(upcoming)


def busy_with(now: datetime) -> datetime | None:
    """正赶上哪一次定时写文件（它的前 CLEANUP_BEFORE 到后 CLEANUP_AFTER 分钟之间）；没有就 None。"""
    local = now.astimezone(backup.BEIJING)
    for days in (-1, 0, 1):
        day = local + timedelta(days=days)
        for hour, minute in SCHEDULED_WRITES:
            at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            start = at - timedelta(minutes=CLEANUP_BEFORE)
            if start <= local <= at + timedelta(minutes=CLEANUP_AFTER):
                return at
    return None


# 往这几个前缀写文件的定时任务（job_runs 里记着）：还在跑就不清
WRITER_JOBS = ("backup", "archive-logs")


def running_writers(database_url: str, now: datetime) -> list[str]:
    """还在跑的 WRITER_JOBS：job_runs 里 status 是 running、3 小时内开始的（job.sh 单次最长
    3 小时）。"""
    from youju.models import JobRun

    engine = create_mysql_engine(database_url)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(JobRun.job).where(
                    JobRun.job.in_(WRITER_JOBS),
                    JobRun.status == "running",
                    JobRun.started_at > now - timedelta(hours=3),
                )
            ).all()
    finally:
        engine.dispose()
    return sorted(set(rows))


def cleanup_counts(client: httpx.Client, env: str) -> dict[str, int]:
    bucket = storage_list.bucket_of(client, env)
    return {
        prefix: len(storage_list.list_objects(client, bucket, prefix))
        for prefix in CLEANUP_PREFIXES
    }


def cleanup(client: httpx.Client, env: str, *, sleep=clock.sleep) -> dict[str, int]:
    """--cleanup：删掉 CLEANUP_PREFIXES 下的全部文件，删完再列一遍，还有剩的就报错（重跑接着删）。
    返回各前缀删了几个。"""
    bucket = storage_list.bucket_of(client, env)
    deleted: dict[str, int] = {}
    for prefix in CLEANUP_PREFIXES:
        keys = [item.key for item in storage_list.list_objects(client, bucket, prefix)]
        for start in range(0, len(keys), BATCH):
            part = [bucket.prefix + key for key in keys[start : start + BATCH]]
            backup._retrying(
                lambda part=part: backup.delete_files(client, env, part, what="云托管上的用户数据"),
                sleep,
            )
        deleted[prefix] = len(keys)
    left = {
        prefix: len(storage_list.list_objects(client, bucket, prefix))
        for prefix in CLEANUP_PREFIXES
    }
    if any(left.values()):
        raise ExportError(
            "还有没删掉的（"
            + "，".join(f"{prefix} {n} 个" for prefix, n in left.items() if n)
            + "）：再跑一次 --cleanup"
        )
    return deleted


def _not_now(at: datetime) -> bool:
    """正赶上云托管每天写文件：打印几点以后再贴，返回 True。"""
    busy = busy_with(at)
    if busy is None:
        return False
    resume = busy + timedelta(minutes=CLEANUP_AFTER)
    print(
        f"云托管 {busy:%H:%M} 写日志归档或整库备份：前 {CLEANUP_BEFORE} 分钟到后 "
        f"{CLEANUP_AFTER} 分钟不清"
    )
    print(f"CLEANUP NOT STARTED (paste the block again after {resume:%H:%M})")
    return True


def run_cleanup(env: str, database_url: str, *, ask, now) -> int:
    """--cleanup：先看时间（正赶上云托管每天写文件就不清）、列出要删几个、要输入 DELETE 才删；
    输入以后再看一次时间（在提示那里停久了也不会赶上）。"""
    if _not_now(now()):
        return 1
    try:
        with httpx.Client(trust_env=False) as client:
            counts = cleanup_counts(client, env)
            print("要删的文件数：" + " ".join(f"{prefix}{n}" for prefix, n in counts.items()))
            print("（服务器上 import-export 看到 IMPORT OK 以后才做这一步；attachments/ 不删）")
            try:
                answer = ask("Type DELETE and press Enter to delete them: ")
            except EOFError:
                answer = ""
            if answer.strip() != "DELETE":
                print("CLEANUP CANCELLED (nothing deleted)")
                return 1
            started = now()
            if _not_now(started):
                return 1
            busy = running_writers(database_url, started)
            if busy:
                print("云托管的定时任务 " + "、".join(busy) + " 还在跑")
                print("CLEANUP NOT STARTED (paste the block again in 10 minutes)")
                return 1
            upcoming = next_scheduled_write(started)
            deleted = cleanup(client, env)
    except (ExportError, backup.BackupError) as exc:
        print(f"清理没做完：{exc}")
        print("CLEANUP FAILED (paste the same block again)")
        return 1
    if now() >= upcoming:
        print(f"清理跨过了 {upcoming:%H:%M}（云托管可能又写了文件）")
        print("CLEANUP CROSSED A SCHEDULED WRITE (paste the same block again)")
        return 1
    print(
        "CLEANUP OK "
        + " ".join(f"{prefix}{n}" for prefix, n in deleted.items())
        + " left=0 (attachments/ kept)"
    )
    print(f"NOW set the admin service to 0 instances, before {upcoming:%H:%M} Beijing time")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    runner=subprocess.run,
    ask=input,
    now=lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(description="有据首发前搬家：云托管导出（D-127）")
    parser.add_argument(
        "--links-only",
        action="store_true",
        help="不再跑归档、备份、校验，只重新列文件、出一份新清单（链接过期时用）",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="服务器上 IMPORT OK 以后（手册第 9 步）：删掉对象存储里的用户数据，删完核对",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    env = settings.cloud_env
    if not env:
        print("没有 YOUJU_CLOUD_ENV：要在云托管 admin 服务的'登录实例'里运行")
        return 1
    if args.cleanup:
        if args.links_only:
            print("--cleanup 和 --links-only 不能一起用")
            return 1
        return run_cleanup(env, settings.database_url, ask=ask, now=now)
    try:
        if args.links_only:
            if not FINGERPRINT.is_file():
                raise ExportError(
                    f"找不到备份前记下的写入核对（{FINGERPRINT}）：不带 --links-only 重跑整个导出"
                )
            before = json.loads(FINGERPRINT.read_text(encoding="utf-8"))
        else:
            run_command(["youju", "archive-logs"], runner)
            engine = create_mysql_engine(settings.database_url)
            try:
                before = fingerprint(engine)
            finally:
                engine.dispose()
            FINGERPRINT.write_text(json.dumps(before), encoding="utf-8")
            run_command(["youju", "backup"], runner)
            run_command(["youju", "backup-verify"], runner)
        with (
            httpx.Client(trust_env=False) as client,
            tempfile.TemporaryDirectory() as tmp,
        ):
            result = export(
                env,
                settings.database_url,
                client=client,
                settings=settings,
                workdir=Path(tmp),
                before=before,
            )
    except (ExportError, backup.BackupError) as exc:
        print(f"导出没做完：{exc}")
        print("EXPORT FAILED（没有删任何东西；查明原因后重跑这个脚本）")
        return 1
    report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
