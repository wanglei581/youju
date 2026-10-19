"""截止线索：提交接口已删（个人信息保护影响评估第六节（二）第 13 条），这里的线索都直接写库，
模拟以前留下的。线索只在私有队列，人工核对后才公开日期与发奖；本人能查看、删除待核对的、导出。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta

import pytest
from sqlalchemy import func, select

from youju import deadline_clues, rewards
from youju.api import v1
from youju.models import DeadlineClue, DocumentDeadline, DocumentMerge, Event, PassReward

from .test_ai_pipeline import add_doc
from .test_rewards import ADMIN, NOW, headers, user
from .test_rewards import context as context


@pytest.fixture
def world(context, session, monkeypatch):
    client, settings = context
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
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


def clue(
    session,
    owner,
    doc,
    *,
    day=date(2026, 10, 20),
    kind="企业申报截止",
    stamp=NOW,
    status="pending",
    quote="请于2026年10月20日前提交材料。",
    deadline_time=None,
):
    row = DeadlineClue(
        user_id=owner.id,
        document_id=doc.id,
        kind=kind,
        date=day,
        time=deadline_time,
        quote=quote,
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


def test_old_clues_private_listing_export_and_delete(world, session):
    """以前留下的线索：本人能查看、导出、删除待核对的；别人看不到也删不了；公开详情不带线索。"""
    client, _, owner, doc = world
    row = clue(session, owner, doc, deadline_time=time(17, 0))
    [item] = client.get("/api/v1/me/deadline-clues", headers=headers(owner.openid)).json()["items"]
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
    assert (item["id"], item["quote"], item["time"], item["status"]) == (
        row.id,
        row.quote,
        "17:00",
        "待核对",
    )
    other = user(session, "other")
    assert (
        client.get("/api/v1/me/deadline-clues", headers=headers(other.openid)).json()["items"] == []
    )
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert detail["deadline"] is None and item["quote"] not in str(detail)
    assert "deadline_clues" not in detail and "can_submit_deadline_clue" not in detail
    exported = client.get("/api/v1/me/data", headers=headers(owner.openid)).json()
    assert exported["deadlineClues"] == [item] and exported["deadlineCluesTotal"] == 1
    url = f"/api/v1/me/deadline-clues/{item['id']}"
    assert client.delete(url, headers=headers(other.openid)).status_code == 404
    assert client.delete(url, headers=headers(owner.openid)).status_code == 204
    assert session.scalar(select(func.count(DeadlineClue.id))) == 0


def test_submission_endpoint_is_gone(world, session):
    """提交线索的接口已删：奖励和批量工具开关都开着、请求合法也写不进去，这个地址只剩查看。"""
    client, settings, owner, doc = world
    assert settings.rewards_enabled and settings.advisor_enabled
    url = "/api/v1/me/deadline-clues"
    response = client.post(url, headers=headers(owner.openid), json=body(doc))
    assert response.status_code == 405 and response.headers["allow"] == "GET"
    assert client.post(url, headers=headers(owner.openid), content=b"bad-json").status_code == 405
    assert session.scalar(select(func.count(DeadlineClue.id))) == 0


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
def test_disabled_admin_review_keeps_clues(world, session, switch):
    client, settings, owner, doc = world
    submitted = clue(session, owner, doc)
    row = candidate(session, doc)
    setattr(settings, switch, False)
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


def test_long_quote_build_and_corrected_approved_date(world, session):
    client, _, owner, doc = world
    quote = "请于2026年10月21日前提交材料。" + "句" * (200 - len("请于2026年10月21日前提交材料。"))
    first_id = clue(session, owner, doc, quote=quote, deadline_time=time(17, 0)).id
    matching = clue(session, user(session, "match"), doc, day=date(2026, 10, 21))
    matching_id, matching_user = matching.id, matching.user_id
    built = client.post(f"/api/admin/deadline-clues/{first_id}/candidate", headers=ADMIN).json()
    row = session.get(DocumentDeadline, built["deadline_id"])
    assert row.evidence == quote and len(row.evidence) == 200
    assert approve(client, row, date="2026-10-21").status_code == 200
    session.expire_all()
    assert session.get(DeadlineClue, first_id).status == "rejected"
    assert session.get(DeadlineClue, matching_id).status == "accepted"
    assert session.scalar(select(PassReward.user_id)) == matching_user
    assert owner.id != matching_user


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

    client, _, owner, doc = world
    submitted = clue(session, owner, doc)
    response = client.post(
        f"/api/admin/deadline-clues/{submitted.id}/candidate", headers=ADMIN
    ).json()
    deadline_id = response["deadline_id"]
    config = alembic_config(db_engine.url.render_as_string(hide_password=False))
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

    client, _, owner, doc = world
    submitted = clue(
        session, owner, doc, kind="材料报送截止", quote="纸质材料于2026年10月20日前报送至科技厅"
    )
    built = client.post(f"/api/admin/deadline-clues/{submitted.id}/candidate", headers=ADMIN).json()
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


@pytest.mark.parametrize(
    "state,kind,can_build",
    [
        ("rejected", "企业申报截止", True),
        ("pending", "材料报送截止", True),
        ("approved", "材料报送截止", True),
        ("pending", "企业申报截止", False),
    ],
)
def test_candidate_blocking_is_kind_and_status_specific(world, session, state, kind, can_build):
    client, _, owner, doc = world
    submitted = clue(session, owner, doc)
    candidate(session, doc, status=state, kind=kind)
    items = client.get("/api/admin/deadline-clues", headers=ADMIN).json()["items"]
    assert items[0]["id"] == submitted.id and items[0]["can_build_candidate"] == can_build
    html = client.get("/admin/review").text
    assert "clueQueue.items.forEach" in html
    assert "clueQueue.items.filter" not in html
    response = client.post(f"/api/admin/deadline-clues/{submitted.id}/candidate", headers=ADMIN)
    assert response.status_code == (200 if can_build else 409)
    assert (
        client.post(f"/api/admin/deadline-clues/{submitted.id}/reject", headers=ADMIN).status_code
        == 200
    )
    pending = select(func.count(DeadlineClue.id)).where(DeadlineClue.status == "pending")
    assert session.scalar(pending) == 0


@pytest.mark.parametrize("status", ["approved", "rejected"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_import_settles_clues_like_single_review(world, session, monkeypatch, status, dry_run):
    client, _, owner, doc = world
    submitted = clue(session, owner, doc)
    row = candidate(session, doc)
    row.fingerprint = "a" * 64
    session.commit()
    calls = []
    original = deadline_clues.settle

    def settle(s, d, stamp):
        calls.append(d.status)
        original(s, d, stamp)

    monkeypatch.setattr(deadline_clues, "settle", settle)
    response = client.post(
        f"/api/admin/reviews/import?dry_run={str(dry_run).lower()}",
        headers=ADMIN,
        json={
            "version": 1,
            "items": [
                {
                    "urlKey": doc.url_key,
                    "fingerprint": row.fingerprint,
                    "kind": row.kind,
                    "candidateDate": row.candidate_date.isoformat(),
                    "evidence": row.evidence,
                    "status": status,
                    "approvedDate": "2026-10-20" if status == "approved" else None,
                    "reviewedBy": "负责人",
                    "reviewedAt": NOW.isoformat(),
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 1 and calls == [status]
    session.refresh(submitted)
    assert submitted.status == ("accepted" if status == "approved" and not dry_run else "pending")
    assert session.scalar(select(func.count(PassReward.id))) == int(
        status == "approved" and not dry_run
    )
    if status == "rejected" and not dry_run:
        assert (
            client.post(
                f"/api/admin/deadline-clues/{submitted.id}/candidate", headers=ADMIN
            ).status_code
            == 200
        )


def test_restricted_api_all_grants_and_switches(
    world, session, restricted_api_factory, monkeypatch
):
    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError

    from youju import access_log, advisor_tasks
    from youju.api import app as app_module
    from youju.db import get_session
    from youju.models import User

    client, settings, _, doc = world
    settings.pay_enabled = True
    identity = headers("limited-on")
    with restricted_api_factory() as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        assert (
            client.post(
                "/api/v1/me/agreement",
                headers=identity,
                json={"version": settings.agreement_version},
            ).status_code
            == 200
        )
        owner = session.scalar(select(User).where(User.openid == "limited-on"))
        # 模拟 admin 发奖，再用受限 API 验证只需 UPDATE 流水即可注销（不需要凭据表的权限）。
        rewards.grant(session, owner.id, "source", 3, "feedback", 1, NOW)
        session.commit()
        assert (
            client.post(
                "/api/v1/me/agreement",
                headers=identity,
                json={"scope": "advisor", "version": advisor_tasks.ADVISOR_AGREEMENT_VERSION},
            ).status_code
            == 200
        )
        # 以前留下的线索和使用记录（提交、埋点的接口已删，直接写库模拟）：受限账号照样能查看、删除、
        # 导出和注销
        pending = clue(session, owner, doc)
        processed = clue(session, owner, doc, kind="材料报送截止", status="accepted")
        session.add(Event(user_id=owner.id, name="search", props={}))
        session.commit()
        items = client.get("/api/v1/me/deadline-clues", headers=identity)
        assert items.status_code == 200
        assert {row["id"] for row in items.json()["items"]} == {pending.id, processed.id}
        url = f"/api/v1/me/deadline-clues/{pending.id}"
        assert client.delete(url, headers=headers("owner")).status_code == 404
        assert client.delete(url, headers=identity).status_code == 204
        clue(session, owner, doc, kind="其他")
        for path in ("/api/v1/me/rewards", "/api/v1/me", "/api/v1/me/data"):
            result = client.get(path, headers=identity)
            assert result.status_code == 200, result.text
        exported = client.get("/api/v1/me/data", headers=identity).json()
        assert (exported["deadlineCluesTotal"], exported["eventsTotal"]) == (2, 1)
        # 部署文件不再给 API 账号这几项权限：写使用记录、新增线索、占线索队列的锁都会被拒
        writes = (
            insert(Event).values(user_id=owner.id, name="search", props={}),
            insert(DeadlineClue).values(
                user_id=owner.id,
                document_id=doc.id,
                kind="其他",
                date=date(2026, 10, 20),
                quote="请于2026年10月20日前提交材料。",
                status="pending",
                submitted_at=NOW,
            ),
        )
        for statement in writes:
            with limited() as s, pytest.raises(DBAPIError):
                s.execute(statement)
        with limited() as s, pytest.raises(DBAPIError):
            deadline_clues.lock_queue(s)
        assert client.delete("/api/v1/me", headers=identity).status_code == 204
        session.expire_all()
        assert session.scalar(select(User.id).where(User.openid == "limited-on")) is None
        # 待核对的删掉，处理过的保留、断开用户；使用记录随用户删除
        assert session.scalars(select(DeadlineClue.id)).all() == [processed.id]
        assert session.get(DeadlineClue, processed.id).user_id is None
        assert session.scalar(select(func.count(Event.id))) == 0
        assert (
            session.scalar(select(func.count(PassReward.id)).where(PassReward.user_id.is_(None)))
            == 1
        )


def test_rebuild_rejected_user_candidate_reuses_fingerprint(world, session):
    client, _, owner, doc = world
    submitted = clue(session, owner, doc)
    url = f"/api/admin/deadline-clues/{submitted.id}/candidate"
    first = client.post(url, headers=ADMIN).json()["deadline_id"]
    assert (
        client.post(
            f"/api/admin/deadlines/{first}/reject", headers=ADMIN, json={"reviewed_by": "负责人"}
        ).status_code
        == 200
    )
    rebuilt = client.post(url, headers=ADMIN)
    assert rebuilt.status_code == 200 and rebuilt.json()["deadline_id"] == first
    assert (
        client.post(
            f"/api/admin/deadlines/{first}/approve", headers=ADMIN, json={"reviewed_by": "负责人"}
        ).status_code
        == 200
    )
