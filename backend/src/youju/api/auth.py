"""登录（只在 YOUJU_SESSION_LOGIN=true 的自建服务器上开放，见 youju/session_auth.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from youju import session_auth, wxopen
from youju.config import get_settings

router = APIRouter(prefix="/api/v1/auth", tags=["登录"])
_limit = session_auth.RateLimit()


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=128, pattern=r"^[0-9A-Za-z_\-]+$")


@router.post("/login")
def login(body: LoginIn, request: Request) -> dict:
    """用 wx.login 的 code 换登录凭证。不建用户记录、不保存 openid（同意协议后才建）。"""
    settings = get_settings()
    if not settings.session_login:
        raise HTTPException(status_code=404, detail="没有找到接口")
    from youju.api.app import client_ip

    if not _limit.allow(client_ip(request) or "?"):
        raise HTTPException(status_code=429, detail="登录太频繁，请稍后再试")
    try:
        openid = wxopen.code2session(body.code, settings)
    except wxopen.OpenApiError as exc:
        if exc.errcode == 40029:
            # code 无效或已用过：小程序重新 wx.login 再来
            raise HTTPException(status_code=401, detail="登录凭证已失效，请重试") from None
        if exc.errcode == 40226:
            raise HTTPException(status_code=403, detail="微信暂时限制了这个账号登录") from None
        raise HTTPException(status_code=503, detail="登录服务暂时不可用，请稍后再试") from None
    token, ttl = session_auth.issue(openid, settings)
    return {"token": token, "expiresIn": ttl}
