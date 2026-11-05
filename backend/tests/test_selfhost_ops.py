"""2.0 自建服务器的运维命令：恢复到刚迁移好的库、外部健康检查、异地备份、两份定时任务表。"""

import json
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
        offsite_storage_class="ARCHIVE",
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
    assert body == b"sealed:backup-bytes" and headers == {"x-cos-storage-class": "ARCHIVE"}
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
