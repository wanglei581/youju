"""联系客服：消息推送里用户发的客服消息回"转客服"，事件和配置检测照旧应答成功。

推送和回包的字段按微信官方文档（2026-10-19 用 curl 取回）：
- 《接收消息和事件》customer-message/receive.html：用户发的文本、图片、小程序卡片三种消息，
  进入会话事件 user_enter_tempsession；ToUserName 是小程序原始 ID，FromUserName 是用户 OpenID。
- 《转发客服消息》customer-message/trans.html：回包四项，ToUserName 填收到的 OpenID，
  FromUserName 填小程序原始 ID，CreateTime 整型，MsgType=transfer_customer_service。
- 《消息推送》server-ability/message-push.html：云托管配置检测的请求体；下面的账号是文档里的示例账号。
"""

import logging
from xml.etree import ElementTree

import pytest
from sqlalchemy import func, select

from tests.test_payments import HEADERS, NOW, create, order, payload, push
from tests.test_payments import setup_pay as setup_pay
from youju.models import PayEvent, PayOrder, WorkbenchPass

USER = "o9AgO5Kd5ggOC-bXrbNODIiE3bGY"
APP = "gh_97417a04a28d"
TRANSFERRED = {
    "ToUserName": USER,
    "FromUserName": APP,
    "CreateTime": int(NOW.timestamp()),
    "MsgType": "transfer_customer_service",
}
# 三种用户消息各自的字段，照文档示例
MESSAGES = {
    "text": {"Content": "导出的表格打不开", "MsgId": 1234567890123456},
    "image": {"PicUrl": "this is a url", "MediaId": "media_id", "MsgId": 1234567890123456},
    "miniprogrampage": {
        "MsgId": 1234567890123456,
        "Title": "某某申报通知",
        "AppId": "wx0000000000000000",
        "PagePath": "pages/doc/doc?id=12345",
        "ThumbUrl": "",
        "ThumbMediaId": "",
    },
}
ENTER = {
    "ToUserName": APP,
    "FromUserName": USER,
    "CreateTime": 1482048670,
    "MsgType": "event",
    "Event": "user_enter_tempsession",
    "SessionFrom": "youju|from=ai_home",
}


def message(kind, **changes):
    return {
        "ToUserName": APP,
        "FromUserName": USER,
        "CreateTime": 1482048670,
        "MsgType": kind,
        **MESSAGES.get(kind, {}),
        **changes,
    }


def push_xml(setup_pay, data):
    root = ElementTree.Element("xml")
    for key, value in data.items():
        ElementTree.SubElement(root, key).text = str(value)
    return setup_pay[0].post("/api/wx/push", headers=HEADERS, content=ElementTree.tostring(root))


def acknowledged(response, xml):
    """和改动前一样的"成功"应答：JSON 是 ErrCode 0，XML 是 <ErrCode>0</ErrCode>，都不带转客服。"""
    assert response.status_code == 200
    if xml:
        root = ElementTree.fromstring(response.content)
        return root.findtext("ErrCode") == "0" and root.find("MsgType") is None
    return response.json() == {"ErrCode": 0, "ErrMsg": "success"}


@pytest.mark.parametrize("kind", sorted(MESSAGES))
def test_user_message_json_is_transferred(setup_pay, session, kind, caplog):
    caplog.set_level(logging.DEBUG)
    response = push(setup_pay, message(kind))
    assert response.status_code == 200
    assert response.json() == TRANSFERRED
    # 不查单、不访问微信、不记支付事件
    assert not setup_pay[2].calls
    assert session.scalar(select(func.count(PayEvent.id))) == 0
    # 用户账号和消息内容不进日志
    assert USER not in caplog.text and "导出的表格" not in caplog.text


@pytest.mark.parametrize("kind", sorted(MESSAGES))
def test_user_message_xml_is_transferred(setup_pay, kind):
    response = push_xml(setup_pay, message(kind))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(response.content)
    assert root.tag == "xml"
    assert [(child.tag, child.text) for child in root] == [
        (key, str(value)) for key, value in TRANSFERRED.items()
    ]
    # 和文档示例一样，文字放在 CDATA 里
    assert b"<MsgType><![CDATA[transfer_customer_service]]></MsgType>" in response.content
    assert not setup_pay[2].calls


def test_other_user_message_types_are_transferred_too(setup_pay):
    # 文档现在只列了三种；以后微信让用户在客服会话里发别的类型，也是用户消息，照样转给客服
    assert push(setup_pay, message("voice", MediaId="media_id")).json() == TRANSFERRED


def test_transfer_does_not_depend_on_payment_switch(setup_pay):
    setup_pay[1].pay_enabled = False
    assert push(setup_pay, message("text")).json() == TRANSFERRED


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize(
    "data",
    [ENTER, {"action": "CheckContainerPath"}, {**ENTER, "Event": "debug_demo"}],
    ids=["enter-session", "container-check", "other-event"],
)
def test_events_and_container_check_are_acknowledged(setup_pay, session, xml, data):
    response = push_xml(setup_pay, data) if xml else push(setup_pay, data)
    assert acknowledged(response, xml)
    assert not setup_pay[2].calls
    assert session.scalar(select(func.count(PayEvent.id))) == 0


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"FromUserName": ""},
        {"ToUserName": None},
        {"FromUserName": "o]]><MsgType>text</MsgType>"},
        {"ToUserName": "gh_" + "a" * 200},
        {"MsgType": ""},
    ],
    ids=["no-user", "no-app", "odd-user", "long-app", "no-type"],
)
def test_message_without_valid_accounts_is_acknowledged(setup_pay, xml, changes, caplog):
    data = {key: value for key, value in message("text", **changes).items() if value is not None}
    response = push_xml(setup_pay, data) if xml else push(setup_pay, data)
    # 回包缺收发方就不是有效的转客服消息，照旧应答成功，不把收到的内容写回去
    assert acknowledged(response, xml)
    assert b"]]>" not in response.content and b"aaaa" not in response.content
    # 收发方不对时留一条警告（只有消息类型），上线后能从日志看出推送格式和文档不一样
    assert ("kf_transfer_skipped type=text" in caplog.text) == ("MsgType" not in changes)
    assert USER not in caplog.text and "导出的表格" not in caplog.text


@pytest.mark.parametrize("msg_type", ["event", "text"])
def test_payment_push_keeps_payment_handling(setup_pay, session, msg_type):
    # 支付通知照旧走支付流程：先看 Event，是支付事件就不看 MsgType，也不回转客服
    row = order(session, create(setup_pay))
    setup_pay[2].status = 2
    data = {**payload(row), "ToUserName": APP, "FromUserName": "pay-user", "MsgType": msg_type}
    response = push(setup_pay, data)
    assert response.json() == {"ErrCode": 0, "ErrMsg": "success"}
    session.expire_all()
    paid = session.get(PayOrder, row.id)
    assert paid.status == "completed" and paid.goods_notified
    assert session.scalar(select(func.count(WorkbenchPass.id))) == 1
    assert "xpay_goods_deliver_notify" in session.scalars(select(PayEvent.kind)).all()


@pytest.mark.parametrize(
    "kind",
    ["xpay_refund_notify", "xpay_complaint_notify", "xpay_subscribe_ios_refund_query_notify"],
)
def test_other_payment_events_never_transfer(setup_pay, kind):
    data = {
        "ToUserName": APP,
        "FromUserName": USER,
        "CreateTime": 1482048670,
        "MsgType": "event",
        "Event": kind,
        "MchOrderId": "unknown",
    }
    body = push(setup_pay, data).json()
    assert body["ErrCode"] == 0 and "MsgType" not in body
