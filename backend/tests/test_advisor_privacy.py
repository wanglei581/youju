"""顾问接口的授权隐藏及通知元数据；全程使用测试库和假模型。"""

import re
from datetime import timedelta

import pytest
from sqlalchemy import delete, event

from tests.test_ai_interpret import NOW, direct_task, run
from tests.test_ai_interpret import client as client
from tests.test_ai_interpret import doc as doc
from tests.test_ai_interpret import settings as settings
from youju import advisor_tasks as service
from youju.api import v1
from youju.models import Document, DocumentMerge


@pytest.mark.parametrize("data_enabled", [False, True])
@pytest.mark.parametrize("ai_enabled", [False, True])
@pytest.mark.parametrize("whitelisted", [False, True])
def test_capabilities_hide_all_ai_metadata(client, settings, data_enabled, ai_enabled, whitelisted):
    settings.advisor_enabled = data_enabled
    settings.ai_advisor_enabled = ai_enabled
    settings.ai_advisor_openids = "internal" if whitelisted else "second"
    response = client.get("/api/v1/advisor/capabilities")
    assert response.status_code == 200
    assert response.json()["data_tools"] == {
        "enabled": data_enabled,
        "features": ["compare", "track"] if data_enabled else [],
        "remaining": {"compare": 3 * int(data_enabled), "track": 3 * int(data_enabled)},
    }
    ai = response.json()["ai_advisor"]
    if ai_enabled and whitelisted:
        assert ai == {
            "enabled": True,
            "features": ["interpret", "clause"],
            "remaining": {"interpret": 5, "clause": 5},
            "disclosure": {"modelLabel": "", "filingNo": ""},
        }
    else:
        assert ai == {"enabled": False, "features": [], "remaining": {}}


@pytest.mark.parametrize("data_enabled", [False, True])
@pytest.mark.parametrize("ai_enabled", [False, True])
@pytest.mark.parametrize("whitelisted", [False, True])
@pytest.mark.parametrize(
    "kind",
    ["compare", "track", "interpret", "clause", "unknown", "omitted", 7, [], {}, True, None],
)
def test_creation_kind_response_groups(
    client, settings, doc, data_enabled, ai_enabled, whitelisted, kind
):
    settings.advisor_enabled = data_enabled
    settings.ai_advisor_enabled = ai_enabled
    settings.ai_advisor_openids = "internal" if whitelisted else "second"
    if data_enabled and kind in ("compare", "track"):
        assert (
            client.post(
                "/api/v1/me/agreement",
                json={"scope": "advisor", "version": service.ADVISOR_AGREEMENT_VERSION},
            ).status_code
            == 200
        )
    body = {"kind": kind, "doc_id": doc.id}
    if kind == "omitted":
        del body["kind"]
    elif kind == "compare":
        body = {"kind": kind, "rows": ["测试客户"]}
    elif kind == "track":
        body = {"kind": kind, "rows": [{"name": "测试客户"}]}
    response = client.post(
        "/api/v1/advisor/tasks", json=body, headers={"Idempotency-Key": "matrix"}
    )
    authorized = ai_enabled and whitelisted
    if not data_enabled and not ai_enabled:
        expected = (404, "没有找到接口")
    elif kind in ("compare", "track"):
        expected = (202, None) if data_enabled else (404, "没有找到接口")
    elif not authorized:
        expected = (422, "kind 只能是 compare 或 track") if data_enabled else (404, "没有找到接口")
    elif kind in ("interpret", "clause"):
        expected = (202, None)
    elif kind == "omitted":
        # 校验错误只返回字段位置和提示，不回显 input（支付审阅第 8 条）。
        expected = (422, [{"loc": ["body", "kind"], "msg": "Field required"}])
    else:
        allowed = "'compare', 'track', 'interpret' or 'clause'"
        expected = (422, [{"loc": ["body", "kind"], "msg": f"Input should be {allowed}"}])
    assert (response.status_code, response.json().get("detail")) == expected
    if expected[0] == 202:
        if kind in ("interpret", "clause"):
            assert (response.json()["docId"], response.json()["docTitle"]) == (doc.id, doc.title)
        else:
            assert not {"docId", "docTitle"} & response.json().keys()


@pytest.mark.parametrize("kind", ["interpret", "clause"])
@pytest.mark.parametrize("origin", ["input", "snapshot", "missing"])
@pytest.mark.parametrize(
    "visibility", ["public", "takedown", "private", "group_takedown", "deleted"]
)
def test_ai_notification_metadata_visibility(client, session, doc, kind, origin, visibility):
    task = direct_task(session, doc)
    task.kind = kind
    if origin == "snapshot":
        task.snapshot = {"doc_id": doc.id}
        task.input_json = None
    elif origin == "missing":
        task.input_json = None
    if visibility == "takedown":
        doc.taken_down_at = NOW
    elif visibility == "private":
        doc.source.rights_level = 2
    elif visibility == "group_takedown":
        twin = Document(
            source_id=doc.source_id,
            title="已下架副本",
            url="https://official.test/twin",
            url_key="official.test/twin",
            first_seen_at=NOW,
            last_seen_at=NOW,
            taken_down_at=NOW,
        )
        session.add(twin)
        session.flush()
        session.add(
            DocumentMerge(
                canonical_id=doc.id, duplicate_id=twin.id, active=True, match_key="测试归并"
            )
        )
    elif visibility == "deleted":
        session.execute(delete(Document).where(Document.id == doc.id))
    session.commit()
    expected = (
        (doc.id, doc.title) if origin != "missing" and visibility == "public" else (None, None)
    )
    for output in (
        client.get(f"/api/v1/advisor/tasks/{task.id}").json(),
        client.get("/api/v1/advisor/tasks").json()["items"][0],
    ):
        assert (output["docId"], output["docTitle"]) == expected


@pytest.mark.parametrize("kind", ["interpret", "clause"])
def test_ai_metadata_survives_seven_day_input_cleanup(
    client, session, settings, doc, monkeypatch, kind
):
    task = direct_task(session, doc)
    task.kind = kind
    run(session, task, settings)
    assert task.snapshot["doc_id"] == doc.id
    stamp = NOW + timedelta(days=8)
    service.cleanup_advisor(session, settings=settings, stamp=stamp)
    session.refresh(task)
    assert task.input_json is None and task.snapshot["doc_id"] == doc.id
    monkeypatch.setattr(v1, "now", lambda: stamp)
    for output in (
        client.get(f"/api/v1/advisor/tasks/{task.id}").json(),
        client.get("/api/v1/advisor/tasks").json()["items"][0],
    ):
        assert (output["docId"], output["docTitle"]) == (doc.id, doc.title)


def test_list_batches_titles_without_loading_inputs_or_snapshots(
    client, session, settings, doc, db_engine, monkeypatch
):
    expected = {}
    for i in range(4):
        notice = Document(
            source_id=doc.source_id,
            title=f"通知 {i}",
            url=f"https://official.test/notice-{i}",
            url_key=f"official.test/notice-{i}",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        session.add(notice)
        session.flush()
        task = direct_task(session, notice, key=f"batch-{i}")
        task.kind = "clause" if i % 2 else "interpret"
        task.snapshot = {"doc_id": notice.id, "paragraphs": ["不应加载完整快照"]}
        # 快照优先，不能被旧输入里的编号覆盖。
        task.input_json = {"doc_id": doc.id, "company": {"region": "云南"}}
        expected[task.id] = (notice.id, notice.title)
    session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    def forbidden(*args, **kwargs):
        raise AssertionError("列表不得重新核对或调用模型")

    monkeypatch.setattr(service, "ChatClient", forbidden)
    monkeypatch.setattr("youju.ai.interpret.prepare", forbidden)
    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/v1/advisor/tasks")
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    rows = response.json()["items"]
    assert {row["id"]: (row["docId"], row["docTitle"]) for row in rows} == expected
    assert all(not {"result", "company", "input_json", "snapshot"} & row.keys() for row in rows)
    assert len([sql for sql in statements if re.search(r"FROM documents\b", sql)]) == 1
    assert len([sql for sql in statements if re.search(r"FROM advisor_tasks\b", sql)]) == 1
    assert all(
        not re.search(
            r"advisor_tasks\.(?:snapshot|input_json)(?: AS [\w]+)?[, \n]+(?:advisor_tasks\.|FROM)",
            sql,
        )
        for sql in statements
    )
