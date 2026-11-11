"""首发前搬家（D-127）：服务器按云托管导出的清单下载文件（youju export-fetch）。

清单由 deploy/selfhost/shared/cloudrun-export.py 在云托管上生成：每个文件的路径、大小和 2 小时有效的
下载链接，下架清单的 SHA-256，汇总数（只有数字）和不涉密的配置。清单的 SHA-256 和链接一起从云托管
终端复制过来，先核对清单，再按清单逐个下载、核对：

- logs/、attachments/（甲方案加 --with-advisor 再带 advisor/out/）：核对大小，附件再核对
  文件名里的 SHA-256，.gz 整个解压一遍（gzip 自带校验）；对了才写进现在的存储（服务器硬盘）。
  已有、大小一样的跳过。
- 最新一份整库备份、下架清单：写到 --dir（服务器上只有 root 能进的目录），不进存储。备份核对
  大小和 gzip，下架清单核对 SHA-256。备份接着由 youju-shared.sh import-export 试恢复、加密归档、
  删掉明文。
- 哪个文件不对就不写、记下来，最后列出、退出码 1；重跑只下还没下好的。链接过期（HTTP 403）时
  在云托管上带 --links-only 重出一份清单。

--dir 里留下 summary.json（汇总数、配置、各前缀的文件数，不含链接）给 youju-shared.sh
export-show 看；manifest.json（含链接）在备份归档后删掉。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from youju import storage

FORMAT = "youju-cloudrun-export"
URL = re.compile(r"https://[A-Za-z0-9.-]+\.(?:tcb\.qcloud\.la|myqcloud\.com)/[!-~]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
_PATH = re.compile(r"(?:logs|attachments|advisor/out|backups)/[A-Za-z0-9_][A-Za-z0-9._/-]*")
_BACKUP = re.compile(r"backups/youju-\d{8}\.jsonl\.gz")
_ATTACHMENT = re.compile(r"attachments/([0-9a-f]{64})\.[A-Za-z0-9]{1,10}")
_CHUNK = 1024 * 1024
MAX_MANIFEST = 64 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024 * 1024


class FetchError(RuntimeError):
    pass


@dataclass
class FetchReport:
    fetched: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup: str = ""
    takedowns: int = 0


def _prefix(path: str) -> str:
    return "advisor/out/" if path.startswith("advisor/out/") else path.split("/", 1)[0] + "/"


def _download(client: httpx.Client, url: str, dest: Path, limit: int) -> int:
    size = 0
    with client.stream("GET", url, timeout=600) as response:
        if response.status_code != 200:
            hint = (
                "（链接过期了：在云托管上带 --links-only 重出一份清单）"
                if response.status_code == 403
                else ""
            )
            raise FetchError(f"HTTP {response.status_code}{hint}")
        with dest.open("wb") as out:
            for chunk in response.iter_bytes(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise FetchError("比清单里写的大")
                out.write(chunk)
    return size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _check_gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(_CHUNK):
                pass
    except (OSError, EOFError, zlib.error) as exc:
        raise FetchError("gzip 解不开（文件不完整或损坏）") from exc


def load_manifest(client: httpx.Client, url: str, sha256: str, dest: Path) -> dict:
    """下载清单、核对 SHA-256、检查格式。dest 已经是这一份（重跑）就不再下载。"""
    if not URL.fullmatch(url):
        raise FetchError("清单链接不对：要是云托管存储的 https 链接")
    if not SHA256.fullmatch(sha256):
        raise FetchError("清单的 SHA-256 要是 64 位小写十六进制")
    if not (dest.is_file() and _sha256(dest) == sha256):
        tmp = dest.with_name(dest.name + ".part")
        try:
            _download(client, url, tmp, MAX_MANIFEST)
        except FetchError as exc:
            tmp.unlink(missing_ok=True)
            raise FetchError(f"下载清单失败：{exc}") from None
        if _sha256(tmp) != sha256:
            tmp.unlink(missing_ok=True)
            raise FetchError("清单的 SHA-256 对不上：复制的那一行不完整，或者不是同一份清单")
        os.replace(tmp, dest)
    manifest = json.loads(dest.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT or manifest.get("version") != 1:
        raise FetchError("不是云托管导出脚本出的清单")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise FetchError("清单里没有文件列表")
    for item in files:
        # 路径不合规的不在这里拦（fetch 里记成没下好、接着下别的），类型不对说明清单坏了
        if not (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and type(item.get("size")) is int
            and isinstance(item.get("url"), str)
            and URL.fullmatch(item["url"])
        ):
            raise FetchError("清单里有格式不对的文件条目")
    takedowns = manifest.get("takedowns") or {}
    if not (
        isinstance(takedowns.get("url"), str)
        and URL.fullmatch(takedowns["url"])
        and SHA256.fullmatch(str(takedowns.get("sha256")))
        and type(takedowns.get("size")) is int
        and type(takedowns.get("count")) is int
    ):
        raise FetchError("清单里的下架清单信息不全")
    backups = [i["path"] for i in manifest.get("files", []) if i["path"].startswith("backups/")]
    if len(backups) != 1 or not _BACKUP.fullmatch(backups[0]):
        raise FetchError("清单里要正好有一份整库备份")
    return manifest


def _summary(manifest: dict) -> dict:
    counts: dict[str, list[int]] = {}
    for item in manifest["files"]:
        entry = counts.setdefault(_prefix(item["path"]), [0, 0])
        entry[0] += 1
        entry[1] += item["size"]
    return {
        "created_at": manifest.get("created_at"),
        "revision": manifest.get("revision"),
        "files": {prefix: {"count": n, "bytes": size} for prefix, (n, size) in counts.items()},
        "takedowns": manifest["takedowns"]["count"],
        "summary": manifest.get("summary"),
        "config": manifest.get("config"),
    }


def fetch(
    client: httpx.Client,
    url: str,
    sha256: str,
    workdir: Path,
    *,
    with_advisor: bool = False,
    store: storage.Backend | None = None,
    say: Callable[[str], None] = print,
) -> FetchReport:
    workdir = Path(workdir)
    (workdir / "backups").mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(client, url, sha256, workdir / "manifest.json")
    if store is None:
        if not storage.emulated():
            raise FetchError("这台的存储不是服务器硬盘（YOUJU_STORAGE_BACKEND），不导入")
        store = storage.backend()
    report = FetchReport()
    wanted = ("logs/", "attachments/", "backups/") + (("advisor/out/",) if with_advisor else ())
    files = [item for item in manifest["files"] if _prefix(item["path"]) in wanted]
    say(f"清单里要下载 {len(files)} 个文件（{sum(i['size'] for i in files)} 字节）")
    for number, item in enumerate(files, start=1):
        path, size = item["path"], item["size"]
        prefix = _prefix(path)
        if not (_PATH.fullmatch(path) and storage.valid_path(path) and 0 <= size <= MAX_FILE):
            report.failed.append((path[:120], "路径或大小不合规，没下载"))
            continue
        is_backup = prefix == "backups/"
        final = workdir / path if is_backup else None
        if final is not None and final.is_file() and final.stat().st_size == size:
            report.skipped[prefix] = report.skipped.get(prefix, 0) + 1
            report.backup = path
            continue
        if not is_backup and store.exists(path) and _disk_size(store, path) == size:
            report.skipped[prefix] = report.skipped.get(prefix, 0) + 1
            continue
        fd, name = tempfile.mkstemp(dir=workdir, prefix=".fetch-")
        os.close(fd)
        tmp = Path(name)
        try:
            got = _download(client, item["url"], tmp, size)
            if got != size:
                raise FetchError(f"大小 {got}，清单里是 {size}")
            match = _ATTACHMENT.fullmatch(path)
            if match and _sha256(tmp) != match.group(1):
                raise FetchError("内容和文件名里的 SHA-256 对不上")
            if path.endswith(".gz"):
                _check_gzip(tmp)
            if is_backup:
                os.replace(tmp, final)
                report.backup = path
            else:
                store.put_file(path, tmp)
        except (FetchError, httpx.HTTPError, storage.StorageError, OSError) as exc:
            reason = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            report.failed.append((path, reason))
            continue
        finally:
            tmp.unlink(missing_ok=True)
        report.fetched[prefix] = report.fetched.get(prefix, 0) + 1
        report.bytes += size
        if number % 500 == 0:
            say(f"  已处理 {number}/{len(files)}")
    _fetch_takedowns(client, manifest["takedowns"], workdir, report)
    summary = _summary(manifest)
    tmp = workdir / "summary.json.part"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, workdir / "summary.json")
    return report


def _disk_size(store: storage.Backend, path: str) -> int | None:
    if isinstance(store, storage.DiskBackend):
        return store._file(path).stat().st_size
    return None  # s3：不知道大小，当作不一样，重下一遍


def _fetch_takedowns(client: httpx.Client, info: dict, workdir: Path, report: FetchReport) -> None:
    target = workdir / "takedowns.jsonl"
    if target.is_file() and _sha256(target) == info["sha256"]:
        report.takedowns = info["count"]
        return
    tmp = workdir / "takedowns.jsonl.part"
    try:
        _download(client, info["url"], tmp, info["size"])
        if _sha256(tmp) != info["sha256"]:
            raise FetchError("SHA-256 对不上")
        os.replace(tmp, target)
        report.takedowns = info["count"]
    except (FetchError, httpx.HTTPError, OSError) as exc:
        reason = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
        report.failed.append((info.get("path", "takedowns"), reason))
    finally:
        tmp.unlink(missing_ok=True)
