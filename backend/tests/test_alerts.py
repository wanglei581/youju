"""定时任务提醒和栏目检查验收。"""

import logging
import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from youju import alerts, cli
from youju.config import Settings
from youju.models import Source, SourceCheck
from youju.source_health import check_sources

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret-key"
NOW = datetime(2026, 9, 28, 7, 15, tzinfo=UTC)


def test_webhook_is_masked_in_settings_repr() -> None:
    settings = Settings(_env_file=None, alert_webhook=WEBHOOK)
    assert "test-secret-key" not in repr(settings)


def test_alert_without_webhook(monkeypatch, caplog, capsys) -> None:
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(alert_webhook=None))
    with caplog.at_level(logging.INFO):
        assert cli.main(["alert", "测试"]) == 0
    assert "未配置提醒，跳过" in caplog.text
    assert capsys.readouterr().out == ""


def test_alert_posts_text_and_limits_utf8_bytes(monkeypatch, caplog, capsys) -> None:
    monkeypatch.setattr(
        alerts, "get_settings", lambda: SimpleNamespace(alert_webhook=SecretStr(WEBHOOK))
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200, json={"errcode": 0, "errmsg": "ok"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(alerts.httpx, "post", post)
    with caplog.at_level(logging.INFO):
        assert cli.main(["alert", "测试文本"]) == 0
        assert cli.main(["alert", "中" * 2000]) == 0
    assert calls[0] == (
        WEBHOOK,
        {"json": {"msgtype": "text", "text": {"content": "【有据】测试文本"}}, "timeout": 10.0},
    )
    long_text = calls[1][1]["json"]["text"]["content"]
    assert long_text.startswith("【有据】") and long_text.endswith("…")
    assert len(long_text.encode("utf-8")) <= alerts.MAX_ALERT_BYTES
    assert WEBHOOK not in caplog.text + capsys.readouterr().out
    assert "test-secret-key" not in caplog.text


@pytest.mark.parametrize("failure", ["network", "http", "robot"])
def test_alert_failure_never_logs_webhook(monkeypatch, caplog, capsys, failure) -> None:
    monkeypatch.setattr(
        alerts, "get_settings", lambda: SimpleNamespace(alert_webhook=SecretStr(WEBHOOK))
    )

    def post(url, **kwargs):
        if failure == "network":
            raise httpx.ConnectError(f"failed for {url}")
        if failure == "http":
            return httpx.Response(500, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"errcode": 45009, "errmsg": url})

    monkeypatch.setattr(alerts.httpx, "post", post)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["alert", "失败测试"]) == 1
    visible = caplog.text + capsys.readouterr().out
    assert "提醒发送失败" in visible
    assert WEBHOOK not in visible
    assert "test-secret-key" not in visible


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement):
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.parametrize(
    ("rows", "passed", "fragment"),
    [
        ([], False, "成功率 0/0"),
        ([("部门", "栏目", "ok")] * 20, True, None),
        (
            [("部门", "栏目", "ok")] * 19 + [("失败部门", "通知", "http_error")],
            True,
            None,
        ),
        (
            [("部门", "栏目", "ok")] * 18 + [("失败部门", "通知", "http_error")] * 2,
            False,
            "失败部门 · 通知 · http_error",
        ),
        (
            [("部门", "栏目", "ok")] * 19 + [("漏检部门", "公告", None)],
            False,
            "漏检部门 · 公告 · 漏检",
        ),
    ],
)
def test_check_sources_cli_outcomes(monkeypatch, caplog, rows, passed, fragment) -> None:
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: nullcontext(FakeSession(rows)))
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    with caplog.at_level(logging.INFO):
        assert cli.main(["check-sources"]) == (0 if passed else 1)
    if passed:
        assert not sent
        assert "检查达标" in caplog.text
    else:
        assert len(sent) == 1
        assert fragment in sent[0]
        assert "成功率" in sent[0]
        assert cli.main(["check-sources", "--job"]) == 20


def test_check_sources_lists_only_ten_and_keeps_failure_code(monkeypatch) -> None:
    rows = [(f"部门{i}", f"栏目{i}", "network_error") for i in range(12)]
    monkeypatch.setattr(cli, "session_factory", lambda: lambda: nullcontext(FakeSession(rows)))
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or False)
    assert cli.main(["check-sources", "--job"]) == 1
    assert len(sent) == 1
    assert sent[0].count(" · network_error") == 10
    assert "部门9" in sent[0] and "部门10" not in sent[0]
    assert "另有 2 个未列出" in sent[0]


def test_check_sources_uses_latest_record_today(session) -> None:
    # NOW 是北京时间 9/28 15:15；"今天"从北京时间 9/28 零点起算
    for index in range(4):
        source = Source(
            level="省级",
            province="北京",
            dept="科技",
            dept_full_name=f"部门{index}",
            column_name=f"栏目{index}",
            url=f"https://example{index}.gov.cn/",
            enabled=index != 2,
            parser_config={},
        )
        session.add(source)
        session.flush()
        checks = (
            [
                (NOW - timedelta(hours=27), "ok"),
                (NOW - timedelta(hours=2), "ok"),
                (NOW - timedelta(hours=1), "http_error"),
            ]
            if index == 0
            # 部门3：昨晚 19:15（北京时间）成功过，今天还没检查——26 小时窗口会把它算成功，今天算漏检
            else [(NOW - timedelta(hours=20), "ok")]
            if index == 3
            else [(NOW - timedelta(hours=27), "ok")]
        )
        for started, status in checks:
            session.add(
                SourceCheck(
                    source_id=source.id,
                    started_at=started,
                    finished_at=started + timedelta(seconds=1),
                    status=status,
                )
            )
    session.flush()
    health = check_sources(session, now=NOW)
    assert (health.total, health.successful, health.missing) == (3, 0, 2)
    assert health.failures == (
        "部门0 · 栏目0 · http_error",
        "部门1 · 栏目1 · 漏检",
        "部门3 · 栏目3 · 漏检",
    )


@pytest.mark.parametrize(
    ("task", "task_code", "expected_code", "expected_alert"),
    [
        ("crawl", 75, 75, "上一次还没跑完，本次重叠跳过"),
        ("crawl", 124, 124, "已超时"),
        ("check-sources", 20, 1, None),
        ("check-sources", 1, 1, "任务执行失败"),
    ],
)
def test_job_alerts_without_changing_failure_code(
    tmp_path, task, task_code, expected_code, expected_alert
) -> None:
    original = (Path(__file__).resolve().parents[1] / "deploy" / "job.sh").read_text()
    script = (
        original.replace("/app/backend/deploy/env.sh", str(tmp_path / "env.sh"))
        .replace("cd /app/backend", f"cd {tmp_path}")
        .replace("/proc/1/fd/1", str(tmp_path / "job.log"))
    )
    (tmp_path / "job.sh").write_text(script)
    (tmp_path / "env.sh").write_text("")
    flock = tmp_path / "flock"
    flock.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$FLOCK_ARGS"\nexit "$TASK_CODE"\n')
    flock.chmod(0o755)
    youju = tmp_path / "youju"
    youju.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ALERT_CALLS"\nexit 1\n')
    youju.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TASK_CODE": str(task_code),
        "FLOCK_ARGS": str(tmp_path / "flock.args"),
        "ALERT_CALLS": str(tmp_path / "alert.calls"),
    }
    result = subprocess.run(["sh", str(tmp_path / "job.sh"), task], env=env, check=False)
    assert result.returncode == expected_code
    assert "-n -E 75" in (tmp_path / "flock.args").read_text()
    calls = (tmp_path / "alert.calls").read_text() if (tmp_path / "alert.calls").exists() else ""
    if expected_alert:
        assert expected_alert in calls
        assert f"退出码 {task_code}" in calls
    else:
        assert not calls


@pytest.mark.parametrize(
    ("round_locked", "crawl_code", "args", "expected"),
    [
        (
            False,
            0,
            [],
            ["crawl", "check-sources", "fetch-details", "extract-deadlines", "parse-lists"],
        ),
        # 早上那一轮：全部做完后再发微信提醒
        (
            False,
            0,
            ["--notify"],
            [
                "crawl",
                "check-sources",
                "fetch-details",
                "extract-deadlines",
                "parse-lists",
                "notify",
            ],
        ),
        # 抓取失败（或抓取本身重叠）时不核对栏目，避免把还没检查到的栏目误报成漏检
        (False, 1, [], ["crawl", "fetch-details", "extract-deadlines", "parse-lists"]),
        (False, 75, [], ["crawl", "fetch-details", "extract-deadlines", "parse-lists"]),
        # 上一轮还没跑完：整轮跳过并发提醒；早上那一轮照样发微信提醒，当天不漏发
        (True, 0, [], ["alert"]),
        (True, 0, ["--notify"], ["alert", "notify"]),
    ],
)
def test_round_runs_in_order_and_checks_only_after_crawl(
    tmp_path, round_locked, crawl_code, args, expected
) -> None:
    original = (Path(__file__).resolve().parents[1] / "deploy" / "round.sh").read_text()
    script = (
        original.replace("D=/app/backend/deploy", f"D={tmp_path}")
        .replace("/proc/1/fd/1", str(tmp_path / "round.log"))
        .replace("/tmp/youju-round.lock", str(tmp_path / "round.lock"))
    )
    (tmp_path / "round.sh").write_text(script)
    job = tmp_path / "job.sh"
    job.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$1" >> "$CALLS"\n'
        '[ "$1" = crawl ] && exit "$CRAWL_CODE"\n'
        '[ "$1" = fetch-details ] && exit 1\n'
        "exit 0\n"
    )
    job.chmod(0o755)
    flock = tmp_path / "flock"
    flock.write_text('#!/bin/sh\nexit "$ROUND_LOCKED"\n')
    flock.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "CRAWL_CODE": str(crawl_code),
        "ROUND_LOCKED": "1" if round_locked else "0",
    }
    result = subprocess.run(["sh", str(tmp_path / "round.sh"), *args], env=env, check=False)
    assert result.returncode == 0
    assert (tmp_path / "calls").read_text().split() == expected
