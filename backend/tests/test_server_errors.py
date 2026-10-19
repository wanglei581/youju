import time
from collections import deque
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

from youju.api import app as app_module
from youju.api.app import app
from youju.db import get_session


def test_unhandled_error_returns_json_500_and_alerts_at_most_every_10_minutes(monkeypatch) -> None:
    sent: list[str] = []
    now = {"t": 1000.0}
    monkeypatch.setattr(app_module, "_send_in_background", sent.append)
    # 只替换 app 模块里的 clock，不动全局 time 模块
    monkeypatch.setattr(
        app_module, "clock", SimpleNamespace(monotonic=lambda: now["t"], time=time.time)
    )
    monkeypatch.setattr(app_module, "_error_state", {"last_alert": None, "count": 0})
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(role="api"))

    def broken_session():
        raise RuntimeError("数据库连不上 user=secret")

    app.dependency_overrides[get_session] = broken_session
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/search", params={"q": "张三的手机号"})
        assert response.status_code == 500
        assert response.json() == {"detail": "服务出错了，请稍后再试"}
        assert sent == ["接口出错（api 服务，上次提醒以来 1 次）：RuntimeError，GET /api/v1/search"]
        # 提醒不带查询参数和异常详情
        assert "张三" not in sent[0] and "secret" not in sent[0]

        # 10 分钟内再出错只计数，不再提醒
        now["t"] += 60
        assert client.get("/api/v1/home").status_code == 500
        assert len(sent) == 1

        # 超过 10 分钟后再出错：提醒一次，带上这期间的次数
        now["t"] += 600
        assert client.get("/api/v1/home").status_code == 500
        assert sent[1] == "接口出错（api 服务，上次提醒以来 2 次）：RuntimeError，GET /api/v1/home"
    finally:
        app.dependency_overrides.clear()


def test_http_errors_are_not_alerted(monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(app_module, "_send_in_background", sent.append)
    client = TestClient(app, raise_server_exceptions=False)
    # 业务上主动返回的 4xx 不是故障
    assert client.get("/api/v1/documents/not-a-number").status_code == 422
    assert client.get("/admin-does-not-exist").status_code == 404
    assert sent == []


def test_error_alert_path_is_sanitized_and_handler_never_fails(monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(app_module, "_send_in_background", sent.append)
    monkeypatch.setattr(app_module, "_error_state", {"last_alert": None, "count": 0})
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(role="api"))

    def boom(rest: str):
        raise RuntimeError("down")

    # 临时挂一个会出错的路由，用带换行、超长的路径请求它
    app.add_api_route("/api/v1/__test_boom/{rest:path}", boom)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/api/v1/__test_boom/" + "9" * 500).status_code == 500
        assert len(sent) == 1
        # 群里只写路由模板，不写实际路径（路径里可能有编号、名称）；异常类型在前面
        message = sent[0]
        assert message.split("：", 1)[1] == "RuntimeError，GET /api/v1/__test_boom/{rest:path}"
        assert "999" not in message and len(message) < 300

        # 提醒这一步自己出错（例如读配置失败）：照样返回固定的 500
        def settings_broken():
            raise ValueError("配置读不到")

        monkeypatch.setattr(app_module, "get_settings", settings_broken)
        monkeypatch.setattr(app_module, "_error_state", {"last_alert": None, "count": 0})
        response = client.get("/api/v1/__test_boom/x")
        assert response.status_code == 500
        assert response.json() == {"detail": "服务出错了，请稍后再试"}
    finally:
        app.router.routes[:] = [
            r
            for r in app.router.routes
            if getattr(r, "path", "") != "/api/v1/__test_boom/{rest:path}"
        ]


def test_printable_replaces_control_characters() -> None:
    # 路径里的回车、换行、终端控制符都不能原样进日志和提醒
    assert app_module._printable("/a\r\nb\x1b[31m", 50) == "/a??b?[31m"
    assert app_module._printable("/" + "x" * 10, 5) == "/xxxx…"


def test_uvicorn_runs_single_process() -> None:
    # 接口出错提醒和口令限流都按进程计数：启动脚本必须固定单进程（不受 WEB_CONCURRENCY 影响）
    from pathlib import Path

    start = (Path(__file__).parents[1] / "deploy" / "start.sh").read_text()
    exec_line = next(line for line in start.splitlines() if line.startswith("exec uvicorn"))
    assert "--workers 1" in exec_line


def _fresh_failed_state(monkeypatch, now, result=True):
    """成批失败提醒在测试里同步发出，send_alert 记下消息并返回 result（True 发出、False 没发出去）。"""
    sent: list[str] = []

    def fake_send(message):
        sent.append(message)
        return result

    monkeypatch.setattr(app_module, "_send_in_background", sent.append)
    monkeypatch.setattr(app_module, "send_alert", fake_send)
    monkeypatch.setattr(app_module, "_start_background", lambda target, *args: target(*args))
    monkeypatch.setattr(
        app_module, "clock", SimpleNamespace(monotonic=lambda: now["t"], time=time.time)
    )
    monkeypatch.setattr(app_module, "_failed_state", {"recent": deque(), "last_alert": None})
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(role="api"))
    return sent


def test_deliberate_5xx_bursts_alert_once_then_stay_quiet_for_an_hour(monkeypatch) -> None:
    """接口主动返回的 5xx（如"附件暂时取不到"）：10 分钟内满 3 次提醒一条，1 小时内不重复。"""
    now = {"t": 5000.0}
    sent = _fresh_failed_state(monkeypatch, now)

    def unavailable(doc_id: int):
        raise HTTPException(status_code=503, detail="附件暂时取不到，请稍后再试")

    app.add_api_route("/api/v1/__test_unavailable/{doc_id}", unavailable)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        for doc_id in (11, 12):
            assert client.get(f"/api/v1/__test_unavailable/{doc_id}").status_code == 503
            now["t"] += 60
        assert sent == []  # 偶尔一两次不打扰人
        assert client.get("/api/v1/__test_unavailable/13").status_code == 503
        # 只写路由模板，不写实际路径（路径里的编号、名称都不带）
        assert sent == [
            "接口成批失败（api 服务，10 分钟内 3 次：返回 5xx 3 次、其他请求超过 15 秒 0 次）："
            "/api/v1/__test_unavailable/{doc_id} 3 次。1 小时内不再重复提醒，"
            "详情看云托管日志里的 access 记录"
        ]
        # 1 小时内再失败只计数
        for _ in range(5):
            now["t"] += 60
            client.get("/api/v1/__test_unavailable/14")
        assert len(sent) == 1
        # 过了 1 小时还在成批失败：再提醒一次
        now["t"] += 3600
        for _ in range(3):
            client.get("/api/v1/__test_unavailable/15")
        assert len(sent) == 2
    finally:
        app.router.routes[:] = [
            r
            for r in app.router.routes
            if getattr(r, "path", "") != "/api/v1/__test_unavailable/{doc_id}"
        ]


def test_slow_requests_count_and_old_failures_drop_out_of_the_window(monkeypatch) -> None:
    now = {"t": 9000.0}
    sent = _fresh_failed_state(monkeypatch, now)
    note = app_module._note_failed_response
    # 4xx 和正常耗时的请求不算
    note("/api/v1/search", 404, 20)
    note("/api/v1/search", 429, 20)
    note("/api/v1/search", 200, 14_999)
    assert not app_module._failed_state["recent"]
    # 两次超过 15 秒，过了 10 分钟才来第三次：窗口里只有 1 次，不提醒
    note("/api/v1/search", 200, 15_000)
    note("/api/v1/search", 200, 30_000)
    now["t"] += 601
    note("/api/v1/search", 200, 16_000)
    assert sent == []
    # 没匹配到路由的只写"其他路径"
    note(None, 503, 10)
    note("/api/v1/search", 200, 16_000)
    assert sent == [
        "接口成批失败（api 服务，10 分钟内 3 次：返回 5xx 1 次、其他请求超过 15 秒 2 次）："
        "/api/v1/search 2 次；其他路径 1 次。1 小时内不再重复提醒，"
        "详情看云托管日志里的 access 记录"
    ]


def test_failed_alert_that_did_not_go_out_is_retried_after_five_minutes(monkeypatch) -> None:
    now = {"t": 20000.0}
    sent = _fresh_failed_state(monkeypatch, now, result=False)
    note = app_module._note_failed_response
    for _ in range(3):
        note("/api/v1/search", 503, 10)
    assert len(sent) == 1  # 发了，但机器人没收（返回 False）
    now["t"] += 240
    note("/api/v1/search", 503, 10)
    assert len(sent) == 1  # 不到 5 分钟不再试
    now["t"] += 61
    note("/api/v1/search", 503, 10)
    assert len(sent) == 2  # 过了 5 分钟、还在成批失败：再发
    # 这次发出去了：恢复 1 小时静默
    monkeypatch.setattr(app_module, "send_alert", lambda message: sent.append(message) or True)
    now["t"] += 301
    for _ in range(3):
        note("/api/v1/search", 503, 10)
    assert len(sent) == 3
    now["t"] += 600
    for _ in range(3):
        note("/api/v1/search", 503, 10)
    assert len(sent) == 3


def test_unhandled_errors_are_not_counted_twice(monkeypatch) -> None:
    """没被处理的异常已经由异常处理提醒，成批失败只数主动返回的 5xx，免得一次故障两条提醒。"""
    now = {"t": 100.0}
    sent = _fresh_failed_state(monkeypatch, now)
    monkeypatch.setattr(app_module, "_error_state", {"last_alert": None, "count": 0})

    def broken_session():
        raise RuntimeError("down")

    app.dependency_overrides[get_session] = broken_session
    try:
        client = TestClient(app, raise_server_exceptions=False)
        for _ in range(4):
            assert client.get("/api/v1/home").status_code == 500
    finally:
        app.dependency_overrides.clear()
    assert not app_module._failed_state["recent"]
    assert sent == ["接口出错（api 服务，上次提醒以来 1 次）：RuntimeError，GET /api/v1/home"]


def test_start_logs_whether_alerts_are_configured_without_blocking_startup() -> None:
    """启动时打一行告警地址有没有配（部署后看日志就能核对 api 配没配）；没配不能拦住启动。"""
    from pathlib import Path

    start = (Path(__file__).parents[1] / "deploy" / "start.sh").read_text()
    lines = start.splitlines()
    probe = lines.index("youju alert-probe || :")
    assert probe < next(i for i, line in enumerate(lines) if line.startswith("exec uvicorn"))


def test_only_the_morning_round_alerts_three_day_streaks() -> None:
    from pathlib import Path

    round_sh = (Path(__file__).parents[1] / "deploy" / "round.sh").read_text()
    assert (
        'if [ "$1" = "--notify" ]; then\n  run crawl && run check-sources --streaks\n' in round_sh
    )
    assert "else\n  run crawl && run check-sources\nfi\n" in round_sh
