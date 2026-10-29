"""微信虚拟支付传输适配器：签名和发送共用同一份 UTF-8 字节。

官方算法及示例：virtual-payment.html（2026-10-04 核对）。
不传播 HTTP 异常，避免其中的 secret / pay_sig 进入异常和日志。
"""

import hashlib
import hmac
import json
import logging
import ssl
from datetime import date

import httpx

from youju.config import Settings

logger = logging.getLogger(__name__)

# 新订单一律现网（env 0，真扣钱）。沙箱已去掉（创始人 10/29："支付那一个沙箱支付去掉"）：
# 苹果支付只支持现网（官方文档 2026-09-14 版），安卓沙箱真机付款也真实扣钱（10/20 实测），
# 留着只会让人看错。
# 去掉沙箱以前下的测试单仍按订单上记的 env 查单、退款（appkey 按 env 取）。
XPAY_ENV = 0


def wx_http() -> httpx.Client:
    """调用微信接口用的 HTTP 客户端，用系统证书库校验 https。

    云托管的开放接口服务接管了容器里到 api.weixin.qq.com 的连接，https 用的是平台自己签发、
    装在系统证书库里的证书；httpx 默认只认 certifi 自带的那份，会报 self-signed certificate，
    配了 AppSecret 的换会话密钥（方式 B）因此一直失败（10/19 沙箱下单 503）。仍然校验证书和域名，
    只是信任的根证书改从系统证书库取。"""
    return httpx.Client(trust_env=False, verify=ssl.create_default_context())


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
        # 方式 A（默认）：云托管开放接口服务，不带 secret，走容器内 http 地址。
        # 方式 B：配置了 AppSecret，直接调用公网 https 接口，secret 不能明文经过 http。
        params = {"js_code": code, "grant_type": "authorization_code"}
        url = "http://api.weixin.qq.com/sns/jscode2session"
        if self.settings.wx_appid:
            params["appid"] = self.settings.wx_appid
        if self.settings.wx_appsecret:
            if not self.settings.wx_appid:
                raise XPayError
            params.update(
                appid=self.settings.wx_appid, secret=self.settings.wx_appsecret.get_secret_value()
            )
            url = "https://api.weixin.qq.com/sns/jscode2session"
        try:
            response = self.http.get(url, params=params, timeout=5)
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            # 只记异常类型：异常文字里可能带 URL，URL 里有 AppSecret 和登录凭证
            logger.warning("换会话密钥失败：%s", type(exc).__name__)
            raise XPayError from None
        # 失败原因只记错误码这类不敏感的信息，不记登录凭证、会话密钥和返回内容
        if not isinstance(result, dict):
            reason = "返回格式不对"
        elif result.get("errcode", 0) != 0:
            reason = f"errcode={result.get('errcode')}"
        elif result.get("openid") != openid:
            reason = "openid 对不上"
        elif not isinstance(result.get("session_key"), str) or not result["session_key"]:
            reason = "没有会话密钥"
        else:
            return result["session_key"]
        logger.warning("换会话密钥失败：%s", reason)
        raise XPayError

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
        return self.call("/xpay/download_bill", {"begin_ds": ds, "end_ds": ds}, env=XPAY_ENV)
