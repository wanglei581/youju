"""需要 MySQL 测试库；清单和写回按真实公开查询及归并规则验证。"""

from datetime import UTC, date, datetime

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
