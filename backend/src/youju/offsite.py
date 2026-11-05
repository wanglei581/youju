"""异地备份（2.0 自建服务器，youju offsite-backup）：把当天的整库备份加密后传到另一处对象存储。

- 来源：youju backup 每天 4:00 存在服务器本机存储里的 backups/youju-日期.jsonl.gz（保留 30 天，
  恢复最快的一份）。这里取最新的一份（最多往前找 2 天）。
- 加密：用 age（https://age-encryption.org，镜像里装了 age 命令）按创始人给的公钥加密。服务器上
  只有公钥，自己解不开；私钥由创始人离线保管，恢复时才用（docs/部署/自建服务器.md 第六节）。
- 存放：YOUJU_OFFSITE_S3_* 配的存储桶（创始人 11/5 定：腾讯云 COS 归档存储），路径
  backups/youju-日期.jsonl.gz.age，存储类型默认 ARCHIVE（归档的文件取回要先在控制台"恢复"）。
- 保留：删掉 YOUJU_OFFSITE_KEEP_DAYS（默认 30 天，和本机备份、隐私保护指引写的一致）以前的。
  归档的文件提前删也按存满 90 天收费；备份不大，多花几块钱，换注销用户的数据不在异地多留。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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


def _config(settings: Settings) -> storage.S3Config:
    keys = [
        k.get_secret_value().strip() if k else ""
        for k in (settings.offsite_s3_access_key, settings.offsite_s3_secret_key)
    ]
    if not (
        settings.offsite_s3_endpoint.startswith("https://")
        and settings.offsite_s3_region.strip()
        and settings.offsite_s3_bucket.strip()
        and all(keys)
        and settings.offsite_age_recipient.strip()
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
