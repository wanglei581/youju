import time
from types import SimpleNamespace

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
        # 超长路径截断，异常类型在前面不会被挤掉
        message = sent[0]
        assert message.split("：", 1)[1].startswith("RuntimeError，GET /api/v1/__test_boom/")
        assert message.endswith("…") and len(message) < 300

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
