"""AI 检查接进流水线：和规则互相印证，AI 找到的候选也要人工核对，出错按时重试。"""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import select

from youju.ai.client import ChatClient
from youju.ai.pipeline import run_ai_checks
from youju.extract.service import extract_existing
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail, Source

BODY = "请各单位于2026年10月15日前完成网上申报。纸质材料于2026年10月20日前报送至科技厅。"


def fake_ai(kind="申报通知", deadlines=None, status=200):
    """按提示词区分两类请求：类型判断返回 kind，截止抽取返回 deadlines。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if status != 200:
            return httpx.Response(status)
        system = body["messages"][0]["content"]
        if "分类" in system:
            content = {"kind": kind, "confidence": 0.9, "reason": "组织企业申报"}
        else:
            content = {"deadlines": deadlines or []}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
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
        title="关于组织申报2026年度科技计划项目的通知",
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


def ai_item(date_text, evidence, label="企业申报截止"):
    return {"date": date_text, "time": "", "label": label, "evidence": evidence, "confidence": "高"}


def test_marks_agreement_and_sends_ai_only_candidates_to_review(session) -> None:
    add_doc(session)
    extract_existing(session)
    rule_rows = session.scalars(select(DocumentDeadline)).all()
    assert rule_rows and all(r.origin == "rule" and r.ai_agrees is None for r in rule_rows)
    rule_dates = {r.candidate_date for r in rule_rows}

    client, _ = fake_ai(
        deadlines=[
            ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。"),
            ai_item(
                "2026-10-20",
                "纸质材料于2026年10月20日前报送至科技厅。",
                label="材料报送截止",
            ),
        ]
    )
    report = run_ai_checks(session, client)

    assert report.documents == 1 and report.errors == 0
    rows = session.scalars(select(DocumentDeadline).order_by(DocumentDeadline.id)).all()
    for row in rows:
        if row.origin == "rule":
            assert row.ai_agrees is True
    ai_rows = [r for r in rows if r.origin == "ai"]
    # AI 找到、规则没找到的才新增，且照样待人工核对
    assert {r.candidate_date for r in ai_rows} == {date(2026, 10, 15), date(2026, 10, 20)} - (
        rule_dates
    )
    assert all(r.status == "pending" and r.ai_agrees is None for r in ai_rows)
    checks = {c.task: c for c in session.scalars(select(AICheck))}
    assert checks["kind"].result["kind"] == "申报通知"
    assert checks["deadlines"].prompt_tokens == 100
    assert report.prompt_tokens == 200 and report.completion_tokens == 40
    assert report.kind_agree == 1


def test_rule_rerun_keeps_ai_candidates(session) -> None:
    add_doc(session, body="评审结果另行通知。请于2026年11月3日前将推荐材料报送我厅。")
    extract_existing(session)
    client, _ = fake_ai(
        deadlines=[ai_item("2026-11-03", "请于2026年11月3日前将推荐材料报送我厅。")]
    )
    run_ai_checks(session, client)
    before = {(r.origin, r.candidate_date) for r in session.scalars(select(DocumentDeadline))}
    extract_existing(session)  # 每轮规则都会重跑
    after = {(r.origin, r.candidate_date) for r in session.scalars(select(DocumentDeadline))}
    assert before == after


def test_already_reviewed_date_is_not_resubmitted(session) -> None:
    doc = add_doc(session)
    extract_existing(session)
    for row in session.scalars(select(DocumentDeadline)):
        row.status = "rejected"
    session.commit()
    client, _ = fake_ai(
        deadlines=[ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。")]
    )
    run_ai_checks(session, client)
    assert not session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == doc.id, DocumentDeadline.origin == "ai"
        )
    ).all()


def test_errors_are_recorded_and_retried_after_a_day(session) -> None:
    add_doc(session)
    failing, calls = fake_ai(status=500)
    report = run_ai_checks(session, failing, now=datetime.now(UTC))
    assert report.errors == 1 and calls
    assert all(c.error for c in session.scalars(select(AICheck)))

    # 24 小时内不重试
    calls.clear()
    assert run_ai_checks(session, failing, now=datetime.now(UTC)).documents == 0
    assert calls == []

    # 过了 24 小时再试，这次成功
    ok, _ = fake_ai(deadlines=[])
    report = run_ai_checks(session, ok, now=datetime.now(UTC) + timedelta(hours=25))
    assert report.documents == 1 and report.errors == 0
    assert all(c.error is None for c in session.scalars(select(AICheck)))


def test_stops_after_consecutive_errors(session) -> None:
    for number in range(1, 6):
        add_doc(session, number)
    failing, _ = fake_ai(status=503)
    report = run_ai_checks(session, failing)
    assert report.stopped_early and report.documents == 3


def test_checked_documents_are_not_sent_again(session) -> None:
    add_doc(session)
    client, calls = fake_ai(deadlines=[])
    run_ai_checks(session, client)
    calls.clear()
    assert run_ai_checks(session, client).documents == 0
    assert calls == []


def test_review_console_shows_ai_agreement(session, session_factory, monkeypatch) -> None:
    """核对台的待核对列表带上来源和 AI 是否一致，页面据此显示"AI 一致 / 仅 AI 找到 / 仅规则找到"。"""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from youju.api.app import app
    from youju.db import get_session

    add_doc(session)
    extract_existing(session)
    client, _ = fake_ai(
        deadlines=[
            ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。"),
            ai_item("2026-10-20", "纸质材料于2026年10月20日前报送至科技厅。", label="材料报送截止"),
        ]
    )
    run_ai_checks(session, client)

    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    try:
        items = (
            TestClient(app)
            .get("/api/admin/deadlines", headers={"X-Admin-Token": "secret"}, params={"due": "all"})
            .json()
        )
    finally:
        app.dependency_overrides.clear()
    badges = {(i["origin"], i["ai_agrees"]) for i in items}
    assert badges and badges <= {("rule", True), ("ai", None)}


def test_kind_failure_is_retried_alone_later(session) -> None:
    """类型判断失败、截止抽取成功：之后只重试类型判断，不重复抽截止。"""
    add_doc(session)
    state = {"fail_kind": True}
    calls = []

    def handler(request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        calls.append("kind" if "分类" in system else "deadlines")
        if "分类" in system and state["fail_kind"]:
            return httpx.Response(503)
        content = (
            {"kind": "申报通知", "confidence": 0.9, "reason": "申报"}
            if "分类" in system
            else {"deadlines": []}
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen-flash",
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    first = datetime.now(UTC)
    run_ai_checks(session, client, now=first)
    assert calls == ["kind", "deadlines"]
    state["fail_kind"] = False
    calls.clear()
    run_ai_checks(session, client, now=first + timedelta(hours=25))
    assert calls == ["kind"]
    assert all(c.error is None for c in session.scalars(select(AICheck)))


def test_invalid_kind_is_an_error_and_retried(session) -> None:
    add_doc(session)
    client, _ = fake_ai(kind="随便", deadlines=[])
    report = run_ai_checks(session, client)
    check = session.scalar(select(AICheck).where(AICheck.task == "kind"))
    assert check.error == "AI 返回的类型无效" and report.errors == 1
    ok, calls = fake_ai(kind="申报通知", deadlines=[])
    run_ai_checks(session, ok, now=datetime.now(UTC) + timedelta(hours=25))
    assert session.scalar(select(AICheck).where(AICheck.task == "kind")).error is None


def test_rule_candidate_supersedes_pending_ai_candidate(session) -> None:
    """AI 先找到的一天，后来规则也找到了：以规则候选为准，标 AI 一致，不重复送审。"""
    doc = add_doc(session)
    session.add(
        DocumentDeadline(
            document_id=doc.id,
            fingerprint="a" * 64,
            kind="企业申报截止",
            candidate_date=date(2026, 10, 15),
            evidence="请各单位于2026年10月15日前完成网上申报。",
            confidence="高",
            status="pending",
            origin="ai",
        )
    )
    session.commit()
    extract_existing(session)
    rows = session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.candidate_date == date(2026, 10, 15))
    ).all()
    assert [(r.origin, r.ai_agrees) for r in rows] == [("rule", True)]


def test_twin_urls_do_not_get_duplicate_ai_candidates(session) -> None:
    """同一通知的另一个网址（归并组）里规则已找到这一天：AI 不再为这个网址另送一条。"""
    from youju.models import DocumentMerge

    master = add_doc(session, 1)
    twin = add_doc(session, 2, body="请于2026年10月15日前完成网上申报。")
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()
    extract_existing(session, document_ids=[master.id])
    client, _ = fake_ai(deadlines=[ai_item("2026-10-15", "请于2026年10月15日前完成网上申报。")])
    run_ai_checks(session, client)
    assert not session.scalars(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == twin.id, DocumentDeadline.origin == "ai"
        )
    ).all()


def test_usage_of_failed_calls_is_counted(session) -> None:
    add_doc(session)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "不是 JSON"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            },
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen-flash",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    report = run_ai_checks(session, client)
    # 两项检查各请求 2 次（1 次 + 重试 1 次），都算用量
    assert report.prompt_tokens == 200 and report.completion_tokens == 20
    assert sum(c.prompt_tokens for c in session.scalars(select(AICheck))) == 200


def test_rule_candidate_uses_ai_results_of_twin_urls(session) -> None:
    """AI 只检查过同一通知的另一个网址：规则在主网址找到同一天，也算 AI 一致。"""
    from youju.models import DocumentMerge

    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.add(
        AICheck(
            document_id=twin.id,
            task="deadlines",
            model="qwen-flash",
            result=[
                {
                    "kind": "企业申报截止",
                    "date": "2026-10-15",
                    "time": None,
                    "evidence": "请各单位于2026年10月15日前完成网上申报。",
                    "confidence": "高",
                }
            ],
            checked_at=datetime.now(UTC),
        )
    )
    session.commit()
    extract_existing(session, document_ids=[master.id])
    row = session.scalar(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == master.id,
            DocumentDeadline.candidate_date == date(2026, 10, 15),
        )
    )
    assert row.ai_agrees is True


def test_ai_check_of_twin_updates_existing_rule_candidates(session) -> None:
    """规则在同一通知的另一个网址上抽出候选，AI 检查主文件后，那个网址的候选也要标 AI 一致。"""
    from youju.models import DocumentMerge

    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()
    extract_existing(session, document_ids=[twin.id])
    client, _ = fake_ai(
        deadlines=[ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。")]
    )
    run_ai_checks(session, client)
    row = session.scalar(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == twin.id,
            DocumentDeadline.candidate_date == date(2026, 10, 15),
        )
    )
    assert row.ai_agrees is True


def test_merged_twin_urls_are_not_checked_again(session) -> None:
    """同一通知的其他网址（已归并）不单独检查，只查主文件。"""
    from youju.models import DocumentMerge

    master = add_doc(session, 1)
    twin = add_doc(session, 2)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()
    client, _ = fake_ai(deadlines=[])
    report = run_ai_checks(session, client)
    assert report.documents == 1
    checked = {c.document_id for c in session.scalars(select(AICheck))}
    assert checked == {master.id}
