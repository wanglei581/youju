"""异地备份（2.0 自建服务器，youju offsite-backup）：把当天的整库备份加密后传到另一处对象存储。

- 来源：youju backup 每天 4:00 存在服务器本机存储里的 backups/youju-日期.jsonl.gz（保留 30 天，
  恢复最快的一份）。这里取最新的一份（最多往前找 2 天）。
- 加密：用 age（https://age-encryption.org，镜像里装了 age 命令）按创始人给的公钥加密。服务器上
  只有公钥，自己解不开；私钥由创始人离线保管，恢复时才用（docs/部署/自建服务器.md 第六节）。
- 存放：YOUJU_OFFSITE_S3_* 配的存储桶（腾讯云 COS），路径 backups/youju-日期.jsonl.gz.age。
- 保留：删掉 YOUJU_OFFSITE_KEEP_DAYS（默认 30 天，和本机备份、隐私保护指引写的一致）以前的。
- 存储类型默认低频（STANDARD_IA，YOUJU_OFFSITE_STORAGE_CLASS）。创始人 11/5 说的是归档，做的时候
  改成低频：备份只留 30 天，归档最短按 90 天计费；归档读之前要先"恢复"（标准取回 3–5 小时），
  本机每天拉不到密文副本（youju offsite-pull，创始人 11/5：备份在本机再留一份）。低频最短按
  30 天计费、能直接读（腾讯云《存储类型概述》，2026-10-12 版）。备份不大，两种每月差几毛钱。
- 本机副本（youju offsite-pull）：在本机跑，用只能列表和下载的子账号密钥，把最新一份密文下到本机
  存储 offsite-backups/，保留同样天数。本机没有私钥，解不开。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from youju import storage
from youju.backup import BEIJING, BackupError, object_path
from youju.config import Settings

PREFIX = "backups/"
_NAME = re.compile(r"backups/youju-(\d{8})\.jsonl\.gz\.age")


@dataclass
class OffsiteReport:
    source: str = ""
    target: str = ""
    bytes: int = 0
    pruned: int = 0


def _config(settings: Settings, *, need_recipient: bool = True) -> storage.S3Config:
    keys = [
        k.get_secret_value().strip() if k else ""
        for k in (settings.offsite_s3_access_key, settings.offsite_s3_secret_key)
    ]
    if not (
        settings.offsite_s3_endpoint.startswith("https://")
        and settings.offsite_s3_region.strip()
        and settings.offsite_s3_bucket.strip()
        and all(keys)
        and (settings.offsite_age_recipient.strip() or not need_recipient)
    ):
        raise BackupError(
            "异地备份要配 YOUJU_OFFSITE_S3_ENDPOINT（https）、_REGION、_BUCKET、_ACCESS_KEY、"
            "_SECRET_KEY 和 YOUJU_OFFSITE_AGE_RECIPIENT"
        )
    return storage.S3Config(
        endpoint=settings.offsite_s3_endpoint.rstrip("/"),
        region=settings.offsite_s3_region.strip(),
        bucket=settings.offsite_s3_bucket.strip(),
        access_key=keys[0],
        secret_key=keys[1],
        path_style=settings.offsite_s3_path_style,
    )


def storage_class_header(endpoint: str, value: str) -> dict[str, str]:
    """COS 认自己的 x-cos-storage-class；别的兼容 S3 的用 x-amz-storage-class。"""
    if not value:
        return {}
    name = "x-cos-storage-class" if ".myqcloud.com" in endpoint else "x-amz-storage-class"
    return {name: value}


def encrypt(source: Path, target: Path, recipient: str) -> None:
    binary = shutil.which("age")
    if binary is None:
        raise BackupError("没有 age 命令：用自建服务器的镜像（Dockerfile 的 selfhost 阶段）")
    try:
        subprocess.run(
            [binary, "-r", recipient.strip(), "-o", str(target), str(source)],
            check=True,
            capture_output=True,
            timeout=3600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # age 的报错只说公钥不对、写不进去这类，不含备份内容
        lines = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = f"（{lines[-1][:200]}）" if lines else ""
        raise BackupError(f"加密备份失败：{type(exc).__name__}{detail}") from None


def _latest_local(settings: Settings, today: date) -> tuple[str, Path] | None:
    store = storage.backend(settings)
    for days in range(0, 3):
        path = object_path(today - timedelta(days=days))
        if store.exists(path):
            if isinstance(store, storage.DiskBackend):
                return path, store._file(path)
            raise BackupError("异地备份只支持从服务器本机存储（disk）取备份")
    return None


def run(
    settings: Settings,
    *,
    now: datetime | None = None,
    backend: storage.S3Backend | None = None,
    encryptor=encrypt,
) -> OffsiteReport:
    config = _config(settings)
    target = backend or storage.S3Backend(config)
    now = now or datetime.now(UTC)
    today = now.astimezone(BEIJING).date()
    found = _latest_local(settings, today)
    if found is None:
        raise BackupError("服务器本机存储里近 3 天没有整库备份（先看 youju backup 跑了没有）")
    path, local = found
    report = OffsiteReport(source=path, target=path + ".age")
    with tempfile.TemporaryDirectory() as tmp:
        sealed = Path(tmp) / (Path(path).name + ".age")
        encryptor(local, sealed, settings.offsite_age_recipient)
        report.bytes = sealed.stat().st_size
        try:
            target.put_file(
                report.target,
                sealed,
                headers=storage_class_header(config.endpoint, settings.offsite_storage_class),
            )
        except storage.StorageError as exc:
            raise BackupError(f"上传异地备份失败：{exc}") from None
    # 每份从备份那天起正好留满保留天数：第 N 天删（D-129，Codex：照旧的删法要到第 N+1 天才删）
    cutoff = today - timedelta(days=settings.offsite_keep_days - 1)
    try:
        for item in target.list(PREFIX):
            match = _NAME.fullmatch(item.key)
            if match and datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
                target.delete(item.key)
                report.pruned += 1
    except storage.StorageError as exc:
        raise BackupError(f"今天的异地备份已上传，但清理旧的失败：{exc}") from None
    return report


# 首发前搬家（D-127）：云托管的旧库不恢复进新库，加密存档在 archive/ 下（不进上面按天清理的
# backups/）。文件名里写到期日，到期由每天的 youju archive-prune 删（本机存储和异地各删各的），
# 删了什么记在 archive/deletions.jsonl。隐私保护指引定的期限：
# - 整库备份按数据库备份算："数据库备份中的副本在最长 30 天的备份周期内随备份清除"，
#   自备份那天起最长 30 天；
# - 交易记录（订单、退款、开票、赠送，导出时已去标识化）：按起算日分份，每份最长 3650 天；
# - 批量工具结果 30 天，导进来的云托管访问日志 190 天。
# 到期日都用 expiry() 算：archive-prune 每天 5:50 删到期日那天的，起算那天最早的一条到删的时候也
# 不超过规定的天数（最晚的一条少留不到两天）。
ARCHIVE_PREFIX = "archive/"
ARCHIVE_RECORD = ARCHIVE_PREFIX + "deletions.jsonl"
# 云托管里的数据库最晚哪天删（最后一份整库备份起 30 天，D-127），和删了没有（谁、哪天、怎么删的）
CLOUDRUN_DEADLINE = ARCHIVE_PREFIX + "cloudrun-deadline.json"
CLOUDRUN_GONE = ARCHIVE_PREFIX + "cloudrun-gone.json"
REMIND_DAYS = 7
# 名字最长 151 个字符：首发前搬家的存档名里带完整的清单 SHA-256（见 cloudrun_fetch.archive_export）
_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,150}")
# 加密的存档（.age），和导进来的云托管访问日志（不加密，和服务器自己的访问日志归档一样）
_ARCHIVED = re.compile(
    r"archive/([A-Za-z0-9][A-Za-z0-9_-]{0,150})\.expires-(\d{8})\.(?:jsonl|tar)\.gz(?:\.age)?"
)
_EXTS = ("jsonl.gz", "tar.gz")
BACKUP_KEEP_DAYS = 30


def expiry(start: date, keep_days: int) -> date:
    """按天算的存档的到期日：起算日加 keep_days - 1 天（见上面）。"""
    return start + timedelta(days=keep_days - 1)


@dataclass
class ArchiveReport:
    target: str = ""
    bytes: int = 0
    expires: date | None = None


def archive_name(stem: str, expires: date, ext: str = "jsonl.gz") -> str:
    if not _STEM.fullmatch(stem) or ext not in _EXTS:
        raise BackupError("存档名字只能是字母、数字、横线、下划线，扩展名 .jsonl.gz 或 .tar.gz")
    return f"{ARCHIVE_PREFIX}{stem}.expires-{expires:%Y%m%d}.{ext}.age"


def archive(
    settings: Settings,
    source: Path,
    stem: str,
    expires: date,
    *,
    ext: str = "jsonl.gz",
    today: date | None = None,
    backend: storage.S3Backend | None = None,
    encryptor=encrypt,
) -> ArchiveReport:
    """把一个 .jsonl.gz 用 age 公钥加密，存到本机存储和异地的 archive/<stem>.expires-<到期日>…，
    只有创始人的私钥能解开；可以重跑（覆盖同名的密文）。异地没配好就不做：调用方存完会删掉明文，
    之后就没法再补传异地那一份了。"""
    today = today or datetime.now(UTC).astimezone(BEIJING).date()
    target_path = archive_name(stem, expires, ext)
    if expires <= today:
        raise BackupError(f"到期日 {expires} 已经过了，不存")
    recipient = (settings.offsite_age_recipient or "").strip()
    if not recipient:
        raise BackupError("还没填 age 公钥（YOUJU_OFFSITE_AGE_RECIPIENT）")
    if not (
        settings.offsite_s3_bucket.strip()
        and settings.offsite_s3_access_key
        and settings.offsite_s3_secret_key
    ):
        raise BackupError("异地备份还没配好（YOUJU_OFFSITE_S3_*）：配好再存档，明文先留着")
    if not Path(source).is_file():
        raise BackupError(f"没有这个文件：{source}")
    report = ArchiveReport(target=target_path, expires=expires)
    with tempfile.TemporaryDirectory() as tmp:
        sealed = Path(tmp) / Path(target_path).name
        encryptor(Path(source), sealed, recipient)
        report.bytes = sealed.stat().st_size
        try:
            storage.backend(settings).put_file(target_path, sealed)
        except storage.StorageError as exc:
            raise BackupError(f"存到本机存储失败：{exc}") from None
        config = _config(settings)
        target = backend or storage.S3Backend(config)
        try:
            target.put_file(
                target_path,
                sealed,
                headers=storage_class_header(config.endpoint, settings.offsite_storage_class),
            )
        except storage.StorageError as exc:
            raise BackupError(f"本机存储已存好，传异地失败（重跑这条命令）：{exc}") from None
    return report


@dataclass
class PruneReport:
    deleted: list[str] = field(default_factory=list)
    kept: int = 0


def cloudrun_reminder(local: storage.DiskBackend, today: date) -> str:
    """云托管的库还没记"删了"、离最晚那天不到 7 天（或已经过了）时的提醒；不用提醒返回空串。
    delete_by 是最晚删的那天（含），见 cloudrun_fetch.archive_export。"""
    if not local.exists(CLOUDRUN_DEADLINE) or local.exists(CLOUDRUN_GONE):
        return ""
    try:
        delete_by = date.fromisoformat(
            json.loads(local._file(CLOUDRUN_DEADLINE).read_text(encoding="utf-8"))["delete_by"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "云托管删除期限的记录读不出来：看一眼 archive/cloudrun-deadline.json"
    if today < delete_by - timedelta(days=REMIND_DAYS):
        return ""
    late = "已经过了期限，" if today > delete_by else ""
    return (
        f"云托管里的数据库最晚 {delete_by} 删掉（隐私保护指引，D-127）：{late}注销环境，"
        "或在云托管控制台删库（对象存储里的用户数据第 9 步已用 --cleanup 清掉；"
        "admin 后来又起来过的话再清一次）；删了以后运行 youju-shared.sh cloudrun-gone "
        "deregistered（注销）或 deleted（手动删）记下来"
    )


def cloudrun_reminder_for(settings: Settings, *, today: date | None = None) -> str:
    """archive-prune 先单独调它（不受后面删存档出错的影响）；存储不在本机就没有这份记录。"""
    local = storage.backend(settings)
    if not isinstance(local, storage.DiskBackend):
        return ""
    return cloudrun_reminder(local, today or datetime.now(UTC).astimezone(BEIJING).date())


def record_cloudrun_gone(settings: Settings, how: str, *, now: datetime | None = None) -> str:
    if how not in ("deregistered", "deleted"):
        raise BackupError("只能是 deregistered（注销了环境）或 deleted（手动删了库）")
    local = storage.backend(settings)
    with tempfile.TemporaryDirectory() as tmp:
        record = Path(tmp) / "gone.json"
        record.write_text(
            json.dumps({"at": (now or datetime.now(UTC)).isoformat(), "how": how}) + "\n",
            encoding="utf-8",
        )
        local.put_file(CLOUDRUN_GONE, record)
    return CLOUDRUN_GONE


def _expired(store, where: str, today: date, report: PruneReport, records: list[dict]) -> None:
    for item in store.list(ARCHIVE_PREFIX):
        match = _ARCHIVED.fullmatch(item.key)
        if not match:
            continue
        expires = datetime.strptime(match.group(2), "%Y%m%d").date()
        if expires > today:
            report.kept += 1
            continue
        store.delete(item.key)
        report.deleted.append(f"{where}:{item.key}")
        records.append(
            {
                "deleted_at": datetime.now(UTC).isoformat(),
                "where": where,
                "path": item.key,
                "expires": expires.isoformat(),
            }
        )


def prune_archive(
    settings: Settings,
    *,
    today: date | None = None,
    backend: storage.S3Backend | None = None,
) -> PruneReport:
    """删掉 archive/ 下到期的存档（本机存储；配了异地也删异地的），每删一个在本机存储的
    archive/deletions.jsonl 记一行（什么时候、哪里、哪个文件、到期日）。每天由定时任务跑。"""
    today = today or datetime.now(UTC).astimezone(BEIJING).date()
    report = PruneReport()
    records: list[dict] = []
    local = storage.backend(settings)
    if not isinstance(local, storage.DiskBackend):
        raise BackupError("存档只放在服务器本机存储（disk）")
    try:
        _expired(local, "local", today, report, records)
        if settings.offsite_s3_bucket.strip():
            _expired(
                backend or storage.S3Backend(_config(settings, need_recipient=False)),
                "offsite",
                today,
                report,
                records,
            )
    except storage.StorageError as exc:
        raise BackupError(f"删到期存档失败（已删的照样记下）：{exc}") from None
    finally:
        if records:
            path = local._file(ARCHIVE_RECORD)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for row in records:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return report


LOCAL_PREFIX = "offsite-backups/"


def pull(
    settings: Settings,
    *,
    now: datetime | None = None,
    backend: storage.S3Backend | None = None,
    http: httpx.Client | None = None,
) -> OffsiteReport:
    """本机：把异地存储里最新的一份加密备份下到本机存储 offsite-backups/（已经有就不下），
    删掉本机 YOUJU_OFFSITE_KEEP_DAYS 天以前的。下的是密文，本机解不开。"""
    config = _config(settings, need_recipient=False)
    source = backend or storage.S3Backend(config)
    local = storage.backend(settings)
    now = now or datetime.now(UTC)
    report = OffsiteReport()
    try:
        candidates = sorted(item.key for item in source.list(PREFIX) if _NAME.fullmatch(item.key))
    except storage.StorageError as exc:
        raise BackupError(f"列异地备份失败：{exc}") from None
    if not candidates:
        raise BackupError("异地存储里还没有备份")
    newest = candidates[-1]
    target = LOCAL_PREFIX + newest[len(PREFIX) :]
    report.source, report.target = newest, target
    if not local.exists(target):
        client = http or httpx.Client(trust_env=False, timeout=600)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                file = Path(tmp) / "backup.age"
                with client.stream("GET", source.url(newest, 600)) as response:
                    if response.status_code != 200:
                        raise BackupError(
                            f"下载异地备份失败（HTTP {response.status_code}；"
                            "存储类型是归档的要先在控制台恢复，建议用低频）"
                        )
                    with file.open("wb") as out:
                        for chunk in response.iter_bytes():
                            out.write(chunk)
                report.bytes = file.stat().st_size
                local.put_file(target, file)
        except httpx.HTTPError as exc:
            raise BackupError(f"下载异地备份失败：{type(exc).__name__}") from None
        finally:
            if http is None:
                client.close()
    # 和异地那份一样，第 N 天删
    cutoff = now.astimezone(BEIJING).date() - timedelta(days=settings.offsite_keep_days - 1)
    for item in local.list(LOCAL_PREFIX):
        match = re.fullmatch(r"offsite-backups/youju-(\d{8})\.jsonl\.gz\.age", item.key)
        if match and datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
            local.delete(item.key)
            report.pruned += 1
    return report
