from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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
    ListEntry,
    Source,
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
