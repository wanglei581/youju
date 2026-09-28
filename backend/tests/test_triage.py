"""纠错分诊（D-023）：用户纠错后复查对应文件已发布的截止日期，结果写在纠错旁边；纠错内容不发给 AI。"""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.ai.client import ChatClient
from youju.ai.triage import triage_feedback
from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import (
    AICheck,
    Document,
    DocumentDeadline,
    DocumentDetail,
    Feedback,
    Source,
    User,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
BODY = "请各单位于2026年10月15日前完成网上申报，纸质材料于10月20日前报送。"
APPLY = "请各单位于2026年10月15日前完成网上申报"
PAPER = "纸质材料于10月20日前报送"
TITLE = "关于组织申报2026年度科技计划项目的通知"
USER_TEXT = "这个截止日期好像写错了，我的电话是13800000000"


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
    doc = Document(
        source_id=source.id,
        url=f"https://kjt.shandong.gov.cn/tzgg/{number}.html",
        url_key=f"kjt.shandong.gov.cn/tzgg/{number}.html",
        title=TITLE if number == 1 else f"{TITLE}（{number}）",
        publish_date=date(2026, 9, 20),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text=body,
            fetched_at=NOW,
            status="ok",
            publish_date=date(2026, 9, 20),
        )
    )
    session.commit()
    return doc


def deadline(session, doc, due, evidence=APPLY, n=0, status="approved", audit_state=None):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{due}-{n}".ljust(64, "x"),
        kind="企业申报截止",
        candidate_date=due,
        evidence=evidence,
        confidence="高",
        status=status,
        reviewed_by="创始人" if status == "approved" else None,
        reviewed_at=NOW - timedelta(days=1) if status == "approved" else None,
        approved_date=due if status == "approved" else None,
        audit_state=audit_state,
        audit_note="AI：之前标的" if audit_state == "flagged" else None,
    )
    session.add(row)
    session.commit()
    return row


def feedback(session, doc, content=USER_TEXT, handled=False):
    user = session.scalar(select(User))
    if user is None:
        user = User(openid="o-user")
        session.add(user)
        session.flush()
    row = Feedback(
        user_id=user.id,
        document_id=doc.id if doc else None,
        content=content,
        check_result="pass",
        handled_at=NOW if handled else None,
    )
    session.add(row)
    session.commit()
    return row


def fake_ai(reply):
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


def test_triage_rechecks_the_document_without_sending_user_text(session) -> None:
    doc = add_doc(session)
    wrong = deadline(session, doc, date(2026, 10, 20), evidence=PAPER)
    right = deadline(session, doc, date(2026, 10, 15), n=1)
    item = feedback(session, doc)
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
    report = triage_feedback(session, client, now=NOW)
    assert (report.triaged, report.flagged, report.errors) == (1, 1, 0)
    session.expire_all()
    note = session.get(Feedback, item.id).triage_note
    assert note.startswith(
        "已发布的截止 2 条，规则和 AI 复查 1 条存疑：2026-10-20 AI：网上申报是15日截止"
    )
    assert session.get(DocumentDeadline, wrong.id).audit_state == "flagged"
    assert session.get(DocumentDeadline, right.id).audit_at is not None
    # 发给 AI 的只有政府文件原文和已发布的截止，没有用户写的内容
    sent = json.dumps(calls, ensure_ascii=False)
    assert "写错了" not in sent and "13800000000" not in sent and APPLY in sent

    calls.clear()
    assert triage_feedback(session, client, now=NOW).triaged == 0 and calls == []  # 只分诊一次


def test_rules_only_note_mentions_pending_candidates_and_kind(session) -> None:
    doc = add_doc(session)
    deadline(session, doc, date(2026, 10, 16))  # 正文里没有这个日期
    deadline(session, doc, date(2026, 10, 30), n=1, status="pending")
    session.add(
        AICheck(
            document_id=doc.id,
            task="kind",
            model="qwen-flash",
            result={"kind": "名单", "confidence": 0.8, "reason": "测试"},
            checked_at=NOW,
        )
    )
    session.commit()
    item = feedback(session, doc)
    report = triage_feedback(session, None, now=NOW)
    assert (report.triaged, report.flagged) == (1, 1)
    note = session.get(Feedback, item.id).triage_note
    assert "规则复查 1 条存疑：2026-10-16 正文里找不到 2026年10月16日" in note
    assert "没配 AI，只做了规则回查" in note and "还有 1 条截止候选待核对" in note
    assert "页面显示为申报通知，AI 认为是名单" in note


def test_confirmed_and_already_flagged_deadlines_are_not_flagged_again(session) -> None:
    doc = add_doc(session)
    cleared = deadline(session, doc, date(2026, 10, 20), evidence=PAPER, audit_state="cleared")
    flagged = deadline(session, doc, date(2026, 10, 15), n=1, audit_state="flagged")
    item = feedback(session, doc)
    client, calls = fake_ai(
        [
            {
                "id": cleared.id,
                "ok": False,
                "quote": APPLY,
                "found_date": "2026-10-15",
                "reason": "网上申报是15日截止",
            }
        ]
    )
    report = triage_feedback(session, client, now=NOW)
    assert report.flagged == 0
    session.expire_all()
    assert session.get(DocumentDeadline, cleared.id).audit_state == "cleared"
    note = session.get(Feedback, item.id).triage_note
    assert "2 条存疑" in note and "之前人确认过无误，未再标" in note
    assert '已在"质检存疑"里' in note
    # 已在质检存疑里的不再发给 AI
    assert f"编号 {flagged.id}" not in calls[0]["messages"][1]["content"]


def test_documents_without_published_deadlines(session) -> None:
    doc = add_doc(session)
    deadline(session, doc, date(2026, 10, 15), status="pending")
    item = feedback(session, doc)
    client, calls = fake_ai([])
    triage_feedback(session, client, now=NOW)
    note = session.get(Feedback, item.id).triage_note
    assert note == "这份文件没有已发布的截止日期；还有 1 条截止候选待核对" and calls == []


def test_ai_errors_leave_feedback_for_the_next_round(session) -> None:
    docs = [add_doc(session, i) for i in range(1, 5)]
    rows = [deadline(session, d, date(2026, 10, 15)) for d in docs]
    items = [feedback(session, d) for d in docs]
    state = {"down": True}

    def reply(payload):
        return httpx.Response(500, json={}) if state["down"] else []

    client, calls = fake_ai(reply)
    report = triage_feedback(session, client, now=NOW)
    assert (report.errors, report.stopped_early, report.triaged, len(calls)) == (3, True, 0, 3)
    session.expire_all()
    assert all(session.get(Feedback, i.id).triaged_at is None for i in items)
    assert all(session.get(DocumentDeadline, r.id).audit_at is None for r in rows)
    state["down"] = False
    assert triage_feedback(session, client, now=NOW).triaged == 4


def test_handled_and_documentless_feedback_are_skipped(session) -> None:
    doc = add_doc(session)
    deadline(session, doc, date(2026, 10, 16))
    handled = feedback(session, doc, handled=True)
    loose = feedback(session, None, content="申请覆盖：山东省商务厅")
    assert triage_feedback(session, None, now=NOW).triaged == 0
    assert session.get(Feedback, handled.id).triage_note is None
    assert session.get(Feedback, loose.id).triage_note is None


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    monkeypatch.setattr(v1, "now", lambda: NOW)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_console_shows_the_triage_note(client, session) -> None:
    doc = add_doc(session)
    deadline(session, doc, date(2026, 10, 15))
    item = feedback(session, doc)
    triage_feedback(session, None, now=NOW)
    listed = client.get("/api/admin/feedback", headers={"X-Admin-Token": "secret"}).json()
    got = next(i for i in listed if i["id"] == item.id)
    assert got["triageNote"].startswith("已发布的截止 1 条，规则复查都对得上原文")
    assert got["triagedAt"] is not None


def test_cli_runs_rules_only_without_ai(session, session_factory, monkeypatch, capsys) -> None:
    import youju.ai as ai_module
    from youju import cli

    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(ai_module, "get_chat_client", lambda: None)
    doc = add_doc(session)
    deadline(session, doc, date(2026, 10, 15))
    feedback(session, doc)
    assert cli.main(["triage-feedback"]) == 0
    assert "纠错分诊 1 条" in capsys.readouterr().out


def long_document(session):
    filler = "。" + "相关工作要求按照有关规定执行" * 60
    sentences = [f"第{i}项材料于2026年10月{i}日前报送" for i in range(1, 11)]
    doc = add_doc(session, body=filler.join(sentences) + filler)
    rows = [
        deadline(session, doc, date(2026, 10, i), evidence=sentences[i - 1], n=i)
        for i in range(1, 11)
    ]
    return doc, rows


def test_long_documents_are_rechecked_in_batches(session) -> None:
    doc, rows = long_document(session)
    item = feedback(session, doc)
    client, calls = fake_ai([])
    triage_feedback(session, client, now=NOW)
    assert len(calls) == 3  # 每批的原文不超过 3000 字，10 条分三批（5、4、1）
    session.expire_all()
    assert all(session.get(DocumentDeadline, r.id).audit_at is not None for r in rows)
    assert session.get(Feedback, item.id).triage_note.startswith(
        "已发布的截止 10 条，规则和 AI 复查都对得上原文"
    )


def test_note_says_which_deadlines_ai_could_not_cover(session, monkeypatch) -> None:
    import youju.ai.triage as triage_module

    monkeypatch.setattr(triage_module, "_MAX_BATCHES", 1)
    doc, _ = long_document(session)
    item = feedback(session, doc)
    client, _ = fake_ai([])
    triage_feedback(session, client, now=NOW)
    note = session.get(Feedback, item.id).triage_note
    assert note.startswith("已发布的截止 10 条，复查过的都对得上原文")
    assert "另有 5 条 AI 没复查到" in note  # 不说"都对得上"


def test_deleted_documents_do_not_break_the_round(session, monkeypatch) -> None:
    import youju.ai.triage as triage_module

    doc = add_doc(session)
    item = feedback(session, doc)
    monkeypatch.setattr(triage_module, "canonical_ids", lambda s, ids: dict.fromkeys(ids, 999999))
    assert triage_feedback(session, None, now=NOW).triaged == 1
    assert session.get(Feedback, item.id).triage_note == "对应文件已删除，没法复查"
