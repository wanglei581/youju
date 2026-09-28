from youju.ai import ChatClient, get_chat_client
from youju.config import get_settings


def test_get_chat_client_without_key(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("YOUJU_AI_API_KEY", raising=False)
    try:
        assert get_chat_client() is None
    finally:
        get_settings.cache_clear()


def test_get_chat_client_with_key_does_not_expose_it(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("YOUJU_AI_API_KEY", "sk-test-secret")
    try:
        client = get_chat_client()
        assert isinstance(client, ChatClient)
        client.close()
        # 配置打印出来（比如写进日志）时不能带出密钥
        assert "sk-test-secret" not in repr(get_settings())
        assert "sk-test-secret" not in str(get_settings().model_dump())
    finally:
        get_settings.cache_clear()
