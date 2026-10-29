"""问一问的对话接口（api/chat.py，开发设计 #328 第五节、第九节、第十节）。

找通知的规则在 test_chat_find.py 里测；这里把 chat_find 换成替身，只测接口：授权和同意、
只能动自己的、拦截的先后、请求编号重发、限频、删除、到期清理、注销和"我的数据"。
不向外网发请求：微信内容安全在测试里不调（trust_wx_headers 为假），要测拦下、出错时换成替身。
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, update

from youju import access_log, chat_store, safety_words, wx_security
from youju import advisor_tasks as service
from youju.agreements import CHAT_AGREEMENT_VERSION
from youju.api import advisor as advisor_api
from youju.api import app as app_module
from youju.api import chat, me, v1
from youju.db import get_session
from youju.models import (
    AdvisorTask,
    AdvisorUsage,
    AIChat,
    AIChatTurn,
    AISafetyHit,
    Document,
    DocumentMerge,
    Source,
    User,
)

NOW = datetime(2026, 10, 28, 2, tzinfo=UTC)
TEST_WORD = "测试违禁词甲"


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(
        advisor_enabled=False,
        ai_advisor_enabled=True,
        ai_advisor_openids="internal,second",
        ai_advisor_audience="whitelist",
        ai_advisor_daily_cap=200,
        ai_model="fake-model",
        ai_api_key=SecretStr("fake-test-value"),
        ai_base_url="https://fake.invalid",
        dev_openid="internal",
        trust_wx_headers=False,
        agreement_version="2026-10",
        agreement_min_version="",
        retain_events_days=90,
        retain_feedback_days=180,
        retain_used_subscriptions_days=30,
        retain_unused_subscriptions_days=365,
        cloud_env=None,
    )
    for module in (advisor_api, me, service, app_module, chat, wx_security):
        monkeypatch.setattr(module, "get_settings", lambda: value)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    return value


@pytest.fixture(autouse=True)
def library(monkeypatch):
    """定稿的测试词库（只有一个无害的测试词）；定稿以前问一问不开放。"""
    value = safety_words.parse(
        {
            "version": "test-1",
            "final": True,
            "categories": {"test": {"name": "测试类别"}},
            "entries": [{"id": "t1", "category": "test", "word": TEST_WORD}],
        }
    )
    monkeypatch.setattr(safety_words, "LIBRARY", value)
    monkeypatch.setattr(chat_store, "CHAT_TERMS_FINAL", True)  # 使用说明也当作定稿了
    chat._TIMES.clear()
    yield value
    chat._TIMES.clear()


class FakeFind:
    """找通知的替身：记下收到的句子，回答只带条件和编号（同真模块存的那一份）。"""

    def __init__(self):
        self.texts = []
        self.error = None
        self.before_return = None

    def run(self, session, text, *, today):
        self.texts.append(text)
        if self.error:
            raise self.error
        if self.before_return:
            self.before_return()
        return {
            "type": "find",
            "conditions": {"keywords": [text[:20]]},
            "total": 0,
            "ids": [],
            "link": False,
            "notes": [],
        }

    def render(self, session, stored, *, today):
        return {**stored, "items": [], "hidden": 0, "conditionsText": "测试条件"}

    def examples(self, session, *, now):
        return ["找湖北近 30 天的申报通知"]


@pytest.fixture
def find(monkeypatch):
    fake = FakeFind()
    monkeypatch.setattr(chat.chat_find, "run", fake.run)
    monkeypatch.setattr(chat.chat_find, "render", fake.render)
    monkeypatch.setattr(chat.chat_find, "examples", fake.examples)
    return fake


@pytest.fixture
def wechat(monkeypatch):
    """微信内容安全的替身：默认通过，记下查了哪些字。"""
    state = SimpleNamespace(calls=[], verdict=wx_security.Verdict(True, "pass", None), down=False)

    def check_text(openid, content, **kwargs):
        state.calls.append((openid, content))
        if state.down:
            raise wx_security.CheckDown("测试：接口出错")
        return state.verdict

    monkeypatch.setattr(wx_security, "check_text", check_text)
    return state


@pytest.fixture
def client(session_factory, settings, find, wechat):
    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    with TestClient(app_module.app) as c:
        agree(c)
        yield c
    app_module.app.dependency_overrides.clear()


def agree(c, chat_terms=True):
    assert c.post("/api/v1/me/agreement", json={"version": "2026-10"}).status_code == 200
    if chat_terms:
        response = c.post(
            "/api/v1/me/agreement", json={"version": CHAT_AGREEMENT_VERSION, "scope": "chat"}
        )
        assert response.status_code == 200, response.text


_KEYS = iter(range(10**6))


def send(c, text="专精特新", *, skill="find", doc_id=None, key=None):
    return c.post(
        "/api/v1/advisor/chat/turns",
        json={"skill": skill, "text": text, "docId": doc_id},
        headers={"Idempotency-Key": key or f"k-{next(_KEYS)}"},
    )


def code(response):
    detail = response.json()["detail"]
    return detail.get("code") if isinstance(detail, dict) else detail


def counts(session):
    session.expire_all()
    return (
        session.scalar(select(func.count(AIChat.id))),
        session.scalar(select(func.count(AIChatTurn.id))),
        session.scalar(select(func.count(AISafetyHit.id))),
    )


def make_doc(session, title="申报通知", url="https://official.test/1"):
    source = session.scalar(select(Source).where(Source.url == "https://official.test/policy/"))
    if source is None:
        source = Source(
            level="省级",
            province="山东",
            dept="科技",
            dept_full_name="科技厅",
            column_name="政策",
            url="https://official.test/policy/",
            parser_config={},
            rights_level=1,
        )
        session.add(source)
        session.flush()
    row = Document(
        source_id=source.id,
        title=title,
        kind_override="申报通知",
        url=url,
        url_key=url.removeprefix("https://"),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(row)
    session.commit()
    return row.id


def user_id(session, openid="internal"):
    session.expire_all()
    return session.scalar(select(User.id).where(User.openid == openid))


# ---- 开关、授权和同意 ----


def test_hidden_when_ai_off_or_not_listed(client, settings, session):
    """AI 关着、不在白名单里：发一问、取对话、示例都是 404，不说"未开放"。"""
    settings.ai_advisor_enabled = False
    for response in (
        send(client),
        client.get("/api/v1/advisor/chat"),
        client.get("/api/v1/advisor/chat/examples"),
    ):
        assert response.status_code == 404 and response.json()["detail"] == "没有找到接口"
    settings.ai_advisor_enabled = True
    settings.ai_advisor_openids = "second"
    assert send(client).status_code == 404
    assert client.get("/api/v1/advisor/chat").status_code == 404
    assert counts(session) == (0, 0, 0)


def test_hidden_until_word_list_final(client, monkeypatch, session):
    """本地高危词库定稿以前问一问不开放：接口 404，能力接口 chat 为假，同意也不收。"""
    draft = safety_words.load()
    assert draft.final is False and draft.version == "draft-0"
    monkeypatch.setattr(safety_words, "LIBRARY", draft)
    assert send(client).status_code == 404
    assert client.get("/api/v1/advisor/chat/examples").status_code == 404
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["chat"] is False
    response = client.post(
        "/api/v1/me/agreement", json={"version": CHAT_AGREEMENT_VERSION, "scope": "chat"}
    )
    assert response.status_code == 404
    assert counts(session) == (0, 0, 0)


def test_hidden_before_general_agreement(session_factory, settings, find, wechat):
    """白名单外的人没同意协议也只收到 404；白名单里的没同意协议是 403（同 AI 任务）。"""

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    try:
        with TestClient(app_module.app) as c:
            settings.dev_openid = "outsider"
            assert send(c).status_code == 404
            settings.dev_openid = "internal"
            response = send(c)
            assert response.status_code == 403
    finally:
        app_module.app.dependency_overrides.clear()


def test_chat_consent_required_and_version_bump(
    session_factory, settings, find, wechat, monkeypatch
):
    """没同意"问一问使用说明"回 403 CHAT_CONSENT；同意后能发；版本升了要重新同意。"""

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    try:
        with TestClient(app_module.app) as c:
            agree(c, chat_terms=False)
            response = send(c)
            assert response.status_code == 403 and code(response) == "CHAT_CONSENT"
            assert find.texts == [] and wechat.calls == []
            caps = c.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
            assert caps["chat"] is True and caps["chatConsent"] is False
            wrong = c.post("/api/v1/me/agreement", json={"version": "chat-old", "scope": "chat"})
            assert wrong.status_code == 422
            agree(c)
            assert c.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["chatConsent"]
            assert send(c).status_code == 200
            assert c.get("/api/v1/me").json()["chatAgreed"]["version"] == CHAT_AGREEMENT_VERSION
            monkeypatch.setattr(chat_store, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
            monkeypatch.setattr(me, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
            response = send(c)
            assert response.status_code == 403 and code(response) == "CHAT_CONSENT"
            again = c.post(
                "/api/v1/me/agreement", json={"version": "chat-2027-01-v1", "scope": "chat"}
            )
            assert again.status_code == 200
            assert send(c).status_code == 200
    finally:
        app_module.app.dependency_overrides.clear()


def test_same_key_after_terms_update_returns_original(
    session_factory, settings, find, wechat, monkeypatch
):
    """发出以后"问一问使用说明"或 AI 协议升了版：同一个请求编号重发拿得到原来那一问，不再交微信
    （Codex 10/28）；新的一问照旧 403。"""

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    try:
        with TestClient(app_module.app) as c:
            agree(c)
            first = send(c, key="before-update")
            assert first.status_code == 200
            calls = len(wechat.calls)
            monkeypatch.setattr(chat_store, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
            monkeypatch.setattr(me, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
            again = send(c, key="before-update")
            assert again.status_code == 200 and again.json() == first.json()
            assert code(send(c)) == "CHAT_CONSENT"
            settings.ai_advisor_audience = "all"  # 对所有人开放，这个人没同意含 AI 条款的协议
            again = send(c, key="before-update")
            assert again.status_code == 200 and again.json() == first.json()
            response = send(c)
            assert response.status_code == 403
            assert response.json()["detail"] == chat.AI_TERMS_UPDATED
            assert len(wechat.calls) == calls
            settings.ai_advisor_enabled = False  # AI 关了：照旧 404，重发也一样
            assert send(c, key="before-update").status_code == 404
    finally:
        app_module.app.dependency_overrides.clear()


def test_same_key_when_chat_closed_is_not_found(client, monkeypatch):
    """问一问关了（词库或使用说明不再算定稿）：同一个请求编号重发也是 404，不返回原来那一问。"""
    assert send(client, key="closed").status_code == 200
    monkeypatch.setattr(chat_store, "CHAT_TERMS_FINAL", False)
    assert send(client, key="closed").status_code == 404


def test_ai_terms_outdated_after_opening_to_all(client, settings, session):
    """对所有人开放后没同意含 AI 条款的协议：403，detail 同 AI 任务那句。"""
    settings.ai_advisor_audience = "all"
    response = send(client)
    assert response.status_code == 403
    assert response.json()["detail"] == chat.AI_TERMS_UPDATED
    assert client.get("/api/v1/advisor/chat").status_code == 403
    assert counts(session) == (0, 0, 0)


# ---- 找通知：建对话、取对话 ----


def test_find_creates_chat_and_reads_back(client, session, find, wechat):
    response = send(client, "  江苏 专精特新  ")
    assert response.status_code == 200, response.text
    body = response.json()
    turn = body["turn"]
    assert turn["seq"] == 1 and turn["skill"] == "find" and turn["status"] == "done"
    assert turn["question"] == "江苏 专精特新"  # 去掉首尾空白再存
    assert turn["counted"] is False and turn["error"] is None and turn["errorCode"] is None
    assert turn["answer"]["conditionsText"] == "测试条件"
    assert find.texts == ["江苏 专精特新"]
    assert wechat.calls == [("internal", "江苏 专精特新")]
    stored = session.scalar(select(AIChatTurn))
    assert stored.answer_json["type"] == "find" and "conditionsText" not in stored.answer_json
    assert stored.expires_at == NOW + timedelta(days=30)
    got = client.get("/api/v1/advisor/chat").json()["chat"]
    assert got["id"] == body["chatId"] and got["hasDoc"] is False and got["docId"] is None
    assert [t["id"] for t in got["turns"]] == [turn["id"]] and got["hasMore"] is False
    assert client.get(f"/api/v1/advisor/chat/turns/{turn['id']}").json()["turn"] == turn
    assert client.get(f"/api/v1/advisor/chats/{body['chatId']}").json()["chat"] == got
    items = client.get("/api/v1/advisor/chats").json()["items"]
    assert items == [
        {
            "id": body["chatId"],
            "hasDoc": False,
            "docId": None,
            "docTitle": None,
            "linkGone": None,
            "turns": 1,
            "lastAt": got["turns"][0]["createdAt"],
            "busy": False,
            "lastQuestion": "江苏 专精特新",
        }
    ]
    second = send(client, "近 30 天的申报通知").json()
    assert second["chatId"] == body["chatId"] and second["turn"]["seq"] == 2


def test_no_chat_yet(client):
    assert client.get("/api/v1/advisor/chat").json() == {"chat": None}
    assert client.get("/api/v1/advisor/chat?docId=5").json() == {"chat": None}
    assert client.get("/api/v1/advisor/chats").json() == {"items": [], "total": 0}


def test_examples(client):
    assert client.get("/api/v1/advisor/chat/examples").json() == {
        "examples": ["找湖北近 30 天的申报通知"]
    }


def test_doc_chat_needs_public_doc(client, session):
    """带通知新建对话时这份要是公开的；没有、不存在的回 404，不建对话。"""
    response = send(client, doc_id=999)
    # 带代码的 422：小程序把不带代码的 404 当成"AI 没开"
    assert response.status_code == 422 and code(response) == "CHAT_DOC_UNAVAILABLE"
    assert counts(session) == (0, 0, 0)
    doc = make_doc(session)
    response = send(client, doc_id=doc)
    assert response.status_code == 200
    chat_row = session.scalar(select(AIChat))
    assert chat_row.doc_key == doc
    got = client.get(f"/api/v1/advisor/chat?docId={doc}").json()["chat"]
    assert got["hasDoc"] is True and got["docId"] == doc and got["docTitle"] == "申报通知"


def test_doc_taken_down_chat_still_open(client, session):
    """通知下架了：对话照样能打开、能删，不给编号和标题；找通知照常能发。"""
    doc = make_doc(session)
    chat_id = send(client, doc_id=doc).json()["chatId"]
    session.get(Document, doc).taken_down_at = NOW
    session.commit()
    got = client.get(f"/api/v1/advisor/chats/{chat_id}").json()["chat"]
    assert got["hasDoc"] is True and got["docId"] is None and got["docTitle"] is None
    listed = client.get("/api/v1/advisor/chats").json()["items"][0]
    assert listed["docId"] is None and listed["docTitle"] is None
    mine = client.get("/api/v1/me/data").json()["aiChats"][0]
    assert mine["hasDoc"] is True and mine["docId"] is None and mine["docTitle"] is None
    assert send(client, doc_id=doc).json()["chatId"] == chat_id
    assert client.delete(f"/api/v1/advisor/chats/{chat_id}").status_code == 204


def test_same_group_same_chat(client, session):
    """同组两个编号进来是同一条对话；后来合并、两条并存时取最近的；拆开以后各是各的。"""
    a = make_doc(session, url="https://official.test/a")
    b = make_doc(session, url="https://official.test/b")
    c = make_doc(session, url="https://official.test/c")
    session.add(DocumentMerge(duplicate_id=b, canonical_id=a, active=True, match_key="t"))
    session.commit()
    first = send(client, doc_id=b).json()["chatId"]
    assert send(client, doc_id=a).json()["chatId"] == first
    assert client.get(f"/api/v1/advisor/chat?docId={a}").json()["chat"]["id"] == first
    # c 先单独建了一条，后来并进 a 这组：组里两条，取最近一问更新的那条（不是编号大的那条）
    third = send(client, doc_id=c).json()["chatId"]
    assert third != first
    session.add(DocumentMerge(duplicate_id=c, canonical_id=a, active=True, match_key="t"))
    session.get(AIChat, first).updated_at = NOW + timedelta(minutes=5)
    session.commit()
    assert client.get(f"/api/v1/advisor/chat?docId={a}").json()["chat"]["id"] == first
    assert client.get(f"/api/v1/advisor/chat?docId={c}").json()["chat"]["id"] == first
    assert send(client, doc_id=c).json()["chatId"] == first  # 接着用最近的那条
    session.get(AIChat, third).updated_at = NOW + timedelta(minutes=9)
    session.commit()
    assert client.get(f"/api/v1/advisor/chat?docId={b}").json()["chat"]["id"] == third
    # 拆开：b 不再和 a 同组；b 名下的那条还是它的，a 组里只剩 c 名下的
    session.get(DocumentMerge, b).active = False
    session.commit()
    assert client.get(f"/api/v1/advisor/chat?docId={b}").json()["chat"]["id"] == first
    assert client.get(f"/api/v1/advisor/chat?docId={a}").json()["chat"]["id"] == third
    # 两条都还能按编号打开
    assert client.get(f"/api/v1/advisor/chats/{first}").status_code == 200


def test_split_creates_new_chat(client, session):
    """拆开以后从原来的重复编号进来：按它新建一条，原来那条仍属于它自己的 doc_key。"""
    a = make_doc(session, url="https://official.test/a")
    b = make_doc(session, url="https://official.test/b")
    session.add(DocumentMerge(duplicate_id=b, canonical_id=a, active=True, match_key="t"))
    session.commit()
    first = send(client, doc_id=a).json()["chatId"]
    assert send(client, doc_id=b).json()["chatId"] == first
    session.get(DocumentMerge, b).active = False
    session.commit()
    new = send(client, doc_id=b).json()["chatId"]
    assert new != first
    assert {row.doc_key for row in session.scalars(select(AIChat))} == {a, b}


def test_paging(client, session, monkeypatch):
    monkeypatch.setattr(chat, "RATE_LIMIT", 10**6)
    for n in range(25):
        assert send(client, f"第{n}问").status_code == 200
    got = client.get("/api/v1/advisor/chat").json()["chat"]
    assert [t["seq"] for t in got["turns"]] == list(range(6, 26)) and got["hasMore"] is True
    older = client.get("/api/v1/advisor/chat?before=6").json()["chat"]
    assert [t["seq"] for t in older["turns"]] == list(range(1, 6)) and older["hasMore"] is False


# ---- 拦截：先后、不建行、违规记录 ----


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("13812345678 的申报通知", "mobile"),
        ("身份证 11010519491231002X", "id_card"),
        ("卡号 4111111111111111", "bank"),
        ("91350100M000100Y43 的名单", "credit_code"),
    ],
)
@pytest.mark.parametrize("skill", ["find", "ask"])
def test_sensitive_blocked(client, session, find, wechat, text, what, skill):
    """像有号码：两个技能都在本机先拦（422 CHAT_SENSITIVE，给 what）；不建行、不发微信、
    不记违规记录。找通知里像信用代码的提示去查名单；问这份通知的提示写明会发给 AI 模型。"""
    response = send(client, text, skill=skill)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CHAT_SENSITIVE" and detail["what"] == what
    if skill == "find" and what == "credit_code":
        assert "查名单" in detail["message"]
    else:
        assert "查名单" not in detail["message"]
    if skill == "ask":
        assert "AI 模型" in detail["message"]
    assert find.texts == [] and wechat.calls == [] and counts(session) == (0, 0, 0)


def test_local_word_list_blocks_and_records(client, session, find, wechat):
    """本地词库命中：422 CHAT_UNSAFE（不说是哪一层），记违规记录，不再交给微信。"""
    text = f"找{TEST_WORD}的通知"
    response = send(client, text)
    assert response.status_code == 422 and code(response) == "CHAT_UNSAFE"
    assert response.json()["detail"]["message"] == chat.UNSAFE
    assert wechat.calls == [] and find.texts == []
    hit = session.scalar(select(AISafetyHit))
    assert hit.uid == access_log.log_uid("internal", session.get_bind())
    assert hit.uid != "internal"
    assert (hit.kind, hit.stage, hit.layer) == ("find", "question", "local")
    assert hit.reason_json == {"category": "test", "entry": "t1", "version": "test-1"}
    assert hit.text == text and hit.document_id is None and hit.turn_id is None
    assert hit.expires_at == NOW + timedelta(days=190)
    assert counts(session) == (0, 0, 1)


def test_go_lists_not_sent_anywhere(client, session, find, wechat):
    """问某家企业在不在名单里：422 CHAT_GO_LISTS，不建行、不发微信、不记违规记录。"""
    response = send(client, "华为技术有限公司在不在专精特新名单里")
    assert response.status_code == 422 and code(response) == "CHAT_GO_LISTS"
    assert "查名单" in response.json()["detail"]["message"]
    assert wechat.calls == [] and find.texts == [] and counts(session) == (0, 0, 0)
    # 认不出的照常走自己的技能
    assert send(client, "找江苏今年的专精特新名单").status_code == 200


@pytest.mark.parametrize("suggest", ["risky", "review"])
def test_wechat_blocks_and_records(client, session, find, wechat, suggest):
    wechat.verdict = wx_security.Verdict(False, suggest, 20001)
    response = send(client, "专精特新")
    assert response.status_code == 422 and code(response) == "CHAT_UNSAFE"
    assert find.texts == []
    hit = session.scalar(select(AISafetyHit))
    assert (hit.layer, hit.reason_json) == ("wechat", {"suggest": suggest, "label": 20001})
    assert hit.text == "专精特新"
    assert counts(session) == (0, 0, 1)


def test_wechat_down(client, session, find, wechat):
    """内容安全接口出错：503 CHAT_CHECK_DOWN，不放行、不记违规记录。"""
    wechat.down = True
    response = send(client, "专精特新")
    assert response.status_code == 503 and code(response) == "CHAT_CHECK_DOWN"
    assert find.texts == [] and counts(session) == (0, 0, 0)


def test_hit_record_failure_still_blocks(client, session, find, wechat, monkeypatch, caplog):
    """违规记录写不进去也照样拦下；日志里只有类型，没有那句话。"""
    monkeypatch.setattr(chat, "AISafetyHit", lambda **kwargs: object())
    text = f"{TEST_WORD}第二句"
    with caplog.at_level(logging.WARNING):
        response = send(client, text)
    assert response.status_code == 422 and code(response) == "CHAT_UNSAFE"
    assert text not in caplog.text and TEST_WORD not in caplog.text


@pytest.mark.parametrize(
    ("skill", "text", "expected"),
    [
        ("find", "", "CHAT_TEXT"),
        ("find", "   ", "CHAT_TEXT"),
        ("find", "字" * 101, "CHAT_TEXT"),
        ("find", "字" * 100, None),
        ("ask", "字" * 201, "CHAT_TEXT"),
        ("ask", "字" * 200, "CHAT_DOC_REQUIRED"),  # 长度过了；问这份通知要带通知（PR-3）
    ],
)
def test_length(client, session, skill, text, expected):
    response = send(client, text, skill=skill)
    if expected is None:
        assert response.status_code == 200
    else:
        assert code(response) == expected
        assert response.status_code == 422


def test_rate_limit_counts_blocked(client, session, monkeypatch, find):
    """每人每分钟 20 问，两个技能合计，被拦下的也算；过了一分钟恢复。"""
    clock = [1000.0]
    monkeypatch.setattr(chat, "_monotonic", lambda: clock[0])
    for n in range(10):
        assert send(client, f"第{n}问").status_code == 200
    for _ in range(5):
        assert code(send(client, "13812345678")) == "CHAT_SENSITIVE"
    for _ in range(5):
        assert code(send(client, "问", skill="ask")) == "CHAT_DOC_REQUIRED"
    response = send(client, "第二十一问")
    assert response.status_code == 429 and code(response) == "CHAT_TOO_FAST"
    clock[0] += 61
    assert send(client, "又能发了").status_code == 200


def test_rate_limit_per_user(client, settings, monkeypatch):
    monkeypatch.setattr(chat, "_monotonic", lambda: 5.0)
    for n in range(20):
        assert send(client, f"第{n}问").status_code == 200
    assert code(send(client)) == "CHAT_TOO_FAST"
    settings.dev_openid = "second"
    agree(client)
    assert send(client).status_code == 200


# ---- 请求编号 ----


def test_same_key_returns_original_without_checks(client, session, find, wechat, monkeypatch):
    """同一个请求编号再发：原样返回，不再查限频、内容安全，也不再发给微信。"""
    first = send(client, "专精特新", key="same")
    assert first.status_code == 200
    calls = len(wechat.calls)
    monkeypatch.setattr(chat, "_monotonic", lambda: 7.0)
    for n in range(20):
        send(client, f"占满{n}")
    assert code(send(client)) == "CHAT_TOO_FAST"
    wechat.down = True
    find.error = RuntimeError("不该再找")
    again = send(client, "专精特新", key="same")
    assert again.status_code == 200 and again.json() == first.json()
    again = send(client, "  专精特新 ", key="same")  # 首尾空白不算不一样
    assert again.status_code == 200
    assert len(wechat.calls) == calls + 19  # 占满限频的 19 问；后面的重发一次都没发
    for body in (
        {"skill": "find", "text": "别的话", "docId": None},
        {"skill": "ask", "text": "专精特新", "docId": None},
    ):
        response = client.post(
            "/api/v1/advisor/chat/turns", json=body, headers={"Idempotency-Key": "same"}
        )
        assert response.status_code == 409


def test_same_key_other_doc_conflicts(client, session):
    doc = make_doc(session)
    assert send(client, key="d", doc_id=doc).status_code == 200
    assert send(client, key="d").status_code == 409
    other = make_doc(session, url="https://official.test/other")
    assert send(client, key="d", doc_id=other).status_code == 409
    assert send(client, key="d", doc_id=doc).status_code == 200


def test_same_key_across_chats(client, session):
    """按这个人所有对话里的请求编号找：换一条对话用同一个编号也认得出。"""
    doc = make_doc(session)
    assert send(client, key="x").status_code == 200
    assert send(client, key="x", doc_id=doc).status_code == 409
    assert counts(session) == (1, 1, 0)


def test_key_required(client):
    response = client.post(
        "/api/v1/advisor/chat/turns", json={"skill": "find", "text": "专精特新", "docId": None}
    )
    assert response.status_code == 422
    response = send(client, key="k" * 129)
    assert response.status_code == 422


def test_concurrent_same_key_creates_one(client, session, find):
    """两个一样的请求同时到：都过了检查，在用户锁里后到的拿到先建成的那一问。"""
    barrier = Barrier(2, timeout=10)
    find.before_return = barrier.wait
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: send(client, "同时到", key="race"), range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["turn"]["id"] == results[1].json()["turn"]["id"]
    assert counts(session) == (1, 1, 0)


def test_concurrent_different_keys_get_own_seq(client, session, find):
    barrier = Barrier(2, timeout=10)
    find.before_return = barrier.wait
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda n: send(client, f"并发{n}", key=f"c{n}"), range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert sorted(r.json()["turn"]["seq"] for r in results) == [1, 2]
    assert counts(session) == (1, 2, 0)


# ---- 只能动自己的 ----


def test_others_chats_are_not_found(client, settings, session):
    chat_id = send(client).json()["chatId"]
    turn_id = session.scalar(select(AIChatTurn.id))
    settings.dev_openid = "second"
    agree(client)
    for response in (
        client.get(f"/api/v1/advisor/chats/{chat_id}"),
        client.get(f"/api/v1/advisor/chat/turns/{turn_id}"),
        client.get(f"/api/v1/me/data/chats/{chat_id}"),
        client.delete(f"/api/v1/advisor/chats/{chat_id}"),
    ):
        assert response.status_code == 404
    assert client.get("/api/v1/me/data/chats").json() == {"items": [], "next": None}
    assert client.get("/api/v1/advisor/chats").json()["items"] == []
    assert client.get("/api/v1/advisor/chat").json() == {"chat": None}
    assert counts(session) == (1, 1, 0)
    # 不存在的编号也一样
    assert client.get("/api/v1/advisor/chats/99999").status_code == 404


@pytest.mark.parametrize("state", ["ai_off", "not_listed", "terms_outdated", "no_chat_terms"])
def test_own_chats_readable_and_deletable_regardless(client, settings, session, monkeypatch, state):
    """AI 关了、移出白名单、没同意新版协议、没同意问一问使用说明：自己的对话照样能列、看、删，
    "我的数据"里也列出（D-093 门槛第 8 条）。"""
    chat_id = send(client, "我的问题").json()["chatId"]
    turn_id = session.scalar(select(AIChatTurn.id))
    if state == "ai_off":
        settings.ai_advisor_enabled = False
    elif state == "not_listed":
        settings.ai_advisor_openids = "second"
    elif state == "terms_outdated":
        settings.agreement_version = "2026-12"
    else:
        monkeypatch.setattr(chat_store, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
    assert client.get("/api/v1/advisor/chats").json()["items"][0]["id"] == chat_id
    assert client.get(f"/api/v1/advisor/chats/{chat_id}").status_code == 200
    assert client.get(f"/api/v1/advisor/chat/turns/{turn_id}").status_code == 200
    data = client.get("/api/v1/me/data").json()
    assert data["aiChats"][0]["id"] == chat_id
    assert client.get("/api/v1/me/data/chats").json()["items"][0]["id"] == chat_id
    # 往后翻也照样能翻：位置在这条对话之后的翻到它，在它之前的翻不到
    for at, expected in ((datetime(2099, 1, 1, tzinfo=UTC), [chat_id]), (NOW - timedelta(1), [])):
        page = client.get("/api/v1/me/data/chats", params={"after": me._chat_cursor(at, 1)})
        assert [c["id"] for c in page.json()["items"]] == expected
    turns = client.get(f"/api/v1/me/data/chats/{chat_id}").json()["turns"]
    assert turns[0]["question"] == "我的问题"
    assert client.delete(f"/api/v1/advisor/chats/{chat_id}").status_code == 204
    assert counts(session) == (0, 0, 0)
    assert client.get(f"/api/v1/me/data/chats/{chat_id}").status_code == 404  # 删了的


# ---- 删除、问答任务、旧任务接口 ----


def _ask_turn(session, chat_id, *, status="queued", day_used=1):
    """手工放一问"问这份通知"和它的任务（PR-3 才从接口建），占了 ask、ask_try 各 1 次。"""
    uid = user_id(session)
    day = service.usage_day(NOW)
    task = AdvisorTask(
        user_id=uid,
        usage_source="free",
        kind="ask",
        status=status,
        input_json={"doc_id": 1},
        row_count=0,
        idempotency_key=f"chat-turn:{chat_id}-{status}",
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    session.add(task)
    for kind in ("ask", chat_store.ASK_TRY):
        row = session.scalar(
            select(AdvisorUsage).where(
                AdvisorUsage.user_id == uid, AdvisorUsage.kind == kind, AdvisorUsage.day == day
            )
        )
        if row is None:
            session.add(AdvisorUsage(user_id=uid, kind=kind, day=day, used_count=day_used))
        else:
            row.used_count += day_used
    session.flush()
    seq = session.scalar(select(func.max(AIChatTurn.seq)).where(AIChatTurn.chat_id == chat_id))
    turn = AIChatTurn(
        chat_id=chat_id,
        seq=(seq or 0) + 1,
        skill="ask",
        question="企业申报截止是哪天？",
        task_id=task.id,
        doc_id=1,
        idempotency_key=f"ask-{task.id}",
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    session.add(turn)
    session.commit()
    return task.id, turn.id


def _usage(session, kind):
    session.expire_all()
    return session.scalar(
        select(AdvisorUsage.used_count).where(
            AdvisorUsage.user_id == user_id(session), AdvisorUsage.kind == kind
        )
    )


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_chat_with_ask_in_flight(client, session, status):
    """有一问正在排队、处理也能当场删：退回 5 次那本，ask_try 不退，任务一起删。"""
    chat_id = send(client).json()["chatId"]
    task_id, turn_id = _ask_turn(session, chat_id, status=status)
    items = client.get("/api/v1/advisor/chats").json()["items"]
    assert items[0]["busy"] is True and items[0]["turns"] == 2
    turn = client.get(f"/api/v1/advisor/chat/turns/{turn_id}").json()["turn"]
    assert turn["status"] == status and turn["counted"] is False
    assert client.delete(f"/api/v1/advisor/chats/{chat_id}").status_code == 204
    session.expire_all()
    assert session.get(AdvisorTask, task_id) is None
    assert counts(session) == (0, 0, 0)
    assert _usage(session, "ask") == 0 and _usage(session, chat_store.ASK_TRY) == 1


def test_delete_chat_done_ask_keeps_count(client, session):
    chat_id = send(client).json()["chatId"]
    task_id, turn_id = _ask_turn(session, chat_id, status="done")
    turn = client.get(f"/api/v1/advisor/chat/turns/{turn_id}").json()["turn"]
    assert turn["status"] == "done" and turn["counted"] is True
    assert client.delete(f"/api/v1/advisor/chats/{chat_id}").status_code == 204
    assert _usage(session, "ask") == 1 and _usage(session, chat_store.ASK_TRY) == 1


def test_old_task_endpoints_ignore_ask(client, settings, session):
    """现有任务接口不管问答：列表不列、取、删、取消都是 404。"""
    settings.advisor_enabled = True
    chat_id = send(client).json()["chatId"]
    task_id, _ = _ask_turn(session, chat_id)
    assert client.get(f"/api/v1/advisor/tasks/{task_id}").status_code == 404
    assert client.post(f"/api/v1/advisor/tasks/{task_id}/cancel").status_code == 404
    assert client.delete(f"/api/v1/advisor/tasks/{task_id}").status_code == 404
    assert task_id not in [t["id"] for t in client.get("/api/v1/advisor/tasks").json()["items"]]
    session.expire_all()
    assert session.get(AdvisorTask, task_id) is not None


def test_capabilities(client, session):
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["chat"] is True and caps["chatConsent"] is True and caps["askTriesFull"] is False
    assert "ask" not in caps["features"]  # 单独线路（YOUJU_AI_ADVISOR_*）没配齐时不给
    uid = user_id(session)
    session.add(
        AdvisorUsage(
            user_id=uid, kind=chat_store.ASK_TRY, day=service.usage_day(NOW), used_count=20
        )
    )
    session.commit()
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["askTriesFull"] is True


# ---- 到期、清理、注销、我的数据 ----


def test_expired_turns_hidden_then_cleaned(client, session, settings):
    chat_id = send(client, "旧的问题").json()["chatId"]
    turn = session.scalar(select(AIChatTurn))
    turn.expires_at = NOW - timedelta(seconds=1)
    session.add(
        AISafetyHit(
            created_at=NOW - timedelta(days=191),
            uid="u",
            kind="find",
            stage="question",
            layer="local",
            text="旧的",
            expires_at=NOW - timedelta(days=1),
        )
    )
    session.add(
        AISafetyHit(
            created_at=NOW,
            uid="u",
            kind="find",
            stage="question",
            layer="local",
            text="新的",
            expires_at=NOW + timedelta(days=190),
        )
    )
    session.commit()
    assert client.get("/api/v1/advisor/chat").json() == {"chat": None}
    assert client.get("/api/v1/advisor/chats").json()["items"] == []
    assert client.get(f"/api/v1/advisor/chats/{chat_id}").status_code == 404
    assert client.get(f"/api/v1/advisor/chat/turns/{turn.id}").status_code == 404
    assert client.get("/api/v1/me/data").json()["aiChats"] == []
    result = service.cleanup_advisor(session, settings=settings, stamp=NOW)
    assert (result["ai_chat_turns"], result["ai_chats"], result["ai_safety_hits"]) == (1, 1, 1)
    assert counts(session) == (0, 0, 1)
    assert session.scalar(select(AISafetyHit.text)) == "新的"


def test_cleanup_keeps_live_chats(client, session, settings):
    send(client, "旧的问题")
    send(client, "新的问题")
    old = session.scalar(select(AIChatTurn).where(AIChatTurn.seq == 1))
    old.expires_at = NOW - timedelta(seconds=1)
    session.commit()
    result = service.cleanup_advisor(session, settings=settings, stamp=NOW)
    assert (result["ai_chat_turns"], result["ai_chats"]) == (1, 0)
    got = client.get("/api/v1/advisor/chat").json()["chat"]
    assert [t["question"] for t in got["turns"]] == ["新的问题"]
    # 接着问：编号接着往下排，不和到期删掉的撞
    assert send(client, "第三问").json()["turn"]["seq"] == 3


def test_cleanup_and_new_question_at_once(client, session, session_factory, monkeypatch):
    """每天的清理正删一条全到期的对话（锁着、还没提交）时，同一个人发来新一问：新一问走到锁对话行那一步
    就等着，清理提交以后再建一条新对话；不死锁、不报错、不丢（真 MySQL 上的锁，Codex 10/28）。"""
    old_chat = send(client, "旧的问题").json()["chatId"]
    session.execute(update(AIChatTurn).values(expires_at=NOW - timedelta(seconds=1)))
    session.commit()
    reached = Event()
    real = chat._chat_for

    def chat_for(s, user_id, doc_id, *, lock=False):
        if lock:
            reached.set()  # 下一句就是锁对话行
        return real(s, user_id, doc_id, lock=lock)

    monkeypatch.setattr(chat, "_chat_for", chat_for)
    with session_factory() as sweeper:
        assert chat_store.cleanup(sweeper, NOW) == {"ai_chat_turns": 1, "ai_chats": 1}
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(send, client, "新的问题")
            try:
                assert reached.wait(10)
                time.sleep(0.5)
                assert not pending.done()  # 在对话行的锁上等清理提交
            finally:
                sweeper.commit()
            response = pending.result(timeout=60)
    assert response.status_code == 200, response.text
    assert response.json()["chatId"] != old_chat
    session.expire_all()
    assert [t.question for t in session.scalars(select(AIChatTurn))] == ["新的问题"]
    assert counts(session) == (1, 1, 0)


def test_account_deletion_deletes_chats_keeps_hits(client, session, find):
    chat_id = send(client).json()["chatId"]
    _ask_turn(session, chat_id)
    assert send(client, f"含{TEST_WORD}").status_code == 422
    assert client.delete("/api/v1/me").status_code == 204
    assert counts(session) == (0, 0, 1)
    assert session.scalar(select(func.count(AdvisorTask.id))) == 0


def test_my_data(client, session, settings):
    """ "我的数据"里的对话（第十节第 8 条）：第一页摘要（对话编号、通知、几问、最近一问的时间），
    每一问另按对话取：只写技能、问题文字、时间、状态、算没算次数，不放回答。"""
    doc = make_doc(session)
    send(client, "不带通知的问题")
    send(client, "带通知的问题", doc_id=doc)
    chat_id = session.scalar(select(AIChat.id).where(AIChat.doc_key == doc))
    _ask_turn(session, chat_id, status="done")
    data = client.get("/api/v1/me/data").json()
    assert data["aiChatsTotal"] == 2 and data["aiChatTurnsTotal"] == 3
    assert data["aiChatsNext"] is None
    assert data["retention"]["aiChats"] == 30
    by_doc = {c["hasDoc"]: c for c in data["aiChats"]}
    assert by_doc[True] == {
        "id": chat_id,
        "hasDoc": True,
        "docId": doc,
        "docTitle": "申报通知",
        "turns": 2,
        "lastAt": by_doc[True]["lastAt"],
    }
    assert by_doc[False]["docId"] is None and by_doc[False]["turns"] == 1
    assert by_doc[False]["lastQuestion"] == "不带通知的问题"
    got = client.get(f"/api/v1/me/data/chats/{chat_id}").json()
    assert [t["question"] for t in got["turns"]] == ["带通知的问题", "企业申报截止是哪天？"]
    assert got["turns"][1] == {
        "seq": got["turns"][1]["seq"],
        "skill": "ask",
        "question": "企业申报截止是哪天？",
        "createdAt": got["turns"][1]["createdAt"],
        "status": "done",
        "counted": True,
    }
    assert got["total"] == 2 and got["range"] == {"from": 1, "to": 2} and got["hasMore"] is False
    for turn in got["turns"]:
        assert set(turn) == {"seq", "skill", "question", "createdAt", "status", "counted"}
    # 问答任务在对话里，不在批量工具、AI 记录那一栏重复列
    assert all(t["kind"] != "ask" for t in data["advisorTasks"])
    assert data["advisorTasksTotal"] == 0
    assert "chatAgreed" in data["user"]


def test_my_data_pages_turns_of_a_chat(client, session, monkeypatch):
    """一条对话的问不截断：一页 50 问，before 往前翻，每页给一共几问和这一页是第几到第几问
    （复制时写明"第 1–50 问，共 205 问"）；翻完就是全部（Codex 第 10 轮）。"""
    monkeypatch.setattr(chat, "RATE_LIMIT", 10**6)
    for n in range(205):
        send(client, f"第{n}问")
    data = client.get("/api/v1/me/data").json()
    assert data["aiChatTurnsTotal"] == 205 and data["aiChats"][0]["turns"] == 205
    chat_id = data["aiChats"][0]["id"]
    pages, before = [], None
    while True:
        params = {"before": before} if before else {}
        got = client.get(f"/api/v1/me/data/chats/{chat_id}", params=params).json()
        pages.append(got)
        if not got["hasMore"]:
            break
        before = got["turns"][0]["seq"]
    assert [(p["range"]["from"], p["range"]["to"]) for p in pages] == [
        (156, 205),
        (106, 155),
        (56, 105),
        (6, 55),
        (1, 5),
    ]
    assert all(p["total"] == 205 for p in pages)
    questions = [t["question"] for p in reversed(pages) for t in p["turns"]]
    assert questions == [f"第{n}问" for n in range(205)]
    assert client.get("/api/v1/me/data/chats/999999").status_code == 404


def test_my_data_pages_chats(client, session):
    """对话列表也不截断：一页 100 条，按最近一问从新到旧（同一时间按编号），next 往后翻，
    翻完就是全部，不重不漏（Codex 第 11 轮）。"""
    send(client, "专精特新")  # 不带通知的那条，最近一问是现在
    uid = user_id(session)
    base = NOW - timedelta(hours=2)
    for n in range(104):
        at = base - timedelta(minutes=n // 10)  # 每 10 条同一个时间：翻页边界落在同一时间的中间
        row = AIChat(user_id=uid, doc_key=10_000 + n, created_at=at, updated_at=at)
        session.add(row)
        session.flush()
        session.add(
            AIChatTurn(
                chat_id=row.id,
                seq=1,
                skill="find",
                question=f"第{n}条",
                idempotency_key=f"p-{n}",
                created_at=at,
                expires_at=at + timedelta(days=30),
            )
        )
    session.commit()
    data = client.get("/api/v1/me/data").json()
    assert data["aiChatsTotal"] == 105 and len(data["aiChats"]) == 100 and data["aiChatsNext"]
    page = client.get("/api/v1/me/data/chats", params={"after": data["aiChatsNext"]}).json()
    assert len(page["items"]) == 5 and page["next"] is None
    items = data["aiChats"] + page["items"]
    assert len({c["id"] for c in items}) == 105
    lasts = [c["lastAt"] for c in items]
    assert lasts == sorted(lasts, reverse=True)
    # 格式不对、位置大得离谱（不是上一页给的）
    for bad in ("坏的", "9" * 19 + "-1", "1-" + "9" * 19):
        assert client.get("/api/v1/me/data/chats", params={"after": bad}).status_code == 422


def test_my_data_full_pages_have_no_next(client, session, monkeypatch):
    """正好一页：50 问的对话不用往前翻，100 条对话不给 next。"""
    monkeypatch.setattr(chat, "RATE_LIMIT", 10**6)
    for n in range(50):
        send(client, f"第{n}问")
    chat_id = client.get("/api/v1/me/data").json()["aiChats"][0]["id"]
    got = client.get(f"/api/v1/me/data/chats/{chat_id}").json()
    assert (got["hasMore"], got["total"], got["range"]) == (False, 50, {"from": 1, "to": 50})
    uid = user_id(session)
    for n in range(99):
        at = NOW - timedelta(minutes=n + 1)
        row = AIChat(user_id=uid, doc_key=20_000 + n, created_at=at, updated_at=at)
        session.add(row)
        session.flush()
        session.add(
            AIChatTurn(
                chat_id=row.id,
                seq=1,
                skill="find",
                question=f"第{n}条",
                idempotency_key=f"f-{n}",
                created_at=at,
                expires_at=at + timedelta(days=30),
            )
        )
    session.commit()
    data = client.get("/api/v1/me/data").json()
    assert (data["aiChatsTotal"], len(data["aiChats"]), data["aiChatsNext"]) == (100, 100, None)


def test_my_data_leaves_out_expired_turns(client, session):
    """到期的问不列（第十节第 1 条）：对话里到期了一部分的，只数没到期的、范围按没到期的算；
    都到期了的不在列表里，取它 404。到期时刻正好是现在也算到期。"""
    chat_id = send(client, "第一问").json()["chatId"]
    send(client, "第二问")
    other = send(client, "带通知的", doc_id=make_doc(session)).json()["chatId"]
    session.execute(
        update(AIChatTurn)
        .where(AIChatTurn.question.in_(["第一问", "带通知的"]))
        .values(expires_at=NOW)
    )
    session.commit()
    data = client.get("/api/v1/me/data").json()
    assert [(c["id"], c["turns"]) for c in data["aiChats"]] == [(chat_id, 1)]
    assert (data["aiChatsTotal"], data["aiChatTurnsTotal"]) == (1, 1)
    got = client.get(f"/api/v1/me/data/chats/{chat_id}").json()
    assert [t["question"] for t in got["turns"]] == ["第二问"]
    assert (got["total"], got["range"]) == (1, {"from": 1, "to": 1})
    assert client.get(f"/api/v1/me/data/chats/{other}").status_code == 404


def test_last_question_truncated(client, session):
    text = "找" + "字" * 40
    send(client, text)
    item = client.get("/api/v1/advisor/chats").json()["items"][0]
    assert item["lastQuestion"] == text[:30] + "…"
    doc = make_doc(session)
    send(client, "带通知的", doc_id=doc)
    items = client.get("/api/v1/advisor/chats").json()["items"]
    assert "lastQuestion" not in items[0] and items[0]["docId"] == doc


# ---- 日志里没有问题文字 ----


def test_find_error_logs_no_text(client, session, find, caplog):
    secret = "一句只在这里出现的话"
    find.error = ValueError(f"出错了：{secret}")
    with caplog.at_level(logging.DEBUG):
        response = send(client, secret)
    assert response.status_code == 500
    assert secret not in caplog.text
    assert counts(session) == (0, 0, 0)


def test_audit_detail_has_no_text(client, monkeypatch):
    seen = []
    original = access_log.write

    def write(entry):
        seen.append(entry)
        return original(entry)

    monkeypatch.setattr(access_log, "write", write)
    send(client, "审计里不能有这句")
    entries = [e for e in seen if e["path"] == "/api/v1/advisor/chat/turns"]
    assert entries and all("审计里不能有这句" not in str(e) for e in entries)
    assert entries[-1]["detail"]["action"] == "chat_turn_create"


# ---- 线上账号的权限、迁移 ----


def _limited_client(limited, monkeypatch):
    monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

    def override():
        with limited() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    return TestClient(app_module.app)


def test_restricted_api_account_runs_chat(
    session, settings, find, wechat, restricted_api_factory, monkeypatch
):
    """按部署文件给 api 账号的授权：发一问、记违规记录、列、删对话、我的数据、注销都能做；
    违规记录 api 只能新增，改不了也删不了（同访问日志）。"""
    from sqlalchemy import delete, update
    from sqlalchemy.exc import DBAPIError

    with restricted_api_factory() as limited:
        try:
            with _limited_client(limited, monkeypatch) as c:
                agree(c)
                chat_id = send(c).json()["chatId"]
                assert code(send(c, f"含{TEST_WORD}")) == "CHAT_UNSAFE"
                assert c.get("/api/v1/advisor/chats").json()["items"][0]["id"] == chat_id
                assert c.get("/api/v1/me/data").json()["aiChatsTotal"] == 1
                assert c.delete(f"/api/v1/advisor/chats/{chat_id}").status_code == 204
                assert send(c).status_code == 200
                assert c.delete("/api/v1/me").status_code == 204
            for statement in (delete(AISafetyHit), update(AISafetyHit).values(text="改")):
                with limited() as s, pytest.raises(DBAPIError):
                    s.execute(statement)
        finally:
            app_module.app.dependency_overrides.clear()
    assert counts(session) == (0, 0, 1)


def test_restricted_api_before_chat_grants(
    session, settings, find, wechat, restricted_api_factory, monkeypatch
):
    """迁移后还没执行问一问的三行授权：没有对话的人照常看对话列表、我的数据，照常注销。"""
    with restricted_api_factory(new_grants=False) as limited:
        try:
            with _limited_client(limited, monkeypatch) as c:
                agree(c)
                assert c.get("/api/v1/advisor/chats").json() == {"items": [], "total": 0}
                assert c.get("/api/v1/me/data").json()["aiChats"] == []
                assert c.delete("/api/v1/me").status_code == 204
        finally:
            app_module.app.dependency_overrides.clear()
    assert user_id(session) is None


def test_migration_0043_downgrade_upgrade(db_engine, session):
    """降级先删问答任务和对话表、把类型约束收回去，别的任务和 0042 的违规记录表不动；再升级回来。"""
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text

    from youju.migrations import alembic_config

    from .conftest import TEST_DB_URL

    user = User(openid="m", agreement_version="2026-10", agreement_at=NOW, last_visited_at=NOW)
    session.add(user)
    session.flush()
    for kind in ("interpret", "ask"):
        session.add(
            AdvisorTask(
                user_id=user.id,
                usage_source="free",
                kind=kind,
                status="done",
                row_count=0,
                idempotency_key=kind,
                created_at=NOW,
                expires_at=NOW + timedelta(days=30),
            )
        )
    session.commit()
    session.close()
    config = alembic_config(TEST_DB_URL)
    # 0043 接在哪个版本后面（合并时随 main 改 down_revision，测试不用跟着改）
    command.downgrade(
        config, ScriptDirectory.from_config(config).get_revision("0043").down_revision
    )
    with db_engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        users = {c["name"] for c in inspect(conn).get_columns("users")}
        beats = {c["name"] for c in inspect(conn).get_columns("ai_heartbeat")}
        kinds = list(conn.scalars(text("SELECT kind FROM advisor_tasks")))
    assert not tables & {"ai_chats", "ai_chat_turns"} and "ai_safety_hits" in tables
    assert not users & {"chat_agreement_version", "chat_agreement_at"}
    assert "ask_ready" not in beats
    assert kinds == ["interpret"]
    command.upgrade(config, "head")
    with db_engine.connect() as conn:
        assert {"ai_chats", "ai_chat_turns", "ai_safety_hits"} <= set(
            inspect(conn).get_table_names()
        )


def test_question_text_only_read_where_allowed():
    """用户打的字（问题原文、违规记录里那句话）只在对话接口和"我的数据"里读出来；管理台、
    运维日报、团队的读日志工具都不碰这两张表（第十节第 6 条）。新加读取的地方要在这里写明理由。"""
    import re
    from pathlib import Path

    import youju

    root = Path(youju.__file__).parent
    # 只认代码里的用法（模型类名、带引号的表名），说明文字里提到表名不算
    pattern = re.compile(r"\bAIChatTurn\b|\bAISafetyHit\b|[\"']ai_(?:chat_turns|safety_hits)[\"']")
    allowed = {
        "models.py",  # 表的定义
        "api/chat.py",  # 本人看、删自己的对话；写违规记录
        "api/me.py",  # 我的数据（本人）
        "chat_store.py",  # 删对话、注销、到期清理
        "public_snapshot.py",  # 写明不导出
        # 下面几处只碰违规记录的层、原因、编号和到期时间，不读那句话（text）
        "ai/result_safety.py",  # AI 结果被拦时写记录（不存文字）
        "ai/ask.py",  # 处理问这份通知：读这一问和前两问的问题交给模型，按编号核对这一问还在
        "advisor_tasks.py",  # 到期删除、1 天内不再查
        "digest.py",  # 运维日报只报次数和通知编号
    }
    used = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert used - allowed == set()
    tools = Path(__file__).parents[2] / "tools"
    for path in tools.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".js", ".cjs", ".sh"):
            assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path


def test_chat_consent_hidden_without_identity(session_factory, settings, find, wechat):
    """同意"问一问使用说明"的接口：没有身份时也是 404，不暴露问一问（D-075）。"""

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    try:
        with TestClient(app_module.app) as c:
            settings.dev_openid = ""
            response = c.post(
                "/api/v1/me/agreement", json={"version": CHAT_AGREEMENT_VERSION, "scope": "chat"}
            )
            assert response.status_code == 404
    finally:
        app_module.app.dependency_overrides.clear()


def test_hidden_until_chat_terms_final(client, monkeypatch, session):
    """词库定稿了、《问一问使用说明》还没定稿：问一问照样关着（两样各管各的）。"""
    monkeypatch.setattr(chat_store, "CHAT_TERMS_FINAL", False)
    assert send(client).status_code == 404
    assert client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["chat"] is False
    assert counts(session) == (0, 0, 0)


def test_daily_report_counts_question_blocks(client, session, settings):
    """运维日报：问一问的提问被拦单独报次数（没有原文可看），不混进 AI 结果被拦的那一句。"""
    from youju.digest import build_digest, digest_text

    assert code(send(client, f"找{TEST_WORD}的通知")) == "CHAT_UNSAFE"
    lines = digest_text(build_digest(session, now=NOW + timedelta(hours=1)), []).splitlines()
    [line] = [line for line in lines if line.startswith("内容安全")]
    assert "问一问拦下提问 1 次" in line and "看一眼原文" not in line
    assert TEST_WORD not in line
