"""消息推送收到普通 HTTPS 地址（YOUJU_WX_PUSH_MODE=signed）：验签、解密、加密回包。

算法的例子全部取自微信官方文档"消息推送"（2026-07-28 版，11/5 本机打开核对）：
Token=AAAAA，EncodingAESKey=43 个 A，AppID=wxba5fad812f8e6fb9。
"""

import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from youju import wx_push
from youju.api import pay as pay_api
from youju.api.app import app

TOKEN, AES, APPID = "AAAAA", "A" * 43, "wxba5fad812f8e6fb9"
ENCRYPTED = (
    "+qdx1OKCy+5JPCBFWw70tm0fJGb2Jmeia4FCB7kao+/Q5c/ohsOzQHi8khUOb05JCpj0JB4RvQMkUyus8TPxLKJ"
    "GQqcvZqzDpVzazhZv6JsXUnnR8XGT740XgXZUXQ7vJVnAG+tE8NUd4yFyjPy7GgiaviNrlCTj+l5kdfMuFUPpRSrf"
    "MZuMcp3Fn2Pede2IuQrKEYwKSqFIZoNqJ4M8EajAsjLY2km32IIjdf8YL/P50F7mStwntrA2cPDrM1kb6mOcfBgRtWy"
    "gb3VIYnSeOBrebufAlr7F9mFUPAJGj04="
)


def settings(aes: str | None = AES, **kw) -> SimpleNamespace:
    base = dict(
        wx_push_mode="signed",
        wx_push_token=SecretStr(TOKEN),
        wx_push_aes_key=SecretStr(aes) if aes else None,
        wx_appid=APPID,
        trust_wx_headers=False,
        wx_push_public=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_official_signature_examples():
    assert (
        wx_push.sha1_signature(TOKEN, "1714036504", "1514711492")
        == "f464b24fc39322e44b38aa78f5edd27bd1441696"
    )
    assert (
        wx_push.sha1_signature(TOKEN, "1714037059", "486452656")
        == "899cf89e464efb63f54ddac96b0a0a235f53aa78"
    )
    assert (
        wx_push.sha1_signature(TOKEN, "1714112445", "415670741", ENCRYPTED)
        == "046e02f8204d34f8ba5fa3b1db94908f3df2e9b3"
    )


def test_official_decrypt_and_encrypt_examples():
    message = json.loads(wx_push.decrypt(AES, ENCRYPTED, APPID))
    assert message["Event"] == "debug_demo" and message["debug_str"] == "hello world"
    sealed = wx_push.encrypt(AES, b'{"demo_resp":"good luck"}', APPID, random16=b"707722b803182950")
    assert sealed == (
        "ELGduP2YcVatjqIS+eZbp80MNLoAUWvzzyJxgGzxZO/5sAvd070Bs6qrLARC9nVHm48Y4hyRbtzve1L32tmxSQ=="
    )
    reply = json.loads(
        wx_push.seal_reply(
            settings(),
            b'{"demo_resp":"good luck"}',
            xml=False,
            nonce="415670741",
            timestamp=1713424427,
            random16=b"707722b803182950",
        )
    )
    assert reply == {
        "Encrypt": sealed,
        "MsgSignature": "1b9339964ed2e271e7c7b6ff2b0ef902fc94dea1",
        "TimeStamp": 1713424427,
        "Nonce": "415670741",
    }


def test_decrypt_rejects_other_appid_and_garbage():
    with pytest.raises(wx_push.PushRejected):
        wx_push.decrypt(AES, ENCRYPTED, "wx-someone-else")
    for bad in ("not base64!", "QUJD", ""):
        with pytest.raises(wx_push.PushRejected):
            wx_push.decrypt(AES, bad, APPID)


def test_open_push_modes():
    secure = {
        "timestamp": "1714112445",
        "nonce": "415670741",
        "encrypt_type": "aes",
        "msg_signature": "046e02f8204d34f8ba5fa3b1db94908f3df2e9b3",
    }
    body = json.dumps({"ToUserName": "gh", "Encrypt": ENCRYPTED}).encode()
    plain, is_secure = wx_push.open_push(settings(), secure, body)
    assert is_secure and json.loads(plain)["Event"] == "debug_demo"
    # 不能拿 signature 代替 msg_signature
    with pytest.raises(wx_push.PushRejected):
        wx_push.open_push(settings(), {**secure, "msg_signature": "0" * 40}, body)
    # XML 包体
    xml = f"<xml><ToUserName>gh</ToUserName><Encrypt><![CDATA[{ENCRYPTED}]]></Encrypt></xml>"
    assert wx_push.open_push(settings(), secure, xml.encode())[1]
    with pytest.raises(wx_push.PushRejected):
        wx_push.open_push(settings(), secure, b"<!DOCTYPE x><xml><Encrypt>a</Encrypt></xml>")
    # 明文模式：核对 signature
    plain_query = {
        "timestamp": "1714037059",
        "nonce": "486452656",
        "signature": "899cf89e464efb63f54ddac96b0a0a235f53aa78",
    }
    assert wx_push.open_push(settings(aes=None), plain_query, b"{}") == (b"{}", False)
    with pytest.raises(wx_push.PushRejected):
        wx_push.open_push(settings(aes=None), {**plain_query, "nonce": "1"}, b"{}")
    # 配了 EncodingAESKey（安全模式）就不收明文
    with pytest.raises(wx_push.PushRejected):
        wx_push.open_push(settings(), plain_query, b"{}")


def test_seal_reply_leaves_success_alone():
    assert wx_push.seal_reply(settings(), b"success", xml=False, nonce="1") == b"success"
    assert wx_push.seal_reply(settings(), b"", xml=True, nonce="1") == b""
    xml = wx_push.seal_reply(
        settings(), b"<xml><ErrCode>0</ErrCode></xml>", xml=True, nonce="9", timestamp=5
    )
    root = ElementTree.fromstring(xml)
    encrypted = root.findtext("Encrypt")
    assert root.findtext("Nonce") == "9" and root.findtext("TimeStamp") == "5"
    assert root.findtext("MsgSignature") == wx_push.sha1_signature(TOKEN, "5", "9", encrypted)
    assert wx_push.decrypt(AES, encrypted, APPID) == b"<xml><ErrCode>0</ErrCode></xml>"


@pytest.fixture
def signed(monkeypatch):
    s = settings()
    monkeypatch.setattr(pay_api, "get_settings", lambda: s)
    monkeypatch.setattr(wx_push, "open_push", wx_push.open_push)
    return s


def test_url_verification_get(signed):
    client = TestClient(app)
    query = {
        "signature": "f464b24fc39322e44b38aa78f5edd27bd1441696",
        "timestamp": "1714036504",
        "nonce": "1514711492",
        "echostr": "4375120948345356249",
    }
    res = client.get("/api/wx/push", params=query)
    assert res.status_code == 200 and res.text == "4375120948345356249"
    assert client.get("/api/wx/push", params={**query, "nonce": "2"}).status_code == 403


def test_url_verification_is_404_in_cloudrun_mode(monkeypatch):
    monkeypatch.setattr(pay_api, "get_settings", lambda: settings(wx_push_mode="cloudrun"))
    assert TestClient(app).get("/api/wx/push", params={"echostr": "1"}).status_code == 404


def test_secure_push_gets_encrypted_reply(signed):
    """配置检测这类非支付事件：照旧应答成功；安全模式下回包按官方算法加密。"""
    client = TestClient(app)
    body = json.dumps({"ToUserName": "gh", "Encrypt": ENCRYPTED})
    query = {
        "signature": "x",
        "timestamp": "1714112445",
        "nonce": "415670741",
        "encrypt_type": "aes",
        "msg_signature": "046e02f8204d34f8ba5fa3b1db94908f3df2e9b3",
    }
    res = client.post("/api/wx/push", params=query, content=body)
    assert res.status_code == 200
    reply = res.json()
    assert reply["Nonce"] == "415670741"
    assert reply["MsgSignature"] == wx_push.sha1_signature(
        TOKEN, str(reply["TimeStamp"]), "415670741", reply["Encrypt"]
    )
    assert json.loads(wx_push.decrypt(AES, reply["Encrypt"], APPID)) == {
        "ErrCode": 0,
        "ErrMsg": "success",
    }
    # 签名不对、伪造来源头都不认
    assert (
        client.post("/api/wx/push", params={**query, "nonce": "1"}, content=body).status_code == 403
    )
    assert (
        client.post(
            "/api/wx/push", headers={"X-WX-SOURCE": "x"}, content=json.dumps({"Event": "e"})
        ).status_code
        == 403
    )
