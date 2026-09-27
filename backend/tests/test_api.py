from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentAttachment, DocumentDetail, Source


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
    assert result["body_text"] == "公开通知正文。"
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
