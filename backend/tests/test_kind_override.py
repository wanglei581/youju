"""类型待定（D-023）：AI 和标题规则不一致的文件由人定类型，定了以后用户端按人定的显示和筛选。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.lists.rules import classify_title
from youju.models import AICheck, Document, Source

HEADERS = {"X-Admin-Token": "secret"}
# 测试用的分歧：标题规则判为申报通知，假设 AI 认为是名单
DISPUTED = "关于开展2026年度高新技术企业认定工作的通知"


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    monkeypatch.setattr(v1, "now", lambda: datetime(2026, 9, 28, 10, 0, tzinfo=UTC))
    yield TestClient(app)
    app.dependency_overrides.clear()


def add(session, number, title, ai_kind=None, error=None):
    source = session.query(Source).first()
    if source is None:
        source = Source(
            level="省级",
            province="山东",
            dept="科技",
            dept_full_name="山东省科学技术厅",
            column_name="通知公告",
            url="https://kjt.shandong.gov.cn/tzgg/",
            enabled=True,
            parser_config={},
        )
        session.add(source)
        session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url=f"https://kjt.shandong.gov.cn/tzgg/{number}.html",
        url_key=f"kjt.shandong.gov.cn/tzgg/{number}.html",
        title=title,
        publish_date=date(2026, 9, 25),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.flush()
    if ai_kind or error:
        session.add(
            AICheck(
                document_id=doc.id,
                task="kind",
                model="qwen-flash",
                result={"kind": ai_kind, "confidence": 0.8, "reason": "测试"} if ai_kind else None,
                error=error,
                checked_at=now,
            )
        )
    session.commit()
    return doc


def test_pending_lists_only_ai_rule_disagreements(client, session) -> None:
    disputed = add(session, 1, DISPUTED, ai_kind="名单")
    add(session, 2, "关于组织申报2026年度科技计划项目的通知", ai_kind="申报通知")  # 一致
    add(session, 3, "关于印发《科技企业管理办法》的通知", error="AI 网络请求失败")  # 出错
    add(session, 4, "关于公布第二批专精特新企业名单的通知")  # AI 还没查
    items = client.get("/api/admin/kinds", headers=HEADERS).json()
    assert [(i["id"], i["rule_kind"], i["ai_kind"]) for i in items] == [
        (disputed.id, "申报通知", "名单")
    ]
    assert client.get("/api/admin/kinds/count", headers=HEADERS).json() == {"pending": 1}


def test_decision_changes_user_facing_kind_filters_and_list_parsing(client, session) -> None:
    doc = add(session, 1, DISPUTED, ai_kind="名单")

    def search_ids(kind):
        found = client.get("/api/v1/search", params={"kind": kind, "time": "不限"}).json()
        return {int(item["id"]) for item in found["items"]}

    assert doc.id in search_ids("申报通知") and doc.id not in search_ids("名单")
    res = client.post(
        f"/api/admin/documents/{doc.id}/kind",
        headers=HEADERS,
        json={"kind": "名单", "decided_by": "创始人"},
    )
    assert res.status_code == 200
    assert doc.id in search_ids("名单") and doc.id not in search_ids("申报通知")
    item = next(
        i
        for i in client.get("/api/v1/search", params={"time": "不限"}).json()["items"]
        if int(i["id"]) == doc.id
    )
    assert item["kind"] == "名单"
    # 定过的不再出现在待定里，"全部"里能看到是谁定的
    assert client.get("/api/admin/kinds", headers=HEADERS).json() == []
    decided = client.get("/api/admin/kinds", headers=HEADERS, params={"status": "all"}).json()
    assert (decided[0]["decided"], decided[0]["decided_by"]) == ("名单", "创始人")
    # 人定为名单的会去解析名单；定为别的就不解析
    assert classify_title(DISPUTED, "名单").is_list
    assert not classify_title("关于公布第二批专精特新企业名单的通知", "政策").is_list

    # 撤回：回到按标题规则
    assert client.post(f"/api/admin/documents/{doc.id}/kind/undo", headers=HEADERS).json() == {
        "kind": "申报通知"
    }
    assert doc.id in search_ids("申报通知")


def test_decision_requires_reviewer_and_a_valid_kind(client, session) -> None:
    doc = add(session, 1, DISPUTED, ai_kind="名单")
    url = f"/api/admin/documents/{doc.id}/kind"
    assert (
        client.post(url, headers=HEADERS, json={"kind": "名单", "decided_by": ""}).status_code
        == 422
    )
    bad = client.post(url, headers=HEADERS, json={"kind": "通知", "decided_by": "创始人"})
    assert bad.status_code == 422
    assert client.post(url, json={"kind": "名单", "decided_by": "创始人"}).status_code == 401
    missing = "/api/admin/documents/999999/kind"
    assert (
        client.post(missing, headers=HEADERS, json={"kind": "名单", "decided_by": "x"}).status_code
        == 404
    )
