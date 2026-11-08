"""2.0 自建服务器的运维命令：恢复到刚迁移好的库、外部健康检查、异地备份、两份定时任务表。"""

import gzip
import json
import re
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from youju import cli, offsite, storage
from youju.access_log import mask_ip
from youju.backup import BackupError
from youju.config import get_settings
from youju.jobs import JOB_NAMES

from .test_job_names import _cron_commands, _job_names_in

SELFHOST = Path(__file__).resolve().parents[1] / "deploy" / "selfhost"


def test_clear_seed_rows_only_on_a_fresh_database(session_factory, db_engine):
    # conftest 每个用例都放一行迁移写的 signing_keys：这就是"刚迁移好的空库"
    cli._clear_seed_rows(db_engine)
    with db_engine.connect() as conn:
        assert conn.scalar(text("SELECT COUNT(*) FROM signing_keys")) == 0
    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO signing_keys (name, key_hex) VALUES ('list_entry', :k)"),
            {"k": "1" * 64},
        )
        conn.execute(text("INSERT INTO users (openid) VALUES ('o-1')"))
    with pytest.raises(BackupError) as err:
        cli._clear_seed_rows(db_engine)
    assert "users" in str(err.value)
    with db_engine.connect() as conn:  # 有数据的库一行都不删
        assert conn.scalar(text("SELECT COUNT(*) FROM signing_keys")) == 1


def test_uptime_check_alerts_once_and_on_recovery(tmp_path, monkeypatch):
    sent, replies = [], []
    monkeypatch.setattr("youju.alerts.send_alert", lambda m: sent.append(m))

    def fake_get(url, **kw):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(httpx, "get", fake_get)
    state = tmp_path / "state.json"

    def run():
        return cli.main(["uptime-check", "--url", "https://api.example.com", "--state", str(state)])

    down = httpx.ConnectError("x")
    up = httpx.Response(200, json={"status": "ok"})
    # 每次看两处（接口、定时任务）；第一处不通就不看第二处
    replies.extend([down, down, down, up, up, up, up])
    assert run() == 1 and sent == []  # 第一次不通先不提醒
    assert run() == 1 and len(sent) == 1 and "连续 2 次" in sent[0]
    assert run() == 1 and len(sent) == 1  # 同一次故障只提醒一次
    assert run() == 0 and len(sent) == 2 and "恢复" in sent[1]
    assert run() == 0 and len(sent) == 2
    assert json.loads(state.read_text()) == {"fails": 0, "alerted": False}


class FakeOffsite:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, dict]] = {}

    def put_file(self, path, local, *, headers=None):
        self.objects[path] = (Path(local).read_bytes(), headers or {})

    def list(self, prefix):
        return [
            storage.StoredObject(k, datetime(2026, 1, 1, tzinfo=UTC))
            for k in sorted(self.objects)
            if k.startswith(prefix)
        ]

    def delete(self, path):
        self.objects.pop(path, None)


def offsite_settings(tmp_path):
    return SimpleNamespace(
        storage_backend="disk",
        storage_dir=tmp_path / "store",
        offsite_s3_endpoint="https://cos.ap-shanghai.myqcloud.com",
        offsite_s3_region="ap-shanghai",
        offsite_s3_bucket="youju-backup-1250000000",
        offsite_s3_access_key=SecretStr("ak"),
        offsite_s3_secret_key=SecretStr("sk"),
        offsite_s3_path_style=False,
        offsite_storage_class="STANDARD_IA",
        offsite_age_recipient="age1" + "q" * 58,
        offsite_keep_days=30,
    )


def test_offsite_encrypts_uploads_to_archive_and_prunes(tmp_path):
    settings = offsite_settings(tmp_path)
    backups = tmp_path / "store" / "backups"
    backups.mkdir(parents=True)
    (backups / "youju-20261105.jsonl.gz").write_bytes(b"backup-bytes")
    fake = FakeOffsite()
    fake.objects["backups/youju-20260901.jsonl.gz.age"] = (b"old", {})
    fake.objects["backups/youju-20261101.jsonl.gz.age"] = (b"recent", {})
    seen = []

    def encryptor(source, target, recipient):
        seen.append(recipient)
        target.write_bytes(b"sealed:" + source.read_bytes())

    report = offsite.run(
        settings, now=datetime(2026, 11, 5, 21, tzinfo=UTC), backend=fake, encryptor=encryptor
    )
    assert report.target == "backups/youju-20261105.jsonl.gz.age"
    body, headers = fake.objects[report.target]
    assert body == b"sealed:backup-bytes" and headers == {"x-cos-storage-class": "STANDARD_IA"}
    assert seen == [settings.offsite_age_recipient]
    # 30 天以前的删掉，近的留着
    assert "backups/youju-20260901.jsonl.gz.age" not in fake.objects
    assert "backups/youju-20261101.jsonl.gz.age" in fake.objects and report.pruned == 1


def test_offsite_refuses_without_config_or_backup(tmp_path):
    settings = offsite_settings(tmp_path)
    settings.offsite_age_recipient = ""
    with pytest.raises(BackupError):
        offsite.run(settings, backend=FakeOffsite())
    settings = offsite_settings(tmp_path)
    with pytest.raises(BackupError) as err:
        offsite.run(settings, backend=FakeOffsite(), encryptor=lambda *a: None)
    assert "近 3 天没有整库备份" in str(err.value)


def test_storage_class_header_names():
    assert offsite.storage_class_header("https://cos.ap-shanghai.myqcloud.com", "ARCHIVE") == {
        "x-cos-storage-class": "ARCHIVE"
    }
    assert offsite.storage_class_header("https://minio.local", "GLACIER") == {
        "x-amz-storage-class": "GLACIER"
    }
    assert offsite.storage_class_header("https://x", "") == {}


def test_mask_ip():
    assert mask_ip("1.2.3.4") == "1.2.x.x"
    assert mask_ip("2408:8207:abcd:1::5") == "2408:8207:abcd:x"
    assert mask_ip(None) is None


@pytest.mark.parametrize("which", ["server", "local"])
def test_selfhost_crontabs_only_use_known_jobs(which):
    text_ = (SELFHOST / which / "crontab").read_text(encoding="utf-8")
    names = _job_names_in(_cron_commands(text_))
    assert names, which
    assert names <= set(JOB_NAMES) | {"advisor-run", "pay-run", "ai-heartbeat", "ai-eligibility"}


def test_user_data_jobs_never_run_on_the_local_machine():
    local = _job_names_in(_cron_commands((SELFHOST / "local" / "crontab").read_text("utf-8")))
    server = _job_names_in(_cron_commands((SELFHOST / "server" / "crontab").read_text("utf-8")))
    user_jobs = {
        "advisor-run",
        "pay-run",
        "pay-reconcile",
        "notify",
        "triage-feedback",
        "archive-logs",
        "cleanup",
    }
    assert not local & user_jobs
    # 抓取和处理只在本机
    assert not server & {
        "crawl",
        "fetch-details",
        "cache-attachments",
        "list-evidence",
        "check-links",
    }
    assert {"sync-push", "backup"} <= local and {"backup", "offsite-backup", "notify"} <= server


def test_reclassify_is_skipped_on_the_replica(monkeypatch, capsys):
    monkeypatch.setenv("YOUJU_HOSTING", "self")
    monkeypatch.setenv("YOUJU_PUBLIC_REPLICA", "true")
    get_settings.cache_clear()
    try:
        assert cli.main(["reclassify"]) == 0
        assert "不在这里重算" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_windows_setup_script_is_utf8_with_bom():
    raw = (SELFHOST / "local" / "setup.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Windows PowerShell 5.1 没有 BOM 会把中文读成乱码
    assert "docker compose up -d" in raw.decode("utf-8-sig")


def test_wsl_setup_scripts_encoding():
    """WSL 方式的两个脚本：ps1 要 BOM + CRLF（Windows PowerShell 5.1），sh 只能是 LF、可执行（bash）。"""
    ps1 = (SELFHOST / "local" / "setup-wsl.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in ps1.replace(b"\r\n", b"")  # 每一行都是 CRLF
    text = ps1.decode("utf-8-sig")
    # 不用 Stop：由别的程序启动时，5.1 会把外部命令的标准错误当成报错中止脚本
    assert '$ErrorActionPreference = "Continue"' in text
    # 中文引号在 PowerShell 里也算字符串引号，写进提示会把字符串截断
    assert "“" not in text and "”" not in text
    sh_path = SELFHOST / "local" / "setup-wsl.sh"
    assert b"\r" not in sh_path.read_bytes()
    assert sh_path.stat().st_mode & 0o111


def test_wsl_setup_keeps_review_fixes():
    """11/8 审阅改过的几处，别被改回去。"""
    ps1 = (SELFHOST / "local" / "setup-wsl.ps1").read_text(encoding="utf-8-sig")
    # 保活：循环重开 wsl（计划任务的"失败重启"不可靠）、conhost 开窗口（Windows Terminal 下藏不住）、
    # -Force 更新已有任务、装 Docker 之前就启动
    assert "while ($true)" in ps1 and '-Execute "conhost.exe"' in ps1 and "-Force" in ps1
    assert ps1.index("Start-ScheduledTask") < ps1.index('setup-wsl.sh")')
    # 数据位置按盘符固定（<盘>:\youju），不跟着安装包解压位置走
    assert "Split-Path -Qualifier $PSScriptRoot" in ps1
    # 只拦开着 UAC 时提升过的管理员窗口（令牌提升类型 2）；关了 UAC 的电脑（11/8 创始人的主机
    # EnableLUA=0，所有程序都是管理员身份）照常装
    assert "GetTokenInformation(token, 18" in ps1 and "$elevation -eq 2" in ps1
    # 升级时先删旧源码再拷，拷贝管道失败要报出来
    assert "rm -rf /opt/youju/local/source" in ps1 and "set -euo pipefail" in ps1
    sh = (SELFHOST / "local" / "setup-wsl.sh").read_text(encoding="utf-8")
    # 导入按「库是空的」判断（不靠标记文件），配了服务器的空库不自动导入、不启动 worker
    assert "SELECT COUNT(*) FROM youju.sources" in sh and "env_value YOUJU_SYNC_URL" in sh
    compose = (SELFHOST / "local" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "start_period: 600s" in compose


def test_caddy_error_log_drops_query_strings():
    """反代出错时 Caddy 的日志会带完整请求地址：查询串（检索词、企业名称）要去掉，和访问日志一样只留路径。"""
    caddyfile = (SELFHOST / "server" / "Caddyfile").read_text(encoding="utf-8")
    assert 'request>uri regexp "\\?.*" "?"' in caddyfile


def test_windows_package_marks_chinese_names_utf8(tmp_path):
    """安装包里的中文文件名（docs/数据来源）要标成 UTF-8：不标的话 Windows 解压时按 GBK 读成乱码，
    构建镜像时找不到来源清单（11/8 在测试虚拟机里就是这样失败的）。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    script = SELFHOST / "make-local-package.sh"
    subprocess.run(["sh", str(script), str(tmp_path)], check=True, capture_output=True)
    (package,) = tmp_path.glob("youju-local-*.zip")
    with zipfile.ZipFile(package) as z:
        names = z.namelist()
        # 顶层是 local 文件夹：解压到 E:\youju 就是 E:\youju\local
        assert all(name.startswith("local/") for name in names)
        assert any(name.startswith("local/source/docs/数据来源/") for name in names)
        assert all(i.flag_bits & 0x800 for i in z.infolist() if not i.filename.isascii())
        for wanted in ("setup-wsl.ps1", "setup-wsl.sh", "setup.ps1", "docker-compose.yml"):
            assert f"local/{wanted}" in names, wanted


def _alembic_head():
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    revisions, parents = set(), set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        found = re.search(r"^revision\b[^=]*=\s*['\"]([^'\"]+)", text, re.M)
        if found:
            revisions.add(found.group(1))
        for line in re.findall(r"^down_revision\b[^=]*=(.*)$", text, re.M):
            parents.update(re.findall(r"['\"]([^'\"]+)['\"]", line))
    (head,) = revisions - parents
    return head


@pytest.mark.parametrize("matches", [True, False])
def test_windows_package_checks_starter_data_revision(tmp_path, matches):
    """起步数据的迁移版本要等于源码的最新迁移，不然装到导入那一步必然失败：不一致就不打包。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    revision = _alembic_head() if matches else "9999"
    export = tmp_path / "export.jsonl.gz"
    with gzip.open(export, "wt", encoding="utf-8") as f:
        f.write(
            json.dumps({"format": "youju-sync-export", "version": 1, "revision": revision}) + "\n"
        )
    out = tmp_path / "out"
    out.mkdir()
    script = SELFHOST / "make-local-package.sh"
    done = subprocess.run(
        ["sh", str(script), str(out), str(export)], capture_output=True, text=True
    )
    assert (done.returncode == 0) is matches, done.stderr
    assert bool(list(out.glob("youju-local-*.zip"))) is matches


def test_compose_base_images_pinned_by_digest():
    """国内经镜像站拉 Docker Hub 的镜像：按摘要锁定，Docker 校验内容，镜像站换不了。"""
    for name in ("local", "server", "test"):
        for line in (SELFHOST / name / "docker-compose.yml").read_text().splitlines():
            if line.strip().startswith("image:") and ("mysql" in line or "caddy" in line):
                assert "@sha256:" in line, f"{name}: {line.strip()}"


def test_offsite_pull_keeps_a_ciphertext_copy_locally(tmp_path):
    """本机每天把异地最新的加密备份下一份（已经有就不下），删掉过期的。"""
    settings = offsite_settings(tmp_path)
    settings.offsite_age_recipient = ""  # 本机不需要公钥
    fake = FakeOffsite()
    fake.objects["backups/youju-20261104.jsonl.gz.age"] = (b"old-cipher", {})
    fake.objects["backups/youju-20261105.jsonl.gz.age"] = (b"new-cipher", {})
    fake.url = lambda path, max_age: f"https://cos.example/{path}"
    old_local = tmp_path / "store" / "offsite-backups"
    old_local.mkdir(parents=True)
    (old_local / "youju-20260901.jsonl.gz.age").write_bytes(b"expired")
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=fake.objects[request.url.path.lstrip("/")][0])

    http = httpx.Client(transport=httpx.MockTransport(handle))
    report = offsite.pull(
        settings, now=datetime(2026, 11, 5, 23, tzinfo=UTC), backend=fake, http=http
    )
    assert report.target == "offsite-backups/youju-20261105.jsonl.gz.age"
    assert (old_local / "youju-20261105.jsonl.gz.age").read_bytes() == b"new-cipher"
    assert not (old_local / "youju-20260901.jsonl.gz.age").exists() and report.pruned == 1
    # 再跑一次：已经有了，不再下载
    again = offsite.pull(
        settings, now=datetime(2026, 11, 5, 23, tzinfo=UTC), backend=fake, http=http
    )
    assert again.bytes == 0 and len(seen) == 1


def test_local_only_mode_skips_sync_and_offsite_pull(monkeypatch, capsys):
    """服务器还没买时本机先单独跑：推送、拉异地备份安静跳过（返回 0，job.sh 不提醒）。"""
    for key in ("YOUJU_SYNC_URL", "YOUJU_SYNC_SECRET", "YOUJU_OFFSITE_S3_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        assert cli.main(["sync-push"]) == 0
        assert cli.main(["offsite-pull"]) == 0
        out = capsys.readouterr().out
        assert "跳过推送" in out and "跳过" in out
    finally:
        get_settings.cache_clear()
