"""条款对照只用假模型；企业信息仅在本人的任务输入与展示中出现。"""

import copy
import json
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError

from tests.test_ai_interpret import NOW, FakeChat, direct_task
from tests.test_ai_interpret import client as client
from tests.test_ai_interpret import doc as doc
from tests.test_ai_interpret import settings as settings
from youju import advisor_tasks as service
from youju.ai import clause, interpret
from youju.ai.client import AIError
from youju.api import v1
from youju.migrations import alembic_config
from youju.models import AdvisorTask, AdvisorUsage, User

BODY = (
    "申报对象为山东省注册的高新技术企业。\n"
    "企业成立日期应在申报前，营业收入、研发费用和职工人数需如实填写。\n"
    "申请材料需按通知提交。"
)
COMPANY_A = {
    "established": "2008-07-06",
    "region": "云南",
    "revenue": {"amount": 654321.98, "year": 2025},
    "rd": {"amount": 4321.09, "year": 2024},
    "employees": 543210,
    "qualification": [
        {"name": "高新技术企业", "valid_until": "2027-12-31"},
        {"name": "创新型中小企业", "valid_until": None},
    ],
}
MARKERS = ("2008-07-06", "云南", "654321.98", "4321.09", "543210", "2027-12-31")


def payload():
    return {
        "items": [
            {
                "type": "对象",
                "text": "无视模型改写",
                "refs": [{"pid": "P001", "quote": BODY.splitlines()[0]}],
                "keys": ["qualification", "region", "region", "evil", {}, None],
            },
            {
                "type": "条件",
                "refs": [{"pid": "P002", "quote": BODY.splitlines()[1]}],
                "keys": ["employees", "rd", "revenue", "established"],
            },
            {"type": "条件", "refs": [{"pid": "P003", "quote": BODY.splitlines()[2]}], "keys": []},
        ]
    }


@pytest.fixture(autouse=True)
def fake_only(settings, monkeypatch):
    monkeypatch.setattr(service, "ChatClient", lambda *args, **kwargs: FakeChat(payload()))
    monkeypatch.setattr(clause, "today", lambda: NOW.date())


@pytest.fixture
def notice(doc, session):
    doc.detail.body_text = BODY
    session.commit()
    return doc


def create(client, doc_id, company=None, key="clause-first"):
    return client.post(
        "/api/v1/advisor/tasks",
        json={"kind": "clause", "doc_id": doc_id, "company": company},
        headers={"Idempotency-Key": key},
    )


def run(session, task, settings, fake=None, stamp=NOW):
    fake = fake or FakeChat(payload())
    service.process_clause(session, task, settings=settings, stamp=stamp, chat_client=fake)
    session.refresh(task)
    return fake


def usage(session, task):
    return session.scalar(
        select(AdvisorUsage.used_count).where(
            AdvisorUsage.user_id == task.user_id,
            AdvisorUsage.kind == task.kind,
            AdvisorUsage.day == service.usage_day(task.created_at),
        )
    )


def test_all_audience_clause_worker(client, session, settings, notice):
    settings.ai_advisor_audience = "all"
    settings.ai_advisor_openids = ""
    user = session.scalar(select(User).where(User.openid == "internal"))
    user.agreement_version = interpret.AI_AGREEMENT_VERSION
    session.commit()
    created = create(client, notice.id, COMPANY_A)
    assert created.status_code == 202
    assert service.run_advisor_tasks(session, settings=settings, stamp=NOW)["done"] == 1
    task = session.get(AdvisorTask, created.json()["id"])
    assert task.status == "done" and usage(session, task) == 1


@pytest.mark.parametrize(
    "company",
    [
        {"unknown": "任意文本"},
        "任意文本",
        [],
        7,
        {"established": "2026-10-04"},
        {"established": "2025-02-29"},
        {"established": "2025-1-01"},
        {"established": "2025-01-01T00:00:00"},
        {"established": 1750000000},
        {"established": True},
        {"region": "国家"},
        {"region": "国家部委"},
        {"region": "山东省"},
        {"region": "自由输入"},
        {"region": 1},
        {"region": True},
        *[
            {key: value}
            for key in ("revenue", "rd")
            for value in (
                "收入很多",
                [],
                {},
                {"amount": 1},
                {"year": 2025},
                {"amount": -1, "year": 2025},
                {"amount": 1e9 + 1, "year": 2025},
                {"amount": 1.001, "year": 2025},
                {"amount": True, "year": 2025},
                {"amount": "1", "year": 2025},
                {"amount": None, "year": 2025},
                {"amount": 1, "year": 1999},
                {"amount": 1, "year": 2027},
                {"amount": 1, "year": "2025"},
                {"amount": 1, "year": 2025.0},
                {"amount": 1, "year": True},
                {"amount": 1, "year": 2025, "note": "自由输入"},
            )
        ],
        *[{"employees": value} for value in (-1, 1000001, 2.5, 2.0, True, "56", [])],
        {"qualification": "高新技术企业"},
        {"qualification": ["高新技术企业"]},
        {"qualification": [None]},
        {"qualification": [{}]},
        {"qualification": [{"name": "自定义资质"}]},
        {"qualification": [{"name": "高新技术企业", "extra": "任意文本"}]},
        {"qualification": [{"name": "高新技术企业", "valid_until": "2027-02-29"}]},
        {"qualification": [{"name": "高新技术企业", "valid_until": "2027-1-01"}]},
        {"qualification": [{"name": "高新技术企业", "valid_until": 1800000000}]},
        {"qualification": [{"name": "高新技术企业"}] * 7},
    ],
)
def test_invalid_company_is_422_without_quota(client, session, notice, company):
    assert create(client, notice.id, company).status_code == 422
    assert session.scalar(select(AdvisorUsage.id)) is None
    assert session.scalar(select(AdvisorTask.id)) is None


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "clause"},
        {"kind": "clause", "doc_id": True},
        {"kind": "clause", "doc_id": "1"},
        {"kind": "clause", "doc_id": 0},
        {"kind": "clause", "doc_id": 1, "rows": None},
        {"kind": "clause", "doc_id": 1, "text": "自由文本"},
        {"kind": "interpret", "doc_id": 1, "company": {}},
        {"kind": "compare", "rows": ["甲公司"], "company": {}},
        {"kind": "track", "rows": [{}], "company": {}},
    ],
)
def test_task_input_shape(client, settings, body):
    settings.advisor_enabled = True
    assert (
        client.post(
            "/api/v1/advisor/tasks", json=body, headers={"Idempotency-Key": "shape"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "company",
    [
        None,
        {},
        dict.fromkeys(clause.KEYS),
        {
            "established": "2026-10-03",
            "region": "山东",
            "employees": 0,
            "revenue": {"amount": 0, "year": 2000},
            "rd": {"amount": 1e9, "year": 2026},
            "qualification": [{"name": "高新技术企业"}] * 6,
        },
    ],
)
def test_allowed_company_and_no_company_model_input(client, session, settings, notice, company):
    response = create(client, notice.id, company)
    assert response.status_code == 202
    assert response.json()["hasResult"] is False and "result" not in response.json()
    task = session.get(AdvisorTask, response.json()["id"])
    fake = run(session, task, settings)
    assert fake.calls == [
        (clause.SYSTEM_PROMPT, {"paragraphs": task.snapshot["paragraphs"]}, 12000)
    ]
    assert task.prompt_version == clause.PROMPT_VERSION and task.result_rows == 3
    result = client.get(f"/api/v1/advisor/tasks/{task.id}").json()["result"]
    if not company or all(v is None for v in company.values()):
        assert all(c["value"] == "未提供" for row in result["rows"] for c in row["company"])
    assert result["rows"][-1]["company"] == []
    assert "dropped_reasons" not in result
    assert set(task.result_json) <= set(clause.RESULT_FIELDS)
    assert not any("company" in row for row in task.result_json["rows"])


@pytest.mark.parametrize(
    "keys,expected",
    [
        (
            ["employees", "region", "established", "region", {}, [], None, 7, "evil"],
            ["established", "region", "employees"],
        ),
        (None, []),
        ("region", []),
        ({"region": True}, []),
        ([], []),
    ],
)
def test_key_filter_and_type_whitelist(keys, expected):
    data = payload()
    data["items"][0]["keys"] = keys
    data["items"].append({**data["items"][0], "type": "支持"})
    result = interpret.validate(data, interpret.split_paragraphs(BODY), skill=clause.CLAUSE)
    assert result["rows"][0]["keys"] == expected
    assert result["rows"][0]["text"] == BODY.splitlines()[0]
    assert result["dropped_reasons"]["bad_shape"] == 1
    assert set(r["type"] for r in result["rows"]) == {"对象", "条件"}


@pytest.mark.parametrize("cache", [True, False])
@pytest.mark.parametrize("company_b", [{}, {"employees": 12, "region": "北京"}])
def test_cross_user_isolation_every_output(client, session, settings, notice, cache, company_b):
    first = session.get(AdvisorTask, create(client, notice.id, COMPANY_A).json()["id"])
    run(session, first, settings)
    assert all(m not in json.dumps(first.result_json, ensure_ascii=False) for m in MARKERS)
    assert all(m not in json.dumps(first.snapshot, ensure_ascii=False) for m in MARKERS)
    own_result = client.get(f"/api/v1/advisor/tasks/{first.id}").json()["result"]
    cells = {c["key"]: c["value"] for row in own_result["rows"] for c in row["company"]}
    assert cells == {
        "established": "2008-07-06",
        "region": "云南",
        "revenue": "2025 年 654321.98 万元",
        "rd": "2024 年 4321.09 万元",
        "employees": "543210 人",
        "qualification": "高新技术企业（有效期至 2027-12-31）、创新型中小企业",
    }
    # 若调用方曾把展示结果错写回缓存，也只允许拷贝通知字段。
    poisoned = copy.deepcopy(first.result_json)
    poisoned["company"] = COMPANY_A
    poisoned["rows"][0]["company"] = own_result["rows"][0]["company"]
    first.result_json = poisoned
    if not cache:
        first.prompt_version = "old-version"
    session.commit()
    settings.dev_openid = "second"
    assert client.post("/api/v1/me/agreement", json={"version": "2026-10"}).status_code == 200
    second = session.get(AdvisorTask, create(client, notice.id, company_b).json()["id"])
    fake = run(session, second, settings)
    assert len(fake.calls) == (0 if cache else 1)
    assert second.cache_key == first.cache_key
    assert all(m not in json.dumps(second.result_json, ensure_ascii=False) for m in MARKERS)
    assert set(second.result_json) <= set(clause.RESULT_FIELDS)
    assert "company" not in second.result_json["rows"][0]
    for path in (f"/api/v1/advisor/tasks/{second.id}", "/api/v1/advisor/tasks", "/api/v1/me/data"):
        response = client.get(path)
        assert response.status_code == 200
        assert all(m not in response.text for m in MARKERS)
    history = client.get("/api/v1/me/data").json()["advisorTasks"]
    assert [r["id"] for r in history] == [second.id]
    assert history[0]["input_json"]["company"] == second.input_json["company"]
    assert history[0]["result"]["rows"]
    assert client.get(f"/api/v1/advisor/tasks/{first.id}").status_code == 404
    listed = client.get("/api/v1/advisor/tasks").json()["items"]
    assert listed[0]["hasResult"] is True
    assert not {"result", "company", "input_json", "snapshot"} & listed[0].keys()
    settings.dev_openid = "internal"
    own_history = client.get("/api/v1/me/data").json()["advisorTasks"]
    assert own_history[0]["input_json"]["company"] == first.input_json["company"]
    assert all(m in json.dumps(own_history, ensure_ascii=False) for m in MARKERS)


def test_cleanup_exact_seven_days_and_cached_result(client, session, settings, notice, monkeypatch):
    task = session.get(AdvisorTask, create(client, notice.id, COMPANY_A).json()["id"])
    run(session, task, settings)
    original = copy.deepcopy(task.result_json)
    boundary = NOW + timedelta(days=7)
    service.cleanup_advisor(session, settings=settings, stamp=boundary - timedelta(microseconds=1))
    session.refresh(task)
    assert task.input_json["company"]
    service.cleanup_advisor(session, settings=settings, stamp=boundary)
    session.refresh(task)
    assert task.input_json is None and task.result_json == original
    monkeypatch.setattr(v1, "now", lambda: boundary)
    result = client.get(f"/api/v1/advisor/tasks/{task.id}").json()["result"]
    assert all(c["value"] == "企业自述已过期清除" for row in result["rows"] for c in row["company"])
    assert result["rows"][-1]["company"] == []
    exported = client.get("/api/v1/me/data").json()["advisorTasks"][0]
    assert "input_json" not in exported and exported["result"] == result
    new = session.get(
        AdvisorTask, create(client, notice.id, {"employees": 56}, key="later").json()["id"]
    )
    fake = run(session, new, settings, stamp=boundary)
    assert not fake.calls and new.result_json == original
    task_id = task.id
    service.cleanup_advisor(session, settings=settings, stamp=NOW + timedelta(days=30))
    session.expire_all()
    # 过了 30 天结果保存期，任务整行删除（和用户的关联也不再保留）
    assert session.get(AdvisorTask, task_id) is None


def test_expired_company_hidden_before_cleanup(client, session, settings, notice, monkeypatch):
    task = session.get(AdvisorTask, create(client, notice.id, COMPANY_A).json()["id"])
    run(session, task, settings)
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(days=7))
    # 即使定时清理延迟，读取接口也不能延长企业自述的保存期限。
    assert task.input_json["company"] == COMPANY_A
    for path in (f"/api/v1/advisor/tasks/{task.id}", "/api/v1/me/data"):
        response = client.get(path)
        assert response.status_code == 200
        assert all(marker not in response.text for marker in MARKERS)
        assert "企业自述已过期清除" in response.text
    assert "input_json" not in client.get("/api/v1/me/data").json()["advisorTasks"][0]


def test_company_omitted_and_model_judgment_discarded(client, session, settings, notice):
    response = client.post(
        "/api/v1/advisor/tasks",
        json={"kind": "clause", "doc_id": notice.id},
        headers={"Idempotency-Key": "omitted"},
    )
    assert response.status_code == 202
    task = session.get(AdvisorTask, response.json()["id"])
    data = payload()
    data["judgment"] = "符合，可以申报"
    data["items"][0].update(text="满足", judgment="符合", company=COMPANY_A)
    data["items"].append({**data["items"][0], "type": "判定"})
    run(session, task, settings, FakeChat(data))
    result = client.get(f"/api/v1/advisor/tasks/{task.id}").json()["result"]
    assert task.input_json["company"] == {}
    assert all(c["value"] == "未提供" for row in result["rows"] for c in row["company"])
    assert all(set(row) == {"type", "text", "refs", "keys", "company"} for row in result["rows"])
    assert all(
        word not in json.dumps(result, ensure_ascii=False)
        for word in ("符合", "满足", "可以申报", "判定")
    )


@pytest.mark.parametrize("field", ["revenue", "rd"])
@pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_amounts_rejected(field, amount):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        clause.Company.model_validate({field: {"amount": amount, "year": 2025}})


@pytest.mark.parametrize("data_enabled", [False, True])
@pytest.mark.parametrize("ai_enabled", [False, True])
@pytest.mark.parametrize("whitelisted", [False, True])
@pytest.mark.parametrize("kind", ["clause", "invalid"])
def test_creation_permission_matrix(
    client, session, settings, notice, data_enabled, ai_enabled, whitelisted, kind
):
    settings.advisor_enabled = data_enabled
    settings.ai_advisor_enabled = ai_enabled
    settings.ai_advisor_openids = "internal" if whitelisted else "second"
    response = client.post(
        "/api/v1/advisor/tasks",
        json={"kind": kind, "doc_id": notice.id},
        headers={"Idempotency-Key": "permission"},
    )
    authorized = ai_enabled and whitelisted
    expected = (
        404
        if not data_enabled and not ai_enabled
        else 202
        if authorized and kind == "clause"
        else 422
        if data_enabled or authorized
        else 404
    )
    assert response.status_code == expected
    if expected == 422 and not authorized:
        assert response.json() == {"detail": "kind 只能是 compare 或 track"}
    if expected != 202:
        assert session.scalar(select(AdvisorTask.id)) is None
        assert session.scalar(select(AdvisorUsage.id)) is None


def test_separate_five_per_day_quota_idempotency_cancel_and_failure(
    client, session, settings, notice
):
    for i in range(5):
        assert create(client, notice.id, key=f"quota-{i}").status_code == 202
    assert create(client, notice.id, key="sixth").status_code == 429
    assert create(client, notice.id, key="quota-0").status_code == 202
    assert create(client, notice.id, {"employees": 1}, key="quota-0").status_code == 409
    assert (
        client.post(
            "/api/v1/advisor/tasks",
            json={"kind": "interpret", "doc_id": notice.id},
            headers={"Idempotency-Key": "interpret-separate"},
        ).status_code
        == 202
    )
    caps = client.get("/api/v1/advisor/capabilities").json()["ai_advisor"]
    assert caps["features"] == ["interpret", "clause"]
    assert caps["remaining"] == {"interpret": 4, "clause": 0}
    task = session.scalar(select(AdvisorTask).where(AdvisorTask.idempotency_key == "quota-0"))
    assert client.post(f"/api/v1/advisor/tasks/{task.id}/cancel").status_code == 200
    assert create(client, notice.id, key="replacement").status_code == 202
    failed = session.scalar(select(AdvisorTask).where(AdvisorTask.idempotency_key == "replacement"))
    run(session, failed, settings, FakeChat(error=AIError("响应不可用", retryable=False)))
    assert failed.status == "failed" and usage(session, failed) == 4
    assert create(client, notice.id, key="after-failure").status_code == 202


@pytest.mark.parametrize("cause", ["not_found", "empty", "invalid_shape", "retry", "body_changed"])
def test_refusal_failure_retry_refund_and_no_company_leak(client, session, settings, notice, cause):
    task = session.get(AdvisorTask, create(client, notice.id, COMPANY_A).json()["id"])
    fake = FakeChat(payload())
    if cause == "not_found":
        task.input_json = {**task.input_json, "doc_id": 999999}
        session.commit()
    elif cause == "empty":
        fake = FakeChat({"items": []})
    elif cause == "invalid_shape":
        fake = FakeChat({"items": "not-a-list"})
    elif cause == "retry":
        fake = FakeChat(error=AIError("temporary", retryable=True))
    else:

        class Changed(FakeChat):
            def complete_json(self, *args, **kwargs):
                response = super().complete_json(*args, **kwargs)
                notice.detail.body_text += "正文已更改。"
                session.flush()
                return response

        fake = Changed(payload())
    run(session, task, settings, fake)
    if cause == "retry":
        assert task.status == "queued" and usage(session, task) == 1
        task.attempts = 3
        session.commit()
        run(session, task, settings, FakeChat(error=AIError("temporary", retryable=True)))
        assert task.status == "failed"
    assert usage(session, task) == 0
    if task.result_json is not None:
        assert task.result_json["refusal"] and task.result_json["rows"] == []
        assert all(m not in json.dumps(task.result_json, ensure_ascii=False) for m in MARKERS)
    settings.dev_openid = "second"
    assert client.post("/api/v1/me/agreement", json={"version": "2026-10"}).status_code == 200
    second = session.get(AdvisorTask, create(client, notice.id, key="second").json()["id"])
    second_fake = run(session, second, settings)
    assert len(second_fake.calls) == (0 if cause == "empty" else 1)
    assert all(m not in client.get("/api/v1/me/data").text for m in MARKERS)


@pytest.mark.parametrize("revoked", ["switch", "whitelist"])
def test_worker_revocation_refunds_and_personal_export_retained(
    client, session, settings, notice, revoked
):
    completed = session.get(AdvisorTask, create(client, notice.id, COMPANY_A).json()["id"])
    run(session, completed, settings)
    queued = session.get(AdvisorTask, create(client, notice.id, key="queue").json()["id"])
    if revoked == "switch":
        settings.ai_advisor_enabled = False
    else:
        settings.ai_advisor_openids = "second"
    assert service.run_advisor_tasks(session, settings=settings, stamp=NOW)["processed"] == 0
    session.refresh(queued)
    assert queued.status == "failed" and usage(session, queued) == 1
    assert client.get(f"/api/v1/advisor/tasks/{completed.id}").status_code == 404
    own_export = client.get("/api/v1/me/data").json()["advisorTasks"]
    assert any(
        r.get("input_json", {}).get("company") == completed.input_json["company"]
        for r in own_export
    )


def test_worker_list_metadata_invalidation_delete_and_logout(
    client, session, settings, notice, monkeypatch
):
    task_id = create(client, notice.id, COMPANY_A).json()["id"]
    assert service.run_advisor_tasks(session, settings=settings, stamp=NOW)["done"] == 1
    task = session.get(AdvisorTask, task_id)
    session.refresh(task)
    assert task.result_json["rows"]
    prepare = interpret.prepare

    def forbidden(*args, **kwargs):
        raise AssertionError("列表与幂等重放不能读取正文")

    monkeypatch.setattr(interpret, "prepare", forbidden)
    assert client.get("/api/v1/advisor/tasks").json()["items"][0]["hasResult"]
    replay = create(client, notice.id, COMPANY_A).json()
    assert replay["hasResult"] and "result" not in replay
    monkeypatch.setattr(interpret, "prepare", prepare)
    notice.taken_down_at = NOW
    session.commit()
    assert client.get(f"/api/v1/advisor/tasks/{task_id}").json()["result"] is None
    assert client.delete(f"/api/v1/advisor/tasks/{task_id}").status_code == 204
    second_id = create(client, notice.id, COMPANY_A, key="logout").json()["id"]
    assert client.delete("/api/v1/me").status_code == 204
    session.expire_all()
    assert session.get(AdvisorTask, task_id) is None and session.get(AdvisorTask, second_id) is None
    assert session.scalar(select(AdvisorUsage.id)) is None
    assert session.scalar(select(User.id)) is None


def test_migration_0015_only_constraint_and_cleanup(db_engine, session, settings, doc):
    old = direct_task(session, doc)
    ai = AdvisorTask(
        user_id=old.user_id,
        kind="clause",
        status="queued",
        row_count=0,
        created_at=NOW,
        idempotency_key="migration",
    )
    session.add(ai)
    assert service.reserve_usage(session, old.user_id, "clause", service.usage_day(NOW))
    session.commit()

    def columns():
        # 反射类型对象没有值相等语义，比较列的完整属性和类型表示。
        return [
            {**column, "type": str(column["type"])}
            for column in inspect(db_engine).get_columns("advisor_tasks")
        ]

    original_columns = columns()
    cfg = alembic_config(db_engine.url.render_as_string(hide_password=False))
    # 只比较 0015 本身的列变化；后续 0026 已合法增加使用期额度列。
    command.downgrade(cfg, "0015")
    columns_0015 = columns()
    command.downgrade(cfg, "0014")
    try:
        assert columns() == columns_0015
        with db_engine.connect() as conn:
            assert conn.scalar(text("SELECT COUNT(*) FROM advisor_tasks WHERE kind='clause'")) == 0
            assert conn.scalar(text("SELECT COUNT(*) FROM advisor_usage WHERE kind='clause'")) == 0
            assert (
                conn.scalar(text("SELECT COUNT(*) FROM advisor_tasks WHERE kind='interpret'")) == 1
            )
            assert (
                conn.scalar(text("SELECT used_count FROM advisor_usage WHERE kind='interpret'"))
                == 1
            )
        with pytest.raises(OperationalError) as rejected, db_engine.begin() as conn:
            conn.execute(text("UPDATE advisor_tasks SET kind='clause' WHERE kind='interpret'"))
        assert rejected.value.orig.args[0] == 3819  # MySQL CHECK 约束违例
    finally:
        command.upgrade(cfg, "head")
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE advisor_tasks SET kind='clause' WHERE kind='interpret'"))
    assert columns() == original_columns
