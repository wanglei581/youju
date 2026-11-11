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
import subprocess
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
            "counts": {"pay_orders": 1},
        }
    )
    + "\n"
    + json.dumps({"table": "pay_orders", "row": {"id": 1, "out_trade_no": "T1"}})
    + "\n"
)


def _export(
    remote: Remote,
    files: dict[str, bytes],
    *,
    sizes=None,
    takedowns=b'{"url_key":"a.gov.cn/1"}\n',
    pay=PAY,
):
    """按导出脚本的格式造一份清单，返回（链接，SHA-256）。"""
    sizes = sizes or {}
    remote.files.update(files)
    remote.files["exports/takedowns-1.jsonl"] = takedowns
    remote.files["exports/pay-records-1.jsonl.gz"] = pay
    manifest = {
        "format": "youju-cloudrun-export",
        "version": 1,
        "created_at": "2026-11-12T08:00:00+00:00",
        "revision": "0046",
        "files": [
            {"path": p, "size": sizes.get(p, len(data)), "url": remote.url(p)}
            for p, data in files.items()
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
    }
    bad_hash = f"attachments/{'0' * 64}.pdf"
    broken_gz = "logs/access-20261111.jsonl.gz"
    remote = Remote({})
    url, sha = _export(
        remote,
        {**good, bad_hash: b"not matching", broken_gz: _gz("x")[:-4]},
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
    # 访问日志不放回 logs/（服务器正式归档会写同名文件），放 archive/、到期日写进文件名（190 天）
    log_day = date(2026, 11, 10)
    expires = log_day + timedelta(days=191)
    assert store.exists(f"archive/cloudrun-access-20261110.expires-{expires:%Y%m%d}.jsonl.gz")
    assert not store.exists("logs/access-20261110.jsonl.gz")
    # 不带批量工具结果（旧库不恢复）；备份、下架清单不进存储，进 --dir
    assert not store.exists("advisor/out/0123456789abcdef/7.xlsx")
    assert "advisor/out/0123456789abcdef/7.xlsx" not in remote.gets
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
    assert again.skipped == {"backups/": 1, "logs/": 1, "attachments/": 1}
    assert set(remote.gets) == {bad_hash, broken_gz}
    assert sorted(path for path, _ in again.failed) == sorted([bad_hash, broken_gz])
    assert not (work / "fetch-complete").exists()
    # 重出一份清单（坏的两个已经不在里面）：全部下好才写完成标记
    remote2 = Remote({})
    url2, sha2 = _export(remote2, good)
    done = cloudrun_fetch.fetch(
        remote2.client, url2, sha2, work, store=store, say=lambda _: None, today=today
    )
    assert not done.failed and (work / "fetch-complete").read_text().startswith(sha2)
    assert json.loads((work / "summary.json").read_text(encoding="utf-8"))["complete"] is True


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
    url, sha = _export(
        remote,
        {"backups/youju-20261113.jsonl.gz": backup, f"attachments/{_sha(pdf)}.pdf": pdf},
        sizes={"backups/youju-20261113.jsonl.gz": len(backup)},
    )
    store = storage.DiskBackend(tmp_path / "store")
    work = tmp_path / "export"
    (work / "backups").mkdir(parents=True)
    # 上次被打断的半截文件；跨过 4:00 重出清单以前下的旧备份
    (work / ".fetch-abc").write_bytes(b"half")
    (work / "takedowns.jsonl.part").write_bytes(b"half")
    (work / "backups" / "youju-20261112.jsonl.gz").write_bytes(b"older")
    need = len(pdf) + (1 + cloudrun_fetch.BACKUP_FACTOR) * len(backup) + cloudrun_fetch.SPARE_BYTES
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


def _bash(
    tmp_path: Path, body: str, *, stdin: str = "", setup: str = ""
) -> subprocess.CompletedProcess:
    """source 函数后把 BASE、SERVER、EXPORT_DIR 指到临时目录，compose、docker 换成只记录参数的假的。"""
    defs = _functions(tmp_path)
    log = tmp_path / "calls.log"
    script = f"""
source "{defs}"
BASE="{tmp_path}"; SERVER="{tmp_path}/server"; EXPORT_DIR="{tmp_path}/export"; SELF=youju-shared.sh
compose() {{ printf 'compose %s | image=%s db=%s url=%s\\n' "$*" "${{YOUJU_IMAGE:-}}" "${{YOUJU_DATABASE_URL:-}}" "${{YOUJU_EXPORT_URL:-}}" >> "{log}"; }}
docker() {{ printf 'docker %s\\n' "$*" >> "{log}"; }}
{setup}
{body}
"""
    return subprocess.run(["bash", "-c", script], input=stdin, capture_output=True, text=True)


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _server_env(tmp_path: Path, text: str) -> Path:
    (tmp_path / "server").mkdir(exist_ok=True)
    env = tmp_path / "server" / ".env"
    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)
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
    proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd)
    os.close(slave)
    out, sent = b"", False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if not sent and "：".encode() in out:
            with contextlib.suppress(OSError):  # 已经退出了（"："是报错信息里的）
                os.write(master, typed.encode())
            sent = True
        if not ready and proc.poll() is not None:
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


@needs_bash
def test_shared_fetch_export_takes_link_on_stdin(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00t\n")
    sha = "ab" * 32
    link = f"https://{HOST}/exports/manifest-1.json?sign=s&t=1"
    df = "df() { printf 'Avail\\n 100G\\n'; }"  # 不看跑测试的机器还剩多少硬盘
    done = _bash(tmp_path, "fetch_export", stdin=link + "\n", setup=df)
    assert done.returncode == 1 and "fetch-export <哈希>" in done.stderr
    done = _bash(tmp_path, f"fetch_export {sha}", stdin="https://evil.example.com/x\n", setup=df)
    assert done.returncode == 1 and "清单链接不对" in done.stderr
    assert _calls(tmp_path) == []  # 链接不对：什么都没停、没下
    done = _bash(tmp_path, f"fetch_export {sha} --with-advisor", stdin=link + "\n", setup=df)
    assert done.returncode == 1 and "fetch-export <哈希>" in done.stderr  # 只收一个参数
    assert _calls(tmp_path) == []
    done = _bash(tmp_path, f"fetch_export {sha}", stdin=link + "\r\n", setup=df)
    assert done.returncode == 0, done.stderr
    calls = _calls(tmp_path)
    # 先停接口和 worker，结束再起来；链接只经环境变量进容器，不在命令行参数里
    assert calls[0].startswith("compose stop api worker")
    run = calls[1]
    assert run.startswith("compose run --rm --no-deps -e YOUJU_EXPORT_URL -v ")
    assert f"youju export-fetch --sha256 {sha} --dir /export |" in run
    assert run.endswith(f"url={link}") and link not in run.split(" | ")[0]
    assert calls[2].startswith("compose up -d --wait --wait-timeout 900 worker")
    assert calls[3].startswith("compose up -d --wait --wait-timeout 300 api")
    assert (tmp_path / "export").stat().st_mode & 0o777 == 0o700


def _downloaded(tmp_path: Path, *, complete: bool = True) -> Path:
    backups = tmp_path / "export" / "backups"
    backups.mkdir(parents=True)
    (backups / "youju-20261112.jsonl.gz").write_bytes(b"x")
    (tmp_path / "export" / "pay-records.jsonl.gz").write_bytes(b"p")
    (tmp_path / "export" / "manifest.json").write_text("{}", encoding="utf-8")
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
    calls = _calls(tmp_path)
    assert calls[0] == "docker image inspect youju-selfhost:a79a0042"
    assert calls[1].startswith("compose stop api worker")
    drill, purge, archive = calls[2], calls[3], calls[4]
    # 试恢复：旧镜像、root 连接串都经环境变量（不在命令行参数里）
    assert drill.startswith("compose run --rm --no-deps -e YOUJU_DATABASE_URL -v ")
    assert "youju restore-drill --file /export/backups/youju-20261112.jsonl.gz |" in drill
    assert "image=youju-selfhost:a79a0042" in drill
    assert "db=mysql+pymysql://root:r00tpw@mysql:3306/youju?charset=utf8mb4 " in drill
    assert "r00tpw" not in drill.split(" | ")[0]
    # 试恢复写进 binlog 的旧库明文清掉；密码用容器里的环境变量，不在命令里
    assert purge.startswith("compose exec -T mysql sh -c ")
    assert "PURGE BINARY LOGS BEFORE NOW()" in purge and 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD"' in purge
    assert "r00tpw" not in purge
    assert "youju archive-export --dir /export |" in archive and "image= " in archive
    # 明文的整库备份、交易记录和带链接的清单删掉；留下"存档过"的标记（再跑 fetch-export 会拒绝）
    assert not (backups / "youju-20261112.jsonl.gz").exists()
    assert not (tmp_path / "export" / "pay-records.jsonl.gz").exists()
    assert not (tmp_path / "export" / "manifest.json").exists()
    assert (tmp_path / "export" / "imported").exists()
    assert "IMPORT OK" in done.stdout


@needs_bash
def test_shared_import_export_stops_when_drill_fails(tmp_path):
    _server_env(tmp_path, "MYSQL_ROOT_PASSWORD=r00tpw\n")
    backups = _downloaded(tmp_path)
    log = tmp_path / "calls.log"
    failing = f'compose() {{ echo "compose $*" >> "{log}"; [ "$1" != run ]; }}'
    done = _bash(tmp_path, "import_export a79a0042", setup=failing)
    assert done.returncode != 0
    calls = _calls(tmp_path)
    assert not any("archive-export" in c for c in calls)  # 试恢复不过就不存档、不删明文
    assert any("PURGE BINARY LOGS" in c for c in calls)  # binlog 照样清
    assert "试恢复没通过" in done.stderr
    assert (backups / "youju-20261112.jsonl.gz").exists()
    assert not (tmp_path / "export" / "imported").exists()
    assert calls[-1].startswith(
        "compose up -d --wait --wait-timeout 300 api"
    )  # 接口、worker 照样起来


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


@needs_bash
def test_local_carry_takedowns_checks_hash_before_applying(tmp_path):
    shutil.copy(SELFHOST / "local" / "carry-takedowns.sh", tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/docker.log"\n', encoding="utf-8")
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
    assert [line.split()[-3:] for line in log[:2]] == [
        ["apply", "--file", "/move/takedowns.jsonl"],
        ["check", "--file", "/move/takedowns.jsonl"],
    ]
    assert all(line.startswith("compose run --rm --no-deps -v ") for line in log[:2])
    assert log[2] == "compose exec -T worker youju sync-push"  # 马上推一次


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
    assert "python3 -I /tmp/youju-export.py --links-only 2>&1 | tee /tmp/youju-export.log" in last
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


def test_archive_export_keeps_backup_30_days_and_pay_records_3650(tmp_path):
    work = tmp_path / "export"
    (work / "backups").mkdir(parents=True)
    (work / "backups" / "youju-20261112.jsonl.gz").write_bytes(b"backup")
    (work / "pay-records.jsonl.gz").write_bytes(PAY)
    fake = FakeS3()
    reports = cloudrun_fetch.archive_export(
        _archive_settings(tmp_path), work, today=date(2026, 11, 13), backend=fake, encryptor=_seal
    )
    # 整库：自备份那天起 30 天；交易记录：最早一笔下单（北京时间 10-30）起 3650 天
    assert [r.target for r in reports] == [
        "archive/cloudrun-youju-20261112.expires-20261212.jsonl.gz.age",
        "archive/cloudrun-pay-records-20261112.expires-20361027.jsonl.gz.age",
    ]
    assert set(fake.files) == {r.target for r in reports}
    bad = gzip.compress(json.dumps({"format": "youju-pay-records", "version": 1}).encode() + b"\n")
    (work / "pay-records.jsonl.gz").write_bytes(bad)
    with pytest.raises(cloudrun_fetch.FetchError, match="说明行"):
        cloudrun_fetch.archive_export(
            _archive_settings(tmp_path),
            work,
            today=date(2026, 11, 13),
            backend=fake,
            encryptor=_seal,
        )


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
    # 导进来的云托管访问日志（不加密）也按文件名里的到期日删
    log = "archive/cloudrun-access-20260601.expires-20261208.jsonl.gz"
    store.put_file(log, blob)
    early = offsite.prune_archive(settings, today=date(2026, 12, 11), backend=fake)
    assert (early.deleted, early.kept) == ([f"local:{log}"], 4)
    report = offsite.prune_archive(settings, today=date(2026, 12, 12), backend=fake)
    expired = "archive/cloudrun-youju-20261112.expires-20261212.jsonl.gz.age"
    assert report.deleted == [f"local:{expired}", f"offsite:{expired}"]
    assert not store.exists(expired) and expired not in fake.files and not store.exists(log)
    assert store.exists("archive/readme.txt") and len(fake.files) == 1
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
