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
                document_id=twin_doc.id, body_text=BODY, status="ok", fetched_at=datetime.now(UTC)
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
            ),
        ]
    )
    session.commit()
    assert verify.apply_one(session, payload(candidate), True) == "通过"
    session.commit()
    twin = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == twin_doc.id)
    )
    assert (
        twin.status == "approved"
        and twin.reviewed_by == "Claude"
        and twin.verification_quote == payload(candidate)["quote"]
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
