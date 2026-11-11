"""有据首发前搬家（D-127）：云托管导出。在云托管 admin 服务的"登录实例"终端里运行。

线上镜像（迁移 0046）没有搬家用的命令，这个脚本只用镜像里已有的命令和函数：
youju archive-logs / backup / backup-verify，backup.upload、backup._open_api，
storage_list.bucket_of、list_objects，takedown.available_condition，和那一版的表结构。

按顺序做（不删、不改任何数据；只往对象存储写当天的备份、日志归档和 exports/ 下的三个文件）：
1. youju archive-logs、youju backup、youju backup-verify（和每天定时任务做的一样）；
   --links-only 跳过这一步（清单里的链接过期了、只要重出一份清单时用）
2. 列出 backups/（只要最新一份）、logs/、attachments/、advisor/out/ 下的文件和大小
3. 下架清单 exports/takedowns-<时间>.jsonl：当前对用户不可见的文件（整组下架的每一份）的规范网址键、
   来源编号、下架时间、原因类别。不带备注、操作人、标题
4. 交易记录 exports/pay-records-<时间>.jsonl.gz：订单、退款（在订单里）、支付流水、开票申请、
   赠送天数、使用期、每日对账。隐私保护指引写的是这些记录"与账号断开，去标识化、限制访问"，
   自下单起保存 retain_pay_orders_days（3650）天；这里照注销账号时（payments.anonymize、
   rewards.anonymize）的口径对所有人做一遍，另外去掉开票的联系邮箱和个人抬头（就是姓名）
5. 给每个文件取 2 小时有效的下载链接，连同汇总数（只有数字）和不涉密的配置写成清单
   exports/manifest-<时间>.json
6. 打印一行给服务器网页终端用的命令（带清单的下载链接和 SHA-256）、各前缀的文件数和大小、汇总数

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
from datetime import UTC, date, datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx
from sqlalchemy import func, select
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
COMMANDS = (["youju", "archive-logs"], ["youju", "backup"], ["youju", "backup-verify"])
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
# 下载链接的域名：和服务器上 youju export-fetch 认的一样（youju/cloudrun_fetch.py 的 URL）
LINK = re.compile(r"https://[A-Za-z0-9.-]+\.(?:tcb\.qcloud\.la|myqcloud\.com)/[!-~]*")


class ExportError(RuntimeError):
    pass


def run_commands(runner=subprocess.run) -> None:
    for args in COMMANDS:
        print(f"== {' '.join(args)}", flush=True)
        done = runner(args)
        if done.returncode != 0:
            raise ExportError(f"STOP: {' '.join(args)} exit {done.returncode}")


def list_with_sizes(
    client: httpx.Client, bucket: storage_list.Bucket, prefix: str
) -> list[tuple[str, int]]:
    """storage_list.list_objects 列文件（翻页、校验、临时密钥都用它的），顺带记下每个文件的大小：
    线上镜像的 list_objects 只返回名字和修改时间，这里在它解析每一页时把 Size 也读出来。"""
    sizes: dict[str, int] = {}
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
        return result

    storage_list._parse = parse
    try:
        objects = storage_list.list_objects(client, bucket, prefix)
    finally:
        storage_list._parse = original
    return [(item.key, sizes[item.key]) for item in objects]


def pick_files(listed: dict[str, list[tuple[str, int]]]) -> list[tuple[str, int]]:
    """backups/ 只要最新一份整库备份（文件名里的日期最大的）；别的前缀全要。"""
    files: list[tuple[str, int]] = []
    for prefix in PREFIXES:
        items = listed.get(prefix, [])
        if prefix == "backups/":
            dumps = sorted(
                (key, size)
                for key, size in items
                if key.startswith("backups/youju-") and key.endswith(".jsonl.gz")
            )
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
    earliest = session.scalar(select(func.min(PayOrder.created_at)))
    header = {
        "format": "youju-pay-records",
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "retain_days": retain_days,
        "earliest_order_at": _utc(earliest).isoformat() if earliest else None,
        "counts": counts,
    }
    return [_ascii(header), *lines], header


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


def export(env: str, database_url: str, *, client: httpx.Client, settings, workdir: Path) -> dict:
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
            pay_lines, pay_header = pay_records(session, settings.retain_pay_orders_days)
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
    ids = [bucket.prefix + key for key, _ in files]
    links = download_links(client, env, [*ids, takedowns_id, pay_id])
    _check_links(links.values())
    manifest = {
        "format": FORMAT,
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "env": env,
        "revision": revision,
        "max_age": MAX_AGE,
        "files": [
            {"path": key, "size": size, "url": links[file_id]}
            for (key, size), file_id in zip(files, ids, strict=True)
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
            "retain_days": pay_header["retain_days"],
            "url": links[pay_id],
        },
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


def main(argv: list[str] | None = None, *, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description="有据首发前搬家：云托管导出（D-127）")
    parser.add_argument(
        "--links-only",
        action="store_true",
        help="不再跑归档、备份、校验，只重新列文件、出一份新清单（链接过期时用）",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    env = settings.cloud_env
    if not env:
        print("没有 YOUJU_CLOUD_ENV：要在云托管 admin 服务的'登录实例'里运行")
        return 1
    try:
        if not args.links_only:
            run_commands(runner)
        with (
            httpx.Client(trust_env=False) as client,
            tempfile.TemporaryDirectory() as tmp,
        ):
            result = export(
                env, settings.database_url, client=client, settings=settings, workdir=Path(tmp)
            )
    except (ExportError, backup.BackupError) as exc:
        print(f"导出没做完：{exc}")
        print("EXPORT FAILED（没有删任何东西；查明原因后重跑这个脚本）")
        return 1
    report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
