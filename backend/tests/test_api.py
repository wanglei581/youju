from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from youju.api.app import app
from youju.db import get_session
from youju.extract.service import extract_existing
from youju.models import Document, DocumentAttachment, DocumentDeadline, DocumentDetail, Source


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
    assert len(pending) == 2
    assert len(client.get(admin_url, headers=headers, params={"limit": 1, "offset": 1}).json()) == 1
    assert pending[0]["evidence"] == "企业申报截止10月1日"
    assert pending[0]["url"] == doc.url
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
