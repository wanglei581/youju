"""微信虚拟支付传输适配器：签名和发送共用同一份 UTF-8 字节。

官方算法及示例：virtual-payment.html（2026-10-04 核对）。
不传播 HTTP 异常，避免其中的 secret / pay_sig 进入异常和日志。
"""

import hashlib
import hmac
import json
import logging
from datetime import date

import httpx

from youju.config import Settings


class _PaymentLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "/xpay/" not in message and "/sns/jscode2session" not in message


# httpx 在 INFO 级别输出完整 URL；这些 URL 含 pay_sig 或 AppSecret，必须丢弃。
logging.getLogger("httpx").addFilter(_PaymentLogFilter())


class XPayError(Exception):
    def __init__(self, code: int | None = None):
        super().__init__("微信支付暂不可用")
        self.code = code


def encode(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def signature(key: str, data: str) -> str:
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def pay_signature(key: str, uri: str, data: str) -> str:
    return signature(key, uri + "&" + data)


class XPayClient:
    def __init__(self, settings: Settings, http: httpx.Client):
        self.settings, self.http = settings, http

    def appkey(self, env: int) -> str:
        secret = self.settings.xpay_appkey_prod if env == 0 else self.settings.xpay_appkey_sandbox
        if not secret or not secret.get_secret_value():
            raise XPayError
        return secret.get_secret_value()

    def session_key(self, code: str, openid: str) -> str:
        # 获取方式可通过替换此方法切换；默认开放接口服务，有 secret 时使用 B。
        params = {"js_code": code, "grant_type": "authorization_code"}
        if self.settings.wx_appid:
            params["appid"] = self.settings.wx_appid
        if self.settings.wx_appsecret:
            if not self.settings.wx_appid:
                raise XPayError
            params.update(
                appid=self.settings.wx_appid, secret=self.settings.wx_appsecret.get_secret_value()
            )
        try:
            response = self.http.get(
                "http://api.weixin.qq.com/sns/jscode2session", params=params, timeout=5
            )
            response.raise_for_status()
            result = response.json()
            if (
                result.get("errcode", 0) != 0
                or result.get("openid") != openid
                or not isinstance(result.get("session_key"), str)
                or not result["session_key"]
            ):
                raise XPayError
            return result["session_key"]
        except Exception:
            raise XPayError from None

    def call(self, uri: str, data: dict, *, env: int) -> dict:
        raw = encode(data)
        sig = pay_signature(self.appkey(env), uri, raw)
        try:
            response = self.http.post(
                "http://api.weixin.qq.com" + uri,
                params={"pay_sig": sig},
                content=raw.encode(),
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise XPayError
            if result.get("errcode") != 0:
                raise XPayError(result.get("errcode"))
            return result
        except XPayError:
            raise
        except Exception:
            raise XPayError from None

    def query(self, openid: str, trade: str, env: int) -> dict:
        result = self.call(
            "/xpay/query_order", {"openid": openid, "env": env, "order_id": trade}, env=env
        )
        if not isinstance(result.get("order"), dict):
            raise XPayError
        return result["order"]

    def refund(self, data: dict) -> dict:
        return self.call("/xpay/refund_order", data, env=data["env"])

    def provide(self, trade: str, env: int) -> dict:
        return self.call("/xpay/notify_provide_goods", {"order_id": trade, "env": env}, env=env)

    def bill(self, day: date) -> dict:
        ds = int(day.strftime("%Y%m%d"))
        return self.call(
            "/xpay/download_bill", {"begin_ds": ds, "end_ds": ds}, env=self.settings.xpay_env
        )
