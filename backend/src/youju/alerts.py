"""定时任务的企业微信群机器人提醒。"""

import logging

import httpx

from youju.config import get_settings

log = logging.getLogger("youju.alerts")
MAX_ALERT_BYTES = 1800


def send_alert(message: str) -> bool | None:
    """返回 True 表示已发送，None 表示未配置，False 表示发送失败。"""
    secret = get_settings().alert_webhook
    if not secret or not secret.get_secret_value():
        log.info("未配置提醒，跳过")
        return None
    webhook = secret.get_secret_value()

    content = "【有据】" + message
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_ALERT_BYTES:
        content = encoded[: MAX_ALERT_BYTES - len("…".encode())].decode("utf-8", "ignore") + "…"

    try:
        response = httpx.post(
            webhook,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10.0,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("errcode") != 0:
            log.error("提醒发送失败（机器人返回错误）")
            return False
    except Exception:  # 依赖库异常可能含完整 webhook，绝不能输出异常内容
        log.error("提醒发送失败（网络或响应异常）")
        return False
    return True
