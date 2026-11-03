"""运行配置：全部从环境变量（前缀 YOUJU_）或 backend/.env 读取。"""

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCES_CSV = REPO_ROOT / "docs" / "数据来源" / "全国来源清单_P0_v1.csv"


class Settings(BaseSettings):
    # hide_input_in_errors：配置错了起不来时，报错（会进部署日志）里不带填进去的值——密钥、数据库地址
    model_config = SettingsConfigDict(
        env_prefix="YOUJU_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

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
    agreement_version: str = "2026-11"
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
    # 对外的 AI（政策解读、条款对照）单独的调用地址、密钥、模型和附加参数（10/17 创始人确认）：
    # 对外 AI 走腾讯云开发（和微信「深度合成 > AI 问答」类目的算法合作协议、网信办登记材料一致），
    # 后台核对继续直连 DeepSeek 官方。地址、密钥、模型三项要么都不填（沿用上面后台的），要么一起填；
    # 附加参数不填沿用 YOUJU_AI_REQUEST_EXTRA，换平台时按新平台填（如 {}）
    ai_advisor_base_url: str = ""
    ai_advisor_api_key: SecretStr | None = None
    ai_advisor_model: str = ""
    ai_advisor_request_extra: dict | None = None
    # 对外 AI 用流式返回（SSE）：经云开发调混元时，网关对不走流式、1 分钟内没生成完的请求不返回结果
    # （D-098 调研，docs.cloudbase.net 的 FAQ）。默认关；实测最长的任务超过 1 分钟再打开
    ai_advisor_stream: bool = False
    ai_model_label: str = ""
    ai_model_filing: str = ""

    sources_csv: Path = DEFAULT_SOURCES_CSV
    # 每个用户 10 分钟内最多导出几次（防止刷接口耗尽资源）；验收 C2"连续导出 100 次"时临时调高
    exports_per_10min: int = 30
    # 云托管环境 ID（如 prod-xxxx）：每天的数据库备份存到这个环境的对象存储（验收 A6）
    cloud_env: str | None = None
    # R1.5 顾问技能包总开关；上线前保持关闭。
    rewards_enabled: bool = False
    advisor_enabled: bool = False
    ai_advisor_enabled: bool = False
    ai_advisor_openids: str = ""
    ai_advisor_audience: Literal["whitelist", "all"] = "whitelist"
    ai_advisor_daily_cap: int = Field(default=200, ge=0)
    ai_advisor_concurrency: int = Field(default=3, ge=1, le=8)
    # 加量体验（D-070 ③，见 boost.py）：单独开关，默认关，还要批量工具开关开着。打开之前先执行
    # mysql_accounts.sql 里的"加量体验权限"，并让写明体验记录的 2026-11 版隐私保护指引先上线
    # （只同意过更早版本的开始不了体验，见 agreements.TOOLS_AGREEMENT_VERSION）。
    # 关掉只是不能再开始新的体验，已经开始的照常用满 72 小时（开始前承诺了连续 3 天）
    boost_trial_enabled: bool = False
    # 申报季加量（D-070 ⑤）：起止日期（北京时间，含首尾）内免费批量次数每月 5 次，每次仍 50 家以内。
    # 开关开着就要两个日期都填（如 2026-11-01），开始不能晚于结束，配错就不启动
    season_boost_enabled: bool = False
    season_boost_start: date | None = None
    season_boost_end: date | None = None

    # 收费必须单独开启；未配置价格的商品不出售。密钥由创始人填环境变量。
    pay_enabled: bool = False
    # 开关打开后先只卖给测试名单（体验版里现网真付、真扣钱；体验版和正式版连的是同一个后端，
    # 不能让所有用户看到）；
    # 门槛达标、创始人批准后改成 all 才对全体开放。名单是逗号分隔的 openid（我的数据页可复制）。
    pay_audience: Literal["whitelist", "all"] = "whitelist"
    pay_openids: str = ""
    # 现行《付费服务协议》的版本号（和小程序 content/pay-terms.js 的 version 一致）。没配就不卖
    # （payments.orderable）；下单时带来的版本不一致就不让下单（协议更新后，旧版小程序要重新同意）。
    pay_terms_version: str = ""
    # 能开哪种发票（D-070：付款前要看得到），创始人确认后填一句，
    # 如"增值税电子普通发票，个人或单位抬头"。购买页照这句显示；没填就不卖（payments.orderable）
    pay_invoice_type: str = Field(default="", max_length=100)
    # 停止服务公示期间（付费服务协议第二条第 5 项：至少提前 30 天公示）打开：不再卖新的使用期，
    # 苹果退款问询对还有剩余使用期的订单答"建议退款"（第二条第 7 项）。默认关；
    # api、admin 两个服务都设（卖不卖、苹果问询都在 api 上判断）
    pay_shutdown_notice: bool = False
    xpay_offer_id: str = ""
    # 新订单一律现网（xpay.XPAY_ENV），没有沙箱开关了（创始人 10/29）；YOUJU_XPAY_ENV 不再起作用。
    # 现网 AppKey 没配就不卖（payments.orderable）；api、admin 都要配
    # （下单签名在 api，查单、发货、退款、对账在 admin）
    xpay_appkey_prod: SecretStr | None = None
    # 只给去掉沙箱以前下的测试单（订单上记的 env 是 1）查单、退款用；没有这类没办完的单就不用配
    xpay_appkey_sandbox: SecretStr | None = None
    wx_appsecret: SecretStr | None = None
    wx_appid: str = ""
    # 批量工具两档（D-070）：30 天、90 天各是小程序后台的一个道具，商品编号和价格（分）分别配；
    # 天数跟着档位走（payments.products）。没配价格的那一档不卖
    xpay_price_pass: int | None = Field(default=None, gt=0, le=2147483647)
    xpay_product_pass: str = "workbench_30d"
    xpay_price_pass_90: int | None = Field(default=None, gt=0, le=2147483647)
    xpay_product_pass_90: str = "workbench_90d"
    # 续买多送 3 天（D-070 ⑥）：到期前后 7 天内续买，每次多送 3 天。单独开关，默认关；
    # 下单时开着才算数（订单记下当时的赠送天数），关掉以后新订单不再送，已经送的不收回
    renew_bonus_enabled: bool = False
    # 纠错被采纳送 1 天（D-070 ⑦，可选）：单独开关，默认关，还要批量工具开关开着。
    # 和旧的奖励总开关（rewards_enabled）分开：只开这一项，
    # 不会连带首次批量、引荐、补截止这些已不做的奖励
    correction_reward_enabled: bool = False
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

    @field_validator("xpay_price_pass", "xpay_price_pass_90", mode="before")
    @classmethod
    def _empty_pass_price(cls, value):
        return None if value == "" else value

    @field_validator("season_boost_start", "season_boost_end", mode="before")
    @classmethod
    def _empty_season_date(cls, value):
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
    def _advisor_endpoint_complete(self) -> "Settings":
        """对外 AI 单独设置时三项一起填：只改了地址、没改密钥或模型，
        每次调用都会失败，所以配错就不启动。"""
        key = self.ai_advisor_api_key.get_secret_value() if self.ai_advisor_api_key else ""
        parts = (bool(self.ai_advisor_base_url), bool(key), bool(self.ai_advisor_model))
        if any(parts) and not all(parts):
            raise ValueError(
                "对外 AI 单独设置时，YOUJU_AI_ADVISOR_BASE_URL、YOUJU_AI_ADVISOR_API_KEY、"
                "YOUJU_AI_ADVISOR_MODEL 三项要一起填"
            )
        # 对所有人开放时只能走对外线路（云开发，D-093 上线门槛第 3 条）：类目协议、实际调用、
        # 页面公示的模型要一致，不能退回后台核对用的线路。白名单内部试用不受这条限制。
        # 只有处理 AI 任务的服务（admin，本机的 all）调模型、要配这条线路；api 服务不调模型，
        # 不用放密钥（D-098：密钥少放一处，也免得只给 admin 配了、api 起不来）
        if (
            self.ai_advisor_enabled
            and self.ai_advisor_audience == "all"
            and self.role != "api"
            and not all(parts)
        ):
            raise ValueError(
                "对所有人开放 AI（YOUJU_AI_ADVISOR_AUDIENCE=all）时，要配好对外线路："
                "YOUJU_AI_ADVISOR_BASE_URL、YOUJU_AI_ADVISOR_API_KEY、YOUJU_AI_ADVISOR_MODEL，"
                "不能用后台核对的线路"
            )
        # 对所有人开放时要能公示所用模型的名称和备案（登记）编号（D-098；上线门槛第 1、5 条）：
        # 没填时 AI 首页和结果页底部那一行不显示，所以不能开放
        if (
            self.ai_advisor_enabled
            and self.ai_advisor_audience == "all"
            and not (self.ai_model_label.strip() and self.ai_model_filing.strip())
        ):
            raise ValueError(
                "对所有人开放 AI（YOUJU_AI_ADVISOR_AUDIENCE=all）时，"
                "要填公示用的模型名称和备案编号：YOUJU_AI_MODEL_LABEL、YOUJU_AI_MODEL_FILING"
            )
        return self

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

    @model_validator(mode="after")
    def _final_pay_terms(self) -> "Settings":
        """现网收费只认定稿的《付费服务协议》（payments.orderable）：填成草稿版本号，
        带着草稿协议的旧小程序也能下单，所以不启动（D-070 开卖前 Codex 审阅）。"""
        if "draft" in self.pay_terms_version.lower():
            raise ValueError(
                "YOUJU_PAY_TERMS_VERSION 不能填草稿版本（如 pay-2026-10-draft），要填定稿的版本号"
            )
        return self

    @model_validator(mode="after")
    def _pass_products_distinct(self) -> "Settings":
        """两档用了同一个商品编号：付款推送分不清买的是哪一档，所以不启动。"""
        if (
            self.xpay_price_pass is not None
            and self.xpay_price_pass_90 is not None
            and self.xpay_product_pass.strip() == self.xpay_product_pass_90.strip()
        ):
            raise ValueError(
                "YOUJU_XPAY_PRODUCT_PASS 和 YOUJU_XPAY_PRODUCT_PASS_90 不能是同一个商品编号"
            )
        return self

    @model_validator(mode="after")
    def _pass_prices_payable(self) -> "Settings":
        """价格不到 1 元：苹果手机付不了（官方：Apple 支付最低 1 元），
        安卓能买、苹果买不了，所以不启动。"""
        if any(p is not None and p < 100 for p in (self.xpay_price_pass, self.xpay_price_pass_90)):
            raise ValueError(
                "YOUJU_XPAY_PRICE_PASS、YOUJU_XPAY_PRICE_PASS_90 按分填，不能少于 100（1 元）"
            )
        return self

    @model_validator(mode="after")
    def _season_boost_dates(self) -> "Settings":
        """申报季加量开着却没写日期、或开始晚于结束：页面上的起止日期和实际次数会对不上，所以不启动。"""
        if self.season_boost_enabled and (
            self.season_boost_start is None
            or self.season_boost_end is None
            or self.season_boost_start > self.season_boost_end
        ):
            raise ValueError(
                "YOUJU_SEASON_BOOST_ENABLED=true 时要填 YOUJU_SEASON_BOOST_START 和"
                " YOUJU_SEASON_BOOST_END（如 2026-11-01），开始不能晚于结束"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
