"""每份文件能不能做 AI 解读（D-075）：预先算好、列表行带上、搜索能筛、定时重算。"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from youju import cli
from youju.ai import eligibility
from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import (
    AIEligibility,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    Source,
)

# 北京时间 2026-10-19 10:00
NOW = datetime(2026, 10, 19, 2, tzinfo=UTC)
TODAY = date(2026, 10, 19)
OLD = NOW - timedelta(days=2)
BODY = "申报对象为科技企业。\n支持方式为资金支持。\n请提交申请书及附件。"


@pytest.fixture
def source(session):
    src = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知",
        url="https://kjt.shandong.gov.cn/tz/",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(src)
    session.flush()
    return src


def make(session, src, n, *, kind="申报通知", body=BODY, status="ok", seen=OLD, fetched=OLD):
    doc = Document(
        source_id=src.id,
        url=f"https://kjt.shandong.gov.cn/{n}.html",
        url_key=f"kjt.shandong.gov.cn/{n}.html",
        title=f"关于组织申报2026年度第{n}批科技项目的通知",
        kind_override=kind,
        publish_date=TODAY - timedelta(days=3),
        first_seen_at=seen,
        last_seen_at=seen,
    )
    session.add(doc)
    session.flush()
    if body is not None:
        session.add(
            DocumentDetail(document_id=doc.id, body_text=body, fetched_at=fetched, status=status)
        )
    session.commit()
    return doc


def reasons(session):
    return dict(session.execute(select(AIEligibility.document_id, AIEligibility.reason)).all())


def test_compute_gives_each_reason(session, source):
    other_source = Source(
        level="省级",
        province="山东",
        dept="工信",
        dept_full_name="山东省工业和信息化厅",
        column_name="转载",
        url="https://gxt.shandong.gov.cn/zz/",
        enabled=True,
        parser_config={},
        rights_level=2,
    )
    session.add(other_source)
    session.flush()
    ok = make(session, source, 1)
    listing = make(session, source, 2, kind="名单")
    other = make(session, source, 3, kind="其他")
    blank = make(session, source, 4, body="   \n ")
    no_detail = make(session, source, 5, body=None)
    failed = make(session, source, 6, body="", status="failed")
    long = make(session, source, 7, body="申报材料。" * 5000)
    level2 = make(session, other_source, 8)
    down = make(session, source, 9)
    down.taken_down_at = NOW
    down.takedown_reason = "测试下架"
    session.commit()
    policy = make(session, source, 10, kind="政策")
    assert eligibility.compute(session, ok.id) is None
    assert eligibility.compute(session, policy.id) is None
    assert eligibility.compute(session, listing.id) == "list"
    assert eligibility.compute(session, other.id) == "kind"
    assert eligibility.compute(session, blank.id) == "no_body"
    assert eligibility.compute(session, no_detail.id) == "no_body"
    assert eligibility.compute(session, failed.id) == "no_body"
    assert eligibility.compute(session, long.id) == "too_long"
    assert eligibility.compute(session, level2.id) == "source"
    assert eligibility.compute(session, down.id) == "unavailable"
    assert eligibility.compute(session, 999_999) == "unavailable"


def test_refresh_inserts_updates_and_counts_changes(session, source):
    ok = make(session, source, 1)
    listing = make(session, source, 2, kind="名单")
    assert eligibility.refresh(session, [ok.id, listing.id], NOW) == 2
    assert reasons(session) == {ok.id: None, listing.id: "list"}
    # 结果没变：不算变化，但时间更新
    later = NOW + timedelta(minutes=10)
    assert eligibility.refresh(session, [ok.id, listing.id], later) == 0
    assert {r.computed_at for r in session.scalars(select(AIEligibility))} == {later}
    ok.detail.body_text = " "
    session.commit()
    assert eligibility.refresh(session, [ok.id], later) == 1
    assert reasons(session)[ok.id] == "no_body"
    # 已删除的文件不插新行
    assert eligibility.refresh(session, [999_999], later) == 0
    assert 999_999 not in reasons(session)


def test_concurrent_refresh_of_same_missing_document(session_factory, session, source, monkeypatch):
    """每 10 分钟的增量和夜里的全量同时补算同一份还没算过的文件：后写的一方改成更新，不撞主键。"""
    doc = make(session, source, 1)
    real = eligibility.compute
    raced = []

    def racing(work_session, doc_id):
        if not raced:  # 这一方算到一半，另一个任务把同一份写了进去
            raced.append(doc_id)
            with session_factory() as other:
                assert eligibility.refresh(other, [doc_id], NOW) == 1
        return real(work_session, doc_id)

    monkeypatch.setattr(eligibility, "compute", racing)
    with session_factory() as late:
        assert eligibility.refresh(late, [doc.id], NOW + timedelta(minutes=1)) == 1
    session.expire_all()
    row = session.get(AIEligibility, doc.id)
    assert raced == [doc.id]
    assert row.reason is None and row.computed_at == NOW + timedelta(minutes=1)


def test_refresh_skips_document_deleted_while_computing(
    session_factory, session, source, monkeypatch
):
    keep = make(session, source, 1)
    gone = make(session, source, 2)
    real = eligibility.compute

    def deleting(work_session, doc_id):
        if doc_id == gone.id:
            with session_factory() as other:
                other.execute(delete(Document).where(Document.id == gone.id))
                other.commit()
        return real(work_session, doc_id)

    monkeypatch.setattr(eligibility, "compute", deleting)
    with session_factory() as work:
        eligibility.refresh(work, [keep.id, gone.id], NOW)
    session.expire_all()
    assert reasons(session) == {keep.id: None}


def test_only_one_refresh_writes_at_a_time(session_factory, session, source, monkeypatch, capsys):
    """增量、全量（包括手工跑的）同一时间只让一个写：先算后写的旧结果不会盖掉新结果。"""
    doc = make(session, source, 1)
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(eligibility, "FULL_WAIT", 1)
    with session_factory() as holder, eligibility.writer_lock(holder, 0) as got:
        assert got
        with session_factory() as other, eligibility.writer_lock(other, 0) as second:
            assert second is False
        # 另一个重算在写：增量跳过这次、不算失败；全量等不到算失败（job.sh 提醒）
        assert cli.main(["ai-eligibility"]) == 0
        assert "这次跳过" in capsys.readouterr().out
        assert cli.main(["ai-eligibility-rebuild"]) == 1
        assert "这次全量没算" in capsys.readouterr().out
        assert reasons(session) == {}
    # 锁放开以后照常算
    assert cli.main(["ai-eligibility"]) == 0
    assert reasons(session) == {doc.id: None}


def test_changed_since_covers_each_trigger(session, source):
    since = NOW - eligibility.RECENT
    untouched = make(session, source, 1)
    new = make(session, source, 2, seen=NOW, fetched=NOW)
    refetched = make(session, source, 3)
    refetched.detail.fetched_at = NOW
    decided = make(session, source, 4)
    decided.kind_decided_at = NOW
    down = make(session, source, 5)
    down.taken_down_at = NOW
    down.takedown_reason = "测试下架"
    restored = make(session, source, 6)
    restored.restored_at = NOW
    canonical = make(session, source, 7)
    duplicate = make(session, source, 8)
    session.add(
        DocumentMerge(
            duplicate_id=duplicate.id, canonical_id=canonical.id, match_key="k", created_at=NOW
        )
    )
    # 老的合并关系里副本重新抓了正文：主条目也要重算（它可能在用副本的正文）
    old_canonical = make(session, source, 9)
    old_duplicate = make(session, source, 10)
    session.add(
        DocumentMerge(
            duplicate_id=old_duplicate.id,
            canonical_id=old_canonical.id,
            match_key="k2",
            created_at=OLD,
        )
    )
    session.flush()
    old_duplicate.detail.fetched_at = NOW
    session.commit()
    changed = eligibility.changed_since(session, since)
    assert untouched.id not in changed
    assert {
        new.id,
        refetched.id,
        decided.id,
        down.id,
        restored.id,
        canonical.id,
        duplicate.id,
        old_canonical.id,
        old_duplicate.id,
    } <= changed


def test_refresh_changed_fills_missing_recent_and_rolling(session, source, monkeypatch):
    first = make(session, source, 1)
    second = make(session, source, 2)
    assert eligibility.refresh_changed(session, NOW) == {"checked": 2, "changed": 2}
    # 再跑一次：没有新变化，只有轮转重算（计算时间最早的 ROLLING 份），结果不变
    assert eligibility.refresh_changed(session, NOW + timedelta(minutes=10)) == {
        "checked": 2,
        "changed": 0,
    }
    second.detail.fetched_at = NOW + timedelta(minutes=15)
    second.detail.body_text = "申报材料。" * 5000
    session.commit()
    report = eligibility.refresh_changed(session, NOW + timedelta(minutes=20))
    assert report == {"checked": 2, "changed": 1}
    assert reasons(session) == {first.id: None, second.id: "too_long"}
    # 一次最多补算 MISSING_LIMIT 份没算过的
    monkeypatch.setattr(eligibility, "MISSING_LIMIT", 1)
    third = make(session, source, 3)
    fourth = make(session, source, 4)
    assert eligibility.missing(session, eligibility.MISSING_LIMIT) == [third.id]
    assert fourth.id not in eligibility.missing(session, 1)


def test_rolling_catches_changes_without_timestamps(session, source, monkeypatch):
    """撤回人工类型会清空 kind_decided_at、合并关系被删不留时间：靠轮转补上（最早算的先重算）。"""
    monkeypatch.setattr(eligibility, "ROLLING", 1)
    undone = make(session, source, 1, kind="名单")
    other = make(session, source, 2)
    eligibility.refresh(session, [undone.id], NOW - timedelta(hours=3))
    eligibility.refresh(session, [other.id], NOW - timedelta(hours=1))
    assert reasons(session)[undone.id] == "list"
    undone.kind_override = None  # 撤回：回到按标题判断（标题是申报通知）
    undone.kind_decided_at = None
    session.commit()
    assert undone.id not in eligibility.changed_since(session, NOW - eligibility.RECENT)
    assert eligibility.oldest(session) == [undone.id]
    report = eligibility.refresh_changed(session, NOW)
    assert report == {"checked": 1, "changed": 1}
    assert reasons(session)[undone.id] is None
    # 下一次轮到的是另一份
    assert eligibility.oldest(session) == [other.id]


def test_refresh_all_recomputes_everything(session, source):
    docs = [make(session, source, n) for n in range(1, 4)]
    assert eligibility.refresh_all(session, NOW) == {"checked": 3, "changed": 3}
    # 来源级别只能直接改库，没有时间戳：按时间找不到，全量重算（和轮转）能发现
    session.get(Source, source.id).rights_level = 2
    session.commit()
    assert not eligibility.changed_since(session, NOW + timedelta(hours=1) - eligibility.RECENT)
    assert eligibility.refresh_all(session, NOW + timedelta(hours=1))["changed"] == 3
    assert set(reasons(session).values()) == {"source"}
    assert len(docs) == 3


def test_cli_commands(session, session_factory, source, monkeypatch, capsys):
    make(session, source, 1)
    make(session, source, 2, kind="名单")
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    assert cli.main(["ai-eligibility"]) == 0
    assert "重算 2 份，结果变了 2 份" in capsys.readouterr().out
    assert cli.main(["ai-eligibility-rebuild"]) == 0
    assert "重算 2 份，结果变了 0 份" in capsys.readouterr().out


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(v1, "now", lambda: NOW.astimezone(ZoneInfo("Asia/Shanghai")))
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_rows_carry_ai_and_search_filters(client, session, source):
    ok = make(session, source, 1)
    listing = make(session, source, 2, kind="名单")
    not_yet = make(session, source, 3)
    eligibility.refresh(session, [ok.id, listing.id], NOW)
    items = {
        item["id"]: item
        for item in client.get("/api/v1/search", params={"time": "不限"}).json()["items"]
    }
    assert items[str(ok.id)]["ai"] == {"interpret": True, "clause": True, "reason": None}
    assert items[str(listing.id)]["ai"] == {"interpret": False, "clause": False, "reason": "list"}
    # 还没算过的给 None，小程序就不显示按钮
    assert items[str(not_yet.id)]["ai"] is None
    body = client.get(
        "/api/v1/search", params={"kind": "申报通知", "time": "近 7 天", "ai": 1, "limit": 5}
    ).json()
    assert [item["id"] for item in body["items"]] == [str(ok.id)]
    assert body["total"] == 1 and body["total_all"] == 1
    home = client.get("/api/v1/home").json()
    assert {item["id"]: item["ai"] for item in home["items"]}[str(ok.id)]["interpret"] is True


def test_report_and_calendar_rows_carry_ai(client, session, source):
    # 机器摘录：8 天后进"截止"列表，7 天内进"可能 7 天内截止"
    excerpt = make(session, source, 1, body="申报截止：2026年10月27日。")
    soon = make(session, source, 2, body="申报截止：10月26日。")
    verified = make(session, source, 3)
    past = make(session, source, 4)
    for doc, days, quote in (
        (excerpt, 8, "申报截止：2026年10月27日。"),
        (soon, 7, "申报截止：10月26日。"),
    ):
        session.add(
            DocumentDeadline(
                document_id=doc.id,
                fingerprint=f"f{doc.id}",
                kind="企业申报截止",
                candidate_date=TODAY + timedelta(days=days),
                evidence=quote,
                confidence="高",
                status="pending",
            )
        )
    for doc, day in ((verified, TODAY + timedelta(days=10)), (past, TODAY - timedelta(days=3))):
        session.add(
            DocumentDeadline(
                document_id=doc.id,
                fingerprint=f"v{doc.id}",
                kind="企业申报截止",
                candidate_date=day,
                evidence="截止",
                confidence="高",
                status="approved",
                reviewed_by="创始人",
                reviewed_at=OLD,
                approved_date=day,
            )
        )
    session.commit()
    eligibility.refresh_all(session, NOW)
    report = client.get("/api/v1/reports/deadlines", params={"days": 30}).json()
    assert [item["id"] for item in report["items"]] == [str(verified.id)]
    assert report["items"][0]["ai"]["interpret"] is True
    report = client.get("/api/v1/reports/deadlines", params={"days": 30, "excerpts": 1}).json()
    assert report["items"] and all(item["ai"]["interpret"] for item in report["items"])
    assert report["unverifiedSoon"]["items"][0]["ai"]["interpret"] is True
    calendar = client.get("/api/v1/calendar", params={"limit": 200}).json()
    assert calendar["items"] and all(item["ai"]["interpret"] for item in calendar["items"])
    # 已截止的也带 ai（能不能解读不随截止变）；按钮放不放由小程序定
    assert calendar["past"] and all(item["ai"]["interpret"] for item in calendar["past"])
    calendar = client.get("/api/v1/calendar", params={"limit": 200, "excerpts": 1}).json()
    assert all(item["ai"]["interpret"] for item in calendar["items"])
    assert calendar["unverifiedSoon"]["items"][0]["ai"]["interpret"] is True
    assert calendar["past"] and all(item["ai"]["interpret"] for item in calendar["past"])
    home = client.get("/api/v1/home", params={"excerpts": 1}).json()
    assert all(item["ai"]["interpret"] for item in home["dueSoon"])


def test_link_gone_is_read_at_list_time_not_stored(client, session, source):
    """原网页撤下了（documents.link_gone_at，youju/link_check.py）：表里存的不变，列表行和"只看能解读的"
    当场换成 link_gone，详情页实时判断也一样；恢复了马上回来，不用等重算。本来就不解读的（名单）
    照旧写原来的原因，没有正文的写撤下了（和 interpret.eligible_body 的先后一致）。"""
    ok = make(session, source, 1)
    listing = make(session, source, 2, kind="名单")
    empty = make(session, source, 3, body=None)
    ids = [ok.id, listing.id, empty.id]
    eligibility.refresh(session, ids, NOW)
    for doc in (ok, listing, empty):
        doc.link_gone_at = NOW
    session.commit()
    # 重算也不把 link_gone 写进表（表的约束只认存的那几种）
    assert eligibility.refresh(session, ids, NOW) == 0
    assert reasons(session) == {ok.id: None, listing.id: "list", empty.id: "no_body"}
    flags = eligibility.flags(session, ids)
    assert flags[ok.id] == {"interpret": False, "clause": False, "reason": "link_gone"}
    assert flags[listing.id]["reason"] == "list"
    assert flags[empty.id]["reason"] == "link_gone"
    items = {
        item["id"]: item
        for item in client.get("/api/v1/search", params={"time": "不限"}).json()["items"]
    }
    assert items[str(ok.id)]["ai"]["reason"] == "link_gone"
    assert items[str(ok.id)]["link_gone"] == "2026-10-19"
    recent = {"kind": "申报通知", "time": "近 7 天", "ai": 1, "limit": 5}
    body = client.get("/api/v1/search", params=recent).json()
    assert body["items"] == [] and body["total"] == 0
    for doc, reason in ((ok, "link_gone"), (listing, "list"), (empty, "link_gone")):
        detail = client.get(f"/api/v1/documents/{doc.id}").json()
        assert detail["ai"] == {"interpret": False, "clause": False, "reason": reason}
    # 网页恢复（检查时能打开就清空）：马上回来
    ok.link_gone_at = None
    session.commit()
    assert eligibility.flags(session, [ok.id])[ok.id]["interpret"] is True
    body = client.get("/api/v1/search", params=recent).json()
    assert [item["id"] for item in body["items"]] == [str(ok.id)]
    assert client.get(f"/api/v1/documents/{ok.id}").json()["ai"]["interpret"] is True


def test_effective_reason_follows_eligible_body_order():
    gone = NOW
    for stored in ("unavailable", "list", "kind", "source"):
        assert eligibility.effective_reason(stored, gone) == stored
    for stored in (None, "no_body", "too_long"):
        assert eligibility.effective_reason(stored, gone) == "link_gone"
        assert eligibility.effective_reason(stored, None) == stored
