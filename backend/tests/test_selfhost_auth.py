"""2.0 自建服务器：自己的登录凭证、自己取 access_token、配置的互斥规则（默认仍是云托管的做法）。"""

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from youju import session_auth, wxopen
from youju.api import auth as auth_api
from youju.api import me
from youju.api.app import app
from youju.config import Settings

SECRET = "s" * 40


def make_settings(**kw) -> SimpleNamespace:
    base = dict(
        session_secret=SecretStr(SECRET),
        session_ttl_hours=168,
        session_login=True,
        trust_wx_headers=False,
        dev_openid=None,
        wx_appid="wx0000000000000000",
        wx_appsecret=SecretStr("appsecret"),
        hosting="self",
        wx_open_api="token",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_defaults_keep_cloudrun_behaviour():
    s = Settings(_env_file=None)
    assert (s.hosting, s.session_login, s.wx_open_api, s.storage_backend, s.wx_push_mode) == (
        "cloudrun",
        False,
        "cloudrun",
        "cloudbase",
        "cloudrun",
    )
    assert s.client_ip_header == "x-original-forwarded-for"
    assert not s.public_replica and s.sync_secret is None
    assert not wxopen.token_mode(s)


def test_token_round_trip_expiry_and_tamper():
    s = make_settings()
    token, ttl = session_auth.issue("o-user", s, now=1000)
    assert ttl == 168 * 3600
    assert session_auth.verify(token, s, now=1001) == "o-user"
    # 到期
    assert session_auth.verify(token, s, now=1000 + ttl) is None
    # 看不出 openid（加密的）
    assert "o-user" not in token
    # 改一个字符就不认
    tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    assert session_auth.verify(tampered, s, now=1001) is None
    # 换了密钥，旧凭证都作废
    assert session_auth.verify(token, make_settings(session_secret=SecretStr("t" * 40))) is None
    for bad in ("", "ys1.", "xx." + token[4:], "ys1." + "A" * 500):
        assert session_auth.verify(bad, s) is None


def test_bearer_header_parsing():
    assert session_auth.bearer("Bearer abc") == "abc"
    assert session_auth.bearer("bearer   abc ") == "abc"
    assert session_auth.bearer("Basic abc") is None
    assert session_auth.bearer(None) is None


def test_rate_limit_per_source_and_memory_bound():
    limit = session_auth.RateLimit(per_minute=2, max_sources=3)
    assert limit.allow("a", now=0) and limit.allow("a", now=1)
    assert not limit.allow("a", now=2)
    assert limit.allow("a", now=62)
    for n in range(10):
        limit.allow(f"x{n}", now=63)
    assert len(limit._hits) <= 3


@pytest.fixture
def session_mode(monkeypatch):
    s = make_settings()
    monkeypatch.setattr(me, "get_settings", lambda: s)
    monkeypatch.setattr(auth_api, "get_settings", lambda: s)
    return s


def test_request_openid_uses_own_token_and_ignores_wx_headers(session_mode):
    token, _ = session_auth.issue("o-real", session_mode)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"})
    assert me.request_openid(request) == "o-real"
    # 自己的服务器在公网上：自带 X-WX-OPENID 一律不认
    spoof = SimpleNamespace(headers={"X-WX-SOURCE": "x", "X-WX-OPENID": "o-spoof"})
    with pytest.raises(Exception) as err:
        me.request_openid(spoof)
    assert getattr(err.value, "status_code", None) == 401


def test_login_endpoint_issues_token(session_mode, monkeypatch):
    monkeypatch.setattr(wxopen, "code2session", lambda code, settings: f"o-{code}")
    client = TestClient(app)
    res = client.post("/api/v1/auth/login", json={"code": "abc123"})
    assert res.status_code == 200
    body = res.json()
    assert body["expiresIn"] == 168 * 3600
    assert session_auth.verify(body["token"], session_mode) == "o-abc123"
    # 带上凭证访问 /me：认得这个用户（还没同意，所以 agreed 为空，也不建用户记录）
    me_res = client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me_res.status_code == 200 and me_res.json()["agreed"] is None
    assert client.get("/api/v1/me").status_code == 401


@pytest.mark.parametrize(
    ("errcode", "status"), [(40029, 401), (40226, 403), (45011, 503), (None, 503)]
)
def test_login_maps_wechat_errors(session_mode, monkeypatch, errcode, status):
    def fail(code, settings):
        raise wxopen.OpenApiError("x", errcode)

    monkeypatch.setattr(wxopen, "code2session", fail)
    assert TestClient(app).post("/api/v1/auth/login", json={"code": "c"}).status_code == status


def test_login_is_404_when_session_login_is_off(monkeypatch):
    monkeypatch.setattr(auth_api, "get_settings", lambda: make_settings(session_login=False))
    assert TestClient(app).post("/api/v1/auth/login", json={"code": "c"}).status_code == 404


def test_login_rejects_bad_code_format(session_mode):
    client = TestClient(app)
    assert client.post("/api/v1/auth/login", json={"code": "a b"}).status_code == 422
    assert client.post("/api/v1/auth/login", json={"code": "x" * 129}).status_code == 422


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"YOUJU_HOSTING": "self", "YOUJU_TRUST_WX_HEADERS": "true"}, "TRUST_WX_HEADERS"),
        ({"YOUJU_SESSION_LOGIN": "true"}, "SESSION_SECRET"),
        (
            {"YOUJU_SESSION_LOGIN": "true", "YOUJU_SESSION_SECRET": "k" * 40},
            "YOUJU_WX_APPSECRET",
        ),
        ({"YOUJU_WX_OPEN_API": "token"}, "YOUJU_WX_APPSECRET"),
        ({"YOUJU_ROLE": "api", "YOUJU_HOSTING": "self"}, "YOUJU_SESSION_LOGIN=true"),
        ({"YOUJU_STORAGE_BACKEND": "disk"}, "YOUJU_CLOUD_ENV"),
        ({"YOUJU_STORAGE_BACKEND": "disk", "YOUJU_CLOUD_ENV": "e"}, "YOUJU_STORAGE_DIR"),
        ({"YOUJU_STORAGE_BACKEND": "s3", "YOUJU_CLOUD_ENV": "e"}, "YOUJU_S3_ENDPOINT"),
        ({"YOUJU_HOSTING": "self", "YOUJU_CLOUD_ENV": "e"}, "disk 或 s3"),
        ({"YOUJU_WX_PUSH_MODE": "signed"}, "WX_PUSH_TOKEN"),
        ({"YOUJU_WX_PUSH_MODE": "signed", "YOUJU_WX_PUSH_TOKEN": "ab"}, "WX_PUSH_TOKEN"),
        (
            {
                "YOUJU_WX_PUSH_MODE": "signed",
                "YOUJU_WX_PUSH_TOKEN": "abc",
                "YOUJU_WX_PUSH_AES_KEY": "x",
            },
            "43",
        ),
        ({"YOUJU_SYNC_SECRET": "short"}, "SYNC_SECRET"),
        (
            {
                "YOUJU_HOSTING": "self",
                "YOUJU_SESSION_LOGIN": "true",
                "YOUJU_SESSION_SECRET": "k" * 40,
                "YOUJU_WX_APPID": "wx0000000000000000",
                "YOUJU_WX_APPSECRET": "s",
            },
            "YOUJU_WX_OPEN_API=token",
        ),
        (
            {"YOUJU_WX_PUSH_MODE": "signed", "YOUJU_WX_PUSH_TOKEN": "abc", "YOUJU_WX_APPID": "wx1"},
            "安全模式",
        ),
        ({"YOUJU_PUBLIC_REPLICA": "true"}, "HOSTING=self"),
        ({"YOUJU_OFFSITE_AGE_RECIPIENT": "not-a-key"}, "age1"),
    ],
)
def test_misconfiguration_refuses_to_start(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)
    assert message in str(err.value)


def test_complete_self_hosted_configuration_starts(monkeypatch, tmp_path):
    env = {
        "YOUJU_HOSTING": "self",
        "YOUJU_ROLE": "api",
        "YOUJU_SESSION_LOGIN": "true",
        "YOUJU_SESSION_SECRET": "k" * 40,
        "YOUJU_WX_APPID": "wx0000000000000000",
        "YOUJU_WX_APPSECRET": "secret",
        "YOUJU_WX_OPEN_API": "token",
        "YOUJU_STORAGE_BACKEND": "disk",
        "YOUJU_STORAGE_DIR": str(tmp_path),
        "YOUJU_CLOUD_ENV": "prod-env",
        "YOUJU_WX_PUSH_MODE": "signed",
        "YOUJU_WX_PUSH_TOKEN": "pushtoken",
        "YOUJU_WX_PUSH_AES_KEY": "A" * 43,
        "YOUJU_CLIENT_IP_HEADER": "x-forwarded-for",
        "YOUJU_SYNC_SECRET": "y" * 40,
        "YOUJU_PUBLIC_REPLICA": "true",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    s = Settings(_env_file=None)
    assert s.hosting == "self" and s.session_login and wxopen.available(s)


def test_secrets_are_not_read_from_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "YOUJU_SESSION_SECRET=" + "k" * 40 + "\nYOUJU_SYNC_SECRET=" + "y" * 40 + "\n",
        encoding="utf-8",
    )
    s = Settings(_env_file=dotenv)
    assert s.session_secret is None and s.sync_secret is None


# ---- 自己取 access_token ----


class FakeWx:
    """假的微信：stable_token 发 token-1、token-2……；别的接口记下请求，按 replies 回。"""

    def __init__(self):
        self.tokens = 0
        self.requests: list[httpx.Request] = []
        self.replies: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/cgi-bin/stable_token":
            body = json.loads(request.content)
            assert body["grant_type"] == "client_credential" and body["secret"] == "appsecret"
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": 7200}
            )
        reply = self.replies.pop(0) if self.replies else {"errcode": 0}
        return httpx.Response(200, json=reply)


def test_token_cache_reuses_and_refreshes():
    fake = FakeWx()
    now = [1000.0]
    cache = wxopen.TokenCache(
        "wx1",
        "appsecret",
        http=lambda: httpx.Client(transport=httpx.MockTransport(fake.handle)),
        now=lambda: now[0],
    )
    assert cache.get() == "token-1"
    assert cache.get() == "token-1"  # 有效期内不再取
    now[0] += 7200 - 299  # 离失效不到 5 分钟：换新的
    assert cache.get() == "token-2"
    assert cache.get(stale="token-2") == "token-3"  # 微信说失效了：重取


def test_token_errors_do_not_leak_secret():
    def handle(request):
        return httpx.Response(200, json={"errcode": 40164, "errmsg": "invalid ip 1.2.3.4"})

    cache = wxopen.TokenCache(
        "wx1", "appsecret", http=lambda: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(wxopen.OpenApiError) as err:
        cache.get()
    assert err.value.errcode == 40164 and "appsecret" not in str(err.value)


def test_transport_adds_access_token_and_retries_once_on_expired_token():
    fake = FakeWx()
    cache = wxopen.TokenCache(
        "wx1", "appsecret", http=lambda: httpx.Client(transport=httpx.MockTransport(fake.handle))
    )
    transport = wxopen.OpenApiTransport(cache, httpx.MockTransport(fake.handle))
    client = httpx.Client(mounts={wxopen.OPEN_API: transport})
    fake.replies = [{"errcode": 42001}, {"errcode": 0, "ok": 1}]
    res = client.post("http://api.weixin.qq.com/wxa/msg_sec_check", json={"content": "x"})
    assert res.json() == {"errcode": 0, "ok": 1}
    sent = [r for r in fake.requests if r.url.path == "/wxa/msg_sec_check"]
    assert [r.url.scheme for r in sent] == ["https", "https"]
    assert [r.url.params["access_token"] for r in sent] == ["token-1", "token-2"]
    assert json.loads(sent[1].content) == {"content": "x"}


def test_transport_keeps_xpay_pay_sig_and_body():
    fake = FakeWx()
    cache = wxopen.TokenCache(
        "wx1", "appsecret", http=lambda: httpx.Client(transport=httpx.MockTransport(fake.handle))
    )
    client = httpx.Client(
        mounts={wxopen.OPEN_API: wxopen.OpenApiTransport(cache, httpx.MockTransport(fake.handle))}
    )
    client.post(
        "http://api.weixin.qq.com/xpay/query_order", params={"pay_sig": "sig"}, content=b'{"a":1}'
    )
    sent = fake.requests[-1]
    assert sent.url.params["pay_sig"] == "sig" and sent.url.params["access_token"] == "token-1"
    assert sent.content == b'{"a":1}'


def test_client_in_cloudrun_mode_is_unchanged(monkeypatch):
    monkeypatch.setattr(wxopen, "get_settings", lambda: SimpleNamespace(wx_open_api="cloudrun"))
    with wxopen.client() as http:
        assert http.trust_env is False
        assert not any("api.weixin.qq.com" in str(k) for k in http._mounts)
    called = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: called.append(url) or "ok")
    assert wxopen.post("http://api.weixin.qq.com/x", json={}) == "ok"
    assert called == ["http://api.weixin.qq.com/x"]


def test_available_rules():
    assert not wxopen.available(SimpleNamespace(hosting="cloudrun", role="all"))
    assert wxopen.available(SimpleNamespace(hosting="cloudrun", trust_wx_headers=True))
    assert wxopen.available(SimpleNamespace(hosting="cloudrun", role="admin"))
    # 自建服务器：只看有没有自己取 token
    assert not wxopen.available(SimpleNamespace(hosting="self", role="admin"))
    assert wxopen.available(SimpleNamespace(hosting="self", wx_open_api="token"))


def test_code2session_maps_errors(monkeypatch):
    s = make_settings()

    def run(reply):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=reply))
        real = httpx.Client
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: real(
                transport=transport, **{k: v for k, v in kw.items() if k == "trust_env"}
            ),
        )
        try:
            return wxopen.code2session("code", s)
        finally:
            monkeypatch.setattr(httpx, "Client", real)

    assert run({"openid": "o-1", "session_key": "k"}) == "o-1"
    with pytest.raises(wxopen.OpenApiError) as err:
        run({"errcode": 40029, "errmsg": "invalid code"})
    assert err.value.errcode == 40029
    with pytest.raises(wxopen.OpenApiError):
        run({"session_key": "k"})


def test_secret_urls_are_dropped_from_httpx_logs():
    f = wxopen._SecretUrlFilter()

    def record(message):
        return SimpleNamespace(getMessage=lambda: message)

    assert not f.filter(record("HTTP Request: POST https://api.weixin.qq.com/x?access_token=abc"))
    assert not f.filter(record("GET https://api.weixin.qq.com/sns/jscode2session?secret=s"))
    assert f.filter(record("GET https://www.gov.cn/notice.html"))
    assert time.time() > 0
