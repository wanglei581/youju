"""首发前搬家（D-127）：服务器按云托管导出的清单下载文件（youju export-fetch）。

清单由 deploy/selfhost/shared/cloudrun-export.py 在云托管上生成：每个文件的路径、大小和 2 小时有效的
下载链接，下架清单的 SHA-256，汇总数（只有数字）和不涉密的配置。清单的 SHA-256 和链接一起从云托管
终端复制过来，先核对清单，再按清单逐个下载、核对：

- logs/、attachments/：核对大小，附件再核对文件名里的 SHA-256，.gz 整个解压一遍（gzip 自带
  校验）；对了才写进现在的存储（服务器硬盘）。已有、大小一样的跳过。批量工具结果（advisor/out/）
  清单里有、不下载：旧库不恢复（D-127），都是测试结果，对应的任务记录在加密存档的整库备份里。
- 访问日志归档按 D-028 留 190 天：已经超过的（文件名里的日期）不下载；下载的不放回 logs/，放到
  archive/cloudrun-access-<日期>.expires-<日期加 191 天>.jsonl.gz（服务器测试期一直在记访问日志，
  打开正式定时任务后 archive-logs 会按 logs/access-<日期> 写同名文件，放回 logs/ 会被覆盖），
  到期由每天的 youju archive-prune 删。
- 访问日志暂存表里还没归档的（导出脚本按天导出的 staging_logs）同样放 archive/，名字带 -staging。
- 批量工具结果（advisor/out/）：按清单里的修改时间，超过 30 天的不下（隐私保护指引：任务和结果保存
  30 天），没超过的下到 --dir/advisor-out/（不进存储，旧库不恢复），由 import-export 打包加密存档、
  到期删。
- 开头先算硬盘够不够：还要下的字节数，加上压缩整库备份的 15 倍（试恢复的临时库和它写进的 binlog
  大约是压缩备份的 10 倍，再加加密副本）和 2G 余量。
- 已经存档过（import-export 留下 --dir/imported）就不再下载：不再下一份明文备份。
- 最新一份整库备份、下架清单、交易记录（订单、退款、开票、赠送，导出时已去标识化）：写到 --dir
  （服务器上只有 root 能进的目录），不进存储。备份核对大小和 gzip，下架清单、交易记录核对 SHA-256。
  备份和交易记录接着由 youju-shared.sh import-export 试恢复、分别加密存档（youju archive-export）、
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
import shutil
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from youju import access_log, storage
from youju.backup import BEIJING

FORMAT = "youju-cloudrun-export"
URL = re.compile(r"https://[A-Za-z0-9.-]+\.(?:tcb\.qcloud\.la|myqcloud\.com)/[!-~]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
_PATH = re.compile(r"(?:logs|attachments|advisor/out|backups)/[A-Za-z0-9_][A-Za-z0-9._/-]*")
_BACKUP = re.compile(r"backups/youju-\d{8}\.jsonl\.gz")
_ATTACHMENT = re.compile(r"attachments/([0-9a-f]{64})\.[A-Za-z0-9]{1,10}")
_LOG = re.compile(r"logs/access-(\d{8})\.jsonl\.gz")
_CHUNK = 1024 * 1024
MAX_MANIFEST = 64 * 1024 * 1024
COMPLETE = "fetch-complete"
ADVISOR_DIR = "advisor-out"
ADVISOR_INDEX = "advisor-out.json"
# 批量工具结果保存 30 天（隐私保护指引：任务和结果保存 30 天）
ADVISOR_KEEP_DAYS = 30
IMPORTED = "imported"
# 试恢复的临时库和 binlog 大约是压缩整库备份的 10 倍，再加加密副本
BACKUP_FACTOR = 15
# 硬盘至少留这么多余量（和别的项目共用一块盘）
SPARE_BYTES = 2 * 1024 * 1024 * 1024
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
    pay_records: dict[str, int] = field(default_factory=dict)
    expired_logs: int = 0
    staging_logs: int = 0
    advisor: int = 0
    expired_advisor: int = 0


def _prefix(path: str) -> str:
    return "advisor/out/" if path.startswith("advisor/out/") else path.split("/", 1)[0] + "/"


def _download(client: httpx.Client, url: str, dest: Path, limit: int) -> int:
    size = 0
    # 连不上 30 秒就算失败（不白等 10 分钟），传输整体最多 10 分钟
    timeout = httpx.Timeout(600.0, connect=30.0)
    with client.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        if response.status_code != 200:
            hint = {
                403: "（链接过期了：在云托管上带 --links-only 重出一份清单）",
                404: "（云托管上已经没有这个文件，可能被定时清理删了："
                "带 --links-only 重出一份清单）",
            }.get(response.status_code, "")
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
    staged = manifest.get("staging_logs")
    if not isinstance(staged, list) or not all(
        isinstance(i, dict)
        and isinstance(i.get("url"), str)
        and URL.fullmatch(i["url"])
        and SHA256.fullmatch(str(i.get("sha256")))
        and type(i.get("size")) is int
        and type(i.get("rows")) is int
        and isinstance(i.get("day"), str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", i["day"])
        for i in staged
    ):
        raise FetchError("清单里的访问日志暂存信息不对")
    pay = manifest.get("pay_records") or {}
    if not (
        isinstance(pay.get("url"), str)
        and URL.fullmatch(pay["url"])
        and SHA256.fullmatch(str(pay.get("sha256")))
        and type(pay.get("size")) is int
        and isinstance(pay.get("counts"), dict)
    ):
        raise FetchError("清单里的交易记录信息不全")
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
        "pay_records": manifest["pay_records"]["counts"],
        "staging_logs": [{"day": i["day"], "rows": i["rows"]} for i in manifest["staging_logs"]],
        "summary": manifest.get("summary"),
        "config": manifest.get("config"),
        "config_unlisted": manifest.get("config_unlisted", []),
    }


def fetch(
    client: httpx.Client,
    url: str,
    sha256: str,
    workdir: Path,
    *,
    store: storage.Backend | None = None,
    say: Callable[[str], None] = print,
    today: date | None = None,
    disk_free: Callable[[Path], int] | None = None,
) -> FetchReport:
    workdir = Path(workdir)
    if (workdir / IMPORTED).exists():
        raise FetchError(
            f"这批已经存档过（IMPORT OK），不再下载明文备份；真要重来，先删掉 {workdir / IMPORTED}"
        )
    (workdir / "backups").mkdir(parents=True, exist_ok=True)
    # 完成标记：这一次全部下好才写；import-export 没看到它就不存档、不删明文和清单
    (workdir / COMPLETE).unlink(missing_ok=True)
    # 上次被打断留下的半截文件
    for leftover in [*workdir.glob(".fetch-*"), *workdir.glob("*.part")]:
        leftover.unlink(missing_ok=True)
    manifest = load_manifest(client, url, sha256, workdir / "manifest.json")
    if store is None:
        if not storage.emulated():
            raise FetchError("这台的存储不是服务器硬盘（YOUJU_STORAGE_BACKEND），不导入")
        store = storage.backend()
    report = FetchReport()
    today = today or datetime.now(UTC).astimezone(BEIJING).date()
    oldest = today - timedelta(days=access_log.KEEP_DAYS)
    advisor_oldest = today - timedelta(days=ADVISOR_KEEP_DAYS)
    wanted = ("logs/", "attachments/", "backups/", "advisor/out/")
    files = [item for item in manifest["files"] if _prefix(item["path"]) in wanted]
    backup_name = next(Path(i["path"]).name for i in files if _prefix(i["path"]) == "backups/")
    # 重出过清单（跨过了云托管每天 4:00 的备份）时只留这一份清单里的备份
    for old in (workdir / "backups").glob("youju-*.jsonl.gz"):
        if old.name != backup_name:
            old.unlink()
    # 目标：("dir", --dir 里的相对路径) 或 ("store", 存储里的路径)
    plan: list[tuple[dict, tuple[str, str]]] = []
    for item in files:
        path, size = item["path"], item["size"]
        if not (_PATH.fullmatch(path) and storage.valid_path(path) and 0 <= size <= MAX_FILE):
            report.failed.append((path[:120], "路径或大小不合规，没下载"))
            continue
        if path.startswith("advisor/out/"):
            modified = _modified(item)
            if modified is None:
                report.failed.append((path, "清单里没有修改时间，没下载"))
            elif modified < advisor_oldest:
                report.expired_advisor += 1  # 已经超过 30 天，不导入
            else:
                plan.append((item, ("dir", f"{ADVISOR_DIR}/{path.removeprefix('advisor/out/')}")))
            continue
        if path.startswith("backups/"):
            plan.append((item, ("dir", path)))
            continue
        target = _target(path, oldest)
        if target is None:
            report.expired_logs += 1  # 已经超过 190 天，不导入
            continue
        plan.append((item, ("store", target)))
    staged = []
    for entry in manifest["staging_logs"]:
        day = date.fromisoformat(entry["day"])
        if day < oldest:
            report.expired_logs += 1
            continue
        expires = day + timedelta(days=access_log.KEEP_DAYS + 1)
        staged.append(
            (
                entry,
                f"archive/cloudrun-access-{day:%Y%m%d}-staging.expires-{expires:%Y%m%d}.jsonl.gz",
            )
        )
    _check_disk(workdir, store, plan, staged, disk_free or _free_bytes)
    say(f"清单里要下载 {len(plan)} 个文件（{sum(i['size'] for i, _ in plan)} 字节）")
    for number, (item, (kind, target)) in enumerate(plan, start=1):
        path, size = item["path"], item["size"]
        prefix = _prefix(path)
        final = workdir / target if kind == "dir" else None
        if final is not None and final.is_file() and final.stat().st_size == size:
            report.skipped[prefix] = report.skipped.get(prefix, 0) + 1
            if prefix == "backups/":
                report.backup = path
            continue
        if kind == "store" and _present(store, target, size):
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
            if final is not None:
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, final)
                if prefix == "backups/":
                    report.backup = path
            else:
                store.put_file(target, tmp)
        except (FetchError, httpx.HTTPError, storage.StorageError, OSError) as exc:
            reason = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            report.failed.append((path, reason))
            continue
        finally:
            tmp.unlink(missing_ok=True)
        report.fetched[prefix] = report.fetched.get(prefix, 0) + 1
        report.bytes += size
        if number % 100 == 0:
            say(f"  已处理 {number}/{len(plan)}")
    say(f"  文件处理完了（{len(plan)} 个），接着取还没归档的访问日志、下架清单和交易记录")
    for entry, target in staged:
        if _present(store, target, entry["size"]) or _fetch_to_store(
            client, entry, target, store, report, workdir
        ):
            report.staging_logs += 1
    index = [
        {"path": item["path"], "modified": item["modified"], "size": item["size"]}
        for item, (kind, target) in plan
        if item["path"].startswith("advisor/out/") and (workdir / target).is_file()
    ]
    report.advisor = len(index)
    (workdir / ADVISOR_INDEX).write_text(
        json.dumps(index, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if _fetch_extra(client, manifest["takedowns"], workdir / "takedowns.jsonl", report):
        report.takedowns = manifest["takedowns"]["count"]
    pay = manifest["pay_records"]
    if _fetch_extra(client, pay, workdir / "pay-records.jsonl.gz", report, gz=True):
        report.pay_records = pay["counts"]
    summary = _summary(manifest)
    summary["complete"] = not report.failed
    tmp = workdir / "summary.json.part"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, workdir / "summary.json")
    if not report.failed:
        stamp = datetime.now(UTC).isoformat()
        (workdir / COMPLETE).write_text(f"{sha256} {stamp}\n", encoding="ascii")
    return report


def _modified(item: dict) -> date | None:
    """清单里文件的修改时间（北京时间的日期）；没有或读不出返回 None。"""
    try:
        return datetime.fromisoformat(item["modified"]).astimezone(BEIJING).date()
    except (KeyError, TypeError, ValueError):
        return None


def _fetch_to_store(
    client: httpx.Client, entry: dict, target: str, store, report, workdir: Path
) -> bool:
    """还没归档的访问日志：核对 SHA-256 和 gzip，写进存储。"""
    fd, name = tempfile.mkstemp(dir=workdir, prefix=".fetch-")
    os.close(fd)
    tmp = Path(name)
    try:
        _download(client, entry["url"], tmp, entry["size"])
        if _sha256(tmp) != entry["sha256"]:
            raise FetchError("SHA-256 对不上")
        _check_gzip(tmp)
        store.put_file(target, tmp)
        return True
    except (FetchError, httpx.HTTPError, storage.StorageError, OSError) as exc:
        reason = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
        report.failed.append((entry.get("path", target), reason))
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _target(path: str, oldest: date) -> str | None:
    """写到存储里的哪里：附件副本照原路径；访问日志放 archive/（到期日写进文件名），超过 190 天的
    返回 None（不导入）；整库备份不进存储（返回原路径，调用方写到 --dir）。"""
    log = _LOG.fullmatch(path)
    if not log:
        return path
    day = datetime.strptime(log.group(1), "%Y%m%d").date()
    if day < oldest:
        return None
    expires = day + timedelta(days=access_log.KEEP_DAYS + 1)
    return f"archive/cloudrun-access-{day:%Y%m%d}.expires-{expires:%Y%m%d}.jsonl.gz"


def _present(store: storage.Backend, path: str, size: int) -> bool:
    if not store.exists(path):
        return False
    if isinstance(store, storage.DiskBackend):
        return store._file(path).stat().st_size == size
    return False  # s3：不知道大小，当作不一样，重下一遍


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _check_disk(
    workdir: Path, store, plan: list, staged: list, free: Callable[[Path], int]
) -> None:
    """还要下的字节数，加上压缩整库备份的 BACKUP_FACTOR 倍（试恢复的临时库和 binlog、加密副本）
    和余量。硬盘和别的项目共用，不够就不下。"""
    remaining = 0
    backup = 0
    for item, (kind, target) in plan:
        if item["path"].startswith("backups/"):
            backup = item["size"]
        if kind == "dir":
            final = workdir / target
            if final.is_file() and final.stat().st_size == item["size"]:
                continue
        elif _present(store, target, item["size"]):
            continue
        remaining += item["size"]
    remaining += sum(e["size"] for e, target in staged if not _present(store, target, e["size"]))
    need = remaining + BACKUP_FACTOR * backup + SPARE_BYTES
    roots = [workdir]
    if isinstance(store, storage.DiskBackend):
        store.root.mkdir(parents=True, exist_ok=True)
        roots.append(store.root)
    have = min(free(root) for root in roots)
    if have < need:
        gib = 1024**3
        raise FetchError(
            f"硬盘不够：还要下 {remaining / gib:.1f}G，加上试恢复和加密要的空间、余量共需约 "
            f"{need / gib:.1f}G，现在只剩 {have / gib:.1f}G（先腾空间，别动别的项目的文件）"
        )


def _fetch_extra(
    client: httpx.Client, info: dict, target: Path, report: FetchReport, *, gz: bool = False
) -> bool:
    """下架清单、交易记录：按清单里的 SHA-256 核对；已经是这一份就不再下载。"""
    if target.is_file() and _sha256(target) == info["sha256"]:
        return True
    tmp = target.with_name(target.name + ".part")
    try:
        _download(client, info["url"], tmp, info["size"])
        if _sha256(tmp) != info["sha256"]:
            raise FetchError("SHA-256 对不上")
        if gz:
            _check_gzip(tmp)
        os.replace(tmp, target)
        return True
    except (FetchError, httpx.HTTPError, OSError) as exc:
        reason = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
        report.failed.append((str(info.get("path", target.name)), reason))
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _pay_header(path: Path) -> dict:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            header = json.loads(fh.readline())
    except (OSError, EOFError, zlib.error, ValueError) as exc:
        raise FetchError("交易记录读不出说明行") from exc
    retain = header.get("retain_days") if isinstance(header, dict) else None
    if (
        not isinstance(header, dict)
        or header.get("format") != "youju-pay-records"
        or header.get("version") != 1
        or type(retain) is not int
        or not 180 <= retain <= 3650
    ):
        raise FetchError("交易记录的说明行不对")
    return header


def _beijing_day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(BEIJING).date()


def _advisor_tar(workdir: Path, index: list[dict], target: Path) -> None:
    import tarfile

    with tarfile.open(target, "w:gz") as tar:
        for item in index:
            rel = item["path"].removeprefix("advisor/out/")
            tar.add(workdir / ADVISOR_DIR / rel, arcname=item["path"], recursive=False)


def archive_export(
    settings,
    workdir: Path,
    *,
    today: date | None = None,
    backend=None,
    encryptor=None,
) -> tuple[list, list[str]]:
    """把下载好的分别加密存档（youju archive-export），到期日写进文件名：
    - 整库备份：自备份那天起 30 天；
    - 交易记录：订单、退款、开票自下单起，赠送自发放之日起 retain_days（3650）天，按最早的一笔算；
    - 批量工具结果：自文件修改时间起 30 天，按最早的一个算（都已过期就不存）。
    返回（存档的报告，说明）。"""
    from youju import offsite

    workdir = Path(workdir)
    today = today or datetime.now(UTC).astimezone(BEIJING).date()
    backups = sorted((workdir / "backups").glob("youju-*.jsonl.gz"))
    if len(backups) != 1 or not _BACKUP.fullmatch(f"backups/{backups[0].name}"):
        raise FetchError("备份目录里要正好有一份整库备份（先 fetch-export）")
    day = datetime.strptime(backups[0].name[len("youju-") :][:8], "%Y%m%d").date()
    pay = workdir / "pay-records.jsonl.gz"
    if not pay.is_file():
        raise FetchError("没有交易记录（先 fetch-export）")
    header = _pay_header(pay)
    starts = [header.get("earliest_order_at"), header.get("earliest_gift_at")]
    start = min(
        (_beijing_day(stamp) for stamp in starts if stamp),
        default=_beijing_day(header["created_at"]),
    )
    extra = {"today": today, "backend": backend}
    if encryptor is not None:
        extra["encryptor"] = encryptor
    reports = [
        offsite.archive(
            settings,
            backups[0],
            f"cloudrun-youju-{day:%Y%m%d}",
            day + timedelta(days=offsite.BACKUP_KEEP_DAYS),
            **extra,
        ),
        offsite.archive(
            settings,
            pay,
            f"cloudrun-pay-records-{day:%Y%m%d}",
            start + timedelta(days=header["retain_days"]),
            **extra,
        ),
    ]
    notes: list[str] = []
    index_file = workdir / ADVISOR_INDEX
    index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.is_file() else []
    if index:
        expires = min(_beijing_day(i["modified"]) for i in index) + timedelta(
            days=ADVISOR_KEEP_DAYS
        )
        if expires <= today:
            notes.append("批量工具结果都已经过了 30 天，不存档")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                packed = Path(tmp) / "advisor-out.tar.gz"
                _advisor_tar(workdir, index, packed)
                reports.append(
                    offsite.archive(
                        settings,
                        packed,
                        f"cloudrun-advisor-out-{day:%Y%m%d}",
                        expires,
                        ext="tar.gz",
                        **extra,
                    )
                )
    else:
        notes.append("没有要存的批量工具结果")
    return reports, notes
