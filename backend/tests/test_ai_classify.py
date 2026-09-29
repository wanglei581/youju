import httpx

from youju.ai.classify import classify_with_ai
from youju.ai.client import ChatClient


def test_invalid_kind_is_discarded():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"kind":"新闻","confidence":2,"reason":"x"}'}}]
            },
        )
    )
    client = ChatClient("https://example.test", "key", "qwen", transport=transport)
    assert classify_with_ai(client, "标题", "正文") is None


def test_classification_is_clamped_and_body_limited():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"kind":"政策","confidence":2,"reason":"说明"}'}}
                ]
            },
        )

    client = ChatClient(
        "https://example.test", "key", "qwen", transport=httpx.MockTransport(handler)
    )
    result = classify_with_ai(client, "标题", "a" * 4000)
    assert result and result.kind == "政策" and result.confidence == 1
    assert "a" * 3001 not in seen["body"]
