"""消息推送收到普通 HTTPS 地址（YOUJU_WX_PUSH_MODE=signed，2.0 自建服务器）：验签、解密、加密回包。

云托管的消息推送由平台转进容器，带 X-WX-SOURCE 就可信；自己的服务器在公网上，要按小程序后台
"开发管理 → 消息推送"填的 Token、EncodingAESKey 自己核对。算法照官方文档（消息推送，2026-07-28 版，
11/5 本机打开核对；测试用的是文档里的例子）：

- 配置时微信发 GET（signature、timestamp、nonce、echostr）：sha1(字典序排好的 Token、timestamp、
  nonce 连起来) 等于 signature 就原样回 echostr。
- 明文模式的 POST：同样核对 signature。不推荐（文档："安全系数较低"）；配了 EncodingAESKey 时
  只收密文，明文推送一律拒绝。
- 安全模式的 POST（encrypt_type=aes）：核对 msg_signature = sha1(字典序的 Token、timestamp、nonce、
  包体里的 Encrypt)，不看 signature；再解密 Encrypt：AESKey = Base64(EncodingAESKey + "=")，
  AES-256-CBC、IV 是密钥前 16 字节、PKCS#7 按 32 字节补齐；明文 = 16 字节随机串 + 4 字节长度（网络
  字节序）+ 消息 + AppID，AppID 要和本小程序的一致。
- 安全模式的回包：空串或 success 不用加密，其他（虚拟支付要回 ErrCode，iOS 退款问询还带结果，
  客服消息回"转客服"）要按同样的算法加密，回 Encrypt、MsgSignature、TimeStamp、Nonce
  （Nonce 回填请求里的 nonce）。

不查时间戳新旧：支付推送只是"去查单"的信号，发货、退款都以向微信查单的结果为准（api/pay.py），
重放一条旧推送改不了订单。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time as clock
from collections.abc import Mapping
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from youju.config import Settings

_BLOCK = 32  # 微信的 PKCS#7 按 32 字节补齐（AES 块是 16 字节，32 是它的整数倍）


class PushRejected(Exception):
    """验签、解密没通过；信息里不带签名、密文和密钥。"""


def sha1_signature(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(parts)).encode()).hexdigest()


def _same(expected: str, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(expected, given)


def _aes_key(encoding_aes_key: str) -> bytes:
    key = base64.b64decode(encoding_aes_key + "=")
    if len(key) != 32:
        raise PushRejected("EncodingAESKey 不对")
    return key


def decrypt(encoding_aes_key: str, encrypted: str, appid: str) -> bytes:
    """解出推送的明文消息；AppID 对不上、补齐或长度不对都抛 PushRejected。"""
    key = _aes_key(encoding_aes_key)
    try:
        data = base64.b64decode(encrypted, validate=True)
    except ValueError:
        raise PushRejected("密文不是 Base64") from None
    if not data or len(data) % 16:
        raise PushRejected("密文长度不对")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    if not 1 <= pad <= _BLOCK or plain[-pad:] != bytes([pad]) * pad:
        raise PushRejected("补齐不对")
    plain = plain[:-pad]
    if len(plain) < 20:
        raise PushRejected("明文太短")
    (size,) = struct.unpack(">I", plain[16:20])
    message, tail = plain[20 : 20 + size], plain[20 + size :]
    if len(message) != size or tail.decode("utf-8", "replace") != appid:
        raise PushRejected("不是本小程序的推送")
    return message


def encrypt(
    encoding_aes_key: str, message: bytes, appid: str, *, random16: bytes | None = None
) -> str:
    key = _aes_key(encoding_aes_key)
    nonce = random16 if random16 is not None else os.urandom(16)
    full = nonce + struct.pack(">I", len(message)) + message + appid.encode()
    pad = _BLOCK - len(full) % _BLOCK
    full += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(full) + encryptor.finalize()).decode()


def _secret(value) -> str:
    return value.get_secret_value().strip() if value else ""


def _envelope_encrypt(raw: bytes) -> str:
    """安全模式包体里的 Encrypt 字段（JSON 或 XML）。"""
    text = raw.lstrip()
    if text.startswith(b"<"):
        upper = raw.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise PushRejected("推送格式无效")
        try:
            node = ElementTree.fromstring(raw).find("Encrypt")
        except ElementTree.ParseError:
            raise PushRejected("推送格式无效") from None
        value = node.text if node is not None else None
    else:
        try:
            data = json.loads(raw)
        except ValueError:
            raise PushRejected("推送格式无效") from None
        value = data.get("Encrypt") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise PushRejected("推送里没有密文")
    return value.strip()


def verify_url(settings: Settings, query: Mapping[str, str]) -> str:
    """配置推送地址时的 GET：验签通过返回 echostr。"""
    token = _secret(settings.wx_push_token)
    expected = sha1_signature(token, query.get("timestamp", ""), query.get("nonce", ""))
    echostr = query.get("echostr", "")
    if not token or not _same(expected, query.get("signature")) or not echostr:
        raise PushRejected("签名不对")
    return echostr


def open_push(settings: Settings, query: Mapping[str, str], raw: bytes) -> tuple[bytes, bool]:
    """核对一条 POST 推送，返回（明文包体, 是不是安全模式）。"""
    token = _secret(settings.wx_push_token)
    aes = _secret(settings.wx_push_aes_key)
    if not token:
        raise PushRejected("没有配推送 Token")
    timestamp, nonce = query.get("timestamp", ""), query.get("nonce", "")
    if query.get("encrypt_type") == "aes":
        if not aes:
            raise PushRejected("没有配 EncodingAESKey，收不了密文")
        encrypted = _envelope_encrypt(raw)
        if not _same(
            sha1_signature(token, timestamp, nonce, encrypted), query.get("msg_signature")
        ):
            raise PushRejected("签名不对")
        return decrypt(aes, encrypted, settings.wx_appid.strip()), True
    if aes:
        # 后台选的是安全模式：明文推送不是微信发的
        raise PushRejected("安全模式只收密文")
    if not _same(sha1_signature(token, timestamp, nonce), query.get("signature")):
        raise PushRejected("签名不对")
    return raw, False


def seal_reply(
    settings: Settings,
    body: bytes,
    *,
    xml: bool,
    nonce: str,
    timestamp: int | None = None,
    random16: bytes | None = None,
) -> bytes:
    """安全模式的回包：空串、success 原样回；其他加密后按 JSON 或 XML 包好。"""
    if body.strip() in (b"", b"success"):
        return body
    token = _secret(settings.wx_push_token)
    aes = _secret(settings.wx_push_aes_key)
    stamp = int(clock.time()) if timestamp is None else timestamp
    encrypted = encrypt(aes, body, settings.wx_appid.strip(), random16=random16)
    # 随机数只留字母数字（XML 回包放在 CDATA 里），签名和回包用同一个：微信按回包里的 Nonce 验签
    safe_nonce = re.sub(r"[^0-9A-Za-z]", "", nonce)
    signature = sha1_signature(token, str(stamp), safe_nonce, encrypted)
    if not xml:
        return json.dumps(
            {
                "Encrypt": encrypted,
                "MsgSignature": signature,
                "TimeStamp": stamp,
                "Nonce": safe_nonce,
            },
            separators=(",", ":"),
        ).encode()
    # 照官方示例的写法：字符串放在 CDATA 里。密文是 Base64、签名是十六进制
    return (
        f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{stamp}</TimeStamp>"
        f"<Nonce><![CDATA[{safe_nonce}]]></Nonce></xml>"
    ).encode()
