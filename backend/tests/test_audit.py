"""AI 质检员（D-023）：复查已发布的截止日期，存疑的标出来由人复核；只标记、不自动撤下。"""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.ai.audit import flagged_summary, run_audit
from youju.ai.client import ChatClient
from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import (
    AICheck,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    Source,
)

HEADERS = {"X-Admin-Token": "secret"}
BODY = "请各单位于2026年10月15日前完成网上申报，纸质材料于10月20日前报送。"


def add_doc(session, number=1, body=BODY):
    source = session.scalar(select(Source))
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
        title=f"关于组织申报2026年度科技计划项目的通知（{number}）",
        publish_date=date(2026, 9, 20),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text=body,
            fetched_at=now,
            status="ok",
            publish_date=date(2026, 9, 20),
        )
    )
    session.commit()
    return doc


def approve(session, doc, due, evidence="请各单位于2026年10月15日前完成网上申报", n=0):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{due}-{n}".ljust(20, "x"),
        kind="企业申报截止",
        candidate_date=due,
        evidence=evidence,
        confidence="高",
        status="approved",
        reviewed_by="创始人",
        reviewed_at=datetime.now(UTC),
        approved_date=due,
    )
    session.add(row)
    session.commit()
    return row


def fake_ai(items_by_call):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        content = json.dumps({"items": items_by_call}, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5},
            },
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen-flash",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    return client, calls


def test_rules_flag_dates_missing_from_text_or_before_publishing(session) -> None:
    doc = add_doc(session)
    good = approve(session, doc, date(2026, 10, 15))
    missing = approve(session, doc, date(2026, 10, 16), n=1)
    early = approve(session, doc, date(2026, 9, 1), n=2)
    report = run_audit(session, None)
    assert report.checked_by_rules == 3 and report.checked_by_ai == 0
    session.expire_all()
    assert session.get(DocumentDeadline, good.id).audit_state is None
    assert "找不到 10月16日" in session.get(DocumentDeadline, missing.id).audit_note
    assert "早于发布日期" in session.get(DocumentDeadline, early.id).audit_note
    assert {i for i, _, _ in report.flagged} == {missing.id, early.id}
    assert "质检发现 2 条" in flagged_summary(report)


def test_confirmed_items_are_not_flagged_again(session) -> None:
    doc = add_doc(session)
    row = approve(session, doc, date(2026, 10, 16))
    run_audit(session, None)
    row = session.get(DocumentDeadline, row.id)
    row.audit_state = "cleared"
    session.commit()
    assert run_audit(session, None).flagged == []
    assert session.get(DocumentDeadline, row.id).audit_state == "cleared"


def test_ai_flags_clear_mismatches_and_waits_30_days(session) -> None:
    doc = add_doc(session)
    wrong = approve(session, doc, date(2026, 10, 15))
    client, calls = fake_ai(
        [{"id": wrong.id, "ok": False, "found_date": "2026-10-25", "reason": "原文是25日"}]
    )
    now = datetime.now(UTC)
    report = run_audit(session, client, now=now)
    assert report.checked_by_ai == 1 and report.prompt_tokens == 30
    session.expire_all()
    note = session.get(DocumentDeadline, wrong.id).audit_note
    assert note.startswith("AI：原文是25日") and "2026-10-25" in note
    assert session.scalar(select(AICheck).where(AICheck.task == "audit")).error is None
    # 发给 AI 的是这份通知的原文片段，不含任何用户数据
    sent = calls[0]["messages"][1]["content"]
    assert "2026年10月15日" in sent and f"编号 {wrong.id}" in sent

    other = add_doc(session, 2)
    approve(session, other, date(2026, 10, 15))
    calls.clear()
    run_audit(session, client, now=now + timedelta(days=1))
    # 已抽过的文件 30 天内不再抽；新文件照常抽
    assert len(calls) == 1 and "（2）" in calls[0]["messages"][1]["content"]


def test_ai_ok_answers_do_not_flag(session) -> None:
    doc = add_doc(session)
    row = approve(session, doc, date(2026, 10, 15))
    client, _ = fake_ai([{"id": row.id, "ok": True, "found_date": "2026-10-15", "reason": ""}])
    assert run_audit(session, client).flagged == []


def test_merged_twin_urls_are_checked_once(session) -> None:
    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()
    approve(session, master, date(2026, 10, 16))
    approve(session, twin, date(2026, 10, 16))
    report = run_audit(session, None)
    assert report.checked_by_rules == 1 and len(report.flagged) == 1


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


def test_console_confirm_and_reopen(client, session) -> None:
    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()
    first = approve(session, master, date(2026, 10, 16))
    twin_row = approve(session, twin, date(2026, 10, 16))
    second = approve(session, master, date(2026, 10, 17), evidence="另一句", n=3)
    run_audit(session, None)

    assert client.get("/api/admin/audit/count", headers=HEADERS).json() == {"flagged": 2}
    items = client.get("/api/admin/audit", headers=HEADERS).json()
    assert {i["id"] for i in items} == {first.id, second.id}

    no_name = {"reviewed_by": ""}
    assert (
        client.post(f"/api/admin/deadlines/{first.id}/reopen", headers=HEADERS, json=no_name)
    ).status_code == 422
    body = {"reviewed_by": "创始人"}
    ok = client.post(f"/api/admin/deadlines/{second.id}/audit-ok", headers=HEADERS, json=body)
    assert ok.json() == {"audit": "cleared"}
    reopened = client.post(f"/api/admin/deadlines/{first.id}/reopen", headers=HEADERS, json=body)
    assert reopened.json() == {"status": "pending"}

    session.expire_all()
    for row_id in (first.id, twin_row.id):  # 同一通知其他网址的同一截止一起退回
        row = session.get(DocumentDeadline, row_id)
        assert (row.status, row.approved_date, row.audit_state) == ("pending", None, None)
    assert session.get(DocumentDeadline, second.id).audit_state == "cleared"
    assert client.get("/api/admin/audit/count", headers=HEADERS).json() == {"flagged": 0}
    # 已经是待核对的不能再退回
    again = client.post(f"/api/admin/deadlines/{first.id}/reopen", headers=HEADERS, json=body)
    assert again.status_code == 409
