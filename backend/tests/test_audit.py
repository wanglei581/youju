"""AI 质检员（D-023）：复查已发布的截止日期，存疑的标出来由人复核；只标记、不自动撤下。"""

import json
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.ai.audit import alert_text, run_audit
from youju.ai.client import ChatClient
from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.dedup import sync_reviewed_twins
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
APPLY = "请各单位于2026年10月15日前完成网上申报"
PAPER = "纸质材料于10月20日前报送"


def add_doc(session, number=1, body=BODY):
    """body 为 None 时不建详情（没抓到正文）。"""
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
    if body is not None:
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


def approve(session, doc, due, evidence=APPLY, n=0, kind="企业申报截止", candidate=None):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{due}-{n}".ljust(64, "x"),
        kind=kind,
        candidate_date=candidate or due,
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


def merge(session, master, twin):
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()


def fake_ai(reply):
    """reply：固定返回的 items，或按请求算出 items / 直接给出响应的函数。"""
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        result = reply(payload) if callable(reply) else reply
        if isinstance(result, httpx.Response):
            return result
        content = json.dumps({"items": result}, ensure_ascii=False)
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
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    return client, calls


def sent_ids(payload) -> set[int]:
    return {int(i) for i in re.findall(r"编号 (\d+)", payload["messages"][1]["content"])}


def test_rules_flag_early_missing_and_bodiless_deadlines(session) -> None:
    doc = add_doc(session)
    good = approve(session, doc, date(2026, 10, 15))
    missing = approve(session, doc, date(2026, 10, 16), n=1)
    early = approve(session, doc, date(2026, 9, 19), n=2)  # 发布前一天
    bodiless = approve(session, add_doc(session, 2, body=None), date(2026, 10, 15))
    same_day = approve(
        session, add_doc(session, 3, body="请于2026年9月20日前报送。"), date(2026, 9, 20)
    )
    report = run_audit(session, None)
    assert report.checked_by_rules == 5 and report.checked_by_ai == 0
    session.expire_all()

    def note(row):
        return session.get(DocumentDeadline, row.id).audit_note

    assert note(good) is None and note(same_day) is None  # 发布当天截止不算早
    assert "找不到 2026年10月16日" in note(missing)
    assert "早于发布日期" in note(early)
    assert "正文缺失" in note(bodiless)
    assert {i for i, _, _ in report.flagged} == {missing.id, early.id, bodiless.id}
    assert "质检发现 3 条" in alert_text(report)


def test_confirmed_items_are_not_flagged_again(session) -> None:
    doc = add_doc(session)
    row = approve(session, doc, date(2026, 10, 16))
    run_audit(session, None)
    row = session.get(DocumentDeadline, row.id)
    row.audit_state = "cleared"
    session.commit()
    report = run_audit(session, None)
    assert report.flagged == [] and alert_text(report) is None
    assert session.get(DocumentDeadline, row.id).audit_state == "cleared"


def test_ai_flags_claims_backed_by_the_text_and_rechecks_after_30_days(session) -> None:
    doc = add_doc(session)
    # 原文 20 日截止的是纸质材料，核对时误当成了企业申报截止
    wrong = approve(session, doc, date(2026, 10, 20), evidence=PAPER)
    right = approve(session, doc, date(2026, 10, 15), n=1)
    client, calls = fake_ai(
        [
            {
                "id": wrong.id,
                "ok": False,
                "quote": APPLY,
                "found_date": "2026-10-15",
                "reason": "网上申报是15日截止",
            },
            {"id": right.id, "ok": True},
        ]
    )
    now = datetime.now(UTC)
    report = run_audit(session, client, now=now)
    assert report.checked_by_ai == 1 and report.prompt_tokens == 30 and report.unverified == 0
    session.expire_all()
    note = session.get(DocumentDeadline, wrong.id).audit_note
    assert note.startswith("AI：网上申报是15日截止") and "2026-10-15" in note and APPLY in note
    kept = session.get(DocumentDeadline, right.id)
    assert kept.audit_state is None and kept.audit_at is not None  # 抽查过、没问题
    check = session.scalar(select(AICheck).where(AICheck.task == "audit"))
    assert check.error is None and sorted(check.result["checked"]) == sorted([wrong.id, right.id])
    # 发给 AI 的是这份通知的原文片段，不含任何用户数据
    sent = calls[0]["messages"][1]["content"]
    assert "2026年10月15日" in sent and f"编号 {wrong.id}" in sent

    fresh = approve(session, add_doc(session, 2), date(2026, 10, 15))
    calls.clear()
    run_audit(session, client, now=now + timedelta(days=1))
    # 抽查过的截止 30 天内不再抽；新通过的照常抽
    assert [sent_ids(c) for c in calls] == [{fresh.id}]
    calls.clear()
    run_audit(session, client, now=now + timedelta(days=30, hours=1))
    assert [sent_ids(c) for c in calls] == [{right.id}]


def test_ai_claims_that_do_not_match_the_text_are_not_flagged(session) -> None:
    doc = add_doc(session)
    rows = [approve(session, doc, date(2026, 10, 15), n=i) for i in range(6)]
    claims = [
        {"quote": "请各单位于2026年10月25日前完成网上申报", "found_date": "2026-10-25"},  # 编的
        {"quote": "10月15日前", "found_date": "2026-10-15"},  # 太短，说明不了什么
        {"quote": APPLY, "found_date": "2026-10-20"},  # 句子里没有它说的日期
        {"quote": APPLY, "found_date": "10月15日"},  # 日期格式不对
        {"quote": APPLY},  # 没说原文写的是哪天
        {},  # 没照抄原文
    ]
    client, _ = fake_ai(
        [
            {"id": row.id, "ok": False, "reason": "不一致", **c}
            for row, c in zip(rows, claims, strict=True)
        ]
    )
    report = run_audit(session, client)
    assert report.flagged == [] and report.unverified == 6
    session.expire_all()
    assert all(session.get(DocumentDeadline, r.id).audit_state is None for r in rows)
    check = session.scalar(select(AICheck).where(AICheck.task == "audit"))
    assert sorted(check.result["unverified"]) == [r.id for r in rows]


def test_ai_errors_retry_next_day_and_stop_after_three_in_a_row(session) -> None:
    rows = [approve(session, add_doc(session, i), date(2026, 10, 15)) for i in range(1, 5)]
    state = {"down": True}

    def reply(payload):
        if state["down"]:
            return httpx.Response(500, json={"usage": {"prompt_tokens": 7, "completion_tokens": 0}})
        return []

    client, calls = fake_ai(reply)
    now = datetime.now(UTC)
    report = run_audit(session, client, now=now)
    assert (report.errors, report.stopped_early, len(calls)) == (3, True, 3)
    assert report.prompt_tokens == 21  # 出错的响应带着用量也记上
    assert "连续出错已提前停止" in alert_text(report)
    session.expire_all()
    assert all(session.get(DocumentDeadline, r.id).audit_at is None for r in rows)  # 不算抽查过

    state["down"] = False
    calls.clear()
    report = run_audit(session, client, now=now + timedelta(hours=2))
    assert len(calls) == 1 and report.checked_by_ai == 1  # 出错的文件隔一天再试
    calls.clear()
    report = run_audit(session, client, now=now + timedelta(days=1, hours=1))
    assert len(calls) == 3 and report.errors == 0 and alert_text(report) is None


def test_long_documents_send_a_bounded_excerpt_and_keep_the_rest_for_later(session) -> None:
    filler = "。" + "相关工作要求按照有关规定执行" * 60
    sentences = [f"第{i}项材料于2026年10月{i}日前报送" for i in range(1, 11)]
    doc = add_doc(session, body=filler.join(sentences) + filler)
    rows = [
        approve(session, doc, date(2026, 10, i), evidence=sentences[i - 1], n=i)
        for i in range(1, 11)
    ]
    client, calls = fake_ai([])
    now = datetime.now(UTC)
    run_audit(session, client, now=now)
    first = sent_ids(calls[0])
    content = calls[0]["messages"][1]["content"]
    excerpt = content.split("相关原文：\n")[1].split("\n\n截止：")[0]
    assert len(excerpt) <= 3000 and "\n……\n" in excerpt and 1 <= len(first) < 10
    session.expire_all()
    assert {r.id for r in rows if session.get(DocumentDeadline, r.id).audit_at} == first

    calls.clear()
    run_audit(session, client, now=now + timedelta(hours=1))
    second = sent_ids(calls[0])
    assert second and not second & first  # 这次没放下的，下次接着抽


def test_ai_ok_answers_do_not_flag(session) -> None:
    doc = add_doc(session)
    row = approve(session, doc, date(2026, 10, 15))
    client, _ = fake_ai([{"id": row.id, "ok": True, "found_date": "2026-10-15", "reason": ""}])
    assert run_audit(session, client).flagged == []


def test_merged_twin_urls_are_checked_once(session) -> None:
    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    merge(session, master, twin)
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
    merge(session, master, twin)
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
        assert (row.status, row.approved_date, row.audit_state) == ("pending", None, "reopened")
        assert "找不到" in row.audit_note and row.audit_by == "创始人"
    assert session.get(DocumentDeadline, second.id).audit_state == "cleared"
    assert client.get("/api/admin/audit/count", headers=HEADERS).json() == {"flagged": 0}
    # 已经是待核对的不能再退回
    again = client.post(f"/api/admin/deadlines/{first.id}/reopen", headers=HEADERS, json=body)
    assert again.status_code == 409


def test_reopen_pulls_every_published_copy_and_waits_for_a_person(client, session) -> None:
    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    merge(session, master, twin)
    a = approve(session, master, date(2026, 10, 16))
    # 同一截止在另一网址上，证据句截得不一样；用户端显示的是同一个截止
    b = approve(session, twin, date(2026, 10, 16), evidence="网上申报于10月16日截止", n=1)
    # 候选日期相同、但核定成了别的日期：是另一个已发布的截止，不退
    c = approve(
        session, twin, date(2026, 10, 18), evidence="另一句", n=2, candidate=date(2026, 10, 16)
    )
    run_audit(session, None)
    body = {"reviewed_by": "创始人"}
    assert client.post(
        f"/api/admin/deadlines/{a.id}/reopen", headers=HEADERS, json=body
    ).json() == {"status": "pending"}
    session.expire_all()
    for row in (a, b):
        got = session.get(DocumentDeadline, row.id)
        assert (got.status, got.audit_state) == ("pending", "reopened")
    assert session.get(DocumentDeadline, c.id).status == "approved"

    # 同组沿用不会把退回的又自动通过（c 的候选相同、退回前就通过了）
    sync_reviewed_twins(session, {master.id})
    session.commit()
    session.expire_all()
    assert {session.get(DocumentDeadline, r.id).status for r in (a, b)} == {"pending"}

    # 导入的核对结果也不能直接通过退回的
    item = {
        "urlKey": master.url_key,
        "fingerprint": a.fingerprint,
        "kind": a.kind,
        "candidateDate": "2026-10-16",
        "evidence": a.evidence,
        "status": "approved",
        "approvedDate": "2026-10-16",
        "reviewedBy": "创始人",
        "reviewedAt": "2026-09-27T10:00:00+00:00",
    }
    imported = client.post(
        "/api/admin/reviews/import",
        headers=HEADERS,
        params={"dry_run": "false"},
        json={"version": 1, "items": [item]},
    ).json()
    assert (imported["applied"], imported["conflict"]) == (0, 1)
    assert "质检退回" in imported["problems"][0]["reason"]

    # 核对台里标着退回原因；人重新核对后标记清掉，通过的以后照常质检
    pending = {i["id"]: i for i in client.get("/api/admin/deadlines", headers=HEADERS).json()}
    assert "找不到" in pending[a.id]["reopen_note"]
    res = client.post(
        f"/api/admin/deadlines/{a.id}/approve",
        headers=HEADERS,
        json={"reviewed_by": "创始人", "date": "2026-10-15"},
    )
    assert res.status_code == 200
    session.expire_all()
    got = session.get(DocumentDeadline, a.id)
    assert (got.status, got.approved_date, got.audit_state, got.audit_note) == (
        "approved",
        date(2026, 10, 15),
        None,
        None,
    )


def test_changed_deadlines_cannot_be_reopened(client, session) -> None:
    doc = add_doc(session, 1)
    newer = add_doc(session, 2)
    row = approve(session, doc, date(2026, 10, 15))
    row.status = "changed"
    row.change_reason = "延期"
    row.changed_to_document_id = newer.id
    session.commit()
    res = client.post(
        f"/api/admin/deadlines/{row.id}/reopen", headers=HEADERS, json={"reviewed_by": "创始人"}
    )
    assert res.status_code == 409
    session.expire_all()
    got = session.get(DocumentDeadline, row.id)
    assert (got.status, got.approved_date, got.changed_to_document_id) == (
        "changed",
        date(2026, 10, 15),
        newer.id,
    )


def test_cli_alerts_only_when_something_needs_a_person(
    session, session_factory, monkeypatch, capsys
) -> None:
    import youju.ai as ai_module
    from youju import alerts, cli

    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(ai_module, "get_chat_client", lambda: None)
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    doc = add_doc(session)
    approve(session, doc, date(2026, 10, 15))
    assert cli.main(["audit-deadlines"]) == 0 and sent == []
    approve(session, doc, date(2026, 10, 16), n=1)
    assert cli.main(["audit-deadlines"]) == 0
    assert len(sent) == 1 and "质检发现 1 条" in sent[0]
    assert "只做规则回查" in capsys.readouterr().out
