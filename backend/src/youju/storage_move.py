"""切换时搬对象存储里的文件（youju storage-export、storage-import；见 docs/部署/自建服务器.md）。

云托管的对象存储切换后跟着云托管一起注销，里面要跟着走的：批量工具结果（advisor/out/，用户 30 天内
还能下载）、访问日志归档（logs/，网络日志要留存不少于六个月）、附件副本（attachments/，本机也能
重新下载，搬过来省得断档）、近几天的整库备份（backups/，用 --since-days 只搬近的，切换后回退用）。
公开快照（public-snapshots/）不搬。

- 导出（在云托管 admin 的"登录实例"里跑）：按前缀列出文件、逐个下载、打成一个 tar.gz；
  不给 --output 时把这个包传回对象存储 exports/ 下，在云托管控制台下载（控制台一次只能下一个文件）。
- 导入（在新服务器上跑）：解开包，按原路径写进现在配的存储（disk、s3）。每个文件按块复制到临时文件
  （不整个读进内存）、边复制边算 SHA-256；服务器存储（disk）里已经有、内容一样的跳过，内容不一样的
  用包里的覆盖、记数（s3 读不到内容，照写一遍）。可以重复导入。
  文件 ID 只看路径（youju/storage.py），库里的旧文件 ID 不用改。
"""

from __future__ import annotations

import hashlib
import logging
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from youju import storage, storage_list
from youju.backup import BackupError, _open_api, upload

log = logging.getLogger("youju.storage_move")
DEFAULT_PREFIXES = ("advisor/out/", "logs/", "attachments/")
# 单个文件最大多少（整库备份最大，按块下载、不进内存）
MAX_FILE = 4 * 1024 * 1024 * 1024


@dataclass
class MoveReport:
    files: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    skipped: int = 0
    # 已经有、但内容和包里的不一样、用包里的覆盖的
    replaced: int = 0
    output: str = ""


def _download_to(client: httpx.Client, env: str, file_id: str, dest: Path) -> int:
    """按块下载到 dest（整库备份几百 MB，不读进内存），返回字节数。"""
    data = _open_api(
        client,
        "/tcb/batchdownloadfile",
        {"env": env, "file_list": [{"fileid": file_id, "max_age": 600}]},
    )
    items = data.get("file_list") or data.get("download_list") or []
    item = next((i for i in items if isinstance(i, dict)), None)
    url = (item or {}).get("download_url") or ""
    if not item or item.get("status") not in (None, 0) or not url.startswith("https://"):
        raise BackupError("没有拿到下载链接")
    size = 0
    with client.stream("GET", url, timeout=600) as response:
        if response.status_code != 200:
            raise BackupError(f"下载失败（HTTP {response.status_code}）")
        with dest.open("wb") as out:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_FILE:
                    raise BackupError("文件太大")
                out.write(chunk)
    return size


def export(
    env: str,
    output: Path,
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
    *,
    since_days: int | None = None,
    now: datetime | None = None,
) -> MoveReport:
    """since_days：只搬这么多天以内改过的（整库备份只搬近几天的）。"""
    report = MoveReport(output=str(output))
    cutoff = (now or datetime.now(UTC)) - timedelta(days=since_days) if since_days else None
    with (
        storage.http_client() as client,
        tarfile.open(output, "w:gz") as tar,
        tempfile.TemporaryDirectory() as tmp,
    ):
        bucket = storage_list.bucket_of(client, env)
        for prefix in prefixes:
            objects = storage_list.list_objects(client, bucket, prefix)
            count = 0
            for item in objects:
                if not storage.valid_path(item.key):
                    report.skipped += 1
                    continue
                if cutoff is not None and item.last_modified < cutoff:
                    continue
                local = Path(tmp) / "f"
                try:
                    size = _download_to(client, env, bucket.prefix + item.key, local)
                except BackupError as exc:
                    if str(exc) != "文件太大":
                        raise
                    report.skipped += 1
                    continue
                info = tar.gettarinfo(str(local), arcname=item.key)
                info.mtime = int(item.last_modified.timestamp())
                with local.open("rb") as fh:
                    tar.addfile(info, fh)
                report.bytes += size
                count += 1
            report.files[prefix] = count
    return report


def export_and_upload(
    env: str, prefixes: tuple[str, ...] = DEFAULT_PREFIXES, *, since_days: int | None = None
) -> MoveReport:
    """导出后传回对象存储 exports/storage-时间.tar.gz（在云托管控制台下载）。"""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "storage.tar.gz"
        report = export(env, local, prefixes, since_days=since_days)
        path = f"exports/storage-{datetime.now(UTC):%Y%m%d%H%M%S}.tar.gz"
        with storage.http_client() as client:
            report.output = upload(client, env, path, local, what="存储导出包")
    return report


CHUNK = 1024 * 1024


def _disk_sha256(store: storage.Backend, path: str) -> str | None:
    """服务器存储（disk）里已有文件的 SHA-256；没有、或 s3（读不到内容）返回 None。"""
    if not isinstance(store, storage.DiskBackend) or not store.exists(path):
        return None
    digest = hashlib.sha256()
    try:
        with store._file(path).open("rb") as fh:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
    except OSError:
        return None  # 读不了已有的：当作不知道，照写一遍
    return digest.hexdigest()


def import_archive(source: Path) -> MoveReport:
    """把导出包按原路径写进现在配的存储（disk、s3）。不是云托管存储才能导入。"""
    if not storage.emulated():
        raise BackupError("导入目标要是 disk 或 s3（YOUJU_STORAGE_BACKEND）")
    store = storage.backend()
    report = MoveReport(output=str(source))
    with tarfile.open(source, "r:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        for member in tar:
            if not member.isfile():
                continue
            path = member.name
            prefix = path.split("/", 1)[0] + "/"
            if not storage.valid_path(path) or member.size > MAX_FILE:
                report.skipped += 1
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                report.skipped += 1
                continue
            local = Path(tmp) / "f"
            digest = hashlib.sha256()
            with local.open("wb") as out:
                while chunk := extracted.read(CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
            existing = _disk_sha256(store, path)
            if existing == digest.hexdigest():
                report.skipped += 1
                continue
            if existing is not None:
                log.warning("%s 已经有、内容和包里的不一样：用包里的覆盖", path)
                report.replaced += 1
            store.put_file(path, local)
            report.files[prefix] = report.files.get(prefix, 0) + 1
            report.bytes += member.size
    return report
