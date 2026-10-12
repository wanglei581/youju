from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

from youju.api.app import app
from youju.db import get_session
from youju.dedup import reconcile_documents
from youju.extract.service import extract_existing
from youju.models import (
    Document,
    DocumentAttachment,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    Feedback,
    ListEntry,
    Source,
    User,
)


@pytest.fixture
def client(session_factory):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def seeded(session):
    now = datetime.now(UTC)
    bj = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url="https://kw.beijing.gov.cn/zwgk/tzgg/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        health="ok",
    )
    sd = Source(
        level="省级",
        province="山东",
        dept="工信",
        dept_full_name="山东省工业和信息化厅",
        column_name="通知",
        url="http://gxt.shandong.gov.cn/col/col15201/index.html",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        health="failing",
    )
    session.add_all([bj, sd])
    session.flush()
    session.add_all(
        [
            Document(
                source_id=bj.id,
                url="https://kw.beijing.gov.cn/a/1.html",
                url_key="kw.beijing.gov.cn/a/1.html",
                title="关于开展2026年度高新技术企业认定工作的通知",
                publish_date=date(2026, 9, 20),
                first_seen_at=now,
                last_seen_at=now,
                is_baseline=True,
            ),
            Document(
                source_id=bj.id,
                url="https://kw.beijing.gov.cn/a/2.html",
                url_key="kw.beijing.gov.cn/a/2.html",
                title="100%_特殊字符标题测试文件",
                publish_date=date(2026, 9, 10),
                first_seen_at=now,
                last_seen_at=now,
                is_baseline=True,
            ),
            Document(
                source_id=sd.id,
                url="http://gxt.shandong.gov.cn/a/3.html",
                url_key="gxt.shandong.gov.cn/a/3.html",
                title="山东省专精特新中小企业认定名单公示",
                publish_date=date(2026, 9, 25),
                first_seen_at=now,
                last_seen_at=now,
                is_baseline=False,
            ),
        ]
    )
    session.commit()


def test_health(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_content_merge_keeps_raw_urls_and_is_reversible(client, seeded, session) -> None:
    original = session.query(Document).filter(Document.title.like("关于开展2026年度%")).one()
    duplicate = Document(
        source_id=original.source_id,
        url=original.url + "?type=2",
        url_key=original.url_key + "?type=2",
        title=original.title,
        publish_date=original.publish_date,
        first_seen_at=original.first_seen_at,
        last_seen_at=original.last_seen_at,
    )
    session.add(duplicate)
    session.flush()
    session.add(
        DocumentDeadline(
            document_id=duplicate.id,
            fingerprint="duplicate-deadline",
            kind="企业申报截止",
            candidate_date=date(2026, 10, 30),
            approved_date=date(2026, 10, 30),
            evidence="企业申报截止2026年10月30日",
            confidence="高",
            status="approved",
        )
    )
    assert reconcile_documents(session) == 1
    session.commit()
    assert client.get("/api/documents").json()["total"] == 3
    assert client.get("/api/v1/search", params={"time": "不限"}).json()["total"] == 3
    detail = client.get(f"/api/documents/{duplicate.id}").json()
    assert detail["id"] == original.id
    assert detail["original_urls"] == [original.url, duplicate.url]
    assert len(detail["deadlines"]) == 1
    assert session.get(Document, duplicate.id) is not None
    mapping = session.get(DocumentMerge, duplicate.id)
    mapping.active = False
    session.commit()
    assert reconcile_documents(session) == 0
    session.commit()
    assert client.get("/api/documents").json()["total"] == 4


def test_merged_list_entries_only_appear_once(client, seeded, session) -> None:
    original = session.query(Document).filter(Document.title.like("山东省专精特新%")).one()
    duplicate = Document(
        source_id=original.source_id,
        url=original.url + "?type=2",
        url_key=original.url_key + "?type=2",
        title=original.title,
        publish_date=original.publish_date,
        first_seen_at=original.first_seen_at,
        last_seen_at=original.last_seen_at,
    )
    session.add(duplicate)
    session.flush()
    for doc in (original, duplicate):
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=doc.url,
                name="山东星河科技有限公司",
                normalized_name="山东星河科技有限公司",
                category="专精特新中小企业",
            )
        )
    reconcile_documents(session)
    session.commit()
    result = client.get("/api/lists/search", params={"q": "山东星河科技有限公司"}).json()
    assert len(result["exact_matches"]) == 1
    assert result["exact_matches"][0]["original_url"] == original.url


def test_list_search_separates_exact_and_similar(client, seeded, session) -> None:
    doc = session.query(Document).filter(Document.title.like("山东省专精特新%")).one()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="名单",
            issuer="山东省工业和信息化厅",
            fetched_at=datetime.now(UTC),
            status="ok",
        )
    )
    session.add_all(
        [
            ListEntry(
                document_id=doc.id,
                source_url="https://gxt.shandong.gov.cn/a/list.xlsx",
                name="山东星河科技有限公司",
                normalized_name="山东星河科技有限公司",
                credit_code="91310117588657256C",
                sequence="1",
                category="专精特新中小企业",
                batch="第4批",
                region="济南市",
            ),
            ListEntry(
                document_id=doc.id,
                source_url="https://gxt.shandong.gov.cn/a/list2.xlsx",
                name="山东星河科技（青岛）有限公司",
                normalized_name="山东星河科技(青岛)有限公司",
                sequence="2",
                category="专精特新中小企业",
                batch="第4批",
            ),
        ]
    )
    session.commit()
    result = client.get("/api/lists/search", params={"q": "山东星河科技有限公司"})
    assert result.status_code == 200
    body = result.json()
    assert len(body["exact_matches"]) == 1
    assert body["exact_matches"][0]["issuer"] == "山东省工业和信息化厅"
    assert body["exact_matches"][0]["original_url"] == doc.url
    assert body["exact_matches"][0]["status"] == "公示"
    assert len(body["similar_candidates"]) == 1
    assert body["similar_candidates"][0]["name"] == "山东星河科技（青岛）有限公司"
    by_code = client.get("/api/lists/search", params={"q": "91310117588657256C"}).json()
    assert len(by_code["exact_matches"]) == 1
    assert by_code["similar_candidates"] == []
    refused = client.get("/api/lists/search", params={"q": "张三"})
    assert refused.status_code == 422
    assert refused.json()["detail"] == "只查企业和机构"
    session.add_all(
        [
            ListEntry(
                document_id=doc.id,
                source_url="https://gxt.shandong.gov.cn/a/short.xlsx",
                name="星河有限公司",
                normalized_name="星河有限公司",
                category="其他",
            ),
            ListEntry(
                document_id=doc.id,
                source_url="https://gxt.shandong.gov.cn/a/short2.xlsx",
                name="星河（青岛）有限公司",
                normalized_name="星河(青岛)有限公司",
                category="其他",
            ),
        ]
    )
    session.commit()
    short = client.get("/api/lists/search", params={"q": "星河有限公司"}).json()
    assert [item["name"] for item in short["exact_matches"]] == ["星河有限公司"]
    assert [item["name"] for item in short["similar_candidates"]] == ["星河（青岛）有限公司"]
    doc.source.rights_level = 2
    session.commit()
    assert (
        client.get("/api/lists/search", params={"q": "山东星河科技有限公司"}).json()[
            "exact_matches"
        ]
        == []
    )


def test_sources_filter(client, seeded) -> None:
    assert len(client.get("/api/sources").json()) == 2
    failing = client.get("/api/sources", params={"health": "failing"}).json()
    assert [s["province"] for s in failing] == ["山东"]


def test_documents_search_and_filters(client, seeded) -> None:
    body = client.get("/api/documents").json()
    assert body["total"] == 3
    assert body["items"][0]["title"].startswith("山东省")  # 按发布日期倒序

    assert client.get("/api/documents", params={"q": "高新技术"}).json()["total"] == 1
    assert client.get("/api/documents", params={"province": "北京"}).json()["total"] == 2
    assert client.get("/api/documents", params={"since": "2026-09-15"}).json()["total"] == 2
    assert client.get("/api/documents", params={"new_only": True}).json()["total"] == 1
    # % 和 _ 按普通字符匹配，不当作通配符
    assert client.get("/api/documents", params={"q": "100%_"}).json()["total"] == 1
    assert client.get("/api/documents", params={"q": "%_"}).json()["total"] == 1
    # 关键词至少 2 个字
    assert client.get("/api/documents", params={"q": "税"}).status_code == 422


def test_document_detail_and_404(client, seeded) -> None:
    first = client.get("/api/documents").json()["items"][0]
    detail = client.get(f"/api/documents/{first['id']}").json()
    assert detail["url"] == first["url"]
    assert detail["detail"] is None
    assert client.get("/api/documents/999999").status_code == 404


def test_document_detail_includes_parsed_content(client, seeded, session) -> None:
    doc = session.query(Document).filter(Document.title.like("关于开展%")).one()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="公开通知正文。",
            issuer="北京市科学技术委员会",
            doc_number="京科〔2026〕1号",
            publish_date=date(2026, 9, 20),
            fetched_at=datetime.now(UTC),
            http_status=200,
            status="ok",
            attachments=[
                DocumentAttachment(
                    position=0,
                    name="附件.pdf",
                    url="https://kw.beijing.gov.cn/a/1.pdf",
                    extension="pdf",
                )
            ],
        )
    )
    session.commit()
    result = client.get(f"/api/documents/{doc.id}").json()["detail"]
    assert "body_text" not in result  # 公开接口不返回正文全文，只给核对过的字段和原文链接
    assert result["issuer"] == "北京市科学技术委员会"
    assert result["doc_number"] == "京科〔2026〕1号"
    assert result["attachments"][0]["extension"] == "pdf"


def test_stats(client, seeded) -> None:
    stats = client.get("/api/stats").json()
    assert stats["sources_total"] == 2
    assert stats["sources_by_health"] == {"ok": 1, "failing": 1}
    assert stats["documents_total"] == 3
    assert stats["documents_new_7d"] == 1
    assert stats["documents_by_province"] == {"北京": 2, "山东": 1}


def test_deadline_admin_permission_and_review_gate(client, seeded, session, monkeypatch) -> None:
    doc = session.query(Document).filter(Document.title.like("关于开展%")).one()
    session.add_all(
        [
            DocumentDeadline(
                document_id=doc.id,
                fingerprint="one",
                kind="企业申报截止",
                candidate_date=date(2026, 10, 1),
                evidence="企业申报截止10月1日",
                confidence="高",
                status="pending",
            ),
            DocumentDeadline(
                document_id=doc.id,
                fingerprint="two",
                kind="材料报送截止",
                candidate_date=date(2026, 10, 2),
                evidence="材料报送截止10月2日",
                confidence="高",
                status="pending",
            ),
        ]
    )
    session.commit()
    rows = session.query(DocumentDeadline).order_by(DocumentDeadline.id).all()
    url = f"/api/documents/{doc.id}"
    assert client.get(url).json()["deadline_text"] == "截止时间见原文"
    admin_url = "/api/admin/deadlines"
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token=None))
    assert client.get(admin_url, headers={"X-Admin-Token": "anything"}).status_code == 401
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    assert client.get(admin_url).status_code == 401
    assert client.get(admin_url, headers={"X-Admin-Token": "wrong"}).status_code == 401
    headers = {"X-Admin-Token": "secret"}
    pending = client.get(admin_url, headers=headers).json()
    # 按候选编号从新到旧
    assert [p["id"] for p in pending] == [rows[1].id, rows[0].id]
    assert pending[1]["evidence"] == "企业申报截止10月1日"
    assert pending[1]["url"] == doc.url
    # 翻页用上一页最小的编号作 before_id；别人先核对了第一页的条目，第二页也不漏
    first = client.get(admin_url, headers=headers, params={"limit": 1}).json()
    assert [p["id"] for p in first] == [rows[1].id]
    rows[1].status = "rejected"
    session.commit()
    second = client.get(admin_url, headers=headers, params={"limit": 1, "before_id": rows[1].id})
    assert [p["id"] for p in second.json()] == [rows[0].id]
    rows[1].status = "pending"
    session.commit()
    approve = f"{admin_url}/{rows[0].id}/approve"
    assert client.post(approve, json={"reviewed_by": "审阅人"}).status_code == 401
    for value in ("17:00:30", "25:00", "5:00"):
        bad = {"reviewed_by": "审阅人", "date": "2026-10-05", "time": value}
        assert client.post(approve, headers=headers, json=bad).status_code == 422, value
    assert (
        client.post(
            approve,
            headers=headers,
            json={
                "reviewed_by": "审阅人",
                "date": "2026-10-05",
                "time": "17:00",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(approve, headers=headers, json={"reviewed_by": "另一个人"}).status_code == 409
    )
    public = client.get(url).json()
    assert public["deadlines"] == [{"kind": "企业申报截止", "date": "2026-10-05", "time": "17:00"}]
    assert "材料报送" not in public["deadline_text"]
    reject = f"{admin_url}/{rows[1].id}/reject"
    assert client.post(reject, headers=headers, json={"reviewed_by": "审阅人"}).status_code == 200
    assert client.get(admin_url, headers=headers).json() == []
    session.refresh(rows[0])
    session.refresh(rows[1])
    assert rows[0].reviewed_at is not None and rows[0].reviewed_by == "审阅人"
    assert rows[1].status == "rejected"


def test_extraction_idempotent_after_review(session, seeded) -> None:
    doc = session.query(Document).filter(Document.title.like("关于开展%")).one()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="企业申报截止2026年10月1日。",
            fetched_at=datetime.now(UTC),
            status="ok",
        )
    )
    session.commit()
    assert extract_existing(session, since=date(2026, 7, 1)) == (1, 1)
    row = session.query(DocumentDeadline).one()
    row.status = "approved"
    row.reviewed_by = "审阅人"
    row.reviewed_at = datetime.now(UTC)
    row.approved_date = date(2026, 10, 5)
    session.commit()
    assert extract_existing(session, since=date(2026, 7, 1)) == (1, 0)
    session.refresh(row)
    assert row.approved_date == date(2026, 10, 5)
    # 证据句截法变了（指纹不同），同一日期已核对过，不再重复送审
    row.fingerprint = "按旧规则截取的证据句"
    session.commit()
    assert extract_existing(session, since=date(2026, 7, 1)) == (1, 0)
    assert session.query(DocumentDeadline).count() == 1
    # 同一天的另一类截止（材料报送）照常送审
    detail = session.get(DocumentDetail, doc.id)
    detail.body_text = "企业申报截止2026年10月1日。材料报送截止2026年10月1日。"
    session.commit()
    assert extract_existing(session, since=date(2026, 7, 1)) == (1, 1)
    kinds = sorted(d.kind for d in session.query(DocumentDeadline))
    assert kinds == ["企业申报截止", "材料报送截止"]
    # 类型判断变了（旧规则当时判成别的类型）：按新类型重新送审，核对人可以纠正
    row.kind = "推荐或审核截止"
    session.commit()
    before = session.query(DocumentDeadline).count()
    extract_existing(session, since=date(2026, 7, 1))
    pending = session.query(DocumentDeadline).filter_by(status="pending", kind="企业申报截止")
    assert session.query(DocumentDeadline).count() == before + 1 and pending.count() == 1


def test_review_console_page_and_pending_count(client, seeded, session, monkeypatch) -> None:
    doc = session.query(Document).filter(Document.title.contains("高新技术企业认定")).one()
    session.add(
        DocumentDeadline(
            document_id=doc.id,
            fingerprint="console",
            kind="企业申报截止",
            candidate_date=date(2026, 10, 18),
            evidence="申报截止时间为10月18日",
            confidence="高",
            status="pending",
        )
    )
    session.commit()
    # 核对台页面本身不含口令，任何人都能打开；数据接口要口令
    page = client.get("/admin/review")
    assert page.status_code == 200 and "截止日期核对台" in page.text
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    assert client.get("/api/admin/deadlines/count").status_code == 401
    headers = {"X-Admin-Token": "secret"}
    assert client.get("/api/admin/deadlines/count", headers=headers).json() == {"pending": 1}
    item = client.get("/api/admin/deadlines", headers=headers).json()[0]
    assert item["source"] == "北京市科学技术委员会" and item["publish_date"] == "2026-09-20"


def test_feedback_migration(db_engine) -> None:
    column = next(
        c for c in inspect(db_engine).get_columns("feedback") if c["name"] == "handled_at"
    )
    assert column["nullable"] is True
    # MySQL 的 DATETIME 不带时区：统一按 UTC 存、保留微秒（见 youju/dbtypes.py）
    assert isinstance(column["type"], mysql.DATETIME)
    assert column["type"].fsp == 6


def test_feedback_admin_listing_and_actions(client, seeded, session, monkeypatch, caplog) -> None:
    import logging

    from youju.api.app import audit_log

    master = session.query(Document).filter(Document.title.contains("高新技术企业认定")).one()
    duplicate = Document(
        source_id=master.source_id,
        url=master.url + "?duplicate=1",
        url_key=master.url_key + "?duplicate=1",
        title="副本标题",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    deleted = Document(
        source_id=master.source_id,
        url=master.url + "?deleted=1",
        url_key=master.url_key + "?deleted=1",
        title="待删除文件",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    user = User(openid="secret-openid")
    session.add_all([duplicate, deleted, user])
    session.flush()
    session.add(
        DocumentMerge(
            duplicate_id=duplicate.id, canonical_id=master.id, active=True, match_key="data"
        )
    )
    rows = [
        Feedback(
            user_id=user.id, document_id=duplicate.id, content="标题有误", check_result="pass"
        ),
        Feedback(
            user_id=user.id, document_id=deleted.id, content="原文已下线", check_result="unchecked"
        ),
        Feedback(user_id=user.id, content="申请覆盖：增加广东", check_result="pass"),
    ]
    session.add_all(rows)
    session.commit()
    session.delete(deleted)
    session.commit()

    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    base = "/api/admin/feedback"
    assert client.get(base).status_code == 401
    assert client.get(base + "/count").status_code == 401
    assert client.post(f"{base}/{rows[0].id}/handle").status_code == 401
    assert client.post(f"{base}/{rows[0].id}/unhandle").status_code == 401
    headers = {"X-Admin-Token": "secret"}
    assert client.get(base + "/count", headers=headers).json() == {"pending": 3}
    first = client.get(base, headers=headers, params={"limit": 1}).json()
    assert [item["id"] for item in first] == [rows[2].id]
    assert first[0]["kind"] == "申请覆盖"
    assert first[0]["docId"] is None and first[0]["docTitle"] is None
    assert first[0]["docUrl"] is None and first[0]["handledAt"] is None
    assert first[0]["createdAt"]
    assert "openid" not in str(first) and "user_id" not in str(first)
    second = client.get(base, headers=headers, params={"limit": 1, "before_id": rows[2].id}).json()
    assert [item["id"] for item in second] == [rows[1].id]
    assert second[0]["docId"] is None and second[0]["docTitle"] is None
    assert second[0]["checkResult"] == "unchecked"
    third = client.get(base, headers=headers, params={"before_id": rows[1].id}).json()
    assert [item["id"] for item in third] == [rows[0].id]
    assert third[0]["kind"] == "纠错"
    assert (third[0]["docId"], third[0]["docTitle"], third[0]["docUrl"]) == (
        master.id,
        master.title,
        master.url,
    )
    assert client.get(base, headers=headers, params={"status": "bad"}).status_code == 422
    assert client.get(base, headers=headers, params={"limit": 101}).status_code == 422

    url = f"{base}/{rows[0].id}"
    audit_log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="youju.audit"):
            assert client.post(url + "/handle", headers=headers).json() == {
                "handled": True,
                "changed": True,
            }
            assert client.post(url + "/handle", headers=headers).json() == {
                "handled": True,
                "changed": False,
            }
    finally:
        audit_log.removeHandler(caplog.handler)
    assert "feedback_handle" in caplog.text
    session.refresh(rows[0])
    assert rows[0].handled_at is not None
    assert client.get(base + "/count", headers=headers).json() == {"pending": 2}
    assert [item["id"] for item in client.get(base, headers=headers).json()] == [
        rows[2].id,
        rows[1].id,
    ]
    all_items = client.get(base, headers=headers, params={"status": "all"}).json()
    assert [item["id"] for item in all_items] == [rows[2].id, rows[1].id, rows[0].id]
    assert all("openid" not in item and "user_id" not in item for item in all_items)
    assert all_items[-1]["handledAt"] is not None
    assert client.post(url + "/unhandle", headers=headers).json() == {
        "handled": False,
        "changed": True,
    }
    assert client.post(url + "/unhandle", headers=headers).json() == {
        "handled": False,
        "changed": False,
    }
    assert client.get(base + "/count", headers=headers).json() == {"pending": 3}
    assert client.post(f"{base}/99999/handle", headers=headers).status_code == 404
    assert client.post(f"{base}/99999/unhandle", headers=headers).status_code == 404


def test_feedback_console_page(monkeypatch) -> None:
    settings = {"role": "all", "admin_token": "secret"}
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(**settings))
    client = TestClient(app)
    page = client.get("/admin/feedback")
    assert page.status_code == 200 and "申请覆盖" in page.text
    assert "secret" not in page.text
    assert "/admin/review" in page.text
    assert "/admin/feedback" in client.get("/admin/review").text
    settings["role"] = "api"
    assert client.get("/admin/feedback").status_code == 404
    assert client.get("/api/admin/feedback/count").status_code == 404
    settings["role"] = "admin"
    assert client.get("/admin/feedback").status_code == 200


def test_review_can_clear_time_explicitly(client, seeded, session, monkeypatch) -> None:
    """改日期时把时间清空（time 传 null）：核定结果不能沿用候选时间。"""
    from datetime import time

    doc = session.query(Document).filter(Document.title.contains("高新技术企业认定")).one()
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint="clear-time",
        kind="企业申报截止",
        candidate_date=date(2026, 10, 18),
        candidate_time=time(17, 0),
        evidence="10月18日17:00前",
        confidence="高",
        status="pending",
    )
    session.add(row)
    session.commit()
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    headers = {"X-Admin-Token": "secret"}
    body = {"reviewed_by": "测试", "date": "2026-10-18", "time": None}
    assert (
        client.post(
            f"/api/admin/deadlines/{row.id}/approve", json=body, headers=headers
        ).status_code
        == 200
    )
    session.refresh(row)
    assert row.approved_time is None
    # 已核对的再提交：409
    again = client.post(f"/api/admin/deadlines/{row.id}/approve", json=body, headers=headers)
    assert again.status_code == 409


def test_pending_duplicates_in_merge_group_are_shown_and_reviewed_once(
    client, seeded, session, monkeypatch
) -> None:
    """同一通知的两个网址归并后，各自抽出的相同候选只列一条、计一条，核对一次两条都处理。"""
    from youju.models import DocumentMerge

    doc = session.query(Document).filter(Document.title.contains("高新技术企业认定")).one()
    twin = Document(
        source_id=doc.source_id,
        url=doc.url + "?type=2",
        url_key=doc.url_key + "?type=2",
        title=doc.title,
        publish_date=doc.publish_date,
        first_seen_at=doc.first_seen_at,
        last_seen_at=doc.last_seen_at,
    )
    session.add(twin)
    session.flush()
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=doc.id, active=True, match_key="k")
    )
    rows = [
        DocumentDeadline(
            document_id=d.id,
            fingerprint=f"twin-{d.id}",
            kind="企业申报截止",
            candidate_date=date(2026, 10, 18),
            evidence="申报截止时间为10月18日",
            confidence="高",
            status="pending",
        )
        for d in (doc, twin)
    ]
    session.add_all(rows)
    session.commit()
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    headers = {"X-Admin-Token": "secret"}
    assert client.get("/api/admin/deadlines/count", headers=headers).json() == {"pending": 1}
    items = client.get("/api/admin/deadlines", headers=headers).json()
    assert len(items) == 1 and items[0]["document_id"] == doc.id  # 标题和链接用主文件
    approve = f"/api/admin/deadlines/{items[0]['id']}/approve"
    assert client.post(approve, json={"reviewed_by": "审阅人"}, headers=headers).status_code == 200
    assert client.get("/api/admin/deadlines/count", headers=headers).json() == {"pending": 0}
    for row in rows:
        session.refresh(row)
        assert row.status == "approved" and row.approved_date == date(2026, 10, 18)


def test_role_guard_and_admin_throttle(client, seeded, monkeypatch, caplog, tmp_path) -> None:
    """部署角色：api 不开放核对台；admin 只开放核对台、管理接口和健康检查；口令连续输错限流并留痕。"""
    import logging
    import os

    from youju.api import app as app_module

    settings = {"role": "api", "admin_token": "t" * 32}
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(**settings))
    assert client.get("/admin/review").status_code == 404
    assert client.get("/api/admin/deadlines/count").status_code == 404
    assert client.get("/api/v1/home").status_code == 200
    # api 服务只开放 /api/v1/ 和健康检查：早期样机的接口不对外
    for old in ("/api/documents", "/api/documents/1", "/api/sources", "/api/stats"):
        assert client.get(old).status_code == 404, old
    assert client.get("/api/lists/search", params={"q": "科技有限公司"}).status_code == 404
    settings["role"] = "admin"
    assert client.get("/api/v1/home").status_code == 404
    assert client.get("/api/documents").status_code == 404
    assert client.get("/admin/review").status_code == 200
    # admin 的健康检查要求定时任务心跳：没有心跳报 503，15 分钟内更新过才算健康
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setattr(app_module, "CRON_HEARTBEAT", heartbeat)
    assert client.get("/health").status_code == 503
    heartbeat.write_text("1")
    assert client.get("/health").status_code == 200
    old = app_module.clock.time() - 3600
    os.utime(heartbeat, (old, old))
    assert client.get("/health").status_code == 503
    settings["role"] = "all"
    wrong = {"X-Admin-Token": "wrong", "X-Original-Forwarded-For": "203.0.113.9"}
    with caplog.at_level(logging.WARNING, logger="youju.audit"):
        for _ in range(5):
            assert client.get("/api/admin/deadlines/count", headers=wrong).status_code == 401
    assert "admin_auth_failed" in caplog.text
    # 同一来源第 6 次：口令对也先锁住；别的来源不受影响（不设全局锁定）
    right = {"X-Admin-Token": "t" * 32, "X-Original-Forwarded-For": "203.0.113.9"}
    assert client.get("/api/admin/deadlines/count", headers=right).status_code == 429
    # 平台若在请求方自带的值后面追加：按最后一项认来源，在前面伪造一项绕不过锁定
    spoofed = {"X-Admin-Token": "t" * 32, "X-Original-Forwarded-For": "192.0.2.77, 203.0.113.9"}
    assert client.get("/api/admin/deadlines/count", headers=spoofed).status_code == 429
    for i in range(40):
        spoof = {"X-Admin-Token": "wrong", "X-Original-Forwarded-For": f"198.51.100.{i}"}
        client.get("/api/admin/deadlines/count", headers=spoof)
    other = {"X-Admin-Token": "t" * 32, "X-Original-Forwarded-For": "192.0.2.1"}
    assert client.get("/api/admin/deadlines/count", headers=other).status_code == 200


def test_health_reports_db_error_type_and_code_only(monkeypatch):
    from fastapi.testclient import TestClient

    from youju.api import app as app_module
    from youju.db import get_session

    class _Orig(Exception):
        pass

    class _Session:
        def execute(self, *_a, **_k):
            err = Exception("wrapper with secret-host and user")
            err.orig = _Orig(1045, "Access denied for user 'x'@'10.0.0.1'")
            raise err

    app_module.app.dependency_overrides[get_session] = lambda: _Session()
    try:
        response = TestClient(app_module.app).get("/health")
    finally:
        app_module.app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 503
    assert response.json() == {"status": "db_error", "error": "_Orig", "code": 1045}
    assert "secret" not in response.text and "10.0.0.1" not in response.text


def test_snapshot_matches_endpoints_without_user_data(client, seeded, monkeypatch) -> None:
    """首屏快照（10/12 冷启动）：内容和各接口一样；调首页时不带请求头，认不出用户，不混进个人数据。"""
    from youju.api import me, v1

    v1._SNAPSHOT_CACHE.clear()
    v1._REPORT_CACHE.clear()
    seen = []
    real = me.known_user

    def spy(request, session):
        seen.append(dict(request.headers))
        return real(request, session)

    monkeypatch.setattr(me, "known_user", spy)
    wx = {"X-WX-SOURCE": "wx_client", "X-WX-OPENID": "o-snapshot"}
    snap = client.get("/api/v1/snapshot", headers=wx).json()
    assert seen == [{}]
    home = client.get("/api/v1/home", params={"group": 1, "excerpts": 1}).json()
    latest = client.get(
        "/api/v1/search", params={"kind": "申报通知", "limit": 8, "offset": 0, "group": 1}
    ).json()
    assert snap["home"]["stats"] == home["stats"]
    assert snap["home"]["dueSoon"] == home["dueSoon"]
    assert snap["home"]["dueSoonUnverifiedTotal"] == home["dueSoonUnverifiedTotal"]
    assert snap["home"]["items"] == latest["items"]
    assert (
        snap["deadlines"]
        == client.get(
            "/api/v1/reports/deadlines", params={"days": 7, "excerpts": 1, "page": 1, "size": 20}
        ).json()
    )
    assert (
        snap["lists"]
        == client.get("/api/v1/lists/recent", params={"limit": 20, "offset": 0, "group": 1}).json()
    )
    assert (
        snap["scope"]
        == client.get("/api/v1/reports/lists", params={"days": 30, "page": 1, "size": 1}).json()[
            "stats"
        ]
    )
    assert snap["generatedAt"][:10] == v1.now().date().isoformat()
    # 60 秒内再取是同一份（不重复算）
    assert client.get("/api/v1/snapshot", headers=wx).json() == snap
    assert len(seen) == 1 + 1  # 上面比对用的 /home 调过一次


def test_snapshot_on_admin_only_through_cloud_calls(client, seeded, monkeypatch) -> None:
    """admin 服务只放行经小程序云调用来的快照请求（带 X-WX-SOURCE）；公网直接访问和其他小程序接口仍是 404。"""
    from youju.api import app as app_module

    settings = {"role": "admin", "admin_token": "t" * 32}
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(**settings))
    assert client.get("/api/v1/snapshot").status_code == 404
    assert client.get("/api/v1/snapshot", headers={"X-WX-SOURCE": "wx_client"}).status_code == 200
    assert client.get("/api/v1/home", headers={"X-WX-SOURCE": "wx_client"}).status_code == 404
    assert client.get("/api/v1/search", headers={"X-WX-SOURCE": "wx_client"}).status_code == 404
    settings["role"] = "api"
    assert client.get("/api/v1/snapshot").status_code == 200
