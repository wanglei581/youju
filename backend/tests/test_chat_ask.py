"""问这份通知（ai/ask.py、api/chat.py 的 skill=ask；问一问开发设计 #328 第十五节 PR-3 验收）。

全程用假的模型（FakeAsk，按 8 个字一段模拟流式）和假的微信内容安全，不向外网发请求。admin 的
处理照真的走 advisor-run 领任务的流程（run_advisor_tasks），只把模型换掉。
"""

import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from youju import access_log, attachment_text, chat_store, safety_words, wx_security
from youju import advisor_tasks as service
from youju.agreements import CHAT_AGREEMENT_VERSION
from youju.ai import ask, content, heartbeat, interpret
from youju.ai.client import AIError
from youju.api import advisor as advisor_api
from youju.api import app as app_module
from youju.api import chat, me, v1
from youju.db import get_session
from youju.models import (
    AdvisorTask,
    AdvisorUsage,
    AIChat,
    AIChatTurn,
    AIHeartbeat,
    AISafetyHit,
    AIUsage,
    Document,
    DocumentAttachment,
    DocumentDeadline,
    DocumentDetail,
    Source,
    User,
)

NOW = datetime(2026, 10, 28, 2, tzinfo=UTC)
TEST_WORD = "测试违禁词甲"
S1 = "一、申报对象为本省注册的科技型中小企业，成立时间不少于1年。"
S2 = "二、企业申报截止时间为2026年11月20日17时。"
S3 = "三、各市科技局于2026年11月30日前完成审核并报送省厅。"
S4 = "四、申报材料详见附件1。"
S5 = "五、具体要求按照有关规定执行。"
S6 = "六、联系人：张三，联系电话：0531-12345678。"  # 入库后遮掉号码
S7 = "七、本通知自发布之日起施行。"
BODY = "\n".join((S1, S2, S3, S4, S5, S6, S7))


# ---- 夹具 ----


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(
        advisor_enabled=False,
        ai_advisor_enabled=True,
        ai_advisor_openids="internal,second",
        ai_advisor_audience="whitelist",
        ai_advisor_daily_cap=200,
        ai_advisor_concurrency=1,
        ai_model="fake-model",
        ai_api_key=SecretStr("fake-test-value"),
        ai_base_url="https://fake.invalid",
        # 对外 AI 的单独线路（问这份通知只走它）
        ai_advisor_base_url="https://advisor.invalid",
        ai_advisor_api_key=SecretStr("fake-advisor-value"),
        ai_advisor_model="advisor-model",
        ai_advisor_stream=True,  # 问这份通知只在流式开着时开放（第九节第 6 条）
        ai_model_label="测试模型",
        ai_model_filing="测试备案号",
        role="all",
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


def word_list(monkeypatch, *words, version="test-1"):
    library = safety_words.parse(
        {
            "version": version,
            "final": True,
            "categories": {"test": {"name": "测试类别"}},
            "entries": [
                {"id": f"t{n}", "category": "test", "word": word} for n, word in enumerate(words, 1)
            ],
        }
    )
    monkeypatch.setattr(safety_words, "LIBRARY", library)


@pytest.fixture(autouse=True)
def library(monkeypatch):
    """定稿的测试词库（只有一个无害的测试词）、使用说明也当作定稿了：问一问开着。"""
    word_list(monkeypatch, TEST_WORD)
    monkeypatch.setattr(chat_store, "CHAT_TERMS_FINAL", True)
    monkeypatch.setattr(chat, "RATE_LIMIT", 1000)
    monkeypatch.setattr("youju.ai.result_safety._pause", lambda seconds: None)
    chat._TIMES.clear()
    yield
    chat._TIMES.clear()


def answer(*pairs):
    return {"status": "answer", "quotes": [{"sid": sid, "quote": quote} for sid, quote in pairs]}


class FakeAsk:
    """假的模型：记下收到的提示词和输入；按 chunk 个字一段把回复交给 on_text（模拟流式）。"""

    model = "advisor-model"
    stream = True  # 按流式算（处置记录里写不写 stream 看它）

    def __init__(self):
        self.payload = answer(("P002", "企业申报截止时间为2026年11月20日"))
        self.error = None
        self.before_return = None
        self.chunk = 8
        self.calls = []
        self.streamed = []
        self.totals = dict(calls=0, errors=0, prompt_tokens=0, completion_tokens=0)

    def complete_json(self, system, user, max_tokens=800, *, on_text=None):
        self.calls.append((system, json.loads(user), max_tokens))
        self.totals["calls"] += 1
        self.totals["prompt_tokens"] += 10
        if self.before_return:
            self.before_return()
        if self.error:
            raise self.error
        reply = json.dumps(self.payload, ensure_ascii=False)
        for end in range(self.chunk, len(reply) + self.chunk, self.chunk):
            self.streamed.append(reply[:end])
            if on_text:
                on_text(reply[:end])
        return copy.deepcopy(self.payload)

    def close(self):
        pass


@pytest.fixture
def model(monkeypatch):
    fake = FakeAsk()
    monkeypatch.setattr(ask, "ChatClient", lambda *args, **kwargs: fake)
    return fake


@pytest.fixture
def wechat(monkeypatch):
    """微信内容安全的替身：默认通过；outcomes 里放好接下来几次的结果（Verdict 或 CheckDown）。"""
    state = SimpleNamespace(calls=[], outcomes=[], verdict=wx_security.Verdict(True, "pass", None))

    def check_text(openid, text, **kwargs):
        state.calls.append((openid, text))
        if state.outcomes:
            outcome = state.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return state.verdict

    monkeypatch.setattr(wx_security, "check_text", check_text)
    return state


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda text: sent.append(text) or True)
    return sent


@pytest.fixture
def client(session_factory, settings, wechat):
    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    with TestClient(app_module.app) as c:
        assert c.post("/api/v1/me/agreement", json={"version": "2026-10"}).status_code == 200
        response = c.post(
            "/api/v1/me/agreement", json={"version": CHAT_AGREEMENT_VERSION, "scope": "chat"}
        )
        assert response.status_code == 200, response.text
        yield c
    app_module.app.dependency_overrides.clear()


def make_doc(
    session,
    *,
    body=BODY,
    url="https://official.test/1",
    title="关于开展2026年科技项目申报的通知",
    kind="申报通知",
):
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
        kind_override=kind,
        url=url,
        url_key=url.removeprefix("https://"),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(row)
    session.flush()
    session.add(DocumentDetail(document_id=row.id, body_text=body, fetched_at=NOW, status="ok"))
    session.commit()
    return row.id


@pytest.fixture
def doc(session):
    return make_doc(session)


_KEYS = iter(range(10**6))


def send(c, text="企业申报截止是哪天？", doc_id=None, *, key=None):
    return c.post(
        "/api/v1/advisor/chat/turns",
        json={"skill": "ask", "text": text, "docId": doc_id},
        headers={"Idempotency-Key": key or f"a-{next(_KEYS)}"},
    )


def code(response):
    detail = response.json()["detail"]
    return detail.get("code") if isinstance(detail, dict) else detail


def work(session, settings):
    """admin 的 advisor-run 领一轮（把排着的都处理完）。"""
    result = service.run_advisor_tasks(session, settings=settings, stamp=NOW)
    session.expire_all()
    return result


def turn_of(c, turn_id):
    response = c.get(f"/api/v1/advisor/chat/turns/{turn_id}")
    assert response.status_code == 200, response.text
    return response.json()["turn"]


def asked(c, session, settings, model, text, doc_id, payload=None):
    """发一问、admin 处理完，返回这一问的现状。"""
    if payload is not None:
        model.payload = payload
    response = send(c, text, doc_id)
    assert response.status_code == 202, response.text
    work(session, settings)
    return turn_of(c, response.json()["turn"]["id"])


def user_id(session, openid="internal"):
    session.expire_all()
    return session.scalar(select(User.id).where(User.openid == openid))


def usage(session, kind):
    session.expire_all()
    return session.scalar(
        select(func.coalesce(func.sum(AdvisorUsage.used_count), 0)).where(AdvisorUsage.kind == kind)
    )


def set_usage(session, kind, count):
    uid, day = user_id(session), service.usage_day(NOW)
    row = session.scalar(
        select(AdvisorUsage).where(
            AdvisorUsage.user_id == uid, AdvisorUsage.kind == kind, AdvisorUsage.day == day
        )
    )
    if row is None:
        session.add(AdvisorUsage(user_id=uid, kind=kind, day=day, used_count=count))
    else:
        row.used_count = count
    session.commit()


def counts(session):
    session.expire_all()
    return tuple(
        session.scalar(select(func.count(table.id)))
        for table in (AIChat, AIChatTurn, AdvisorTask, AISafetyHit)
    )


def set_body(session_factory, doc_id, body):
    with session_factory() as s:
        s.get(DocumentDetail, doc_id).body_text = body
        s.commit()


def hits(session):
    session.expire_all()
    return list(session.scalars(select(AISafetyHit).order_by(AISafetyHit.id)))


# ---- 规则（不碰数据库） ----


@pytest.mark.parametrize(
    ("question", "back"),
    [
        ("刚才那句是什么意思", True),
        ("上面说的材料要盖章吗", True),
        ("你列的第二句呢", True),
        ("第 2 句里的时间", True),  # 第 N 句（中间有空格也认）
        ("这一句是什么意思", True),
        ("回答里的截止是谁的", True),
        ("第五条是什么", False),  # 政策原文自己就有"第五条"
        ("这条通知什么时候截止", False),
        ("第3个条件是什么", False),
    ],
)
def test_refers_back_only_on_the_listed_words(question, back):
    assert ask.refers_back(question) is back


def test_nth_and_drafting():
    assert ask.nth("刚才第五句呢") == 5 and ask.nth("第 12 句") == 12 and ask.nth("第十一句") == 11
    assert ask.nth("刚才那句") is None
    # 先认"第 N 句"，没有再认"第 N 条""第 N 个"；第一批、第二季度这类不算
    assert ask.nth("上面第2条第3句") == 3 and ask.nth("刚才第三条呢") == 3
    assert ask.nth("你列的第2个") == 2
    assert ask.nth("刚才第3项呢") == 3 and ask.nth("你说的第二点要盖章吗") == 2
    assert ask.nth("刚才说的第一批申报截止哪天") is None and ask.nth("上面第二季度的呢") is None
    for text in ("帮我写一份申报书", "替我写申请书", "起草个方案", "润色一下自评报告", "代写材料"):
        assert ask.drafting(text), text
    # 要紧挨着才算：问要交什么的不挡
    for text in ("帮我写一下要交的材料", "申报书怎么写", "要交哪些材料"):
        assert not ask.drafting(text), text


def test_changed_compares_config_sources_and_segments():
    base = {"config": "body-v1", "sources": {"body": "v1"}, "segments": "s1"}
    assert not ask.changed(base, dict(base))
    assert ask.changed(base, {**base, "config": "attach-v1"})
    assert ask.changed(base, {**base, "segments": "s2"})
    assert ask.changed(base, {**base, "sources": {"body": "v2"}})
    # 没读的附件变了（不在读了的来源里）不算
    assert not ask.changed(base, {**base, "sources": {"body": "v1", "att-x": "a"}})
    assert ask.changed(None, base) and ask.changed({"config": "body-v1"}, base)


def _texts(*sentences):
    """只有正文的内容（段号 P001 起，同 content.load）。"""
    return content.Texts.from_paragraphs(interpret.split_paragraphs("\n".join(sentences)))


def test_validate_expands_dedups_sorts_and_caps():
    lines = [f"第{n}条、申报单位须满足第{n}项要求。" for n in range(1, 11)]
    texts = _texts(*lines)
    quotes = [{"sid": "P010", "quote": "申报单位须满足第10项要求"}] + [
        {"sid": f"P{n:03d}", "quote": f"申报单位须满足第{n}项要求"} for n in range(1, 9)
    ]
    result = ask.validate({"status": "answer", "quotes": quotes}, texts)
    # 超过 8 句只取模型给的前 8 句，按原文先后排、编号；扩成完整的句子
    assert [q["sid"] for q in result["quotes"]] == [f"P{n:03d}" for n in range(1, 8)] + ["P010"]
    assert [q["n"] for q in result["quotes"]] == list(range(1, 9))
    assert result["quotes"][0]["text"] == lines[0] and result["quotes"][0]["source"] == "body"
    # 重复的、段号对不上的、太短的、格式不对的都不要
    result = ask.validate(
        {
            "status": "answer",
            "quotes": [
                {"sid": "P002", "quote": "申报单位须满足第2项要求"},
                {"sid": "P002", "quote": "第2条、申报单位须满足"},
                {"sid": "P999", "quote": "申报单位须满足第3项要求"},
                {"sid": "P003", "quote": "第3条"},
                "坏的",
            ],
        },
        texts,
    )
    assert [q["sid"] for q in result["quotes"]] == ["P002"] and result["dropped"] == 4
    # 其他三种状态时模型给的句子一律不用
    for status in ("not_found", "out_of_scope", "clarify"):
        got = ask.validate({"status": status, "quotes": quotes}, texts)
        assert got["status"] == status and got["quotes"] == []
    # answer 但给的句子一句都没核对上：unverified（摘录失败，不是没写，D-105）；
    # 句子列表是空的：not_found（Codex 第 11 轮）
    got = ask.validate(
        {"status": "answer", "quotes": [{"sid": "P001", "quote": "原文里没有"}]}, texts
    )
    assert got["status"] == "unverified" and got["quotes"] == []
    got = ask.validate({"status": "answer", "quotes": []}, texts)
    assert got["status"] == "not_found" and got["quotes"] == []
    for bad in (None, [], {"status": "yes", "quotes": []}, {"status": "answer", "quotes": "x"}):
        with pytest.raises(ValueError):
            ask.validate(bad, texts)


def test_validate_skips_redacted_and_counts_them():
    texts = _texts(S1, v1.redact_sensitive(S6))
    result = ask.validate(
        answer(("P002", "联系电话：[电话已脱敏]"), ("P001", "申报对象为本省注册的科技型中小企业")),
        texts,
    )
    assert [q["sid"] for q in result["quotes"]] == ["P001"] and result["redacted"] == 1
    assert result["status"] == "answer"  # 有能显示的：answer，含号码的另写个数


def test_validate_status_order_without_shown_quotes():
    """没有能显示的原句时按核对结果定状态（D-105，Codex 第 11 轮的先后）：都含遮掉的号码 →
    redacted；一句对不上、一句对上但含号码 → 也是 redacted；都对不上 → unverified。"""
    texts = _texts(S1, v1.redact_sensitive(S6))
    masked = ("P002", "联系电话：[电话已脱敏]")
    got = ask.validate(answer(masked), texts)
    assert (got["status"], got["quotes"], got["redacted"]) == ("redacted", [], 1)
    got = ask.validate(answer(("P001", "原文里没有的一句话"), masked), texts)
    assert (got["status"], got["redacted"], got["dropped"]) == ("redacted", 1, 1)
    got = ask.validate(answer(("P001", "原文里没有的一句话"), ("P009", "段号也不对的一句")), texts)
    assert (got["status"], got["redacted"], got["dropped"]) == ("unverified", 0, 2)


def test_validate_only_looks_in_the_quotes_own_source():
    """每句只在它的段号所属来源里逐字找：附件里的句子标成正文段号对不上，反过来也一样。"""
    body = S1
    attachment = "评分标准：创新能力占40分。"
    texts = content.Texts(
        sources=(
            content.SourceText(key="body", label="正文", version="b", text=body),
            content.SourceText(key="att-1", label="附件 1《指南》", version="a", text=attachment),
        ),
        segments=(
            content.Segment("P001", "body", body, 0, len(body)),
            content.Segment("A1-1", "att-1", attachment, 0, len(attachment), place="第 1 段"),
        ),
    )
    result = ask.validate(
        answer(
            ("P001", "评分标准：创新能力占40分"),
            ("A1-1", "申报对象为本省注册的科技型中小企业"),
            ("A1-1", "评分标准：创新能力占40分"),
        ),
        texts,
    )
    assert [(q["sid"], q["source"], q["version"]) for q in result["quotes"]] == [
        ("A1-1", "att-1", "a")
    ]
    assert result["quotes"][0]["place"] == "第 1 段" and result["dropped"] == 2


# ---- 发一问：建任务、处理、读 ----


def test_ask_creates_task_then_finds_quotes(client, session, settings, doc, model, wechat):
    text = "企业申报截止是哪天？"
    response = send(client, text, doc, key="first")
    assert response.status_code == 202, response.text
    turn = response.json()["turn"]
    assert (turn["skill"], turn["status"], turn["queuePosition"]) == ("ask", "queued", 0)
    assert turn["counted"] is False and turn["answer"] is None and turn["etaSeconds"] > 0
    session.expire_all()
    row = session.get(AIChatTurn, turn["id"])
    task = session.scalar(select(AdvisorTask))
    # 建任务时必填列齐全；任务输入只有编号，问题文字只在这一问里
    assert (task.kind, task.status, task.row_count, task.usage_source) == (
        "ask",
        "queued",
        0,
        "free",
    )
    assert task.input_json == {"doc_id": doc, "turn_id": row.id}
    assert task.idempotency_key == f"chat-turn:{row.id}"
    assert task.expires_at == NOW + timedelta(days=30) == row.expires_at
    assert (row.task_id, row.doc_id, row.answer_json, row.question) == (task.id, doc, None, text)
    assert usage(session, "ask") == 1 and usage(session, chat_store.ASK_TRY) == 1
    assert wechat.calls == [("internal", text)]
    # 同一个请求编号再发：原样返回那一问，不再建、不再占、不再发给微信
    again = send(client, text, doc, key="first")
    assert again.status_code == 202 and again.json()["turn"]["id"] == row.id
    assert counts(session)[2] == 1 and usage(session, "ask") == 1 and len(wechat.calls) == 1
    assert send(client, "别的问题", doc, key="first").status_code == 409

    work(session, settings)
    got = turn_of(client, row.id)
    assert got["status"] == "done" and got["counted"] is True and got["errorCode"] is None
    result = got["answer"]
    session.expire_all()
    task = session.get(AdvisorTask, task.id)
    assert result["status"] == "answer" and result["by"] == "model"
    assert result["lead"] == "摘出了原文里和你问的有关的 1 句（可能没摘全，以原文为准）："
    assert result["quotes"] == [
        {
            "n": 1,
            "sid": "P002",
            "source": "body",
            "version": task.snapshot["versions"]["sources"]["body"],
            "label": "正文",
            "text": S2,  # 扩成了完整的句子
            "reader": {"docId": doc, "attachmentId": None},
            "unreadable": False,
        }
    ]
    assert result["scope"] == {
        "text": "读了：正文（这份通知没有附件）",
        "partial": False,
        "unread": [],
    }
    assert result["notice"] == interpret.NOTICE and result["footer"] == ask.FOOTER
    assert result["disclosure"] == {"modelLabel": "测试模型", "filingNo": "测试备案号"}
    assert result["linkGone"] is None and result["redacted"] == 0
    # 交给模型的：ask-v1 的系统提示词，段落在前、问题在后
    system, sent, _ = model.calls[0]
    assert system == ask.SYSTEM_PROMPT and list(sent) == [
        "read",
        "paragraphs",
        "recent",
        "previousAnswer",
        "question",
    ]
    assert sent["question"] == text and sent["recent"] == [] and sent["previousAnswer"] is None
    assert sent["read"] == "读了：正文（这份通知没有附件）"
    assert sent["paragraphs"][1] == {"sid": "P002", "text": S2}
    # 问题文字不进任务的输入、快照、结果
    for value in (task.input_json, task.snapshot, task.result_json):
        assert text not in json.dumps(value, ensure_ascii=False)
    assert (task.model, task.prompt_version, task.result_rows) == ("advisor-model", "ask-v1", 1)
    assert task.snapshot["doc_id"] == doc and task.snapshot["prev"] is None
    # 找到原句：保留这 1 次；全站调用记 1 次；要显示的原句交微信复核
    assert usage(session, "ask") == 1 and usage(session, chat_store.ASK_TRY) == 1
    assert session.scalar(select(func.sum(AIUsage.calls)).where(AIUsage.task == "ask")) == 1
    assert wechat.calls[-1] == ("internal", S2)
    assert task.result_json["safety"] == {"words": "test-1", "wechat": False}
    # 取对话（按通知、按对话编号）出的是同一个回答
    chat_id = response.json()["chatId"]
    by_doc = client.get("/api/v1/advisor/chat", params={"docId": doc}).json()["chat"]
    by_id = client.get(f"/api/v1/advisor/chats/{chat_id}").json()["chat"]
    assert by_doc["turns"][0] == by_id["turns"][0] == got


def test_submit_refusals_create_nothing(client, session, settings, doc, wechat):
    """提交前的规则：没带通知、这份不能问（名单、下架、原文链接失效）、后台处理不了。都不建行、
    不扣次数、不发给微信。"""
    response = send(client, "截止是哪天", None)
    assert response.status_code == 422 and code(response) == "CHAT_DOC_REQUIRED"
    assert response.json()["detail"]["message"] == ask.NO_DOC
    listing = make_doc(session, url="https://official.test/list", title="入选名单", kind="名单")
    response = send(client, "截止是哪天", listing)
    assert code(response) == "AI_NOT_ELIGIBLE" and response.json()["detail"]["reason"] == "list"
    gone = make_doc(session, url="https://official.test/gone")
    taken = make_doc(session, url="https://official.test/taken")
    session.get(Document, gone).link_gone_at = datetime(2026, 10, 24, 2, tzinfo=UTC)
    session.get(Document, taken).taken_down_at = NOW
    session.commit()
    response = send(client, "截止是哪天", gone)
    detail = response.json()["detail"]
    assert response.status_code == 422 and detail["reason"] == "link_gone"
    assert detail["message"] == (
        "原文链接已失效（10 月 24 日检查时打不开），这份通知不再回答新的问题，已有的回答照常能看。"
        "可以点上面的“换一份通知”，或用“找通知”找别的。"
    )
    response = send(client, "截止是哪天", taken)
    assert code(response) == "AI_NOT_ELIGIBLE" and response.json()["detail"]["reason"] == (
        "unavailable"
    )
    # 单独线路没配齐：后台处理不了（本机按本进程的设置算），能力接口也不给 ask
    settings.ai_advisor_base_url = ""
    response = send(client, "截止是哪天", doc)
    assert response.status_code == 429 and code(response) == "AI_UNAVAILABLE"
    assert response.json()["detail"]["message"] == ask.UNAVAILABLE
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert "ask" not in caps["features"] and caps["chat"] is True
    assert wechat.calls == [] and counts(session) == (0, 0, 0, 0)
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 0


def test_rules_answer_on_the_spot(client, session, settings, doc, model, wechat):
    """规则先挡（指代对不上、要代写）：当场回答、存这一问，不建任务、不扣次数。"""
    response = send(client, "刚才那句是什么意思", doc)
    assert response.status_code == 202
    turn = response.json()["turn"]
    assert turn["status"] == "done" and turn["counted"] is False
    assert turn["answer"]["status"] == "clarify" and turn["answer"]["by"] == "rule"
    assert turn["answer"]["lead"] == ask.CLARIFY_NONE and turn["answer"]["notice"] is None
    draft = send(client, "帮我写一份申报书", doc).json()["turn"]
    assert draft["answer"]["status"] == "out_of_scope" and draft["answer"]["lead"] == ask.DRAFT
    assert counts(session)[2] == 0 and usage(session, "ask") == 0
    stored = session.get(AIChatTurn, draft["id"]).answer_json
    assert stored["versions"]["config"] == content.CONFIG_BODY
    assert "申报书" not in json.dumps(stored, ensure_ascii=False)
    # 问要交什么的不挡，交给模型；规则答的两问也算有回答：最近两问的问题文字带进上下文，
    # 上一答是后一问（没有原句）
    got = asked(client, session, settings, model, "帮我写一下要交的材料", doc)
    assert got["status"] == "done"
    sent = model.calls[-1][1]
    assert sent["recent"] == ["刚才那句是什么意思", "帮我写一份申报书"]
    assert sent["previousAnswer"] == {"question": "帮我写一份申报书", "quotes": []}


def test_followups_and_content_updates(
    client, session, session_factory, settings, doc, model, monkeypatch
):
    """追问（第七节，评测 E1–E4）：带上前两问和上一答的原句；指回上一答却对不上的挡下；
    内容更新以后，明确指回的挡下，没明确指回的不带上下文。"""
    monkeypatch.setattr(service, "usage_limit", lambda kind: 100)
    first = asked(
        client,
        session,
        settings,
        model,
        "企业申报截止是哪天？",
        doc,
        answer(
            ("P002", "企业申报截止时间为2026年11月20日"), ("P003", "各市科技局于2026年11月30日")
        ),
    )
    assert [q["n"] for q in first["answer"]["quotes"]] == [1, 2]
    second = asked(client, session, settings, model, "那市里报送呢？", doc, answer(("P003", S3)))
    assert model.calls[-1][1]["recent"] == ["企业申报截止是哪天？"]
    assert model.calls[-1][1]["previousAnswer"] == {
        "question": "企业申报截止是哪天？",
        "quotes": [{"n": 1, "sid": "P002", "quote": S2}, {"n": 2, "sid": "P003", "quote": S3}],
    }
    session.expire_all()
    snapshot = session.get(AdvisorTask, session.get(AIChatTurn, second["id"]).task_id).snapshot
    assert snapshot["prev"] == first["id"]
    assert snapshot["prevQuotes"] == [{"n": 1, "sid": "P002"}, {"n": 2, "sid": "P003"}]
    assert "企业申报截止" not in json.dumps(snapshot, ensure_ascii=False)
    # 指回上一答的第 5 句，上一答只列了 1 句
    nth = send(client, "刚才第五句呢？", doc).json()["turn"]
    assert nth["answer"]["lead"] == (
        "上一个回答里没有第 5 句（只列了 1 句）。你指的是哪一句？可以把那句的几个字打出来再问。"
        "这次不算次数。"
    )
    # 原文自己有"第五条"：不算指回上一答，交给模型
    calls = len(model.calls)
    asked(client, session, settings, model, "第五条是什么", doc, answer(("P005", S5)))
    assert len(model.calls) == calls + 1
    assert model.calls[-1][1]["recent"] == ["那市里报送呢？", "刚才第五句呢？"]
    assert model.calls[-1][1]["previousAnswer"] == {"question": "刚才第五句呢？", "quotes": []}
    # 上一答之后正文更新了
    set_body(session_factory, doc, BODY + "\n八、新增的一句说明。")
    changed = send(client, "刚才那句呢？", doc).json()["turn"]
    assert changed["answer"]["status"] == "clarify" and changed["answer"]["lead"] == (
        ask.CLARIFY_CHANGED
    )
    calls = len(model.calls)
    reset = asked(client, session, settings, model, "那市里报送呢？", doc, answer(("P003", S3)))
    assert len(model.calls) == calls + 1
    assert model.calls[-1][1]["recent"] == [] and model.calls[-1][1]["previousAnswer"] is None
    assert reset["answer"]["notes"][0] == {"code": "reset", "text": ask.RESET, "head": True}
    assert reset["answer"]["quotes"][0]["text"] == S3
    # 再问一个完整的：照常回答；上一答是按现在的内容答的，最近两问的问题文字照常带上
    last = asked(client, session, settings, model, "市里报送截止是哪天", doc, answer(("P003", S3)))
    assert all(note["code"] != "reset" for note in last["answer"]["notes"])
    assert model.calls[-1][1]["recent"] == ["刚才那句呢？", "那市里报送呢？"]
    assert model.calls[-1][1]["previousAnswer"] == {
        "question": "那市里报送呢？",
        "quotes": [{"n": 1, "sid": "P003", "quote": S3}],
    }
    assert usage(session, "ask") == 5  # 找到原句的 5 问；规则挡下的 2 问不占


def test_two_asks_in_one_chat_at_once(client, session, doc, wechat, monkeypatch):
    """同一条对话同时发两问"问这份通知"：都过了提交前的检查，锁里后到的回 ASK_IN_PROGRESS。"""
    barrier = Barrier(2, timeout=10)

    def check_text(openid, text, **kwargs):
        barrier.wait()
        return wx_security.Verdict(True, "pass", None)

    monkeypatch.setattr(wx_security, "check_text", check_text)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda n: send(client, f"第{n}个问题", doc, key=f"c{n}"), range(2)))
    assert sorted(r.status_code for r in results) == [202, 409]
    refused = next(r for r in results if r.status_code == 409)
    assert code(refused) == "ASK_IN_PROGRESS"
    assert refused.json()["detail"]["message"] == ask.IN_PROGRESS
    assert counts(session)[1:3] == (1, 1)
    assert usage(session, "ask") == 1 and usage(session, chat_store.ASK_TRY) == 1


def test_previous_ask_still_running(client, session, settings, doc, wechat, model):
    """上一问还没出结果：这条对话不收新的"问这份通知"，也不把字交给微信；别的通知照常能问。"""
    assert send(client, "第一问", doc).status_code == 202
    calls = len(wechat.calls)
    response = send(client, "第二问", doc)
    assert response.status_code == 409 and code(response) == "ASK_IN_PROGRESS"
    assert len(wechat.calls) == calls
    other = make_doc(session, url="https://official.test/other")
    assert send(client, "第二问", other).status_code == 202
    work(session, settings)
    assert send(client, "第二问", doc).status_code == 202


def test_daily_counts(client, session, settings, doc, model):
    """每天 5 次只算找到原句的；20 问那本单独记、永不退回（删对话也不清零）；全站满了。"""
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert "ask" in caps["features"] and caps["remaining"]["ask"] == 5
    set_usage(session, "ask", 5)
    response = send(client, "截止是哪天", doc)
    assert response.status_code == 429 and code(response) == "ASK_DAILY_LIMIT"
    assert response.json()["detail"]["message"] == ask.DAILY_LIMIT
    assert usage(session, chat_store.ASK_TRY) == 0
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["remaining"]["ask"] == 0
    set_usage(session, "ask", 0)
    # 没找到：退回 5 次那本，20 问那本不退
    got = asked(
        client, session, settings, model, "截止是哪天", doc, {"status": "not_found", "quotes": []}
    )
    assert got["counted"] is False
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1
    set_usage(session, chat_store.ASK_TRY, 20)
    response = send(client, "截止是哪天", doc)
    assert response.status_code == 429 and code(response) == "ASK_DAILY_TRIES"
    assert response.json()["detail"]["message"] == ask.DAILY_TRIES
    assert usage(session, "ask") == 0  # 占上的 5 次那本随整笔回滚撤销
    assert client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["askTriesFull"] is True
    chat_id = session.scalar(select(AIChat.id))
    assert client.delete(f"/api/v1/advisor/chats/{chat_id}").status_code == 204
    assert usage(session, chat_store.ASK_TRY) == 20
    assert code(send(client, "截止是哪天", doc)) == "ASK_DAILY_TRIES"
    # 规则当场答的不占，满了也照常答
    assert send(client, "帮我写一份申报书", doc).json()["turn"]["answer"]["lead"] == ask.DRAFT
    set_usage(session, chat_store.ASK_TRY, 0)
    settings.ai_advisor_daily_cap = 0
    response = send(client, "截止是哪天", doc)
    assert response.status_code == 429 and code(response) == "AI_DAILY_CAP"
    assert response.json()["detail"]["message"] == ask.SITE_FULL
    assert counts(session)[2] == 0  # 只有没找到的那一问建过任务，删对话时一起删了


def test_task_key_clash_rolls_back(client, session, doc, model):
    """任务的请求编号撞了（这个人自己用解读接口发过 chat-turn: 开头的）：整个回滚，回 409，
    不扣次数；换个请求编号重发就好。"""
    uid = user_id(session)
    session.add(
        AdvisorTask(
            user_id=uid,
            kind="interpret",
            status="done",
            row_count=0,
            idempotency_key="chat-turn:1",
            created_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
    )
    session.commit()
    response = send(client, "截止是哪天", doc)
    assert response.status_code == 409 and code(response) == "CHAT_KEY_REUSED"
    assert counts(session)[:3] == (0, 0, 1) and usage(session, "ask") == 0
    assert send(client, "截止是哪天", doc).status_code == 202


def test_delete_chat_while_processing(client, session, session_factory, settings, doc, model):
    """处理中删对话：当场删完、退回 5 次那本；admin 收尾时任务和这一问都没了，不写结果、不报错。"""
    chat_id = send(client, "截止是哪天", doc).json()["chatId"]
    uid = user_id(session)

    def delete_now():
        with session_factory() as s:
            s.execute(select(User.id).where(User.id == uid).with_for_update()).one()
            chat_store.delete_chats(s, [chat_id])
            s.commit()

    model.before_return = delete_now
    result = work(session, settings)
    assert result["processed"] == 1 and result["failed"] == 1
    assert counts(session) == (0, 0, 0, 0)
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1
    assert session.scalar(select(func.sum(AIUsage.calls)).where(AIUsage.task == "ask")) == 1


@pytest.mark.parametrize(
    ("change", "error_code"),
    [
        ("consent", "AI_OFF"),
        ("whitelist", "AI_OFF"),
        ("route", "AI_UNAVAILABLE"),
        ("site_full", "AI_DAILY_CAP"),
        ("taken_down", "AI_NOT_ELIGIBLE"),
        ("updated_during_call", "ASK_DOC_UPDATED"),
        ("ai_off", "AI_UNAVAILABLE"),
    ],
)
def test_admin_rechecks_and_refunds(
    client, session, session_factory, settings, doc, model, monkeypatch, change, error_code
):
    """admin 处理时再核：问一问使用说明的同意、AI 授权、单独线路、全站调用上限、这份还能不能问；
    调用期间通知更新了不发布；admin 整个处理不了 AI（关了）时排队的由 _fail_revoked_interpret 退回。
    都记失败、退回 5 次那本（20 问那本不退）。"""
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    if change == "ai_off":
        settings.ai_advisor_enabled = False
    elif change == "consent":
        monkeypatch.setattr(chat_store, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
    elif change == "whitelist":
        settings.ai_advisor_openids = "second"
    elif change == "route":
        settings.ai_advisor_model = ""
    elif change == "site_full":
        settings.ai_advisor_daily_cap = 1
        session.add(AIUsage(day=service.usage_day(NOW), task="interpret", model="m", calls=1))
        session.commit()
    elif change == "taken_down":
        session.get(Document, doc).taken_down_at = NOW
        session.commit()
    else:
        model.before_return = lambda: set_body(session_factory, doc, BODY + "\n八、新增。")
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["status"] == "failed" and got["counted"] is False and got["answer"] is None
    assert got["errorCode"] == error_code
    if error_code == "ASK_DOC_UPDATED":
        assert got["error"] == "这次没做成：这份通知刚更新，请再问一次。这次不算次数。"
    assert got["error"].endswith("这次不算次数。")
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1
    assert len(model.calls) == (1 if change == "updated_during_call" else 0)
    if change == "ai_off":  # 退回了几个记进心跳，下一分钟往运维群提醒
        assert session.get(AIHeartbeat, "admin").refunds_pending == 1


@pytest.mark.parametrize(
    ("error", "error_code", "attempts"),
    [
        (None, "ASK_BAD_OUTPUT", 1),  # 格式不对（status 不是那四种）
        (
            AIError("AI 返回内容不是合法 JSON 对象（truncated）", code="truncated"),
            "ASK_BAD_OUTPUT",
            1,
        ),
        (AIError("AI 服务暂时不可用（HTTP 503）", retryable=True), "AI_UNAVAILABLE", 3),
        (TimeoutError(), "ASK_TIMEOUT", 3),
    ],
)
def test_model_failures(client, session, settings, doc, model, error, error_code, attempts):
    """模型出错：格式不对、截断直接失败；网络、模型方繁忙、超时最多重排两次，用完了才失败。都退回。"""
    model.payload = {"status": "maybe", "quotes": []}
    model.error = error
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    for _ in range(attempts):
        work(session, settings)
    task = session.scalar(select(AdvisorTask))
    assert task.status == "failed" and task.attempts == attempts and task.result_json is None
    got = turn_of(client, turn_id)
    assert got["errorCode"] == error_code and got["error"].startswith("这次没做成：")
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1
    assert len(model.calls) == attempts


def test_answer_states_and_notes(client, session, settings, model, monkeypatch):
    """四种状态和固定提示（第六节第 3 条那张表、第十二节第 3 条的说法）。"""
    monkeypatch.setattr(service, "usage_limit", lambda kind: 100)
    doc = make_doc(session)
    session.add(
        DocumentDeadline(
            document_id=doc,
            fingerprint="d1",
            kind="企业申报截止",
            candidate_date=date(2026, 11, 20),
            approved_date=date(2026, 11, 20),
            approved_time=time(17, 0),
            evidence=S2,
            confidence="high",
            status="approved",
            reviewed_at=NOW,
        )
    )
    session.commit()

    def got(text, payload):
        return asked(client, session, settings, model, text, doc, payload)["answer"]

    # 问时间：已核对的截止一块；原句里有两个以上日期或审核、报送的字 → 提醒看清是谁的时间
    timing = got("企业申报截止是哪天？", answer(("P002", S2), ("P003", S3)))
    assert timing["verified"] == [
        {"kind": "企业申报截止", "date": "2026-11-20", "time": "17:00", "remainingDays": 23}
    ]
    assert [n["code"] for n in timing["notes"]] == ["time_roles"]
    assert timing["notes"][0]["text"] == ask.TIME_ROLES
    # 摘到的原句里一个日期都没有
    none = got("截止时间呢", answer(("P001", S1)))
    assert [(n["code"], n["text"]) for n in none["notes"]] == [("time_none", ask.TIME_NONE)]
    # 只有一个日期、又没有那几个字：都不加
    assert got("什么时候截止", answer(("P002", S2)))["notes"] == []
    # 问能不能报：开头那句换掉
    eligible = got("成立半年能不能报", answer(("P001", S1)))
    assert eligible["lead"] == (
        "我们不替你判断能不能报（以主管部门解释为准）。下面摘出了原文里和这个问题有关的 1 句"
        "（可能没摘全），请逐句对照你们的情况："
    )
    assert eligible["verified"] == []  # 不是问时间的不给
    # 引用的原句提到"有关规定"：写在最前面
    rules = got("要求是什么", answer(("P005", S5), ("P001", S1)))
    assert rules["notes"] == [{"code": "rules", "text": ask.RULES_NOTE, "head": True}]
    # 有句子含遮掉的号码：不引用，写有几句
    redacted = got("联系人是谁", answer(("P006", "联系电话：[电话已脱敏]"), ("P001", S1)))
    assert [q["sid"] for q in redacted["quotes"]] == ["P001"] and redacted["redacted"] == 1
    assert (
        redacted["notes"][-1]["text"] == "有 1 句涉及电话、联系人或证件号，小程序里不显示这类号码。"
    )
    # 摘到的都含号码：不说"没有写"，也不叫人去看原文；不算次数
    only = asked(
        client,
        session,
        settings,
        model,
        "咨询电话是多少",
        doc,
        answer(("P006", "联系电话：[电话已脱敏]")),
    )
    assert only["counted"] is False and only["answer"]["status"] == "redacted"
    assert only["answer"]["lead"] == "有 1 句涉及电话、联系人或证件号，小程序里不显示这类号码。"
    assert only["answer"]["notes"] == [
        {"code": "not_counted", "text": "这次不算次数。", "head": False}
    ]
    assert only["answer"]["reader"] is None and only["answer"]["quotes"] == []
    # 没找到
    missing = asked(
        client,
        session,
        settings,
        model,
        "评分标准是什么",
        doc,
        {"status": "not_found", "quotes": []},
    )
    assert missing["counted"] is False and missing["answer"]["lead"] == ask.NOT_FOUND == (
        "这次没在已读的内容里找到和你问的直接相关的原句，不代表原文一定没写。"
    )
    assert missing["answer"]["notes"] == [
        {"code": "not_found", "text": ask.NOT_FOUND_RETRY, "head": False}
    ]
    assert missing["answer"]["reader"] == {"docId": doc, "attachmentId": None}
    assert missing["answer"]["scope"]["text"] == "读了：正文（这份通知没有附件）"
    # answer 但一句都对不上：unverified，说对不上、可以再问（D-105）；不算次数，给"站内阅读"
    wrong = got("评分标准是什么", answer(("P001", "原文里没有的一句话")))
    assert wrong["status"] == "unverified" and wrong["lead"] == ask.UNVERIFIED == (
        "这次摘出的句子和原文对不上，没有显示。可以再问一次，或点“站内阅读”自己找。"
    )
    assert wrong["notes"] == [{"code": "not_counted", "text": "这次不算次数。", "head": False}]
    assert wrong["reader"] == {"docId": doc, "attachmentId": None} and wrong["quotes"] == []
    # 模型回的范围外、要问清楚：只有固定说法，模型给的句子不用
    scope = asked(
        client,
        session,
        settings,
        model,
        "广东那份什么时候截止",
        doc,
        {"status": "out_of_scope", "quotes": [{"sid": "P002", "quote": S2}]},
    )
    assert scope["counted"] is False and scope["answer"]["lead"] == ask.OUT_OF_SCOPE
    assert scope["answer"]["quotes"] == [] and scope["answer"]["by"] == "model"
    clarify = got("那个呢", {"status": "clarify", "quotes": []})
    assert clarify["lead"] == ask.CLARIFY_MODEL
    assert usage(session, "ask") == 6  # 找到原句的 6 问


def test_attachment_notes_and_unread_scope(client, session, settings, model, monkeypatch):
    """原句说"见附件"、附件没读：写明附件 N 没读；站内能打开的给"站内阅读"，打不开的写读不出来。
    没找到、有附件没读：提示没读的附件里可能有。"""
    monkeypatch.setattr(service, "usage_limit", lambda kind: 100)
    doc = make_doc(session)
    att = DocumentAttachment(
        document_id=doc,
        position=1,
        name="申报书.docx",
        url="https://kjt.example.gov.cn/a.docx",
        extension="docx",
    )
    session.add(att)
    session.commit()
    closed = asked(client, session, settings, model, "要交什么", doc, answer(("P004", S4)))
    result = closed["answer"]
    assert result["notes"] == [
        {
            "code": "see_attachment",
            "text": "原句说“见附件”，附件 1 没读，这份附件暂时读不出来。",
            "head": True,
            "attachmentId": None,
        }
    ]
    assert result["scope"] == {
        "text": "只读了正文，附件没读",
        "partial": False,
        "unread": [
            {
                "label": "附件 1《申报书.docx》",
                "reason": "off",
                "attachmentId": att.id,
                "readable": False,
            }
        ],
    }
    missing = asked(
        client, session, settings, model, "评分标准", doc, {"status": "not_found", "quotes": []}
    )
    # 附件打不开：照样说可能在附件里，但不叫人点"站内阅读"
    assert [(n["code"], n["text"]) for n in missing["answer"]["notes"]] == [
        ("not_found_unread", ask.NOT_FOUND_UNREAD_CLOSED),
        ("not_found", ask.NOT_FOUND_RETRY),
    ]
    # 附件在站内能打开了（同站内阅读的判断）
    monkeypatch.setattr(
        attachment_text,
        "judge",
        lambda session, master, rows, now: [
            attachment_text.Reading(1, a.name, a.url, "docx", "ok", attachment_id=a.id)
            for a in rows
        ],
    )
    opened = turn_of(client, closed["id"])["answer"]
    assert opened["notes"][0]["text"] == "原句说“见附件”，附件 1 没读，可以点“站内阅读”看。"
    assert opened["notes"][0]["attachmentId"] == att.id
    assert opened["scope"]["unread"][0]["readable"] is True
    missing = turn_of(client, missing["id"])["answer"]
    assert [n["text"] for n in missing["notes"]] == [ask.NOT_FOUND_UNREAD, ask.NOT_FOUND_RETRY]


def _attachment_load(monkeypatch, state):
    """在真的正文后面接一个附件来源（附件读入第二步以前 content.load 只取正文）。"""
    real = content.load
    text = "评分标准：创新能力占40分。\n申报单位须提交盖章的申请书。"

    def load(session, doc_id, *, link=True):
        loaded = real(session, doc_id, link=link)
        source = content.SourceText(
            key="att-abc123def456",
            label=state["label"],
            version="sha-abc:extract-v1",
            text=text,
            attachment_id=state["id"],
            readable=state["readable"],
        )
        first, second = text.split("\n")
        segments = (
            content.Segment("Aabc123def456-1", source.key, first, 0, len(first), place="第 1 段"),
            content.Segment(
                "Aabc123def456-2",
                source.key,
                second,
                len(first) + 1,
                len(text),
                place="第 2 段",
            ),
        )
        texts = content.Texts(
            sources=(*loaded.sources, source), segments=(*loaded.segments, *segments)
        )
        unread = list(state.get("unread", []))
        return replace(loaded, texts=texts, config=content.CONFIG_ATTACH, unread=unread)

    monkeypatch.setattr(content, "load", load)


def test_attachment_quotes_follow_current_names(client, session, settings, doc, model, monkeypatch):
    """回答里每句存来源的内容版本和段号：附件改名、换了位置以后，出处和"站内阅读"按现在的拼，
    回答不作废；站内打不开了不给链接。正文在前、附件在后。"""
    state = {"label": "附件 1《申报指南》", "id": 11, "readable": True}
    _attachment_load(monkeypatch, state)
    got = asked(
        client,
        session,
        settings,
        model,
        "评分标准是什么",
        doc,
        answer(("Aabc123def456-1", "评分标准：创新能力占40分"), ("P001", S1)),
    )
    quotes = got["answer"]["quotes"]
    assert [(q["n"], q["source"], q["label"]) for q in quotes] == [
        (1, "body", "正文"),
        (2, "att-abc123def456", "附件 1《申报指南》第 1 段"),
    ]
    assert quotes[1]["reader"] == {"docId": doc, "attachmentId": 11}
    assert quotes[1]["version"] == "sha-abc:extract-v1"
    assert got["answer"]["scope"]["text"] == "读了：正文、附件 1《申报指南》"
    state.update(label="附件 2《申报指南（修订）》", id=12)
    again = turn_of(client, got["id"])["answer"]
    assert again["status"] == "answer"
    assert again["quotes"][1]["label"] == "附件 2《申报指南（修订）》第 1 段"
    assert again["quotes"][1]["reader"] == {"docId": doc, "attachmentId": 12}
    state["readable"] = False
    again = turn_of(client, got["id"])["answer"]
    assert again["quotes"][1]["reader"] is None and again["quotes"][1]["unreadable"] is True


@pytest.mark.parametrize("change", ["body", "taken_down", "kind", "config", "words", "cloud"])
def test_read_time_recheck(
    client, session, session_factory, settings, doc, model, monkeypatch, change
):
    """读的时候再核一次：内容变了、读附件的开关变了、下架了、现在不能问了、按新词库查出来了、
    线上读到没查过微信的标记（admin 配错），都不显示原句，接口照常返回这一问（状态 hidden）。
    轮询时通知刚好更新，下一次就作废。"""
    got = asked(client, session, settings, model, "截止是哪天", doc)
    assert got["answer"]["status"] == "answer"
    chat_id = session.scalar(select(AIChat.id))
    expected = {
        "body": ("stale", ask.STALE),
        "config": ("stale", ask.STALE),
        "taken_down": ("unavailable", ask.TAKEN_DOWN),
        "kind": ("ineligible", ask.INELIGIBLE),
        "words": ("removed", ask.REMOVED),
        "cloud": ("removed", ask.UNCHECKED),
    }[change]
    if change == "body":
        set_body(session_factory, doc, BODY + "\n八、新增。")
    elif change == "taken_down":
        session.get(Document, doc).taken_down_at = NOW
        session.commit()
    elif change == "kind":
        session.get(Document, doc).kind_override = "名单"
        session.commit()
    elif change == "config":
        real = content.load
        monkeypatch.setattr(
            content,
            "load",
            lambda s, d, *, link=True: replace(real(s, d, link=link), config=content.CONFIG_ATTACH),
        )
    elif change == "words":
        word_list(monkeypatch, TEST_WORD, "截止时间", version="test-2")
    else:
        # 本机做的回答没交微信查（标记里 wechat 是 false）：在云托管里读就不算数
        monkeypatch.setattr(wx_security, "in_cloud", lambda settings: True)
    hidden = turn_of(client, got["id"])
    assert hidden["status"] == "done" and hidden["counted"] is True
    result = hidden["answer"]
    assert (result["status"], result["hidden"], result["lead"]) == ("hidden", *expected)
    assert result["quotes"] == [] and result["notes"] == [] and result["scope"] is None
    assert S2 not in json.dumps(hidden, ensure_ascii=False)
    if change == "kind":
        assert result["reason"] == "list"
    opened = client.get(f"/api/v1/advisor/chats/{chat_id}").json()["chat"]["turns"][0]
    assert opened == hidden


def test_link_gone_after_submission(client, session, settings, doc, model):
    """排队时才确认原文链接失效的照常做完；已有回答照常显示、卡片顶上加一句，不放站内阅读。"""
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    session.get(Document, doc).link_gone_at = datetime(2026, 10, 24, 2, tzinfo=UTC)
    session.commit()
    work(session, settings)
    got = turn_of(client, turn_id)
    result = got["answer"]
    assert got["counted"] is True and result["status"] == "answer"
    assert result["linkGone"] == "2026-10-24"
    assert result["notes"][0] == {
        "code": "link_gone",
        "text": "原文链接 10 月 24 日检查时已失效。这条回答依据来自此前收录的内容，仅供参考，"
        "以发文机关现在公布的为准。",
        "head": True,
    }
    assert result["quotes"][0]["reader"] is None
    # 新问题不再回答
    assert send(client, "要交哪些材料", doc).json()["detail"]["reason"] == "link_gone"


# ---- 回答的两层内容安全（第九节第 6 条） ----


def test_stream_hit_stops_and_records(client, session, settings, doc, model, wechat):
    """流式接收中本地词库命中：立刻断开，记 ASK_UNSAFE、退回 5 次那本、记违规记录，
    模型的输出不保存、不交给微信。"""
    # 词出现在前面、后面还有一大段：收到凑齐这个词的那一段就当场查到，中途断开
    model.payload = answer(("P001", f"申报对象为本省{TEST_WORD}" + "注册的企业" * 60))
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["status"] == "failed" and got["errorCode"] == "ASK_UNSAFE"
    assert got["error"] == ask.UNSAFE_TEXT and got["answer"] is None
    reply = json.dumps(model.payload, ensure_ascii=False)
    assert TEST_WORD in model.streamed[-1] and len(model.streamed[-1]) < len(reply)  # 中途断开
    assert TEST_WORD not in model.streamed[-2]  # 凑齐的那一段当场断开，不多收
    task = session.scalar(select(AdvisorTask))
    assert task.result_json is None and task.status == "failed"
    [hit] = hits(session)
    assert (hit.kind, hit.stage, hit.layer) == ("ask", "answer", "local")
    assert (hit.document_id, hit.turn_id, hit.task_id) == (doc, turn_id, task.id)
    assert hit.text is None and hit.uid == access_log.log_uid("internal", session.get_bind())
    assert hit.reason_json == {
        "category": "test",
        "entry": "t1",
        "version": "test-1",
        "stream": True,
    }
    assert len(wechat.calls) == 1  # 只有提问那一次
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1


def test_whole_sentence_checked_after_expanding(client, session, settings, model, wechat):
    """模型摘的半句没命中，扩成整句以后命中：照样拦下（要显示的原句再过一遍本地词库）。"""
    doc = make_doc(session, body=BODY + f"\n八、申报材料须按要求填写，{TEST_WORD}。")
    turn_id = send(client, "材料怎么填", doc).json()["turn"]["id"]
    model.payload = answer(("P008", "八、申报材料须按要求填写"))
    work(session, settings)
    assert turn_of(client, turn_id)["errorCode"] == "ASK_UNSAFE"
    [hit] = hits(session)
    assert hit.layer == "local" and hit.reason_json["sid"] == "P008"
    assert "stream" not in hit.reason_json and len(wechat.calls) == 1


@pytest.mark.parametrize("suggest", ["risky", "review"])
def test_wechat_blocks_answer(client, session, settings, doc, model, wechat, suggest):
    wechat.outcomes = [
        wx_security.Verdict(True, "pass", None),  # 提问
        wx_security.Verdict(False, suggest, 20001, "trace-1"),  # 回答
    ]
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["errorCode"] == "ASK_UNSAFE" and got["answer"] is None
    assert wechat.calls[-1] == ("internal", S2)
    [hit] = hits(session)
    assert (hit.kind, hit.stage, hit.layer, hit.turn_id) == ("ask", "answer", "wechat", turn_id)
    assert hit.uid == access_log.log_uid("internal", session.get_bind()) and hit.text is None
    assert hit.reason_json == {"suggest": suggest, "label": 20001, "traceId": "trace-1"}
    assert usage(session, "ask") == 0


def test_wechat_down_on_answer(client, session, settings, doc, model, wechat, alerts):
    """复核回答时内容安全接口出错：不放行，记 ASK_CHECK_DOWN、退回，不重排；往运维群提醒。"""
    wechat.outcomes = [
        wx_security.Verdict(True, "pass", None),
        wx_security.CheckDown("测试：接口出错", 45009),
    ]
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["errorCode"] == "ASK_CHECK_DOWN"
    assert got["error"] == "这次没做成：内容安全检测暂时不可用。这次不算次数。"
    task = session.scalar(select(AdvisorTask))
    assert task.status == "failed" and task.attempts == 1
    assert usage(session, "ask") == 0 and hits(session) == []
    assert len(alerts) == 1 and "45009" in alerts[0]


def test_errors_log_no_text(client, session, settings, doc, model, caplog):
    """出错时日志里只有编号和类型，没有用户打的字和模型的输出（第十节第 6 条）。"""
    question = "一句只在这里出现的提问"
    output = "一段只在模型输出里出现的话"
    model.error = RuntimeError(f"模型说：{output}；问的是：{question}")
    with caplog.at_level(logging.DEBUG):
        turn_id = send(client, question, doc).json()["turn"]["id"]
        work(session, settings)
    assert turn_of(client, turn_id)["errorCode"] == "AI_UNAVAILABLE"
    assert question not in caplog.text and output not in caplog.text
    assert "RuntimeError" in caplog.text


# ---- 心跳、能力接口、旧任务接口 ----


def test_ask_ready_follows_admin_heartbeat(session, settings):
    """问答能用：admin 能处理 AI 任务、单独线路三项都配好、按流式接收；api 看 admin 的心跳
    （新鲜、设置一样）。"""
    admin = SimpleNamespace(**{**vars(settings), "role": "admin"})
    api = SimpleNamespace(**{**vars(settings), "role": "api"})
    assert heartbeat.ask_ready(session, api, NOW) is False  # 还没有心跳
    heartbeat.beat(session, admin, "admin", NOW)
    assert session.get(AIHeartbeat, "admin").ask_ready is True
    assert heartbeat.ask_ready(session, api, NOW) is True
    assert heartbeat.ask_ready(session, api, NOW + heartbeat.STALE + timedelta(seconds=1)) is False
    # admin 没配单独线路：解读、对照照常（白名单退回后台线路），问答不能用
    heartbeat.beat(
        session, SimpleNamespace(**{**vars(admin), "ai_advisor_base_url": ""}), "admin", NOW
    )
    assert session.get(AIHeartbeat, "admin").ask_ready is False
    assert heartbeat.worker_ready(session, api, NOW) is True
    assert heartbeat.ask_ready(session, api, NOW) is False
    # 两边设置对不上
    heartbeat.beat(session, admin, "admin", NOW)
    other = SimpleNamespace(**{**vars(api), "ai_advisor_openids": "internal"})
    assert heartbeat.ask_ready(session, other, NOW) is False
    # 没开流式：边收边查做不到，问这份通知不开放（第九节第 6 条，Codex 第 8 轮）
    heartbeat.beat(
        session, SimpleNamespace(**{**vars(admin), "ai_advisor_stream": False}), "admin", NOW
    )
    assert session.get(AIHeartbeat, "admin").ask_ready is False
    assert heartbeat.worker_ready(session, api, NOW) is True
    assert heartbeat.ask_ready(session, api, NOW) is False
    heartbeat.beat(session, admin, "admin", NOW)
    # 本机开发和测试按本进程的设置算
    assert heartbeat.ask_ready(session, settings, NOW) is True
    settings.ai_advisor_stream = False
    assert heartbeat.ask_ready(session, settings, NOW) is False
    settings.ai_advisor_stream = True
    settings.ai_advisor_api_key = None
    assert heartbeat.ask_ready(session, settings, NOW) is False


def test_capabilities_and_old_task_endpoints(client, session, settings, doc, model, monkeypatch):
    """能力接口：ask 只在问一问开着、单独线路配齐时给；remaining.ask 是今天还剩几次，和 ask 同一个
    条件（Codex 10/28）。旧任务接口不管问答：不收 kind=ask、不列、取不到。"""
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["features"] == ["interpret", "clause", "ask"] and caps["remaining"]["ask"] == 5
    asked(client, session, settings, model, "截止是哪天", doc)
    assert client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]["remaining"]["ask"] == 4
    monkeypatch.setattr(chat_store, "CHAT_TERMS_FINAL", False)
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert "ask" not in caps["features"] and "ask" not in caps["remaining"]  # 问一问没开：都不提
    monkeypatch.setattr(chat_store, "CHAT_TERMS_FINAL", True)
    key = settings.ai_advisor_api_key
    settings.ai_advisor_api_key = None  # 单独线路没配齐：同样都不提
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert "ask" not in caps["features"] and "ask" not in caps["remaining"]
    settings.ai_advisor_api_key = key
    response = client.post(
        "/api/v1/advisor/tasks",
        json={"kind": "ask", "doc_id": doc},
        headers={"Idempotency-Key": "old-api"},
    )
    assert response.status_code == 422
    task_id = session.scalar(select(AdvisorTask.id))
    assert client.get(f"/api/v1/advisor/tasks/{task_id}").status_code == 404
    assert task_id not in [t["id"] for t in client.get("/api/v1/advisor/tasks").json()["items"]]


def test_word_list_update_sweeps_shown_answers(client, session, settings, doc, model, monkeypatch):
    """词库更新以后，每天的清理用新词库再查一遍还显示着的回答：命中的撤下、记处置记录（记这一问的
    编号），这一问照常返回、写已撤下。"""
    got = asked(client, session, settings, model, "截止是哪天", doc)
    assert got["answer"]["status"] == "answer"
    word_list(monkeypatch, TEST_WORD, "截止时间", version="test-2")
    assert service.sweep_results(session, NOW) == 1
    task = session.scalar(select(AdvisorTask))
    assert task.status == "failed" and task.result_json is None
    [hit] = hits(session)
    assert (hit.kind, hit.stage, hit.turn_id, hit.document_id) == ("ask", "answer", got["id"], doc)
    assert hit.reason_json["sweep"] is True and hit.text is None
    removed = turn_of(client, got["id"])
    assert removed["errorCode"] == "ASK_UNSAFE" and removed["error"] == ask.REMOVED
    assert removed["counted"] is True and usage(session, "ask") == 1  # 撤下的没退次数，照样算
    assert removed["answer"] is None


def test_task_without_its_turn_fails_and_refunds(client, session, settings, doc, model):
    """按编号取不到这一问：任务记失败、退回 5 次那本，不调模型。"""
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    session.delete(session.get(AIChatTurn, turn_id))
    session.commit()
    work(session, settings)
    task = session.scalar(select(AdvisorTask))
    assert task.status == "failed" and task.error == heartbeat.UNAVAILABLE
    assert usage(session, "ask") == 0 and model.calls == []


def test_partial_read_note_comes_first(client, session, settings, doc, model, monkeypatch):
    """读不完时第一句写"没读完：……"（附件读入第二步以后才会有，这里换掉取内容的结果来测）。"""
    real = content.load

    def load(session, doc_id, *, link=True):
        loaded = real(session, doc_id, link=link)
        unread = [content.Unread("附件 3《实施细则》", "too_long", "第 41 段以后")]
        return replace(loaded, partial=True, unread=unread, config=content.CONFIG_ATTACH)

    monkeypatch.setattr(content, "load", load)
    got = asked(client, session, settings, model, "要满足哪些条件", doc, answer(("P001", S1)))
    assert got["answer"]["notes"][0] == {
        "code": "partial",
        "text": "没读完：附件 3《实施细则》第 41 段以后（超出字数）",
        "head": True,
    }
    assert got["answer"]["scope"]["partial"] is True


# ---- 审阅后补的（PR-3 第二轮） ----


def test_stream_guard_window_is_linear(client, session, settings, model, monkeypatch):
    """边收边查不是每段都从头查：每段只查"新收到的这段，加上它前面最长词条长度减 1 个字"
    （第九节第 6 条），一段两个字、回答 8000 来字，每次查的都只有几个字；收完再整段查一次。"""
    doc = make_doc(session)
    model.payload = answer(("P001", "某" * 7950))  # 原文里没有，核对不过：没有要显示的原句
    model.chunk = 2
    turn_id = send(client, "要满足哪些条件", doc).json()["turn"]["id"]
    windows, whole = [], []
    real_window = safety_words.check_normalized

    def window(text, library=None):  # 边收边查的窗口
        windows.append(len(text))
        return real_window(text, library)

    def full(text, library=None):  # 整段查（不经上面那个，免得算进窗口）
        whole.append(len(text))
        return real_window(safety_words.normalize(text), library)

    monkeypatch.setattr(safety_words, "check_normalized", window)
    monkeypatch.setattr(safety_words, "check", full)
    work(session, settings)
    reply = len(json.dumps(model.payload, ensure_ascii=False))
    assert reply > 7900 and len(model.streamed) > 3900
    carry = safety_words.LIBRARY.longest - 1
    # 每段一查，窗口不超过：前面 carry 个字 + 新的两个字 + 暂留的一个字（JSON 里的空白不算）
    assert len(windows) >= len(model.streamed) - 1 and max(windows) <= carry + model.chunk + 1
    assert reply in whole  # 收完整段查一次兜底
    assert turn_of(client, turn_id)["answer"]["status"] == "unverified"  # 原文里没有，对不上


def test_hit_in_the_last_characters_is_caught(client, session, settings, doc, model, wechat):
    """词在回答的最后几个字里：收到最后一段时当场拦下（以前要等收完整段再查）。"""
    model.payload = answer(("P001", "某" * 500 + TEST_WORD))
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    assert turn_of(client, turn_id)["errorCode"] == "ASK_UNSAFE"
    reply = json.dumps(model.payload, ensure_ascii=False)
    assert TEST_WORD in reply[-20:] and model.streamed[-1] == reply
    [hit] = hits(session)
    assert hit.layer == "local" and hit.reason_json["stream"] is True  # 收到那一段当场查到的
    assert session.scalar(select(AdvisorTask.result_json)) is None and len(wechat.calls) == 1


def test_retry_of_a_just_created_ask_returns_it(client, session, doc, wechat, monkeypatch):
    """断线重发：开头不加锁地按请求编号找时还没看到（另一个请求刚建成），提交前发现这条对话正忙——
    再找一次，返回原来那一问，不回 ASK_IN_PROGRESS，也不再发给微信。"""
    first = send(client, "截止是哪天", doc, key="retry")
    assert first.status_code == 202
    calls = len(wechat.calls)
    real = chat._turn_by_key
    missed = []

    def lookup(session, user_id, key, stamp):
        if not missed:
            missed.append(key)
            return None
        return real(session, user_id, key, stamp)

    monkeypatch.setattr(chat, "_turn_by_key", lookup)
    again = send(client, "截止是哪天", doc, key="retry")
    assert again.status_code == 202, again.text
    assert again.json()["turn"]["id"] == first.json()["turn"]["id"]
    assert missed == ["retry"] and len(wechat.calls) == calls
    assert counts(session)[1:3] == (1, 1) and usage(session, "ask") == 1


def test_ask_retry_after_terms_update_returns_it(client, session, doc, wechat, monkeypatch):
    """问这份通知发出以后"问一问使用说明"升了版：同一个请求编号重发拿得到原来那一问（202），不再交
    微信、不再占次数；新的一问回 403（Codex 10/28）。"""
    first = send(client, "截止是哪天", doc, key="before-update")
    assert first.status_code == 202
    calls = len(wechat.calls)
    monkeypatch.setattr(chat_store, "CHAT_AGREEMENT_VERSION", "chat-2027-01-v1")
    again = send(client, "截止是哪天", doc, key="before-update")
    assert again.status_code == 202, again.text
    assert again.json()["turn"]["id"] == first.json()["turn"]["id"]
    assert code(send(client, "别的问题", doc)) == "CHAT_CONSENT"
    assert len(wechat.calls) == calls and usage(session, "ask") == 1


def test_requeue_then_success_counts_once(client, session, settings, doc, model):
    """模型方繁忙重排了一次、第二次做成：只算 1 次，20 问那本也只记 1 问。"""
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    model.error = AIError("AI 服务暂时不可用（HTTP 503）", retryable=True)
    work(session, settings)
    task = session.scalar(select(AdvisorTask))
    assert (task.status, task.attempts) == ("queued", 1)
    assert turn_of(client, turn_id)["status"] == "queued"
    model.error = None
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["status"] == "done" and got["counted"] is True
    assert usage(session, "ask") == 1 and usage(session, chat_store.ASK_TRY) == 1
    assert session.scalar(select(func.sum(AIUsage.calls)).where(AIUsage.task == "ask")) == 2


def test_account_deleted_while_processing(client, session, settings, doc, model, wechat, caplog):
    """处理途中注销了：admin 收尾时用户、任务、这一问都没了，什么都不写，也不当作出错；
    回答不再交给微信（不再用他的 openid）。"""
    send(client, "截止是哪天", doc)
    assert len(wechat.calls) == 1  # 提问那一次
    deleted = []
    model.before_return = lambda: deleted.append(client.delete("/api/v1/me").status_code)
    with caplog.at_level(logging.WARNING):
        result = work(session, settings)
    assert deleted == [204] and result["failed"] == 1
    assert len(wechat.calls) == 1
    assert user_id(session) is None and counts(session) == (0, 0, 0, 0)
    assert "问答任务" not in caplog.text


def test_link_gone_wording(client, session, settings, doc, model):
    """原文链接失效以后：没写日期、没找到的说法不叫人点"站内阅读"，也不给站内阅读。"""
    first = asked(client, session, settings, model, "截止时间呢", doc, answer(("P001", S1)))
    second = send(client, "评分标准是什么", doc).json()["turn"]["id"]
    model.payload = {"status": "not_found", "quotes": []}
    session.get(Document, doc).link_gone_at = datetime(2026, 10, 24, 2, tzinfo=UTC)
    session.commit()
    work(session, settings)
    timing = turn_of(client, first["id"])["answer"]
    assert [n["code"] for n in timing["notes"]] == ["link_gone", "time_none"]
    assert timing["notes"][1]["text"] == ask.TIME_NONE_GONE == "摘到的原句里没有写具体日期。"
    assert timing["quotes"][0]["reader"] is None
    missing = turn_of(client, second)["answer"]
    assert missing["lead"] == ask.NOT_FOUND and missing["reader"] is None
    assert [(n["code"], n["text"]) for n in missing["notes"]] == [
        ("link_gone", missing["notes"][0]["text"]),
        ("not_found", "可以换个说法再问。这次不算次数。"),
    ]


def _sse_events(*texts, usage=None, finish="stop"):
    events = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
        for text in texts
    ]
    if finish:
        events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    if usage:
        events.append({"choices": [], "usage": usage})
    return events


@pytest.fixture
def real_stream(monkeypatch, settings):
    """真的 ChatClient（流式）接在假的网络上（httpx.MockTransport 按段吐 SSE），走 ask.process。
    sent 记下对方已经吐出去的段，看得出是不是中途断开的。"""
    import httpx

    from youju.ai import client as client_module

    state = SimpleNamespace(events=[], sent=[], requests=[], done=True)

    def body():
        for event in state.events:
            state.sent.append(event)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
        if state.done:  # 为假时模拟中途断了：没有 [DONE]
            yield b"data: [DONE]\n\n"

    def handler(request):
        state.requests.append(json.loads(request.content))
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    real = client_module.ChatClient
    monkeypatch.setattr(
        ask, "ChatClient", lambda *args, **kwargs: real(*args, transport=transport, **kwargs)
    )
    settings.ai_advisor_stream = True
    return state


def test_real_stream_hit_aborts_mid_stream(client, session, settings, doc, real_stream, wechat):
    """真的流式接收：词库命中就断开，后面的不再收；不存模型的输出，记 ASK_UNSAFE 和处置记录，
    退回 5 次那本；这次调用记一次出错。"""
    head = '{"status": "answer", "quotes": [{"sid": "P001", "quote": "申报对象为'
    real_stream.events = _sse_events(head, TEST_WORD, *(["本省注册的科技型中小企业"] * 30), '"}]}')
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    assert real_stream.requests[0]["stream"] is True
    assert len(real_stream.sent) < len(real_stream.events)  # 中途断开
    got = turn_of(client, turn_id)
    assert got["errorCode"] == "ASK_UNSAFE" and got["answer"] is None
    assert session.scalar(select(AdvisorTask.result_json)) is None
    [hit] = hits(session)
    assert (hit.layer, hit.turn_id, hit.reason_json["stream"]) == ("local", turn_id, True)
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1
    row = session.scalar(select(AIUsage).where(AIUsage.task == "ask"))
    assert (row.calls, row.errors) == (1, 1)
    assert len(wechat.calls) == 1  # 只有提问那一次


def test_real_stream_records_usage(client, session, settings, doc, real_stream):
    real_stream.events = _sse_events(
        '{"status": "answer", ',
        '"quotes": [{"sid": "P002", ',
        '"quote": "企业申报截止时间为2026年11月20日"}]}',
        usage={"prompt_tokens": 120, "completion_tokens": 30},
    )
    turn_id = send(client, "截止是哪天", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["counted"] is True and [q["text"] for q in got["answer"]["quotes"]] == [S2]
    request = real_stream.requests[0]
    assert request["stream"] is True and request["model"] == "advisor-model"
    assert request["messages"][0]["content"] == ask.SYSTEM_PROMPT
    row = session.scalar(select(AIUsage).where(AIUsage.task == "ask"))
    assert (row.model, row.calls, row.errors) == ("advisor-model", 1, 0)
    assert (row.prompt_tokens, row.completion_tokens) == (120, 30)


def test_tail_checked_when_the_call_fails_unexpectedly(
    client, session, settings, doc, model, wechat, monkeypatch
):
    """调用中途出了别的错（比如流的分块解码出错，不算网络错）：已经收到的那截在收到时就当场查过，
    词在那里的照样记 ASK_UNSAFE 和处置记录（写 stream），退回 5 次那本。"""

    def broken(system, user, max_tokens=800, *, on_text=None):
        model.totals["calls"] += 1
        on_text('{"status": "answer", "quotes": [{"sid": "P001", "quote": "' + TEST_WORD)
        raise httpx.DecodingError("分块解码出错")

    monkeypatch.setattr(model, "complete_json", broken)
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["errorCode"] == "ASK_UNSAFE" and got["answer"] is None
    [hit] = hits(session)
    assert (hit.layer, hit.turn_id) == ("local", turn_id)
    assert hit.reason_json["stream"] is True
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1


@pytest.mark.parametrize("ending", ["broken", "invalid_json"])
def test_real_stream_tail_checked_when_the_call_fails(
    client, session, settings, doc, real_stream, wechat, ending
):
    """调用出错（流中途断了、返回的不是 JSON）以前，最后那几段收到时就当场查过：词在那里的
    照样记 ASK_UNSAFE 和处置记录（写 stream），退回 5 次那本。"""
    head = '{"status": "answer", "quotes": [{"sid": "P001", "quote": "'
    texts = [head, *(["本省注册的科技型中小企业"] * 20), TEST_WORD]
    if ending == "broken":
        real_stream.events = _sse_events(*texts, finish=None)
        real_stream.done = False
    else:
        real_stream.events = _sse_events(*texts, "然后就不是 JSON 了")
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["errorCode"] == "ASK_UNSAFE" and got["answer"] is None
    assert session.scalar(select(AdvisorTask.result_json)) is None
    [hit] = hits(session)
    assert (hit.layer, hit.turn_id, hit.text) == ("local", turn_id, None)
    assert hit.reason_json["stream"] is True
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1
    assert len(wechat.calls) == 1


# ---- #396 合并以后按设计稿 #328 第 6–11 轮改的（D-105、边收边查、上下文、附件编号） ----


def _guard_hits(*texts):
    """把 texts 依次当作流式收到的全部文字交给 _Guard，返回第几段（从 1 起）拦下；没拦下为 None。"""
    guard = ask._Guard(streaming=True)
    for n, text in enumerate(texts, 1):
        try:
            guard(text)
        except ask.StreamBlocked:
            return n
    return None


def _grow(*parts):
    """流式收到的全部文字：每段到了都是前面所有段接起来。"""
    return ["".join(parts[: n + 1]) for n in range(len(parts))]


def test_guard_checks_each_segment_on_the_spot(monkeypatch):
    """每段一到就当场查，单字词条也一样；跨段的词、被空白和零宽字符隔开的、全角大写的都查得到
    （第九节第 6 条验收；Codex 第 9–11 轮）。"""
    word_list(monkeypatch, "é", "甲乙", "ab", "café")
    # 单字 é：收到 e 时不拦，单独收到组合重音符的那一段当场拦下
    assert _guard_hits(*_grow("x e", "\u0301")) == 2
    # 跨两段、第二段开头是组合符号
    assert _guard_hits(*_grow("le caf", "e", "\u0301 noir")) == 3
    # "甲""一串空格""乙"三段：第三段当场拦下（按规范化以后的字截窗口）
    assert _guard_hits(*_grow("甲", "      ", "乙")) == 3
    assert _guard_hits(*_grow("甲", "\u200b\u200d", "乙")) == 3
    # 全角、大写：规范化以后是 ab
    assert _guard_hits(*_grow("Ａ", "Ｂ")) == 2
    # 窗口外的不重复查、也不漏：前面隔了一大段再出现
    assert _guard_hits(*_grow("开头", "某" * 300, "甲", "乙")) == 4
    # 都没有：不拦
    assert _guard_hits(*_grow("一段", "正常的", "回答")) is None


def test_guard_waits_for_hangul_jamo_to_combine(monkeypatch):
    """韩文字母会和前后的字母合成一个字：初声、中声、终声分三段收到，合成以后的字照样查得到，
    中间不会把还没合成的字母先定下来。"""
    word_list(monkeypatch, "한")
    assert _guard_hits(*_grow("\u1112", "\u1161", "\u11ab")) == 3
    assert _guard_hits(*_grow("가하", "\u11ab")) == 2  # 先收到"하"、再收到终声


def test_guard_final_still_checks_everything(monkeypatch):
    """收完整段再查一次兜底（不是流式的整段回答也靠它）。"""
    word_list(monkeypatch, "甲乙")
    with pytest.raises(ask.StreamBlocked) as blocked:
        ask._Guard.final("前面\n甲 乙")
    assert blocked.value.streaming is False


def test_boundary_keeps_combining_text_together():
    """规范化边界：边界之前的字单独规范化一次就定了；组合符号、韩文字母前面不是边界。"""
    assert ask._boundary("abc") == 2
    # b 后面跟着组合符号：b 前面不当边界（保守：整截暂留，下一段来了再连同它们规范化）
    assert ask._boundary("ab\u0301") == 0
    assert ask._boundary("ab\u0301c") == 3
    assert ask._boundary("\u1112\u1161") == 0
    assert ask._boundary("e") == 0


def test_previous_answer_is_found_past_many_unanswered_turns(client, session, settings, doc, model):
    """上一答往前找不设上限（Codex 第 8 轮）；最近两问按顺序取、不管有没有回答，和上一答分开传
    （Codex 第 9 轮）：中间连着 55 问都没有回答，上一答还是第一问，recent 是最近那两问。"""
    first = asked(
        client, session, settings, model, "企业申报截止是哪天？", doc, answer(("P002", S2))
    )
    turn = session.get(AIChatTurn, first["id"])
    for seq in range(turn.seq + 1, turn.seq + 56):
        session.add(
            AIChatTurn(
                chat_id=turn.chat_id,
                seq=seq,
                skill="ask",
                question=f"没答上的第{seq}问",
                doc_id=doc,
                idempotency_key=f"gap-{seq}",
                created_at=NOW,
                expires_at=NOW + timedelta(days=30),
            )
        )
    session.commit()
    asked(client, session, settings, model, "那市里报送呢？", doc, answer(("P003", S3)))
    sent = model.calls[-1][1]
    last = turn.seq + 55
    assert sent["recent"] == [f"没答上的第{last - 1}问", f"没答上的第{last}问"]
    assert sent["previousAnswer"] == {
        "question": "企业申报截止是哪天？",
        "quotes": [{"n": 1, "sid": "P002", "quote": S2}],
    }


def test_unread_attachments_matched_by_id_not_order(client, session, settings, model, monkeypatch):
    """没读的附件按附件编号配能不能在站内打开，不按顺序配（Codex 第 8 轮）：换了顺序以后，
    能打开的还是那一个。"""
    monkeypatch.setattr(service, "usage_limit", lambda kind: 100)
    doc = make_doc(session)
    first = DocumentAttachment(
        document_id=doc,
        position=1,
        name="指南.pdf",
        url="https://kjt.example.gov.cn/a.pdf",
        extension="pdf",
    )
    second = DocumentAttachment(
        document_id=doc,
        position=2,
        name="申报书.docx",
        url="https://kjt.example.gov.cn/b.docx",
        extension="docx",
    )
    session.add_all([first, second])
    session.commit()
    opens = second.id
    monkeypatch.setattr(
        attachment_text,
        "judge",
        lambda session, master, rows, now: [
            attachment_text.Reading(
                1,
                a.name,
                a.url,
                a.extension,
                "ok" if a.id == opens else "scan",
                attachment_id=a.id,
            )
            for a in rows
        ],
    )
    got = asked(
        client, session, settings, model, "评分标准", doc, {"status": "not_found", "quotes": []}
    )
    unread = {u["attachmentId"]: u["readable"] for u in got["answer"]["scope"]["unread"]}
    assert unread == {first.id: False, second.id: True}
    # 换了顺序：还是按编号配
    first.position, second.position = 2, 1
    session.commit()
    again = turn_of(client, got["id"])["answer"]["scope"]["unread"]
    assert [(u["label"], u["readable"]) for u in again] == [
        ("附件 1《申报书.docx》", True),
        ("附件 2《指南.pdf》", False),
    ]


def test_no_quote_statuses_keep_their_own_wording(client, session, settings, doc, model):
    """没给出原句的三种（D-105）：只用自己那句说法；问能不能报的前面加一句不替你判断；
    问时间的照样给"已核对的截止"一块；都不算次数，次数退回。"""
    unverified = asked(
        client,
        session,
        settings,
        model,
        "我们能报吗",
        doc,
        answer(("P001", "原文里没有的一句话")),
    )
    assert unverified["counted"] is False
    assert unverified["answer"]["status"] == "unverified"
    assert unverified["answer"]["lead"] == ask.ELIGIBLE_PREFIX + ask.UNVERIFIED
    redacted = asked(
        client,
        session,
        settings,
        model,
        "符合条件的话联系谁",
        doc,
        answer(("P006", "联系电话：[电话已脱敏]")),
    )
    assert redacted["answer"]["lead"] == ask.ELIGIBLE_PREFIX + ask.REDACTED.format(n=1)
    missing = asked(
        client,
        session,
        settings,
        model,
        "截止日期是哪天",
        doc,
        {"status": "not_found", "quotes": []},
    )
    assert missing["answer"]["lead"] == ask.NOT_FOUND
    assert all(n["code"] not in ("time_roles", "time_none") for n in missing["answer"]["notes"])
    assert usage(session, "ask") == 0  # 三种都退回了


def test_results_stored_before_the_three_statuses(client, session, settings, doc, model):
    """D-105 以前存的回答：模型回 answer、却没有能显示的原句的都记成了 not_found。读的时候按存下的
    个数分：有含遮掉号码的 → redacted，给了句子却都对不上 → unverified；模型自己回的 not_found 不变。"""
    got = asked(client, session, settings, model, "评分标准", doc, answer(("P001", S1)))
    task = session.get(AdvisorTask, session.get(AIChatTurn, got["id"]).task_id)
    for stored, expected in (
        ({"redacted": 1, "dropped": 0}, "redacted"),
        ({"redacted": 0, "dropped": 2}, "unverified"),
        ({"redacted": 0, "dropped": 0}, "not_found"),
    ):
        task.result_json = {**task.result_json, "status": "not_found", "quotes": [], **stored}
        session.commit()
        assert turn_of(client, got["id"])["answer"]["status"] == expected


def test_each_status_with_eligibility_and_time_questions(
    client, session, settings, model, monkeypatch
):
    """验收（Codex 第 11 轮）：四种状态各配"能不能报""什么时候截止"两种问法。能不能报：answer 换开头，
    没给出原句的三种在自己的说法前面加一句不替你判断；问时间：四种都给"已核对的截止"一块，时间提示
    只给 answer；只有 answer 算次数。"""
    monkeypatch.setattr(service, "usage_limit", lambda kind: 100)
    doc = make_doc(session)
    session.add(
        DocumentDeadline(
            document_id=doc,
            fingerprint="d1",
            kind="企业申报截止",
            candidate_date=date(2026, 11, 20),
            approved_date=date(2026, 11, 20),
            approved_time=time(17, 0),
            evidence=S2,
            confidence="high",
            status="approved",
            reviewed_at=NOW,
        )
    )
    session.commit()
    payloads = {
        "answer": answer(("P002", S2), ("P003", S3)),
        "not_found": {"status": "not_found", "quotes": []},
        "unverified": answer(("P001", "原文里没有的一句话")),
        "redacted": answer(("P006", "联系电话：[电话已脱敏]")),
    }
    verified = [
        {"kind": "企业申报截止", "date": "2026-11-20", "time": "17:00", "remainingDays": 23}
    ]
    own = {
        "not_found": ask.NOT_FOUND,
        "unverified": ask.UNVERIFIED,
        "redacted": ask.REDACTED.format(n=1),
    }
    for status, payload in payloads.items():
        eligible = asked(client, session, settings, model, "我们能不能报", doc, payload)
        timing = asked(client, session, settings, model, "什么时候截止", doc, payload)
        for got in (eligible, timing):
            assert got["answer"]["status"] == status and got["counted"] is (status == "answer")
        assert timing["answer"]["verified"] == verified
        assert eligible["answer"]["verified"] == []
        codes = [n["code"] for n in timing["answer"]["notes"]]
        if status == "answer":
            assert eligible["answer"]["lead"] == ask.LEAD_ELIGIBLE.format(n=2)
            assert timing["answer"]["lead"] == ask.LEAD.format(n=2) and "time_roles" in codes
        else:
            assert eligible["answer"]["lead"] == ask.ELIGIBLE_PREFIX + own[status]
            assert timing["answer"]["lead"] == own[status]
            assert "time_roles" not in codes and "time_none" not in codes
    assert usage(session, "ask") == 2  # 只有 answer 的两问算


def test_mixed_read_and_unread_attachments(client, session, settings, doc, model, monkeypatch):
    """读附件开着、有的附件读了、有的没读：没读的那个按附件编号配能不能在站内打开（Codex 第 8 轮），
    没找到时提一句可能在没读的附件里。"""
    read_one = DocumentAttachment(
        document_id=doc,
        position=1,
        name="申报指南.pdf",
        url="https://kjt.example.gov.cn/1.pdf",
        extension="pdf",
    )
    unread_one = DocumentAttachment(
        document_id=doc,
        position=2,
        name="汇总表.pdf",
        url="https://kjt.example.gov.cn/2.pdf",
        extension="pdf",
    )
    session.add_all([read_one, unread_one])
    session.commit()
    state = {
        "label": "附件 1《申报指南.pdf》",
        "id": read_one.id,
        "readable": True,
        "unread": [content.Unread("附件 2《汇总表.pdf》", "scanned", attachment_id=unread_one.id)],
    }
    _attachment_load(monkeypatch, state)
    opens = {unread_one.id}
    monkeypatch.setattr(
        attachment_text,
        "judge",
        lambda session, master, rows, now: [
            attachment_text.Reading(
                1,
                a.name,
                a.url,
                a.extension,
                "ok" if a.id in opens else "scan",
                attachment_id=a.id,
            )
            for a in rows
        ],
    )
    got = asked(
        client, session, settings, model, "评审专家几人", doc, {"status": "not_found", "quotes": []}
    )
    scope = got["answer"]["scope"]
    assert scope["unread"] == [
        {
            "label": "附件 2《汇总表.pdf》",
            "reason": "scanned",
            "attachmentId": unread_one.id,
            "readable": True,
        }
    ]
    assert [n["text"] for n in got["answer"]["notes"]] == [
        ask.NOT_FOUND_UNREAD,
        ask.NOT_FOUND_RETRY,
    ]
    opens.clear()  # 现在打不开了：只说可能在附件里
    again = turn_of(client, got["id"])["answer"]
    assert again["scope"]["unread"][0]["readable"] is False
    assert [n["text"] for n in again["notes"]] == [
        ask.NOT_FOUND_UNREAD_CLOSED,
        ask.NOT_FOUND_RETRY,
    ]
