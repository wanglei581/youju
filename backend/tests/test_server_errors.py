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
        assert sent == ["接口出错（api 服务，上次提醒以来 1 次）：GET /api/v1/search：RuntimeError"]
        # 提醒不带查询参数和异常详情
        assert "张三" not in sent[0] and "secret" not in sent[0]

        # 10 分钟内再出错只计数，不再提醒
        now["t"] += 60
        assert client.get("/api/v1/home").status_code == 500
        assert len(sent) == 1

        # 超过 10 分钟后再出错：提醒一次，带上这期间的次数
        now["t"] += 600
        assert client.get("/api/v1/home").status_code == 500
        assert sent[1] == "接口出错（api 服务，上次提醒以来 2 次）：GET /api/v1/home：RuntimeError"
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
