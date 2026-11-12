"""本机全栈冒烟测试：对着 deploy/selfhost/test/docker-compose.yml 起来的整套跑一遍主路径。

用法（整套起来以后，在宿主机上）：python3 smoke.py [http://127.0.0.1:18080]
只用标准库。每一步打一行，出错就退出 1。用的密钥都是 docker-compose.yml 里写死的测试值。
"""

import base64
import hashlib
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18080").rstrip("/")
PUSH_TOKEN, AES_KEY, APPID = "testpushtoken", "A" * 43, "wxtest000000000000"


def call(method, path, body=None, token=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def check(name, ok, detail=""):
    print(("通过 " if ok else "失败 ") + name + (f"：{detail}" if detail and not ok else ""))
    if not ok:
        sys.exit(1)


def main():
    for _ in range(60):
        status, _ = call("GET", "/health")
        if status == 200:
            break
        time.sleep(2)
    check("服务器 /health", status == 200)

    status, body = call("GET", "/api/v1/me")
    check("没登录访问“我的”回 401", status == 401, body[:100])
    status, body = call("GET", "/api/v1/home")
    check("不登录也能看首页", status == 200, body[:100])

    status, body = call("POST", "/api/v1/auth/login", {"code": "smoke-code"})
    check("登录（假微信换 openid）", status == 200, body[:200])
    token = json.loads(body)["token"]
    status, body = call("GET", "/api/v1/me", token=token)
    check("带凭证访问“我的”", status == 200 and json.loads(body)["agreed"] is None, body[:200])
    status, _ = call("GET", "/api/v1/me", token=token + "x")
    check("凭证被改过就不认", status == 401)
    status, _ = call("GET", "/api/v1/me", headers={"X-WX-SOURCE": "x", "X-WX-OPENID": "spoof"})
    check("自带 X-WX-OPENID 不认", status == 401)

    status, body = call("POST", "/api/v1/me/agreement", {"version": "2026-12"}, token=token)
    check("同意协议", status == 200, body[:200])
    status, body = call("POST", "/api/v1/feedback", {"content": "冒烟测试：日期不对"}, token=token)
    check("纠错（内容安全经 access_token 调假微信）", status == 201, body[:200])

    status, body = call(
        "POST", "/api/admin/takedown/1/remove", {}, headers={"x-admin-token": "x" * 40}
    )
    check("对外的域名上没有核对台", status == 404, body[:100])

    # 消息推送：安全模式（按官方算法加密、签名）
    message = json.dumps({"ToUserName": "gh", "MsgType": "event", "Event": "debug_demo"}).encode()
    encrypted = _encrypt(message)
    stamp, nonce = str(int(time.time())), "12345"
    sig = hashlib.sha1("".join(sorted([PUSH_TOKEN, stamp, nonce, encrypted])).encode()).hexdigest()
    query = f"?timestamp={stamp}&nonce={nonce}&encrypt_type=aes&msg_signature={sig}&signature=x"
    status, body = call("POST", "/api/wx/push" + query, {"ToUserName": "gh", "Encrypt": encrypted})
    reply = json.loads(body) if status == 200 else {}
    expect = hashlib.sha1(
        "".join(
            sorted([PUSH_TOKEN, str(reply.get("TimeStamp")), nonce, str(reply.get("Encrypt"))])
        ).encode()
    ).hexdigest()
    check(
        "消息推送验签、解密，回包加密且签名对",
        status == 200 and reply.get("MsgSignature") == expect,
        body[:200],
    )
    status, _ = call("POST", "/api/wx/push" + query.replace(nonce, "1"), {"Encrypt": encrypted})
    check("推送签名不对就拒", status == 403)
    print("全部通过")


def _encrypt(message: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = base64.b64decode(AES_KEY + "=")
    full = os.urandom(16) + struct.pack(">I", len(message)) + message + APPID.encode()
    pad = 32 - len(full) % 32
    full += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(enc.update(full) + enc.finalize()).decode()


if __name__ == "__main__":
    main()
