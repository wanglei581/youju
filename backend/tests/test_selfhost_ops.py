"""2.0 自建服务器的运维命令：恢复到刚迁移好的库、外部健康检查、异地备份、两份定时任务表。"""

import ast
import gzip
import json
import os
import re
import shutil
import subprocess
import tarfile
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
from youju.config import Settings, get_settings
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
    # 北京时间 11/6 跑：每份正好留满 30 天——10/7 那份到今天满 30 天，删；10/8 那份留着
    fake.objects["backups/youju-20261007.jsonl.gz.age"] = (b"thirty-days", {})
    fake.objects["backups/youju-20261008.jsonl.gz.age"] = (b"twenty-nine-days", {})
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
    assert "backups/youju-20261007.jsonl.gz.age" not in fake.objects
    assert "backups/youju-20261008.jsonl.gz.age" in fake.objects
    assert "backups/youju-20261101.jsonl.gz.age" in fake.objects and report.pruned == 2


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


@pytest.mark.parametrize("which", ["server/crontab", "local/crontab", "shared/crontab.idle"])
def test_selfhost_crontabs_only_use_known_jobs(which):
    text_ = (SELFHOST / which).read_text(encoding="utf-8")
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
    # 旧库存档到期删除（D-127）：只在服务器上跑；测试期的共用服务器也跑（备份副本最长 30 天）
    idle = _job_names_in(_cron_commands((SELFHOST / "shared" / "crontab.idle").read_text("utf-8")))
    assert "archive-prune" in server and idle == {"archive-prune"} and "archive-prune" not in local


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
    assert "while ($true)" in ps1 and '-Execute "conhost.exe"' in ps1
    assert re.search(r"Register-ScheduledTask [^\n]*-Force", ps1)
    # 重跑时任务已经在跑就不再启动；只有注册失败才手动开保活进程（不占着安装包文件夹）
    assert '"$state" -ne "Running"' in ps1 and "-WorkingDirectory $env:SystemRoot" in ps1
    assert ps1.index("Start-ScheduledTask") < ps1.index('setup-wsl.sh")')
    # 数据位置按盘符固定（<盘>:\youju），不跟着安装包解压位置走
    assert "Split-Path -Qualifier $PSScriptRoot" in ps1
    # 只拦开着 UAC 时提升过的管理员窗口（令牌提升类型 2）；关了 UAC 的电脑（EnableLUA=0，
    # 所有程序都是管理员身份）照常装
    assert "GetTokenInformation(token, 18" in ps1 and "$elevation -eq 2" in ps1
    # 升级时先删旧源码再拷，拷贝管道失败要报出来
    assert "rm -rf /opt/youju/local/source" in ps1 and "set -euo pipefail" in ps1
    sh = (SELFHOST / "local" / "setup-wsl.sh").read_text(encoding="utf-8")
    # 导入按「库是空的」判断（不靠标记文件），配了服务器的空库不自动导入、不启动 worker
    assert "SELECT COUNT(*) FROM youju.sources" in sh and "env_value YOUJU_SYNC_URL" in sh
    # 升级时先停旧 worker 再迁移；查不到数据量就停下，不照样启动 worker
    assert sh.index("docker compose stop worker") < sh.index("worker youju db-upgrade")
    assert 'if [ -z "$sources" ]; then' in sh
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
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name):
                continue
            if target.id == "revision":
                revisions.add(ast.literal_eval(value))
            elif target.id == "down_revision":
                down = ast.literal_eval(value)
                parents.update(down if isinstance(down, (tuple, list)) else [down] if down else [])
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
    for name in ("local", "server", "test", "shared"):
        for line in (SELFHOST / name / "docker-compose.yml").read_text().splitlines():
            if line.strip().startswith("image:") and ("mysql" in line or "caddy" in line):
                assert "@sha256:" in line, f"{name}: {line.strip()}"


SHARED = SELFHOST / "shared"


def _shared_services():
    """共用服务器的 compose 文件按缩进切成各服务的文本（测试环境没有 PyYAML）。"""
    text = (SHARED / "docker-compose.yml").read_text(encoding="utf-8")
    body = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services = {}
    for block in re.split(r"\n(?=  [a-z]+:\n)", "\n" + body):
        found = re.match(r"\n?  ([a-z]+):\n", block)
        if found:
            services[found.group(1)] = block
    return text, services


def test_shared_server_compose_stays_small_and_local():
    """D-126：和别的项目共用一台 4G 的服务器——三个容器合计不超过 1.5G、不许用交换区、主机内存紧张时
    先停有据的；只听本机回环地址（对外只经宝塔 nginx 的站点），不带 Caddy（80、443 是宝塔的）。"""
    text, services = _shared_services()
    assert set(services) == {"mysql", "api", "worker"}
    total = 0
    for name, block in services.items():
        limit = re.search(r"^    mem_limit: (\d+)m$", block, re.M).group(1)
        swap = re.search(r"^    memswap_limit: (\d+)m$", block, re.M).group(1)
        assert swap == limit, name
        total += int(limit)
    assert total <= 1536
    # 接口和 worker 经 x-app 继承
    assert (
        "oom_score_adj: 500" in services["mysql"]
        and "oom_score_adj: 500" in text.split("services:")[0]
    )
    assert re.findall(r'^\s+- "([^"]+)"$', text, re.M) == [
        "127.0.0.1:18100:8000",
        "127.0.0.1:18101:8000",
    ]
    assert "image: caddy" not in text and '"80:80"' not in text and '"443:443"' not in text
    # 测试期 worker 只写心跳，上线前在 .env 里换成服务器的定时任务表
    assert (
        "${YOUJU_CRONTAB:-/app/backend/deploy/selfhost/shared/crontab.idle}" in services["worker"]
    )


def test_shared_server_nginx_site_only_forwards_app_paths():
    """宝塔 nginx 上有据的站点：只转小程序接口、健康检查、消息推送和本机同步，别的路径 404；关掉宝塔
    proxy.conf 全局开着的 proxy_cache 和 404 换上游（接口回的是各人自己的数据）；不记访问日志；来源地址覆盖写入。"""
    conf = (SHARED / "nginx-https.conf").read_text(encoding="utf-8")
    https = conf.split("listen 443 ssl;", 1)[1]
    assert re.findall(r"^    location ([^{]+?) \{", https, re.M) == [
        "^~ /api/sync/",
        "= /health/worker",
        "^~ /api/v1/",
        "= /health",
        "= /api/wx/push",
        "/",
    ]
    assert set(re.findall(r"proxy_pass (\S+);", https)) == {
        "http://127.0.0.1:18101",
        "http://127.0.0.1:18101/health",
        "http://127.0.0.1:18100",
    }
    assert https.rstrip().endswith("location / {\n        return 404;\n    }\n}")
    for needed in (
        "proxy_cache off;",
        "proxy_next_upstream off;",
        "access_log off;",
        "proxy_set_header X-Forwarded-For $remote_addr;",
    ):
        assert needed in https, needed
    http_only = (SHARED / "nginx-http.conf").read_text(encoding="utf-8")
    assert "proxy_pass" not in http_only and "return 404;" in http_only


def test_shared_server_script_guards():
    """youju-shared.sh 以 root 跑在别的项目也在用的服务器上：几处保护别被改掉。"""
    path = SHARED / "youju-shared.sh"
    sh = path.read_text(encoding="utf-8")
    assert sh.startswith("#!/bin/bash\n") and "\nset -euo pipefail\n" in sh
    assert b"\r" not in path.read_bytes()
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(path)], check=True)
    units = re.search(r'DESKTOP_UNITS="([^"]+)"', sh).group(1).split()
    # 只许停桌面、远程桌面、打印和桌面配套这几类（D-126）；网卡靠的 NetworkManager、安全更新、Docker、SSH、
    # 定时任务、别的项目的服务一个都不能混进来：清单要改先改这里
    assert units[0] == "gdm.service" and len(units) == len(set(units))
    assert set(units) == {
        "gdm.service",
        "gnome-remote-desktop.service",
        "xrdp.service",
        "xrdp-sesman.service",
        "cups.path",
        "cups.socket",
        "cups.service",
        "cups-browsed.service",
        "avahi-daemon.socket",
        "avahi-daemon.service",
        "fwupd-refresh.timer",
        "fwupd.service",
        "ModemManager.service",
        "kerneloops.service",
        "power-profiles-daemon.service",
        "switcheroo-control.service",
        "colord.service",
        "accounts-daemon.service",
        "upower.service",
        "udisks2.service",
        "rtkit-daemon.service",
    }
    # 这把钥匙只能传文件：sftp、restrict（不给命令行和端口转发）
    assert (
        'restrict,command="%s -d %s -P %s"' in sh
        and "SFTP_SERVER=/usr/lib/openssh/sftp-server" in sh
    )
    assert "port-forwarding" not in sh and "permitopen" not in sh
    # 只能上传：读文件、列目录、链接、服务器内复制都不许
    assert "SFTP_DENY=read,opendir,readdir,readlink,symlink,hardlink,copy-data\n" in sh
    # 家目录、.ssh、钥匙文件都归 root：youju 用 sftp 改不了自己这把钥匙的限制
    assert "useradd --no-create-home --home-dir /home/youju" in sh
    assert "install -d -m 755 -o root -g root /home/youju /home/youju/.ssh" in sh
    assert "chown root:root /home/youju/.ssh/authorized_keys.tmp" in sh
    assert not re.search(r"(?<!no-)create-home", sh)
    # 解包前先拷到只有 root 能进的目录再核对哈希（incoming 归 youju，核对完可能被换掉）
    assert sh.index('install -m 600 "$BASE/incoming/$name"') < sh.index("sha256sum -c -")
    # 改无桌面开机之前，先查有没有别的服务只挂在桌面模式下
    assert sh.index("extra=$(graphical_only) || die") < sh.index(
        "systemctl set-default multi-user.target"
    )
    # 续期按"现在的证书和 nginx 已加载的不一样"判断：哪次重载失败，下周还会再试
    assert '"$(cat "$LOADED_FILE" 2>/dev/null || true)"' in sh and "mark_loaded" in sh
    # nginx：动之前现有配置要能过检查；新文件过不了就撤回、不重载
    assert sh.index('"$NGINX" -t >/dev/null 2>&1 || die') < sh.index(
        'mv -f "$SITE.youju-tmp" "$SITE"'
    )
    assert "已撤回，nginx 没有重载" in sh
    # 改之前整个站点目录先打包备份（只用来对照，回退只删有据自己的文件）
    assert sh.index('tar -czf "$backup"') < sh.index('mv -f "$SITE.youju-tmp" "$SITE"')
    # 改 nginx 前后核对别的站点状态码的只读子命令
    assert '  sites) sites "$@" ;;' in sh and '--resolve "$h:443:127.0.0.1"' in sh
    # 停桌面服务的回退命令写的是正在跑的这份脚本的绝对路径；gdm 以别名启用的也照原样恢复
    assert 'SELF=$(readlink -f "$0")' in sh and '[ "$enabled" = alias ]' in sh
    # 不能为了腾内存去结束一个用户的全部会话：别的项目的进程可能挂在它的 SSH 会话下
    assert "terminate-user" not in sh and "pkill -u ubuntu" not in sh
    # 证书工具按摘要锁定；.env 只有 root 能读、已有就不覆盖
    assert re.search(r"CERTBOT_IMAGE=certbot/certbot:v[\d.]+@sha256:[0-9a-f]{64}\n", sh)
    assert "umask 077" in sh and "已有 ${SERVER}/.env，不动" in sh
    # 不把 bash 函数起名叫 install / env：会盖掉脚本里要用的系统命令
    assert not re.search(r"^(install|env)\(\)", sh, re.M)


def test_shared_server_files_keep_server_details_out_of_the_repo():
    """仓库是公开的：共用服务器的 IP、域名和盘点结果不写进这些文件，用占位符；具体值在创始人电脑上的本地文件。"""
    texts = [path.read_text(encoding="utf-8") for path in SHARED.iterdir() if path.is_file()]
    doc = Path(__file__).resolve().parents[2] / "docs" / "部署" / "自建服务器.md"
    texts.append(doc.read_text(encoding="utf-8").split("## 十、", 1)[1])
    for body in texts:
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", body):
            assert ip in {"127.0.0.1", "0.0.0.0"}, ip
    assert "DOMAIN=${YOUJU_DOMAIN:-}\n" in (SHARED / "youju-shared.sh").read_text(encoding="utf-8")
    section = doc.read_text(encoding="utf-8").split("## 十、", 1)[1]
    assert section.index("install -m 600 /opt/youju/incoming/") < section.index("sha256sum -c -")
    assert "-o root -g root /home/youju /home/youju/.ssh" in section
    assert "chown root:root /home/youju/.ssh/authorized_keys" in section


def test_shared_server_env_passes_startup_checks(monkeypatch):
    """youju-shared.sh env 生成的配置要能过程序启动时的检查（接口、worker 两个角色），不然装到 up 才发现
    起不来；测试期微信接口指向本机不存在的地址，不拿占位的 AppSecret 去调微信。"""
    sh = (SHARED / "youju-shared.sh").read_text(encoding="utf-8")
    body = sh.split('cat > "$SERVER/.env.tmp" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]
    values = {
        "ver": "abcdef12",
        "root_pw": "r" * 48,
        "db_pw": "d" * 48,
        "APPID": "wx0000000000000000",
        "aes": "A" * 43,
        "cloud_env": "test-env",
    }
    env = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        value = re.sub(
            r"\$\(openssl rand -hex (\d+)\)", lambda m: "f" * (2 * int(m.group(1))), value
        )
        value = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], value)
        assert "$" not in value, line
        env[key] = value
    assert env["YOUJU_WX_API_BASE"] == "http://127.0.0.1:9"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for role in ("api", "admin"):
        monkeypatch.setenv("YOUJU_ROLE", role)
        settings = Settings(_env_file=None)
        assert (settings.hosting, settings.role, settings.public_replica) == ("self", role, True)


def test_server_package_structure(tmp_path):
    """共用服务器的安装包：shared/ 是部署文件，source/ 是构建镜像的源码（VERSION 是提交号），属主都是 root。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    script = SELFHOST / "make-server-package.sh"
    done = subprocess.run(
        ["sh", str(script), str(tmp_path)], check=True, capture_output=True, text=True
    )
    (package,) = tmp_path.glob("youju-server-*.tar.gz")
    sha = package.name.removeprefix("youju-server-").removesuffix(".tar.gz")
    assert re.fullmatch(r"[0-9a-f]{8}", sha) and package.name in done.stdout
    with tarfile.open(package) as tar:
        members = {m.name: m for m in tar.getmembers()}
        for name in ("docker-compose.yml", "mysql.cnf", "nginx-http.conf", "nginx-https.conf"):
            assert f"shared/{name}" in members, name
        assert members["shared/youju-shared.sh"].mode == 0o755
        assert tar.extractfile("source/VERSION").read().decode().strip() == sha
        assert "source/Dockerfile" in members and "source/backend/src/youju/config.py" in members
        assert any(name.startswith("source/docs/数据来源/") for name in members)
        assert all(m.uid == 0 and m.gid == 0 for m in members.values())
        assert all(name.split("/", 1)[0] in {"shared", "source"} for name in members)
        assert not any(name.endswith(".env") for name in members)


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
    # 北京时间 11/6 跑：10/7 那份满 30 天，删；10/8 那份留着
    (old_local / "youju-20261007.jsonl.gz.age").write_bytes(b"thirty-days")
    (old_local / "youju-20261008.jsonl.gz.age").write_bytes(b"twenty-nine-days")
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
    assert not (old_local / "youju-20260901.jsonl.gz.age").exists()
    assert not (old_local / "youju-20261007.jsonl.gz.age").exists()
    assert (old_local / "youju-20261008.jsonl.gz.age").exists() and report.pruned == 2
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


def test_manifest_url_check_ignores_locale():
    """清单链接的格式检查在 C 语言环境里比。11/12 导出窗口：OrcaTerm 的 root 是 LANG=en_US.UTF-8，
    bash 按排序规则解释 [!-~]，字母不在范围里，合法链接被判成"清单链接不对"。"""
    sh = (SHARED / "youju-shared.sh").read_text(encoding="utf-8")
    fn = re.search(r"^manifest_url_ok\(\) \(\n.*?\n\)\n", sh, re.S | re.M)
    assert fn, "manifest_url_ok 要是一个在子 shell 里跑的函数（LC_ALL 不影响外面）"
    assert "\n  LC_ALL=C\n" in fn.group(0)
    assert 'manifest_url_ok "$url"' in sh
    if not shutil.which("bash"):
        pytest.skip("没有 bash")
    good = (
        "https://7072-prod-x-1.tcb.qcloud.la/exports/manifest-20261112152810.json"
        "?sign=0123456789abcdef&t=1700000000"
    )

    def run(script: str, url: str, lang: str) -> int:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
        env["LANG"] = lang
        return subprocess.run(
            ["bash", "-c", script, "t", url], env=env, capture_output=True
        ).returncode

    # 对照：不加 LC_ALL=C 的老写法在这台机器上确实判错，才说明复现得了；复现不了（没装这个语言环境，
    # 或者不是 glibc，比如 macOS）的话测试就证明不了什么：CI 上算失败（该装语言环境了），本机跳过
    old = fn.group(0).replace("\n  LC_ALL=C\n", "\n")
    if run(old + 'manifest_url_ok "$1"', good, "en_US.UTF-8") == 0:
        if os.environ.get("CI"):
            pytest.fail("CI 上复现不了 en_US.UTF-8 下的老问题：先在工作流里装好这个语言环境")
        pytest.skip("这台机器复现不了 en_US.UTF-8 下的老问题（没有这个语言环境或不是 glibc）")
    cases = [
        (good, 0),
        ("https://youju-backup-1250000000.cos.ap-shanghai.myqcloud.com/exports/m.json?q=1", 0),
        ("http://x.tcb.qcloud.la/a", 1),
        ("https://evil.example.com/a", 1),
        ("https://x.tcb.qcloud.la/a b", 1),
        ("https://x.tcb.qcloud.la/a\tb", 1),
        ("", 1),
    ]
    for lang in ("en_US.UTF-8", "C.UTF-8"):
        for url, code in cases:
            assert run(fn.group(0) + 'manifest_url_ok "$1"', url, lang) == code, (lang, url)


def test_pasted_manifest_url_only_trims_the_ends():
    """粘贴的链接只去掉首尾的空白和回车；中间带回车、空白的原样留着、判不对，不会悄悄拼成另一条链接
    （Codex 确认轮）。"""
    sh = (SHARED / "youju-shared.sh").read_text(encoding="utf-8")
    trim = re.search(r"^trim_pasted\(\) \{.*\}\n", sh, re.M)
    check = re.search(r"^manifest_url_ok\(\) \(\n.*?\n\)\n", sh, re.S | re.M)
    assert trim and check
    assert 'url=$(trim_pasted "$url")' in sh and "tr -d '\\r'" not in sh
    if not shutil.which("bash"):
        pytest.skip("没有 bash")
    script = (
        trim.group(0)
        + check.group(0)
        + 'u=$(trim_pasted "$1"); manifest_url_ok "$u" && printf %s "$u"'
    )
    url = "https://x.tcb.qcloud.la/exports/m.json?sign=ab&t=1"
    for raw, want in [
        (f"  {url}\r", url),
        (f"\t{url} \r\r", url),
        (f"{url}\n", url),
        ("https://x.tcb.qcloud.la/exports/m.json?si\rgn=ab", None),
        ("https://x.tcb.qcloud.la/exports/m.json ?sign=ab", None),
    ]:
        done = subprocess.run(["bash", "-c", script, "t", raw], capture_output=True, text=True)
        if want is None:
            assert done.returncode == 1 and done.stdout == "", repr(raw)
        else:
            assert done.returncode == 0 and done.stdout == want, repr(raw)
