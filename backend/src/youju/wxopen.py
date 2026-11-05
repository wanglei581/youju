"""微信开放接口：云托管里经"开放接口服务"免 access_token；自建服务器自己带 access_token。

代码里调微信服务端接口一律写云托管的写法：http://api.weixin.qq.com/<接口>，不带 access_token
（虚拟支付 xpay.py、内容安全 wx_security.py、订阅消息 notify.py）。

- YOUJU_WX_OPEN_API=cloudrun（默认，现在的线上）：client() 和原来的 xpay.wx_http() 完全一样，
  post() 就是 httpx.post，请求由云托管的开放接口服务代填 access_token。
- YOUJU_WX_OPEN_API=token（2.0 自建服务器）：client() 在 http://api.weixin.qq.com 上挂
  OpenApiTransport：改成 https、加上 access_token 再发；微信说 access_token 失效
  （40001、40014、42001）时取一个新的重发一次。access_token 用 AppSecret 调 stable_token 取
  （官方推荐，和 getAccessToken 互相隔离；普通模式在有效期内重复调用拿到的是同一个，几个进程各自
  取不会互相顶掉），每个进程缓存到失效前 5 分钟。不用强制刷新模式（每天限 20 次）。
  取 token 要在小程序后台"开发管理 → 开发设置 → IP 白名单"里加服务器的公网 IP（否则 40164）。

日志：httpx 在 INFO 级别打完整网址，里面有 access_token（token 模式）、AppSecret 和登录凭证
（jscode2session），这些行一律丢掉；报错只写错误码，不写网址和返回内容。
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time as clock
from collections.abc import Callable
from typing import Any

import httpx

from youju.config import Settings, get_settings

OPEN_API = "http://api.weixin.qq.com"
TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
# 不带 access_token 的接口：取 token 本身；换 openid（用 AppSecret）
_NO_TOKEN = frozenset({"/cgi-bin/stable_token", "/sns/jscode2session"})
# access_token 失效：不合法、不是最新、已过期
EXPIRED_CODES = frozenset({40001, 40014, 42001})
# 离失效不到这么多秒就换新的（官方：普通模式下平台提前 5 分钟更新）
_REFRESH_BEFORE = 300
# 只检查不超过这么大的返回里的 errcode（开放接口的 JSON 返回都很小）
_CHECK_LIMIT = 64 * 1024

log = logging.getLogger("youju.wxopen")


class _SecretUrlFilter(logging.Filter):
    """丢掉网址里带 access_token、AppSecret、登录凭证的 httpx 日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(
            mark in message
            for mark in ("access_token=", "/sns/jscode2session", "/cgi-bin/stable_token")
        )


logging.getLogger("httpx").addFilter(_SecretUrlFilter())


class OpenApiError(Exception):
    """开放接口没调成。信息里只有错误码，不带网址、密钥和返回内容。"""

    def __init__(self, message: str, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


def token_mode(settings: Settings | None = None) -> bool:
    return getattr(settings or get_settings(), "wx_open_api", "cloudrun") == "token"


def _verify() -> ssl.SSLContext:
    # 和原来的 xpay.wx_http 一样用系统证书库：云托管开放接口服务接管 https 时用的是平台证书；
    # 自建服务器上系统证书库就是普通的公共根证书
    return ssl.create_default_context()


class TokenCache:
    """一个小程序的 access_token（每个进程一份）。线程安全；取不到抛 OpenApiError。"""

    def __init__(
        self,
        appid: str,
        secret: str,
        *,
        http: Callable[[], httpx.Client] | None = None,
        now: Callable[[], float] = clock.time,
    ) -> None:
        self._appid, self._secret = appid, secret
        self._http = http or (lambda: httpx.Client(trust_env=False, verify=_verify()))
        self._now = now
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, *, stale: str | None = None) -> str:
        """有效的 access_token。stale：刚被微信说失效的那个，缓存里还是它就重新取。"""
        with self._lock:
            now = self._now()
            if (
                self._token is not None
                and self._token != stale
                and now < self._expires_at - _REFRESH_BEFORE
            ):
                return self._token
            token, expires_in = self._fetch()
            self._token, self._expires_at = token, now + expires_in
            return token

    def _fetch(self) -> tuple[str, int]:
        body = {"grant_type": "client_credential", "appid": self._appid, "secret": self._secret}
        try:
            with self._http() as http:
                response = http.post(TOKEN_URL, json=body, timeout=10)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 只记类型：异常文字里可能带网址
            raise OpenApiError(f"取 access_token 失败：{type(exc).__name__}") from None
        if not isinstance(data, dict):
            raise OpenApiError("取 access_token 失败：返回格式不对")
        token, expires_in = data.get("access_token"), data.get("expires_in")
        if not isinstance(token, str) or not token or type(expires_in) is not int:
            code = data.get("errcode")
            code = code if type(code) is int else None
            raise OpenApiError(f"取 access_token 失败（errcode {code}）", code)
        return token, expires_in


_caches: dict[tuple[str, str], TokenCache] = {}
_caches_lock = threading.Lock()


def tokens(settings: Settings | None = None) -> TokenCache:
    settings = settings or get_settings()
    secret = settings.wx_appsecret.get_secret_value().strip() if settings.wx_appsecret else ""
    appid = settings.wx_appid.strip()
    if not (appid and secret):
        raise OpenApiError("没有配 YOUJU_WX_APPID、YOUJU_WX_APPSECRET，取不了 access_token")
    with _caches_lock:
        return _caches.setdefault((appid, secret), TokenCache(appid, secret))


def _errcode(response: httpx.Response) -> int | None:
    if len(response.content) > _CHECK_LIMIT:
        return None
    try:
        data = json.loads(response.content)
    except ValueError:
        return None
    code = data.get("errcode") if isinstance(data, dict) else None
    return code if type(code) is int else None


class OpenApiTransport(httpx.BaseTransport):
    """挂在 http://api.weixin.qq.com 上：改成 https、加 access_token 再发（见模块说明）。"""

    def __init__(self, cache: TokenCache, inner: httpx.BaseTransport) -> None:
        self._cache, self._inner = cache, inner

    def _send(self, request: httpx.Request, token: str | None) -> httpx.Response:
        url = request.url.copy_with(scheme="https", port=None)
        if token is not None:
            url = url.copy_set_param("access_token", token)
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() != "host"]
        outgoing = httpx.Request(
            request.method,
            url,
            headers=headers,
            content=request.read(),
            extensions=request.extensions,
        )
        response = self._inner.handle_request(outgoing)
        content = response.read()
        response.close()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
            extensions=response.extensions,
            request=request,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path in _NO_TOKEN:
            return self._send(request, None)
        try:
            token = self._cache.get()
        except OpenApiError as exc:
            # 当作网络错误往外抛：各处调用方本来就按"开放接口暂不可用"处理网络错误
            raise httpx.ConnectError(str(exc), request=request) from None
        response = self._send(request, token)
        if _errcode(response) in EXPIRED_CODES:
            try:
                fresh = self._cache.get(stale=token)
            except OpenApiError:
                return response
            if fresh != token:
                response = self._send(request, fresh)
        return response

    def close(self) -> None:
        self._inner.close()


def client(**kwargs: Any) -> httpx.Client:
    """调微信开放接口用的 HTTP 客户端（调用方负责关闭）。

    cloudrun：和原来的 xpay.wx_http() 一样（系统证书库）；token：挂上 OpenApiTransport。"""
    if not token_mode():
        return httpx.Client(trust_env=False, verify=_verify(), **kwargs)
    inner = httpx.HTTPTransport(verify=_verify(), trust_env=False)
    return httpx.Client(
        trust_env=False,
        verify=_verify(),
        mounts={OPEN_API: OpenApiTransport(tokens(), inner)},
        **kwargs,
    )


def post(url: str, **kwargs: Any) -> httpx.Response:
    """模块级 httpx.post 的替身：cloudrun 原样调 httpx.post（和原来一样）；token 走 client()。"""
    if not token_mode():
        return httpx.post(url, **kwargs)
    with client() as http:
        return http.post(url, **kwargs)


def available(settings: Settings | None = None) -> bool:
    """能不能真的调微信开放接口：云托管里（信任请求头的接口服务，或 admin 服务），
    或配了 token 模式。本机开发和测试都不是，不调（内容安全直接算通过、订阅消息只演练）。"""
    settings = settings or get_settings()
    token = getattr(settings, "wx_open_api", "cloudrun") == "token"
    if getattr(settings, "hosting", "cloudrun") == "self":
        return token
    return (
        token
        or bool(getattr(settings, "trust_wx_headers", False))
        or getattr(settings, "role", "all") == "admin"
    )


def code2session(code: str, settings: Settings | None = None) -> str:
    """用 wx.login 的 code 换 openid（自己的登录用，YOUJU_SESSION_LOGIN）。会话密钥不留。

    失败抛 OpenApiError：errcode 40029（code 无效或用过了）、40226（微信判定的高风险用户）、
    45011（太频繁）等原样带出，网络问题 errcode 为 None。"""
    settings = settings or get_settings()
    secret = settings.wx_appsecret.get_secret_value().strip() if settings.wx_appsecret else ""
    if not (settings.wx_appid.strip() and secret):
        raise OpenApiError("没有配 YOUJU_WX_APPID、YOUJU_WX_APPSECRET")
    params = {
        "appid": settings.wx_appid.strip(),
        "secret": secret,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    try:
        with httpx.Client(trust_env=False, verify=_verify()) as http:
            response = http.get(CODE2SESSION_URL, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenApiError(f"换 openid 失败：{type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise OpenApiError("换 openid 失败：返回格式不对")
    code_value = data.get("errcode", 0)
    if code_value not in (0, None):
        errcode = code_value if type(code_value) is int else None
        raise OpenApiError(f"换 openid 失败（errcode {errcode}）", errcode)
    openid = data.get("openid")
    if not isinstance(openid, str) or not openid.strip() or len(openid) > 128:
        raise OpenApiError("换 openid 失败：返回里没有 openid")
    return openid
