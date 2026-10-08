"""申报条件清单（D-052 第④项）：抽取校验、每轮抽取、核对台和对外读取。不调用真实模型。"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju import cli
from youju import conditions as service
from youju.ai.client import AIError
from youju.ai.conditions import INPUT_MAX, QUOTE_MAX, prepare_text, validate
from youju.api import app as app_module
from youju.api import v1
from youju.config import get_settings
from youju.db import get_session
from youju.models import (
    AICheck,
    ConditionItem,
    ConditionList,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    Source,
)

NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
HEADERS = {"X-Admin-Token": "secret"}
BODY = (
    "各有关单位：\n"
    "一、申报条件\n"
    "（一）申报单位注册地在本市，具有独立法人资格。\n"
    "（二）上年度研发投入占营业收入比例不低于３％。\n"
    "（三）拥有与主营业务相关的发明专利 2 件以上。\n"
    "（四）项目负责人须为本单位在职人员，咨询电话 13812345678。\n"
    "联系人：张三，电话 13812345678。\n"
    "二、支持方式\n对符合条件的项目给予最高 100 万元资金支持。"
)
GOOD = [
    {"category": "主体资格", "text": "注册地在本市", "quote": "申报单位注册地在本市"},
    # 全角百分号、空白和模型给的不一样：按空白和全角半角宽容比对，存正文原样
    {
        "category": "财务指标",
        "text": "上年度研发投入占营业收入比例不低于 3%",
        "quote": "上年度研发投入占营业收入比例不低于3%",
    },
    {
        "category": "知识产权",
        "text": "发明专利 2 件以上",
        "quote": "拥有与主营业务相关的发明专利 2 件以上",
    },
]
BAD = [
    {"category": "人员", "text": "研发人员不少于 10 人", "quote": "研发人员不少于10人"},  # 编的
    {"category": "主体资格", "text": "注册地在本省", "quote": "注册地"},  # 太短
    {
        "category": "财务指标",
        "text": "研发投入不低于 5%",
        "quote": "研发投入占营业收入比例不低于３％",
    },
    {"category": "主体资格", "text": "注册地在本市的企业可以申报", "quote": "申报单位注册地在本市"},
    # 原句里有脱敏的个人信息：不能对外，丢弃
    {"category": "人员", "text": "项目负责人须为在职人员", "quote": "项目负责人须为本单位在职人员"},
    {"category": "主体资格", "text": "注册地在本市", "quote": "注册地在本市，具有独立法人资格"},
    "不是对象",
]


class FakeClient:
    """模拟后台模型：记下发过去的内容，按 respond 返回结果或抛错。"""

    model = "fake-model"

    def __init__(self, respond=None):
        self.respond = respond or (lambda user: {"conditions": GOOD + BAD})
        self.calls: list[str] = []
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.totals = {"calls": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def complete_json(self, system, user, max_tokens=800):
        self.calls.append(user)
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 20}
        result = self.respond(user)
        if isinstance(result, Exception):
            raise result
        return result


# ---- 校验（不连数据库） ----


def test_validate_keeps_only_verbatim_quotes_and_stores_original_sentence():
    body = service.public_body(BODY)
    result = validate({"conditions": GOOD + BAD}, body)
    assert [(c.category, c.text) for c in result.items] == [
        ("主体资格", "注册地在本市"),
        ("财务指标", "上年度研发投入占营业收入比例不低于 3%"),
        ("知识产权", "发明专利 2 件以上"),
    ]
    for item in result.items:
        # 存的是正文里的原样文字（补成完整原句），在正文里逐字找得到，位置也对
        assert body[item.offset : item.offset + len(item.quote)] == item.quote
    assert result.items[0].quote == "（一）申报单位注册地在本市，具有独立法人资格。"
    assert result.items[1].quote == "（二）上年度研发投入占营业收入比例不低于３％。"
    assert result.reasons == {
        "bad_shape": 1,
        "too_long": 0,
        "too_short": 1,
        "not_found": 1,
        "redacted": 1,
        "number_mismatch": 1,
        "judgement": 1,
        "duplicate": 1,
        "too_many": 0,
    }
    assert result.dropped == 7


def test_validate_tolerates_whitespace_but_not_rewording():
    body = service.public_body(BODY)
    # 正文里的“2 件”模型写成“2件”、全角括号写成半角：仍是同一句原文，存正文原样
    loose = {
        "category": "知识产权",
        "text": "发明专利 2 件以上",
        "quote": "(三)拥有与主营业务相关的发明专利2件以上",
    }
    reworded = {
        "category": "知识产权",
        "text": "发明专利 2 件以上",
        "quote": "拥有与主营业务相关的发明专利两件以上",
    }
    result = validate({"conditions": [loose, reworded]}, body)
    assert [item.quote for item in result.items] == [
        "（三）拥有与主营业务相关的发明专利 2 件以上。"
    ]
    assert result.reasons["not_found"] == 1


def test_validate_category_fallback_long_sentence_and_bad_structure():
    sentence = "申报单位应当" + "依法经营、" * 100 + "无不良记录。"
    body = f"前言。\n{sentence}\n"
    result = validate(
        {"conditions": [{"category": "资质", "text": "依法经营", "quote": "申报单位应当依法经营"}]},
        body,
    )
    # 类别不在五类之内的归“其他”；原句超长时只存模型引用的那一段（仍是正文原样）
    assert result.items[0].category == "其他"
    assert result.items[0].quote == "申报单位应当依法经营"
    assert len(sentence) > QUOTE_MAX
    for payload in ({}, {"conditions": "无"}, []):
        with pytest.raises(AIError):
            validate(payload, body)


def test_prepare_text_prefers_condition_paragraphs_when_too_long():
    filler = "\n".join(["本市工作安排如下" + "。" * 200] * 80)
    body = filler + "\n申报单位须注册地在本市。\n" + filler
    prepared = prepare_text(body)
    assert len(prepared) <= INPUT_MAX
    assert "申报单位须注册地在本市。" in prepared
    assert prepare_text(BODY) == BODY


# ---- 数据库 ----


def add_notice(session, n=1, *, title=None, body=BODY, rights=1, due=None, due_status="pending"):
    source = Source(
        level="省级",
        province="辽宁",
        dept="科技",
        dept_full_name="辽宁省科技厅",
        column_name="通知",
        url=f"https://example.gov.cn/source/{n}",
        rights_level=rights,
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        url=f"https://example.gov.cn/{n}",
        url_key=f"example.gov.cn/{n}",
        title=title or f"关于组织申报科技项目的通知{n}",
        first_seen_at=NOW,
        last_seen_at=NOW,
        publish_date=date(2026, 9, 29),
    )
    session.add(doc)
    session.flush()
    session.add(DocumentDetail(document_id=doc.id, body_text=body, fetched_at=NOW, status="ok"))
    if due is not None:
        session.add(
            DocumentDeadline(
                document_id=doc.id,
                fingerprint=f"f{n}",
                kind="企业申报截止",
                candidate_date=due,
                approved_date=due if due_status == "approved" else None,
                evidence="申报截止。",
                confidence="高",
                status=due_status,
            )
        )
    session.commit()
    return doc


def test_run_extraction_orders_due_soon_first_and_respects_limit(session):
    later = add_notice(session, 1, due=date(2026, 12, 30))
    soon = add_notice(session, 2, due=date(2026, 10, 20), due_status="approved")
    sooner = add_notice(session, 3, due=date(2026, 10, 10))
    unknown = add_notice(session, 4)
    expired = add_notice(session, 5, due=date(2026, 9, 1))
    add_notice(session, 6, title="关于2026年度科技项目立项名单的公示")  # 不是申报通知
    add_notice(session, 7, rights=2)  # 不是一级公开来源
    add_notice(session, 8, body="")  # 没有正文
    client = FakeClient()
    report = service.run_extraction(session, client, limit=4, now=NOW)
    assert report.documents == 4 and report.due_soon == 2
    assert report.lists == 4 and report.items == 12 and report.dropped == 28
    picked = [
        row.document_id for row in session.scalars(select(ConditionList).order_by(ConditionList.id))
    ]
    # 30 天内截止的先（截止早的先），再是截止更晚或不知道的（新的先），已过截止的最后
    assert picked == [sooner.id, soon.id, unknown.id, later.id]
    # 发给模型的是脱敏后的正文，不带个人手机号
    assert all("13812345678" not in user and "[手机号已脱敏]" in user for user in client.calls)
    # 第二轮只抽剩下的，抽过的不再花用量
    report = service.run_extraction(session, client, limit=10, now=NOW)
    assert report.documents == 1
    assert session.scalars(select(ConditionList.document_id)).all()[-1] == expired.id
    assert service.run_extraction(session, client, limit=10, now=NOW).documents == 0
    row = session.scalar(select(ConditionList).where(ConditionList.document_id == sooner.id))
    assert row.status == "pending" and row.dropped == 7 and row.model == "fake-model"
    assert [item.status for item in row.items] == ["pending"] * 3
    check = session.scalar(select(AICheck).where(AICheck.document_id == sooner.id))
    assert check.task == "conditions" and check.error is None and check.result["items"] == 3


def test_run_extraction_records_errors_retries_later_and_handles_empty(session):
    first = add_notice(session, 1)
    second = add_notice(session, 2)
    failing = FakeClient(lambda user: AIError("AI 服务暂时不可用（HTTP 503）", retryable=True))
    report = service.run_extraction(session, failing, now=NOW)
    assert report.errors == 2 and report.lists == 0
    assert session.scalars(select(ConditionList)).all() == []
    # 出错不到 24 小时不重试，过了再试
    assert (
        service.run_extraction(session, FakeClient(), now=NOW + timedelta(hours=1)).documents == 0
    )
    # 去掉和前面重复的那条（单独出现时它是有效的），剩下的都通不过原文核对
    empty = FakeClient(lambda user: {"conditions": BAD[:5] + BAD[6:]})
    report = service.run_extraction(session, empty, now=NOW + timedelta(hours=25))
    assert report.documents == 2 and report.empty == 2 and report.lists == 0
    statuses = set(session.scalars(select(ConditionList.status)))
    assert statuses == {"empty"}
    # 没有条目的不进核对台，也不再重抽
    assert service.pending_count(session) == 0
    assert (
        service.run_extraction(session, FakeClient(), now=NOW + timedelta(hours=26)).documents == 0
    )
    assert {first.id, second.id} == set(session.scalars(select(ConditionList.document_id)))


def test_consecutive_errors_stop_the_round(session):
    for n in range(1, 6):
        add_notice(session, n)
    report = service.run_extraction(
        session, FakeClient(lambda user: AIError("AI 网络请求失败", retryable=True)), now=NOW
    )
    assert report.stopped_early and report.documents == 3


def test_body_change_supersedes_pending_list_and_reextracts(session):
    doc = add_notice(session, 1)
    service.run_extraction(session, FakeClient(), now=NOW)
    session.get(DocumentDetail, doc.id).body_text = BODY + "\n（四）近三年无失信记录。"
    session.commit()
    assert service.pending_count(session) == 0  # 旧版正文的清单立即不进核对台
    service.run_extraction(session, FakeClient(), now=NOW)
    rows = session.scalars(select(ConditionList).order_by(ConditionList.id)).all()
    assert [row.status for row in rows] == ["superseded", "pending"]
    assert service.pending_count(session) == 1


def test_cli_skips_without_ai_key(monkeypatch, capsys):
    monkeypatch.delenv("YOUJU_AI_API_KEY", raising=False)
    get_settings.cache_clear()

    def no_database():
        raise AssertionError("没配 AI 时不应连接数据库")

    monkeypatch.setattr(cli, "session_factory", no_database)
    try:
        assert cli.main(["extract-conditions", "--limit", "5"]) == 0
    finally:
        get_settings.cache_clear()
    assert "跳过申报条件抽取" in capsys.readouterr().out


# ---- 核对台和对外 ----


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(
        app_module, "get_settings", lambda: SimpleNamespace(admin_token="secret", role="all")
    )
    app_module.app.dependency_overrides[get_session] = override
    v1._REPORT_CACHE.clear()
    with TestClient(app_module.app) as client:
        yield client
    app_module.app.dependency_overrides.clear()


@pytest.fixture
def audit_records():
    records: list[dict] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(json.loads(record.getMessage()))

    handler = Collect()
    logger = logging.getLogger("youju.audit")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def extracted(session, n=1, **kwargs):
    doc = add_notice(session, n, **kwargs)
    service.run_extraction(session, FakeClient(), now=NOW)
    row = session.scalar(select(ConditionList).where(ConditionList.document_id == doc.id))
    return doc, row


def act(client, path, **body):
    return client.post(
        f"/api/admin/conditions/{path}", headers=HEADERS, json={"reviewed_by": "创始人", **body}
    )


def detail(client, doc_id):
    return client.get(f"/api/v1/documents/{doc_id}").json()


def test_admin_api_requires_token(client):
    for path in ("/api/admin/conditions", "/api/admin/conditions/count"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert (
        client.post("/api/admin/conditions/1/confirm", json={"reviewed_by": "x"}).status_code == 401
    )
    page = client.get("/admin/conditions")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'id="token"' in page.text and "/api/admin/conditions" in page.text


def test_queue_shows_items_with_highlight_context(session, client):
    doc, row = extracted(session, due=date(2026, 10, 10))
    data = client.get("/api/admin/conditions", headers=HEADERS).json()
    assert client.get("/api/admin/conditions/count", headers=HEADERS).json() == {"pending": 1}
    assert len(data) == 1
    card = data[0]
    assert (card["list_id"], card["document_id"], card["tier"], card["due"]) == (
        row.id,
        doc.id,
        0,
        "2026-10-10",
    )
    assert card["tier_label"] == "30 天内截止" and card["dropped"] == 7
    body = service.public_body(BODY)
    for item in card["items"]:
        # 上下文 + 引用 + 上下文 就是正文里连续的一段
        assert item["before"] + item["quote"] + item["after"] in body
        assert item["status"] == "pending"


def test_unconfirmed_list_is_never_public_and_confirm_needs_every_item(session, client):
    doc, row = extracted(session)
    first, second, third = (item.id for item in row.items)
    assert "apply_conditions" not in detail(client, doc.id)
    assert act(client, f"items/{first}/approve").json() == {"status": "approved"}
    # 逐条通过了也不对外，要整份确认
    assert "apply_conditions" not in detail(client, doc.id)
    response = act(client, f"{row.id}/confirm")
    assert response.status_code == 409 and "还有 2 条没核对" in response.json()["detail"]
    assert act(client, f"items/{second}/delete").json() == {"status": "deleted"}
    assert act(client, f"items/{third}/approve").status_code == 200
    assert act(client, f"{row.id}/confirm").json() == {"status": "confirmed", "approved": 2}
    shown = detail(client, doc.id)["apply_conditions"]
    # 对外只给类别和原文原句，条件那句话（模型的概括）不给
    assert shown == {
        "items": [
            {"category": "主体资格", "quote": "（一）申报单位注册地在本市，具有独立法人资格。"},
            {"category": "知识产权", "quote": "（三）拥有与主营业务相关的发明专利 2 件以上。"},
        ],
        "checked": datetime.now(UTC).astimezone(service.BEIJING).date().isoformat(),
    }
    assert client.get("/api/admin/conditions/count", headers=HEADERS).json() == {"pending": 0}


def test_edit_keeps_quote_and_is_audited(session, client, audit_records):
    doc, row = extracted(session)
    item = row.items[1]
    quote, offset = item.quote, item.quote_offset
    response = act(
        client,
        f"items/{item.id}/edit",
        text="  上年度研发投入占营业收入的比例 不低于 3%  ",
        category="其他",
        quote="随便改的引用",
        quote_offset=0,
    )
    assert response.json() == {
        "status": "approved",
        "text": "上年度研发投入占营业收入的比例 不低于 3%",
        "category": "其他",
    }
    session.expire_all()
    edited = session.get(ConditionItem, item.id)
    assert (edited.quote, edited.quote_offset) == (quote, offset)
    assert edited.original_text == "上年度研发投入占营业收入比例不低于 3%"
    assert edited.reviewed_by == "创始人" and edited.reviewed_at is not None
    record = next(r for r in audit_records if r["action"] == "condition_edit")
    assert record["before"]["text"] == "上年度研发投入占营业收入比例不低于 3%"
    assert record["after"] == {
        "status": "approved",
        "text": "上年度研发投入占营业收入的比例 不低于 3%",
        "category": "其他",
    }
    for other in row.items:
        if other.id != item.id:
            act(client, f"items/{other.id}/delete")
    act(client, f"{row.id}/confirm")
    assert any(r["action"] == "conditions_confirm" for r in audit_records)
    # 人工改写的那句话也不对外，对外永远是原文原句
    shown = detail(client, doc.id)["apply_conditions"]
    assert shown["items"] == [{"category": "其他", "quote": quote}]
    assert "不低于 3%" not in json.dumps(shown, ensure_ascii=False)


def test_edit_rejects_judgement_and_empty_text(session, client):
    _, row = extracted(session)
    item = row.items[0]
    response = act(client, f"items/{item.id}/edit", text="注册地在本市的企业可以申报")
    assert response.status_code == 422 and "不写能否申报" in response.json()["detail"]
    assert act(client, f"items/{item.id}/edit", text="   ").status_code == 422
    assert act(client, f"items/{item.id}/edit", text="条" * 121).status_code == 422
    assert (
        act(client, f"items/{item.id}/edit", text="注册地在本市", category="资质").status_code
        == 422
    )


def test_confirmed_list_is_locked_until_reopened_and_reopen_hides_it(session, client):
    doc, row = extracted(session)
    for item in row.items:
        act(client, f"items/{item.id}/approve")
    act(client, f"{row.id}/confirm")
    assert len(detail(client, doc.id)["apply_conditions"]["items"]) == 3
    response = act(client, f"items/{row.items[0].id}/delete")
    assert response.status_code == 409 and "先撤回确认" in response.json()["detail"]
    confirmed = client.get("/api/admin/conditions?state=confirmed", headers=HEADERS).json()
    assert [card["list_id"] for card in confirmed] == [row.id] and confirmed[0]["current"]
    assert act(client, f"{row.id}/reopen").json() == {"status": "pending"}
    assert "apply_conditions" not in detail(client, doc.id)
    assert act(client, f"{row.id}/reopen").status_code == 409
    # 撤回后回到待核对，逐条结果保留，改完可以再确认
    assert act(client, f"items/{row.items[0].id}/reset").json() == {"status": "pending"}
    assert act(client, f"{row.id}/confirm").status_code == 409
    act(client, f"items/{row.items[0].id}/delete")
    assert act(client, f"{row.id}/confirm").json()["approved"] == 2


def test_all_deleted_confirmed_list_shows_no_card(session, client):
    doc, row = extracted(session)
    for item in row.items:
        act(client, f"items/{item.id}/delete")
    assert act(client, f"{row.id}/confirm").json() == {"status": "confirmed", "approved": 0}
    assert "apply_conditions" not in detail(client, doc.id)


def test_body_change_hides_confirmed_list_immediately(session, client):
    doc, row = extracted(session)
    for item in row.items:
        act(client, f"items/{item.id}/approve")
    act(client, f"{row.id}/confirm")
    assert "apply_conditions" in detail(client, doc.id)
    session.get(DocumentDetail, doc.id).body_text = BODY + "\n补充说明。"
    session.commit()
    # 不等重新抽取：正文指纹对不上就不再显示，也不能再核对
    assert "apply_conditions" not in detail(client, doc.id)
    act(client, f"{row.id}/reopen")
    response = act(client, f"items/{row.items[0].id}/approve")
    assert response.status_code == 409 and "原文已变化" in response.json()["detail"]


def test_kind_change_hides_conditions(session, client):
    doc, row = extracted(session)
    for item in row.items:
        act(client, f"items/{item.id}/approve")
    act(client, f"{row.id}/confirm")
    assert "apply_conditions" in detail(client, doc.id)
    doc = session.get(Document, doc.id)
    doc.kind_override = "政策"
    session.commit()
    assert "apply_conditions" not in detail(client, doc.id)
    assert service.public_conditions(session, doc.id) is None


@pytest.mark.parametrize("public_is_master", [True, False])
def test_merged_group_uses_confirmed_list_from_public_member(session, client, public_is_master):
    master = add_notice(session, 1, rights=1 if public_is_master else 2)
    twin = add_notice(session, 2, rights=2 if public_is_master else 1)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="k")
    )
    session.commit()
    public, private = (master, twin) if public_is_master else (twin, master)
    for doc in (public, private):
        digest = service.body_fingerprint(BODY)
        result = validate({"conditions": GOOD}, service.public_body(BODY))
        row = service.save_list(session, doc.id, digest, result, model="m", now=NOW)
        for item in row.items:
            item.status = "approved"
            if doc is private:
                item.text = "非公开来源的条件"
        row.status, row.confirmed_by, row.confirmed_at = "confirmed", "创始人", NOW
        session.commit()
    items = detail(client, master.id)["apply_conditions"]["items"]
    assert len(items) == 3 and all(set(item) == {"category", "quote"} for item in items)


def test_public_payload_shape_and_wording(session, client):
    doc, row = extracted(session)
    for item in row.items:
        act(client, f"items/{item.id}/approve")
    act(client, f"{row.id}/confirm")
    shown = detail(client, doc.id)["apply_conditions"]
    assert set(shown) == {"items", "checked"}
    assert all(set(item) == {"category", "quote"} for item in shown["items"])
    text = json.dumps(shown, ensure_ascii=False)
    for word in ("AI", "智能", "大模型", "模型", "fake-model", "能否申报"):
        assert word not in text
    assert date.fromisoformat(shown["checked"])


def test_same_sentence_split_into_several_items_is_shown_once(session, client):
    """一句原文拆成了几条（同一句里的几项要求）：对外只列一次原句，列在排在前面的类别下。"""
    doc = add_notice(session, 1)
    split = [
        {"category": "财务指标", "text": "具有独立法人资格", "quote": "具有独立法人资格"},
        {"category": "主体资格", "text": "注册地在本市", "quote": "申报单位注册地在本市"},
    ]
    service.run_extraction(session, FakeClient(lambda user: {"conditions": split}), now=NOW)
    row = session.scalar(select(ConditionList).where(ConditionList.document_id == doc.id))
    assert [item.quote for item in row.items] == [
        "（一）申报单位注册地在本市，具有独立法人资格。"
    ] * 2
    for item in row.items:
        act(client, f"items/{item.id}/approve")
    act(client, f"{row.id}/confirm")
    assert detail(client, doc.id)["apply_conditions"]["items"] == [
        {"category": "主体资格", "quote": "（一）申报单位注册地在本市，具有独立法人资格。"}
    ]


def test_body_changed_and_back_revives_pending_list(session, client, caplog):
    """正文 A → B → A：A 版那份被替代的清单恢复成待核对，不卡住；B 版的待核对清单改为被替代。"""
    doc = add_notice(session, 1)
    service.run_extraction(session, FakeClient(), now=NOW)
    first = session.scalar(select(ConditionList).where(ConditionList.document_id == doc.id))
    act(client, f"items/{first.items[0].id}/approve")  # 核了一半
    session.get(DocumentDetail, doc.id).body_text = BODY + "\n（五）近三年无失信记录。"
    session.commit()
    service.run_extraction(session, FakeClient(), now=NOW)
    session.get(DocumentDetail, doc.id).body_text = BODY
    session.commit()
    session.expire_all()
    assert [
        r.status for r in session.scalars(select(ConditionList).order_by(ConditionList.id))
    ] == [
        "superseded",
        "pending",
    ]
    # 改回 A 版后：恢复前 A 版清单既不重抽也不进核对台（以前就卡在这里）
    assert service.pending_count(session) == 0
    client_calls = FakeClient()
    with caplog.at_level(logging.INFO, logger="youju.conditions"):
        report = service.run_extraction(session, client_calls, now=NOW)
    assert report.revived == 1 and report.documents == 0 and client_calls.calls == []
    session.expire_all()
    rows = session.scalars(select(ConditionList).order_by(ConditionList.id)).all()
    assert [r.status for r in rows] == ["pending", "superseded"]
    assert service.pending_ids(session) == [(first.id, doc.id)]
    # 已核过的那条保留，接着核对就行
    assert [item.status for item in rows[0].items] == ["approved", "pending", "pending"]
    assert any("恢复为 pending" in r.getMessage() for r in caplog.records)
    # 再跑一轮不会重复恢复
    assert service.run_extraction(session, FakeClient(), now=NOW).revived == 0


def test_revived_list_that_was_confirmed_goes_back_to_confirmed(session, client):
    doc, row = extracted(session)
    for item in row.items:
        act(client, f"items/{item.id}/approve")
    act(client, f"{row.id}/confirm")
    session.expire_all()
    # 旧数据里可能有确认人、确认时间都在却被标成被替代的清单：恢复成已确认，原样对外
    row = session.get(ConditionList, row.id)
    row.status = "superseded"
    session.commit()
    assert "apply_conditions" not in detail(client, doc.id)
    assert service.revive_lists(session) == [(row.id, doc.id, "confirmed")]
    assert len(detail(client, doc.id)["apply_conditions"]["items"]) == 3


def test_eval_script_simulated_model_never_calls_real_model():
    """评测脚本的第一步只用模拟模型和校验：留下的引用都在正文里逐字找得到，混进去的坏条目都被丢掉。"""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "eval_conditions", Path(__file__).parents[1] / "scripts" / "eval_conditions.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert not hasattr(script, "ChatClient") and not hasattr(script, "get_chat_client")
    summary = script.check_validation([{"title": "关于组织申报科技项目的通知", "body_text": BODY}])
    assert summary["documents"] == 1 and summary["kept"] >= 2
    assert summary["verbatim_errors"] == 0
    assert summary["dropped"].get("judgement") == summary["planted"]["judgement"] == 1
