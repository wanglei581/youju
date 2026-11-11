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

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
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
        raise BackupError(f"加密备份失败：{type(exc).__name__}") from None


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
    cutoff = today - timedelta(days=settings.offsite_keep_days)
    try:
        for item in target.list(PREFIX):
            match = _NAME.fullmatch(item.key)
            if match and datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
                target.delete(item.key)
                report.pruned += 1
    except storage.StorageError as exc:
        raise BackupError(f"今天的异地备份已上传，但清理旧的失败：{exc}") from None
    return report


# 首发前搬家（D-127）：云托管的最后一份整库备份不恢复进新库，加密后归档（交易记录按《电子商务法》
# 第三十一条留 3 年）。放在 archive/ 下，不进上面按天清理的 backups/，到期手动删
ARCHIVE_PREFIX = "archive/"
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.jsonl\.gz")


@dataclass
class ArchiveReport:
    target: str = ""
    bytes: int = 0
    offsite: bool = False


def archive(
    settings: Settings,
    source: Path,
    name: str,
    *,
    backend: storage.S3Backend | None = None,
    encryptor=encrypt,
) -> ArchiveReport:
    """把一份整库备份用 age 公钥加密，存到本机存储 archive/，再传一份到异地 archive/。
    只有创始人的私钥能解开；可以重跑（覆盖同名的密文）。异地没配好就不做：调用方归档完会删掉明文，
    之后就没法再补传异地那一份了。"""
    if not _ARCHIVE_NAME.fullmatch(name):
        raise BackupError("归档名字只能是字母、数字、点、横线、下划线，以 .jsonl.gz 结尾")
    recipient = (settings.offsite_age_recipient or "").strip()
    if not recipient:
        raise BackupError("还没填 age 公钥（YOUJU_OFFSITE_AGE_RECIPIENT）")
    if not (
        settings.offsite_s3_bucket.strip()
        and settings.offsite_s3_access_key
        and settings.offsite_s3_secret_key
    ):
        raise BackupError("异地备份还没配好（YOUJU_OFFSITE_S3_*）：配好再归档，明文备份先留着")
    if not Path(source).is_file():
        raise BackupError(f"没有这个文件：{source}")
    report = ArchiveReport(target=ARCHIVE_PREFIX + name + ".age")
    with tempfile.TemporaryDirectory() as tmp:
        sealed = Path(tmp) / (name + ".age")
        encryptor(Path(source), sealed, recipient)
        report.bytes = sealed.stat().st_size
        try:
            storage.backend(settings).put_file(report.target, sealed)
        except storage.StorageError as exc:
            raise BackupError(f"存到本机存储失败：{exc}") from None
        config = _config(settings)
        target = backend or storage.S3Backend(config)
        try:
            target.put_file(
                report.target,
                sealed,
                headers=storage_class_header(config.endpoint, settings.offsite_storage_class),
            )
        except storage.StorageError as exc:
            raise BackupError(f"本机存储已存好，传异地失败（重跑这条命令）：{exc}") from None
        report.offsite = True
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
    cutoff = now.astimezone(BEIJING).date() - timedelta(days=settings.offsite_keep_days)
    for item in local.list(LOCAL_PREFIX):
        match = re.fullmatch(r"offsite-backups/youju-(\d{8})\.jsonl\.gz\.age", item.key)
        if match and datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
            local.delete(item.key)
            report.pruned += 1
    return report
