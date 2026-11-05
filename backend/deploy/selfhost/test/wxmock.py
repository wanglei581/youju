"""本机全栈测试用的假微信服务端（只在 deploy/selfhost/test/docker-compose.yml 里跑，线上不用）。

没有真的 AppSecret 也能把"wx.login → 换 openid → 登录凭证 → 带 access_token 调开放接口"整条路走通：
- POST /cgi-bin/stable_token：核对 appid、secret，发 mock-token；
- GET /sns/jscode2session：任何 code 都换成同一个测试用户（开发者工具里每次 wx.login 的 code 都不同）；
- 其余接口（内容安全、订阅消息、虚拟支付……）：要带 access_token=mock-token，否则回 40001；
  内容安全一律通过，别的回 errcode 0。每个请求打一行日志，测试时能看到带没带 access_token。
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APPID, SECRET, TOKEN, OPENID = "wxtest000000000000", "test-only-appsecret", "mock-token", "o-mock-devtools"


class Handler(BaseHTTPRequestHandler):
    def _reply(self, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        print(f"wxmock {self.command} {url.path} token={'yes' if query.get('access_token') else 'no'}")
        sys.stdout.flush()
        if url.path == "/cgi-bin/stable_token":
            if body.get("appid") != APPID or body.get("secret") != SECRET:
                return self._reply({"errcode": 40125, "errmsg": "invalid appsecret"})
            return self._reply({"access_token": TOKEN, "expires_in": 7200})
        if url.path == "/sns/jscode2session":
            if query.get("appid") != APPID or query.get("secret") != SECRET:
                return self._reply({"errcode": 40125, "errmsg": "invalid appsecret"})
            return self._reply({"openid": OPENID, "session_key": "bW9jay1zZXNzaW9uLWtleQ=="})
        if query.get("access_token") != TOKEN:
            return self._reply({"errcode": 40001, "errmsg": "invalid credential"})
        if url.path == "/wxa/msg_sec_check":
            return self._reply(
                {"errcode": 0, "errmsg": "ok", "result": {"suggest": "pass", "label": 100}}
            )
        return self._reply({"errcode": 0, "errmsg": "ok"})

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args) -> None:  # 上面已经打了一行
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
