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
