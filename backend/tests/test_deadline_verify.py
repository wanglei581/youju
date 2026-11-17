"""需要 MySQL 测试库；清单和写回按真实公开查询及归并规则验证。"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from tests.test_api_v1 import _doc, client, seeded  # noqa: F401
from youju import deadline_excerpts
from youju import deadline_verify as verify
from youju.api import v1
from youju.models import Document, DocumentDeadline, DocumentDetail, DocumentMerge

TODAY = date(2026, 9, 27)
BODY = "申报截止2026年9月30日。修改后截止2026年10月1日。"


@pytest.fixture
def candidate(session, seeded):  # noqa: F811
    doc_id = seeded["apply"]
    for old in session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.document_id == doc_id)
    ):
        old.status = "rejected"
    detail = session.get(DocumentDetail, doc_id)
    detail.body_text = BODY
    row = DocumentDeadline(
        document_id=doc_id,
        fingerprint="verify",
        kind="企业申报截止",
        candidate_date=date(2026, 9, 30),
        evidence="申报截止2026年9月30日。",
        confidence="高",
        status="pending",
        origin="rule",
    )
    session.add(row)
    session.commit()
    return row


def payload(row, **kw):
    return {
        "id": row.id,
        "verdict": "confirmed",
        "quote": "申报截止2026年9月30日。",
        "body_sha256": verify.body_hash(BODY),
        **kw,
    }


def test_queue_only_seven_day_block_and_unclear(session, candidate):
    row = candidate
    far = DocumentDeadline(
        document_id=row.document_id,
        fingerprint="far",
        kind="材料报送截止",
        candidate_date=date(2026, 10, 10),
        evidence="10月10日",
        confidence="高",
        status="pending",
        origin="rule",
    )
    session.add(far)
    session.commit()
    assert [x["id"] for x in verify.queue(session, TODAY)] == [row.id]
    assert (
        verify.queue(session, TODAY)[0]["reopened"],
        verify.queue(session, TODAY)[0]["audit_note"],
    ) == (False, None)
    assert (
        verify.apply_one(session, payload(row, verdict="unclear", note="日期依据不足"), True)
        == "记录看不准"
    )
    session.commit()
    assert row.status == "pending" and not verify.queue(session, TODAY)
    session.get(DocumentDetail, row.document_id).body_text += "补充。"
    session.commit()
    assert [x["id"] for x in verify.queue(session, TODAY)] == [row.id]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"body_sha256": "bad"}, "正文指纹"),
        ({"quote": "不存在2026年9月30日"}, "不在当前正文"),
        ({"quote": "修改后截止2026年10月1日。"}, "日期不匹配"),
        (
            {
                "verdict": "corrected",
                "date": "2026-10-02",
                "time": None,
                "quote": "修改后截止2026年10月1日。",
            },
            "日期不匹配",
        ),
        ({"reviewed_by": "别人"}, "不支持"),
    ],
)
def test_invalid_result_skips(session, candidate, changes, reason):
    assert reason in verify.apply_one(session, payload(candidate, **changes), True)
    assert candidate.status == "pending"


def test_dry_run_status_and_corrected(session, candidate):
    item = payload(
        candidate,
        verdict="corrected",
        date="2026-10-01",
        time=None,
        quote="修改后截止2026年10月1日。",
    )
    assert verify.apply_one(session, item) == "通过并修改日期"
    assert candidate.status == "pending" and candidate.verification_quote is None
    assert verify.apply_one(session, item, True) == "通过并修改日期"
    session.commit()
    assert (
        candidate.status,
        candidate.approved_date,
        candidate.reviewed_by,
        candidate.verification_corrected,
    ) == ("approved", date(2026, 10, 1), "Claude", True)
    assert "状态已变" in verify.apply_one(session, item, True)
    approved = v1._approved(session, [candidate.document_id])
    views, _, _ = deadline_excerpts.batch(session, [candidate.document_id], approved, TODAY)
    view = views[candidate.document_id][0]
    assert (view.source, view.day, view.quote) == ("verified", date(2026, 10, 1), item["quote"])


def test_twins_follow_confirmation(session, candidate):
    doc = session.get(Document, candidate.document_id)
    twin_doc = _doc(doc.source, 99, doc.title, TODAY)
    session.add(twin_doc)
    session.flush()
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=twin_doc.id, canonical_id=doc.id, match_key="same", active=True
            ),
            DocumentDetail(
                document_id=twin_doc.id,
                body_text=BODY.replace("申报截止", "申报  截止"),
                status="ok",
                fetched_at=datetime.now(UTC),
            ),
            DocumentDeadline(
                document_id=twin_doc.id,
                fingerprint="twin",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence=candidate.evidence,
                confidence="高",
                status="pending",
                origin="rule",
                audit_state="reopened",
                audit_note="副本的退回原因",
            ),
        ]
    )
    session.commit()
    # 同组同一截止的副本被退回过：核主文件这条也要看到退回原因（agy 审阅 11/12）
    entry = next(x for x in verify.queue(session, TODAY) if x["id"] == candidate.id)
    assert (entry["reopened"], entry["audit_note"]) == (True, "质检退回原因：副本的退回原因")
    assert "带退回原因的清单" in verify.apply_one(session, payload(candidate), True)
    assert "不一样" in verify.apply_one(session, payload(candidate, reopened=True), True)
    result = {**entry, **payload(candidate)}
    assert "note" in verify.apply_one(session, result, True)
    assert (
        verify.apply_one(session, {**result, "note": "核了原文：申报截止就是这一天"}, True)
        == "通过"
    )
    session.commit()
    twin = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == twin_doc.id)
    )
    assert (
        twin.status == "approved"
        and twin.reviewed_by == "Claude"
        and twin.verification_quote == payload(candidate)["quote"].replace("申报截止", "申报  截止")
    )
    assert twin.audit_state is None and twin.audit_note is None
    assert (
        twin.verification_unclear_note
        == "质检退回原因：副本的退回原因；Claude 处理：核了原文：申报截止就是这一天"
    )


def test_detail_shows_the_quote_recorded_when_verified(client, session, candidate):  # noqa: F811
    # 详情页附的原句用核对时记下的那句，不用抽取时的候选原句
    candidate.evidence = "截止2026年9月30日"
    session.commit()
    assert verify.apply_one(session, payload(candidate), True) == "通过"
    session.commit()
    d = client.get(f"/api/v1/documents/{candidate.document_id}").json()
    assert d["deadline"]["date"] == "2026-09-30"
    assert d["excerpt"]["text"] == v1.shown_quote(payload(candidate)["quote"])


@pytest.mark.parametrize("verdict", ["confirmed", "corrected"])
def test_audit_reopened_rows_can_be_verified(session, candidate, verdict):
    candidate.audit_state = "reopened"
    candidate.audit_note = "原文写的是材料报送截止"
    session.commit()
    queued = verify.queue(session, TODAY)
    assert len(queued) == 1 and queued[0]["reopened"] is True
    assert queued[0]["audit_note"] == f"质检退回原因：{candidate.audit_note}"
    context = {**queued[0], "note": "核了原文：申报截止就是这一天"}
    assert "不在当前正文" in verify.apply_one(
        session, {**context, **payload(candidate, quote="不存在的2026年9月30日截止原句")}, True
    )
    assert candidate.status == "pending" and candidate.audit_note == "原文写的是材料报送截止"
    changes = {"verdict": verdict}
    if verdict == "corrected":
        changes.update(date="2026-10-01", time=None, quote="修改后截止2026年10月1日。")
    result = {**context, **payload(candidate, **changes)}
    assert verify.apply_one(session, result, True).startswith("通过")
    session.commit()
    assert candidate.audit_state is None and candidate.audit_note is None
    assert candidate.verification_unclear_note == (
        "质检退回原因：原文写的是材料报送截止；Claude 处理：核了原文：申报截止就是这一天"
    )


def test_reopened_unclear_remains_blocked_until_body_changes(session, candidate):
    candidate.audit_state = "reopened"
    candidate.audit_note = "日期存疑"
    session.commit()
    assert (
        verify.apply_one(session, payload(candidate, verdict="unclear", note="仍看不准"), True)
        == "记录看不准"
    )
    session.commit()
    assert candidate.audit_note == "日期存疑" and not verify.queue(session, TODAY)
    session.get(DocumentDetail, candidate.document_id).body_text += "补充。"
    session.commit()
    assert verify.queue(session, TODAY)[0]["audit_note"] == "质检退回原因：日期存疑"


def test_twin_added_later_keeps_the_verified_quote(session, candidate):
    from youju.dedup import sync_reviewed_twins

    item = payload(
        candidate,
        verdict="corrected",
        date="2026-10-01",
        time=None,
        quote="修改后截止2026年10月1日。",
    )
    assert verify.apply_one(session, item, True) == "通过并修改日期"
    session.commit()
    doc = session.get(Document, candidate.document_id)
    twin_doc = _doc(doc.source, 98, doc.title, TODAY)
    session.add(twin_doc)
    session.flush()
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=twin_doc.id, canonical_id=doc.id, match_key="same", active=True
            ),
            DocumentDeadline(
                document_id=twin_doc.id,
                fingerprint="later",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence=candidate.evidence,
                confidence="高",
                status="pending",
                origin="rule",
            ),
        ]
    )
    session.commit()
    assert sync_reviewed_twins(session, {doc.id}) == 1
    session.commit()
    twin = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == twin_doc.id)
    )
    # 沿用改过的日期，也沿用写着这个日期的原句（副本自己的候选原句写的是旧日期）
    assert (twin.approved_date, twin.verification_quote, twin.verification_corrected) == (
        date(2026, 10, 1),
        item["quote"],
        True,
    )


def test_stats_with_real_rows_and_twins(session, candidate):
    stamp = datetime(2026, 11, 11, 16, tzinfo=UTC)
    doc = session.get(Document, candidate.document_id)
    twin_doc = _doc(doc.source, 97, doc.title, TODAY)
    other_doc = _doc(doc.source, 96, "另一份通知", TODAY)
    session.add_all([twin_doc, other_doc])
    session.flush()
    candidate.status = "approved"
    candidate.approved_date = candidate.candidate_date
    candidate.reviewed_by = "Claude"
    candidate.reviewed_at = stamp
    candidate.ai_agrees = True
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=twin_doc.id, canonical_id=doc.id, match_key="same", active=True
            ),
            DocumentDeadline(
                document_id=twin_doc.id,
                fingerprint="stats-twin",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence=candidate.evidence,
                confidence="高",
                origin="rule",
                status="approved",
                reviewed_by="Claude",
                reviewed_at=stamp,
                approved_date=candidate.candidate_date,
                ai_agrees=True,
            ),
            DocumentDeadline(
                document_id=other_doc.id,
                fingerprint="stats-corrected",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence=candidate.evidence,
                confidence="高",
                origin="rule",
                status="approved",
                reviewed_by="Claude",
                reviewed_at=stamp,
                approved_date=candidate.candidate_date,
                verification_corrected=True,
                ai_agrees=True,
            ),
            DocumentDeadline(
                document_id=other_doc.id,
                fingerprint="stats-unclear",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence=candidate.evidence,
                confidence="高",
                origin="rule",
                status="pending",
                ai_agrees=False,
                verification_unclear_at=stamp,
            ),
        ]
    )
    session.commit()
    result = verify.stats(session, date(2026, 11, 12))
    assert result["ai_agrees"] == dict(confirmed=1, corrected=1, unclear=0, accuracy=0.5)
    assert result["other"] == dict(confirmed=0, corrected=0, unclear=1, accuracy=None)
    assert verify.stats(session, date(2026, 11, 13))["total"]["confirmed"] == 0


def test_corrected_with_the_candidate_date_is_refused(session, candidate):
    # 日期没改却写 corrected 会把准确率统计算错：要求写 confirmed
    item = payload(candidate, verdict="corrected", date="2026-09-30", time=None)
    assert "应写 confirmed" in verify.apply_one(session, item, True)
    assert candidate.status == "pending"


def test_reopened_without_a_reason_still_leaves_a_trace(session, candidate):
    # 管理台退回时没写原因：通过以后也要留一笔"质检退回"（agy 审阅 11/12）
    candidate.audit_state = "reopened"
    candidate.audit_note = None
    session.commit()
    [entry] = verify.queue(session, TODAY)
    assert (entry["reopened"], entry["audit_note"]) == (True, "质检退回（没写原因）")
    assert (
        verify.apply_one(
            session, {**entry, **payload(candidate), "note": "核了原文：申报截止就是这一天"}, True
        )
        == "通过"
    )
    session.commit()
    assert candidate.audit_state is None
    assert (
        candidate.verification_unclear_note
        == "质检退回（没写原因）；Claude 处理：核了原文：申报截止就是这一天"
    )


def test_reopen_after_the_queue_waits_for_the_next_queue(session, candidate):
    # 出清单以后同一截止又有退回（原因变了）：按旧清单写的结果不放行，等下一份清单（独立 Claude 审阅）
    candidate.audit_state = "reopened"
    candidate.audit_note = "第一次退回"
    session.commit()
    [entry] = verify.queue(session, TODAY)
    candidate.audit_note = "第二次退回，原文另有修改通知"
    session.commit()
    result = {**entry, **payload(candidate), "note": "核了原文：申报截止就是这一天"}
    assert "等下一份清单" in verify.apply_one(session, result, True)
    assert candidate.status == "pending" and candidate.audit_state == "reopened"
    [entry] = verify.queue(session, TODAY)
    assert (
        verify.apply_one(
            session, {**entry, **payload(candidate), "note": "核了原文：申报截止就是这一天"}, True
        )
        == "通过"
    )


def test_same_deadline_on_two_pages_both_reopened_can_be_handled_in_one_file(session, candidate):
    # 同一截止在两个网址上、证据句不同（不在同一组锁里），都退回、原因相同：清单两条的退回原因都是
    # 两段；同一份结果先处理第一条以后，第二条照样放行，不会成了没人管的孤儿（独立 Claude 审阅）
    doc = session.get(Document, candidate.document_id)
    other_doc = _doc(doc.source, 97, doc.title, TODAY)
    session.add(other_doc)
    session.flush()
    other = DocumentDeadline(
        document_id=other_doc.id,
        fingerprint="other-page",
        kind=candidate.kind,
        candidate_date=candidate.candidate_date,
        evidence="网上申报于9月30日截止",
        confidence="高",
        status="pending",
        origin="rule",
        audit_state="reopened",
        audit_note="原因 X",
    )
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=other_doc.id, canonical_id=doc.id, match_key="same", active=True
            ),
            DocumentDetail(
                document_id=other_doc.id, body_text=BODY, status="ok", fetched_at=datetime.now(UTC)
            ),
            other,
        ]
    )
    candidate.audit_state = "reopened"
    candidate.audit_note = "原因 X"
    session.commit()
    entries = {x["id"]: x for x in verify.queue(session, TODAY)}
    assert {e["audit_note"] for e in entries.values()} == {
        "质检退回原因：原因 X；质检退回原因：原因 X"
    }
    for row in (candidate, other):
        result = {**entries[row.id], **payload(row), "note": "核了原文"}
        assert verify.apply_one(session, result, True) == "通过"
        session.commit()
    assert (candidate.status, other.status) == ("approved", "approved")


def test_reopen_with_the_same_reason_after_the_queue_waits(session, candidate):
    # 出清单以后同一截止又有一条退回、原因字样和已有的一样：按旧清单写的结果也不放行（agy 审阅）
    candidate.audit_state = "reopened"
    candidate.audit_note = "原因 X"
    session.commit()
    [entry] = verify.queue(session, TODAY)
    doc = session.get(Document, candidate.document_id)
    other_doc = _doc(doc.source, 96, doc.title, TODAY)
    session.add(other_doc)
    session.flush()
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=other_doc.id, canonical_id=doc.id, match_key="same", active=True
            ),
            DocumentDetail(
                document_id=other_doc.id, body_text=BODY, status="ok", fetched_at=datetime.now(UTC)
            ),
            DocumentDeadline(
                document_id=other_doc.id,
                fingerprint="later-reopen",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence="网上申报于9月30日截止",
                confidence="高",
                status="pending",
                origin="rule",
                audit_state="reopened",
                audit_note="原因 X",
            ),
        ]
    )
    session.commit()
    result = {**entry, **payload(candidate), "note": "核了原文"}
    assert "等下一份清单" in verify.apply_one(session, result, True)
    assert candidate.status == "pending"


def test_spotcheck_real_group_and_queue(session, candidate):
    doc = session.get(Document, candidate.document_id)
    twin_doc = _doc(doc.source, 199, doc.title, TODAY)
    session.add(twin_doc)
    session.flush()
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=twin_doc.id, canonical_id=doc.id, match_key="spot", active=True
            ),
            DocumentDetail(
                document_id=twin_doc.id, body_text=BODY, status="ok", fetched_at=datetime.now(UTC)
            ),
            DocumentDeadline(
                document_id=twin_doc.id,
                fingerprint="spot-twin",
                kind=candidate.kind,
                candidate_date=candidate.candidate_date,
                evidence=candidate.evidence,
                confidence="高",
                status="pending",
                origin="rule",
            ),
        ]
    )
    session.commit()
    assert verify.apply_one(session, payload(candidate), True) == "通过"
    session.commit()
    now = datetime.now(UTC)
    items = verify.sample(session, days=7, minimum=100, now=now)
    assert sum(x["id"] == candidate.id for x in items) == 1
    twin = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == twin_doc.id)
    )
    assert twin.id not in [x["id"] for x in items]
    candidate.reviewed_at = twin.reviewed_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.flush()
    assert candidate.id not in [
        x["id"] for x in verify.sample(session, days=7, minimum=100, now=now)
    ]
    candidate.reviewed_at = twin.reviewed_at = now
    session.flush()
    result = dict(
        id=candidate.id,
        date=None,
        time=None,
        quote=None,
        note="未找到",
        body_sha256=verify.body_hash(BODY),
    )
    assert "不一致" in verify.spotcheck(session, result, True, now)["result"]
    session.commit()
    assert candidate.status == twin.status == "pending"
    entry = next(
        x for x in verify.queue(session, TODAY + timedelta(days=1)) if x["id"] == candidate.id
    )
    assert entry["reopened"] and "Codex 抽查不一致" in entry["audit_note"]


def test_spotcheck_with_a_long_quote_and_note_still_reopens(session, candidate):
    # 退回原因记在 audit_note（最长 200 字）：Codex 给的原句和说明很长时截断，不能写库报错
    long_body = BODY + "修改后截止2026年10月1日，" + "其余事项按原通知执行" * 30 + "。"
    session.get(DocumentDetail, candidate.document_id).body_text = long_body
    session.commit()
    item = payload(candidate, body_sha256=verify.body_hash(long_body))
    assert verify.apply_one(session, item, True) == "通过"
    session.commit()
    quote = long_body[long_body.index("修改后截止") :]
    result = dict(
        id=candidate.id,
        date="2026-10-01",
        time=None,
        quote=quote,
        note="说明" * 100,
        body_sha256=verify.body_hash(long_body),
    )
    assert "不一致" in verify.spotcheck(session, result, True)["result"]
    session.commit()
    assert candidate.status == "pending" and candidate.audit_state == "reopened"
    assert len(candidate.audit_note) <= 200 and candidate.audit_note.startswith("Codex 抽查不一致")


def test_auto_verify_twins_and_dry_run(session, candidate):
    from youju.ai.deadlines import RESULT_VERSION
    from youju.models import AICheck

    doc = session.get(Document, candidate.document_id)
    twin_doc = _doc(doc.source, 99, doc.title, TODAY)
    session.add(twin_doc)
    session.flush()
    twin = DocumentDeadline(
        document_id=twin_doc.id,
        fingerprint="auto-twin",
        kind=candidate.kind,
        candidate_date=candidate.candidate_date,
        evidence=candidate.evidence,
        confidence="高",
        status="pending",
        origin="rule",
        ai_agrees=True,
    )
    candidate.ai_agrees = True
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=twin_doc.id, canonical_id=doc.id, match_key="auto", active=True
            ),
            DocumentDetail(
                document_id=twin_doc.id,
                body_text=BODY.replace("申报截止", "申报  截止"),
                status="ok",
                fetched_at=datetime.now(UTC),
            ),
            twin,
            AICheck(
                document_id=doc.id,
                task="deadlines",
                model="test",
                error=None,
                checked_at=datetime.now(UTC),
                result={
                    "version": RESULT_VERSION,
                    "items": [
                        {
                            "kind": candidate.kind,
                            "date": "2026-09-30",
                            "time": None,
                            "evidence": candidate.evidence,
                        }
                    ],
                },
            ),
        ]
    )
    session.commit()
    current = datetime(2026, 9, 27, 12)
    assert [(x["id"], x["result"]) for x in verify.auto_verify(session, TODAY, current)] == [
        (candidate.id, "通过"),
        (twin.id, "同一截止已由较早的候选通过"),
    ]
    assert candidate.status == twin.status == "pending"
    assert [(x["id"], x["result"]) for x in verify.auto_verify(session, TODAY, current, True)] == [
        (candidate.id, "通过"),
        (twin.id, "同一截止已由较早的候选通过"),
    ]
    assert candidate.reviewed_by == twin.reviewed_by == verify.AUTO_REVIEWER
    assert candidate.verification_quote == candidate.evidence
    assert twin.verification_quote == candidate.evidence.replace("申报截止", "申报  截止")
    counts = verify.stats(session)["auto"]
    assert counts["approved"] >= 1


def _auto_ai_record(session, candidate):
    from youju.ai.deadlines import RESULT_VERSION
    from youju.models import AICheck

    candidate.ai_agrees = True
    session.add(
        AICheck(
            document_id=candidate.document_id,
            task="deadlines",
            model="test",
            error=None,
            checked_at=datetime.now(UTC),
            result={
                "version": RESULT_VERSION,
                "items": [
                    {
                        "kind": candidate.kind,
                        "date": "2026-09-30",
                        "time": None,
                        "evidence": candidate.evidence,
                    }
                ],
            },
        )
    )


def test_auto_same_deadline_different_evidence(session, candidate):
    detail = session.get(DocumentDetail, candidate.document_id)
    second_quote = "请于2026年9月30日前提交申报材料。"
    detail.body_text += second_quote
    second = DocumentDeadline(
        document_id=candidate.document_id,
        fingerprint="auto-second",
        kind=candidate.kind,
        candidate_date=candidate.candidate_date,
        evidence=second_quote,
        confidence="高",
        status="pending",
        origin="rule",
        ai_agrees=True,
    )
    session.add(second)
    _auto_ai_record(session, candidate)
    session.commit()
    results = list(verify.auto_verify(session, TODAY, datetime(2026, 9, 27, 12), True))
    assert [(r["id"], r["result"]) for r in results] == [
        (candidate.id, "通过"),
        (second.id, "同一截止已由较早的候选通过"),
    ]


def test_admin_reopens_auto_verified(session, candidate, client, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    _auto_ai_record(session, candidate)
    session.commit()
    results = list(verify.auto_verify(session, TODAY, datetime(2026, 9, 27, 12), True))
    assert [(r["id"], r["result"]) for r in results] == [(candidate.id, "通过")]
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    response = client.post(
        f"/api/admin/deadlines/{candidate.id}/reopen",
        headers={"X-Admin-Token": "secret"},
        json={"reviewed_by": "创始人"},
    )
    assert response.json() == {"status": "pending"}
    session.expire_all()
    assert candidate.audit_note == "人工退回"
    assert candidate.audit_by.endswith(verify.AUTO_REOPEN_TAG)
    assert verify.stats(session)["auto"]["reopened"] == 1


def test_auto_result_carries_over_only_to_twins_with_their_own_sentence(
    session, candidate, monkeypatch
):
    """D-135：AI 自动通过的结果只沿用给证据句相同（同一句话）、规则抽出、AI 也一致的副本，而且这句也在
    副本自己的当前正文里（原句记它自己的）；正文里没有这句的、证据句不同的都留在待核对。同一截止另有
    人核过的，按人核的沿用。"""
    from zoneinfo import ZoneInfo

    from youju.dedup import sync_reviewed_twins

    # 沿用时按当时的北京时间判断过期：固定在 9 月 27 日；自动核对开着（开关 1、配了 AI 密钥）
    monkeypatch.setattr(
        v1, "now", lambda: datetime(2026, 9, 27, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    monkeypatch.setattr(verify, "auto_enabled", lambda settings=None: True)
    _auto_ai_record(session, candidate)
    session.commit()
    results = list(verify.auto_verify(session, TODAY, datetime(2026, 9, 27, 12), True))
    assert [r["result"] for r in results] == ["通过"]
    doc = session.get(Document, candidate.document_id)
    same = candidate.evidence
    other = "各单位请于2026年9月30日前完成网上申报。"
    rows = {}
    cases = (
        (f"通知正文。{same}", same),  # 同一句、在自己正文里：沿用，原句记自己的
        ("正文改过，没有那一句。", same),  # 同一句、自己正文里没有：不沿用
        (f"通知正文。{other}", other),  # 不是同一句：不沿用
    )
    for n, (body, evidence) in enumerate(cases, start=1):
        twin_doc = _doc(doc.source, 90 + n, doc.title, TODAY)
        session.add(twin_doc)
        session.flush()
        session.add_all(
            [
                DocumentMerge(
                    duplicate_id=twin_doc.id,
                    canonical_id=doc.id,
                    match_key=f"carry{n}",
                    active=True,
                ),
                DocumentDetail(
                    document_id=twin_doc.id,
                    body_text=body,
                    status="ok",
                    fetched_at=datetime.now(UTC),
                ),
            ]
        )
        rows[n] = DocumentDeadline(
            document_id=twin_doc.id,
            fingerprint=f"carry-{n}",
            kind=candidate.kind,
            candidate_date=candidate.candidate_date,
            evidence=evidence,
            confidence="高",
            status="pending",
            origin="rule",
            ai_agrees=True,
        )
        session.add(rows[n])
    session.commit()
    sync_reviewed_twins(session, {doc.id})
    session.commit()
    assert rows[1].status == "approved" and rows[1].reviewed_by == verify.AUTO_REVIEWER
    assert rows[1].verification_quote == same
    assert rows[2].status == rows[3].status == "pending"
    # 同一截止另有人核过的：照原来的规则，人核的结果沿用给这一截止（类型、日期、时刻相同）的各份副本，
    # 不被自动通过的那条挡住
    human = DocumentDeadline(
        document_id=rows[1].document_id,
        fingerprint="carry-human",
        kind=candidate.kind,
        candidate_date=candidate.candidate_date,
        evidence=other,
        confidence="高",
        status="approved",
        origin="rule",
        reviewed_by="创始人",
        reviewed_at=datetime.now(UTC),
        approved_date=candidate.candidate_date,
    )
    session.add(human)
    session.commit()
    sync_reviewed_twins(session, {doc.id})
    session.commit()
    assert rows[2].reviewed_by == rows[3].reviewed_by == "创始人"
    assert rows[2].status == rows[3].status == "approved"
    assert rows[1].reviewed_by == verify.AUTO_REVIEWER  # 已经沿用过的不动


def test_auto_skips_when_another_time_of_the_same_day_was_reopened(session, candidate):
    """同一通知同一天，17:00 那条被质检退回、还待核对时，没写时刻的那条规则和 AI 一致也不自动通过：
    这一类一旦核过，退回那条就从清单里消失了，用户照样看到这个日期。"""
    _auto_ai_record(session, candidate)
    detail = session.get(DocumentDetail, candidate.document_id)
    reopened_quote = "网上申报截止时间为2026年9月30日17:00。"
    detail.body_text += reopened_quote
    reopened = DocumentDeadline(
        document_id=candidate.document_id,
        fingerprint="auto-reopened-1700",
        kind=candidate.kind,
        candidate_date=candidate.candidate_date,
        candidate_time=time(17),
        evidence=reopened_quote,
        confidence="高",
        status="pending",
        origin="rule",
        ai_agrees=True,
        audit_state="reopened",
        audit_note="Codex 抽查不一致：原文不符",
    )
    session.add(reopened)
    session.commit()
    results = {
        r["id"]: r["result"]
        for r in verify.auto_verify(session, TODAY, datetime(2026, 9, 27, 12), True)
    }
    assert results[candidate.id] == "同一截止有质检退回，须人工核对"
    session.expire_all()
    assert candidate.status == "pending" and candidate.reviewed_by is None


def test_auto_only_approves_eligible_twins_with_the_same_sentence(session, candidate):
    """自动核对写入时，同组证据相同的副本也要是规则抽出、AI 一致的才一起通过；AI 单独找到的、
    AI 不一致的留在待核对。"""
    _auto_ai_record(session, candidate)
    doc = session.get(Document, candidate.document_id)
    twins = {}
    for n, (origin, agrees) in enumerate((("rule", True), ("ai", None), ("rule", False)), start=1):
        twin_doc = _doc(doc.source, 80 + n, doc.title, TODAY)
        session.add(twin_doc)
        session.flush()
        session.add_all(
            [
                DocumentMerge(
                    duplicate_id=twin_doc.id, canonical_id=doc.id, match_key=f"elig{n}", active=True
                ),
                DocumentDetail(
                    document_id=twin_doc.id,
                    body_text=BODY,
                    status="ok",
                    fetched_at=datetime.now(UTC),
                ),
            ]
        )
        twins[n] = DocumentDeadline(
            document_id=twin_doc.id,
            fingerprint=f"elig-{n}",
            kind=candidate.kind,
            candidate_date=candidate.candidate_date,
            evidence=candidate.evidence,
            confidence="高",
            status="pending",
            origin=origin,
            ai_agrees=agrees,
        )
        session.add(twins[n])
    session.commit()
    results = {
        r["id"]: r["result"]
        for r in verify.auto_verify(session, TODAY, datetime(2026, 9, 27, 12), True)
    }
    assert results[candidate.id] == "通过"
    session.expire_all()
    assert candidate.reviewed_by == twins[1].reviewed_by == verify.AUTO_REVIEWER
    assert twins[2].status == twins[3].status == "pending"
    assert twins[2].reviewed_by is None and twins[3].reviewed_by is None


def test_auto_waits_for_another_writer_in_the_same_notice(session, session_factory, candidate):
    """另一个连接（管理台、Claude 写回）正锁着同一通知另一类的截止行时，自动核对等它提交以后再做，
    不会互相等成死锁：自动核对一开始就锁住这份通知的全部截止行，之后不再去拿别的行。"""
    import threading
    import time as clock

    _auto_ai_record(session, candidate)
    other = DocumentDeadline(
        document_id=candidate.document_id,
        fingerprint="other-kind",
        kind="材料报送截止",
        candidate_date=date(2026, 10, 20),
        evidence="纸质材料请于2026年10月20日前报送。",
        confidence="高",
        status="pending",
        origin="rule",
    )
    session.add(other)
    session.commit()
    holder = session_factory()
    holder.scalars(
        select(DocumentDeadline).where(DocumentDeadline.id == other.id).with_for_update()
    ).all()
    results, errors = [], []

    def run():
        try:
            with session_factory() as worker:
                results.extend(verify.auto_verify(worker, TODAY, datetime(2026, 9, 27, 12), True))
        except Exception as exc:  # noqa: BLE001 - 断言里报出来
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    clock.sleep(1.5)
    assert thread.is_alive()  # 在等另一个连接放开
    # 另一个连接提交以后，自动核对接着做完
    holder.commit()
    holder.close()
    thread.join(30)
    assert not thread.is_alive() and errors == []
    assert [(r["id"], r["result"]) for r in results] == [(candidate.id, "通过")]


def test_auto_holds_the_whole_notice_while_deciding(
    session, session_factory, candidate, monkeypatch
):
    """写库时自动核对一开始就锁住这份通知（各网址）的全部截止行，不分类型：它判断的时候，另一个连接
    去锁同一通知另一类的行要等（这里等锁时间设成 1 秒，等不到就超时）。只锁一类的话这一行是空着的，
    第二遍扫描还要回头去拿，才会和管理台互相等成死锁。"""
    import threading

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    _auto_ai_record(session, candidate)
    other = DocumentDeadline(
        document_id=candidate.document_id,
        fingerprint="other-kind-held",
        kind="材料报送截止",
        candidate_date=date(2026, 10, 20),
        evidence="纸质材料请于2026年10月20日前报送。",
        confidence="高",
        status="pending",
        origin="rule",
    )
    session.add(other)
    session.commit()
    deciding, release = threading.Event(), threading.Event()
    real_reason = verify._auto_reason

    def paused(*args):
        deciding.set()
        release.wait(30)
        return real_reason(*args)

    monkeypatch.setattr(verify, "_auto_reason", paused)
    results, errors = [], []

    def run():
        try:
            with session_factory() as worker:
                results.extend(verify.auto_verify(worker, TODAY, datetime(2026, 9, 27, 12), True))
        except Exception as exc:  # noqa: BLE001 - 断言里报出来
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert deciding.wait(30)
        with session_factory() as probe:
            probe.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            try:
                with pytest.raises(OperationalError) as caught:
                    probe.scalars(
                        select(DocumentDeadline)
                        .where(DocumentDeadline.id == other.id)
                        .with_for_update()
                    ).all()
                assert caught.value.orig.args[0] == 1205
            finally:
                # 这条连接要还回整个测试共用的连接池：等锁时间改回默认，免得后面的测试只等 1 秒
                probe.rollback()
                probe.execute(text("SET SESSION innodb_lock_wait_timeout = DEFAULT"))
                probe.commit()
    finally:
        release.set()
        thread.join(30)
    assert not thread.is_alive() and errors == []
    assert [(r["id"], r["result"]) for r in results] == [(candidate.id, "通过")]


def test_auto_result_is_not_carried_over_when_ai_changed_or_expired(
    session, candidate, monkeypatch
):
    """沿用前对副本把自动核对的条件再核一遍：同组当前的 AI 结果不再相符的、已经过期的、自动核对关着的
    （开关不是 1 或没配 AI 密钥），都不沿用。"""
    from zoneinfo import ZoneInfo

    from youju.dedup import sync_reviewed_twins
    from youju.models import AICheck

    monkeypatch.setattr(verify, "auto_enabled", lambda settings=None: True)
    _auto_ai_record(session, candidate)
    session.commit()
    results = list(verify.auto_verify(session, TODAY, datetime(2026, 9, 27, 12), True))
    assert [r["result"] for r in results] == ["通过"]
    doc = session.get(Document, candidate.document_id)
    twin_doc = _doc(doc.source, 70, doc.title, TODAY)
    session.add(twin_doc)
    session.flush()
    twin = DocumentDeadline(
        document_id=twin_doc.id,
        fingerprint="carry-checks",
        kind=candidate.kind,
        candidate_date=candidate.candidate_date,
        evidence=candidate.evidence,
        confidence="高",
        status="pending",
        origin="rule",
        ai_agrees=True,
    )
    session.add_all(
        [
            DocumentMerge(
                duplicate_id=twin_doc.id, canonical_id=doc.id, match_key="checks", active=True
            ),
            DocumentDetail(
                document_id=twin_doc.id, body_text=BODY, status="ok", fetched_at=datetime.now(UTC)
            ),
            twin,
        ]
    )
    session.commit()
    # 过期了（北京时间 10 月 1 日）：不沿用
    monkeypatch.setattr(
        v1, "now", lambda: datetime(2026, 10, 1, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    sync_reviewed_twins(session, {doc.id})
    session.commit()
    assert twin.status == "pending"
    # 没过期，但 AI 现在给这一天标的是别的类型：不沿用
    monkeypatch.setattr(
        v1, "now", lambda: datetime(2026, 9, 27, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    record = session.scalars(select(AICheck).where(AICheck.document_id == doc.id)).one()
    record.result = {
        **record.result,
        "items": [{**record.result["items"][0], "kind": "材料报送截止"}],
    }
    session.commit()
    sync_reviewed_twins(session, {doc.id})
    session.commit()
    assert twin.status == "pending"
    # AI 改回来，但自动核对关了（开关改回 0 或去掉了 AI 密钥）：不沿用
    record.result = {
        **record.result,
        "items": [{**record.result["items"][0], "kind": candidate.kind}],
    }
    session.commit()
    monkeypatch.setattr(verify, "auto_enabled", lambda settings=None: False)
    sync_reviewed_twins(session, {doc.id})
    session.commit()
    assert twin.status == "pending"
    # 自动核对开着：这一回沿用，原句记它自己的
    monkeypatch.setattr(verify, "auto_enabled", lambda settings=None: True)
    sync_reviewed_twins(session, {doc.id})
    session.commit()
    assert twin.status == "approved" and twin.reviewed_by == verify.AUTO_REVIEWER
