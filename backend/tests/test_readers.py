"""原生阅读的真实 MySQL 授权、脱敏、分页和版本变化测试。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, text, update

from youju import attachment_text as texts
from youju.api import attachments as download_api
from youju.api import readers, v1
from youju.api.app import app
from youju.db import get_session
from youju.models import (
    AttachmentTextSegment,
    AttachmentTextUrl,
    AttachmentTextVersion,
    Document,
    DocumentAttachment,
    DocumentDetail,
    DocumentMerge,
    Source,
)

NOW = datetime(2026, 10, 23, tzinfo=UTC)


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(v1, "now", lambda: NOW)
    download_api._times.clear()
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    download_api._times.clear()


def seed(session, n=1, *, rights=1, kind="申报通知", body=None):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科技厅",
        column_name="通知",
        url=f"https://kjt.shandong.gov.cn/list{n}/",
        enabled=True,
        parser_config={},
        rights_level=rights,
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        url=f"https://kjt.shandong.gov.cn/{n}.html",
        url_key=f"kjt.shandong.gov.cn/{n}.html",
        title="关于开展科技计划项目申报的通知",
        kind_override=kind,
        publish_date=date(2026, 10, 22),
        first_seen_at=NOW,
        last_seen_at=NOW,
        status="fetched",
    )
    session.add(doc)
    session.flush()
    detail = DocumentDetail(
        document_id=doc.id,
        body_text=body if body is not None else "申报说明。" * 100,
        fetched_at=NOW,
        status="ok",
    )
    session.add(detail)
    att = DocumentAttachment(
        document_id=doc.id,
        position=0,
        name="申报材料.docx",
        url=f"https://kjt.shandong.gov.cn/{n}.docx",
        extension="docx",
    )
    session.add(att)
    version = AttachmentTextVersion(
        sha256=str(n) * 64,
        file_type="docx",
        size=100,
        status="ok",
        extractor=texts.EXTRACTOR_VERSION,
        segments=45,
        chars=450,
        extracted_at=NOW,
    )
    session.add(version)
    session.flush()
    url = AttachmentTextUrl(
        url_hash=texts.url_key(att.url),
        url=att.url,
        declared_type="docx",
        version_id=version.id,
        status="ok",
        extractor=texts.EXTRACTOR_VERSION,
        checked_at=NOW,
        changed_at=NOW,
    )
    session.add(url)
    session.add_all(
        [
            AttachmentTextSegment(version_id=version.id, n=i, kind="p", text=f"第{i}段申报说明")
            for i in range(1, 46)
        ]
    )
    session.commit()
    return doc, att, url, version


def path(doc, att=None):
    return f"/api/v1/documents/{doc.id}" + (f"/attachments/{att.id}" if att else "") + "/reading"


def test_attachment_pages_real_db_no_duplicates(client, session):
    doc, att, _, _ = seed(session)
    seen = []
    params = {}
    while True:
        response = client.get(path(doc, att), params=params)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["status"] == "ok" and data["totalSegments"] == 45
        seen.extend(s["n"] for s in data["segments"])
        if data["nextCursor"] is None:
            break
        params = {"cursor": data["nextCursor"], "revision": data["revision"]}
    assert seen == list(range(1, 46))


def test_body_longer_than_old_limit_and_version_change(client, session):
    body = "正文示例。" * 5000 + "\n\n最后一段。"
    doc, _, _, _ = seed(session, body=body)
    first = client.get(path(doc)).json()
    assert first["nextCursor"] is not None
    second = client.get(
        path(doc), params={"cursor": first["nextCursor"], "revision": first["revision"]}
    ).json()
    assert "".join(s["text"] for s in first["segments"] + second["segments"]) == body.replace(
        "\n\n", ""
    )
    session.get(DocumentDetail, doc.id).body_text += "修订内容"
    session.commit()
    changed = client.get(
        path(doc), params={"cursor": first["nextCursor"], "revision": first["revision"]}
    )
    assert changed.status_code == 409 and changed.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", ["sha", "reextract"])
def test_attachment_version_conflict_even_same_sha(client, session, change):
    doc, att, _, version = seed(session)
    first = client.get(path(doc, att)).json()
    if change == "sha":
        version.sha256 = "a" * 64
    else:
        version.extracted_at += timedelta(seconds=1)
    session.commit()
    response = client.get(
        path(doc, att), params={"cursor": first["nextCursor"], "revision": first["revision"]}
    )
    assert response.status_code == 409


@pytest.mark.parametrize("change", ["url", "version"])
def test_old_extractor_is_not_exposed(client, session, change):
    doc, att, url, version = seed(session)
    (url if change == "url" else version).extractor = texts.EXTRACTOR_VERSION - 1
    session.commit()
    data = client.get(path(doc, att)).json()
    assert data["status"] == "unavailable" and data["segments"] == []


@pytest.mark.parametrize("field", ["taken_down_at", "link_gone_at"])
def test_withdrawal_between_pages(client, session, field):
    doc, att, _, _ = seed(session)
    first = client.get(path(doc, att)).json()
    setattr(doc, field, NOW)
    session.commit()
    response = client.get(
        path(doc, att), params={"cursor": first["nextCursor"], "revision": first["revision"]}
    )
    assert response.status_code == (404 if field == "taken_down_at" else 200)
    assert not response.json().get("segments")
    assert response.headers["cache-control"] == "no-store"


def test_cross_document_attachment_and_private_source(client, session):
    doc, att, _, _ = seed(session)
    other, other_att, _, _ = seed(session, 2, rights=2)
    assert client.get(path(doc, other_att)).status_code == 404
    assert client.get(path(other, other_att)).status_code == 404
    assert client.get(path(other)).status_code == 404
    session.add(
        DocumentMerge(
            duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="reader-test"
        )
    )
    session.commit()
    assert client.get(path(doc, other_att)).status_code == 404
    assert client.get(path(doc, att)).status_code == 200


def test_merged_member_takedown_blocks_master(client, session):
    doc, att, _, _ = seed(session)
    other, _, _, _ = seed(session, 2)
    session.add(
        DocumentMerge(
            duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="reader-test"
        )
    )
    other.taken_down_at = NOW
    session.commit()
    assert client.get(path(doc, att)).status_code == 404


def test_body_uses_actual_public_source(client, session):
    doc, _, _, _ = seed(session, body="")
    other, _, _, _ = seed(session, 2)
    other.title = "转载：关于科技计划项目申报的通知"
    session.add(
        DocumentMerge(
            duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="reader-test"
        )
    )
    session.commit()
    data = client.get(path(doc)).json()
    assert data["source"]["id"] == str(other.id)
    assert data["source"]["title"] == other.title


@pytest.mark.parametrize("main_body", ["详见附件", "【打印本页】" * 100])
def test_short_or_cleaned_empty_main_uses_readable_repost(client, session, main_body):
    doc, _, _, _ = seed(session, body=main_body)
    repost_body = "转载申报正文。" * 400
    other, _, _, _ = seed(session, 2, body=repost_body)
    session.add(
        DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="body")
    )
    session.commit()
    data = client.get(path(doc)).json()
    assert data["status"] == "ok"
    assert data["source"]["id"] == str(other.id)
    assert "".join(s["text"] for s in data["segments"]) == repost_body
    assert v1._reading_body(session, doc.id) == repost_body


def test_readable_main_still_preferred_to_longer_repost(client, session):
    main_body = "主文申报说明。" * 50
    doc, _, _, _ = seed(session, body=main_body)
    other, _, _, _ = seed(session, 2, body="更长转载申报说明。" * 500)
    session.add(
        DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="body")
    )
    session.commit()
    data = client.get(path(doc)).json()
    assert data["source"]["id"] == str(doc.id)
    assert "".join(s["text"] for s in data["segments"]) == main_body
    assert v1._reading_body(session, doc.id) == main_body


def test_equal_length_reposts_choose_lowest_id_and_keep_revision(client, session):
    doc, _, _, _ = seed(session, body="详见附件")
    first, _, _, _ = seed(session, 2, body="甲" * 25000)
    second, _, _, _ = seed(session, 3, body="乙" * 25000)
    # 归并记录故意反序插入；选择应按 document.id，而不是归并顺序或数据库返回顺序。
    for other in (second, first):
        session.add(
            DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="body")
        )
    session.commit()
    data = client.get(path(doc)).json()
    assert data["source"]["id"] == str(first.id)
    repeated = client.get(path(doc)).json()
    assert repeated["revision"] == data["revision"]
    response = client.get(
        path(doc), params={"cursor": data["nextCursor"], "revision": data["revision"]}
    )
    assert response.status_code == 200
    assert response.json()["nextCursor"] is None
    assert (
        "".join(s["text"] for s in data["segments"] + response.json()["segments"]) == "甲" * 25000
    )
    assert v1._reading_body(session, doc.id) == "甲" * 25000


def test_repost_reading_revision_includes_main_title(client, session):
    doc, _, _, _ = seed(session, body="详见附件")
    other, _, _, _ = seed(session, 2, body="正文" * 12500)
    session.add(
        DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="body")
    )
    session.commit()
    first = client.get(path(doc)).json()
    assert first["source"]["id"] == str(other.id) and first["nextCursor"]
    doc.title = "关于开展另一批科技计划项目申报的通知"
    session.commit()
    response = client.get(
        path(doc), params={"cursor": first["nextCursor"], "revision": first["revision"]}
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"


def test_fallback_rejects_private_and_list_bodies_and_preserves_link_policy(client, session):
    doc, _, _, _ = seed(session, body="详见附件")
    private, _, _, _ = seed(session, 2, rights=2, body="非一级来源" * 1000)
    roster, _, _, _ = seed(session, 3, kind="名单", body="个人名单" * 1000)
    gone, _, _, _ = seed(session, 4, body="原站撤下前正文" * 400)
    public, _, _, _ = seed(session, 5, body="可用转载正文" * 200)
    gone.link_gone_at = NOW
    for other in (private, roster, gone, public):
        session.add(
            DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, active=True, match_key="body")
        )
    session.commit()
    data = client.get(path(doc)).json()
    assert data["source"]["id"] == str(public.id)
    assert "".join(s["text"] for s in data["segments"]) == "可用转载正文" * 200
    # 旧详情/AI 的来源撤下规则不因原生阅读改动而改变。
    assert v1._reading_body(session, doc.id) == "原站撤下前正文" * 400


@pytest.mark.parametrize("kind", ["名单", "其他"])
def test_nonpublic_body_kinds_stay_protected(client, session, kind):
    doc, att, _, _ = seed(session, kind=kind)
    # 名单类通知的正文挡掉名单内容后可读（D-092，见 test_list_body.py）；其他类照旧不给
    if kind == "名单":
        assert client.get(path(doc)).json()["status"] == "ok"
    else:
        assert client.get(path(doc)).json()["status"] == "unsupported"
    data = client.get(path(doc, att)).json()
    assert data["segments"] == []
    # 名单附件：说明只提供查询结果（页面给"在已收录名单中查询企业"按钮，D-088）
    if kind == "名单":
        assert data["status"] == "list" and "企业查询结果" in data["notice"]
    else:
        assert data["status"] == "unsupported"


def test_pii_masked_across_body_boundaries_and_metadata(client, session):
    doc, att, _, version = seed(session, body="甲" * 998 + "13812345678" + "申报说明" * 100)
    att.name = "材料-13812345678.docx"
    row = session.query(AttachmentTextSegment).filter_by(version_id=version.id, n=1).one()
    row.text = "联系电话：138 1234 5678"
    row.loc = {"sheet": "手机号13812345678", "row": 1, "secret": "should-not-pass"}
    row.cells = [{"c": 1, "t": "13812345678", "h": "电话"}]
    session.commit()
    for url in (path(doc), path(doc, att)):
        response = client.get(url)
        assert response.status_code == 200
        assert "13812345678" not in response.text and "138 1234 5678" not in response.text
        assert "should-not-pass" not in response.text


def test_scan_partial_and_empty_are_not_fulltext(client, session):
    doc, att, url, version = seed(session)
    version.scanned_pages = 2
    session.commit()
    data = client.get(path(doc, att)).json()
    assert data["status"] == "partial" and data["scannedPages"] == 2
    for reason in ("scan", "empty", "gone"):
        url.status = version.status = "failed"
        url.reason = version.reason = reason
        session.commit()
        data = client.get(path(doc, att)).json()
        assert data["segments"] == []
        assert data["status"] == (reason if reason != "gone" else "unavailable")


def test_retention_and_extension_limits(client, session):
    doc, att, _, _ = seed(session)
    doc.publish_date = date(2020, 1, 1)
    session.commit()
    assert client.get(path(doc, att)).json()["status"] == "unavailable"
    doc.publish_date = date(2026, 10, 22)
    att.extension = "zip"
    session.commit()
    assert client.get(path(doc, att)).json()["status"] == "unsupported"


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "1"},
        {"cursor": "-1"},
        {"cursor": "1e4"},
        {"revision": "bad"},
        {"cursor": "9999999999"},
    ],
)
def test_bad_cursor_never_skips_version_check(client, session, params):
    doc, att, _, _ = seed(session)
    response = client.get(path(doc, att), params=params)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"


def test_oversized_table_row_is_bounded(client, session):
    doc, att, _, version = seed(session)
    row = session.query(AttachmentTextSegment).filter_by(version_id=version.id, n=1).one()
    row.text = "甲" * 100000
    row.kind = "row"
    session.commit()
    response = client.get(path(doc, att))
    assert response.json()["status"] == "unavailable"
    assert len(response.content) < readers.MAX_PAGE_BYTES


def test_restricted_deployed_account_can_read(session, restricted_api_factory):
    doc, att, _, _ = seed(session)
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    with restricted_api_factory() as factory, factory() as restricted:
        readers._reading_session(restricted)
        data = readers.attachment_reading(doc.id, att.id, restricted, request)
        assert data["status"] == "ok" and data["segments"]


def test_attachment_read_is_one_snapshot_during_reextraction(
    client, session, session_factory, monkeypatch
):
    doc, att, _, version = seed(session)
    first = client.get(path(doc, att)).json()
    original = readers._check_revision
    changed = False

    def replace_after_revision(cursor, expected, current):
        nonlocal changed
        after = original(cursor, expected, current)
        if not changed:
            changed = True
            with session_factory() as writer:
                writer.execute(
                    delete(AttachmentTextSegment).where(
                        AttachmentTextSegment.version_id == version.id
                    )
                )
                writer.add_all(
                    [
                        AttachmentTextSegment(
                            version_id=version.id, n=i, kind="p", text=f"新版第{i}段"
                        )
                        for i in range(1, 24)
                    ]
                )
                writer.execute(
                    update(AttachmentTextVersion)
                    .where(AttachmentTextVersion.id == version.id)
                    .values(extracted_at=NOW + timedelta(seconds=1))
                )
                writer.commit()
        return after

    monkeypatch.setattr(readers, "_check_revision", replace_after_revision)
    during = client.get(path(doc, att)).json()
    assert changed and during["revision"] == first["revision"]
    assert during["totalSegments"] == 45
    assert during["segments"] == first["segments"]
    latest = client.get(path(doc, att)).json()
    assert latest["totalSegments"] == 23
    assert latest["segments"][0]["text"] == "新版第1段"
    assert latest["revision"] != first["revision"]
    assert (
        client.get(
            path(doc, att), params={"cursor": "20", "revision": first["revision"]}
        ).status_code
        == 409
    )


def test_body_read_keeps_candidate_and_content_in_one_snapshot(client, session, session_factory):
    doc, _, _, _ = seed(session, body="旧版申报说明。" * 100)
    first = client.get(path(doc)).json()
    changed = False
    engine = session_factory.kw["bind"]

    def replace_before_body(conn, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if changed or not statement.startswith("SELECT document_details.body_text "):
            return
        changed = True
        reader_connection = conn.scalar(text("SELECT CONNECTION_ID()"))
        with session_factory() as writer:
            assert writer.scalar(text("SELECT CONNECTION_ID()")) != reader_connection
            writer.execute(
                update(DocumentDetail)
                .where(DocumentDetail.document_id == doc.id)
                .values(body_text="新版申报说明。" * 100)
            )
            writer.execute(
                update(Document)
                .where(Document.id == doc.id)
                .values(title="关于新一轮科技计划申报的通知")
            )
            writer.commit()

    event.listen(engine, "before_cursor_execute", replace_before_body)
    try:
        during = client.get(path(doc)).json()
    finally:
        event.remove(engine, "before_cursor_execute", replace_before_body)
    assert changed
    assert during == first
    latest = client.get(path(doc)).json()
    assert latest["title"] == "关于新一轮科技计划申报的通知"
    assert latest["segments"][0]["text"].startswith("新版申报说明")
    assert latest["revision"] != first["revision"]


def test_reading_snapshot_isolation_restores_pooled_connection(session_factory):
    engine = session_factory.kw["bind"]
    engine.dispose()
    with session_factory() as reader:
        readers._reading_session(reader)
        reader_id = reader.scalar(text("SELECT CONNECTION_ID()"))
        assert reader.scalar(text("SELECT @@transaction_isolation")) == "REPEATABLE-READ"
    with session_factory() as ordinary:
        assert ordinary.scalar(text("SELECT CONNECTION_ID()")) == reader_id
        assert ordinary.scalar(text("SELECT @@transaction_isolation")) == "READ-COMMITTED"


def test_real_docx_extraction_to_native_response(client, session):
    from .test_attachments import make_docx

    doc, att, _, _ = seed(session)
    content = make_docx("申报指南", "联系电话：138 1234 5678", "申请时间：2026年10月31日")
    texts.record(session, att.url, "docx", NOW, texts.TextReport(), content=content)
    data = client.get(path(doc, att)).json()
    assert data["status"] == "ok"
    text = "\n".join(row["text"] for row in data["segments"])
    assert "申报指南" in text and "2026年10月31日" in text
    assert "138" not in text and "脱敏" in text


def test_url_withdrawal_cannot_read_previous_version(client, session):
    doc, att, url, _ = seed(session)
    first = client.get(path(doc, att)).json()
    url.version_id = None
    url.status, url.reason = "failed", "gone"
    session.commit()
    data = client.get(
        path(doc, att), params={"cursor": first["nextCursor"], "revision": first["revision"]}
    ).json()
    assert data["status"] == "unavailable" and not data["segments"]


def test_out_of_range_cursor_and_rate_limit(client, session, monkeypatch):
    doc, att, _, _ = seed(session)
    first = client.get(path(doc, att)).json()
    assert (
        client.get(
            path(doc, att), params={"cursor": "999", "revision": first["revision"]}
        ).status_code
        == 422
    )
    monkeypatch.setattr(download_api, "PARTS_PER_10MIN", 1)
    response = client.get(path(doc, att))
    assert response.status_code == 429 and response.headers["cache-control"] == "no-store"
