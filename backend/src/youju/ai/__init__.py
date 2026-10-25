"""后台 AI（模型方可配置，OpenAI 兼容接口）。只处理政府公开文件，不发送任何用户数据。"""

from .client import AIError, ChatClient


def get_chat_client(timeout: float = 30.0) -> ChatClient | None:
    """读取配置并返回客户端；没配置密钥时返回 None，表示不用 AI、照常用规则。
    timeout：单次请求最多等多少秒（输出长的任务，如申报条件抽取，要放宽）。"""
    from youju.config import get_settings

    settings = get_settings()
    key = settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""
    if not key:
        return None
    # 后台核对可以单独用便宜的模型（YOUJU_AI_BACKGROUND_MODEL），不填就用 YOUJU_AI_MODEL。
    # 对外 AI 单独设置的线路（YOUJU_AI_ADVISOR_*）不影响这里：后台核对一直走这一套
    model = getattr(settings, "ai_background_model", "") or settings.ai_model
    return ChatClient(
        settings.ai_base_url,
        key,
        model,
        timeout=timeout,
        extra_body=getattr(settings, "ai_request_extra", None),
    )


def advisor_endpoint(settings) -> tuple[str, str, str, dict | None]:
    """对外 AI（政策解读、条款对照）的调用地址、密钥、模型和附加参数。
    单独配了（YOUJU_AI_ADVISOR_*，三项一起填，见 config）用单独的。没配时：白名单内部试用沿用
    后台 AI 的线路；对所有人开放（audience=all）时不退回后台线路，返回空密钥（config 启动时也会
    拦下这种配置，D-093 上线门槛第 3 条）。密钥为空表示没配置，调用方据此报「AI 服务尚未配置」。"""
    advisor_key = getattr(settings, "ai_advisor_api_key", None)
    advisor_key = advisor_key.get_secret_value() if advisor_key else ""
    if getattr(settings, "ai_advisor_base_url", "") and advisor_key:
        base_url, key = settings.ai_advisor_base_url, advisor_key
        model = getattr(settings, "ai_advisor_model", "") or settings.ai_model
    elif getattr(settings, "ai_advisor_audience", "whitelist") == "all":
        return "", "", "", None
    else:
        base_url = settings.ai_base_url
        key = settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""
        model = settings.ai_model
    extra = getattr(settings, "ai_advisor_request_extra", None)
    if extra is None:
        extra = getattr(settings, "ai_request_extra", None)
    return base_url, key, model, extra


__all__ = ["AIError", "ChatClient", "advisor_endpoint", "get_chat_client"]
