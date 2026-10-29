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
        self.ensure_ascii = False  # True：回复里的汉字都写成 \uXXXX 转义（JSON 允许）
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
        reply = json.dumps(self.payload, ensure_ascii=self.ensure_ascii)
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


def test_validate_expands_dedups_keeps_model_order_and_caps():
    lines = [f"第{n}条、申报单位须满足第{n}项要求。" for n in range(1, 11)]
    texts = _texts(*lines)
    quotes = [{"sid": "P010", "quote": "申报单位须满足第10项要求"}] + [
        {"sid": f"P{n:03d}", "quote": f"申报单位须满足第{n}项要求"} for n in range(1, 9)
    ]
    result = ask.validate({"status": "answer", "quotes": quotes}, texts)
    # 超过 8 句只取模型给的前 8 句，照模型给的先后编号（最相关的在前，不按原文先后重排，Codex
    # 第 15 轮）；扩成完整的句子
    assert [q["sid"] for q in result["quotes"]] == ["P010"] + [f"P{n:03d}" for n in range(1, 8)]
    assert [q["n"] for q in result["quotes"]] == list(range(1, 9))
    assert result["quotes"][0]["text"] == lines[9] and result["quotes"][1]["text"] == lines[0]
    assert result["quotes"][0]["source"] == "body"
    # 重复的、段号对不上的、太短的、格式不对的都不要；重复的不算对不上（不进 dropped）
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
    assert [q["sid"] for q in result["quotes"]] == ["P002"] and result["dropped"] == 3
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


def test_validate_short_masked_fragment_is_redacted():
    """含遮掉号码的先判、不看长短（Codex 第 40 轮）：只摘了遮挡前"联系电话："这样不到 6 个字的
    一小段，也算核对上了、不显示（redacted），不当成对不上。不含号码的太短的才算对不上。"""
    texts = _texts(S1, v1.redact_sensitive(S6))
    got = ask.validate(answer(("P002", "联系电话：")), texts)
    assert (got["status"], got["redacted"], got["dropped"]) == ("redacted", 1, 0)
    got = ask.validate(answer(("P001", "一、申报")), texts)
    assert (got["status"], got["dropped"]) == ("unverified", 1)


def test_validate_counts_a_masked_sentence_once():
    """同一句含遮掉号码的摘了几次（整句、它的一截）只记一句，"有 N 句"不多算；多摘的是重复，
    不算对不上。"""
    texts = _texts(S1, v1.redact_sensitive(S6))
    phone, name = ("P002", "联系电话：[电话已脱敏]"), ("P002", "六、联系人：[姓名已脱敏]")
    got = ask.validate(answer(phone, name, phone), texts)
    assert (got["status"], got["redacted"], got["dropped"]) == ("redacted", 1, 0)
    got = ask.validate(answer(phone, ("P001", S1), name), texts)
    shown = (got["status"], len(got["quotes"]), got["redacted"], got["dropped"])
    assert shown == ("answer", 1, 1, 0)


def test_validate_counts_masked_sentences_inside_a_longer_quote():
    """一句引用跨了几句时按句记（Codex 第 3 轮）：含号码的那句单独摘一次、连着下一句又摘一次，
    只记一句；一句引用里有两句含号码的记两句。都对得上，不算对不上。"""
    masked = v1.redact_sensitive(S6)
    texts = _texts(masked + S7)  # 一段里两句
    got = ask.validate(
        answer(("P001", "联系电话：[电话已脱敏]"), ("P001", "[电话已脱敏]。七、本通知自发布")),
        texts,
    )
    assert (got["status"], got["quotes"], got["redacted"], got["dropped"]) == ("redacted", [], 1, 0)
    texts = _texts("联系人：[姓名已脱敏]。联系电话：[电话已脱敏]。")
    got = ask.validate(answer(("P001", "[姓名已脱敏]。联系电话")), texts)
    assert (got["status"], got["redacted"], got["dropped"]) == ("redacted", 2, 0)
    # 先摘跨两句的、再摘其中一句：后一句记过了，不重复记
    got = ask.validate(
        answer(("P001", "[姓名已脱敏]。联系电话"), ("P001", "联系电话：[电话已脱敏]")), texts
    )
    assert (got["status"], got["redacted"], got["dropped"]) == ("redacted", 2, 0)


def test_validate_only_looks_in_the_quotes_own_source():
    """每句只在它的段号所属来源里逐字找：附件里的句子标成正文段号对不上，反过来也一样。"""
    body = S1
    attachment = "评分标准：创新能力占40分。"
    texts = content.Texts(
        sources=(
            content.SourceText(key="body", version="b", text=body),
            content.SourceText(
                key="att-1",
                version="a",
                text=attachment,
                # 内容一样的两个附件：只读一次，两个编号都记在这份内容上（按位置排）
                attachments=(
                    content.AttachmentRef(9, 3, "指南（另一份）", "a", True),
                    content.AttachmentRef(7, 1, "指南", "a", True),
                ),
            ),
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
            ("P001", S1),
        ),
        texts,
    )
    assert [(q["sid"], q["source"], q["version"]) for q in result["quotes"]] == [
        ("A1-1", "att-1", "a"),
        ("P001", "body", "b"),
    ]
    assert result["quotes"][0]["place"] == "第 1 段" and result["dropped"] == 2
    # 附件里的句子另存回答时这份内容对应的附件编号（读的时候按它挑出处）；正文的不存
    assert result["quotes"][0]["attachmentIds"] == [7, 9]
    assert "attachmentIds" not in result["quotes"][1]


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
    # 附件打不开：照样说可能在附件里，但不叫人点"站内阅读"（只有一份没读：这份附件）
    assert [(n["code"], n["text"]) for n in missing["answer"]["notes"]] == [
        (
            "not_found_unread",
            "没读的附件里可能有，这份附件暂时读不出来（原因见读的范围）。",
        ),
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
    again = turn_of(client, missing["id"])["answer"]
    assert [n["text"] for n in again["notes"]] == [ask.NOT_FOUND_UNREAD, ask.NOT_FOUND_RETRY]
    # 原文链接失效以后：只留"没读的附件里可能有。"，叫人去点、说读不出来的半句都不写
    session.get(Document, doc).link_gone_at = datetime(2026, 10, 24, 2, tzinfo=UTC)
    session.commit()
    gone = turn_of(client, missing["id"])["answer"]
    assert [(n["code"], n["text"]) for n in gone["notes"]] == [
        ("link_gone", gone["notes"][0]["text"]),
        ("not_found_unread", "没读的附件里可能有。"),
        ("not_counted", "这次不算次数。"),
    ]


ATT_VERSION = "sha-abc:extract-v1"  # 假附件来源的内容版本


def ref(attachment_id, n, name="申报指南", readable=True, version=ATT_VERSION, checked_at=None):
    """详情页附件列表里的一个附件现在的样子（content.AttachmentRef）。"""
    return content.AttachmentRef(attachment_id, n, name, version, readable, checked_at)


def _attachment_load(monkeypatch, state):
    """在真的正文后面接一个附件来源（附件读入第二步以前 content.load 只取正文）。
    state["attachments"] 是现在详情页附件列表里的附件（ref：编号、"附件 N"的 N、名称、能不能站内打开、
    现在的内容版本）；读的时候改它，就是附件改名、换位置、增删、能不能打开、内容变了。版本是
    ATT_VERSION 的那几个就是这份附件内容的附件；一个都没有，这份内容就读不到了（同真的 load：
    没有这个来源）。"""
    real = content.load
    text = state.get("text", "评分标准：创新能力占40分。\n申报单位须提交盖章的申请书。")

    def load(session, doc_id, *, link=True):
        loaded = real(session, doc_id, link=link)
        listed = tuple(state["attachments"])
        unread = list(state.get("unread", []))
        same = tuple(a for a in listed if a.version == ATT_VERSION)
        if not same:
            return replace(loaded, config=content.CONFIG_ATTACH, unread=unread, attachments=listed)
        source = content.SourceText(
            key="att-abc123def456", version=ATT_VERSION, text=text, attachments=same
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
        return replace(
            loaded, texts=texts, config=content.CONFIG_ATTACH, unread=unread, attachments=listed
        )

    monkeypatch.setattr(content, "load", load)


def test_attachment_quotes_follow_current_names(client, session, settings, doc, model, monkeypatch):
    """回答里每句存来源的内容版本和段号：附件改名、换了位置以后，出处和"站内阅读"按现在的拼，
    回答不作废；站内打不开了不给链接。照模型给的先后排（附件那句在前）。"""
    state = {"attachments": [ref(11, 1)]}
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
        (1, "att-abc123def456", "附件 1《申报指南》第 1 段"),
        (2, "body", "正文"),
    ]
    assert quotes[0]["reader"] == {"docId": doc, "attachmentId": 11}
    assert quotes[0]["version"] == "sha-abc:extract-v1"
    assert "attachmentIds" not in quotes[0]  # 记下的编号只在结果里，读的时候不往外给
    assert got["answer"]["scope"]["text"] == "读了：正文、附件 1《申报指南》"
    # 同一个附件改名、换到第 2 个
    state["attachments"] = [ref(11, 2, "申报指南（修订）")]
    again = turn_of(client, got["id"])["answer"]
    assert again["status"] == "answer"
    assert again["quotes"][0]["label"] == "附件 2《申报指南（修订）》第 1 段"
    assert again["quotes"][0]["reader"] == {"docId": doc, "attachmentId": 11}
    assert again["scope"]["text"] == "读了：正文、附件 2《申报指南（修订）》"
    state["attachments"] = [ref(11, 2, "申报指南（修订）", False)]
    again = turn_of(client, got["id"])["answer"]
    assert again["quotes"][0]["reader"] is None and again["quotes"][0]["unreadable"] is True
    assert again["quotes"][0]["label"] == "附件 2《申报指南（修订）》第 1 段"


def test_same_content_attachments_keep_name_and_reader_together(
    client, session, settings, doc, model, monkeypatch
):
    """内容一样的几个附件只读一次（第六节第 3 条"只更新显示"⓪–⑤，Codex 第 39、40、61–63 轮）：每句按
    回答时记下的附件编号挑，出处名称、"站内阅读"入口、点进去打开的（入口带的附件编号）始终是同一个
    附件；挑中替代的，出处后面只注明一句；记下的附件还在列表里、内容换了的作废，哪怕另有附件还是旧
    内容；现在的版本还不知道的当作读不到。"""
    state = {"attachments": [ref(11, 1)]}
    _attachment_load(monkeypatch, state)
    got = asked(
        client,
        session,
        settings,
        model,
        "评分标准是什么",
        doc,
        answer(("Aabc123def456-1", "评分标准：创新能力占40分")),
    )
    task = session.get(AdvisorTask, session.scalar(select(AdvisorTask.id)))
    assert task.result_json["quotes"][0]["attachmentIds"] == [11]
    assert task.snapshot["sourceAttachments"] == {"att-abc123def456": [11]}

    def shown(attachments):
        state["attachments"] = attachments
        answer_now = turn_of(client, got["id"])["answer"]
        assert answer_now["status"] == "answer", answer_now
        quote = answer_now["quotes"][0]
        return quote["label"], quote["reader"], quote["unreadable"]

    def hidden(attachments):
        state["attachments"] = attachments
        answer_now = turn_of(client, got["id"])["answer"]
        return answer_now["status"], answer_now.get("hidden"), answer_now["lead"]

    def opens(attachment_id):
        return {"docId": doc, "attachmentId": attachment_id}

    other = ref(12, 1, "申报指南（另一份）")  # 后来加的、内容一样
    moved, closed = ref(11, 2), ref(11, 2, readable=False)
    replaced = "附件 1《申报指南（另一份）》第 1 段" + ask.SUBSTITUTE
    # ① 记下的还在、能打开：就用它，哪怕排在前面的另一个也能打开（shown_id 是那一个）
    assert shown([other, moved]) == ("附件 2《申报指南》第 1 段", opens(11), False)
    # 读的范围里的名称和"附件取于"也用挑中的那一个（两个附件核对的日子不一样）
    state["attachments"] = [
        replace(other, checked_at=datetime(2026, 10, 20, 2, tzinfo=UTC)),
        replace(moved, checked_at=datetime(2026, 10, 17, 18, tzinfo=UTC)),  # 北京时间 18 日
    ]
    scope = turn_of(client, got["id"])["answer"]["scope"]["text"]
    assert scope == "读了：正文、附件 2《申报指南》（附件取于 10 月 18 日）"
    state["attachments"] = [state["attachments"][0], replace(closed, checked_at=None)]
    scope = turn_of(client, got["id"])["answer"]["scope"]["text"]
    assert scope == "读了：正文、附件 1《申报指南（另一份）》（附件取于 10 月 20 日）"
    # ② 记下的还在、打不开，内容一样的另一个能打开：用另一个，注明
    assert shown([other, closed]) == (replaced, opens(12), False)
    # ② 记下的不在附件列表里了：同一句注明
    assert shown([other]) == (replaced, opens(12), False)
    # 记下的还在、现在的版本还不知道（换了链接、还没重新取到）：当作读不到，能不能打开都一样
    for readable in (True, False):
        assert shown([other, ref(11, 2, readable=readable, version=None)]) == (
            replaced,
            opens(12),
            False,
        )
    # ③ 都打不开：用记下的那个，不给入口、不注明
    assert shown([replace(other, readable=False), closed]) == (
        "附件 2《申报指南》第 1 段",
        None,
        True,
    )
    # ④ 记下的不在了、内容一样的另一个也打不开：用另一个，注明，不给入口
    assert shown([replace(other, readable=False)]) == (replaced, None, True)
    # 记下的又能打开了：回到它
    assert shown([ref(11, 1)]) == ("附件 1《申报指南》第 1 段", opens(11), False)
    # ⓪ 记下的还在列表里、内容换了：作废，哪怕另有附件还是旧内容
    changed = ref(11, 2, version="sha-new:extract-v1")
    assert hidden([other, changed]) == ("hidden", "stale", ask.STALE)
    # ⑤ 这份内容一个附件都读不到了（唯一的那个版本还不知道、不在列表里）：读了的来源变了，作废
    assert hidden([ref(11, 1, version=None)])[:2] == ("hidden", "stale")
    assert hidden([])[:2] == ("hidden", "stale")
    # 这一版以前存的回答没有记下编号：用读的范围里写出名字的那个，不注明
    row = session.get(AdvisorTask, task.id)
    stored, snap = copy.deepcopy(row.result_json), copy.deepcopy(row.snapshot)
    del stored["quotes"][0]["attachmentIds"], snap["sourceAttachments"]
    row.result_json, row.snapshot = stored, snap
    session.commit()
    assert shown([other, moved]) == ("附件 1《申报指南（另一份）》第 1 段", opens(12), False)


def test_read_attachment_changed_goes_stale_even_if_not_cited(
    client, session, settings, doc, model, monkeypatch
):
    """⓪ 按回答时读了的附件算（sourceAttachments），引没引用、回答是不是没找到都算（Codex 第 62 轮）：
    读了附件 A、没引用它，A 换了内容、另有 B 还是旧内容 → 作废（answer、not_found 各一题）；同内容的
    附件加一个、减一个不作废（Codex 第 63 轮）。"""
    state = {"attachments": [ref(11, 1)]}
    _attachment_load(monkeypatch, state)
    cited_body = asked(client, session, settings, model, "申报对象是谁", doc, answer(("P001", S1)))
    missing = asked(
        client, session, settings, model, "评审专家是谁", doc, {"status": "not_found", "quotes": []}
    )
    snapshots = session.scalars(select(AdvisorTask.snapshot).order_by(AdvisorTask.id)).all()
    assert [s["sourceAttachments"] for s in snapshots] == [{"att-abc123def456": [11]}] * 2

    def statuses(attachments):
        state["attachments"] = attachments
        return [turn_of(client, t["id"])["answer"]["status"] for t in (cited_body, missing)]

    other = ref(12, 2, "申报指南（另一份）")
    assert statuses([ref(11, 1), other]) == ["answer", "not_found"]  # 加一个
    assert statuses([other]) == ["answer", "not_found"]  # 减一个
    assert statuses([ref(11, 1, version="sha-new:extract-v1"), other]) == ["hidden", "hidden"]


def test_attachment_changed_during_the_call(client, session, settings, doc, model, monkeypatch):
    """调完再核和读的时候是同一个判断（第六节第 3 条"处理时"第 6 步，Codex 第 64 轮）：调模型的时候
    读了的附件 A 换了内容、另有 B 还是旧内容（版本摘要没变），也不发布：记"这份通知刚更新"、退回。"""
    state = {"attachments": [ref(11, 1)]}
    _attachment_load(monkeypatch, state)

    def swap():
        state["attachments"] = [
            ref(11, 1, version="sha-new:extract-v1"),
            ref(12, 2, "申报指南（另一份）"),
        ]

    model.before_return = swap
    got = asked(
        client,
        session,
        settings,
        model,
        "评分标准是什么",
        doc,
        answer(("Aabc123def456-1", "评分标准：创新能力占40分")),
    )
    assert (got["status"], got["errorCode"]) == ("failed", "ASK_DOC_UPDATED")
    assert usage(session, "ask") == 0 and usage(session, chat_store.ASK_TRY) == 1


def test_only_read_attachment_with_unknown_version_goes_stale(
    client, session, settings, doc, model, monkeypatch
):
    """唯一读了的附件现在的版本还不知道（换了链接、还没重新取到）、又没有内容一样的别的附件：当作现在
    读不到，作废；answer、not_found 各一题，能不能站内打开都一样（Codex 第 63 轮）。"""
    state = {"attachments": [ref(11, 1)]}
    _attachment_load(monkeypatch, state)
    cited = asked(
        client,
        session,
        settings,
        model,
        "评分标准是什么",
        doc,
        answer(("Aabc123def456-1", "评分标准：创新能力占40分")),
    )
    missing = asked(
        client, session, settings, model, "评审专家是谁", doc, {"status": "not_found", "quotes": []}
    )
    assert [cited["answer"]["status"], missing["answer"]["status"]] == ["answer", "not_found"]
    for readable in (True, False):
        state["attachments"] = [ref(11, 1, readable=readable, version=None)]
        got = [turn_of(client, t["id"])["answer"] for t in (cited, missing)]
        assert [(a["status"], a["hidden"], a["lead"]) for a in got] == [
            ("hidden", "stale", ask.STALE)
        ] * 2


def test_card_reader_opens_the_attachment_named_in_scope(
    client, session, settings, doc, model, monkeypatch
):
    """没读正文时，卡片上的"站内阅读 ›"开的就是读的范围里写出名字的那个附件（按回答时记下的编号挑，
    同原句出处）：回答时读的是 A，后来排在前面加了内容一样的 B，入口照旧开 A；A 打不开了才开 B。"""
    state = {"attachments": [ref(11, 1)]}
    _attachment_only_load(monkeypatch, state)
    missing = asked(
        client, session, settings, model, "评审专家是谁", doc, {"status": "not_found", "quotes": []}
    )
    other = ref(12, 1, "申报指南（另一份）")

    def card(attachments):
        state["attachments"] = attachments
        got = turn_of(client, missing["id"])["answer"]
        return got["reader"], got["scope"]["text"]

    assert card([other, ref(11, 2)]) == (
        {"docId": doc, "attachmentId": 11},
        "读了：附件 2《申报指南》",
    )
    assert card([other, ref(11, 2, readable=False)]) == (
        {"docId": doc, "attachmentId": 12},
        "读了：附件 1《申报指南（另一份）》",
    )


def test_pick_without_any_attachment_gives_nothing():
    """内容一样的附件一个都没有（⑤）：不挑，调用方作废。"""
    assert ask._pick(content.SourceText(key="att-x", version="x", text="甲"), [1]) == (None, "")


def _attachment_only_load(monkeypatch, state):
    """没读正文、只读了一个附件（D-080 ②：没有正文但附件能读的照样能问）。"""
    _attachment_load(monkeypatch, state)
    with_body = content.load

    def load(session, doc_id, *, link=True):
        loaded = with_body(session, doc_id, link=link)
        texts = content.Texts(
            sources=tuple(s for s in loaded.sources if s.key != content.BODY),
            segments=tuple(s for s in loaded.segments if s.source != content.BODY),
        )
        return replace(loaded, texts=texts)

    monkeypatch.setattr(content, "load", load)


def test_card_reader_follows_what_was_read(client, session, settings, doc, model, monkeypatch):
    """卡片上的"站内阅读 ›"按这次读了的来源定（第六节第 3 条，Codex 第 41 轮）：读了正文开正文
    （别的测试）；只读了附件开那个附件；读了的来源一个都打不开时不放入口，叫人去点的半句也不写。
    not_found、unverified 各一题，日期那句同样；读的时候按现在的状态定。"""
    state = {
        "attachments": [ref(11, 1)],
        "text": "评分标准：创新能力占40分。\n申报单位须提交盖章的申请书。联系电话：[电话已脱敏]。",
    }
    _attachment_only_load(monkeypatch, state)
    missing = asked(
        client, session, settings, model, "评审专家是谁", doc, {"status": "not_found", "quotes": []}
    )
    wrong = asked(
        client,
        session,
        settings,
        model,
        "要交哪些材料",
        doc,
        answer(("Aabc123def456-2", "原文里没有的一句话")),
    )
    dated = asked(
        client,
        session,
        settings,
        model,
        "截止时间是哪天",
        doc,
        answer(("Aabc123def456-2", "申报单位须提交盖章的申请书")),
    )
    mixed = asked(
        client,
        session,
        settings,
        model,
        "联系电话是多少",
        doc,
        answer(("Aabc123def456-2", "联系电话："), ("Aabc123def456-2", "原文里没有的一句话")),
    )
    opened = {"docId": doc, "attachmentId": 11}
    shown = [turn["answer"] for turn in (missing, wrong, dated, mixed)]
    assert shown[0]["reader"] == opened and shown[0]["notes"][-1]["text"] == ask.NOT_FOUND_RETRY
    assert (shown[1]["reader"], shown[1]["lead"]) == (opened, ask.UNVERIFIED)
    assert [n["text"] for n in shown[2]["notes"] if n["code"] == "time_none"] == [ask.TIME_NONE]
    assert shown[3]["reader"] == opened
    assert shown[3]["lead"] == ask.REDACTED.format(n=1) + ask.REDACTED_REST
    state["attachments"] = [ref(11, 1, readable=False)]  # 读了的附件现在打不开了
    shown = [turn_of(client, turn["id"])["answer"] for turn in (missing, wrong, dated, mixed)]
    assert shown[0]["reader"] is None
    assert shown[0]["notes"][-1] == {
        "code": "not_found",
        "text": "可以换个说法再问。这次不算次数。",
        "head": False,
    }
    assert shown[1]["reader"] is None
    assert shown[1]["lead"] == "这次摘出的句子和原文对不上，没有显示。可以再问一次。"
    assert [n["text"] for n in shown[2]["notes"] if n["code"] == "time_none"] == [
        "摘到的原句里没有写具体日期。"
    ]
    assert shown[3]["reader"] is None
    assert shown[3]["lead"] == (
        "有 1 句涉及电话、联系人或证件号，小程序里不显示这类号码。"
        "其余摘出的句子和原文对不上，没有显示。可以再问一次。"
    )


def test_card_reader_picks_the_first_openable_source():
    """卡片入口按读了的来源定：读了正文开正文；没读正文开读了的附件里第一个能站内打开的
    （读了两个附件、第一个打不开时开第二个）；都打不开不给。"""
    body = content.SourceText(key="body", version="b", text="正文")
    first, second = (
        content.SourceText(
            key=f"att-{n}",
            version="a",
            text="甲",
            attachments=(ref(10 + n, n, f"附件{n}", n == 2, "a"),),
        )
        for n in (1, 2)
    )

    def reader(*sources):
        return ask._card_reader(7, SimpleNamespace(sources=sources))

    assert reader(body, first, second) == {"docId": 7, "attachmentId": None}
    assert reader(first, second) == {"docId": 7, "attachmentId": 12}
    assert reader(first) is None


def test_card_reader_compares_positions_across_sources():
    """没读正文时按位置跨来源比：附件 1 打不开、附件 2 能打开、附件 3 能打开，附件 1 和附件 3 内容
    一样（同一个来源，读的范围里写的是能打开的附件 3）——入口开附件 2，不开排在前面那个来源的附件 3；
    开的那个就是读的范围里写出名字的那个（名称和入口是同一个附件）。"""
    same = content.SourceText(
        key="att-a",
        version="a",
        text="甲",
        attachments=(ref(31, 3, "丙", True, "a"), ref(11, 1, "甲", False, "a")),
    )
    other = content.SourceText(
        key="att-b", version="b", text="乙", attachments=(ref(21, 2, "乙", True, "b"),)
    )
    assert (same.attachment_ids, same.shown_id, same.label) == ((11, 31), 31, "附件 3《丙》")
    assert ask._card_reader(7, SimpleNamespace(sources=(same, other))) == {
        "docId": 7,
        "attachmentId": 21,
    }
    assert other.label == "附件 2《乙》" and other.shown_id == 21
    # 都打不开：名称用第一个，不给入口
    closed = content.SourceText(
        key="att-c",
        version="c",
        text="丁",
        attachments=(
            ref(41, 4, "丁", False, "c"),
            ref(51, 5, "戊", False, "c"),
        ),
    )
    assert (closed.shown_id, closed.readable, closed.label) == (41, False, "附件 4《丁》")
    assert ask._card_reader(7, SimpleNamespace(sources=(closed,))) is None


def test_redacted_with_the_rest_unverified(client, session, settings, doc, model):
    """含号码的对上了、其余的对不上（一句能显示的都没有）：仍是 redacted、不算次数，在"有 N 句……"
    后面接"其余摘出的句子和原文对不上……"，入口同 unverified；只有含号码的不放入口；原文链接失效
    以后只留事实那半句（Codex 第 42 轮，待创始人确认）。"""
    mixed = asked(
        client,
        session,
        settings,
        model,
        "联系人是谁",
        doc,
        answer(("P006", "联系电话：[电话已脱敏]"), ("P001", "原文里没有的一句话")),
    )
    got = mixed["answer"]
    assert (got["status"], mixed["counted"]) == ("redacted", False)
    assert got["lead"] == (
        "有 1 句涉及电话、联系人或证件号，小程序里不显示这类号码。"
        "其余摘出的句子和原文对不上，没有显示。可以再问一次，或点“站内阅读”自己找。"
    )
    assert got["reader"] == {"docId": doc, "attachmentId": None}
    only = asked(
        client,
        session,
        settings,
        model,
        "咨询电话",
        doc,
        answer(("P006", "联系电话：[电话已脱敏]")),
    )
    assert (only["answer"]["lead"], only["answer"]["reader"]) == (ask.REDACTED.format(n=1), None)
    eligible = asked(
        client,
        session,
        settings,
        model,
        "联系人符合要求吗",
        doc,
        answer(("P006", "联系电话：[电话已脱敏]"), ("P001", "原文里没有的一句话")),
    )
    assert eligible["answer"]["lead"] == (
        ask.ELIGIBLE_PREFIX + ask.REDACTED.format(n=1) + ask.REDACTED_REST
    )
    session.get(Document, doc).link_gone_at = datetime(2026, 10, 24, 2, tzinfo=UTC)
    session.commit()
    gone = turn_of(client, mixed["id"])["answer"]
    assert gone["lead"] == ask.REDACTED.format(n=1) + "其余摘出的句子和原文对不上，没有显示。"
    assert gone["reader"] is None
    gone = turn_of(client, only["id"])["answer"]  # 只有含号码的：失效以后照旧
    assert (gone["lead"], gone["reader"]) == (ask.REDACTED.format(n=1), None)


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
    """原文链接失效以后：没写日期、没找到、摘的对不上的说法不叫人点"站内阅读"，也不叫人再问
    （失效的通知不再收新问），只留事实那半句；也不给站内阅读（第六节第 3 条"原文链接失效了"）。"""
    first = asked(client, session, settings, model, "截止时间呢", doc, answer(("P001", S1)))
    off = asked(
        client, session, settings, model, "要交哪些材料", doc, answer(("P001", "原文里没有的话"))
    )
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
        ("not_counted", "这次不算次数。"),
    ]
    unverified = turn_of(client, off["id"])["answer"]
    assert unverified["lead"] == ask.UNVERIFIED_GONE == "这次摘出的句子和原文对不上，没有显示。"
    assert unverified["reader"] is None
    assert [n["code"] for n in unverified["notes"]] == ["link_gone", "not_counted"]


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


def test_retry_from_scratch_checked_on_the_spot(client, session, settings, doc, model, monkeypatch):
    """客户端重试（格式不对、网络出错）时从头再收：新的一次收到的也当场查，命中就拦下，处置记录
    写 stream（不是等调用出错以后才查到）。"""

    def retried(system, user, max_tokens=800, *, on_text=None):
        model.totals["calls"] += 1
        on_text('{"status": "answer", "quotes": [' + "格式不对的一大段" * 30)  # 第一次：要重试
        on_text('{"status": "answer", "quotes": [{"sid": "P001", "quote": "' + TEST_WORD)
        raise httpx.DecodingError("没拦下的话走到这里")

    monkeypatch.setattr(model, "complete_json", retried)
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    assert turn_of(client, turn_id)["errorCode"] == "ASK_UNSAFE"
    [hit] = hits(session)
    assert (hit.layer, hit.turn_id, hit.reason_json["stream"]) == ("local", turn_id, True)


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
    # 看不见的字符先去掉再规范化：组合用字形连接符夹在 e 和重音符中间也照样合成 é
    assert _guard_hits(*_grow("x e\N{COMBINING GRAPHEME JOINER}", "\u0301")) == 2
    assert _guard_hits(*_grow("caf", "e\N{SOFT HYPHEN}", "\N{VARIATION SELECTOR-1}\u0301")) == 3


def test_guard_starts_over_when_the_client_retries(monkeypatch):
    """客户端重试时从空的开始重新拼（ChatClient 每次重试都从头收）：前一次收到的作废、从头查，
    新的一次开头的词当场拦下，不会因为前一次收得多就跳过。"""
    word_list(monkeypatch, "甲乙")
    assert _guard_hits("开头" + "某" * 300, "甲乙") == 2
    assert _guard_hits("甲", "某" * 50, *_grow("甲", "乙")) == 4  # 重试以后跨段的照样拦


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


BS = "\\"


def test_guard_checks_json_unescaped_text(monkeypatch):
    """边收边查的是按 JSON 转义还原以后的字（第九节第 6 条，Codex 第 31 轮）：高危词整个写成
    \\uXXXX、转义被分在两段里、代理对分在两段里、词中间隔着 \\n，都在收到凑齐的那一段当场拦下；
    原始的字照样也查；\\\\u 是一个反斜杠加 u，不还原；落单的高位代理照原样，不会一直卡着。"""
    word_list(monkeypatch, "甲乙", "𠀀丙")
    assert _guard_hits(BS + "u7532" + BS + "u4e59") == 1
    assert _guard_hits(*_grow(BS + "u75", "32" + BS + "u4e59")) == 2
    assert _guard_hits(*_grow("某" + BS, "u7532" + BS + "u4e", "59")) == 3
    # 代理对：高位、低位分在两段里，或者低位的反斜杠落在前一段末尾
    assert _guard_hits(*_grow(BS + "ud840", BS + "udc00" + BS + "u4e19")) == 2
    assert _guard_hits(*_grow(BS + "ud840" + BS, "udc00丙")) == 2
    # 转义的换行、制表符还原以后是空白，词照样连得上（反斜杠落在前一段末尾也一样）
    assert _guard_hits(*_grow("甲" + BS, "n乙")) == 2
    assert _guard_hits(*_grow("甲" + BS + "n" + BS + "t", "乙")) == 2
    assert _guard_hits(*_grow("某", "甲乙")) == 2  # 原始的字照样查
    assert _guard_hits(BS + BS + "u7532乙") is None  # 转义的是反斜杠，后面的 u7532 不还原
    assert _guard_hits(*_grow(BS + "ud840", "abc", "某")) is None
    # 收完整段兜底也查还原以后的字
    for whole in (json.dumps({"q": "甲\n乙"}, ensure_ascii=False), json.dumps({"q": "甲乙"})):
        with pytest.raises(ask.StreamBlocked):
            ask._Guard.final(whole)


def test_unescape_holds_half_written_escapes():
    """转义写了一半就到了末尾的留到下一段；收完了（final）照原样算。不是转义的照原样留着。"""
    assert ask._unescape(BS + "u7532" + BS + "n") == ("甲\n", "")
    assert ask._unescape("甲" + BS + "u75") == ("甲", BS + "u75")
    assert ask._unescape("甲" + BS) == ("甲", BS)
    assert ask._unescape(BS + "ud840") == ("", BS + "ud840")
    assert ask._unescape(BS + "ud840" + BS + "ud") == ("", BS + "ud840" + BS + "ud")
    assert ask._unescape(BS + "ud840" + BS + "u0041") == ("\ud840A", "")
    assert ask._unescape("甲" + BS + "u75", final=True) == ("甲" + BS + "u75", "")
    assert ask._unescape(BS + "x" + BS + "u12zz") == (BS + "x" + BS + "u12zz", "")
    assert ask._unescape(BS + '"' + BS + "/" + BS + BS) == ('"/' + BS, "")


def test_escaped_stream_hit_stops_on_the_spot(client, session, settings, doc, model, wechat):
    """模型把回答里的汉字都写成 \\uXXXX 转义（JSON 允许，原始的字里没有那个词）：照样在收到凑齐
    高危词的那一段当场断开，记 ASK_UNSAFE、退回 5 次那本、记违规记录（stream）。"""
    model.ensure_ascii = True
    model.payload = answer(("P001", f"申报对象为本省{TEST_WORD}" + "注册的企业" * 60))
    turn_id = send(client, "申报对象是谁", doc).json()["turn"]["id"]
    work(session, settings)
    got = turn_of(client, turn_id)
    assert got["errorCode"] == "ASK_UNSAFE" and got["answer"] is None
    reply = json.dumps(model.payload, ensure_ascii=True)
    assert TEST_WORD not in reply and len(model.streamed[-1]) < len(reply)  # 中途断开
    [hit] = hits(session)
    assert hit.layer == "local" and hit.reason_json["stream"] is True
    assert usage(session, "ask") == 0 and len(wechat.calls) == 1


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
    （Codex 第 9 轮）：中间连着 55 问都没有回答，上一答还是第一问，recent 是最近那两问。
    （问题里用汉字写序号：阿拉伯数字交给模型前会隐去。）"""

    def gap(seq):
        return "没答上的第" + "".join("〇一二三四五六七八九"[int(d)] for d in str(seq)) + "问"

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
                question=gap(seq),
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
    assert sent["recent"] == [gap(last - 1), gap(last)]
    assert sent["previousAnswer"] == {
        "question": "企业申报截止是哪天？",
        "quotes": [{"n": 1, "sid": "P002", "quote": S2}],
    }


def test_follow_up_numbers_follow_the_shown_order(client, session, settings, doc, model):
    """原句照模型给的先后编号（第 15 轮）：先摘 P003、后摘 P002 时，第 1 句是 P003 那句；追问
    交给模型的 previousAnswer 照这个编号。"""
    first = asked(
        client,
        session,
        settings,
        model,
        "报送和截止分别是哪天",
        doc,
        answer(("P003", S3), ("P002", S2)),
    )
    assert [(q["n"], q["sid"]) for q in first["answer"]["quotes"]] == [(1, "P003"), (2, "P002")]
    asked(client, session, settings, model, "第2句是谁的截止", doc, answer(("P002", S2)))
    assert model.calls[-1][1]["previousAnswer"]["quotes"] == [
        {"n": 1, "sid": "P003", "quote": S3},
        {"n": 2, "sid": "P002", "quote": S2},
    ]


def test_expired_turns_left_out_of_recent_and_previous(client, session, settings, doc, model):
    """到期的问不算（第十节第 1 条）：上一答、最近两问都只从没到期的里取；到期时刻正好是现在
    也算到期。"""
    first = asked(
        client, session, settings, model, "企业申报截止是哪天？", doc, answer(("P002", S2))
    )
    second = asked(client, session, settings, model, "市里哪天报送？", doc, answer(("P003", S3)))
    later = session.get(AIChatTurn, second["id"])
    chat_id, seq = later.chat_id, later.seq + 1
    later.expires_at = NOW
    session.commit()
    assert ask.previous(session, chat_id, seq, NOW).question == "企业申报截止是哪天？"
    assert ask.recent(session, chat_id, seq, NOW) == ["企业申报截止是哪天？"]
    session.get(AIChatTurn, first["id"]).expires_at = NOW - timedelta(days=1)
    session.commit()
    assert ask.previous(session, chat_id, seq, NOW) is None
    assert ask.recent(session, chat_id, seq, NOW) == []


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
    body = {"docId": doc, "attachmentId": None}
    for stored, expected, lead, reader in (
        ({"redacted": 1, "dropped": 0}, "redacted", ask.REDACTED.format(n=1), None),
        # 两个个数都大于 0：照含号码的对上、其余对不上的写，入口同 unverified（第六节第 3 条）
        (
            {"redacted": 1, "dropped": 2},
            "redacted",
            ask.REDACTED.format(n=1) + ask.REDACTED_REST,
            body,
        ),
        ({"redacted": 0, "dropped": 2}, "unverified", ask.UNVERIFIED, body),
        ({"redacted": 0, "dropped": 0}, "not_found", ask.NOT_FOUND, body),
    ):
        task.result_json = {**task.result_json, "status": "not_found", "quotes": [], **stored}
        session.commit()
        shown = turn_of(client, got["id"])["answer"]
        assert (shown["status"], shown["lead"], shown["reader"]) == (expected, lead, reader)


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
        "attachments": [ref(read_one.id, 1, "申报指南.pdf")],
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
        ask.NOT_FOUND_UNREAD_CLOSED_ONE,
        ask.NOT_FOUND_RETRY,
    ]
    # 两份没读、都打不开：这几份附件
    state["unread"] = [
        *state["unread"],
        content.Unread("附件 3《说明.pdf》", "scanned", attachment_id=unread_one.id + 1000),
    ]
    again = turn_of(client, got["id"])["answer"]
    assert [n["text"] for n in again["notes"]] == [
        "没读的附件里可能有，这几份附件暂时读不出来（原因见读的范围）。",
        ask.NOT_FOUND_RETRY,
    ]


# ---- 第一批（设计稿 #328 第 39–46 轮：同内容附件、转义还原以后查、隐去数字） ----


def test_numbers_hidden_before_the_model(client, session, settings, doc, model, wechat):
    """交给模型前隐去问题里的阿拉伯数字（第九节第 2 条；第一节第 3 件待创始人定，先按推荐做）：这一问、
    recent 的两问、previousAnswer 的问题都现做（前面存下的问题也一样），序号和写到哪一天的日期照旧，
    上一答摘的原句不换；存下的、显示的、送微信检查的都是原话。"""
    first = "我司去年研发投入500万，研发人员15人，能报吗"
    second = "第 1 句里的2026年11月20日是谁的截止，3 个月内能报吗"
    third = "那市里报送呢？近 3 年的也算吗"
    turns = [
        asked(client, session, settings, model, first, doc, answer(("P002", S2))),
        asked(client, session, settings, model, second, doc, answer(("P003", S3))),
        asked(client, session, settings, model, third, doc, answer(("P001", S1))),
    ]
    sent = [call[1] for call in model.calls]
    hidden = [
        "我司去年研发投入[数字已隐去]万，研发人员[数字已隐去]人，能报吗",
        "第 1 句里的2026年11月20日是谁的截止，[数字已隐去] 个月内能报吗",
        "那市里报送呢？近 [数字已隐去] 年的也算吗",
    ]
    assert [s["question"] for s in sent] == hidden
    assert sent[1]["recent"] == hidden[:1]
    assert sent[1]["previousAnswer"] == {
        "question": hidden[0],
        "quotes": [{"n": 1, "sid": "P002", "quote": S2}],  # 上一答的原句是通知原文，不换
    }
    assert sent[2]["recent"] == hidden[:2]
    assert sent[2]["previousAnswer"]["question"] == hidden[1]
    assert "不要猜原来是多少" in model.calls[0][0]
    # 存下的、显示的、送微信检查的都是原话
    session.expire_all()
    stored = session.scalars(select(AIChatTurn.question).order_by(AIChatTurn.seq)).all()
    assert stored == [first, second, third]
    assert [turn_of(client, t["id"])["question"] for t in turns] == [first, second, third]
    assert [text for _, text in wechat.calls if text in (first, second, third)] == [
        first,
        second,
        third,
    ]
    assert all(t["status"] == "done" and t["counted"] for t in turns)
