"""切换时搬对象存储里的文件（youju storage-export、storage-import；见 docs/部署/自建服务器.md）。

云托管的对象存储里有要跟着走的：批量工具结果（advisor/out/，用户 30 天内还能下载）、访问日志归档
（logs/，网络日志要留存不少于六个月）、附件副本（attachments/，本机也能重新下载，搬过来省得断档）。
备份（backups/）和公开快照（public-snapshots/）不搬：切换时另做一份整库备份恢复。

- 导出（在云托管 admin 的"登录实例"里跑）：按前缀列出文件、逐个下载、打成一个 tar.gz；
  不给 --output 时把这个包传回对象存储 exports/ 下，在云托管控制台下载（控制台一次只能下一个文件）。
- 导入（在新服务器上跑）：解开包，按原路径写进现在配的存储（disk、s3），已经有、大小一样的跳过，
  不一样的覆盖（可以重复导入）。
  文件 ID 只看路径（youju/storage.py），库里的旧文件 ID 不用改。
"""

from __future__ import annotations

import io
import logging
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from youju import storage, storage_list
from youju.backup import BackupError, _open_api, upload

log = logging.getLogger("youju.storage_move")
DEFAULT_PREFIXES = ("advisor/out/", "logs/", "attachments/")
MAX_FILE = 64 * 1024 * 1024


@dataclass
class MoveReport:
    files: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    skipped: int = 0
    output: str = ""


def _download(client: httpx.Client, env: str, file_id: str) -> bytes:
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
    response = client.get(url, timeout=300)
    if response.status_code != 200:
        raise BackupError(f"下载失败（HTTP {response.status_code}）")
    return response.content


def export(env: str, output: Path, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> MoveReport:
    report = MoveReport(output=str(output))
    with storage.http_client() as client, tarfile.open(output, "w:gz") as tar:
        bucket = storage_list.bucket_of(client, env)
        for prefix in prefixes:
            objects = storage_list.list_objects(client, bucket, prefix)
            count = 0
            for item in objects:
                if not storage.valid_path(item.key):
                    report.skipped += 1
                    continue
                content = _download(client, env, bucket.prefix + item.key)
                if len(content) > MAX_FILE:
                    report.skipped += 1
                    continue
                info = tarfile.TarInfo(item.key)
                info.size = len(content)
                info.mtime = int(item.last_modified.timestamp())
                tar.addfile(info, io.BytesIO(content))
                report.bytes += len(content)
                count += 1
            report.files[prefix] = count
    return report


def export_and_upload(env: str, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> MoveReport:
    """导出后传回对象存储 exports/storage-时间.tar.gz（在云托管控制台下载）。"""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "storage.tar.gz"
        report = export(env, local, prefixes)
        path = f"exports/storage-{datetime.now(UTC):%Y%m%d%H%M%S}.tar.gz"
        with storage.http_client() as client:
            report.output = upload(client, env, path, local, what="存储导出包")
    return report


def _size(store: storage.Backend, path: str) -> int | None:
    if isinstance(store, storage.DiskBackend):
        return store._file(path).stat().st_size
    return None  # s3：大小不知道，当作不一样，重写一遍


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
            if store.exists(path) and _size(store, path) == member.size:
                report.skipped += 1
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                report.skipped += 1
                continue
            local = Path(tmp) / "f"
            local.write_bytes(extracted.read())
            store.put_file(path, local)
            report.files[prefix] = report.files.get(prefix, 0) + 1
            report.bytes += member.size
    return report
