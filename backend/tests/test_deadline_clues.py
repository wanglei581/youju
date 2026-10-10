"""用户线索只进私有队列，人工核对后才公开日期与发奖。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from youju import deadline_clues, rewards
from youju.api import me, v1
from youju.models import DeadlineClue, DocumentDeadline, DocumentMerge, PassReward

from .test_ai_pipeline import add_doc
from .test_rewards import ADMIN, NOW, headers, user
from .test_rewards import context as context


@pytest.fixture
def world(context, session, monkeypatch):
    client, settings = context
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    monkeypatch.setattr(me, "check_feedback", lambda *_: True)
    owner = user(session, "owner")
    doc = add_doc(session)
    return client, settings, owner, doc


def body(doc, **changes):
    return {
        "document_id": doc.id,
        "kind": "企业申报截止",
        "date": "2026-10-20",
        "time": "17:00",
        "quote": "  请于2026年10月20日17:00前提交申报材料。  ",
        **changes,
    }


def submit(world, **changes):
    client, _, owner, doc = world
    return client.post(
        "/api/v1/me/deadline-clues", headers=headers(owner.openid), json=body(doc, **changes)
    )


def clue(
    session, owner, doc, *, day=date(2026, 10, 20), kind="企业申报截止", stamp=NOW, status="pending"
):
    row = DeadlineClue(
        user_id=owner.id,
        document_id=doc.id,
        kind=kind,
        date=day,
        quote="请于2026年10月20日前提交材料。",
        submitted_at=stamp,
        status=status,
    )
    session.add(row)
    session.commit()
    return row


def candidate(session, doc, *, kind="企业申报截止", day=date(2026, 10, 20), status="pending"):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{kind}-{day}",
        kind=kind,
        candidate_date=day,
        evidence="请于2026年10月20日前提交材料。",
        confidence="高",
        status=status,
        approved_date=day if status == "approved" else None,
    )
    session.add(row)
    session.commit()
    return row


def approve(client, row, **changes):
    return client.post(
        f"/api/admin/deadlines/{row.id}/approve",
        headers=ADMIN,
        json={"reviewed_by": "负责人", **changes},
    )


def test_submit_private_listing_export_and_delete(world, session):
    client, _, owner, doc = world
    res = submit(world)
    assert res.status_code == 201, res.text
    item = res.json()
    assert set(item) == {
        "id",
        "document_id",
        "kind",
        "date",
        "time",
        "quote",
        "status",
        "submitted_at",
        "processed_at",
        "deadline_id",
    }
    assert item["quote"] == body(doc)["quote"].strip() and item["status"] == "待核对"
    assert client.get("/api/v1/me/deadline-clues", headers=headers(owner.openid)).json()[
        "items"
    ] == [item]
    other = user(session, "other")
    assert (
        client.get("/api/v1/me/deadline-clues", headers=headers(other.openid)).json()["items"] == []
    )
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert detail["deadline"] is None and detail["can_submit_deadline_clue"] is True
    assert item["quote"] not in str(detail) and "deadline_clues" not in detail
    exported = client.get("/api/v1/me/data", headers=headers(owner.openid)).json()
    assert exported["deadlineClues"] == [item] and exported["deadlineCluesTotal"] == 1
    url = f"/api/v1/me/deadline-clues/{item['id']}"
    assert client.delete(url, headers=headers(other.openid)).status_code == 404
    assert client.delete(url, headers=headers(owner.openid)).status_code == 204
    assert session.scalar(select(func.count(DeadlineClue.id))) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"document_id": 0},
        {"document_id": "1"},
        {"document_id": 999999},
        {"kind": "未知性质"},
        {"date": "2026-02-30"},
        {"date": "20261020"},
        {"date": 1792454400},
        {"date": "2026-09-19"},
        {"date": "2029-09-21"},
        {"time": "24:00"},
        {"time": "17:60"},
        {"time": "1:00"},
        {"time": "17:00:00"},
        {"quote": "   短句   "},
        {"quote": "字" * 201},
        {"quote": 123},
        {"extra": True},
    ],
)
def test_input_validation(world, session, change):
    assert submit(world, **change).status_code == 422
    assert session.scalar(select(func.count(DeadlineClue.id))) == 0


@pytest.mark.parametrize("mutation", ["kind", "hidden", "rights", "missing_date"])
def test_document_validation(world, session, mutation):
    _, _, _, doc = world
    if mutation == "kind":
        doc.kind_override = "政策"
    elif mutation == "hidden":
        doc.taken_down_at = NOW
    elif mutation == "rights":
        doc.source.rights_level = 2
    else:
        doc.publish_date = None
    session.commit()
    assert submit(world).status_code == 422


@pytest.mark.parametrize("day,quote", [("2026-09-20", "字" * 6), ("2029-09-20", "字" * 200)])
def test_date_quote_boundaries_and_optional_time(world, day, quote):
    assert submit(world, date=day, time=None, quote=quote).status_code == 201


def test_leap_year_boundaries(world, session):
    doc = world[3]
    doc.publish_date = date(2024, 2, 29)
    session.commit()
    assert submit(world, date="2027-03-01").status_code == 422
    assert submit(world, date="2027-02-28").status_code == 201


@pytest.mark.parametrize("identity", ["anonymous", "unknown", "unagreed", "outdated"])
def test_login_and_agreement(world, session, identity):
    client, _, owner, doc = world
    request_headers = headers(owner.openid)
    if identity == "anonymous":
        request_headers = {}
    elif identity == "unknown":
        request_headers = headers("unknown")
    elif identity == "unagreed":
        owner.agreement_at = None
    else:
        owner.agreement_version = "old"
    session.commit()
    assert client.post(
        "/api/v1/me/deadline-clues", headers=request_headers, json=body(doc)
    ).status_code == (401 if identity == "anonymous" else 403)


@pytest.mark.parametrize("result", [False, "error", "http"])
def test_safety_fail_closed(world, session, monkeypatch, result):
    def check(*_):
        if result == "error":
            raise ValueError("private-content")
        if result == "http":
            raise httpx.ConnectError("private-content")
        return False

    monkeypatch.setattr(me, "check_feedback", check)
    response = submit(world)
    assert response.status_code == (422 if result is False else 503)
    assert "private-content" not in response.text
    assert session.scalar(select(func.count(DeadlineClue.id))) == 0


def test_safety_called_and_sensitive_quote_masked(world, session, monkeypatch):
    seen = []
    monkeypatch.setattr(me, "check_feedback", lambda uid, text: seen.append((uid, text)) or True)
    res = submit(world, quote="请于10月20日前联系13812345678提交材料。")
    assert res.status_code == 201
    assert seen == [(world[2].openid, res.json()["quote"])]
    assert "13812345678" not in res.text


@pytest.mark.parametrize("limit", ["duplicate", "user", "site", "approved"])
def test_conflicts(world, session, limit):
    client, _, owner, doc = world
    if limit == "duplicate":
        clue(session, owner, doc)
        message = "你已经提交过这项线索，请等核对结果"
    elif limit == "approved":
        candidate(session, doc, status="approved")
        message = "这份通知的截止日期已经核对过了"
    else:
        for i in range(20 if limit == "user" else 50):
            other_doc = add_doc(session, number=100 + i)
            other_owner = owner if limit == "user" else user(session, f"other-{i}")
            clue(session, other_owner, other_doc)
        message = (
            "你已有 20 条待核对线索，请等核对结果"
            if limit == "user"
            else "线索暂时收满了，过几天再来"
        )
    response = submit(world)
    assert response.status_code == 409 and response.json()["detail"] == message
    can_submit = client.get(f"/api/v1/documents/{doc.id}").json()["can_submit_deadline_clue"]
    assert can_submit == (limit in ("duplicate", "user"))


def test_can_submit_kind_and_rewards(world, session):
    client, settings, _, doc = world
    settings.rewards_enabled = False
    assert client.get(f"/api/v1/documents/{doc.id}").json()["can_submit_deadline_clue"] is False
    settings.rewards_enabled = True
    doc.kind_override = "政策"
    session.commit()
    assert client.get(f"/api/v1/documents/{doc.id}").json()["can_submit_deadline_clue"] is False


def test_admin_build_pending_only_and_list(world, session):
    client, _, owner, doc = world
    first = clue(session, owner, doc)
    assert client.get("/api/admin/deadline-clues").status_code == 401
    queue = client.get("/api/admin/deadline-clues", headers=ADMIN).json()
    assert queue["enabled"] and queue["items"][0]["can_build_candidate"]
    assert "user_id" not in str(queue) and owner.openid not in str(queue)
    url = f"/api/admin/deadline-clues/{first.id}/candidate"
    assert client.post(url).status_code == 401
    built = client.post(url, headers=ADMIN)
    assert built.status_code == 200, built.text
    row = session.get(DocumentDeadline, built.json()["deadline_id"])
    assert row.origin == "user" and row.status == "pending" and row.evidence == first.quote
    assert client.get(f"/api/v1/documents/{doc.id}").json()["deadline"] is None
    items = client.get("/api/admin/deadlines?due=all", headers=ADMIN).json()
    assert items[0]["deadline_clues"][0]["id"] == first.id
    assert client.post(url, headers=ADMIN).status_code == 409
    assert (
        client.get("/api/admin/deadline-clues", headers=ADMIN).json()["items"][0][
            "can_build_candidate"
        ]
        is False
    )
    assert "用这条线索建候选" in client.get("/admin/review").text


def test_accept_earliest_reject_other_dates_undo_no_duplicate(world, session):
    client, _, owner, doc = world
    earliest = clue(session, owner, doc, stamp=NOW - timedelta(seconds=1))
    other = user(session, "other")
    later = clue(session, other, doc)
    wrong = clue(session, user(session, "wrong"), doc, day=date(2026, 10, 21))
    row = candidate(session, doc)
    assert approve(client, row).status_code == 200
    session.expire_all()
    assert session.get(DeadlineClue, earliest.id).status == "accepted"
    assert session.get(DeadlineClue, later.id).status == "accepted"
    assert session.get(DeadlineClue, wrong.id).status == "rejected"
    assert all(
        session.get(DeadlineClue, c.id).processed_at
        and session.get(DeadlineClue, c.id).deadline_id == row.id
        for c in (earliest, later, wrong)
    )
    reward = session.scalar(select(PassReward))
    assert reward.user_id == owner.id and reward.granted_days == 1
    assert reward.ref_type == deadline_clues.reward_ref(row.kind) and reward.ref_id == doc.id
    assert client.get(f"/api/v1/documents/{doc.id}").json()["deadline"]["date"] == "2026-10-20"
    assert (
        client.delete(
            f"/api/v1/me/deadline-clues/{earliest.id}", headers=headers(owner.openid)
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/admin/deadlines/{row.id}/undo", headers=ADMIN, json={"reviewer": "负责人"}
        ).status_code
        == 200
    )
    assert approve(client, row).status_code == 200
    assert session.scalar(select(func.count(PassReward.id))) == 1
    assert session.get(DeadlineClue, earliest.id).status == "accepted"


def test_reject_candidate_keeps_clue_and_explicit_reject(world, session):
    client, _, owner, doc = world
    submitted = clue(session, owner, doc)
    row = candidate(session, doc)
    assert (
        client.post(
            f"/api/admin/deadlines/{row.id}/reject", headers=ADMIN, json={"reviewed_by": "负责人"}
        ).status_code
        == 200
    )
    session.refresh(submitted)
    assert submitted.status == "pending"
    url = f"/api/admin/deadline-clues/{submitted.id}/reject"
    assert client.post(url, headers=ADMIN).json()["status"] == "未采纳"
    assert client.post(url, headers=ADMIN).status_code == 409
    assert client.post("/api/admin/deadline-clues/999/reject", headers=ADMIN).status_code == 404
    assert client.post("/api/admin/deadline-clues/999/candidate", headers=ADMIN).status_code == 404


def test_reward_failure_review_persists_reconcile(world, session, monkeypatch, caplog):
    client, settings, owner, doc = world
    submitted = clue(session, owner, doc)
    row = candidate(session, doc)
    original = rewards.grant

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("must-not-leak")

    monkeypatch.setattr(rewards, "grant", fail)
    assert approve(client, row).status_code == 200
    session.expire_all()
    assert session.get(DocumentDeadline, row.id).status == "approved"
    assert session.get(DeadlineClue, submitted.id).status == "accepted"
    assert session.scalar(select(func.count(PassReward.id))) == 0
    assert "rewards-reconcile" in caplog.text and "must-not-leak" not in caplog.text
    monkeypatch.setattr(rewards, "grant", original)
    assert rewards.reconcile(session, settings=settings, stamp=NOW)["granted"] == 1
    assert rewards.reconcile(session, settings=settings, stamp=NOW)["granted"] == 0


def test_monthly_ten_and_kind_specific_reference(world, session):
    client, _, owner, _ = world
    for i in range(11):
        doc = add_doc(session, number=100 + i)
        clue(session, owner, doc)
        assert approve(client, candidate(session, doc)).status_code == 200
    rows = session.scalars(select(PassReward).order_by(PassReward.id)).all()
    assert [r.granted_days for r in rows] == [1] * 10 + [0]
    doc = add_doc(session, number=200)
    for kind in ("企业申报截止", "材料报送截止"):
        clue(session, user(session, kind), doc, kind=kind)
        assert approve(client, candidate(session, doc, kind=kind)).status_code == 200
    assert session.scalar(select(func.count(PassReward.id)).where(PassReward.ref_id == doc.id)) == 2


@pytest.mark.parametrize("switch", ["rewards_enabled", "advisor_enabled"])
def test_disabled_submit_admin_review_keeps_clues(world, session, switch):
    client, settings, owner, doc = world
    submitted = clue(session, owner, doc)
    row = candidate(session, doc)
    setattr(settings, switch, False)
    assert submit(world).status_code == 404
    assert client.post("/api/v1/me/deadline-clues", content=b"bad-json").status_code == 404
    assert client.get("/api/admin/deadline-clues", headers=ADMIN).json() == {
        "enabled": False,
        "items": [],
    }
    assert (
        client.get("/api/admin/deadlines?due=all", headers=ADMIN).json()[0]["deadline_clues"] == []
    )
    for action in ("candidate", "reject"):
        assert (
            client.post(
                f"/api/admin/deadline-clues/{submitted.id}/{action}", headers=ADMIN
            ).status_code
            == 404
        )
    assert approve(client, row).status_code == 200
    session.refresh(submitted)
    assert submitted.status == "pending"
    assert session.scalar(select(func.count(PassReward.id))) == 0
    assert client.get("/api/v1/me/deadline-clues", headers=headers(owner.openid)).status_code == 200


def test_delete_account_pending_removed_processed_unlinked(world, session):
    client, settings, owner, doc = world
    accepted = clue(session, owner, doc, status="accepted")
    rejected = clue(session, owner, doc, kind="材料报送截止", status="rejected")
    pending = clue(session, owner, doc, kind="其他")
    pending_id = pending.id
    settings.rewards_enabled = False
    assert client.delete("/api/v1/me", headers=headers(owner.openid)).status_code == 204
    session.expire_all()
    assert session.get(DeadlineClue, pending_id) is None
    assert session.get(DeadlineClue, accepted.id).user_id is None
    assert session.get(DeadlineClue, rejected.id).user_id is None


def test_latest_fifty_and_delete_without_current_agreement(world, session):
    client, _, owner, doc = world
    for i in range(55):
        clue(session, owner, doc, status="rejected", stamp=NOW + timedelta(seconds=i))
    pending = clue(session, owner, doc)
    items = client.get("/api/v1/me/deadline-clues", headers=headers(owner.openid)).json()["items"]
    assert len(items) == 50 and items[0]["submitted_at"] > items[-1]["submitted_at"]
    owner.agreement_version = "old"
    session.commit()
    assert (
        client.delete(
            f"/api/v1/me/deadline-clues/{pending.id}", headers=headers(owner.openid)
        ).status_code
        == 204
    )


def test_merged_document_no_duplicate_submission(world, session):
    client, _, _, doc = world
    duplicate = add_doc(session, number=2)
    session.add(
        DocumentMerge(duplicate_id=duplicate.id, canonical_id=doc.id, active=True, match_key="test")
    )
    session.commit()
    assert submit(world, document_id=duplicate.id).json()["document_id"] == doc.id
    assert submit(world).status_code == 409
    candidate(session, duplicate, status="approved")
    assert submit(world, kind="企业申报截止").json()["detail"] == "这份通知的截止日期已经核对过了"
    assert (
        client.get(f"/api/v1/documents/{duplicate.id}").json()["can_submit_deadline_clue"] is False
    )


def test_concurrent_submission_unique(world, session):
    client, _, owner, doc = world

    def send(_):
        return client.post(
            "/api/v1/me/deadline-clues", headers=headers(owner.openid), json=body(doc)
        ).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(send, range(3)))
    assert sorted(codes) == [201, 409, 409]
    assert session.scalar(select(func.count(DeadlineClue.id))) == 1


def test_long_quote_build_and_corrected_approved_date(world, session):
    client, _, owner, doc = world
    quote = "请于2026年10月21日前提交材料。" + "句" * (200 - len("请于2026年10月21日前提交材料。"))
    first = submit(world, quote=quote).json()
    matching = clue(session, user(session, "match"), doc, day=date(2026, 10, 21))
    built = client.post(f"/api/admin/deadline-clues/{first['id']}/candidate", headers=ADMIN).json()
    row = session.get(DocumentDeadline, built["deadline_id"])
    assert row.evidence == quote and len(row.evidence) == 200
    assert approve(client, row, date="2026-10-21").status_code == 200
    session.expire_all()
    assert session.get(DeadlineClue, first["id"]).status == "rejected"
    assert session.get(DeadlineClue, matching.id).status == "accepted"
    assert session.scalar(select(PassReward.user_id)) == matching.user_id
    assert owner.id != matching.user_id


def test_referred_switch_does_not_disable_clues(world):
    world[1].referral_enabled = False
    assert submit(world).status_code == 201


@pytest.mark.parametrize("capacity", ["site", "user"])
def test_concurrent_capacity(world, session, capacity):
    client, _, owner, _ = world
    for i in range(49 if capacity == "site" else 19):
        doc = add_doc(session, number=100 + i)
        clue_owner = user(session, f"owner-{i}") if capacity == "site" else owner
        clue(session, clue_owner, doc)
    docs = [add_doc(session, number=300 + i) for i in range(3)]

    def send(doc):
        return client.post(
            "/api/v1/me/deadline-clues", headers=headers(owner.openid), json=body(doc)
        ).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(send, docs))
    assert sorted(codes) == [201, 409, 409]
    assert deadline_clues.pending_count(session, None if capacity == "site" else owner.id) == (
        50 if capacity == "site" else 20
    )


def test_concurrent_approvals_one_award(world, session):
    client, _, owner, doc = world
    submitted = clue(session, owner, doc)
    row = candidate(session, doc)
    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(lambda _: approve(client, row).status_code, range(3)))
    assert sorted(codes) == [200, 409, 409]
    session.refresh(submitted)
    assert submitted.status == "accepted"
    assert session.scalar(select(func.count(PassReward.id))) == 1


def test_concurrent_month_limit(world, session):
    client, _, owner, _ = world
    for i in range(9):
        doc = add_doc(session, number=100 + i)
        clue(session, owner, doc)
        assert approve(client, candidate(session, doc)).status_code == 200
    rows = []
    for i in range(3):
        doc = add_doc(session, number=300 + i)
        clue(session, owner, doc)
        rows.append(candidate(session, doc))
    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(lambda row: approve(client, row).status_code, rows))
    assert codes == [200] * 3
    assert session.scalar(select(func.sum(PassReward.granted_days))) == 10


def test_anonymous_winner_does_not_award_later_user(world, session):
    client, settings, owner, doc = world
    earliest = clue(session, owner, doc, stamp=NOW - timedelta(seconds=1), status="accepted")
    clue(session, user(session, "later"), doc, status="accepted")
    earliest.user_id = None
    session.commit()
    assert rewards.reconcile(session, settings=settings, stamp=NOW)["granted"] == 0
    assert session.scalar(select(func.count(PassReward.id))) == 0


def test_migration_user_candidate_down_up(world, session, db_engine):
    from alembic import command
    from sqlalchemy import inspect

    from youju.migrations import alembic_config

    client, _, _, _ = world
    submitted = submit(world).json()
    response = client.post(
        f"/api/admin/deadline-clues/{submitted['id']}/candidate", headers=ADMIN
    ).json()
    deadline_id = response["deadline_id"]
    config = alembic_config(str(db_engine.url))
    try:
        command.downgrade(config, "0026")
        assert "deadline_clues" not in inspect(db_engine).get_table_names()
        assert "reward_claims" not in inspect(db_engine).get_table_names()
    finally:
        command.upgrade(config, "head")
        session.expire_all()
    assert session.get(DocumentDeadline, deadline_id) is None


def test_rule_reextract_preserves_user_candidate(world, session):
    from youju.extract.service import extract_existing

    client, _, _, _ = world
    submitted = submit(
        world, kind="材料报送截止", time=None, quote="纸质材料于2026年10月20日前报送至科技厅"
    ).json()
    built = client.post(
        f"/api/admin/deadline-clues/{submitted['id']}/candidate", headers=ADMIN
    ).json()
    row = session.get(DocumentDeadline, built["deadline_id"])
    fingerprint, evidence = row.fingerprint, row.evidence
    extract_existing(session)
    session.refresh(row)
    assert row.origin == "user" and row.status == "pending"
    assert row.fingerprint == fingerprint and row.evidence == evidence
    assert (
        session.scalar(
            select(func.count(DocumentDeadline.id)).where(DocumentDeadline.origin == "rule")
        )
        == 2
    )
    assert session.scalar(select(func.count(PassReward.id))) == 0


def test_approval_on_merged_duplicate_settles_master_clues(world, session):
    client, _, owner, doc = world
    duplicate = add_doc(session, number=2)
    session.add(
        DocumentMerge(duplicate_id=duplicate.id, canonical_id=doc.id, active=True, match_key="test")
    )
    session.commit()
    submitted = clue(session, owner, doc)
    row = candidate(session, duplicate)
    assert approve(client, row).status_code == 200
    session.refresh(submitted)
    assert submitted.status == "accepted" and submitted.deadline_id == row.id
    assert session.scalar(select(PassReward.ref_id)) == doc.id


def test_merge_after_award_does_not_reaward(world, session):
    client, settings, owner, doc = world
    duplicate = add_doc(session, number=2)
    clue(session, owner, duplicate)
    row = candidate(session, duplicate)
    assert approve(client, row).status_code == 200
    assert session.scalar(select(PassReward.ref_id)) == duplicate.id
    session.add(
        DocumentMerge(duplicate_id=duplicate.id, canonical_id=doc.id, active=True, match_key="test")
    )
    session.commit()
    assert rewards.reconcile(session, settings=settings, stamp=NOW)["granted"] == 0
    assert session.scalar(select(func.count(PassReward.id))) == 1


@pytest.mark.parametrize("mutation", ["date", "hidden"])
def test_document_changed_during_safety_check(world, session, monkeypatch, mutation):
    doc = world[3]

    def check(*_):
        if mutation == "date":
            doc.publish_date = date(2026, 10, 21)
        else:
            doc.taken_down_at = NOW
        session.commit()
        return True

    monkeypatch.setattr(me, "check_feedback", check)
    assert submit(world).status_code == 422
    assert session.scalar(select(func.count(DeadlineClue.id))) == 0
