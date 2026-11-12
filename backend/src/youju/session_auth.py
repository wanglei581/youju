"""自己的登录凭证（YOUJU_SESSION_LOGIN，2.0 自建服务器）。

云托管里每个请求都带平台注入的 openid；自己的服务器在公网上，请求头谁都能伪造，所以：
小程序 wx.login 拿 code → POST /api/v1/auth/login → 后端用 AppSecret 调 jscode2session 换 openid →
发一个我们签发的登录凭证，之后每个请求带 Authorization: Bearer <凭证>。

- 凭证是加密的（AES-256-GCM，密钥由 YOUJU_SESSION_SECRET 派生）：里面只有 openid 和到期时间，
  拿到凭证看不出 openid，改不了内容。不写数据库：同意协议之前仍然不保存 openid（隐私保护指引一·1）。
- 到期（默认 7 天）、换了密钥、格式不对都当作没登录：接口回 401，小程序自动重新登录一次再发。
- 微信的会话密钥（session_key）不留：付款时要用的那一次由付款接口另外换（payments、xpay）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time as clock
from collections import deque

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from youju.config import Settings

PREFIX = "ys1."
_NONCE = 12
# 凭证最长多少字符（再长的一律不认，免得拿超长的串来耗 CPU）
MAX_TOKEN = 400


def _key(settings: Settings) -> bytes:
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    if len(secret.strip()) < 32:
        raise ValueError("YOUJU_SESSION_SECRET 没有配好")
    return hashlib.sha256(b"youju-session-v1\0" + secret.strip().encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(openid: str, settings: Settings, *, now: float | None = None) -> tuple[str, int]:
    """签发登录凭证，返回（凭证, 有效秒数）。"""
    now = clock.time() if now is None else now
    ttl = settings.session_ttl_hours * 3600
    payload = json.dumps({"o": openid, "e": int(now) + ttl}, separators=(",", ":")).encode()
    nonce = os.urandom(_NONCE)
    sealed = AESGCM(_key(settings)).encrypt(nonce, payload, PREFIX.encode())
    return PREFIX + _b64(nonce + sealed), ttl


def verify(token: str, settings: Settings, *, now: float | None = None) -> str | None:
    """凭证有效就返回 openid；过期、被改过、换了密钥、格式不对都返回 None。"""
    if not token or len(token) > MAX_TOKEN or not token.startswith(PREFIX):
        return None
    try:
        raw = _unb64(token[len(PREFIX) :])
        if len(raw) <= _NONCE + 16:
            return None
        payload = AESGCM(_key(settings)).decrypt(raw[:_NONCE], raw[_NONCE:], PREFIX.encode())
        data = json.loads(payload)
    except (ValueError, InvalidTag):
        return None
    if not isinstance(data, dict):
        return None
    openid, expires = data.get("o"), data.get("e")
    now = clock.time() if now is None else now
    if not isinstance(openid, str) or not openid.strip() or len(openid) > 128:
        return None
    if type(expires) is not int or expires <= now:
        return None
    return openid


def bearer(header: str | None) -> str | None:
    """Authorization 头里的凭证；不是 Bearer 的返回 None。"""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


class RateLimit:
    """同一来源地址每分钟最多登录几次（进程内计数）：jscode2session 有频率限制，
    也不让人拿接口刷微信。"""

    def __init__(self, per_minute: int = 30, max_sources: int = 10000) -> None:
        self.per_minute, self.max_sources = per_minute, max_sources
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, source: str, now: float | None = None) -> bool:
        now = clock.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(source, deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            if len(self._hits) > self.max_sources:
                for key in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                    del self._hits[key]
                # 一分钟内来源太多（被人换着地址刷）：先清最早记下的，内存有上限
                while len(self._hits) > self.max_sources:
                    del self._hits[next(iter(self._hits))]
            return True
