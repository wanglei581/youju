import json

import httpx
import pytest

from youju.ai.client import AIError, ChatClient


def response(content, status=200):
    return httpx.Response(
        status,
        json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 1}},
    )


def test_success_and_request_format():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer secret-key"
        return response('{"ok": true}')

    client = ChatClient(
        "https://example.test/v1", "secret-key", "qwen", transport=httpx.MockTransport(handler)
    )
    assert client.complete_json("输出 JSON", "请输出 JSON") == {"ok": True}
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["temperature"] == 0
    assert client.last_usage == {"prompt_tokens": 1, "completion_tokens": 0}


def test_invalid_json_then_success():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return response("坏了") if calls == 1 else response('{"ok": 1}')

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    assert client.complete_json("JSON", "JSON") == {"ok": 1}
    assert calls == 2


def test_429_then_success():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls == 1 else response('{"ok": 1}')

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    assert client.complete_json("JSON", "JSON") == {"ok": 1}


def test_timeout_exhausted_does_not_leak_key():
    def handler(request):
        raise httpx.ReadTimeout("secret-key leaked")

    client = ChatClient(
        "https://example.test",
        "secret-key",
        "qwen",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError) as error:
        client.complete_json("JSON", "JSON")
    assert "secret-key" not in str(error.value)


def test_server_disconnect_is_retried_as_a_network_error():
    """对方中途断开（RemoteProtocolError 不属于 NetworkError）也要重试，最后按网络失败报错。"""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")
        return response('{"ok": true}')

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    assert client.complete_json("JSON", "JSON") == {"ok": True}

    def always(request):
        raise httpx.RemoteProtocolError("Server disconnected")

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=1,
        transport=httpx.MockTransport(always),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError, match="网络"):
        client.complete_json("JSON", "JSON")


def test_usage_counts_every_response_and_survives_failure():
    """格式不对要重试的那几次也花了用量：都要算上，最终失败时也不丢。"""
    replies = iter(["不是 JSON", '{"ok": true}'])
    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(lambda request: response(next(replies))),
        sleep=lambda _: None,
    )
    assert client.complete_json("JSON", "x") == {"ok": True}
    assert client.last_usage["prompt_tokens"] == 2
    assert client.totals["prompt_tokens"] == 2  # 重试的那次也记进累计用量

    failing = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=1,
        transport=httpx.MockTransport(lambda request: response("还是不是 JSON")),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError):
        failing.complete_json("JSON", "x")
    assert failing.last_usage["prompt_tokens"] == 2
    assert failing.totals == {"calls": 1, "errors": 1, "prompt_tokens": 2, "completion_tokens": 0}


def test_usage_in_error_responses_is_counted():
    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=0,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503, json={"usage": {"prompt_tokens": 7, "completion_tokens": 0}}
            )
        ),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError):
        client.complete_json("JSON", "x")
    assert client.last_usage["prompt_tokens"] == 7


PREFIX = "AI 返回内容不是合法 JSON 对象"
SECRET = "sk-live-SHOULD-NOT-LEAK"


def chat(body):
    return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    ("body", "code", "hidden"),
    [
        ([], "invalid_envelope", ""),
        ({}, "invalid_envelope", ""),
        ({"choices": []}, "invalid_envelope", ""),
        ({"choices": "no"}, "invalid_envelope", ""),
        ({"choices": ["bad"]}, "invalid_envelope", ""),
        ({"choices": [{}]}, "invalid_envelope", ""),
        ({"choices": [{"message": "x"}]}, "invalid_envelope", ""),
        ({"choices": [{"message": {}}]}, "empty_content", ""),
        ({"choices": [{"message": {"content": None}}]}, "empty_content", ""),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": "  \n"}}]},
            "empty_content",
            "",
        ),
        (
            {"choices": [{"finish_reason": None, "message": {"content": "坏了"}}]},
            "invalid_json",
            "",
        ),
        ({"choices": [{"message": {"content": "坏了"}}]}, "invalid_json", SECRET),
        (
            {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]},
            "invalid_json",
            "",
        ),
        ({"choices": [{"message": {"content": '{"ok": true'}}]}, "invalid_json", ""),
        ({"choices": [{"message": {"content": "[1, 2]"}}]}, "not_object", ""),
        ({"choices": [{"message": {"content": '"{\\"ok\\": true}"'}}]}, "not_object", ""),
        ({"choices": [{"message": {"content": "null"}}]}, "not_object", ""),
        ({"choices": [{"message": {"content": ["分段"]}}]}, "not_object", ""),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": '{"ok": true}'}}]},
            "truncated",
            "",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": {"ok": True, "secret": SECRET}},
                    }
                ]
            },
            "truncated",
            SECRET,
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": '{"ok":'}}]},
            "truncated",
            "",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": f"content_filter {SECRET}",
                        "message": {"content": f'{{"ok": true, "secret": "{SECRET}"}}'},
                    }
                ]
            },
            "non_stop_finish",
            SECRET,
        ),
        (
            {"choices": [{"finish_reason": "", "message": {"content": '{"ok": true}'}}]},
            "non_stop_finish",
            "",
        ),
    ],
)
def test_json_errors_use_safe_codes(body, code, hidden):
    if hidden and code == "invalid_json":
        body = {"choices": [{"message": {"content": f"{hidden} {{未闭合"}}]}
    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=0,
        transport=httpx.MockTransport(lambda request: chat(body)),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError) as error:
        client.complete_json("JSON", "JSON")
    assert error.value.code == code
    assert str(error.value) == f"{PREFIX}（{code}）"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    if hidden:
        assert hidden not in str(error.value)
        assert hidden not in repr(error.value)


def test_non_json_body_is_invalid_envelope():
    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not-json")),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError) as error:
        client.complete_json("JSON", "JSON")
    assert error.value.code == "invalid_envelope"
    assert str(error.value).startswith(PREFIX)
    assert error.value.__cause__ is None


def test_missing_or_null_finish_reason_still_accepts_object():
    bodies = [
        {"choices": [{"message": {"content": {"ok": True}}}]},
        {"choices": [{"finish_reason": None, "message": {"content": '{"ok": 1}'}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": 2}'}}]},
    ]

    def handler(request):
        return chat(bodies.pop(0))

    client = ChatClient(
        "https://example.test", "key", "qwen", transport=httpx.MockTransport(handler)
    )
    assert client.complete_json("JSON", "JSON") == {"ok": True}
    assert client.complete_json("JSON", "JSON") == {"ok": 1}
    assert client.complete_json("JSON", "JSON") == {"ok": 2}


def test_retries_keep_max_tokens_and_sum_usage():
    bodies = iter(
        [
            {
                "choices": [{"finish_reason": "stop", "message": {"content": "不是 JSON"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 9},
            },
            {
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": 1}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        ]
    )
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["max_tokens"])
        return chat(next(bodies))

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    assert client.complete_json("JSON", "JSON", max_tokens=3000) == {"ok": 1}
    assert seen == [3000, 3000]
    assert client.last_usage == {"prompt_tokens": 9, "completion_tokens": 11}
    assert client.totals["errors"] == 0
    assert client.totals["prompt_tokens"] == 9


def test_parse_retry_count_stays_the_same():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["max_tokens"])
        return response("坏了")

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    with pytest.raises(AIError) as error:
        client.complete_json("JSON", "x", max_tokens=800)
    assert seen == [800, 800, 800]
    assert error.value.code == "invalid_json"
    assert client.totals["calls"] == 1
    assert client.totals["errors"] == 1


def test_max_retries_zero_rejects_complete_dict_when_length():
    seen = []
    sleeps = []

    def handler(request):
        seen.append(json.loads(request.content)["max_tokens"])
        return chat(
            {
                "choices": [{"finish_reason": "length", "message": {"content": {"ok": True}}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 8},
            }
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    with pytest.raises(AIError) as error:
        client.complete_json("JSON", "x", max_tokens=6000)
    assert seen == [6000]
    assert sleeps == []
    assert error.value.code == "truncated"
    assert str(error.value) == f"{PREFIX}（truncated）"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert client.last_usage == {"prompt_tokens": 2, "completion_tokens": 8}
    assert client.totals["errors"] == 1


@pytest.mark.parametrize(
    ("finish_reason", "content", "code"),
    [
        ("length", {"ok": True}, "truncated"),
        ("length", '{"conditions":[{"text":"', "truncated"),
        ("content_filter", '{"ok": true}', "non_stop_finish"),
    ],
)
def test_terminal_finish_is_not_retried_and_keeps_usage(finish_reason, content, code):
    """默认 max_retries=2。截断和非正常结束只请求一次，用量仍计入。"""
    seen = []
    sleeps = []

    def handler(request):
        seen.append(json.loads(request.content)["max_tokens"])
        return chat(
            {
                "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
                "usage": {"prompt_tokens": 6, "completion_tokens": 4},
            }
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    assert client.max_retries == 2
    with pytest.raises(AIError) as error:
        client.complete_json("JSON", "JSON", max_tokens=6000)
    assert seen == [6000]
    assert sleeps == []
    assert error.value.code == code
    assert error.value.retryable is False
    assert error.value.__cause__ is None
    assert client.last_usage == {"prompt_tokens": 6, "completion_tokens": 4}
    assert client.totals == {"calls": 1, "errors": 1, "prompt_tokens": 6, "completion_tokens": 4}


# ---- 流式返回（SSE，D-098）：云开发 AI 网关对不走流式、1 分钟内没生成完的请求不返回结果 ----


def sse(*events, done=True):
    lines = [f"data: {json.dumps(e, ensure_ascii=False)}" for e in events]
    if done:
        lines.append("data: [DONE]")
    return httpx.Response(
        200,
        content=("\n\n".join(lines) + "\n\n").encode(),
        headers={"content-type": "text/event-stream"},
    )


def delta(text=None, finish=None, reasoning=None):
    d = {}
    if text is not None:
        d["content"] = text
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}]}


def test_stream_joins_deltas_and_reads_usage():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["accept"] = request.headers.get("accept")
        return sse(
            delta(reasoning="先想一想"),
            delta('{"ok": '),
            delta("true}"),
            delta(finish="stop"),
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )

    client = ChatClient(
        "https://example.test/v1", "key", "hy3", transport=httpx.MockTransport(handler), stream=True
    )
    assert client.complete_json("输出 JSON", "请输出 JSON") == {"ok": True}
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["accept"] == "text/event-stream"
    assert client.last_usage == {"prompt_tokens": 7, "completion_tokens": 3}


def test_stream_must_end_properly():
    """流要正常结束：有 [DONE] 或 finish_reason。中途断了的半截内容，就算碰巧是合法 JSON 也不用。"""

    def run(reply):
        return ChatClient(
            "https://example.test",
            "key",
            "hy3",
            transport=httpx.MockTransport(lambda request: reply),
            stream=True,
            max_retries=0,
        )

    with pytest.raises(AIError) as exc:
        run(sse(delta('{"a": 1}'), done=False)).complete_json("s", "u")
    assert exc.value.code == "invalid_envelope"
    assert run(sse(delta('{"a": 1}'))).complete_json("s", "u") == {"a": 1}
    assert run(sse(delta('{"a": 2}', finish="stop"), done=False)).complete_json("s", "u") == {
        "a": 2
    }


def test_stream_error_event_drops_partial_content():
    """流里中途来了错误事件：已收到的内容不用，按 AI 服务暂时不可用处理，用量照样计入。"""
    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        transport=httpx.MockTransport(
            lambda request: sse(
                delta('{"a": 1}'),
                {"error": {"message": "upstream"}, "usage": {"prompt_tokens": 4}},
                delta(finish="stop"),
            )
        ),
        stream=True,
        max_retries=0,
    )
    with pytest.raises(AIError) as exc:
        client.complete_json("s", "u")
    assert exc.value.retryable is True and "502" in str(exc.value)
    assert client.last_usage["prompt_tokens"] == 4


def test_stream_read_gap_is_capped():
    """两段之间最多等 45 秒：对方停发又不断开连接时，不会再等满整个时限。"""
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return sse(delta('{"ok": 1}', finish="stop"))

    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        timeout=180,
        transport=httpx.MockTransport(handler),
        stream=True,
    )
    assert client.complete_json("s", "u") == {"ok": 1}
    assert seen["read"] == 45 and seen["connect"] == 180
    short = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        timeout=20,
        transport=httpx.MockTransport(handler),
        stream=True,
    )
    short.complete_json("s", "u")
    assert seen["read"] == 20


def test_stream_length_finish_is_truncated_and_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return sse(delta('{"a": '), delta(finish="length"))

    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        transport=httpx.MockTransport(handler),
        stream=True,
        sleep=lambda _: None,
    )
    with pytest.raises(AIError) as exc:
        client.complete_json("s", "u")
    assert exc.value.code == "truncated" and calls == 1


def test_stream_429_then_success_and_error_usage_counted():
    replies = iter(
        [
            httpx.Response(429, json={"error": "busy", "usage": {"prompt_tokens": 2}}),
            sse(delta('{"ok": 1}', finish="stop")),
        ]
    )
    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        transport=httpx.MockTransport(lambda request: next(replies)),
        stream=True,
        sleep=lambda _: None,
    )
    assert client.complete_json("s", "u") == {"ok": 1}
    assert client.last_usage["prompt_tokens"] == 2


def test_stream_bad_chunk_is_invalid_envelope_and_keeps_usage():
    def handler(request):
        body = (
            'data: {"choices": [], "usage": {"prompt_tokens": 5}}\n\n'
            "data: 不是 JSON\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        transport=httpx.MockTransport(handler),
        stream=True,
        max_retries=0,
    )
    with pytest.raises(AIError) as exc:
        client.complete_json("s", "u")
    assert exc.value.code == "invalid_envelope"
    assert client.last_usage["prompt_tokens"] == 5
    assert "不是 JSON" not in str(exc.value)


def test_stream_overall_deadline_is_a_retryable_network_error():
    ticks = iter([0.0, 5.0, 31.0, 62.0, 93.0])
    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        timeout=30,
        transport=httpx.MockTransport(
            lambda request: sse(delta('{"a"'), delta(": 1"), delta("}"), delta(finish="stop"))
        ),
        stream=True,
        max_retries=0,
        clock=lambda: next(ticks),
    )
    with pytest.raises(AIError) as exc:
        client.complete_json("s", "u")
    assert exc.value.retryable is True
    assert str(exc.value) == "AI 网络请求失败"


def test_stream_skips_comments_and_event_lines():
    body = (
        ": keep-alive\n\n"
        "event: message\n"
        f"data: {json.dumps(delta('{"x": 2}'))}\n\n"
        f"data: {json.dumps(delta(finish='stop'))}\n\n"
        "data: [DONE]\n\n"
    )
    client = ChatClient(
        "https://example.test",
        "key",
        "hy3",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/event-stream"}
            )
        ),
        stream=True,
    )
    assert client.complete_json("s", "u") == {"x": 2}


def test_non_stream_is_unchanged_by_default():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return response('{"ok": true}')

    client = ChatClient(
        "https://example.test", "key", "qwen", transport=httpx.MockTransport(handler)
    )
    assert client.complete_json("s", "u") == {"ok": True}
    assert "stream" not in seen and "stream_options" not in seen
