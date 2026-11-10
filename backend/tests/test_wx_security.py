"""微信文本内容安全（第九节第 5、6 条）。不发真的请求：替换 httpx.post。"""

from types import SimpleNamespace

import httpx
import pytest

from youju import wx_security
from youju.wx_security import CheckDown, Verdict, check_feedback, check_text

URL = "http://api.weixin.qq.com/wxa/msg_sec_check"


class Response:
    def __init__(self, body=None, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            request = httpx.Request("POST", URL)
            raise httpx.HTTPStatusError(
                "error", request=request, response=httpx.Response(self.status, request=request)
            )

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def result(suggest="pass", label=100, errcode=0):
    return {"errcode": errcode, "errmsg": "ok", "result": {"suggest": suggest, "label": label}}


@pytest.fixture
def calls(monkeypatch):
    """记下每次请求；replies 里按顺序放每次的返回（Response 或要抛的异常），默认都通过。"""
    sent = SimpleNamespace(requests=[], replies=[])

    def post(url, *, json, timeout):
        sent.requests.append({"url": url, "json": json, "timeout": timeout})
        reply = sent.replies.pop(0) if sent.replies else Response(result())
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(wx_security.httpx, "post", post)
    return sent


def cloud(monkeypatch, *, trust=True, role="api"):
    settings = SimpleNamespace(trust_wx_headers=trust, role=role)
    monkeypatch.setattr(wx_security, "get_settings", lambda: settings)


def test_pass(calls, monkeypatch):
    cloud(monkeypatch)
    assert check_text("wx", "专精特新申报什么时候截止") == Verdict(True, "pass", 100)
    assert calls.requests == [
        {
            "url": URL,
            "json": {
                "openid": "wx",
                "version": 2,
                "scene": 2,
                "content": "专精特新申报什么时候截止",
            },
            "timeout": 5.0,
        }
    ]


def test_scene_and_timeout_passed_through(calls, monkeypatch):
    cloud(monkeypatch)
    check_text("wx", "问题", scene=4, timeout=2.5)
    assert calls.requests[0]["json"]["scene"] == 4
    assert calls.requests[0]["timeout"] == 2.5


@pytest.mark.parametrize(("suggest", "label"), [("risky", 20001), ("review", 21000)])
def test_risky_and_review_are_blocked(calls, monkeypatch, suggest, label):
    cloud(monkeypatch)
    calls.replies.append(Response(result(suggest, label)))
    assert check_text("wx", "测试内容") == Verdict(False, suggest, label)


def test_label_not_a_number_kept_as_none(calls, monkeypatch):
    cloud(monkeypatch)
    calls.replies.append(Response(result("risky", "20001")))
    assert check_text("wx", "测试内容") == Verdict(False, "risky", None)


def test_errcode_is_check_down(calls, monkeypatch):
    cloud(monkeypatch)
    calls.replies.append(Response(result(errcode=61010)))
    with pytest.raises(CheckDown) as error:
        check_text("wx", "不能出现在报错里的提问")
    assert error.value.errcode == 61010
    assert "不能出现在报错里的提问" not in str(error.value)


@pytest.mark.parametrize(
    "reply",
    [
        httpx.TimeoutException("timeout"),
        httpx.ConnectError("offline"),
        Response(result(), status=500),
        Response(ValueError("not json")),
        Response(["not", "a", "dict"]),
        Response({"errcode": 0}),  # 没有 result
        Response({"errcode": 0, "result": {"suggest": "unknown", "label": 100}}),
        Response({"errmsg": "ok", "result": {"suggest": "pass"}}),  # 没有 errcode
    ],
)
def test_failures_are_check_down_not_pass(calls, monkeypatch, reply):
    """出错、超时、返回看不懂都抛 CheckDown，不在出错时放行。"""
    cloud(monkeypatch)
    calls.replies.append(reply)
    with pytest.raises(CheckDown) as error:
        check_text("wx", "不能出现在报错里的提问")
    assert "不能出现在报错里的提问" not in str(error.value)


def test_long_text_split_into_pieces(calls, monkeypatch):
    """超过 2500 字分几段查，每段不超过 2500 字，前后重叠 50 字，接起来是全文。"""
    cloud(monkeypatch)
    text = "".join(f"{i:04d}" for i in range(1500))  # 6000 字，处处不同
    assert check_text("wx", text) == Verdict(True, "pass", 100)
    pieces = [r["json"]["content"] for r in calls.requests]
    assert [len(p) for p in pieces] == [2500, 2500, 1100]
    assert pieces[0][-50:] == pieces[1][:50] and pieces[1][-50:] == pieces[2][:50]
    assert pieces[0] + pieces[1][50:] + pieces[2][50:] == text
    assert {r["json"]["openid"] for r in calls.requests} == {"wx"}


def test_exactly_2500_is_one_call(calls, monkeypatch):
    cloud(monkeypatch)
    check_text("wx", "字" * 2500)
    assert len(calls.requests) == 1
    check_text("wx", "字" * 2501)
    assert [len(r["json"]["content"]) for r in calls.requests[1:]] == [2500, 51]


def test_one_bad_piece_blocks_and_stops(calls, monkeypatch):
    cloud(monkeypatch)
    calls.replies += [Response(result()), Response(result("risky", 20006))]
    assert check_text("wx", "字" * 7000) == Verdict(False, "risky", 20006)
    assert len(calls.requests) == 2  # 第三段不再查


def test_error_in_later_piece_is_check_down(calls, monkeypatch):
    cloud(monkeypatch)
    calls.replies += [Response(result()), httpx.TimeoutException("timeout")]
    with pytest.raises(CheckDown):
        check_text("wx", "字" * 3000)


def test_not_in_cloud_passes_without_calling(calls, monkeypatch):
    """本机开发、测试（不信任微信请求头，也不是核对台服务）不调接口，直接算通过。"""
    cloud(monkeypatch, trust=False, role="all")
    assert check_text("wx", "测试内容") == Verdict(True, "pass", None)
    assert calls.requests == []


def test_admin_service_calls_too(calls, monkeypatch):
    """核对台服务不信任请求头（配置不许），但问这份通知的回答在那里查，照样要调。"""
    cloud(monkeypatch, trust=False, role="admin")
    calls.replies.append(Response(result("review", 21000)))
    assert check_text("wx", "要显示的原句") == Verdict(False, "review", 21000)
    assert len(calls.requests) == 1


def test_empty_text_refused(calls, monkeypatch):
    cloud(monkeypatch)
    with pytest.raises(ValueError):
        check_text("wx", "  ")
    assert calls.requests == []


def test_check_feedback_same_as_before(calls, monkeypatch):
    """纠错照原来 api/me.py 的写法：只看通过没有，不看在不在云托管里，不分段，出错照原样抛出。"""
    cloud(monkeypatch, trust=False, role="all")  # 调用方自己看 trust_wx_headers，这里不看
    assert check_feedback("wx", "纠错") is True
    assert calls.requests == [
        {
            "url": URL,
            "json": {"openid": "wx", "version": 2, "scene": 2, "content": "纠错"},
            "timeout": 5,
        }
    ]
    calls.replies += [
        Response(result("review")),
        Response(result("risky")),
        Response({"errcode": 0}),
    ]
    assert [check_feedback("wx", "纠错") for _ in range(3)] == [False, False, False]
    check_feedback("wx", "字" * 3000)
    assert len(calls.requests[-1]["json"]["content"]) == 3000

    calls.replies.append(Response(result(errcode=500)))
    with pytest.raises(ValueError, match="微信内容安全接口未成功"):
        check_feedback("wx", "纠错")
    calls.replies.append(httpx.TimeoutException("timeout"))
    with pytest.raises(httpx.TimeoutException):
        check_feedback("wx", "纠错")
    calls.replies.append(Response(result(), status=500))
    with pytest.raises(httpx.HTTPStatusError):
        check_feedback("wx", "纠错")
    calls.replies.append(Response({"errcode": 0, "result": None}))
    with pytest.raises(AttributeError):
        check_feedback("wx", "纠错")
