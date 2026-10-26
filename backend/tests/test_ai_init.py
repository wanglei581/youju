import pytest

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


ADVISOR_ENV = {
    "YOUJU_AI_ADVISOR_BASE_URL": "https://cloud1-test.api.tcloudbasegateway.com/v1/ai/cloudbase",
    "YOUJU_AI_ADVISOR_API_KEY": "sk-advisor-secret",
    "YOUJU_AI_ADVISOR_MODEL": "deepseek-v4-pro-0813",
}


def _settings(monkeypatch, **env):
    from youju.config import Settings

    for name in (*ADVISOR_ENV, "YOUJU_AI_ADVISOR_REQUEST_EXTRA", "YOUJU_AI_REQUEST_EXTRA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YOUJU_AI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("YOUJU_AI_API_KEY", "sk-background-secret")
    monkeypatch.setenv("YOUJU_AI_MODEL", "deepseek-v4-pro")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_advisor_endpoint_falls_back_to_background_settings(monkeypatch):
    """对外 AI 没单独设置：沿用后台的地址、密钥、模型和附加参数（行为和以前一样）。"""
    from youju.ai import advisor_endpoint

    settings = _settings(monkeypatch, YOUJU_AI_REQUEST_EXTRA='{"thinking": {"type": "disabled"}}')
    assert advisor_endpoint(settings) == (
        "https://api.deepseek.com",
        "sk-background-secret",
        "deepseek-v4-pro",
        {"thinking": {"type": "disabled"}},
    )


def test_advisor_endpoint_uses_its_own_settings_background_unchanged(monkeypatch):
    """对外 AI 单独设置（走腾讯云开发）：用自己的地址、密钥、模型；后台核对仍直连原来的（10/17 创始人确认）。"""
    from youju.ai import advisor_endpoint

    settings = _settings(
        monkeypatch,
        YOUJU_AI_REQUEST_EXTRA='{"thinking": {"type": "disabled"}}',
        YOUJU_AI_BACKGROUND_MODEL="deepseek-flash",
        **ADVISOR_ENV,
    )
    url, key, model, extra = advisor_endpoint(settings)
    assert (url, key, model) == tuple(ADVISOR_ENV.values())
    # 附加参数没单独填：沿用后台的；单独填了（换平台时如 {}）用单独的
    assert extra == {"thinking": {"type": "disabled"}}
    settings = _settings(monkeypatch, YOUJU_AI_ADVISOR_REQUEST_EXTRA="{}", **ADVISOR_ENV)
    assert advisor_endpoint(settings)[3] == {}
    # 后台客户端不受影响
    get_settings.cache_clear()
    monkeypatch.setenv("YOUJU_AI_BACKGROUND_MODEL", "deepseek-flash")
    try:
        client = get_chat_client()
        assert client.model == "deepseek-flash"
        assert client.base_url == "https://api.deepseek.com"
        client.close()
        # 密钥不能出现在配置的打印内容里
        dumped = repr(get_settings()) + str(get_settings().model_dump())
        assert "sk-advisor-secret" not in dumped and "sk-background-secret" not in dumped
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "partial",
    [
        {"YOUJU_AI_ADVISOR_BASE_URL": ADVISOR_ENV["YOUJU_AI_ADVISOR_BASE_URL"]},
        {k: v for k, v in ADVISOR_ENV.items() if k != "YOUJU_AI_ADVISOR_MODEL"},
        {"YOUJU_AI_ADVISOR_API_KEY": "sk-advisor-secret"},
    ],
)
def test_advisor_endpoint_settings_must_be_filled_together(monkeypatch, partial):
    """只改了地址、没改密钥或模型：每次调用都会失败，配错就不启动。"""
    with pytest.raises(ValueError, match="三项要一起填"):
        _settings(monkeypatch, **partial)


def test_open_to_all_requires_the_external_line(monkeypatch):
    """对所有人开放 AI 时只能走对外线路（云开发），没配就不启动（D-093 上线门槛第 3 条）；
    白名单内部试用还能沿用后台线路。"""
    from types import SimpleNamespace

    from youju.ai import advisor_endpoint

    for name in ("YOUJU_AI_ADVISOR_ENABLED", "YOUJU_AI_ADVISOR_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="对所有人开放 AI"):
        _settings(monkeypatch, YOUJU_AI_ADVISOR_ENABLED="true", YOUJU_AI_ADVISOR_AUDIENCE="all")
    whitelist = _settings(
        monkeypatch, YOUJU_AI_ADVISOR_ENABLED="true", YOUJU_AI_ADVISOR_AUDIENCE="whitelist"
    )
    assert advisor_endpoint(whitelist)[1] == "sk-background-secret"
    opened = _settings(
        monkeypatch, YOUJU_AI_ADVISOR_ENABLED="true", YOUJU_AI_ADVISOR_AUDIENCE="all", **ADVISOR_ENV
    )
    assert advisor_endpoint(opened)[:3] == tuple(ADVISOR_ENV.values())
    # 万一配置绕过了启动检查（例如直接改了对象），对所有人开放时也不退回后台线路
    bypassed = SimpleNamespace(
        ai_advisor_api_key=None,
        ai_advisor_base_url="",
        ai_advisor_audience="all",
        ai_base_url="https://api.deepseek.com",
        ai_api_key=whitelist.ai_api_key,
        ai_model="deepseek-v4-pro",
    )
    assert advisor_endpoint(bypassed) == ("", "", "", None)
