"""联系客服：把用户在客服会话里发的消息转给客服人员（网页版小程序客服、"客服小助手"）。

小程序配了消息推送（云托管 → api 的推送路径）以后，用户发给客服的消息先推到我们这里；
要在回包里回 MsgType=transfer_customer_service 的消息，微信才把这条消息转给客服人员，
回"成功"的话消息就停在我们这里，客服看不到。官方文档《转发客服消息》：
https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/customer-message/trans.html
"""

import logging
import re

from fastapi.responses import JSONResponse, Response

from youju.api import v1

log = logging.getLogger("youju.api.contact")
TRANSFER = "transfer_customer_service"
# 用户的 OpenID、小程序原始 ID（gh_ 开头）都只由字母、数字、下划线和连字符组成；
# 回包要原样写回这两项，先查格式，回包里就混不进别的内容。
_ACCOUNT = re.compile(r"[A-Za-z0-9_-]{1,128}")


def transfer_to_kf(data: dict, xml: bool) -> Response | None:
    """用户发来的消息回"转客服"；不是用户消息的推送返回 None，由调用方照旧应答成功。

    微信的推送分两种：用户发的消息（MsgType 是 text、image、miniprogrampage 这类消息类型）
    和事件（MsgType=event，例如用户从客服按钮进入会话、支付通知）。文档写明只转用户发的消息，
    事件不转，否则客服会看到无意义的消息。配置检测（action=CheckContainerPath）没有 MsgType，
    也不转。
    """
    kind = data.get("MsgType")
    if not isinstance(kind, str) or not kind or kind == "event":
        return None
    # 回包的收件人是发消息的用户（收到的 FromUserName），发件人是小程序（收到的 ToUserName），
    # 正好对调
    user, app = data.get("FromUserName"), data.get("ToUserName")
    if not (
        isinstance(user, str)
        and _ACCOUNT.fullmatch(user)
        and isinstance(app, str)
        and _ACCOUNT.fullmatch(app)
    ):
        # 收到的推送和文档写的不一样才会走到这里：留一条警告（线上 api 只输出警告及以上），
        # 只记消息类型，不记账号和消息内容
        log.warning(
            "客服消息缺少收发方账号，没有转给客服（kf_transfer_skipped type=%s）", kind[:32]
        )
        return None
    created = int(v1.now().timestamp())
    if not xml:
        return JSONResponse(
            {"ToUserName": user, "FromUserName": app, "CreateTime": created, "MsgType": TRANSFER}
        )
    # XML 按文档示例的写法（文字放在 CDATA 里）；两个账号上面查过格式，带不进 "]]>" 这类内容
    return Response(
        f"<xml><ToUserName><![CDATA[{user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{app}]]></FromUserName>"
        f"<CreateTime>{created}</CreateTime>"
        f"<MsgType><![CDATA[{TRANSFER}]]></MsgType></xml>",
        media_type="application/xml",
    )
