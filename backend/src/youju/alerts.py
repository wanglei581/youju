"""定时任务的企业微信群机器人提醒。"""

import logging
import re
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from youju.config import get_settings

log = logging.getLogger("youju.alerts")
MAX_ALERT_BYTES = 1800


def probe_alert(*, send: bool = False, probe_id: str | None = None) -> dict:
    """默认只检查配置；显式发送时返回提交结果，接收人确认另行记录。"""
    if probe_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", probe_id):
        raise ValueError("探测标记须为6至64位字母、数字、下划线或连字符")
    settings = get_settings()
    secret = settings.alert_webhook
    configured = bool(secret and secret.get_secret_value().strip())
    result = {
        "role": settings.role,
        "checked_at": datetime.now(UTC).isoformat(),
        "configured": configured,
        "state": "not_tested" if configured else "not_configured",
        "received": False,
        "ready_for_release": False,
    }
    if not send or not configured:
        return result
    marker = probe_id or uuid4().hex[:16]
    result["probe_id"] = marker
    sent = send_alert(f"告警验收测试，标记 {marker}。这是受控测试，无需处理业务故障。")
    # 机器人接口成功只证明提交成功；不能据此宣称接收人已经收到。
    result["state"] = "submitted" if sent is True else "failed"
    return result


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
