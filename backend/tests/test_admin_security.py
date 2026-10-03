"""管理台防嵌入、解析前请求体限制与限流容量保护。"""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from youju.api import app as admin
from youju.db import get_session

TOKEN = "security-test-token"
SECURITY_HEADERS = {
    "x-frame-options": "SAMEORIGIN",
    "content-security-policy": "frame-ancestors 'self'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


@pytest.fixture(autouse=True)
def security_settings(monkeypatch):
    monkeypatch.setattr(
        admin, "get_settings", lambda: SimpleNamespace(role="all", admin_token=TOKEN)
    )
    entries = []
    monkeypatch.setattr(admin.access_log, "write", entries.append)
    return entries


def scope_for(path, method="POST", headers=()):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "method": method,
        "headers": list(headers),
        "client": ("192.0.2.1", 1234),
        "server": ("testserver", 80),
    }


def request_asgi(scope, chunks=()):
    """显式控制 receive：未提供的帧一旦被读取，测试立即失败。"""
    messages = iter(chunks)
    sent = []
    reads = []

    async def receive():
        message = next(messages, None)
        assert message is not None, "拒绝请求后仍在读取请求体"
        reads.append(message)
        return message

    async def send(message):
        sent.append(message)

    asyncio.run(admin.app(scope, receive, send))
    return sent[0]["status"], reads


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        *[
            f"/admin/{p}"
            for p in ("review", "fields", "kinds", "audit", "feedback", "sources", "embed.js")
        ],
    ],
)
def test_admin_security_headers(path):
    response = TestClient(admin.app).get(path)
    assert response.status_code == 200
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("path", ["/admin/", "/admin/missing"])
def test_admin_redirect_and_error_also_have_headers(path):
    response = TestClient(admin.app).get(path, follow_redirects=False)
    assert response.status_code in (307, 404)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize("declared", [False, True])
def test_missing_token_rejected_without_reading_body(method, declared, security_settings):
    headers = [(b"content-length", b"99999999")] if declared else []
    status, reads = request_asgi(scope_for("/api/admin/reviews/import", method, headers))
    assert status == 401 and not reads
    assert len(security_settings) == 1 and security_settings[0]["status"] == 401


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize(
    "path,limit",
    [("/api/admin/sources/trial", 256 * 1024), ("/api/admin/reviews/import", 5 * 1024 * 1024)],
)
def test_declared_oversize_does_not_read_body(path, limit, method):
    headers = [(b"x-admin-token", TOKEN.encode()), (b"content-length", str(limit + 1).encode())]
    status, reads = request_asgi(scope_for(path, method, headers))
    assert status == 413 and not reads


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize(
    "path,limit",
    [("/api/admin/sources/trial", 256 * 1024), ("/api/admin/reviews/import", 5 * 1024 * 1024)],
)
@pytest.mark.parametrize("declared", [False, True])
def test_stream_stops_at_first_oversize_chunk(path, limit, method, declared):
    headers = [(b"x-admin-token", TOKEN.encode())]
    headers += [(b"content-length", b"1")] if declared else [(b"transfer-encoding", b"chunked")]
    chunks = [
        {"type": "http.request", "body": b"x" * limit, "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": True},
    ]
    status, reads = request_asgi(scope_for(path, method, headers), chunks)
    assert status == 413 and len(reads) == 2


@pytest.mark.parametrize("size", [256 * 1024 + 1, 5 * 1024 * 1024])
@pytest.mark.parametrize("chunked", [False, True])
def test_import_within_five_mib_reaches_real_endpoint(session_factory, size, chunked):
    def session_override():
        with session_factory() as session:
            yield session

    body = b'{"version":1,"items":[]}'
    body += b" " * (size - len(body))
    content = iter((body[:100], body[100:])) if chunked else body
    admin.app.dependency_overrides[get_session] = session_override
    try:
        response = TestClient(admin.app).post(
            "/api/admin/reviews/import",
            headers={"X-Admin-Token": TOKEN, "Content-Type": "application/json"},
            content=content,
        )
    finally:
        admin.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["dryRun"] is True and response.json()["applied"] == 0


def test_regular_body_boundary_and_real_auth():
    # 无效 URL 在鉴权之后才返回 422；借此确认完整请求体已正确转交，且口令仍会校验。
    body = b'{"url":"invalid"}'
    body += b" " * (256 * 1024 - len(body))
    client = TestClient(admin.app)
    assert (
        client.post(
            "/api/admin/sources/trial", content=body, headers={"X-Admin-Token": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/admin/sources/trial", content=body, headers={"X-Admin-Token": TOKEN}
        ).status_code
        == 422
    )


def test_v1_bypasses_admin_headers_token_and_body_limits():
    response = TestClient(admin.app).post("/api/v1/missing", content=b"x" * (6 * 1024 * 1024))
    assert response.status_code == 404
    assert not SECURITY_HEADERS.keys() & response.headers.keys()


def test_api_role_still_hides_admin_without_reading_body(monkeypatch):
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(role="api"))
    status, reads = request_asgi(scope_for("/api/admin/reviews/import"))
    assert status == 404 and not reads


def auth(ip, token=TOKEN):
    scope = scope_for("/api/admin/overview", "GET")
    scope["client"] = (ip, 1234)
    admin._require_admin(Request(scope), token)


def test_full_throttle_retains_locks_and_evicts_oldest_unlocked(monkeypatch):
    monkeypatch.setattr(admin, "_MAX_SOURCES", 3)
    monkeypatch.setattr(admin.clock, "monotonic", lambda: 1000.0)
    admin._admin_fails.update(
        locked=deque([500.0] * 5),
        newer=deque([800.0]),
        oldest=deque([700.0]),
        expired=deque([399.0]),
        newest=deque([900.0]),
    )
    with pytest.raises(HTTPException) as caught:
        auth("locked")
    assert caught.value.status_code == 429
    assert set(admin._admin_fails) == {"locked", "newer", "newest"}
    for i in range(10):
        with pytest.raises(HTTPException) as caught:
            auth(f"attacker-{i}", "wrong")
        assert caught.value.status_code == 401
        assert len(admin._admin_fails) == 3
        with pytest.raises(HTTPException) as caught:
            auth("locked")
        assert caught.value.status_code == 429


def test_full_throttle_removes_expired_failures_before_deciding_lock(monkeypatch):
    monkeypatch.setattr(admin, "_MAX_SOURCES", 2)
    monkeypatch.setattr(admin.clock, "monotonic", lambda: 1000.0)
    admin._admin_fails.update(
        no_longer_locked=deque([399.0] * 4 + [500.0]),
        locked=deque([600.0] * 5),
        recent=deque([900.0]),
    )
    auth("valid-admin")
    assert set(admin._admin_fails) == {"locked", "recent"}


def test_when_all_sources_locked_only_oldest_is_evicted(monkeypatch):
    monkeypatch.setattr(admin, "_MAX_SOURCES", 2)
    monkeypatch.setattr(admin.clock, "monotonic", lambda: 1000.0)
    admin._admin_fails.update(
        newest=deque([900.0] * 5), oldest=deque([500.0] * 5), middle=deque([700.0] * 5)
    )
    auth("valid-admin")
    assert set(admin._admin_fails) == {"middle", "newest"}
