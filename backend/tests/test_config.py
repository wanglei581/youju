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
    # 核对台服务不开放小程序接口，不要求信任请求头
    assert settings(role="admin", trust_wx_headers=False, dev_openid=None).role == "admin"
