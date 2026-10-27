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


def test_result_not_recorded_when_body_changes_during_the_call(
    session, session_factory, monkeypatch
) -> None:
    """调模型期间正文被换掉（youju refetch-bodies 重取正文时删掉检查记录、要求重查）：这次的结果不记，
    下一轮按新正文重查，免得旧正文的结果把"要重查"盖掉。"""
    from types import SimpleNamespace

    from youju.ai import pipeline

    doc = add_doc(session)
    seen = []

    def classify(client, title, body):
        seen.append(body)
        if len(seen) == 1:
            with session_factory() as other:
                other.get(DocumentDetail, doc.id).body_text = BODY + "（重新取的正文）"
                other.commit()
        return SimpleNamespace(kind="申报通知", confidence=0.9, reason="组织企业申报")

    monkeypatch.setattr(pipeline, "classify_with_ai", classify)
    monkeypatch.setattr(pipeline, "extract_deadlines_with_ai", lambda *args: [])
    client, _ = fake_ai()

    report = run_ai_checks(session, client)
    assert (report.documents, report.body_changed, report.kind_agree) == (1, 1, 0)
    assert session.scalars(select(AICheck)).all() == []
    report = run_ai_checks(session, client)
    assert (report.documents, report.body_changed, report.kind_agree) == (1, 0, 1)
    assert seen[-1] == BODY + "（重新取的正文）"
    assert sorted(c.task for c in session.scalars(select(AICheck))) == ["deadlines", "kind"]


def _deadline_check(session, doc):
    return session.scalar(
        select(AICheck).where(AICheck.document_id == doc.id, AICheck.task == "deadlines")
    )


def test_deadline_result_is_stored_with_version(session) -> None:
    from youju.ai.deadlines import RESULT_VERSION

    doc = add_doc(session)
    client, _ = fake_ai(
        deadlines=[ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。")]
    )
    run_ai_checks(session, client)
    result = _deadline_check(session, doc).result
    assert result["version"] == RESULT_VERSION
    assert [(item["date"], item["kind"]) for item in result["items"]] == [
        ("2026-10-15", "企业申报截止")
    ]


def test_old_list_results_still_count(session) -> None:
    """第 1 版的记录直接存列表：规则候选照样认得 AI 结果。"""
    doc = add_doc(session)
    session.add(
        AICheck(
            document_id=doc.id,
            task="deadlines",
            model="m",
            result=[{"kind": "企业申报截止", "date": "2026-10-15", "time": None}],
            checked_at=datetime.now(UTC),
        )
    )
    session.commit()
    extract_existing(session)
    row = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.candidate_date == date(2026, 10, 15))
    )
    assert row.ai_agrees is True


def test_one_sided_clock_is_not_agreement_and_not_resubmitted(session) -> None:
    """D-099 期 0：规则没写时刻、AI 写了 17:00，不算一致；但也不当成新的一天另送一条 AI 候选。"""
    doc = add_doc(session)
    extract_existing(session, document_ids=[doc.id])
    item = ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。")
    item["time"] = "17:00"
    client, _ = fake_ai(deadlines=[item])
    report = run_ai_checks(session, client)
    rows = session.scalars(select(DocumentDeadline).where(DocumentDeadline.document_id == doc.id))
    assert sorted((r.origin, r.candidate_date, r.ai_agrees) for r in rows) == [
        ("rule", date(2026, 10, 15), False),
        ("rule", date(2026, 10, 20), False),
    ]
    assert (report.deadlines_agree, report.deadlines_rule_only, report.deadlines_ai_only) == (
        0,
        2,
        0,
    )


def _old_check(session, doc, result=None):
    session.add(
        AICheck(
            document_id=doc.id,
            task="deadlines",
            model="deepseek-flash",
            result=[] if result is None else result,
            prompt_tokens=1000,
            completion_tokens=100,
            checked_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    )
    session.commit()


def test_rerun_only_old_versions_within_the_days(session) -> None:
    from youju.ai.deadlines import RESULT_VERSION
    from youju.ai.pipeline import old_deadline_checks, rerun_old_deadlines

    now = datetime(2026, 10, 28, 1, tzinfo=UTC)
    recent = add_doc(session, 1)
    old = add_doc(session, 2)
    done = add_doc(session, 3)
    for doc in (old,):
        doc.publish_date = date(2026, 7, 1)
        session.get(DocumentDetail, doc.id).publish_date = date(2026, 7, 1)
    session.commit()
    _old_check(session, recent)
    _old_check(session, old)
    _old_check(session, done, {"version": RESULT_VERSION, "items": []})
    assert [doc.id for doc, _, _ in old_deadline_checks(session, now, 90)] == [recent.id]
    client, calls = fake_ai(
        deadlines=[ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。")]
    )
    report = rerun_old_deadlines(session, client, days=90, limit=10, now=now)
    assert report.documents == 1 and len(calls) == 1
    assert _deadline_check(session, recent).result["version"] == RESULT_VERSION
    assert _deadline_check(session, old).result == []
    assert _deadline_check(session, done).result == {"version": RESULT_VERSION, "items": []}
    # AI 新找到、规则没有的这一天作为 AI 候选送审；再跑一次没有旧版本可跑
    assert session.scalar(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == recent.id, DocumentDeadline.origin == "ai"
        )
    )
    assert old_deadline_checks(session, now, 90) == []


def test_rerun_keeps_the_old_result_when_the_call_fails(session) -> None:
    from youju.ai.pipeline import rerun_old_deadlines

    doc = add_doc(session)
    old = [{"kind": "企业申报截止", "date": "2026-10-15", "time": None}]
    _old_check(session, doc, old)
    client, _ = fake_ai(status=500)
    report = rerun_old_deadlines(
        session, client, days=90, limit=10, now=datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert report.errors == 1
    assert _deadline_check(session, doc).result == old


def test_recheck_command_only_counts_without_apply(session, session_factory, monkeypatch, capsys):
    """不加 --apply：只清点份数、按上次用量估费用，不调模型、不改数据。"""
    import argparse

    from youju import cli

    doc = add_doc(session)
    today = datetime.now(UTC).date()
    doc.publish_date = today - timedelta(days=10)
    session.get(DocumentDetail, doc.id).publish_date = today - timedelta(days=10)
    session.commit()
    _old_check(session, doc)
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)

    def no_client(*args, **kwargs):
        raise AssertionError("不加 --apply 不该调模型")

    monkeypatch.setattr("youju.ai.get_chat_client", no_client)
    code = cli._cmd_ai_recheck_deadlines(argparse.Namespace(days=90, limit=300, apply=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "主文件 1 份" in out and "这次约 0.00 元" in out and "没调模型" in out
    session.expire_all()
    assert _deadline_check(session, doc).result == []
