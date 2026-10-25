import pytest
from pydantic import ValidationError

from youju.config import Settings


def settings(**values) -> Settings:
    # 不读 backend/.env，只看传入的值
    return Settings(_env_file=None, **values)


def test_production_roles_refuse_dev_identity() -> None:
    for role in ("api", "admin"):
        with pytest.raises(ValidationError, match="YOUJU_DEV_OPENID"):
            settings(role=role, trust_wx_headers=True, dev_openid="dev-user")
    assert settings(role="all", trust_wx_headers=False, dev_openid="dev-user").dev_openid


def test_api_role_requires_platform_identity_headers() -> None:
    with pytest.raises(ValidationError, match="YOUJU_TRUST_WX_HEADERS"):
        settings(role="api", trust_wx_headers=False, dev_openid=None)
    assert settings(role="api", trust_wx_headers=True, dev_openid=None).trust_wx_headers
    # 核对台服务不开放小程序接口，不要求信任请求头；它开公网，也不许信任请求头
    assert settings(role="admin", trust_wx_headers=False, dev_openid=None).role == "admin"
    with pytest.raises(ValidationError, match="YOUJU_TRUST_WX_HEADERS"):
        settings(role="admin", trust_wx_headers=True, dev_openid=None)


def test_xpay_env_reads_text_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # 云托管的环境变量都是文字；10/19 填 YOUJU_XPAY_ENV=1 后 api 起不来
    for text, expected in (("1", 1), ("0", 0), (" 0 ", 0)):
        monkeypatch.setenv("YOUJU_XPAY_ENV", text)
        assert Settings(_env_file=None).xpay_env == expected
    monkeypatch.setenv("YOUJU_XPAY_ENV", "2")
    with pytest.raises(ValidationError, match="xpay_env"):
        Settings(_env_file=None)
    monkeypatch.delenv("YOUJU_XPAY_ENV")
    assert Settings(_env_file=None).xpay_env == 1


def test_startup_errors_do_not_print_secrets() -> None:
    # 配置错了起不来时报错会进部署日志：不带填进去的值（密钥、数据库地址）
    with pytest.raises(ValidationError) as exc:
        settings(
            ai_advisor_enabled=True,
            ai_advisor_audience="all",
            ai_api_key="sk-very-secret-value",
            database_url="mysql+pymysql://root:db-password@127.0.0.1/x",
        )
    text = str(exc.value)
    assert "对所有人开放 AI" in text
    assert "sk-very" not in text and "db-password" not in text
