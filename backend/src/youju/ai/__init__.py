"""后台 AI（模型方可配置，OpenAI 兼容接口）。只处理政府公开文件，不发送任何用户数据。"""

from .client import AIError, ChatClient


def get_chat_client() -> ChatClient | None:
    """读取配置并返回客户端；没配置密钥时返回 None，表示不用 AI、照常用规则。"""
    from youju.config import get_settings

    settings = get_settings()
    key = settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""
    if not key:
        return None
    # 后台核对可以单独用便宜的模型（YOUJU_AI_BACKGROUND_MODEL），不填就和 AI 顾问用同一个
    model = getattr(settings, "ai_background_model", "") or settings.ai_model
    return ChatClient(
        settings.ai_base_url, key, model, extra_body=getattr(settings, "ai_request_extra", None)
    )


__all__ = ["AIError", "ChatClient", "get_chat_client"]
