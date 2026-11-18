"""首发前搬家（D-127）：服务器按导出清单下载（youju export-fetch），youju-shared.sh 的搬家和录密钥
子命令，本机的录密钥、带下架清单脚本，云托管终端用的粘贴块。"""

import base64
import contextlib
import gzip
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from youju import cli, cloudrun_fetch, offsite, storage
from youju.backup import BackupError
from youju.config import Settings, get_settings

SELFHOST = Path(__file__).resolve().parents[1] / "deploy" / "selfhost"
SHARED_SH = SELFHOST / "shared" / "youju-shared.sh"
needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="没有 bash")
HOST = "7072-prod-1abc-1250000000.tcb.qcloud.la"


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    """下载前算硬盘够不够：测试里当作够（专门的测试另给数）。"""
    monkeypatch.setattr(cloudrun_fetch, "_free_bytes", lambda path: 1 << 40)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode(), mtime=0)


class Remote:
    """云托管存储的下载链接：path → 内容；links 里的链接按 path 取。"""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.gets: list[str] = []
        self.expired: set[str] = set()
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def url(self, path: str) -> str:
        return f"https://{HOST}/{path}?sign=s&t=1"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        self.gets.append(path)
        if path in self.expired:
            return httpx.Response(403)
        if path not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.files[path])


PAY = _gz(
    json.dumps(
        {
            "format": "youju-pay-records",
            "version": 1,
            "created_at": "2026-11-12T08:00:00+00:00",
            "retain_days": 3650,
            "earliest_order_at": "2026-10-29T16:30:00+00:00",
            # 赠送比第一笔订单早：保存期从赠送那天算
            "earliest_gift_at": "2026-10-20T02:00:00+00:00",
            "counts": {"pay_orders": 1},
        }
    )
    + "\n"
    + json.dumps(
        {
            "table": "pay_orders",
            "row": {"id": 1, "out_trade_no": "T1", "created_at": "2026-10-29T16:30:00+00:00"},
        }
    )
    + "\n"
    + json.dumps(
        {"table": "invoice_requests", "row": {"id": 1, "order_id": 1, "title_type": "company"}}
    )
    + "\n"
    + json.dumps(
        {"table": "pass_rewards", "row": {"id": 1, "granted_at": "2026-10-20T02:00:00+00:00"}}
    )
    + "\n"
)


def _export(
    remote: Remote,
    files: dict[str, bytes],
    *,
    sizes=None,
    takedowns=b'{"url_key":"a.gov.cn/1"}\n',
    pay=PAY,
    modified=None,
    staging=None,
    etags=None,
):
    """按导出脚本的格式造一份清单，返回（链接，SHA-256）。staging：{日期: 内容} 还没归档的访问日志。"""
    sizes = sizes or {}
    modified = modified or {}
    staging = staging or {}
    etags = etags or {}
    remote.files.update(files)
    for day, data in staging.items():
        remote.files[f"exports/access-staging-{day.replace('-', '')}-1.jsonl.gz"] = data
    remote.files["exports/takedowns-1.jsonl"] = takedowns
    remote.files["exports/pay-records-1.jsonl.gz"] = pay
    manifest = {
        "format": "youju-cloudrun-export",
        "version": 1,
        "created_at": "2026-11-12T08:00:00+00:00",
        "revision": "0046",
        "files": [
            {
                "path": p,
                "size": sizes.get(p, len(data)),
                "modified": modified.get(p, "2026-11-12T00:00:00+00:00"),
                "etag": etags.get(p, "etag-" + _sha(data)[:8]),
                "url": remote.url(p),
            }
            for p, data in files.items()
        ],
        "staging_logs": [
            {
                "day": day,
                "path": f"exports/access-staging-{day.replace('-', '')}-1.jsonl.gz",
                "size": len(data),
                "sha256": _sha(data),
                "rows": 1,
                "url": remote.url(f"exports/access-staging-{day.replace('-', '')}-1.jsonl.gz"),
            }
            for day, data in staging.items()
        ],
        "takedowns": {
            "path": "exports/takedowns-1.jsonl",
            "size": len(takedowns),
            "sha256": _sha(takedowns),
            "count": 1,
            "url": remote.url("exports/takedowns-1.jsonl"),
        },
        "pay_records": {
            "path": "exports/pay-records-1.jsonl.gz",
            "size": len(pay),
            "sha256": _sha(pay),
            "counts": {"pay_orders": 1},
            "earliest_order_at": "2026-10-29T16:30:00+00:00",
            "retain_days": 3650,
            "url": remote.url("exports/pay-records-1.jsonl.gz"),
        },
        "summary": {"users": 3},
        "config": {"YOUJU_AI_MODEL_FILING": "备案号"},
    }
    body = json.dumps(manifest).encode()
    remote.files["exports/manifest-1.json"] = body
    return remote.url("exports/manifest-1.json"), _sha(body)


def test_fetch_checks_and_imports_files(tmp_path):
    pdf = b"%PDF-1.7 attachment"
    good = {
        "backups/youju-20261112.jsonl.gz": _gz('{"format":"youju-backup"}\n'),
        "logs/access-20261110.jsonl.gz": _gz('{"path":"/api/v1/x"}\n'),
        # 超过 190 天的访问日志不导入（到期的由服务器每天的 archive-logs 接着删）
        "logs/access-20260501.jsonl.gz": _gz('{"path":"/api/v1/old"}\n'),
        f"attachments/{_sha(pdf)}.pdf": pdf,
        "advisor/out/0123456789abcdef/7.xlsx": b"xlsx",
        # 批量工具结果超过 30 天的不导入
        "advisor/out/0123456789abcdef/1.xlsx": b"old",
    }
    modified = {"advisor/out/0123456789abcdef/1.xlsx": "2026-10-01T00:00:00+00:00"}
    staging = {"2026-11-12": _gz('{"path":"/api/v1/today"}\n')}
    bad_hash = f"attachments/{'0' * 64}.pdf"
    broken_gz = "logs/access-20261111.jsonl.gz"
    remote = Remote({})
    url, sha = _export(
        remote,
        {**good, bad_hash: b"not matching", broken_gz: _gz("x")[:-4]},
        modified=modified,
        staging=staging,
    )
    store = storage.DiskBackend(tmp_path / "store")
    work = tmp_path / "export"
    today = date(2026, 11, 12)
    report = cloudrun_fetch.fetch(
        remote.client, url, sha, work, store=store, say=lambda _: None, today=today
    )
    assert report.expired_logs == 1 and not store.exists("logs/access-20260501.jsonl.gz")
    assert "logs/access-20260501.jsonl.gz" not in remote.gets
    # 有没下好的：不写完成标记（import-export 看不到它就不存档、不删清单）
    assert not (work / "fetch-complete").exists()
    assert json.loads((work / "summary.json").read_text(encoding="utf-8"))["complete"] is False
    assert (work / "pay-records.jsonl.gz").read_bytes() == PAY
    assert report.pay_records == {"pay_orders": 1}
    # 不对的两个不写进存储、记下来；别的照常下载
    assert sorted(path for path, _ in report.failed) == sorted([bad_hash, broken_gz])
    assert not store.exists(bad_hash) and not store.exists(broken_gz)
    assert store.exists(f"attachments/{_sha(pdf)}.pdf")
    # 访问日志不放回 logs/（服务器正式归档会写同名文件），放 archive/、到期日写进文件名：
    # 最长 190 天，5:50 删到期日那天的，所以是日志那天加 189 天
    log_day = date(2026, 11, 10)
    expires = log_day + timedelta(days=189)
    assert store.exists(f"archive/cloudrun-access-20261110.expires-{expires:%Y%m%d}.jsonl.gz")
    assert not store.exists("logs/access-20261110.jsonl.gz")
    # 还没归档的访问日志：同样放 archive/，名字带 -staging
    staged_expires = date(2026, 11, 12) + timedelta(days=189)
    assert store.exists(
        f"archive/cloudrun-access-20261112-staging.expires-{staged_expires:%Y%m%d}.jsonl.gz"
    )
    assert report.staging_logs == 1
    # 批量工具结果：没过 30 天的下到 --dir/advisor-out/（不进存储），过了的不下
    assert not store.exists("advisor/out/0123456789abcdef/7.xlsx")
    assert (work / "advisor-out/0123456789abcdef/7.xlsx").read_bytes() == b"xlsx"
    assert "advisor/out/0123456789abcdef/1.xlsx" not in remote.gets
    assert (report.advisor, report.expired_advisor) == (1, 1)
    assert json.loads((work / "advisor-out.json").read_text(encoding="utf-8")) == [
        {
            "path": "advisor/out/0123456789abcdef/7.xlsx",
            "modified": "2026-11-12T00:00:00+00:00",
            "size": 4,
        }
    ]
    assert not store.exists("backups/youju-20261112.jsonl.gz")
    assert (work / "backups/youju-20261112.jsonl.gz").read_bytes() == good[
        "backups/youju-20261112.jsonl.gz"
    ]
    assert (work / "takedowns.jsonl").read_bytes() == b'{"url_key":"a.gov.cn/1"}\n'
    assert report.backup == "backups/youju-20261112.jsonl.gz" and report.takedowns == 1
    summary = json.loads((work / "summary.json").read_text(encoding="utf-8"))
    assert summary["summary"] == {"users": 3} and summary["config"] == {
        "YOUJU_AI_MODEL_FILING": "备案号"
    }
    assert "url" not in json.dumps(summary)  # 汇总里没有链接
    assert not list(work.glob(".fetch-*"))

    # 重跑：已经下好的跳过（不再下载），只再试没下好的
    remote.gets.clear()
    remote.files[bad_hash] = pdf  # 换成对的内容也不行：文件名里的哈希对不上
    again = cloudrun_fetch.fetch(
        remote.client, url, sha, work, store=store, say=lambda _: None, today=today
    )
    assert again.skipped == {"backups/": 1, "logs/": 1, "attachments/": 1, "advisor/out/": 1}
    assert set(remote.gets) == {bad_hash, broken_gz}
    assert sorted(path for path, _ in again.failed) == sorted([bad_hash, broken_gz])
    assert not (work / "fetch-complete").exists()
    # 重出一份清单（坏的两个已经不在里面）：全部下好才写完成标记
    remote2 = Remote({})
    url2, sha2 = _export(remote2, good, modified=modified, staging=staging)
    done = cloudrun_fetch.fetch(
        remote2.client, url2, sha2, work, store=store, say=lambda _: None, today=today
    )
    assert not done.failed and (work / "fetch-complete").read_text().startswith(sha2)
    assert json.loads((work / "summary.json").read_text(encoding="utf-8"))["complete"] is True


def test_fetch_redownloads_when_the_etag_changes(tmp_path):
    """同一天重做的备份大小可能一样：重跑时按对象存储的 ETag 判断是不是同一份，不只看大小。"""
    store = storage.DiskBackend(tmp_path / "store")
    work = tmp_path / "export"
    remote = Remote({})
    url, sha = _export(remote, {"backups/youju-20261112.jsonl.gz": _gz("{}\n" * 3)}, etags={})
    cloudrun_fetch.fetch(remote.client, url, sha, work, store=store, say=lambda _: None)
    newer = _gz("[]\n" * 3)
    assert len(newer) == len(_gz("{}\n" * 3))
    remote2 = Remote({})
    url2, sha2 = _export(
        remote2,
        {"backups/youju-20261112.jsonl.gz": newer},
        etags={"backups/youju-20261112.jsonl.gz": "etag-newer"},
    )
    again = cloudrun_fetch.fetch(remote2.client, url2, sha2, work, store=store, say=lambda _: None)
    assert again.fetched == {"backups/": 1} and not again.skipped
    assert (work / "backups" / "youju-20261112.jsonl.gz").read_bytes() == newer


def test_fetch_redownloads_when_the_manifest_has_no_etag(tmp_path):
    store = storage.DiskBackend(tmp_path / "store")
    work = tmp_path / "export"
    remote = Remote({})
    files = {"backups/youju-20261112.jsonl.gz": _gz("{}\n")}
    url, sha = _export(remote, files, etags={"backups/youju-20261112.jsonl.gz": ""})
    cloudrun_fetch.fetch(remote.client, url, sha, work, store=store, say=lambda _: None)
    again = cloudrun_fetch.fetch(remote.client, url, sha, work, store=store, say=lambda _: None)
    assert again.fetched == {"backups/": 1} and not again.skipped


def test_fetch_refuses_wrong_manifest_and_reports_expired_links(tmp_path):
    remote = Remote({})
    url, sha = _export(remote, {"backups/youju-20261112.jsonl.gz": _gz("{}\n")})
    store = storage.DiskBackend(tmp_path / "store")
    with pytest.raises(cloudrun_fetch.FetchError, match="SHA-256 对不上"):
        cloudrun_fetch.fetch(remote.client, url, "f" * 64, tmp_path / "a", store=store)
    with pytest.raises(cloudrun_fetch.FetchError, match="清单链接不对"):
        cloudrun_fetch.fetch(
            remote.client, "https://evil.example.com/m.json", sha, tmp_path / "a", store=store
        )
    remote.expired.add("backups/youju-20261112.jsonl.gz")
    report = cloudrun_fetch.fetch(
        remote.client, url, sha, tmp_path / "b", store=store, say=lambda _: None
    )
    assert report.failed == [
        (
            "backups/youju-20261112.jsonl.gz",
            "HTTP 403（链接过期了：在云托管上带 --links-only 重出一份清单）",
        )
    ]
    # 比清单写的大：不写
    remote = Remote({})
    url, sha = _export(
        remote,
        {
            "backups/youju-20261112.jsonl.gz": _gz("{}\n"),
            "logs/access-20261110.jsonl.gz": _gz("{}\n"),
        },
        sizes={"logs/access-20261110.jsonl.gz": 3},
    )
    report = cloudrun_fetch.fetch(
        remote.client,
        url,
        sha,
        tmp_path / "c",
        store=store,
        say=lambda _: None,
        today=date(2026, 11, 12),
    )
    assert [p for p, _ in report.failed] == ["logs/access-20261110.jsonl.gz"]
    assert not list(store.list("archive/")) and not store.exists("logs/access-20261110.jsonl.gz")


def test_fetch_refuses_after_the_export_was_archived(tmp_path):
    remote = Remote({})
    url, sha = _export(remote, {"backups/youju-20261112.jsonl.gz": _gz("{}\n")})
    work = tmp_path / "export"
    work.mkdir()
    (work / "imported").write_text("2026-11-13 10:00:00\n", encoding="utf-8")
    with pytest.raises(cloudrun_fetch.FetchError, match="已经存档过"):
        cloudrun_fetch.fetch(
            remote.client, url, sha, work, store=storage.DiskBackend(tmp_path / "s")
        )
    assert remote.gets == [] and not (work / "backups").exists()


def test_fetch_checks_disk_space_and_cleans_up_leftovers(tmp_path):
    remote = Remote({})
    backup = _gz("{}\n" * 10)
    pdf = b"%PDF-1.7"
    listing = b'{"url_key":"a.gov.cn/1"}\n'
    url, sha = _export(
        remote,
        {"backups/youju-20261113.jsonl.gz": backup, f"attachments/{_sha(pdf)}.pdf": pdf},
        sizes={"backups/youju-20261113.jsonl.gz": len(backup)},
        takedowns=listing,
    )
    store = storage.DiskBackend(tmp_path / "store")
    work = tmp_path / "export"
    (work / "backups").mkdir(parents=True)
    # 上次被打断的半截文件；跨过 4:00 重出清单以前下的旧备份
    (work / ".fetch-abc").write_bytes(b"half")
    (work / "takedowns.jsonl.part").write_bytes(b"half")
    (work / "backups" / "youju-20261112.jsonl.gz").write_bytes(b"older")
    # 要下的（文件、下架清单、交易记录）加上压缩整库备份的 15 倍和余量
    need = (
        len(pdf)
        + len(listing)
        + len(PAY)
        + (1 + cloudrun_fetch.BACKUP_FACTOR) * len(backup)
        + cloudrun_fetch.SPARE_BYTES
    )
    with pytest.raises(cloudrun_fetch.FetchError, match="硬盘不够"):
        cloudrun_fetch.fetch(
            remote.client,
            url,
            sha,
            work,
            store=store,
            say=lambda _: None,
            disk_free=lambda path: need - 1,
        )
    assert remote.gets == ["exports/manifest-1.json"]  # 只下了清单，文件一个没下
    assert not (work / ".fetch-abc").exists() and not (work / "takedowns.jsonl.part").exists()
    assert not (work / "backups" / "youju-20261112.jsonl.gz").exists()
    report = cloudrun_fetch.fetch(
        remote.client,
        url,
        sha,
        work,
        store=store,
        say=lambda _: None,
        disk_free=lambda path: need,
    )
    assert not report.failed and report.backup == "backups/youju-20261113.jsonl.gz"
    remote.files.pop(f"attachments/{_sha(pdf)}.pdf")
    store.delete(f"attachments/{_sha(pdf)}.pdf")
    report = cloudrun_fetch.fetch(remote.client, url, sha, work, store=store, say=lambda _: None)
    assert report.failed == [
        (
            f"attachments/{_sha(pdf)}.pdf",
            "HTTP 404（云托管上已经没有这个文件，可能被定时清理删了：带 --links-only 重出一份清单）",
        )
    ]


def test_cli_export_fetch_reads_link_from_environment(tmp_path, monkeypatch, capsys):
    remote = Remote({})
    url, sha = _export(remote, {"backups/youju-20261112.jsonl.gz": _gz("{}\n")})
    monkeypatch.setattr(storage, "http_client", lambda **kw: remote.client)
    monkeypatch.setenv("YOUJU_STORAGE_BACKEND", "disk")
    monkeypatch.setenv("YOUJU_STORAGE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("YOUJU_HOSTING", "self")
    monkeypatch.setenv("YOUJU_CLOUD_ENV", "prod-1abc")
    monkeypatch.setenv("YOUJU_EXPORT_URL", url)
    get_settings.cache_clear()
    try:
        assert cli.main(["export-fetch", "--sha256", sha, "--dir", str(tmp_path / "x")]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "FETCH OK" in out and url not in out


# ---- shell ----


def _functions(tmp_path: Path) -> Path:
    """youju-shared.sh 去掉最后"要 root、按子命令分派"那段，只留变量和函数，给测试 source。"""
    sh = SHARED_SH.read_text(encoding="utf-8")
    head, _ = sh.split('\n[ "$(id -u)" -eq 0 ] || die "要用 root 运行"\n', 1)
    path = tmp_path / "functions.sh"
    path.write_text(head + "\n", encoding="utf-8")
    return path


def _script(tmp_path: Path, body: str, setup: str = "") -> str:
    """source 函数后把 BASE、SERVER、EXPORT_DIR 指到临时目录，compose、docker 换成只记录参数的假的，
    df 当作还剩 100G（不看跑测试的机器）。"""
    defs = _functions(tmp_path)
    log = tmp_path / "calls.log"
    return f"""
source "{defs}"
BASE="{tmp_path}"; SERVER="{tmp_path}/server"; EXPORT_DIR="{tmp_path}/export"; SELF=youju-shared.sh
compose() {{ printf 'compose %s | image=%s db=%s url=%s\\n' "$*" "${{YOUJU_IMAGE:-}}" "${{YOUJU_DATABASE_URL:-}}" "${{YOUJU_EXPORT_URL:-}}" >> "{log}"; if [ "$1" = exec ]; then cat >/dev/null; fi; }}
docker() {{ printf 'docker %s\\n' "$*" >> "{log}"; if [ "$1" = info ]; then echo "{tmp_path}"; fi; }}
df() {{ case "$*" in *-B1*) printf 'Avail\\n 107374182400\\n' ;; *) printf 'Avail\\n 100G\\n' ;; esac; }}
{setup}
{body}
"""


def _bash(
    tmp_path: Path, body: str, *, stdin: str = "", setup: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _script(tmp_path, body, setup)], input=stdin, capture_output=True, text=True
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


ROOT_PW, MIGRATE_PW, API_PW, WORKER_PW = "r00tpw", "m1gratepw", "ap1pw0", "w0rkerpw"
DB_SUFFIX = "@mysql:3306/youju?charset=utf8mb4"
MYSQL_ACCOUNTS = SELFHOST.parent / "mysql_accounts.sql"


def _url(user: str, password: str) -> str:
    return f"mysql+pymysql://{user}:{password}{DB_SUFFIX}"


def _server_env(tmp_path: Path, text: str) -> Path:
    """服务器的 .env（只有 root 能读）。写了 MYSQL_ROOT_PASSWORD 的，照现在的做法把库账号分开放：root 和 youju
    的口令进 mysql-root.env，接口、worker 各有 db-api.env、db-worker.env；.env 里不留这几项。"""
    server = tmp_path / "server"
    server.mkdir(exist_ok=True)
    lines = text.splitlines(keepends=True)
    secret = ("MYSQL_ROOT_PASSWORD=", "MYSQL_PASSWORD=", "YOUJU_DATABASE_URL=")
    env = server / ".env"
    env.write_text("".join(x for x in lines if not x.startswith(secret)), encoding="utf-8")
    env.chmod(0o600)
    root = [x for x in lines if x.startswith("MYSQL_ROOT_PASSWORD=")]
    if root:
        migrate = [x for x in lines if x.startswith("MYSQL_PASSWORD=")]
        files = {
            "mysql-root.env": root[0]
            + (migrate[0] if migrate else f"MYSQL_PASSWORD={MIGRATE_PW}\n"),
            "db-api.env": f"YOUJU_DATABASE_URL={_url('youju_api', API_PW)}\n",
            "db-worker.env": f"YOUJU_DATABASE_URL={_url('youju_worker', WORKER_PW)}\n",
        }
        for name, body in files.items():
            (server / name).write_text(body, encoding="utf-8")
            (server / name).chmod(0o600)
    return env


@needs_bash
def test_shared_set_env_and_secret_formats(tmp_path):
    env = _server_env(tmp_path, "A=1\nYOUJU_WX_APPSECRET=old\nB=2\nYOUJU_WX_APPSECRET=dup\n")
    done = _bash(tmp_path, "set_env YOUJU_WX_APPSECRET 'x&y/=z\\1' && set_env NEW_KEY v")
    assert done.returncode == 0, done.stderr
    assert env.read_text(encoding="utf-8") == "A=1\nYOUJU_WX_APPSECRET=x&y/=z\\1\nB=2\nNEW_KEY=v\n"
    assert env.stat().st_mode & 0o777 == 0o600
    checks = {
        ("YOUJU_WX_APPSECRET", "0123456789abcdef0123456789abcdef"): True,
        ("YOUJU_WX_APPSECRET", "0123456789abcdef0123456789abcde"): False,
        ("YOUJU_OFFSITE_AGE_RECIPIENT", "age1" + "q" * 58): True,
        ("YOUJU_OFFSITE_AGE_RECIPIENT", "AGE-SECRET-KEY-1" + "Q" * 58): False,
        (
            "YOUJU_ALERT_WEBHOOK",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=ab-12&x=1",
        ): True,
        ("YOUJU_ALERT_WEBHOOK", "http://qyapi.weixin.qq.com/x"): False,
        ("YOUJU_AI_API_KEY", "sk-0123456789abcdef"): True,
        ("YOUJU_AI_API_KEY", "sk 0123456789"): False,
        ("YOUJU_AI_API_KEY", "sk-$(reboot)"): False,
        ("YOUJU_AI_API_KEY", "short"): False,
    }
    for (name, value), ok in checks.items():
        done = _bash(tmp_path, f"secret_ok {name} '{value}'")
        assert (done.returncode == 0) is ok, (name, value)


@needs_bash
def test_shared_secret_show_need_a_terminal(tmp_path):
    _server_env(tmp_path, "YOUJU_SYNC_SECRET=" + "a" * 64 + "\n")
    for body in ("secret YOUJU_WX_APPSECRET", "show sync", "show push"):
        done = _bash(tmp_path, body, stdin="0123456789abcdef0123456789abcdef\n")
        assert done.returncode == 1 and "只能在交互终端里用" in done.stderr, body
        assert "a" * 64 not in done.stdout
    done = _bash(tmp_path, "secret YOUJU_DATABASE_URL")
    assert done.returncode == 1 and "用法：secret" in done.stderr


def _tty(args: list[str], typed: str, *, env=None, cwd=None, timeout=20) -> tuple[int, str]:
    """在伪终端里跑（read -s 要真的终端），看到提示的"："再输入。"""
    master, slave = pty.openpty()
    # 终端本身先不回显：看到提示就输入时，bash 的 read -s 可能还没来得及关回显（机器忙时），
    # 输入会被终端回显出来。这里只验证脚本自己不打印输入的值
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd)
    os.close(slave)
    out, sent = b"", False

    def read(wait: float) -> bytes | None:
        """读一块输出；没有（等了 wait 秒）返回 b""；终端已经关了返回 None。"""
        ready, _, _ = select.select([master], [], [], wait)
        if not ready:
            return b""
        try:
            return os.read(master, 4096) or None
        except OSError:  # 子进程退出、终端关了（macOS 上是 EIO）
            return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = read(0.1)
        if chunk is None:
            break
        out += chunk
        if not sent and "：".encode() in out:
            with contextlib.suppress(OSError):  # 已经退出了（"："是报错信息里的）
                os.write(master, typed.encode())
            sent = True
        if not chunk and proc.poll() is not None:
            # 退出了：把还没读的输出读完再走（机器忙时最后几行会晚一点到）
            while (rest := read(0.5)) is not None and rest:
                out += rest
            break
    proc.wait(timeout=5)
    os.close(master)
    return proc.returncode, out.decode("utf-8", "replace")


@needs_bash
def test_shared_secret_writes_env_without_echo(tmp_path):
    env = _server_env(tmp_path, "YOUJU_WX_APPSECRET=test-placeholder-not-a-secret\n")
    defs = _functions(tmp_path)
    value = "0123456789abcdef0123456789abcdef"
    code, out = _tty(
        ["bash", "-c", f'source "{defs}"; SERVER="{tmp_path}/server"; secret YOUJU_WX_APPSECRET'],
        value + "\n",
    )
    assert code == 0, out
    assert value not in out  # 输入不回显、结果只给长度和指纹
    assert f"32 位，指纹 {_sha(value.encode())[:4]}" in out
    assert env.read_text(encoding="utf-8") == f"YOUJU_WX_APPSECRET={value}\n"


@needs_bash
def test_shared_rotate_sync_prints_only_a_fingerprint(tmp_path):
    env = _server_env(tmp_path, "YOUJU_SYNC_SECRET=" + "a" * 64 + "\n")
    done = _bash(tmp_path, "rotate_sync")
    assert done.returncode == 0, done.stderr
    new = env.read_text(encoding="utf-8").strip().split("=", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", new) and new != "a" * 64
    assert new not in done.stdout and _sha(new.encode())[:4] in done.stdout


SPLIT_COMPOSE = (
    "services:\n  mysql:\n    env_file: mysql-root.env\n"
    "  api:\n    env_file: [.env, db-api.env]\n  worker:\n    env_file: [.env, db-worker.env]\n"
)
OLD_ENV = (
    f"YOUJU_IMAGE=x\nMYSQL_ROOT_PASSWORD={ROOT_PW}\nMYSQL_PASSWORD={MIGRATE_PW}\n"
    f"YOUJU_DATABASE_URL={_url('youju', MIGRATE_PW)}\nYOUJU_SYNC_SECRET=s\n"
)
SPLIT_ENV = "YOUJU_IMAGE=x\nYOUJU_SYNC_SECRET=s\n"


def _old_layout(tmp_path: Path, env_text: str, compose: str = SPLIT_COMPOSE) -> Path:
    """还没把库账号分开放的 .env（只有 root 能读）、装好的 docker-compose.yml 和授权清单。"""
    server = tmp_path / "server"
    server.mkdir(exist_ok=True)
    env = server / ".env"
    env.write_text(env_text, encoding="utf-8")
    env.chmod(0o600)
    (server / "docker-compose.yml").write_text(compose, encoding="utf-8")
    shutil.copy(MYSQL_ACCOUNTS, server / "mysql_accounts.sql")
    return env


def _db_files(tmp_path: Path) -> dict[str, Path]:
    server = tmp_path / "server"
    return {name: server / name for name in ("mysql-root.env", "db-api.env", "db-worker.env")}


def _texts(files: dict[str, Path]) -> dict[str, str]:
    return {name: path.read_text(encoding="utf-8") for name, path in files.items()}


@needs_bash
def test_shared_split_db_env_gives_each_service_its_own_account(tmp_path):
    """数据库账号按最小权限分开：root、youju 的口令进 mysql-root.env，接口、worker 各生成一个自己的账号，
    .env 里不留连库口令；值不显示，文件都只有 root 能读；重跑不动，放到一半的补完。"""
    env = _old_layout(tmp_path, OLD_ENV, compose="services:\n  mysql:\n")
    # 装好的 compose 还是旧的（不认这几个口令文件）：什么都不动
    done = _bash(tmp_path, "split_db_env")
    assert done.returncode == 1 and "先 install 这一版" in done.stderr
    assert env.read_text(encoding="utf-8") == OLD_ENV
    assert not any(path.exists() for path in _db_files(tmp_path).values())
    env = _old_layout(tmp_path, OLD_ENV)
    done = _bash(tmp_path, "split_db_env")
    assert done.returncode == 0, done.stderr
    files = _db_files(tmp_path)
    assert env.read_text(encoding="utf-8") == SPLIT_ENV
    assert files["mysql-root.env"].read_text(encoding="utf-8") == (
        f"MYSQL_ROOT_PASSWORD={ROOT_PW}\nMYSQL_PASSWORD={MIGRATE_PW}\n"
    )
    made = {}
    for svc in ("api", "worker"):
        line = files[f"db-{svc}.env"].read_text(encoding="utf-8")
        pattern = rf"YOUJU_DATABASE_URL=mysql\+pymysql://youju_{svc}:([0-9a-f]{{48}}){re.escape(DB_SUFFIX)}\n"
        found = re.fullmatch(pattern, line)
        assert found, line
        made[svc] = found.group(1)
    assert made["api"] != made["worker"]
    for path in [env, *files.values()]:
        assert path.stat().st_mode & 0o777 == 0o600, path
    out = done.stdout + done.stderr
    assert not any(value in out for value in (ROOT_PW, MIGRATE_PW, *made.values()))
    assert not list((tmp_path / "server").glob("*.tmp"))
    # 重跑：已经分好的不动（接口、worker 的口令不换）
    before = _texts(files)
    assert _bash(tmp_path, "split_db_env").returncode == 0
    assert _texts(files) == before
    # 上次放到一半（口令文件写好了、.env 里还没删）：重跑把 .env 里那几项删掉，口令文件照旧
    env.write_text(OLD_ENV, encoding="utf-8")
    assert _bash(tmp_path, "split_db_env").returncode == 0
    assert env.read_text(encoding="utf-8") == SPLIT_ENV and _texts(files) == before


@needs_bash
@pytest.mark.parametrize(
    "env_text, existing, message",
    [
        (
            OLD_ENV.replace(ROOT_PW, "other1"),
            {"mysql-root.env": f"MYSQL_ROOT_PASSWORD={ROOT_PW}\n"},
            "MYSQL_ROOT_PASSWORD 和 .env 里的不一样",
        ),
        (OLD_ENV.replace("@mysql:3306", "@elsewhere:3306"), {}, "不是 youju 账号连本机 MySQL"),
        (
            OLD_ENV,
            {"db-api.env": f"YOUJU_DATABASE_URL={_url('root', 'x')}\n"},
            "db-api.env 写法不对",
        ),
        (f"YOUJU_IMAGE=x\nMYSQL_ROOT_PASSWORD={ROOT_PW}\n", {}, "都没有 MYSQL_PASSWORD"),
    ],
)
def test_shared_split_db_env_stops_before_touching_anything(tmp_path, env_text, existing, message):
    """口令对不上、连接串不是 youju 连本机的、已有的文件写法不对、缺口令：停下，一个文件都不动；只打指纹。"""
    _old_layout(tmp_path, env_text)
    for name, body in existing.items():
        (tmp_path / "server" / name).write_text(body, encoding="utf-8")
    server = tmp_path / "server"
    before = {p.name: p.read_text(encoding="utf-8") for p in server.iterdir() if p.is_file()}
    done = _bash(tmp_path, "split_db_env")
    assert done.returncode == 1 and message in done.stderr, done.stderr
    assert {
        p.name: p.read_text(encoding="utf-8") for p in server.iterdir() if p.is_file()
    } == before
    assert not any(value in done.stderr for value in (ROOT_PW, MIGRATE_PW, "other1"))


@needs_bash
def test_shared_unsplit_db_env_writes_back_for_a_rollback(tmp_path):
    """回退到不认这几个口令文件的旧版本之前：root、youju 的口令和 youju 的连接串写回 .env（值不显示）；
    写回以后新脚本的 up 会拦住，再升级回来照样分开。"""
    env = _old_layout(tmp_path, OLD_ENV)
    assert _bash(tmp_path, "split_db_env").returncode == 0
    done = _bash(tmp_path, "unsplit_db_env")
    assert done.returncode == 0, done.stderr
    text = env.read_text(encoding="utf-8")
    for line in (
        f"MYSQL_ROOT_PASSWORD={ROOT_PW}",
        f"MYSQL_PASSWORD={MIGRATE_PW}",
        f"YOUJU_DATABASE_URL={_url('youju', MIGRATE_PW)}",
    ):
        assert line + "\n" in text, line
    assert env.stat().st_mode & 0o777 == 0o600
    assert ROOT_PW not in done.stdout + done.stderr and MIGRATE_PW not in done.stdout + done.stderr
    assert _bash(tmp_path, "up", setup="status() { :; }").returncode == 1
    assert _bash(tmp_path, "split_db_env").returncode == 0
    assert env.read_text(encoding="utf-8") == SPLIT_ENV


@needs_bash
def test_shared_up_migrates_as_youju_then_sets_accounts_before_starting(tmp_path):
    """up：口令没分开放就停下、一个容器都不动（先停后起的 pause_services、import-export 也是）；分好以后先起
    MySQL、停接口和 worker，用 youju 迁移（连接串经环境变量），再在 MySQL 容器里用 root 设好接口、worker 的账号
    和授权（SQL 经标准输入），最后起 worker、接口。"""
    _old_layout(tmp_path, OLD_ENV)
    fake_status = "status() { echo STATUS; }"
    done = _bash(tmp_path, "up", setup=fake_status)
    assert done.returncode == 1 and "先运行 youju-shared.sh split-db-env" in done.stderr
    done = _bash(tmp_path, "pause_services")
    assert done.returncode == 1 and "split-db-env" in done.stderr
    assert _calls(tmp_path) == []
    _downloaded(tmp_path)
    done = _bash(tmp_path, "import_export a79a0042")
    assert done.returncode == 1 and "mysql-root.env 里没有 MYSQL_ROOT_PASSWORD" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    assert _bash(tmp_path, "split_db_env").returncode == 0
    (tmp_path / "calls.log").unlink(missing_ok=True)
    done = _bash(tmp_path, "pause_services; trap - EXIT")
    assert done.returncode == 0 and _calls(tmp_path)[0].startswith("compose stop api worker")
    (tmp_path / "calls.log").unlink()
    sql_file = tmp_path / "accounts.sql"
    log = tmp_path / "calls.log"
    # 跑测试的进程环境里有测试库的连接串（conftest 设的）；服务器上跑脚本的 root 环境里没有
    setup = (
        fake_status
        + "\nunset YOUJU_DATABASE_URL\n"
        + f'compose() {{ printf "compose %s | db=%s\\n" "$*" "${{YOUJU_DATABASE_URL:-}}" >> "{log}"; '
        + f'if [ "$1" = exec ]; then cat > "{sql_file}"; fi; }}'
    )
    done = _bash(tmp_path, "up", setup=setup)
    assert done.returncode == 0, done.stderr
    calls = _calls(tmp_path)
    assert [c.split(" | ")[0] for c in calls] == [
        "compose config -q",
        "compose up -d --wait --wait-timeout 300 mysql",
        "compose stop worker api",
        "compose run --rm --no-deps -e YOUJU_DATABASE_URL worker youju db-upgrade",
        "compose run --rm --no-deps -e YOUJU_DATABASE_URL worker youju sync-server-setup",
        'compose exec -T mysql sh -c MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B',
        "compose up -d --wait --wait-timeout 900 worker",
        "compose up -d --wait --wait-timeout 300 api",
    ]
    # 迁移、建收同步的进度表用 youju（经环境变量）；别的调用都不带连接串，口令都不在参数里
    assert calls[3].endswith(f"| db={_url('youju', MIGRATE_PW)}")
    assert calls[4].endswith(f"| db={_url('youju', MIGRATE_PW)}")
    assert all(c.endswith("| db=") for i, c in enumerate(calls) if i not in (3, 4))
    assert not any(MIGRATE_PW in c.split(" | ")[0] or ROOT_PW in c for c in calls)
    api_line = (tmp_path / "server" / "db-api.env").read_text(encoding="utf-8")
    api_pw = re.search(r"youju_api:([0-9a-f]{48})@", api_line).group(1)
    sql = sql_file.read_text(encoding="utf-8")
    assert f"CREATE USER IF NOT EXISTS 'youju_api'@'%' IDENTIFIED BY '{api_pw}';" in sql
    assert "接口（youju_api）、worker（youju_worker）的库账号和授权已照清单设好" in done.stdout
    assert "STATUS" in done.stdout


def _conftest_api_grants() -> list[str]:
    """tests/conftest.py 建受限账号时取接口授权的办法：GRANT 语句里 TO 'youju_api'@ 的那些。"""
    sql = MYSQL_ACCOUNTS.read_text(encoding="utf-8")
    return [
        s for s in re.findall(r"^GRANT\b[^;]*;", sql, re.M) if re.search(r"TO\s+'youju_api'@", s)
    ]


@needs_bash
def test_shared_db_accounts_sql_checks_tables_before_changing_grants(tmp_path):
    """接口、worker 的账号：建好、口令和 db-*.env 对齐；先逐条查授权用到的表和列都在，再收回、照清单重授。
    接口的授权就是 mysql_accounts.sql 第 ② 节那些行（和 conftest 建受限账号同一个取法），一条不漏。"""
    _server_env(tmp_path, f"YOUJU_IMAGE=x\nMYSQL_ROOT_PASSWORD={ROOT_PW}\n")
    shutil.copy(MYSQL_ACCOUNTS, tmp_path / "server" / "mysql_accounts.sql")
    done = _bash(tmp_path, "db_accounts_sql")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    grants = _conftest_api_grants()
    assert grants and lines[-len(grants) :] == grants
    assert lines[:4] == [
        f"CREATE USER IF NOT EXISTS 'youju_api'@'%' IDENTIFIED BY '{API_PW}';",
        f"ALTER USER 'youju_api'@'%' IDENTIFIED BY '{API_PW}';",
        f"CREATE USER IF NOT EXISTS 'youju_worker'@'%' IDENTIFIED BY '{WORKER_PW}';",
        f"ALTER USER 'youju_worker'@'%' IDENTIFIED BY '{WORKER_PW}';",
    ]
    revoke = lines.index(
        "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'youju_api'@'%', 'youju_worker'@'%';"
    )
    assert lines[revoke + 1] == (
        "GRANT SELECT, INSERT, UPDATE, DELETE, LOCK TABLES ON youju.* TO 'youju_worker'@'%';"
    )
    checks = lines[4:revoke]
    per_table = [g for g in grants if re.search(r" ON youju\.\w+ TO ", g)]
    assert len(checks) == len(per_table)  # 按表的授权每条查一句（youju.* 那条不用查）
    assert all(re.fullmatch(r"SELECT [a-z_, 1]+ FROM youju\.\w+ WHERE 1 = 0;", c) for c in checks)
    assert "SELECT ends_at FROM youju.boost_trials WHERE 1 = 0;" in checks
    assert "SELECT 1 FROM youju.access_logs WHERE 1 = 0;" in checks
    # 清单里没有接口的授权：停下
    (tmp_path / "server" / "mysql_accounts.sql").write_text("-- 空\n", encoding="utf-8")
    done = _bash(tmp_path, "db_accounts_sql")
    assert done.returncode == 1 and "没找到接口的授权" in done.stderr


def test_api_grants_name_tables_and_columns_that_exist():
    """mysql_accounts.sql 第 ② 节授权用到的表和列都在现在的表结构里：表或列删了要同时删掉 GRANT 行，不然共用
    服务器的 up 会停在改权限之前。"""
    from youju.models import Base

    tables = Base.metadata.tables
    for grant in _conftest_api_grants():
        found = re.search(r"^GRANT (.+) ON youju\.(\w+|\*) TO ", grant)
        assert found, grant
        if found.group(2) == "*":
            continue
        assert found.group(2) in tables, grant
        for cols in re.findall(r"\(([^)]*)\)", found.group(1)):
            for col in cols.split(","):
                assert col.strip() in tables[found.group(2)].c, grant


@needs_bash
def test_shared_status_names_each_services_account_without_secrets(tmp_path):
    """status（up 最后也打）：接口、worker 各用哪个库账号，里面有没有 root 和 youju 的口令；不打口令。"""
    _server_env(tmp_path, f"YOUJU_IMAGE=x\nMYSQL_ROOT_PASSWORD={ROOT_PW}\n")
    script = tmp_path / "probe.sh"
    setup = (
        "curl() { return 7; }; free() { :; }\n"
        # 容器里的 sh -c 脚本记下来；worker 当作查不了
        f'compose() {{ if [ "$1" = exec ]; then [ "$3" = worker ] && return 1; '
        f'printf "%s" "$6" > "{script}"; echo "youju_api 没有"; fi; }}'
    )
    done = _bash(tmp_path, "status", setup=setup)
    assert done.returncode == 0, done.stderr
    assert "api：连库账号 youju_api；root、youju 的口令：没有" in done.stdout
    assert "worker：查不了" in done.stdout
    probe = script.read_text(encoding="utf-8")
    clean = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "YOUJU_DATABASE_URL")
    }

    def run(extra: dict) -> str:
        got = subprocess.run(
            ["sh", "-c", probe], capture_output=True, text=True, env={**clean, **extra}
        )
        return got.stdout + got.stderr

    out = run({"YOUJU_DATABASE_URL": _url("youju_api", API_PW)})
    assert out.strip() == "youju_api 没有" and API_PW not in out
    out = run({"YOUJU_DATABASE_URL": _url("youju", MIGRATE_PW), "MYSQL_PASSWORD": MIGRATE_PW})
    assert out.strip() == "youju 有" and MIGRATE_PW not in out
    assert run({}).strip() == "无 没有"


@needs_bash
def test_shared_install_splits_accounts_before_building(tmp_path):
    """已装好的服务器 install 这一版：换文件之前先核对分得了；新 compose 一装上就把库账号分开放好、拷好授权
    清单，再构建镜像；分不了就停下，一个文件都不换、不构建（只打指纹）；回退装旧版本时不分，照常构建。"""
    ver = "abcdef12"
    rel = tmp_path / "releases" / ver
    (rel / "source" / "backend" / "deploy").mkdir(parents=True)
    (rel / "source" / "VERSION").write_text(ver + "\n", encoding="utf-8")
    shutil.copy(MYSQL_ACCOUNTS, rel / "source" / "backend" / "deploy" / "mysql_accounts.sql")
    shared = rel / "shared"
    shared.mkdir()
    (shared / "docker-compose.yml").write_text(SPLIT_COMPOSE, encoding="utf-8")
    for name in ("mysql.cnf", "nginx-http.conf", "nginx-https.conf", "youju-shared.sh"):
        (shared / name).write_text("x\n", encoding="utf-8")
    old_env = OLD_ENV.replace("YOUJU_IMAGE=x", "YOUJU_IMAGE=youju-selfhost:old")
    env = _old_layout(tmp_path, old_env, compose="old compose\n")
    (tmp_path / "server" / "mysql_accounts.sql").unlink()
    # 证书、验证目录也指到临时目录；跑测试的机器可能是 BSD sed：只替身 install 里那一处 sed -i
    portable = (
        f'WEBROOT="{tmp_path}/acme-webroot"; LE_DIR="{tmp_path}/letsencrypt"\n'
        'sed() { if [ "$1" = -i ]; then command sed "$2" "$3" > "$3.t" && mv "$3.t" "$3"; '
        'else command sed "$@"; fi; }'
    )
    done = _bash(tmp_path, f"install_release {ver}", setup=portable)
    assert done.returncode == 0, done.stderr
    files = _db_files(tmp_path)
    assert all(path.exists() for path in files.values())
    assert (
        env.read_text(encoding="utf-8")
        == f"YOUJU_IMAGE=youju-selfhost:{ver}\nYOUJU_SYNC_SECRET=s\n"
    )
    copied = tmp_path / "server" / "mysql_accounts.sql"
    assert copied.read_bytes() == MYSQL_ACCOUNTS.read_bytes()
    assert ROOT_PW not in done.stdout + done.stderr and MIGRATE_PW not in done.stdout + done.stderr
    assert done.stdout.index("按最小权限分开放") < done.stdout.index("构建镜像")
    assert any(c.startswith("docker build") for c in _calls(tmp_path))
    # 对不上：换文件之前就停下（只打两边的指纹），一个文件都不换，不构建
    (tmp_path / "calls.log").unlink()
    for path in files.values():
        path.unlink()
    env.write_text(OLD_ENV, encoding="utf-8")
    files["mysql-root.env"].write_text("MYSQL_ROOT_PASSWORD=other1\n", encoding="utf-8")
    installed = tmp_path / "server" / "docker-compose.yml"
    installed.write_text("old compose\n", encoding="utf-8")
    script = tmp_path / "bin" / "youju-shared.sh"
    script.write_text("old script\n", encoding="utf-8")
    done = _bash(tmp_path, f"install_release {ver}", setup=portable)
    assert done.returncode == 1 and "不一样" in done.stderr and "一个文件都没换" in done.stderr
    assert _sha(b"other1")[:4] in done.stderr and _sha(ROOT_PW.encode())[:4] in done.stderr
    assert "other1" not in done.stderr and ROOT_PW not in done.stdout + done.stderr
    assert installed.read_text(encoding="utf-8") == "old compose\n"
    assert script.read_text(encoding="utf-8") == "old script\n"
    assert not any(c.startswith("docker build") for c in _calls(tmp_path))
    # 回退：装的是还不认这几个文件的旧版本（口令已经写回 .env）：不分，照常构建
    (tmp_path / "calls.log").unlink(missing_ok=True)
    files["mysql-root.env"].unlink()
    (shared / "docker-compose.yml").write_text("services:\n  mysql:\n", encoding="utf-8")
    done = _bash(tmp_path, f"install_release {ver}", setup=portable)
    assert done.returncode == 0, done.stderr
    assert f"MYSQL_PASSWORD={MIGRATE_PW}" in env.read_text(encoding="utf-8")
    assert not any(path.exists() for path in files.values())
    assert any(c.startswith("docker build") for c in _calls(tmp_path))


@needs_bash
def test_shared_make_env_writes_db_secrets_per_account(tmp_path):
    """新装（env）：.env 里没有连库口令；root 和 youju 的在 mysql-root.env，接口、worker 各一个账号；
    都只有 root 能读，已有就不动；只剩口令文件、没有 .env 时不另生成。"""
    if shutil.which("openssl") is None:
        pytest.skip("没有 openssl")
    ver = "abcdef12"
    rel = tmp_path / "releases" / ver / "source"
    (rel / "backend/deploy/selfhost/server").mkdir(parents=True)
    (rel / "VERSION").write_text(ver + "\n", encoding="utf-8")
    (rel / "backend/deploy/selfhost/server/env.example").write_text(
        "YOUJU_CLOUD_ENV=test-env\n", encoding="utf-8"
    )
    server = tmp_path / "server"
    server.mkdir()
    (server / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    done = _bash(tmp_path, f"make_env {ver}")
    assert done.returncode == 0, done.stderr
    env, files = server / ".env", _db_files(tmp_path)
    text = env.read_text(encoding="utf-8")
    for name in ("MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "YOUJU_DATABASE_URL"):
        assert name + "=" not in text, name
    found = re.fullmatch(
        r"MYSQL_ROOT_PASSWORD=([0-9a-f]{48})\nMYSQL_PASSWORD=([0-9a-f]{48})\n",
        files["mysql-root.env"].read_text(encoding="utf-8"),
    )
    assert found and found.group(1) != found.group(2)
    secrets = [found.group(1), found.group(2)]
    for svc in ("api", "worker"):
        line = files[f"db-{svc}.env"].read_text(encoding="utf-8")
        pattern = rf"YOUJU_DATABASE_URL=mysql\+pymysql://youju_{svc}:([0-9a-f]{{48}}){re.escape(DB_SUFFIX)}\n"
        url = re.fullmatch(pattern, line)
        assert url, line
        secrets.append(url.group(1))
    assert len(set(secrets)) == 4 and not any(s in text or s in done.stdout for s in secrets)
    for path in [env, *files.values()]:
        assert path.stat().st_mode & 0o777 == 0o600, path
    assert not list(server.glob("*.tmp"))
    before = _texts(files)
    again = _bash(tmp_path, f"make_env {ver}")
    assert again.returncode == 0 and "不动" in again.stdout and _texts(files) == before
    # 只剩口令文件（数据库可能已经用它们建好了）：不另生成
    env.unlink()
    done = _bash(tmp_path, f"make_env {ver}")
    assert done.returncode == 1 and "先人工核对" in done.stderr and not env.exists()


@needs_bash
def test_shared_fetch_export_reads_link_in_a_terminal(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00t\n")
    sha = "ab" * 32
    link = f"https://{HOST}/exports/manifest-1.json?sign=s&t=1"
    # 参数不对、不在交互终端里（"执行命令"会把输入存进执行记录）：什么都没停、没下
    for body in ("fetch_export", f"fetch_export {sha} --with-advisor"):
        done = _bash(tmp_path, body)
        assert done.returncode == 1 and "fetch-export <哈希>" in done.stderr
    done = _bash(tmp_path, f"fetch_export {sha}", stdin=link + "\n")
    assert done.returncode == 1 and "只能在交互终端里用" in done.stderr
    assert _calls(tmp_path) == []
    # 交互终端里按提示粘贴：链接不对就停下
    code, out = _tty(
        ["bash", "-c", _script(tmp_path, f"fetch_export {sha}")], "https://evil.example.com/x\n"
    )
    assert code == 1 and "清单链接不对" in out and _calls(tmp_path) == []
    # 网页终端复制时带上的首尾空白去掉
    code, out = _tty(
        ["bash", "-c", _script(tmp_path, f"fetch_export {sha}")], "  " + link + " \t\r\n"
    )
    assert code == 0, out
    assert link not in out  # 粘贴的链接不显示
    calls = _calls(tmp_path)
    # 先停接口和 worker，结束再起来；链接只经环境变量进容器，不在命令行参数里
    assert calls[0].startswith("compose stop api worker")
    run = calls[1]
    assert run.startswith("compose run --rm --no-deps -e YOUJU_EXPORT_URL -v ")
    assert f"youju export-fetch --sha256 {sha} --dir /export |" in run
    assert run.endswith(f"url={link}") and link not in run.split(" | ")[0]
    # 退出时先看有没有还在跑的一次性容器（这里没有），再起服务
    assert calls[2].startswith(
        "docker ps -q --filter label=com.docker.compose.project=youju-server"
    )
    assert calls[3].startswith("compose up -d --wait --wait-timeout 900 worker")
    assert calls[4].startswith("compose up -d --wait --wait-timeout 300 api")
    assert (tmp_path / "export").stat().st_mode & 0o777 == 0o700


def _downloaded(tmp_path: Path, *, complete: bool = True) -> Path:
    backups = tmp_path / "export" / "backups"
    backups.mkdir(parents=True)
    (backups / "youju-20261112.jsonl.gz").write_bytes(_gz('{"format":"youju-backup"}\n'))
    (tmp_path / "export" / "pay-records.jsonl.gz").write_bytes(b"p")
    (tmp_path / "export" / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "export" / "advisor-out" / "u").mkdir(parents=True)
    (tmp_path / "export" / "advisor-out" / "u" / "1.xlsx").write_bytes(b"x")
    (tmp_path / "export" / "advisor-out.json").write_text("[]", encoding="utf-8")
    if complete:
        (tmp_path / "export" / "fetch-complete").write_text("x\n", encoding="utf-8")
    return backups


@needs_bash
def test_shared_import_export_needs_a_complete_fetch(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    backups = _downloaded(tmp_path, complete=False)
    done = _bash(tmp_path, "import_export a79a0042")
    assert done.returncode == 1 and "下载还没全部完成" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    assert (backups / "youju-20261112.jsonl.gz").exists()
    assert (tmp_path / "export" / "manifest.json").exists()


@needs_bash
def test_shared_import_export_drills_with_old_image_then_archives(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    backups = _downloaded(tmp_path)
    done = _bash(tmp_path, "import_export a79a0042")
    assert done.returncode == 0, done.stderr
    calls = [c for c in _calls(tmp_path) if not c.startswith("docker info")]
    assert calls[0] == "docker image inspect youju-selfhost:a79a0042"
    assert calls[1].startswith("compose stop api worker")
    drill, purge, archive = calls[2], calls[3], calls[4]
    # 试恢复：旧镜像、root 连接串都经环境变量（不在命令行参数里）
    assert drill.startswith("compose run --rm --no-deps -e YOUJU_DATABASE_URL -v ")
    assert "youju restore-drill --file /export/backups/youju-20261112.jsonl.gz |" in drill
    assert "image=youju-selfhost:a79a0042" in drill
    assert "db=mysql+pymysql://root:r00tpw@mysql:3306/youju?charset=utf8mb4 " in drill
    assert "r00tpw" not in drill.split(" | ")[0]
    # 试恢复写进 binlog 的旧库明文清掉（脚本经标准输入给容器里的 sh，见下面的测试）
    assert purge.startswith("compose exec -T mysql sh -s |") and "r00tpw" not in purge
    assert "youju archive-export --dir /export |" in archive and "image= " in archive
    # 明文的整库备份、交易记录和带链接的清单删掉；留下"存档过"的标记（再跑 fetch-export 会拒绝）
    assert not (backups / "youju-20261112.jsonl.gz").exists()
    assert not (tmp_path / "export" / "pay-records.jsonl.gz").exists()
    assert not (tmp_path / "export" / "manifest.json").exists()
    assert not (tmp_path / "export" / "advisor-out").exists()
    assert not (tmp_path / "export" / "advisor-out.json").exists()
    assert (tmp_path / "export" / "imported").exists()
    assert "IMPORT OK" in done.stdout


@needs_bash
def test_shared_import_export_checks_disk_before_the_drill(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    _downloaded(tmp_path)
    # 只差一点（按 G 取整会算成够）：按字节比，不够
    near = 2 * 1024**3 + 3 * len(b'{"format":"youju-backup"}\n') - 1
    done = _bash(
        tmp_path,
        "import_export a79a0042",
        setup=f"df() {{ printf 'Avail\\n {near}\\n'; }}",
    )
    assert done.returncode == 1 and "硬盘不够试恢复" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    # docker 的数据目录（MySQL 数据卷）在另一块盘上、那块不够：也停下
    root = tmp_path / "docker-root"
    root.mkdir()
    setup = (
        f'docker() {{ printf "docker %s\\n" "$*" >> "{tmp_path}/calls.log"; '
        f'if [ "$1" = info ]; then echo "{root}"; fi; }}\n'
        f'df() {{ case "$*" in *"{root}"*) printf "Avail\\n 1073741824\\n" ;; '
        '*) printf "Avail\\n 107374182400\\n" ;; esac; }'
    )
    done = _bash(tmp_path, "import_export a79a0042", setup=setup)
    assert done.returncode == 1 and "硬盘不够试恢复" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    # 读不出 docker 的数据目录：停下，不当作没有这块盘
    setup = f'docker() {{ printf "docker %s\\n" "$*" >> "{tmp_path}/calls.log"; }}'
    done = _bash(tmp_path, "import_export a79a0042", setup=setup)
    assert done.returncode == 1 and "读不出 docker 的数据目录" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]


@needs_bash
def test_shared_cloudrun_gone_records_through_the_worker(tmp_path):
    done = _bash(tmp_path, "cloudrun_gone later")
    assert done.returncode == 1 and "deregistered|deleted" in done.stderr
    done = _bash(tmp_path, "cloudrun_gone deleted")
    assert done.returncode == 0, done.stderr
    assert _calls(tmp_path)[0].startswith(
        "compose exec -T worker youju cloudrun-gone --how deleted"
    )


@needs_bash
def test_shared_export_reset_clears_the_last_batch_marks(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    done = _bash(tmp_path, "export_reset")
    assert done.returncode == 1 and "不用重置" in done.stderr
    for name in ("imported", "fetch-complete", "summary.json", "fetched.json"):
        (export / name).write_text("{}", encoding="utf-8")
    # 明文还在：停下查明
    (export / "pay-records.jsonl.gz").write_bytes(b"p")
    done = _bash(tmp_path, "export_reset")
    assert done.returncode == 1 and "明文" in done.stderr and (export / "imported").exists()
    (export / "pay-records.jsonl.gz").unlink()
    done = _bash(tmp_path, "export_reset")
    assert done.returncode == 0, done.stderr
    assert not (export / "imported").exists() and not (export / "fetch-complete").exists()
    assert not (export / "summary.json").exists()
    assert (export / "fetched.json").exists()  # 下载过、没变的照样按 ETag 跳过


@needs_bash
def test_shared_alert_probe_sends_through_the_worker(tmp_path):
    done = _bash(tmp_path, "alert_probe")
    assert done.returncode == 0, done.stderr
    assert _calls(tmp_path)[0].startswith("compose exec -T worker youju alert-probe --send")


@needs_bash
def test_shared_import_export_stops_when_drill_fails(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    backups = _downloaded(tmp_path)
    log = tmp_path / "calls.log"
    failing = (
        f'compose() {{ echo "compose $*" >> "{log}"; '
        'if [ "$1" = exec ]; then cat >/dev/null; fi; [ "$1" != run ]; }'
    )
    done = _bash(tmp_path, "import_export a79a0042", setup=failing)
    assert done.returncode != 0
    calls = _calls(tmp_path)
    assert not any("archive-export" in c for c in calls)  # 试恢复不过就不存档、不删明文
    assert "compose exec -T mysql sh -s" in calls  # binlog 照样清
    assert "试恢复没通过" in done.stderr
    assert (backups / "youju-20261112.jsonl.gz").exists()
    assert not (tmp_path / "export" / "imported").exists()
    assert calls[-1].startswith(
        "compose up -d --wait --wait-timeout 300 api"
    )  # 接口、worker 照样起来


def _drill_setup(tmp_path: Path, hhmm: str = "1430") -> str:
    """恢复演练用：docker volume inspect 指到临时目录里的"存储卷"，北京时间固定成 hhmm。"""
    log = tmp_path / "calls.log"
    return (
        f'docker() {{ printf "docker %s\\n" "$*" >> "{log}"; case "$1" in '
        f'info) echo "{tmp_path}" ;; volume) echo "{tmp_path}/volume" ;; esac; }}\n'
        f"beijing_hhmm() {{ echo {hhmm}; }}"
    )


def _drill_backups(tmp_path: Path, *names: str) -> Path:
    backups = tmp_path / "volume" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    for name in names:
        (backups / name).write_bytes(gzip.compress(b'{"format":"youju-backup"}\n'))
    return backups


@needs_bash
def test_shared_restore_drill_uses_root_and_the_newest_backup(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    # 名字不对的（没有 8 位日期、没压完的）不算
    _drill_backups(
        tmp_path,
        "youju-20261118.jsonl.gz",
        "youju-20261119.jsonl.gz",
        "youju-2026112.jsonl.gz",
        "youju-20261120.jsonl.gz.part",
    )
    done = _bash(tmp_path, "restore_drill", setup=_drill_setup(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "DRILL OK youju-20261119.jsonl.gz" in done.stdout
    calls = [c for c in _calls(tmp_path) if c.startswith("compose")]
    # 只停 worker（临时容器用它那份内存），接口照常
    assert calls[0].startswith("compose stop worker |")
    drill = calls[1]
    assert drill.startswith(
        "compose run --rm --no-deps -e YOUJU_DATABASE_URL worker "
        "youju restore-drill --file /data/storage/backups/youju-20261119.jsonl.gz |"
    )
    # root 连接串经环境变量（不在命令行参数里）；镜像用 .env 里现在这一版
    assert "db=mysql+pymysql://root:r00tpw@mysql:3306/youju?charset=utf8mb4 " in drill
    assert "r00tpw" not in drill.split(" | ")[0]
    assert "image= " in drill
    # 服务器自己的备份：不清 binlog（库里本来就有这些数据，binlog 3 天过期）
    assert not any(c.startswith("compose exec -T mysql") for c in calls)
    # 结束把 worker 起来（接口本来就在跑，up 一下不影响）
    assert calls[2].startswith("compose up -d --wait --wait-timeout 900 worker")
    assert calls[3].startswith("compose up -d --wait --wait-timeout 300 api")


@needs_bash
@pytest.mark.parametrize(
    ("hhmm", "ok"),
    [("0959", False), ("1000", True), ("2330", True), ("2331", False), ("0415", False)],
)
def test_shared_restore_drill_runs_only_in_the_daytime(tmp_path, hhmm, ok):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    _drill_backups(tmp_path, "youju-20261119.jsonl.gz")
    done = _bash(tmp_path, "restore_drill", setup=_drill_setup(tmp_path, hhmm))
    compose = [c for c in _calls(tmp_path) if c.startswith("compose")]
    if ok:
        assert done.returncode == 0, done.stderr
    else:
        assert done.returncode == 1 and "只在 10:00–23:30 之间跑" in done.stderr
        assert not compose  # 什么都没停


@needs_bash
def test_shared_restore_drill_needs_a_backup_and_enough_disk(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    (tmp_path / "volume" / "backups").mkdir(parents=True)
    done = _bash(tmp_path, "restore_drill", setup=_drill_setup(tmp_path))
    assert done.returncode == 1 and "存储里还没有整库备份" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    _drill_backups(tmp_path, "youju-20261119.jsonl.gz")
    small = _drill_setup(tmp_path) + "\ndf() { printf 'Avail\\n 1073741824\\n'; }"
    done = _bash(tmp_path, "restore_drill", setup=small)
    assert done.returncode == 1 and "硬盘不够试恢复" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    # 读不出存储卷的位置：停下
    done = _bash(tmp_path, "restore_drill", setup="beijing_hhmm() { echo 1430; }")
    assert done.returncode == 1 and "读不出存储卷的位置" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]


@needs_bash
def test_shared_restore_drill_brings_the_worker_back_when_it_fails(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    _drill_backups(tmp_path, "youju-20261119.jsonl.gz")
    log = tmp_path / "calls.log"
    failing = (
        _drill_setup(tmp_path) + f'\ncompose() {{ echo "compose $*" >> "{log}"; '
        'if [ "$1" = exec ]; then cat >/dev/null; fi; [ "$1" != run ]; }'
    )
    done = _bash(tmp_path, "restore_drill", setup=failing)
    assert done.returncode != 0
    assert "恢复演练没通过" in done.stderr and "DRILL OK" not in done.stdout
    calls = [c for c in _calls(tmp_path) if c.startswith("compose")]
    assert calls[0] == "compose stop worker"
    assert calls[-1].startswith("compose up -d --wait --wait-timeout 300 api")
    assert "compose up -d --wait --wait-timeout 900 worker" in calls


@needs_bash
def test_shared_takedowns_prints_parts_that_decode(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    listing = "".join(
        json.dumps(
            {
                "url_key": f"example.gov.cn/{n}.html",
                "origin_source_id": n,
                "taken_down_at": "2026-10-20T08:00:00+00:00",
                "reason": "\\u5176\\u4ed6",
            }
        )
        + "\n"
        for n in range(3000)
    )
    (export / "takedowns.jsonl").write_text(listing, encoding="ascii")
    first = _bash(tmp_path, "takedowns")
    assert first.returncode == 0, first.stderr
    header = first.stdout.splitlines()[0]
    parts = int(re.search(r"共 (\d+) 段", header).group(1))
    assert parts >= 2 and _sha(listing.encode()) in header
    lines = first.stdout.splitlines()[1:]
    for part in range(2, parts + 1):
        lines += _bash(tmp_path, f"takedowns {part}").stdout.splitlines()[1:]
    assert all(len(line) <= 76 for line in lines)
    assert gzip.decompress(base64.b64decode("".join(lines))).decode() == listing


@needs_bash
def test_local_set_secret(tmp_path):
    shutil.copy(SELFHOST / "local" / "set-secret.sh", tmp_path)
    env = tmp_path / ".env"
    env.write_text(
        "YOUJU_SYNC_URL=https://yj.example.cn\r\nYOUJU_SYNC_SECRET=old\r\n", encoding="utf-8"
    )
    done = subprocess.run(
        ["bash", str(tmp_path / "set-secret.sh"), "YOUJU_SYNC_SECRET"],
        input="x\n",
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1 and "只能在交互终端里用" in done.stderr
    value = "b" * 64
    code, out = _tty(["bash", str(tmp_path / "set-secret.sh"), "YOUJU_SYNC_SECRET"], value + "\n")
    assert code == 0, out
    assert value not in out and f"指纹 {_sha(value.encode())[:4]}" in out
    assert (
        env.read_text(encoding="utf-8")
        == f"YOUJU_SYNC_URL=https://yj.example.cn\nYOUJU_SYNC_SECRET={value}\n"
    )
    code, out = _tty(["bash", str(tmp_path / "set-secret.sh"), "YOUJU_SYNC_SECRET"], "short\n")
    assert code == 1 and "格式不对" in out
    code, out = _tty(["bash", str(tmp_path / "set-secret.sh"), "YOUJU_ADMIN_TOKEN"], "x\n")
    assert code == 1 and "用法" in out
    # 后台 AI 的密钥也在本机录（和服务器同一个）：不回显，只给指纹
    key = "sk-0123456789abcdef0123"
    code, out = _tty(["bash", str(tmp_path / "set-secret.sh"), "YOUJU_AI_API_KEY"], key + "\n")
    assert code == 0, out
    assert key not in out and f"指纹 {_sha(key.encode())[:4]}" in out
    assert env.read_text(encoding="utf-8").endswith(f"YOUJU_AI_API_KEY={key}\n")


def _end_line(count: int) -> str:
    """show ai 的最后一行（纯 ASCII 的开头加中文说明）。"""
    return f"#END {count} （以上 {count} 项；YOUJU_AI_API_KEY 不打印：35 位，指纹 ab12；本机用 set-secret.sh 录同一个，指纹要一样）"


@needs_bash
def test_local_set_config(tmp_path, monkeypatch):
    shutil.copy(SELFHOST / "local" / "set-config.sh", tmp_path)
    env = tmp_path / ".env"
    before = (
        "YOUJU_SYNC_URL=https://yj.example.cn\r\n# YOUJU_AI_MODEL=\r\n"
        "YOUJU_AI_MODEL=old-model\r\nYOUJU_AI_BACKGROUND_MODEL=old-bg\r\nYOUJU_AI_MODEL=dup\r\n"
    )
    env.write_text(before, encoding="utf-8", newline="")
    script = ["bash", str(tmp_path / "set-config.sh")]
    # 附加参数照 show ai 的打法：纯 ASCII、紧凑，中文写成转义
    extra = json.dumps(
        {"thinking": {"type": "disabled"}, "stop": ["\n"], "note": "中文 a#b"},
        ensure_ascii=True,
        separators=(",", ":"),
    )

    done = subprocess.run(script, input="x\n", capture_output=True, text=True)
    assert done.returncode == 1 and "只能在交互终端里用" in done.stderr

    # 换行变成回车加换行、行尾带空格、#END 那行的中文复制成乱码：照样认
    pasted = (
        "YOUJU_AI_BASE_URL=https://api.deepseek.com:443/v1 \r\n"
        "YOUJU_AI_MODEL=deepseek-chat\t\r\n"
        f"YOUJU_AI_REQUEST_EXTRA={extra}\r\n"
        "#END 3 ���\r\n"
        "YOUJU_AI_MODEL=after-the-end-line-is-not-read\r\n"
    )
    # 上次留下的 .env.tmp（权限宽）不碍事：临时文件每次用 mktemp 新建，用完不留
    stale = tmp_path / ".env.tmp"
    stale.write_text("junk\n", encoding="utf-8")
    stale.chmod(0o644)
    code, out = _tty(script, pasted)
    assert code == 0, out
    assert stale.read_text(encoding="utf-8") == "junk\n"
    assert sorted(x.name for x in tmp_path.glob(".env*")) == [".env", ".env.tmp"]
    # 原来有的就地改、重复的旧行去掉、没有的加在最后；粘贴里没有的（服务器没填）本机也去掉；
    # 读到 #END 就停；注释行原样留下
    assert env.read_bytes().decode() == (
        "YOUJU_SYNC_URL=https://yj.example.cn\n# YOUJU_AI_MODEL=\n"
        "YOUJU_AI_MODEL=deepseek-chat\n"
        "YOUJU_AI_BASE_URL=https://api.deepseek.com:443/v1\n"
        f"YOUJU_AI_REQUEST_EXTRA={extra}\n"
    )
    assert env.stat().st_mode & 0o777 == 0o600
    # 只回名称和长度，不回显粘贴的值
    assert "deepseek-chat" not in out and "api.deepseek.com" not in out and "thinking" not in out
    assert "已写入 YOUJU_AI_MODEL（13 位）" in out
    assert "YOUJU_AI_BACKGROUND_MODEL：服务器没填，本机也不填" in out
    # 程序按这份 .env 读回来的和粘贴的一样（附加参数是同一个字典）
    from youju.config import Settings

    for name in ("YOUJU_AI_BASE_URL", "YOUJU_AI_MODEL", "YOUJU_AI_REQUEST_EXTRA"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=str(env))
    assert settings.ai_base_url == "https://api.deepseek.com:443/v1"
    assert settings.ai_model == "deepseek-chat"
    assert settings.ai_request_extra == json.loads(extra)

    # 服务器四项都没填：只有 #END 0，本机这几项都去掉
    code, out = _tty(script, _end_line(0) + "\n")
    assert code == 0, out
    assert env.read_bytes().decode() == "YOUJU_SYNC_URL=https://yj.example.cn\n# YOUJU_AI_MODEL=\n"

    # 有一行不对、没粘到 #END、行数和项数对不上（漏了开头几行），都整批不写；
    # 不认识的行、误粘的密钥都不打出来；行号不算空行
    env.write_text(before, encoding="utf-8", newline="")
    secret = "sk-pasted-by-mistake-0123456789"
    bad_batches = {
        f"YOUJU_AI_MODEL=deepseek-chat\r\nYOUJU_AI_API_KEY={secret}\r\n{_end_line(1)}\n": "第 2 行不认识",
        f"{secret}\n{_end_line(0)}\n": "第 1 行不认识",
        f"YOUJU_AI_MODEL={secret}\n{_end_line(1)}\n": "格式不对",
        f"YOUJU_AI_MODEL=x{'a1' * 16}\n{_end_line(1)}\n": "格式不对",
        "YOUJU_AI_MODEL=deepseek-chat\n\x04": "没粘到最后那行 #END",
        f"YOUJU_AI_MODEL=deepseek-chat\nYOUJU_AI_REQUEST_EXTRA={{}}\n{_end_line(3)}\n": "可能没复制全",
        f"YOUJU_AI_MODEL=a\nYOUJU_AI_MODEL=b\n{_end_line(2)}\n": "出现了两次",
        f"YOUJU_AI_REQUEST_EXTRA=[1, 2]\n{_end_line(1)}\n": "格式不对",
        f"YOUJU_AI_REQUEST_EXTRA={{bad\n{_end_line(1)}\n": "格式不对",
        f'YOUJU_AI_REQUEST_EXTRA={{"k": "{secret} #x"}}\n{_end_line(1)}\n': "格式不对",
        f'YOUJU_AI_REQUEST_EXTRA={{"k": "$HOME"}}\n{_end_line(1)}\n': "格式不对",
        f'YOUJU_AI_REQUEST_EXTRA={{"note": "{chr(0xFFFD) * 2}"}}\n{_end_line(1)}\n': "格式不对",
        f"YOUJU_AI_MODEL=model-$(reboot)\n{_end_line(1)}\n": "格式不对",
        f"YOUJU_AI_BASE_URL=http://api.deepseek.com/v1\n{_end_line(1)}\n": "格式不对",
        f"YOUJU_AI_BASE_URL=https://api.deepseek.com:99999/v1\n{_end_line(1)}\n": "格式不对",
        f"YOUJU_AI_BASE_URL=https://api.deepseek.com:0/v1\n{_end_line(1)}\n": "格式不对",
        f"YOUJU_AI_BASE_URL=https://api.deepseek.com/v1?key={secret}\n{_end_line(1)}\n": "格式不对",
    }
    for text, message in bad_batches.items():
        code, out = _tty(script, text)
        assert code == 1 and message in out, (text, out)
        assert secret not in out
        assert env.read_bytes() == before.encode(), text  # 原样没动（连 CRLF 都没改）

    # 写临时文件失败（目录不能写）：报错退出，.env 原样（root 不受目录权限限制，跳过）
    if os.geteuid() != 0:
        tmp_path.chmod(0o555)
        try:
            code, out = _tty(script, f"YOUJU_AI_MODEL=deepseek-chat\n{_end_line(1)}\n")
        finally:
            tmp_path.chmod(0o755)
        assert code != 0, out
        assert env.read_bytes() == before.encode()

    env.unlink()
    code, out = _tty(script, f"YOUJU_AI_MODEL=deepseek-chat\n{_end_line(1)}\n")
    assert code == 1 and "还没有 .env" in out


@needs_bash
def test_local_set_config_checks_json_in_the_worker_without_python(tmp_path):
    """电脑的发行版里没有 python3 时，附加参数改在 worker 镜像里查（docker compose run）。"""
    shutil.copy(SELFHOST / "local" / "set-config.sh", tmp_path)
    env = tmp_path / ".env"
    env.write_text("YOUJU_SYNC_URL=https://yj.example.cn\n", encoding="utf-8")
    # PATH 里只有脚本要用的几个命令，没有 python3；docker 换成假的：记下参数，用真的 python 查
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "stty", "dd", "wc", "tr", "grep", "mktemp", "mv", "rm", "seq", "cat"):
        os.symlink(shutil.which(tool), bin_dir / tool)
    log = tmp_path / "docker.log"
    real_python = sys.executable
    (bin_dir / "docker").write_text(
        f'#!/bin/bash\necho "$@" >> "{log}"\n'
        # docker compose run --rm --no-deps -T worker python -I -c <代码>
        f'[ "$1 $2 $6 $7" = "compose run worker python" ] || exit 99\nshift 7\nexec "{real_python}" "$@"\n',
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)
    run_env = {**os.environ, "PATH": str(bin_dir)}
    script = [shutil.which("bash"), str(tmp_path / "set-config.sh")]
    extra = '{"thinking":{"type":"disabled"}}'
    code, out = _tty(script, f"YOUJU_AI_REQUEST_EXTRA={extra}\n{_end_line(1)}\n", env=run_env)
    assert code == 0, out
    assert env.read_text(encoding="utf-8").endswith(f"YOUJU_AI_REQUEST_EXTRA={extra}\n")
    assert "compose run --rm --no-deps -T worker python -I -c" in log.read_text(encoding="utf-8")
    code, out = _tty(script, f"YOUJU_AI_REQUEST_EXTRA=[1]\n{_end_line(1)}\n", env=run_env)
    assert code == 1 and "格式不对" in out


@needs_bash
def test_local_set_config_drains_leftover_paste_and_restores_the_terminal(tmp_path):
    """读完、出错、中途被停下都先把终端里没读完的粘贴内容丢掉（不然会交给外面的终端当命令执行，
    万一是密钥还会在报错里打出来），再恢复回显。"""
    shutil.copy(SELFHOST / "local" / "set-config.sh", tmp_path)
    (tmp_path / ".env").write_text("YOUJU_SYNC_URL=https://yj.example.cn\n", encoding="utf-8")
    secret = "sk-leftover-0123456789"
    # 跑完打出退出码、回显恢复没有（echo / -echo）、终端里还剩什么
    # 剩没剩：换成非规范模式读（没按回车的半行也读得到），等 2 秒
    after = (
        'echo "EXIT=$?"; stty -a | tr " " "\\n" | grep -x -e echo -e -echo; '
        "stty -icanon min 0 time 20; left=$(dd bs=4096 count=1 2>/dev/null); stty icanon; "
        'if [ -n "$left" ]; then echo "LEFT=$left"; else echo "LEFT-NONE"; fi'
    )
    run = f'stty echo; bash "{tmp_path}/set-config.sh"; {after}'
    # 中途被停下：测试的伪终端不是控制终端，Ctrl-C 发不出信号，这里直接发 TERM（和 INT 走同一段恢复）；
    # 放到后台时标准输入要明写 <&0，不然是 /dev/null
    # 等它关掉回显再发（关回显之前已经装好了信号处理），不靠固定等几秒
    stopped = (
        f'stty echo; bash "{tmp_path}/set-config.sh" <&0 & pid=$!; '
        'for i in $(seq 100); do stty -a | tr " " "\\n" | grep -qx -- -echo && break; sleep 0.1; done; '
        f"kill -TERM $pid; wait $pid; {after}"
    )
    cases = {
        "读完": (
            run,
            f"YOUJU_AI_MODEL=deepseek-chat\n{_end_line(1)}\n{secret}\necho PWNED\n",
            "EXIT=0",
        ),
        "出错": (
            run,
            f"YOUJU_AI_MODEL=bad model\n{secret}\necho PWNED\n{_end_line(1)}\n",
            "EXIT=1",
        ),
        "停下": (stopped, "", "EXIT=130"),
        # 最后一行没按回车（规范模式下读不到的半行）也要丢掉
        "半行": (run, f"YOUJU_AI_MODEL=deepseek-chat\n{_end_line(1)}\n{secret}", "EXIT=0"),
        # 一下子粘了很多（远多于 30 次 4KB 的读）：照样全丢掉
        "很多": (
            run,
            f"YOUJU_AI_MODEL=deepseek-chat\n{_end_line(1)}\n"
            + "".join(f"echo PWNED-{i:05d} {'y' * 40}\n" for i in range(3000))
            + secret,
            "EXIT=0",
        ),
    }
    for label, (wrapper, typed, exit_line) in cases.items():
        code, out = _tty(["bash", "-c", wrapper], typed)
        lines = out.replace("\r", "").splitlines()
        assert exit_line in lines, (label, out)
        assert "echo" in lines and "-echo" not in lines, (label, out)
        assert "LEFT-NONE" in lines, (label, out)
        assert secret not in out and "PWNED" not in out, (label, out)


@needs_bash
def test_local_set_config_switch(tmp_path):
    """本机自己的开关（截止日期自动核对，D-135）：只收 0 或 1；命令后面、粘贴里都能给，给了才改，
    不算进 AI 配置的行数；--only 只改开关、不粘贴，AI 那几项原样留着。"""
    shutil.copy(SELFHOST / "local" / "set-config.sh", tmp_path)
    env = tmp_path / ".env"
    env.write_text(
        "YOUJU_AI_MODEL=deepseek-chat\nYOUJU_SYNC_URL=https://yj.example.cn\n", encoding="utf-8"
    )
    script = ["bash", str(tmp_path / "set-config.sh")]
    # #END 后面什么都没有也认
    paste = "YOUJU_AI_MODEL=deepseek-chat\n#END 1\n"

    # 上线那天：命令后面带开关，再粘贴 show ai 的几行
    code, out = _tty([*script, "YOUJU_DEADLINE_AUTO_VERIFY=1"], paste)
    assert code == 0, out
    assert env.read_text(encoding="utf-8") == (
        "YOUJU_AI_MODEL=deepseek-chat\nYOUJU_SYNC_URL=https://yj.example.cn\n"
        "YOUJU_DEADLINE_AUTO_VERIFY=1\n"
    )
    assert "YOUJU_DEADLINE_AUTO_VERIFY=1（截止日期自动核对：开）" in out

    # 以后再粘贴 AI 那几行、没带开关：开关原样留着
    code, out = _tty(script, paste)
    assert code == 0 and "YOUJU_DEADLINE_AUTO_VERIFY=1\n" in env.read_text(encoding="utf-8")

    # 粘贴里给开关也行（不算进 AI 配置的行数）；命令里也给了的，以命令里的为准
    code, out = _tty(
        [*script, "YOUJU_DEADLINE_AUTO_VERIFY=0"], "YOUJU_DEADLINE_AUTO_VERIFY=1\n" + paste
    )
    assert code == 0, out
    assert env.read_text(encoding="utf-8").endswith("YOUJU_DEADLINE_AUTO_VERIFY=0\n")

    # --only：不粘贴，只改开关；AI 那几项、别的行原样
    code, out = _tty([*script, "--only", "YOUJU_DEADLINE_AUTO_VERIFY=1"], "")
    assert code == 0, out
    assert "粘贴" not in out and "已写入 YOUJU_AI_MODEL" not in out
    assert env.read_text(encoding="utf-8") == (
        "YOUJU_AI_MODEL=deepseek-chat\nYOUJU_SYNC_URL=https://yj.example.cn\n"
        "YOUJU_DEADLINE_AUTO_VERIFY=1\n"
    )

    # 只收 0 或 1；不认识的参数不打出来；有一项不对整批不写
    before = env.read_bytes()
    secret = "sk-typed-by-mistake-0123456789"
    bad = [
        ([*script, "--only", "YOUJU_DEADLINE_AUTO_VERIFY=2"], "", "只能是 0 或 1"),
        ([*script, "--only", "YOUJU_DEADLINE_AUTO_VERIFY=yes"], "", "只能是 0 或 1"),
        ([*script, "--only", "YOUJU_DEADLINE_AUTO_VERIFY="], "", "只能是 0 或 1"),
        ([*script, "--only"], "", "--only 要和开关一起给"),
        ([*script, "--only", "YOUJU_AI_MODEL=other"], "", "参数不认识"),
        ([*script, "--only", secret], "", "参数不认识"),
        (script, f"YOUJU_DEADLINE_AUTO_VERIFY=true\n{_end_line(0)}\n", "格式不对"),
        (
            [*script, "YOUJU_DEADLINE_AUTO_VERIFY=1"],
            f"YOUJU_AI_MODEL=bad model\n{_end_line(1)}\n",
            "格式不对",
        ),
    ]
    for args, typed, message in bad:
        code, out = _tty(args, typed)
        assert code == 1 and message in out, (args, out)
        assert secret not in out
        assert env.read_bytes() == before, args


@needs_bash
def test_shared_show_ai_prints_route_and_only_a_key_fingerprint(tmp_path):
    defs = _functions(tmp_path)
    key = "sk-0123456789abcdef0123456789abcdef"
    # 服务器 .env 里附加参数带中文、有空格：show ai 按纯 ASCII、紧凑写法打（意思不变）
    raw_extra = '{"thinking": {"type": "disabled"}, "note": "中文"}'
    extra = json.dumps(json.loads(raw_extra), ensure_ascii=True, separators=(",", ":"))
    _server_env(
        tmp_path,
        "YOUJU_AI_BASE_URL=https://api.deepseek.com/v1\nYOUJU_AI_MODEL=deepseek-chat\n"
        f"YOUJU_AI_REQUEST_EXTRA={raw_extra}\nYOUJU_AI_API_KEY={key}\nYOUJU_SYNC_SECRET={'a' * 64}\n",
    )
    show = ["bash", "-c", f'source "{defs}"; SERVER="{tmp_path}/server"; show ai']
    code, out = _tty(show, "")
    assert code == 0, out
    lines = [line for line in out.replace("\r", "").splitlines() if line]
    assert lines[:3] == [
        "YOUJU_AI_BASE_URL=https://api.deepseek.com/v1",
        "YOUJU_AI_MODEL=deepseek-chat",
        f"YOUJU_AI_REQUEST_EXTRA={extra}",
    ]
    # 没填的不打；密钥和别的密钥都不打，只给长度和指纹；最后一行是 #END 加项数
    assert "YOUJU_AI_BACKGROUND_MODEL" not in out
    assert key not in out and "a" * 64 not in out
    assert len(lines) == 4 and lines[-1].startswith("#END 3 ")
    assert f"{len(key)} 位，指纹 {_sha(key.encode())[:4]}" in lines[-1]

    # 原样粘进本机 set-config.sh：本机这几项和服务器一样
    shutil.copy(SELFHOST / "local" / "set-config.sh", tmp_path)
    local_env = tmp_path / ".env"
    local_env.write_text("YOUJU_AI_BACKGROUND_MODEL=stale\n", encoding="utf-8")
    code, pasted_out = _tty(["bash", str(tmp_path / "set-config.sh")], "\n".join(lines) + "\n")
    assert code == 0, pasted_out
    assert local_env.read_text(encoding="utf-8") == "\n".join(lines[:3]) + "\n"
    # 漏了第一行：项数对不上，整批不写
    local_env.write_text("YOUJU_AI_BACKGROUND_MODEL=stale\n", encoding="utf-8")
    code, pasted_out = _tty(["bash", str(tmp_path / "set-config.sh")], "\n".join(lines[1:]) + "\n")
    assert code == 1 and "可能没复制全" in pasted_out
    assert local_env.read_text(encoding="utf-8") == "YOUJU_AI_BACKGROUND_MODEL=stale\n"

    _server_env(tmp_path, "YOUJU_AI_MODEL=deepseek-chat\n")
    code, out = _tty(show, "")
    assert code == 0 and "#END 1 " in out and "服务器上还没录 YOUJU_AI_API_KEY" in out

    # 看着像夹带了密钥的值：一行都不打；max_tokens 这类正常参数照打
    token = "sk-in-the-config-0123456789"
    refused = {
        f'YOUJU_AI_REQUEST_EXTRA={{"headers":{{"Authorization":"Bearer {token}"}}}}': "YOUJU_AI_REQUEST_EXTRA",
        f'YOUJU_AI_REQUEST_EXTRA={{"api_key":"{token}"}}': "YOUJU_AI_REQUEST_EXTRA",
        'YOUJU_AI_REQUEST_EXTRA={"authToken":"short1"}': "YOUJU_AI_REQUEST_EXTRA",
        'YOUJU_AI_REQUEST_EXTRA={"x":{"clientSecret":"short2"}}': "YOUJU_AI_REQUEST_EXTRA",
        'YOUJU_AI_REQUEST_EXTRA={"APISecret":"short3"}': "YOUJU_AI_REQUEST_EXTRA",
        'YOUJU_AI_REQUEST_EXTRA={"apisecret":"short4"}': "YOUJU_AI_REQUEST_EXTRA",
        f'YOUJU_AI_REQUEST_EXTRA={{"x":["{token}"]}}': "YOUJU_AI_REQUEST_EXTRA",
        f"YOUJU_AI_BASE_URL=https://api.deepseek.com/v1?key={token}": "YOUJU_AI_BASE_URL",
        f"YOUJU_AI_BASE_URL=https://user:{token}@api.deepseek.com/v1": "YOUJU_AI_BASE_URL",
        f"YOUJU_AI_BACKGROUND_MODEL={token}": "YOUJU_AI_BACKGROUND_MODEL",
        f"YOUJU_AI_BACKGROUND_MODEL=m{'a1' * 16}": "YOUJU_AI_BACKGROUND_MODEL",
        # 用转义写的 sk-：按 JSON 解开以后查，打之前就拦下
        'YOUJU_AI_REQUEST_EXTRA={"k":"'
        + chr(92)
        + "u0073k-"
        + token[3:]
        + '"}': "YOUJU_AI_REQUEST_EXTRA",
    }
    for line, name in refused.items():
        _server_env(tmp_path, f"YOUJU_AI_MODEL=deepseek-chat\n{line}\n")
        code, out = _tty(show, "")
        assert code == 1 and f"{name} 看着不像" in out, (line, out)
        assert token not in out and "YOUJU_AI_MODEL=deepseek-chat" not in out
    # 转成纯 ASCII 以后本机收不了的也不打：转义的 $ 会还原成 $；中文转义以后超过 2000 字；
    # 用转义藏起来的长串（转写后现出来）
    esc = chr(92) + "u00"
    dollar = '{"k":"' + esc + '24x"}'
    hidden_run = '{"k":"' + (esc + "61") * 40 + '"}'
    for line in (
        f"YOUJU_AI_REQUEST_EXTRA={dollar}",
        'YOUJU_AI_REQUEST_EXTRA={"k":"' + "中" * 400 + '"}',
        f"YOUJU_AI_REQUEST_EXTRA={hidden_run}",
    ):
        _server_env(tmp_path, f"YOUJU_AI_MODEL=deepseek-chat\n{line}\n")
        code, out = _tty(show, "")
        assert code == 1 and "本机收不了" in out, (line[:60], out)
        assert "YOUJU_AI_MODEL=deepseek-chat" not in out
    _server_env(
        tmp_path,
        'YOUJU_AI_REQUEST_EXTRA={"max_tokens":1024,"thinking":{"type":"disabled"}}\n',
    )
    code, out = _tty(show, "")
    assert code == 0 and '"max_tokens":1024' in out and "#END 1 " in out
    # "执行命令"里（没有交互终端）不显示
    done = _bash(tmp_path, "show ai")
    assert done.returncode == 1 and "只能在交互终端里用" in done.stderr
    # show push、show sync 照旧以"看完关掉这个终端窗口"结尾（show ai 不打这行，见上）
    _server_env(
        tmp_path,
        f"YOUJU_SYNC_SECRET={'a' * 64}\nYOUJU_WX_PUSH_TOKEN=t0ken\nYOUJU_WX_PUSH_AES_KEY=aes\n",
    )
    for sub in ("push", "sync"):
        code, out = _tty(
            ["bash", "-c", f'source "{defs}"; SERVER="{tmp_path}/server"; show {sub}'], ""
        )
        lines = [line for line in out.replace("\r", "").splitlines() if line]
        assert code == 0 and lines[-1] == "看完关掉这个终端窗口", (sub, out)


@needs_bash
def test_local_carry_takedowns_checks_hash_before_applying(tmp_path):
    shutil.copy(SELFHOST / "local" / "carry-takedowns.sh", tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(
        f'#!/bin/sh\necho "$*" >> "{tmp_path}/docker.log"\n'
        'case "$*" in *sync-push*) [ -n "$PUSH_OUT" ] && echo "$PUSH_OUT"; exit "${PUSH_CODE:-0}" ;; esac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    listing = '{"url_key":"example.gov.cn/1.html"}\n'
    blob = base64.encodebytes(gzip.compress(listing.encode(), mtime=0)).decode()
    script = str(tmp_path / "carry-takedowns.sh")
    wrong = subprocess.run(
        ["bash", script, "0" * 64], input=blob, capture_output=True, text=True, env=env
    )
    assert wrong.returncode == 1 and "SHA-256 对不上" in wrong.stderr
    assert not (tmp_path / "docker.log").exists() and not (tmp_path / "takedowns.jsonl").exists()
    # 服务器 takedowns 每段第一行的中文说明一起贴进来也行（只留 base64 的行）
    pasted = "下架清单 1 个网址；SHA-256 …；共 1 段，第 1 段：\n" + blob
    done = subprocess.run(
        ["bash", script, _sha(listing.encode())],
        input=pasted.replace("\n", "\r\n"),
        capture_output=True,
        text=True,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "takedowns.jsonl").read_text(encoding="utf-8") == listing
    log = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert log[0].split()[-5:] == ["apply", "--file", "/move/takedowns.jsonl", "--origin", "d127"]
    assert log[1].split()[-3:] == ["check", "--file", "/move/takedowns.jsonl"]
    assert all(line.startswith("compose run --rm --no-deps -v ") for line in log[:2])
    assert log[2] == "compose exec -T worker youju sync-push"  # 马上推一次
    assert "已经推到服务器" in done.stdout
    # 定时任务正在推（锁）：本机已应用，等下一轮
    busy = subprocess.run(
        ["bash", script, _sha(listing.encode())],
        input=blob,
        capture_output=True,
        text=True,
        env={
            **env,
            "PUSH_OUT": "另一个同步正在推（定时任务或手动全推），等它推完再来",
            "PUSH_CODE": "1",
        },
    )
    assert busy.returncode == 0 and "10 分钟内会推到服务器" in busy.stdout
    # 别的原因没推成：说清楚、退出码不是 0
    broken = subprocess.run(
        ["bash", script, _sha(listing.encode())],
        input=blob,
        capture_output=True,
        text=True,
        env={**env, "PUSH_OUT": "连不上服务器", "PUSH_CODE": "1"},
    )
    assert broken.returncode == 1 and "推到服务器没成功" in broken.stderr


@needs_bash
def test_local_carry_emergency_listing_keeps_the_move_listing(tmp_path):
    """紧急下架带回本机：另存一个文件（不盖掉搬家的那份），apply 带 --origin emergency。"""
    shutil.copy(SELFHOST / "local" / "carry-takedowns.sh", tmp_path)
    (tmp_path / "takedowns.jsonl").write_text("move\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/docker.log"\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    listing = '{"url_key":"example.gov.cn/1.html","reason":"侵权投诉"}\n'
    blob = base64.encodebytes(gzip.compress(listing.encode(), mtime=0)).decode()
    script = str(tmp_path / "carry-takedowns.sh")
    sha = _sha(listing.encode())
    bad = subprocess.run(
        ["bash", script, sha, "other"], input=blob, capture_output=True, text=True, env=env
    )
    assert bad.returncode == 1 and "用法" in bad.stderr
    done = subprocess.run(
        ["bash", script, sha, "emergency"], input=blob, capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "emergency-takedowns.jsonl").read_text(encoding="utf-8") == listing
    assert (tmp_path / "takedowns.jsonl").read_text(encoding="utf-8") == "move\n"
    log = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert log[0].split()[-5:] == [
        "apply",
        "--file",
        "/move/emergency-takedowns.jsonl",
        "--origin",
        "emergency",
    ]
    assert log[1].split()[-3:] == ["check", "--file", "/move/emergency-takedowns.jsonl"]
    assert "emergency-settle" in done.stdout


def _interrupt_at_prompt(args: list[str], timeout: float = 10) -> tuple[int, str]:
    """在伪终端里跑，看到提示的"："先等一会儿（边问边启动的写法这时已经起来了），再给整个进程组
    发中断（和网页终端里按 Ctrl-C 一样）。"""
    master, slave = pty.openpty()
    proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    out = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and "：".encode() not in out:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                out += os.read(master, 4096)
            except OSError:
                break
    time.sleep(0.5)
    os.killpg(proc.pid, signal.SIGINT)
    proc.wait(timeout=5)
    os.close(master)
    return proc.returncode, out.decode("utf-8", "replace")


@needs_bash
def test_shared_emergency_commands_pass_arguments_and_carry_decodes(tmp_path):
    listing = (
        json.dumps(
            {
                "url_key": "example.gov.cn/1.html",
                "origin_source_id": 1,
                "taken_down_at": "2026-11-13T02:00:00+00:00",
                "reason": "侵权投诉",
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    log = tmp_path / "calls.log"
    # 假的 compose：每个参数单独加括号记下（引号传错、说明被拆开都看得出来），exec 时连标准输入一起记
    exporting = (
        f"compose() {{ {{ printf compose; printf ' [%s]' \"$@\"; "
        f'if [ "$1" = exec ]; then printf \' stdin=[%s]\' "$(cat)"; fi; echo; }} >> "{log}"; '
        f"case \"$*\" in *' export') printf '%s' '{listing}' ;; esac; }}"
    )
    head = "compose [exec] [-T] [worker] [youju] [emergency-takedown]"
    # 没有终端（如控制台"执行命令"）：不问、不带说明
    done = _bash(
        tmp_path,
        "YOUJU_BY=创始人 emergency_takedown 12 complaint; emergency_lift 12",
        setup=exporting,
    )
    assert done.returncode == 0, done.stderr
    assert _calls(tmp_path) == [
        f"{head} [add] [12] [--reason] [complaint] [--note-stdin] [--by] [创始人] stdin=[]",
        f"{head} [lift] [12] [--note-stdin] [--by] [] stdin=[]",
    ]
    log.unlink()
    # 网页终端里：提示里输入的说明经标准输入给，不在命令参数里
    code, out = _tty(
        ["bash", "-c", _script(tmp_path, "emergency_takedown 12 other", exporting)],
        "来电 要求删除\n",
    )
    assert code == 0, out
    assert "别写投诉人的姓名、电话" in out and "Ctrl-C 取消" in out
    assert _calls(tmp_path) == [
        f"{head} [add] [12] [--reason] [other] [--note-stdin] [--by] [] stdin=[来电 要求删除]"
    ]
    log.unlink()
    # 在提示处 Ctrl-C：说明还没读完时容器里的命令还没起来（边问边启动的话，这时已经起来了，
    # 中断只停得了这边），中断以后也什么都没做
    for body in ("emergency_takedown 12 other", "emergency_lift 12"):
        code, out = _interrupt_at_prompt(["bash", "-c", _script(tmp_path, body, exporting)])
        assert "Ctrl-C 取消" in out and code != 0, out
        assert not log.exists(), body
    for body in (
        "emergency_takedown 12",
        "emergency_takedown 12 complaint 说明",
        "emergency_lift",
        "emergency_lift 12 说明",
    ):
        wrong = _bash(tmp_path, body, setup=exporting)
        assert wrong.returncode == 1 and "用法" in wrong.stderr, body
    carried = _bash(tmp_path, "emergency_carry", setup=exporting)
    assert carried.returncode == 0, carried.stderr
    header, *lines = carried.stdout.splitlines()
    assert _sha(listing.encode()) in header and "共 1 段" in header
    assert gzip.decompress(base64.b64decode("".join(lines))).decode() == listing
    empty = _bash(tmp_path, "emergency_carry", setup="compose() { :; }")
    assert empty.returncode == 0 and "没有紧急下架着的" in empty.stdout
    assert _bash(tmp_path, "emergency_carry x", setup=exporting).returncode == 1


def test_export_paste_block_is_ascii_and_decodes_to_the_script(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("没有 openssl")
    script = SELFHOST / "shared" / "cloudrun-export.py"
    done = subprocess.run(
        ["sh", str(SELFHOST / "shared" / "export-paste.sh"), "--links-only"],
        capture_output=True,
        text=True,
        check=True,
    )
    block = done.stdout
    assert block.isascii() and max(len(line) for line in block.splitlines()) < 400
    lines = block.splitlines()
    assert lines[0] == "cat > /tmp/youju-export.b64 <<'YOUJU_EOF'"
    end = lines.index("YOUJU_EOF")
    data = gzip.decompress(base64.b64decode("".join(lines[1:end])))
    assert data == script.read_bytes()
    last = lines[end + 1]
    assert f"echo '{_sha(data)}  /tmp/youju-export.py' | sha256sum -c -" in last
    # 输出不存进容器里的文件（里面有清单链接）；跑完删掉 /tmp 里的脚本，写入核对留着
    assert last.endswith(
        " && python3 -I /tmp/youju-export.py --links-only;"
        " rm -f /tmp/youju-export.b64 /tmp/youju-export.py"
    )
    assert "tee " not in last and "youju-export.log" not in last
    assert "youju-export-fingerprint" not in last
    cleanup = subprocess.run(
        ["sh", str(SELFHOST / "shared" / "export-paste.sh"), "--cleanup"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "python3 -I /tmp/youju-export.py --cleanup;" in cleanup.stdout.splitlines()[-1]
    wrong = subprocess.run(
        ["sh", str(SELFHOST / "shared" / "export-paste.sh"), "--delete"],
        capture_output=True,
        text=True,
    )
    assert wrong.returncode == 1
    bad = subprocess.run(
        ["sh", str(SELFHOST / "shared" / "export-paste.sh"), "--delete"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1


@needs_bash
def test_shared_plain_config_and_config_from_export(tmp_path, monkeypatch):
    env = _server_env(
        tmp_path,
        "YOUJU_WX_API_BASE=http://127.0.0.1:9\nYOUJU_SYNC_SECRET="
        + "a" * 64
        + "\nYOUJU_AI_MODEL=old\n",
    )
    done = _bash(
        tmp_path, "env_set YOUJU_CRONTAB", stdin="/app/backend/deploy/selfhost/server/crontab\n"
    )
    assert done.returncode == 0, done.stderr
    done = _bash(tmp_path, "env_unset YOUJU_WX_API_BASE")
    assert done.returncode == 0, done.stderr
    # 密钥、装的时候生成的不能这样改；值里有 $、换行、以引号开头的写不进不加引号的 .env
    for name, value in (
        ("YOUJU_SYNC_SECRET", "b" * 64),
        ("YOUJU_WX_APPSECRET", "0" * 32),
        ("MYSQL_PASSWORD", "x"),
        ("YOUJU_AI_MODEL", "a$b"),
        ("YOUJU_AI_MODEL", '"quoted"'),
        ("YOUJU_AI_MODEL", "a #b"),
    ):
        done = _bash(tmp_path, f"env_set {name}", stdin=value + "\n")
        assert done.returncode == 1, (name, value)
    export = tmp_path / "export"
    export.mkdir()
    (export / "summary.json").write_text(
        json.dumps(
            {
                "config": {
                    "YOUJU_AI_MODEL": "qwen-plus",
                    "YOUJU_AI_MODEL_LABEL": "通义千问",
                    "YOUJU_TEMPLATE_DEADLINE": {"id": "abc", "page": "pages/detail/detail"},
                    "YOUJU_PAY_ENABLED": True,
                    "YOUJU_XPAY_PRICE_PASS": 2900,
                    "YOUJU_XPAY_IOS_VALUES": ["ios"],
                    "YOUJU_PAY_INVOICE_TYPE": "多行\n说明",
                    "YOUJU_WX_APPSECRET": "0" * 32,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 假的 compose exec：在本机的 python 里跑那段转换
    fake = 'compose() { if [ "$1" = exec ]; then shift 4; python3 "$@"; fi; }'
    done = _bash(tmp_path, "config_from_export", setup=fake)
    assert done.returncode == 0, done.stderr
    assert "跳过 YOUJU_PAY_INVOICE_TYPE" in done.stderr and "跳过 YOUJU_WX_APPSECRET" in done.stderr
    assert env.read_text(encoding="utf-8").splitlines() == [
        "YOUJU_SYNC_SECRET=" + "a" * 64,
        "YOUJU_AI_MODEL=qwen-plus",
        "YOUJU_CRONTAB=/app/backend/deploy/selfhost/server/crontab",
        "YOUJU_AI_MODEL_LABEL=通义千问",
        "YOUJU_PAY_ENABLED=true",
        'YOUJU_TEMPLATE_DEADLINE={"id":"abc","page":"pages/detail/detail"}',
        'YOUJU_XPAY_IOS_VALUES=["ios"]',
        "YOUJU_XPAY_PRICE_PASS=2900",
    ]
    # 程序读得回同样的值（docker compose 按 env_file 原样传进容器的环境变量）
    for line in env.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.template_deadline == {"id": "abc", "page": "pages/detail/detail"}
    assert settings.xpay_ios_values == ["ios"] and settings.pay_enabled is True
    assert settings.xpay_price_pass == 2900 and settings.ai_model_label == "通义千问"


# ---- 存档：到期日写进文件名，到期自动删、留记录 ----


class FakeS3:
    def __init__(self):
        self.files: dict[str, tuple[bytes, dict | None]] = {}

    def put_file(self, path, local, *, headers=None):
        self.files[path] = (Path(local).read_bytes(), headers)

    def list(self, prefix):
        return [
            storage.StoredObject(key, datetime(2026, 11, 12, tzinfo=UTC), len(data))
            for key, (data, _) in sorted(self.files.items())
            if key.startswith(prefix)
        ]

    def delete(self, path):
        self.files.pop(path, None)


def _seal(source, target, recipient):
    Path(target).write_bytes(b"sealed:" + recipient.encode() + b":" + Path(source).read_bytes())


def _archive_settings(tmp_path, *, offsite_ready=True):
    settings = Settings(
        _env_file=None,
        hosting="self",
        storage_backend="disk",
        storage_dir=tmp_path / "store",
        cloud_env="test-env",
        offsite_age_recipient="age1" + "q" * 58,
    )
    if not offsite_ready:
        return settings
    return settings.model_copy(
        update={
            "offsite_s3_endpoint": "https://cos.ap-shanghai.myqcloud.com",
            "offsite_s3_region": "ap-shanghai",
            "offsite_s3_bucket": "backup-1250000000",
            "offsite_s3_access_key": SecretStr("ak"),
            "offsite_s3_secret_key": SecretStr("sk"),
        }
    )


def test_archive_names_files_by_expiry_and_needs_offsite(tmp_path):
    source = tmp_path / "youju-20261112.jsonl.gz"
    source.write_bytes(b"backup")
    today = date(2026, 11, 12)
    fake = FakeS3()
    # 异地没配：不做（调用方存完会删明文，之后补传不了）
    with pytest.raises(BackupError, match="异地备份还没配好"):
        offsite.archive(
            _archive_settings(tmp_path, offsite_ready=False),
            source,
            "cloudrun-youju-20261112",
            date(2026, 12, 12),
            today=today,
            backend=fake,
            encryptor=_seal,
        )
    assert not fake.files and not (tmp_path / "store" / "archive").exists()
    settings = _archive_settings(tmp_path)
    report = offsite.archive(
        settings,
        source,
        "cloudrun-youju-20261112",
        date(2026, 12, 12),
        today=today,
        backend=fake,
        encryptor=_seal,
    )
    target = "archive/cloudrun-youju-20261112.expires-20261212.jsonl.gz.age"
    assert (report.target, report.expires) == (target, date(2026, 12, 12))
    assert (tmp_path / "store" / target).read_bytes().startswith(b"sealed:age1")
    assert fake.files[target][1] == {"x-cos-storage-class": "STANDARD_IA"}
    for stem, expires in (("../x", date(2026, 12, 12)), ("a.b", date(2026, 12, 12)), ("ok", today)):
        with pytest.raises(BackupError):
            offsite.archive(
                settings, source, stem, expires, today=today, backend=fake, encryptor=_seal
            )


def test_archive_export_splits_by_start_date(tmp_path):
    work = tmp_path / "export"
    (work / "backups").mkdir(parents=True)
    (work / "manifest.json").write_text('{"created_at": "2026-11-12T08:00:00+00:00"}')
    first = _sha((work / "manifest.json").read_bytes())
    (work / "backups" / "youju-20261112.jsonl.gz").write_bytes(b"backup")
    (work / "pay-records.jsonl.gz").write_bytes(PAY)
    (work / "advisor-out" / "0123456789abcdef").mkdir(parents=True)
    for name in ("7.xlsx", "8.xlsx"):
        (work / "advisor-out" / "0123456789abcdef" / name).write_bytes(name.encode())
    (work / "advisor-out.json").write_text(
        json.dumps(
            [
                {
                    "path": "advisor/out/0123456789abcdef/7.xlsx",
                    "modified": "2026-10-20T03:00:00+00:00",
                    "size": 6,
                },
                {
                    "path": "advisor/out/0123456789abcdef/8.xlsx",
                    "modified": "2026-11-10T03:00:00+00:00",
                    "size": 6,
                },
            ]
        ),
        encoding="utf-8",
    )
    fake = FakeS3()
    reports, notes = cloudrun_fetch.archive_export(
        _archive_settings(tmp_path), work, today=date(2026, 11, 13), backend=fake, encryptor=_seal
    )
    # 到期日 = 起算日加规定天数减 1（5:50 删到期日那天的，最早的一条也不超过规定天数）。
    # 整库：自备份那天起最长 30 天。交易记录按起算日分组、各自 3650 天：赠送（北京时间 10-20）一组，
    # 订单和跟着它的开票申请（10-30）一组。批量工具结果按生成日期分组：10-20 那组 11-18 到期，
    # 11-10 那组 12-09 到期
    gift = date(2026, 10, 20) + timedelta(days=3649)
    order = date(2026, 10, 30) + timedelta(days=3649)
    # 存档名里带这一批的导出时间（交易记录说明行的 created_at，UTC）：第二批不会盖掉第一批
    assert [r.target for r in reports] == [
        f"archive/cloudrun-youju-20261112-202611120800-{first}.expires-20261211.jsonl.gz.age",
        f"archive/cloudrun-pay-records-20261020-202611120800-{first}.expires-{gift:%Y%m%d}.jsonl.gz.age",
        f"archive/cloudrun-pay-records-20261030-202611120800-{first}.expires-{order:%Y%m%d}.jsonl.gz.age",
        f"archive/cloudrun-advisor-out-20261020-202611120800-{first}.expires-20261118.tar.gz.age",
        f"archive/cloudrun-advisor-out-20261110-202611120800-{first}.expires-20261209.tar.gz.age",
    ]
    assert set(fake.files) == {r.target for r in reports}
    order_part = fake.files[reports[2].target][0].split(b":", 2)[2]
    lines = gzip.decompress(order_part).decode().splitlines()
    assert json.loads(lines[0])["counts"] == {"invoice_requests": 1, "pay_orders": 1}
    assert {json.loads(line)["table"] for line in lines[1:]} == {"pay_orders", "invoice_requests"}
    # 云托管的数据库最晚哪天删（含这天）：和整库存档同一天到期
    deadline = json.loads((tmp_path / "store" / "archive" / "cloudrun-deadline.json").read_text())
    assert deadline == {"backup_date": "2026-11-12", "delete_by": "2026-12-11"}
    assert any("云托管里的数据库最晚 2026-12-11 删掉" in note for note in notes)
    # 晚几天再存：已经过了 30 天的那组批量工具结果不存，没过的照存
    fake = FakeS3()
    reports, notes = cloudrun_fetch.archive_export(
        _archive_settings(tmp_path), work, today=date(2026, 11, 19), backend=fake, encryptor=_seal
    )
    assert [r.target for r in reports][-1] == (
        f"archive/cloudrun-advisor-out-20261110-202611120800-{first}.expires-20261209.tar.gz.age"
    )
    assert "2026-10-20 生成的批量工具结果已经过了 30 天，不存" in notes
    # 退回云托管、再切回来导的第二批，导出时间和第一批在同一分钟、清单不同：同一天的结果另存
    # 一份，不盖掉第一批（名字里是完整的清单 SHA-256）
    second = (
        gzip.decompress(PAY)
        .decode()
        .replace(
            '"created_at": "2026-11-12T08:00:00+00:00"',
            '"created_at": "2026-11-12T08:00:40+00:00"',
            1,
        )
    )
    (work / "pay-records.jsonl.gz").write_bytes(gzip.compress(second.encode()))
    (work / "manifest.json").write_text('{"created_at": "2026-11-12T08:00:40+00:00"}')
    again = _sha((work / "manifest.json").read_bytes())
    assert again != first
    cloudrun_fetch.archive_export(
        _archive_settings(tmp_path),
        work,
        today=date(2026, 11, 13),
        backend=FakeS3(),
        encryptor=_seal,
    )
    kept = sorted(
        path.name
        for path in (tmp_path / "store" / "archive").glob("cloudrun-advisor-out-20261110-*")
    )
    assert kept == [
        f"cloudrun-advisor-out-20261110-202611120800-{first}.expires-20261209.tar.gz.age",
        f"cloudrun-advisor-out-20261110-202611120800-{again}.expires-20261209.tar.gz.age",
    ]
    # 同一批重跑：名字不变，覆盖的是同一份
    (work / "manifest.json").write_text('{"created_at": "2026-11-12T08:00:00+00:00"}')
    (work / "pay-records.jsonl.gz").write_bytes(PAY)
    cloudrun_fetch.archive_export(
        _archive_settings(tmp_path),
        work,
        today=date(2026, 11, 13),
        backend=FakeS3(),
        encryptor=_seal,
    )
    assert (
        sorted(
            path.name
            for path in (tmp_path / "store" / "archive").glob("cloudrun-advisor-out-20261110-*")
        )
        == kept
    )
    (work / "pay-records.jsonl.gz").write_bytes(PAY)
    # 有一行读不出起算日（拿导出日顶替会存得比指引久）：停下，不存
    orphan = gzip.decompress(PAY).decode() + json.dumps(
        {"table": "pay_events", "row": {"id": 9, "order_id": 404, "kind": "notify"}}
    )
    (work / "pay-records.jsonl.gz").write_bytes(gzip.compress(orphan.encode() + b"\n"))
    untouched = FakeS3()
    with pytest.raises(cloudrun_fetch.FetchError, match="pay_events 9"):
        cloudrun_fetch.archive_export(
            _archive_settings(tmp_path),
            work,
            today=date(2026, 11, 13),
            backend=untouched,
            encryptor=_seal,
        )
    assert untouched.files == {}  # 先核对再存：一份都没存
    # 说明行写的保存天数不是 D-127 定的 3650：停下
    bad = gzip.compress(
        json.dumps({"format": "youju-pay-records", "version": 1, "retain_days": 1095}).encode()
        + b"\n"
    )
    (work / "pay-records.jsonl.gz").write_bytes(bad)
    with pytest.raises(cloudrun_fetch.FetchError, match="说明行"):
        cloudrun_fetch.archive_export(
            _archive_settings(tmp_path),
            work,
            today=date(2026, 11, 13),
            backend=fake,
            encryptor=_seal,
        )


def test_split_pay_records_links_rows_and_stops_on_orphans(tmp_path):
    header = {"format": "youju-pay-records", "version": 1, "retain_days": 3650}

    def split(rows):
        path = tmp_path / "pay.jsonl.gz"
        lines = [json.dumps(header)] + [json.dumps({"table": t, "row": r}) for t, r in rows]
        path.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
        return {
            day.isoformat(): sorted(f"{t} {r['id']}" for t, r in group)
            for day, group in cloudrun_fetch.split_pay_records(path, header).items()
        }

    order = ("pay_orders", {"id": 1, "created_at": "2026-10-29T16:30:00+00:00"})
    gift = ("pass_rewards", {"id": 2, "granted_at": "2026-10-20T02:00:00+00:00"})
    groups = split(
        [
            order,
            gift,
            # 开票、流水、使用期跟着订单走（它们自己的时间更晚，不用）
            (
                "invoice_requests",
                {"id": 3, "order_id": 1, "created_at": "2026-11-05T00:00:00+00:00"},
            ),
            ("pay_events", {"id": 4, "order_id": 1, "created_at": "2026-11-06T00:00:00+00:00"}),
            ("workbench_passes", {"id": 5, "order_id": 1, "reward_id": None}),
            # 使用期跟着赠送走
            ("workbench_passes", {"id": 6, "order_id": None, "reward_id": 2}),
            # 没有订单的支付流水按它自己的时间；对账按那一天
            ("pay_events", {"id": 7, "order_id": None, "created_at": "2026-10-25T03:00:00+00:00"}),
            ("pay_reconciliations", {"id": 8, "day": "2026-10-28"}),
        ]
    )
    assert groups == {
        "2026-10-20": ["pass_rewards 2", "workbench_passes 6"],
        "2026-10-25": ["pay_events 7"],
        "2026-10-28": ["pay_reconciliations 8"],
        "2026-10-30": ["invoice_requests 3", "pay_events 4", "pay_orders 1", "workbench_passes 5"],
    }
    # 该关联的关联不上：不拿它自己的时间顶替，停下
    for orphan in (
        ("invoice_requests", {"id": 9, "order_id": 404, "created_at": "2026-11-05T00:00:00+00:00"}),
        ("pay_events", {"id": 9, "order_id": 404, "created_at": "2026-11-05T00:00:00+00:00"}),
        ("workbench_passes", {"id": 9, "order_id": None, "reward_id": 404, "starts_at": "x"}),
    ):
        with pytest.raises(cloudrun_fetch.FetchError, match=f"{orphan[0]} 9"):
            split([order, gift, orphan])


def test_cloudrun_deletion_reminder_and_record(tmp_path):
    settings = _archive_settings(tmp_path)
    store = storage.DiskBackend(tmp_path / "store")
    assert offsite.cloudrun_reminder(store, date(2026, 12, 10)) == ""  # 还没有期限
    deadline = tmp_path / "deadline.json"
    deadline.write_text(json.dumps({"backup_date": "2026-11-12", "delete_by": "2026-12-12"}))
    store.put_file(offsite.CLOUDRUN_DEADLINE, deadline)
    assert offsite.cloudrun_reminder(store, date(2026, 12, 4)) == ""  # 离期限还有 8 天
    assert "最晚 2026-12-12 删掉" in offsite.cloudrun_reminder(store, date(2026, 12, 5))
    assert "已经过了期限" in offsite.cloudrun_reminder(store, date(2026, 12, 13))
    assert "最晚 2026-12-12 删掉" in offsite.cloudrun_reminder_for(
        settings, today=date(2026, 12, 6)
    )
    assert "已经过了期限" not in offsite.cloudrun_reminder(store, date(2026, 12, 12))
    with pytest.raises(BackupError):
        offsite.record_cloudrun_gone(settings, "later")
    offsite.record_cloudrun_gone(settings, "deregistered", now=datetime(2026, 12, 1, tzinfo=UTC))
    record = json.loads((tmp_path / "store" / "archive" / "cloudrun-gone.json").read_text())
    assert record == {"at": "2026-12-01T00:00:00+00:00", "how": "deregistered"}
    assert offsite.cloudrun_reminder(store, date(2026, 12, 13)) == ""  # 记下删了就不再提醒


def test_cli_archive_prune_reminds_first_and_fails_when_not_sent(monkeypatch, capsys):
    from youju import alerts

    note = "云托管里的数据库最晚 2026-12-12 删掉"
    monkeypatch.setattr(offsite, "cloudrun_reminder_for", lambda settings: note)
    sent: list[str] = []

    def send(text):
        sent.append(text)
        return True

    def broken(settings):
        raise BackupError("列异地存储失败")

    monkeypatch.setattr(alerts, "send_alert", send)
    monkeypatch.setattr(offsite, "prune_archive", broken)
    # 删到期存档出错：提醒照样先发了
    assert cli.main(["archive-prune"]) == 1
    assert sent == [note]
    monkeypatch.setattr(offsite, "prune_archive", lambda settings: offsite.PruneReport())
    assert cli.main(["archive-prune"]) == 0
    # 提醒发不出去（告警没配或发送失败）：按失败退出，任务记录里看得到
    for result in (False, None):
        monkeypatch.setattr(alerts, "send_alert", lambda text, result=result: result)
        assert cli.main(["archive-prune"]) == 1
    assert "这条提醒没发出去" in capsys.readouterr().out


def test_prune_archive_deletes_expired_on_both_sides_and_records_it(tmp_path):
    settings = _archive_settings(tmp_path)
    fake = FakeS3()
    store = storage.DiskBackend(tmp_path / "store")
    names = {
        "archive/cloudrun-youju-20261112.expires-20261212.jsonl.gz.age",
        "archive/cloudrun-pay-records-20261112.expires-20361027.jsonl.gz.age",
    }
    blob = tmp_path / "blob"
    blob.write_bytes(b"sealed")
    for name in names:
        store.put_file(name, blob)
        fake.put_file(name, blob)
    store.put_file("archive/readme.txt", blob)  # 不是存档的文件名：不动
    # 批量工具结果打的包（.tar.gz.age）也认
    tarball = "archive/cloudrun-advisor-out-20261112.expires-20361027.tar.gz.age"
    store.put_file(tarball, blob)
    # 导进来的云托管访问日志（不加密）也按文件名里的到期日删
    log = "archive/cloudrun-access-20260601.expires-20261208.jsonl.gz"
    store.put_file(log, blob)
    early = offsite.prune_archive(settings, today=date(2026, 12, 11), backend=fake)
    assert (early.deleted, early.kept) == ([f"local:{log}"], 5)
    report = offsite.prune_archive(settings, today=date(2026, 12, 12), backend=fake)
    expired = "archive/cloudrun-youju-20261112.expires-20261212.jsonl.gz.age"
    assert report.deleted == [f"local:{expired}", f"offsite:{expired}"]
    assert not store.exists(expired) and expired not in fake.files and not store.exists(log)
    assert store.exists("archive/readme.txt") and len(fake.files) == 1 and store.exists(tarball)
    record = tmp_path / "store" / "archive" / "deletions.jsonl"
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [(r["where"], r["path"], r["expires"]) for r in rows] == [
        ("local", log, "2026-12-08"),
        ("local", expired, "2026-12-12"),
        ("offsite", expired, "2026-12-12"),
    ]
    # 第二天再跑：没有可删的，记录不变
    again = offsite.prune_archive(settings, today=date(2026, 12, 13), backend=fake)
    assert again.deleted == [] and len(record.read_text(encoding="utf-8").splitlines()) == 3


@needs_bash
def test_import_export_purges_binlog_even_when_interrupted(tmp_path):
    """试恢复中途被打断（关掉网页终端、Ctrl+C）：退出时照样清 binlog，再把接口、worker 起来。"""
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    _downloaded(tmp_path)
    log = tmp_path / "calls.log"
    interrupted = (
        f'compose() {{ echo "compose $*" >> "{log}"; '
        'if [ "$1" = exec ]; then cat >/dev/null; fi; '
        'if [ "$1" = run ]; then kill -TERM $$; sleep 1; fi; }\n'
        # 终端断了以后还在跑的试恢复容器（compose 起的一次性容器）
        f'docker() {{ echo "docker $*" >> "{log}"; if [ "$1" = ps ]; then echo abc123; fi; '
        f'if [ "$1" = info ]; then echo "{tmp_path}"; fi; }}'
    )
    done = _bash(tmp_path, "import_export a79a0042", setup=interrupted)
    assert done.returncode != 0
    calls = _calls(tmp_path)
    run = next(i for i, c in enumerate(calls) if c.startswith("compose run"))
    # 先停掉还在跑的一次性容器，再清 binlog，最后把接口、worker 起来
    assert calls[run + 1].startswith(
        "docker ps -q --filter label=com.docker.compose.project=youju-server"
    )
    assert "com.docker.compose.oneoff=True" in calls[run + 1]
    assert calls[run + 2] == "docker stop -t 60 abc123"
    assert calls[run + 3] == "compose exec -T mysql sh -s"
    assert calls[-1].startswith("compose up -d --wait --wait-timeout 300 api")
    assert not any("archive-export" in c for c in calls)


def _binlog_off_setup(
    tmp_path: Path, state: str = "0", *, state_fails: bool = False, cleanup_fails: bool = False
) -> str:
    """假的 compose：删留下的 binlog 文件那段（含 binlog.index）回"删掉了"或失败；查开没开那段回
    state 或失败（exec 返回 1，像连不上 MySQL）；清 binlog 那段照常读掉标准输入。"""
    log = tmp_path / "calls.log"
    cleanup = "return 1" if cleanup_fails else "echo cleaned"
    check = "return 1" if state_fails else f"echo '{state}'"
    return (
        f'compose() {{ echo "compose $*" >> "{log}"; if [ "$1" = exec ]; then s=$(cat); '
        f'case "$s" in *binlog.index*) {cleanup} ;; *"@@GLOBAL.log_bin"*) {check} ;; esac; fi; }}'
    )


@needs_bash
def test_shared_binlog_off_purges_recreates_checks_and_cleans_up(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    (tmp_path / "server" / "mysql.cnf").write_text("[mysqld]\ndisable_log_bin\n", encoding="utf-8")
    done = _bash(tmp_path, "binlog_off", setup=_binlog_off_setup(tmp_path))
    assert done.returncode == 0, done.stderr
    # worker、接口起来了才打 BINLOG OFF（这时关终端不会打断起服务），而且是最后一行
    assert done.stdout.strip().splitlines()[-1] == "BINLOG OFF"
    calls = _calls(tmp_path)
    # 先停接口和 worker，清掉现有的 binlog，用新配置重建 MySQL，查是 0，删留下的 binlog 文件，
    # 最后起 worker、接口；退出时不再起一遍（trap 已撤掉）
    assert calls == [
        "compose stop api worker",
        "compose exec -T mysql sh -s",
        "compose up -d --force-recreate --wait --wait-timeout 300 mysql",
        "compose exec -T mysql sh -s",
        "compose exec -T mysql sh -s",
        "compose up -d --wait --wait-timeout 900 worker",
        "compose up -d --wait --wait-timeout 300 api",
    ]
    assert "r00tpw" not in "\n".join(calls)


@needs_bash
def test_shared_binlog_off_needs_the_new_config_and_stops_on_any_doubt(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    # 服务器上的 mysql.cnf 还是旧的（没 install 新版本）：什么都不停
    (tmp_path / "server" / "mysql.cnf").write_text(
        "[mysqld]\nbinlog_expire_logs_seconds = 259200\n", encoding="utf-8"
    )
    done = _bash(tmp_path, "binlog_off", setup=_binlog_off_setup(tmp_path))
    assert done.returncode == 1 and "还没有 disable_log_bin" in done.stderr
    assert not [c for c in _calls(tmp_path) if c.startswith("compose")]
    (tmp_path / "server" / "mysql.cnf").write_text("[mysqld]\ndisable_log_bin\n", encoding="utf-8")
    cases = [
        # 重建以后还开着、读不出、查的时候连不上：不删文件，报错
        (_binlog_off_setup(tmp_path, "1"), "@@GLOBAL.log_bin=1", 2),
        (_binlog_off_setup(tmp_path, ""), "@@GLOBAL.log_bin=读不出", 2),
        (_binlog_off_setup(tmp_path, state_fails=True), "查不出 MySQL 的 binlog 开没开", 2),
        # 关了，但留下的 binlog 文件没删掉：报错
        (_binlog_off_setup(tmp_path, cleanup_fails=True), "留下的 binlog 文件没删掉", 3),
    ]
    for setup, shown, execs in cases:
        (tmp_path / "calls.log").unlink(missing_ok=True)
        done = _bash(tmp_path, "binlog_off", setup=setup)
        assert done.returncode != 0 and shown in done.stderr, done.stderr
        assert "BINLOG OFF" not in done.stdout
        calls = [c for c in _calls(tmp_path) if c.startswith("compose")]
        assert calls.count("compose exec -T mysql sh -s") == execs
        # 照样把 worker、接口起来（on_exit）
        assert calls[-1].startswith("compose up -d --wait --wait-timeout 300 api")


@needs_bash
@pytest.mark.parametrize("state", ["0", "1", "fail"])
def test_binlog_files_cleanup_script_deletes_only_leftover_binlog_files(tmp_path, state):
    """容器里跑的那段：binlog 确实关着才删数据目录里的 binlog.<6 位编号> 和 binlog.index，别的不碰；
    开着、读不出时按失败退出，什么都不删；口令只在环境变量里。"""
    defs = _functions(tmp_path)
    script = subprocess.run(
        ["bash", "-c", f'source "{defs}"; binlog_files_cleanup_sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = tmp_path / "data"
    data.mkdir()
    old = ["binlog.000001", "binlog.000002", "binlog.index"]
    keep = ["binlog.000001.bak", "ibdata1", "mysql.ibd"]
    for name in old + keep:
        (data / name).write_text("x", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mysql"
    answer = "exit 1" if state == "fail" else f"echo {state}"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$* | pwd=$MYSQL_PWD" >> "{tmp_path}/mysql.log"\n'
        'case "$*" in\n'
        f'  *"@@GLOBAL.log_bin"*) {answer} ;;\n'
        f'  *"@@GLOBAL.datadir"*) echo "{data}/" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "MYSQL_ROOT_PASSWORD": "r00t"}
    done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True, env=env)
    lines = (tmp_path / "mysql.log").read_text(encoding="utf-8").splitlines()
    assert all(line.endswith("| pwd=r00t") and "r00t" not in line.split(" | ")[0] for line in lines)
    left = sorted(p.name for p in data.iterdir())
    if state == "0":
        assert done.returncode == 0 and "删掉了以前留下的 binlog 文件 3 个" in done.stdout
        assert left == sorted(keep)
    else:
        assert done.returncode != 0 and left == sorted(old + keep)


@pytest.mark.skipif(not shutil.which("mysqld"), reason="没有 mysqld")
@pytest.mark.parametrize("variant", ["shared", "server"])
def test_server_mysql_configs_pass_mysqld_validation(tmp_path, variant):
    """两份 mysql.cnf 交给真的 mysqld：写法要过校验（拼错的选项会报错），binlog 也真的关了
    （关了以后 log-replica-updates 跟着是 FALSE，开着是 TRUE）。先拷进临时目录再给它：Ubuntu 的
    mysqld 有 AppArmor，读不了检出目录里的文件。"""
    cnf = tmp_path / "mysql.cnf"
    shutil.copyfile(SELFHOST / variant / "mysql.cnf", cnf)
    cnf.chmod(0o644)
    done = subprocess.run(
        ["mysqld", f"--defaults-file={cnf}", "--validate-config"], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    shown = subprocess.run(
        ["mysqld", f"--defaults-file={cnf}", "--verbose", "--help"], capture_output=True, text=True
    )
    assert re.search(r"^log-(replica|slave)-updates\s+FALSE\s*$", shown.stdout, re.M), shown.stderr


@needs_bash
def test_binlog_state_script_reads_the_global_switch_with_the_password_from_env(tmp_path):
    defs = _functions(tmp_path)
    script = subprocess.run(
        ["bash", "-c", f'source "{defs}"; binlog_state_sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mysql"
    fake.write_text(
        f'#!/bin/sh\necho "$* | pwd=$MYSQL_PWD" >> "{tmp_path}/mysql.log"\necho 0\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "MYSQL_ROOT_PASSWORD": "r00t"}
    done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True, env=env)
    assert done.returncode == 0 and done.stdout.strip() == "0"
    (line,) = (tmp_path / "mysql.log").read_text(encoding="utf-8").splitlines()
    assert line == "-uroot -N -B -e SELECT @@GLOBAL.log_bin | pwd=r00t"


@needs_bash
def test_on_exit_keeps_going_when_a_step_fails(tmp_path):
    """退出时清 binlog 失败（或往断开的终端写字失败）也要接着把接口、worker 起来。"""
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    log = tmp_path / "calls.log"
    setup = (
        f'compose() {{ echo "compose $*" >> "{log}"; '
        'if [ "$1" = exec ]; then cat >/dev/null; return 1; fi; }'
    )
    done = _bash(tmp_path, "pause_services; PURGE_BINLOG=1; false", setup=setup)
    assert done.returncode != 0 and "binlog 没清掉" in done.stderr
    calls = _calls(tmp_path)
    assert calls[-1].startswith("compose up -d --wait --wait-timeout 300 api")


@needs_bash
@pytest.mark.parametrize("left", [1, 2])
def test_binlog_purge_script_purges_up_to_the_current_file(tmp_path, left):
    """容器里跑的那段：轮换以后按文件名删到当前这个（不用 BEFORE NOW()：同一秒里删不掉刚轮换出来的），
    只剩一个才算清干净；密码只在环境变量里。"""
    defs = _functions(tmp_path)
    script = subprocess.run(
        ["bash", "-c", f'source "{defs}"; binlog_purge_sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mysql"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$* | pwd=$MYSQL_PWD" >> "{tmp_path}/mysql.log"\n'
        'case "$*" in\n'
        '  *"SELECT @@log_bin"*) echo 1 ;;\n'
        "  *\"SHOW MASTER STATUS\"*) printf 'binlog.000045\\t157\\t\\t\\t\\n' ;;\n"
        f'  *"SHOW BINARY LOGS"*) for i in $(seq {left}); do echo "binlog.00004$i 157 No"; done ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "MYSQL_ROOT_PASSWORD": "r00t"}
    done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True, env=env)
    lines = (tmp_path / "mysql.log").read_text(encoding="utf-8").splitlines()
    assert any("PURGE BINARY LOGS TO 'binlog.000045'" in line for line in lines)
    assert not any("BEFORE NOW()" in line for line in lines)
    assert all(line.endswith("| pwd=r00t") and "r00t" not in line.split(" | ")[0] for line in lines)
    if left == 1:
        assert done.returncode == 0 and "binlog 已清" in done.stdout
    else:
        assert done.returncode != 0 and "没清干净" in done.stderr


@needs_bash
@pytest.mark.parametrize("answer", ["0", "fail"])
def test_binlog_purge_script_skips_only_when_binlog_is_really_off(tmp_path, answer):
    """binlog 关着才打"不用清"、退出 0；连不上 MySQL（查不出开没开）时按失败退出，不当作关着。"""
    defs = _functions(tmp_path)
    script = subprocess.run(
        ["bash", "-c", f'source "{defs}"; binlog_purge_sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "mysql"
    fake.write_text(
        "#!/bin/sh\n" + ("echo 0\n" if answer == "0" else "echo 'cannot connect' >&2\nexit 1\n"),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "MYSQL_ROOT_PASSWORD": "r00t"}
    done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True, env=env)
    if answer == "0":
        assert done.returncode == 0 and "binlog 没开，不用清" in done.stdout
    else:
        assert done.returncode != 0 and "不用清" not in done.stdout
