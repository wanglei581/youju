"""核对结果导出、导入：在一个库里核对，导入另一个库（如国庆期间在开发库核对，上线后导入正式库）。"""

import copy
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentDeadline, Source

HEADERS = {"X-Admin-Token": "secret"}


NOW = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    # 日历的 items 只放还没截止的（截止过的在 past 里）；数据写的是 2026 年 9—11 月，
    # 业务时间固定在 10/1，免得日子一过就挪进 past（10/21 起 10/20 那条就不在 items 里了）
    monkeypatch.setattr(v1, "now", lambda: NOW)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _doc(session, src, key, title, day):
    now = datetime(2026, 9, 28, 8, 0)
    doc = Document(
        source_id=src.id,
        url=f"https://kw.beijing.gov.cn/{key}.html",
        url_key=f"kw.beijing.gov.cn/{key}.html",
        title=title,
        publish_date=day,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.flush()
    return doc


def _candidate(session, doc, fingerprint, kind, due, evidence):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=fingerprint,
        kind=kind,
        candidate_date=due,
        evidence=evidence,
        confidence="高",
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def reviewed(client, session):
    """源库：三条候选分别通过（改了日期时间）、通过后标为延期、驳回；另一条没核对。"""
    src = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url="https://kw.beijing.gov.cn/tz/",
        enabled=True,
        parser_config={},
    )
    session.add(src)
    session.flush()
    notice = _doc(session, src, "a", "关于开展2026年度专项申报工作的通知", date(2026, 9, 1))
    delay = _doc(session, src, "b", "关于延长2026年度专项申报截止时间的通知", date(2026, 9, 10))
    other = _doc(session, src, "c", "关于组织申报科技计划项目的通知", date(2026, 9, 5))
    first = _candidate(session, notice, "a" * 64, "企业申报截止", date(2026, 10, 18), "10月18日")
    second = _candidate(session, notice, "b" * 64, "材料报送截止", date(2026, 10, 25), "10月25日")
    third = _candidate(session, other, "c" * 64, "企业申报截止", date(2026, 11, 1), "11月1日")
    _candidate(session, other, "d" * 64, "推荐或审核截止", date(2026, 11, 8), "11月8日")
    session.commit()
    base = "/api/admin/deadlines"
    ok = {"reviewed_by": "核对人甲"}
    r = client.post(
        f"{base}/{first.id}/approve",
        headers=HEADERS,
        json={**ok, "date": "2026-10-20", "time": "17:00"},
    )
    assert r.status_code == 200
    assert client.post(f"{base}/{second.id}/approve", headers=HEADERS, json=ok).status_code == 200
    change = {**ok, "reason": "延期", "new_document_id": delay.id}
    assert (
        client.post(f"{base}/{second.id}/change", headers=HEADERS, json=change).status_code == 200
    )
    assert client.post(f"{base}/{third.id}/reject", headers=HEADERS, json=ok).status_code == 200
    return SimpleNamespace(notice=notice, delay=delay, other=other)


def _reset_to_pending(session):
    """模拟另一个库：同样的候选重新抽取出来，都还没核对。"""
    session.execute(
        update(DocumentDeadline).values(
            status="pending",
            reviewed_by=None,
            reviewed_at=None,
            approved_date=None,
            approved_time=None,
            changed_to_document_id=None,
            change_reason=None,
            changed_by=None,
            changed_at=None,
        )
    )
    session.commit()


def _statuses(session):
    session.expire_all()
    return {
        d.fingerprint[0]: (d.status, d.approved_date, d.approved_time, d.change_reason)
        for d in session.query(DocumentDeadline).order_by(DocumentDeadline.id)
    }


def test_export_import_roundtrip_preview_then_apply_then_idempotent(client, session, reviewed):
    exported = client.get("/api/admin/reviews/export", headers=HEADERS)
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    body = exported.json()
    assert body["version"] == 1
    # 只导出核对过的（通过、驳回、已变更），没核对的不导出
    assert sorted(i["status"] for i in body["items"]) == ["approved", "changed", "rejected"]
    changed = next(i for i in body["items"] if i["status"] == "changed")
    assert changed["change"]["toUrlKey"] == "kw.beijing.gov.cn/b.html"
    assert changed["change"]["reason"] == "延期"
    source = _statuses(session)

    _reset_to_pending(session)
    assert all(s[0] == "pending" for s in _statuses(session).values())
    # 公开端这时看不到任何截止
    assert client.get("/api/v1/calendar").json()["items"] == []

    preview = client.post("/api/admin/reviews/import", headers=HEADERS, json=body)
    assert preview.status_code == 200
    assert preview.json()["dryRun"] is True and preview.json()["applied"] == 3
    assert all(s[0] == "pending" for s in _statuses(session).values())  # 预览不写入

    done = client.post("/api/admin/reviews/import?dry_run=false", headers=HEADERS, json=body)
    assert done.json()["applied"] == 3 and done.json()["conflict"] == 0
    assert _statuses(session) == source
    # 日历按导入的结果显示：改过的日期；已延期的不再作为有效截止
    dues = [i["due"] for i in client.get("/api/v1/calendar").json()["items"]]
    assert dues == ["2026-10-20"]

    again = client.post("/api/admin/reviews/import?dry_run=false", headers=HEADERS, json=body)
    assert again.json()["applied"] == 0 and again.json()["already"] == 3


def test_import_reports_conflicts_and_missing_without_overwriting(client, session, reviewed):
    body = client.get("/api/admin/reviews/export", headers=HEADERS).json()
    _reset_to_pending(session)
    # 这边已经把"企业申报截止"核对成另一个日期：记为冲突，不覆盖
    row = session.query(DocumentDeadline).filter_by(fingerprint="a" * 64).one()
    row.status, row.reviewed_by, row.reviewed_at = "approved", "另一核对人", datetime(2026, 9, 29)
    row.approved_date = date(2026, 10, 19)
    session.commit()
    edited = copy.deepcopy(body)
    for item in edited["items"]:
        if item["status"] == "changed":
            # 这边还没抓到发布延期的新通知：整条不导入（只导入"通过"会让已延期的截止重新显示）
            item["change"]["toUrlKey"] = "kw.beijing.gov.cn/not-yet.html"
        if item["status"] == "rejected":
            item["fingerprint"] = "e" * 64  # 这边还没有这条候选（日期也不同，兜底匹配不上）
            item["candidateDate"] = "2026-12-31"
    result = client.post("/api/admin/reviews/import?dry_run=false", headers=HEADERS, json=edited)
    got = result.json()
    assert (got["applied"], got["conflict"], got["missingTarget"], got["missingCandidate"]) == (
        0,
        1,
        1,
        1,
    )
    assert {p["reason"] for p in got["problems"]} >= {"这边已核对，结果不同，未覆盖"}
    now = _statuses(session)
    assert now["a"] == ("approved", date(2026, 10, 19), None, None)  # 没被覆盖
    assert now["b"][0] == "pending"  # 延期的那条没有被当作"通过"导入
    assert now["c"][0] == "pending"


def test_import_rejects_malformed_items_and_requires_token(client, session, reviewed):
    body = client.get("/api/admin/reviews/export", headers=HEADERS).json()
    assert client.post("/api/admin/reviews/import", json=body).status_code == 401
    bad = copy.deepcopy(body)
    rejected = next(i for i in bad["items"] if i["status"] == "rejected")
    rejected["approvedDate"] = "2026-11-01"  # 驳回的记录不能带核定日期
    assert client.post("/api/admin/reviews/import", headers=HEADERS, json=bad).status_code == 422
    missing = copy.deepcopy(body)
    missing["items"][0]["urlKey"] = "kw.beijing.gov.cn/unknown.html"
    got = client.post("/api/admin/reviews/import", headers=HEADERS, json=missing).json()
    assert got["missingDocument"] == 1


def test_import_falls_back_to_same_kind_and_date_when_evidence_changed(client, session, reviewed):
    body = client.get("/api/admin/reviews/export", headers=HEADERS).json()
    _reset_to_pending(session)
    edited = copy.deepcopy(body)
    for item in edited["items"]:
        if item["status"] == "approved":
            # 重抓后证据句的空白或断句变了，指纹对不上；同一文件里类型、日期、时间相同的只有一条
            item["fingerprint"] = "f" * 64
    got = client.post("/api/admin/reviews/import?dry_run=false", headers=HEADERS, json=edited)
    assert got.json()["applied"] == 3
    assert _statuses(session)["a"][:2] == ("approved", date(2026, 10, 20))


def test_import_does_not_guess_when_fallback_is_ambiguous(client, session, reviewed):
    body = client.get("/api/admin/reviews/export", headers=HEADERS).json()
    _reset_to_pending(session)
    # 同一文件里再有一条类型、日期都相同的候选（证据句不同）：分不清就不导入
    _candidate(session, reviewed.notice, "g" * 64, "企业申报截止", date(2026, 10, 18), "另一处")
    session.commit()
    edited = copy.deepcopy(body)
    item = next(i for i in edited["items"] if i["status"] == "approved")
    item["fingerprint"] = "f" * 64
    got = client.post(
        "/api/admin/reviews/import?dry_run=false", headers=HEADERS, json={**edited, "items": [item]}
    ).json()
    assert got["applied"] == 0 and got["missingCandidate"] == 1
    assert "分不清" in got["problems"][0]["reason"]
    assert _statuses(session)["a"][0] == "pending"


def test_import_rejects_impossible_time_with_422(client, session, reviewed):
    body = client.get("/api/admin/reviews/export", headers=HEADERS).json()
    # 只收 HH:MM：不存在的时间、带秒、带时区、不补零都不收（带秒的存进去再导出会变，比对不上）
    for value in ("99:99", "10:30:15", "10:30+08:00", "9:30", "1030"):
        bad = copy.deepcopy(body)
        next(i for i in bad["items"] if i["status"] == "approved")["approvedTime"] = value
        response = client.post("/api/admin/reviews/import", headers=HEADERS, json=bad)
        assert response.status_code == 422, value


def test_import_warns_when_here_approved_but_export_says_changed(client, session, reviewed):
    body = client.get("/api/admin/reviews/export", headers=HEADERS).json()
    _reset_to_pending(session)
    # 这边已经把"材料报送截止"单独核对为通过，但导出记录说它已延期、变更通知这边还没有
    row = session.query(DocumentDeadline).filter_by(fingerprint="b" * 64).one()
    row.status, row.reviewed_by, row.reviewed_at = "approved", "另一核对人", datetime(2026, 9, 29)
    row.approved_date = date(2026, 10, 25)
    session.commit()
    edited = copy.deepcopy(body)
    item = next(i for i in edited["items"] if i["status"] == "changed")
    item["change"]["toUrlKey"] = "kw.beijing.gov.cn/not-yet.html"
    got = client.post(
        "/api/admin/reviews/import?dry_run=false", headers=HEADERS, json={**edited, "items": [item]}
    ).json()
    assert got["missingTarget"] == 1
    assert "这边已通过" in got["problems"][0]["reason"]
