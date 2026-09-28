"""公开导出只包含归并后的题录、原文网址和人工核对字段。"""

import json
from collections import deque
from datetime import UTC, date, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from youju.api import exports
from youju.api.app import app
from youju.api.exports import MAX_BYTES
from youju.db import get_session
from youju.models import Document, DocumentDeadline, DocumentDetail, DocumentMerge, Source


@pytest.fixture
def client(session_factory):
    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def docs(session):
    source = Source(
        level="省级",
        province="辽宁",
        dept="工信",
        dept_full_name="辽宁省工业和信息化厅",
        column_name="通知",
        url="https://gxt.ln.gov.cn/tz/",
        enabled=True,
        parser_config={},
        verified_on=date(2026, 9, 25),
    )
    session.add(source)
    session.flush()

    def add_doc(number, title, day=date(2026, 9, 24)):
        now = datetime.now(UTC)
        doc = Document(
            source_id=source.id,
            url=f"https://gxt.ln.gov.cn/{number}.html",
            url_key=f"gxt.ln.gov.cn/{number}.html",
            title=title,
            publish_date=day,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(doc)
        session.flush()
        return doc

    title = "辽宁省关于组织申报科技项目的通知"
    original = add_doc(1, title)
    duplicate = add_doc(2, title)
    listed = add_doc(3, "辽宁省科技型中小企业拟入库名单公示")
    changed_to = add_doc(4, "关于延长科技项目申报时间的通知", date(2026, 9, 26))
    session.add(
        DocumentMerge(
            duplicate_id=duplicate.id, canonical_id=original.id, active=True, match_key="test"
        )
    )
    session.add(
        DocumentDetail(
            document_id=duplicate.id,
            body_text="",
            issuer="辽宁省工业和信息化厅",
            doc_number="辽工信〔2026〕1号",
            fetched_at=datetime.now(UTC),
            status="ok",
        )
    )
    reviewed = datetime(2026, 9, 26, 3, tzinfo=UTC)

    def deadline(doc, fingerprint, day, status, kind="企业申报截止", **extra):
        return DocumentDeadline(
            document_id=doc.id,
            fingerprint=fingerprint,
            kind=kind,
            candidate_date=day,
            evidence="核对记录",
            confidence="高",
            status=status,
            approved_date=day if status != "pending" else None,
            reviewed_at=reviewed if status != "pending" else None,
            **extra,
        )

    session.add_all(
        [
            deadline(
                original,
                "a",
                date(2026, 10, 18),
                "changed",
                change_reason="延期",
                changed_to_document_id=changed_to.id,
                changed_at=reviewed,
            ),
            deadline(duplicate, "b", date(2026, 10, 18), "approved"),
            deadline(duplicate, "c", date(2026, 10, 20), "approved", kind="推荐或审核截止"),
            deadline(listed, "d", date(2026, 10, 5), "pending"),
        ]
    )
    session.commit()
    return original.id, duplicate.id, listed.id


def _export(client, docs, format):
    original, duplicate, listed = docs
    response = client.post(
        "/api/v1/exports",
        json={"ids": [str(duplicate), str(original), str(listed)], "format": format},
    )
    assert response.status_code == 200
    assert len(response.content) < MAX_BYTES
    assert (
        "filename*=UTF-8''%E6%9C%89%E6%8D%AE%E5%AF%BC%E5%87%BA_"
        in response.headers["content-disposition"]
    )
    return response


def test_xlsx_export(client, docs):
    response = _export(client, docs, "xlsx")
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    book = load_workbook(BytesIO(response.content))
    assert book.sheetnames == ["文件清单", "数据说明"]
    sheet = book["文件清单"]
    assert sheet.max_row == 3  # 两个原文网址归并为一行，请求顺序仍保留
    assert [sheet.cell(1, i).value for i in range(1, 10)] == [
        "序号",
        "标题",
        "类型",
        "名单状态",
        "发文机关",
        "文号",
        "发布日期",
        "地区",
        "截止日期",
    ]
    assert sheet["J1"].value == "原文链接" and sheet["K1"].value == "原文链接 2"
    assert sheet["B2"].value == "辽宁省关于组织申报科技项目的通知"
    assert sheet["D3"].value == "公示"
    assert "2026-10-20" in sheet["I2"].value
    assert "原定 企业申报截止 2026-10-18 已延期" in sheet["I2"].value
    assert "2026-10-05" not in sheet["I3"].value and sheet["I3"].value == "见原文"
    assert sheet["J2"].hyperlink.target.endswith("/1.html")
    assert sheet["K2"].hyperlink.target.endswith("/2.html")
    note = book["数据说明"]
    assert "第三方整理，非政府平台" in note["B1"].value
    assert note["D4"].value == "2026-09-25"
    assert note["E4"].value == "2026-09-26"


def test_docx_export(client, docs):
    response = _export(client, docs, "docx")
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    book = WordDocument(BytesIO(response.content))
    headings = [p.text for p in book.paragraphs if p.style.name == "Heading 1"]
    assert len(headings) == 2
    assert headings[0].startswith("1. 辽宁省关于组织申报")
    assert "第三方整理，非政府平台" in book.paragraphs[1].text
    text = "\n".join(p.text for p in book.paragraphs)
    assert "https://gxt.ln.gov.cn/1.html" in text
    assert "https://gxt.ln.gov.cn/2.html" in text
    assert len(book.tables) == 3
    assert len(book.tables[0].rows) == 3
    assert book.tables[0].rows[1].cells[4].text == "2026-09-26"
    fields = {row.cells[0].text: row.cells[1].text for row in book.tables[1].rows}
    assert "2026-10-20" in fields["截止日期"]
    assert "原定 企业申报截止 2026-10-18 已延期" in fields["截止日期"]
    assert fields["截止日期核对日期"] == "2026-09-26"
    listed_fields = {row.cells[0].text: row.cells[1].text for row in book.tables[2].rows}
    assert listed_fields["截止日期"] == "见原文"


def test_export_rejects_missing_ids_and_over_200(client, docs):
    original, _, _ = docs
    missing = client.post(
        "/api/v1/exports", json={"ids": [str(original), "999999", "bad"], "format": "xlsx"}
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["不存在的文件编号"] == ["999999", "bad"]
    too_many = client.post("/api/v1/exports", json={"ids": [str(original)] * 201, "format": "docx"})
    assert too_many.status_code == 422


def test_export_rejects_file_at_size_limit(client, docs, monkeypatch):
    monkeypatch.setattr(exports, "_xlsx", lambda items, stamp: b"x" * MAX_BYTES)
    response = client.post("/api/v1/exports", json={"ids": [str(docs[0])], "format": "xlsx"})
    assert response.status_code == 413


def test_restricted_sources_are_not_exported_or_listed(client, docs, session):
    """只有 2 级来源（只存题录）、没有 1 级副本的文件：搜索、详情、导出都当作没有。"""
    restricted = Source(
        level="省级",
        province="辽宁",
        dept="工信",
        dept_full_name="某授权平台",
        column_name="资讯",
        url="https://example.gov.cn/zx/",
        enabled=True,
        parser_config={},
        rights_level=2,
    )
    session.add(restricted)
    session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=restricted.id,
        url="https://example.gov.cn/zx/1.html",
        url_key="example.gov.cn/zx/1.html",
        title="辽宁省关于组织申报智能制造项目的通知",
        publish_date=date(2026, 9, 24),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.commit()
    exported = client.post("/api/v1/exports", json={"ids": [str(doc.id)], "format": "xlsx"})
    assert exported.status_code == 422
    assert exported.json()["detail"]["不存在的文件编号"] == [str(doc.id)]
    assert client.get(f"/api/v1/documents/{doc.id}").status_code == 404
    found = client.get("/api/v1/search", params={"q": "智能制造", "time": "不限"}).json()
    assert found["total"] == 0


def test_exports_are_rate_limited_per_user(client, docs, monkeypatch):
    from youju.api import exports

    original, _, _ = docs
    monkeypatch.setattr(exports, "get_settings", lambda: SimpleNamespace(exports_per_10min=2))
    monkeypatch.setattr(
        "youju.api.me.get_settings",
        lambda: SimpleNamespace(trust_wx_headers=True, dev_openid=None),
    )
    body = {"ids": [str(original)], "format": "xlsx"}

    def post(openid):
        headers = {"X-WX-OPENID": openid, "X-WX-SOURCE": "wx"}
        return client.post("/api/v1/exports", json=body, headers=headers).status_code

    assert [post("o-a"), post("o-a"), post("o-a")] == [200, 200, 429]
    assert post("o-b") == 200  # 别的用户不受影响


def test_export_log_never_grows_without_bound(monkeypatch):
    from youju.api import exports

    monkeypatch.setattr(exports, "get_settings", lambda: SimpleNamespace(exports_per_10min=5))
    now = 1000.0
    for n in range(10001):  # 一万多个同时活跃的来源（异常流量）
        exports._export_times[f"u:{n}"] = deque([now])
    assert exports._allow_export("u:new", now + 1)
    assert len(exports._export_times) == 1  # 超上限后整个清空，只剩这次的


def test_home_and_calendar_skip_restricted_sources(client, session, monkeypatch):
    """首页的本周计数、最新文件和日历都不算只有 2 级来源的文件。"""
    from youju.api import v1

    monkeypatch.setattr(v1, "now", lambda: datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    restricted = Source(
        level="省级",
        province="辽宁",
        dept="工信",
        dept_full_name="某授权平台",
        column_name="资讯",
        url="https://example.gov.cn/zx/",
        enabled=True,
        parser_config={},
        rights_level=2,
    )
    session.add(restricted)
    session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=restricted.id,
        url="https://example.gov.cn/zx/9.html",
        url_key="example.gov.cn/zx/9.html",
        title="辽宁省关于组织申报智能制造项目的通知",
        publish_date=date(2026, 9, 29),
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDeadline(
            document_id=doc.id,
            fingerprint="r" * 64,
            kind="企业申报截止",
            candidate_date=date(2026, 10, 10),
            approved_date=date(2026, 10, 10),
            evidence="请于10月10日前申报",
            confidence="高",
            status="approved",
            reviewed_by="审阅人",
            reviewed_at=now,
        )
    )
    session.commit()
    home = client.get("/api/v1/home").json()
    assert home["stats"]["week_total"] == 0
    assert all(item["id"] != str(doc.id) for item in home["items"])
    calendar = json.dumps(client.get("/api/v1/calendar").json(), ensure_ascii=False)
    assert "智能制造" not in calendar
