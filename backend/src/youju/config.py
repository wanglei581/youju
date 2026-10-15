"""运行配置：全部从环境变量（前缀 YOUJU_）或 backend/.env 读取。"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # 开公网的推送入口必须带微信来源头；私网入口按云托管转发信任推送。
    wx_push_public: bool = True
    dev_openid: str | None = None
    template_deadline: dict | None = None
    template_newdocs: dict | None = None
    # 定时任务真正发送订阅消息（云托管开放接口服务）；不开时只演练。与是否信任请求头分开设置
    send_wx_messages: bool = False
    # 用户协议和隐私保护指引的当前版本，和小程序 content/legal.js 的 version 一致。
    # 版本号按时间可排序（2026-10、2026-11……），后端和小程序都直接比字符串：
    # 同意过更新的版本也算同意了旧的
    agreement_version: str = "2026-10"
    # 仍然认的最早版本，平时不填（等于当前版本，旧版的同意失效、要重新同意）。
    # 协议升版只是给新功能加了条款（如 AI 版）时，填升版前的版本：旧的同意照旧能用旧功能
    # （关注、纠错、提醒），新功能要求同意过写进它的条款的那一版（me.agreement_at_least），
    # 还没更新的旧版小程序也能同意旧版。
    # 只有旧条款不再覆盖旧功能时才调高；调高后停在旧版本的用户要更新小程序、重新同意
    agreement_min_version: str = ""
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

    # 后台 AI（模型方可配置，OpenAI 兼容接口）。不配密钥就不调用 AI，照常用规则。
    # 密钥只从环境读取（云托管环境变量由创始人填写），不写入日志或异常信息
    ai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ai_api_key: SecretStr | None = None
    ai_model: str = "qwen-flash"
    # AI 顾问（解读、条款对照）单次调用最多等多久。整篇摘录输出长，强模型常要 1–3 分钟；
    # 不能超过任务总时限（advisor_tasks.TASK_TIMEOUT，5 分钟）。
    ai_advisor_timeout_seconds: float = Field(default=180, gt=0, le=290)
    # 附加到每次模型请求里的参数（JSON），按模型方填，例如 DeepSeek 关闭思考模式：
    # YOUJU_AI_REQUEST_EXTRA={"thinking": {"type": "disabled"}}
    ai_request_extra: dict = {}
    # 后台核对（截止日期交叉抽取、质检）用的模型；空值表示和 ai_model 相同。调用量大，适合便宜的模型
    ai_background_model: str = ""
    ai_model_label: str = ""
    ai_model_filing: str = ""

    sources_csv: Path = DEFAULT_SOURCES_CSV
    # 每个用户 10 分钟内最多导出几次（防止刷接口耗尽资源）；验收 C2"连续导出 100 次"时临时调高
    exports_per_10min: int = 30
    # 云托管环境 ID（如 prod-xxxx）：每天的数据库备份存到这个环境的对象存储（验收 A6）
    cloud_env: str | None = None
    # R1.5 顾问技能包总开关；上线前保持关闭。
    rewards_enabled: bool = False
    referral_enabled: bool = False
    advisor_enabled: bool = False
    ai_advisor_enabled: bool = False
    ai_advisor_openids: str = ""
    ai_advisor_audience: Literal["whitelist", "all"] = "whitelist"
    ai_advisor_daily_cap: int = Field(default=200, ge=0)
    ai_advisor_concurrency: int = Field(default=3, ge=1, le=8)

    # 收费必须单独开启；未配置价格的商品不出售。密钥由创始人填环境变量。
    pay_enabled: bool = False
    # 现行《付费服务协议》的版本号（和小程序 content/pay-terms.js 的 version 一致）。配了以后，
    # 下单时带来的版本不一致就不让下单（协议更新后，旧版小程序要重新同意）；不配只记录不比对。
    pay_terms_version: str = ""
    xpay_offer_id: str = ""
    xpay_env: Literal[0, 1] = 1
    xpay_appkey_prod: SecretStr | None = None
    xpay_appkey_sandbox: SecretStr | None = None
    wx_appsecret: SecretStr | None = None
    wx_appid: str = ""
    xpay_price_pass: int | None = Field(default=None, gt=0, le=2147483647)
    xpay_product_pass: str = "workbench_30d"
    wx_push_path: str = "/api/wx/push"
    # 空值按官方 order_type 枚举识别；可配置字段映射应对接口差异，未识别时不退款。
    xpay_platform_field: str = ""
    xpay_ios_values: list[str] = []
    xpay_android_values: list[str] = []
    # 财务留存期限为待确认配置值，不代表已经完成法律审阅。
    retain_pay_orders_days: int = Field(default=3650, ge=180)
    # 日账单字段和单位由线上样本确认；不配置则对账报告明确标为无法解析。
    xpay_bill_order_column: str = ""
    xpay_bill_fee_column: str = ""
    xpay_bill_fee_unit: Literal["fen", "yuan"] = "fen"
    xpay_bill_hosts: list[str] = []

    @field_validator("xpay_price_pass", mode="before")
    @classmethod
    def _empty_pass_price(cls, value):
        return None if value == "" else value

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        def public_dotenv():
            values = dotenv_settings()
            for key in ("wx_appsecret", "xpay_appkey_prod", "xpay_appkey_sandbox"):
                values.pop(key, None)
            return values

        def public_file_secrets():
            values = file_secret_settings()
            for key in ("wx_appsecret", "xpay_appkey_prod", "xpay_appkey_sandbox"):
                values.pop(key, None)
            return values

        return init_settings, env_settings, public_dotenv, public_file_secrets

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
        # 协议版本按"年-月"直接比字符串：写法不对、最早版本比当前版本还新，都会让同意的判断出错
        versions = [self.agreement_version] + [self.agreement_min_version] * bool(
            self.agreement_min_version
        )
        if not all(re.match(r"\d{4}-\d{2}", v) for v in versions):
            raise ValueError(
                "YOUJU_AGREEMENT_VERSION、YOUJU_AGREEMENT_MIN_VERSION 要写成年-月开头，如 2026-10"
            )
        if self.agreement_min_version > self.agreement_version:
            raise ValueError("YOUJU_AGREEMENT_MIN_VERSION 不能比 YOUJU_AGREEMENT_VERSION 新")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
