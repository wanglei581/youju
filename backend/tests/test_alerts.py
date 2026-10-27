"""定时任务提醒和栏目检查验收。"""

import logging
import os
import shutil
import subprocess
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from youju import alerts, cli, source_health
from youju.config import Settings
from youju.models import Source, SourceCheck
from youju.source_health import check_sources, failing_sources

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret-key"
NOW = datetime(2026, 9, 28, 7, 15, tzinfo=UTC)


def test_webhook_is_masked_in_settings_repr() -> None:
    settings = Settings(_env_file=None, alert_webhook=WEBHOOK)
    assert "test-secret-key" not in repr(settings)


def test_alert_without_webhook(monkeypatch, caplog, capsys) -> None:
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(alert_webhook=None))
    with caplog.at_level(logging.INFO):
        assert cli.main(["alert", "测试"]) == 1
    assert "未配置提醒，跳过" in caplog.text
    assert "未配置告警，消息未发送" in capsys.readouterr().out


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


def add_source(session, index, *, last_success=None, checks=()):
    source = Source(
        level="省级",
        province="湖南",
        dept="工信",
        dept_full_name=f"部门{index}",
        column_name=f"栏目{index}",
        url=f"https://example{index}.gov.cn/",
        enabled=True,
        parser_config={},
        last_success_at=last_success,
    )
    session.add(source)
    session.flush()
    for started, status in checks:
        session.add(
            SourceCheck(
                source_id=source.id,
                started_at=started,
                finished_at=started + timedelta(seconds=1),
                status=status,
            )
        )
    session.commit()
    return source


def test_failing_sources_carry_last_success_and_days(session) -> None:
    # NOW 是北京时间 9/28 15:15
    add_source(
        session,
        0,
        last_success=NOW - timedelta(days=3),
        checks=[(NOW - timedelta(days=3), "ok"), (NOW - timedelta(hours=1), "parse_empty")],
    )
    # 今天成功过：不算失败
    add_source(
        session, 1, last_success=NOW - timedelta(hours=2), checks=[(NOW - timedelta(hours=2), "ok")]
    )
    # 从没成功过：从第一次检查那天算
    add_source(
        session,
        2,
        checks=[(NOW - timedelta(days=5), "http_error"), (NOW - timedelta(hours=1), "http_error")],
    )
    # 今天早上成功、下午失败：上次成功是今天，0 天
    add_source(
        session,
        3,
        last_success=NOW - timedelta(hours=6),
        checks=[(NOW - timedelta(hours=6), "ok"), (NOW - timedelta(hours=1), "network_error")],
    )
    # 从没检查过：漏检
    add_source(session, 4)
    got = failing_sources(session, now=NOW)
    # 天数 = 连续没抓到第几天：上次成功 9/25 的，9/28 是第 3 天；从没成功过的，第一次检查（9/23）
    # 那天就是第 1 天，9/28 是第 6 天；今天成功过又失败的是 0
    assert [(item.dept, item.status, item.days, item.label()) for item in got] == [
        ("部门0", "parse_empty", 3, "上次成功 9月25日"),
        ("部门2", "http_error", 6, "从没成功过，9月23日起"),
        ("部门3", "network_error", 0, "上次成功 9月28日"),
        ("部门4", "漏检", None, "从没检查过"),
    ]
    # 和成功率检查是同一批失败
    assert len(got) == len(check_sources(session, now=NOW).failures)
    assert got[0].text() == "部门0 · 栏目0 · parse_empty（上次成功 9月25日）"


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("days", "ok_sources", "args", "code", "alerts_sent"),
    [
        # 达标（20/21 ≥ 95%）且有栏目正好连续第 3 天没抓到：单独提醒一次，任务照常成功
        (3, 20, ["check-sources", "--streaks", "--job"], 0, 1),
        # 第 2、4 天不提醒；第 7 天是下一个节点（第 3 天那次早轮没跑也不会永远错过）
        (2, 20, ["check-sources", "--streaks", "--job"], 0, 0),
        (4, 20, ["check-sources", "--streaks", "--job"], 0, 0),
        (7, 20, ["check-sources", "--streaks", "--job"], 0, 1),
        # 晚上那一轮不带 --streaks：不看连续天数
        (3, 20, ["check-sources", "--job"], 0, 0),
        # 不达标：连续天数和成功率各发一条，按"详细提醒已发"退出
        (3, 5, ["check-sources", "--streaks", "--job"], 20, 2),
    ],
)
def test_check_sources_streak_alert_on_day_three(
    session, monkeypatch, days, ok_sources, args, code, alerts_sent
) -> None:
    monkeypatch.setattr(source_health, "datetime", FixedClock)
    for index in range(ok_sources):
        add_source(
            session,
            index,
            last_success=NOW - timedelta(hours=1),
            checks=[(NOW - timedelta(hours=1), "ok")],
        )
    add_source(
        session,
        99,
        last_success=NOW - timedelta(days=days),
        checks=[(NOW - timedelta(days=days), "ok"), (NOW - timedelta(hours=1), "parse_empty")],
    )
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    assert cli.main(args) == code
    assert len(sent) == alerts_sent
    if alerts_sent:
        assert sent[0].startswith("这些栏目连续好几天没抓到") and "成功率" not in sent[0]
        last = (NOW - timedelta(days=days)).astimezone(source_health.BEIJING)
        assert (
            f"第 {days} 天：部门99 · 栏目99 · parse_empty（上次成功 {last.month}月{last.day}日）"
            in sent[0]
        )
    if alerts_sent == 2:
        assert "成功率" in sent[1] and "部门99 · 栏目99 · parse_empty" in sent[1]


def test_streak_milestones() -> None:
    from youju.source_health import is_streak_milestone

    hits = [days for days in range(0, 125) if is_streak_milestone(days)]
    assert hits == [3, 7, 14, 30, 60, 90, 120]
    assert not is_streak_milestone(None)


def test_never_successful_source_alerts_on_its_third_failing_day(session, monkeypatch) -> None:
    """新接入就一直失败的栏目：第一次检查那天是第 1 天，第 3 天提醒。"""
    monkeypatch.setattr(source_health, "datetime", FixedClock)
    for index in range(20):
        add_source(
            session,
            index,
            last_success=NOW - timedelta(hours=1),
            checks=[(NOW - timedelta(hours=1), "ok")],
        )
    add_source(
        session,
        99,
        checks=[(NOW - timedelta(days=2), "http_error"), (NOW - timedelta(hours=1), "http_error")],
    )
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    assert cli.main(["check-sources", "--streaks", "--job"]) == 0
    assert (
        len(sent) == 1
        and "第 3 天：部门99 · 栏目99 · http_error（从没成功过，9月26日起）" in sent[0]
    )


def test_streak_alerts_list_every_source_within_the_message_limit(session, monkeypatch) -> None:
    """栏目多、名称长时按群消息的字节上限分成几条发，每个到节点的栏目都列出来。"""
    monkeypatch.setattr(source_health, "datetime", FixedClock)
    for index in range(30):
        source = add_source(
            session,
            index,
            last_success=NOW - timedelta(days=3),
            checks=[(NOW - timedelta(days=3), "ok"), (NOW - timedelta(hours=1), "parse_empty")],
        )
        source.dept_full_name = f"某某省某某某某某某厅{index}" * 3
        source.column_name = "通知公告栏目名称很长" * 2
    session.commit()
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    # 30 个都失败：成功率也不达标，另发一条成功率提醒，按"详细提醒已发"退出
    assert cli.main(["check-sources", "--streaks", "--job"]) == 20
    streak = [text for text in sent if text.startswith("这些栏目连续好几天没抓到")]
    assert len(streak) > 1 and len(sent) == len(streak) + 1
    for text in streak:
        assert len(("【有据】" + text).encode()) <= alerts.MAX_ALERT_BYTES
    listed = [line for text in streak for line in text.splitlines() if line.startswith("第 3 天：")]
    assert len(listed) == 30
    # 其中一条没发出去：任务算失败
    calls = []
    monkeypatch.setattr(
        alerts, "send_alert", lambda message: calls.append(message) or len(calls) != 2
    )
    assert cli.main(["check-sources", "--streaks", "--job"]) == 1


def test_streak_alert_that_fails_to_send_fails_the_job(session, monkeypatch) -> None:
    """第 3 天只提醒这一次：发送失败要在任务记录里看得到；没配告警地址不算失败。"""
    monkeypatch.setattr(source_health, "datetime", FixedClock)
    for index in range(20):
        add_source(
            session,
            index,
            last_success=NOW - timedelta(hours=1),
            checks=[(NOW - timedelta(hours=1), "ok")],
        )
    add_source(
        session,
        99,
        last_success=NOW - timedelta(days=3),
        checks=[(NOW - timedelta(days=3), "ok"), (NOW - timedelta(hours=1), "parse_empty")],
    )
    monkeypatch.setattr(alerts, "send_alert", lambda message: False)
    assert cli.main(["check-sources", "--streaks", "--job"]) == 1
    monkeypatch.setattr(alerts, "send_alert", lambda message: None)
    assert cli.main(["check-sources", "--streaks", "--job"]) == 0
    # 不达标时：成功率那条发出去了、连续天数那条没发出去，也不算"详细提醒已发"
    for index in range(20, 25):
        add_source(
            session,
            index,
            last_success=NOW - timedelta(days=1),
            checks=[(NOW - timedelta(hours=1), "parse_empty")],
        )
    monkeypatch.setattr(alerts, "send_alert", lambda message: "成功率" in message)
    assert cli.main(["check-sources", "--streaks", "--job"]) == 1


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
    youju.write_text(
        '#!/bin/sh\ncase "$1" in job-start|job-finish) exit 1 ;; esac\n'
        'printf "%s\\n" "$*" >> "$ALERT_CALLS"\nexit 1\n'
    )
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


def _job_script(tmp_path: Path) -> Path:
    """真实的 job.sh，环境、日志和 /tmp 下的锁、开始时间都换到测试目录。"""
    original = (Path(__file__).resolve().parents[1] / "deploy" / "job.sh").read_text()
    script = (
        original.replace("/app/backend/deploy/env.sh", str(tmp_path / "env.sh"))
        .replace("cd /app/backend", f"cd {tmp_path}")
        .replace("/proc/1/fd/1", str(tmp_path / "job.log"))
        .replace("/tmp/youju-", f"{tmp_path}/youju-")
    )
    (tmp_path / "job.sh").write_text(script)
    (tmp_path / "env.sh").write_text("")
    return tmp_path / "job.sh"


def _stub(tmp_path: Path, name: str, body: str) -> None:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


# 容器里的 /bin/sh 是 dash：本机有 dash 时两种都跑
SHELLS = ["sh"] + (["dash"] if shutil.which("dash") else [])


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("started", "flock_code", "expected_code", "alert"),
    [
        ("ago:30", 75, 0, None),
        ("ago:590", 75, 0, None),
        ("ago:610", 75, 75, "上一次还没跑完"),
        ("ago:-120", 75, 75, "上一次还没跑完"),  # 时钟往回调过：当作卡住
        ("garbage", 75, 75, "上一次还没跑完"),
        (None, 75, 75, "上一次还没跑完"),
        ("ago:30", 1, 1, "任务执行失败"),  # 不是等锁超时：照常提醒
    ],
)
def test_advisor_run_overlap_is_quiet_unless_stuck(
    tmp_path, shell, started, flock_code, expected_code, alert
):
    # advisor-run 领到的任务做完才退出，常跨过下一分钟。下一分钟这一次等满 50 秒还没拿到锁：
    # 上一轮开始不到 10 分钟就安静跳过；超过 10 分钟、读不到开始时间、时钟往回调过都照常提醒
    job = _job_script(tmp_path)
    if started == "garbage":
        (tmp_path / "youju-advisor-run.started").write_text("abc\n")
    elif started is not None:
        ago = int(started.split(":")[1])
        (tmp_path / "youju-advisor-run.started").write_text(f"{int(time.time()) - ago}\n")
    _stub(tmp_path, "flock", 'printf "%s\\n" "$*" > "$FLOCK_ARGS"\nexit "$FLOCK_CODE"\n')
    _stub(tmp_path, "youju", 'printf "%s\\n" "$*" >> "$ALERT_CALLS"\nexit 0\n')
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FLOCK_ARGS": str(tmp_path / "flock.args"),
        "FLOCK_CODE": str(flock_code),
        "ALERT_CALLS": str(tmp_path / "alert.calls"),
    }
    result = subprocess.run([shell, str(job), "advisor-run"], env=env, check=False)
    assert result.returncode == expected_code
    # 下一分钟这一次最多等 50 秒，上一轮一结束就接着跑
    assert (tmp_path / "flock.args").read_text().startswith("-w 50 -E 75 ")
    calls = (tmp_path / "alert.calls").read_text() if (tmp_path / "alert.calls").exists() else ""
    log = (tmp_path / "job.log").read_text()
    if alert:
        assert alert in calls and f"退出码 {flock_code}" in calls
        assert "跳过：youju advisor-run" not in log
    else:
        assert not calls
        assert "跳过：youju advisor-run" in log


@pytest.mark.parametrize("shell", SHELLS)
def test_advisor_run_records_start_time_inside_lock(tmp_path, shell):
    # 拿到锁以后才写开始时间（锁外写会把正在跑的那一轮的时间改掉）；youju 只收到 advisor-run，
    # 开始时间文件不会混进参数；别的任务不写开始时间、还是不等锁
    job = _job_script(tmp_path)
    _stub(
        tmp_path,
        "flock",
        'printf "%s\\n" "$*" >> "$FLOCK_ARGS"\n'
        'case "$1" in -w) shift 5 ;; *) shift 4 ;; esac\nexec "$@"\n',
    )
    _stub(tmp_path, "timeout", 'shift\nexec "$@"\n')
    _stub(tmp_path, "youju", 'printf "%s\\n" "$*" >> "$CALLS"\nexit 0\n')
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "FLOCK_ARGS": str(tmp_path / "flock.args"),
    }
    before = int(time.time())
    assert subprocess.run([shell, str(job), "advisor-run"], env=env, check=False).returncode == 0
    started = int((tmp_path / "youju-advisor-run.started").read_text())
    assert before <= started <= int(time.time())
    assert not (tmp_path / "youju-advisor-run.started.tmp").exists()
    assert (tmp_path / "calls").read_text().splitlines() == ["advisor-run"]
    assert subprocess.run([shell, str(job), "crawl"], env=env, check=False).returncode == 0
    assert not (tmp_path / "youju-crawl.started").exists()
    flock_args = (tmp_path / "flock.args").read_text().splitlines()
    assert flock_args[0].startswith("-w 50 -E 75 ") and flock_args[1].startswith("-n -E 75 ")


@pytest.mark.parametrize(
    ("round_locked", "crawl_code", "args", "expected"),
    [
        (
            False,
            0,
            [],
            [
                "crawl",
                "check-sources",
                "fetch-details",
                "attachment-texts",
                "repost-groups",
                "extract-deadlines",
                "extract-fields",
                "ai-check",
                "extract-conditions",
                "triage-feedback",
                "parse-lists",
            ],
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
                "attachment-texts",
                "repost-groups",
                "extract-deadlines",
                "extract-fields",
                "ai-check",
                "extract-conditions",
                "triage-feedback",
                "parse-lists",
                "notify",
            ],
        ),
        # 抓取失败（或抓取本身重叠）时不核对栏目，避免把还没检查到的栏目误报成漏检
        (
            False,
            1,
            [],
            [
                "crawl",
                "fetch-details",
                "attachment-texts",
                "repost-groups",
                "extract-deadlines",
                "extract-fields",
                "ai-check",
                "extract-conditions",
                "triage-feedback",
                "parse-lists",
            ],
        ),
        (
            False,
            75,
            [],
            [
                "crawl",
                "fetch-details",
                "attachment-texts",
                "repost-groups",
                "extract-deadlines",
                "extract-fields",
                "ai-check",
                "extract-conditions",
                "triage-feedback",
                "parse-lists",
            ],
        ),
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
        .replace("cd /app/backend", f"cd {tmp_path}")
        .replace("/proc/1/fd/1", str(tmp_path / "round.log"))
        .replace("/tmp/youju-round.lock", str(tmp_path / "round.lock"))
    )
    (tmp_path / "round.sh").write_text(script)
    (tmp_path / "env.sh").write_text("")
    # 这组只核对任务顺序；记账失败不影响任务，不调用真实 CLI 或数据库。
    youju = tmp_path / "youju"
    youju.write_text("#!/bin/sh\nexit 1\n")
    youju.chmod(0o755)
    job = tmp_path / "job.sh"
    job.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
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
    calls = (tmp_path / "calls").read_text().splitlines()
    assert [line.split()[0] for line in calls] == expected
    if "attachment-texts" in expected:
        # 补完详情（这里补详情失败也照跑）紧接着取附件文字，每轮最多 200 个（取舍写在 round.sh 里）
        assert "attachment-texts --limit 200" in calls


@pytest.mark.parametrize(
    ("round_locked", "failing", "expected", "code"),
    [
        (
            False,
            "",
            [
                "import-sources",
                "crawl",
                "fetch-details",
                "extract-deadlines",
                "parse-lists",
            ],
            0,
        ),
        # 某一步失败就停下（失败提醒由 job.sh 发）
        (False, "crawl", ["import-sources", "crawl"], 1),
        # 定时任务的一轮正在进行：什么都不做，提示后退出
        (True, "", [], 1),
    ],
)
def test_first_round_runs_every_step_through_job_sh(
    tmp_path, round_locked, failing, expected, code
) -> None:
    original = (Path(__file__).resolve().parents[1] / "deploy" / "first-round.sh").read_text()
    script = original.replace("D=/app/backend/deploy", f"D={tmp_path}").replace(
        "/tmp/youju-round.lock", str(tmp_path / "round.lock")
    )
    (tmp_path / "first-round.sh").write_text(script)
    job = tmp_path / "job.sh"
    job.write_text(
        '#!/bin/sh\nprintf "%s %s\\n" "$1" "$*" >> "$CALLS"\n'
        '[ "$1" = "$FAILING" ] && exit 1\n'
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
        "FAILING": failing,
        "ROUND_LOCKED": "1" if round_locked else "0",
    }
    result = subprocess.run(["sh", str(tmp_path / "first-round.sh")], env=env, check=False)
    assert result.returncode == code
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    assert [line.split()[0] for line in calls] == expected
    if expected[2:3] == ["fetch-details"]:
        # 补详情一次补完：--limit 2000（定时任务每轮只补 300）
        assert "--limit 2000" in calls[2]


def test_alert_probe_missing_configuration_fails_without_sending(monkeypatch, capsys):
    import json

    monkeypatch.setattr(
        alerts, "get_settings", lambda: SimpleNamespace(alert_webhook=None, role="admin")
    )
    monkeypatch.setattr(alerts, "send_alert", lambda _: pytest.fail("must not send"))
    for args in ([], ["--send"]):
        assert cli.main(["alert-probe", *args]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["state"] == "not_configured"
        assert result["configured"] is False
        assert result["received"] is False
        assert result["ready_for_release"] is False
        assert "probe_id" not in result


def test_alert_probe_is_read_only_by_default(monkeypatch, capsys):
    import json

    monkeypatch.setattr(
        alerts,
        "get_settings",
        lambda: SimpleNamespace(alert_webhook=SecretStr(WEBHOOK), role="api"),
    )
    monkeypatch.setattr(alerts, "send_alert", lambda _: pytest.fail("must not send"))
    assert cli.main(["alert-probe"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["configured"] is True
    assert result["state"] == "not_tested"
    assert result["received"] is False
    assert result["ready_for_release"] is False
    assert "test-secret-key" not in output


@pytest.mark.parametrize(
    "sent,code,state", [(True, 0, "submitted"), (False, 1, "failed"), (None, 1, "failed")]
)
def test_alert_probe_correlates_submission_without_claiming_receipt(
    monkeypatch, capsys, sent, code, state
):
    import json

    monkeypatch.setattr(
        alerts,
        "get_settings",
        lambda: SimpleNamespace(alert_webhook=SecretStr(WEBHOOK), role="admin"),
    )
    messages = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: messages.append(message) or sent)
    assert cli.main(["alert-probe", "--send", "--probe-id", "acceptance-1015"]) == code
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == state
    assert result["probe_id"] == "acceptance-1015"
    assert result["received"] is False
    assert result["ready_for_release"] is False
    assert messages == ["告警验收测试，标记 acceptance-1015。这是受控测试，无需处理业务故障。"]


def test_alert_probe_rejects_arbitrary_message_in_marker(monkeypatch, capsys):
    monkeypatch.setattr(alerts, "send_alert", lambda _: pytest.fail("must not send"))
    assert cli.main(["alert-probe", "--send", "--probe-id", "secret\nmessage"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "secret" not in output.err
