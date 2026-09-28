"""运行配置：全部从环境变量（前缀 YOUJU_）或 backend/.env 读取。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCES_CSV = REPO_ROOT / "docs" / "数据来源" / "全国来源清单_P0_v1.csv"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YOUJU_", env_file=".env", extra="ignore")

    # 本机开发用 scripts/dev_mysql.sh 启动的 MySQL；线上填云托管 MySQL 的内网地址
    database_url: str = "mysql+pymysql://root@127.0.0.1:33306/youju?charset=utf8mb4"
    admin_token: str | None = None
    # 企业微信群机器人地址只从环境读取，不写入日志或异常信息。
    alert_webhook: SecretStr | None = None
    # 部署角色（云托管两个服务用同一镜像）：api 只开放小程序接口，admin 只开放核对台和管理接口，
    # all（本机开发默认）全部开放
    role: Literal["all", "api", "admin"] = "all"
    trust_wx_headers: bool = False
    dev_openid: str | None = None
    template_deadline: dict | None = None
    template_newdocs: dict | None = None
    # 定时任务真正发送订阅消息（云托管开放接口服务）；不开时只演练。与是否信任请求头分开设置
    send_wx_messages: bool = False
    # 用户协议和隐私保护指引的当前版本：只接受同意这个版本；更新版本后，旧版的同意失效、要重新同意
    agreement_version: str = "2026-10"
    # 个人信息保存期限（天），以隐私保护指引为准，由创始人确认
    retain_events_days: int = 90
    retain_feedback_days: int = 180
    retain_used_subscriptions_days: int = 30
    retain_unused_subscriptions_days: int = 365

    # 透明标识：保留常见浏览器标识以兼容政府网站，同时在末尾写明爬虫身份。
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 YoujuBot/0.1"
    )
    request_timeout: float = 20.0
    # 同一网站两次请求之间至少间隔的秒数。
    per_host_interval: float = 3.0
    # 同时抓取的网站数；同一网站内始终串行。
    max_workers: int = 8
    # 连续失败多少次后把来源标记为 failing 并告警。
    failure_alert_threshold: int = 3

    # 后台 AI（阿里云百炼通义千问，OpenAI 兼容接口）。不配密钥就不调用 AI，照常用规则。
    # 密钥只从环境读取（云托管环境变量由创始人填写），不写入日志或异常信息
    ai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ai_api_key: SecretStr | None = None
    ai_model: str = "qwen-flash"

    sources_csv: Path = DEFAULT_SOURCES_CSV

    @model_validator(mode="after")
    def _production_identity(self) -> "Settings":
        """云托管的两个服务只认平台注入的用户身份，配错就不启动（部署时立刻发现，而不是上线后出事）。"""
        if self.role != "all" and self.dev_openid:
            # 开发用的固定身份会让所有请求都成为同一个用户
            raise ValueError(
                f"YOUJU_ROLE={self.role} 时不能设置 YOUJU_DEV_OPENID，它只用于本机开发"
            )
        if self.role == "admin" and self.trust_wx_headers:
            # 核对台服务开公网，公网请求可以自带身份头
            raise ValueError(
                "YOUJU_ROLE=admin 时不能设置 YOUJU_TRUST_WX_HEADERS，它只用于小程序接口服务"
            )
        if self.role == "api" and not self.trust_wx_headers:
            raise ValueError(
                "YOUJU_ROLE=api 时要设置 YOUJU_TRUST_WX_HEADERS=true，用户身份取云托管注入的请求头"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
