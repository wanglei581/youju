"""公开内容快照：只导出公开内容表，用户数据、密钥、日志不出线上；本机导入后条数、外键、内容都对得上。"""

import gzip
import hashlib
import io
import json
from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from youju import public_snapshot
from youju.backup import BackupError, dump, restore
from youju.models import (
    AppKey,
    Base,
    DeadlineClue,
    Document,
    DocumentDeadline,
    DocumentTakedownEvent,
    Feedback,
    Follow,
    SigningKey,
    User,
)
from youju.public_snapshot import (
    EXCLUDED_TABLES,
    PUBLIC_TABLES,
    object_path,
    public_tables,
    run,
)

from .conftest import TEST_DB_URL
from .test_backup import NOW, clear_restore_target, fake_cloud, populate

SECRETS = {
    "openid": "o-private-openid-7",
    "feedback": "我是投诉人李四，电话 13912345678",
    "clue": "用户自己填的依据：截止到十月底",
    "takedown": "权利人王五来信要求下架",
    "restore": "王五撤回投诉",
    "app_key": "app-key-secret-value",
    "signing_key": "ab" * 32,
}


def populate_private(session, doc: Document) -> None:
    """公开内容之外，再放进各种不该导出的东西。"""
    user = User(openid=SECRETS["openid"])
    session.add(user)
    session.flush()
    session.add(
        Feedback(
            user_id=user.id,
            document_id=doc.id,
            content=SECRETS["feedback"],
            check_result="pass",
        )
    )
    session.add(Follow(user_id=user.id, name="我的关注", conditions={"region": ["山东"]}))
    clue_deadline = DocumentDeadline(
        document_id=doc.id,
        fingerprint="u" * 64,
        kind="企业申报截止",
        candidate_date=date(2026, 10, 31),
        evidence=SECRETS["clue"],
        confidence="低",
        origin="user",
        status="pending",
    )
    session.add(clue_deadline)
    session.flush()
    session.add(
        DeadlineClue(
            user_id=user.id,
            document_id=doc.id,
            kind="企业申报截止",
            date=date(2026, 10, 31),
            quote=SECRETS["clue"],
            submitted_at=NOW,
            deadline_id=clue_deadline.id,
        )
    )
    # 这份文件下架过、又恢复了（恢复后不算下架）：备注还留在库里
    doc.taken_down_at = None
    doc.takedown_reason = "rights"
    doc.takedown_note = SECRETS["takedown"]
    doc.taken_down_by = "创始人"
    doc.restored_at = NOW
    doc.restore_note = SECRETS["restore"]
    doc.restored_by = "创始人"
    session.add(
        DocumentTakedownEvent(
            document_id=doc.id,
            action="takedown",
            reason="rights",
            note=SECRETS["takedown"],
            operated_by="创始人",
            operated_at=NOW,
        )
    )
    session.add(AppKey(name="secret", value=SECRETS["app_key"]))
    session.add(SigningKey(name="pay", key_hex=SECRETS["signing_key"]))
    session.commit()


def _snapshot(db_engine) -> bytes:
    out = io.BytesIO()
    public_snapshot.dump(db_engine, out, now=NOW)
    return out.getvalue()


def test_every_table_is_classified() -> None:
    """新加的表要写明是公开内容还是不导出；两边都写或都没写就失败。"""
    names = {t.name for t in Base.metadata.sorted_tables}
    assert not PUBLIC_TABLES & EXCLUDED_TABLES
    assert names - PUBLIC_TABLES - EXCLUDED_TABLES == set(), (
        "新表要写进 PUBLIC_TABLES 或 EXCLUDED_TABLES"
    )
    assert (PUBLIC_TABLES | EXCLUDED_TABLES) - names == set(), "名单里有已经不存在的表"


def test_user_tables_and_secrets_are_never_public() -> None:
    must_stay = {
        "users",
        "follows",
        "feedback",
        "deadline_clues",
        "subscriptions",
        "events",
        "advisor_tasks",
        "ai_safety_hits",
        "pay_orders",
        "invoice_requests",
        "workbench_passes",
        "app_keys",
        "signing_keys",
        "access_logs",
    }
    assert must_stay <= EXCLUDED_TABLES
    # 公开表的外键只指向公开表：导入时不需要、也不会带出别的表
    for table in public_tables():
        for fk in table.foreign_keys:
            assert fk.column.table.name in PUBLIC_TABLES, f"{table.name}.{fk.parent.name}"
    # 任何公开表都没有指向用户的字段
    for table in public_tables():
        assert not {"user_id", "openid", "user_hash"} & {c.name for c in table.columns}, table.name


def test_snapshot_leaves_out_user_data(session, db_engine) -> None:
    doc = populate(session)
    populate_private(session, doc)
    raw = _snapshot(db_engine)
    text = gzip.decompress(raw).decode()
    for name, secret in SECRETS.items():
        assert secret not in text, name
    lines = text.splitlines()
    header = json.loads(lines[0])
    assert header["format"] == "youju-public-snapshot" and header["revision"]
    assert set(header["tables"]) == PUBLIC_TABLES
    assert header["not_exported"] == sorted(EXCLUDED_TABLES)
    assert "code_revision" in header
    tables_seen = {json.loads(line)["t"] for line in lines[1:-1]}
    assert tables_seen <= PUBLIC_TABLES
    # 用户线索生成的截止日期不导出，核对过的那条照常导出
    deadlines = [json.loads(ln)["r"] for ln in lines[1:-1] if '"t": "document_deadlines"' in ln]
    assert [d["origin"] for d in deadlines] == ["rule"] and header["tables"][
        "document_deadlines"
    ] == 1
    # 下架、恢复备注清空，原因和操作人保留
    docs = [json.loads(ln)["r"] for ln in lines[1:-1] if '"t": "documents"' in ln]
    assert docs[0]["takedown_note"] is None and docs[0]["restore_note"] is None
    assert docs[0]["takedown_reason"] == "rights" and docs[0]["taken_down_by"] == "创始人"
    # 下架、恢复的操作记录整张表不导出
    assert not [ln for ln in lines[1:-1] if '"t": "document_takedown_events"' in ln]
    # 线上库本身没被改动
    session.expire_all()
    assert session.scalar(select(Document.takedown_note)) == SECRETS["takedown"]


def _insert(session, table_name: str, /, **values) -> int:
    table = Base.metadata.tables[table_name]
    return session.execute(table.insert().values(**values)).inserted_primary_key[0]


def test_taken_down_documents_are_left_out(session, db_engine) -> None:
    """下架的文件（归并组里有一份下架，整组都算）和挂在它们下面的行不导出；
    只被它们用到的附件、附件文字、名单文件、转发组也不导出，和公开文件共用的照常导出。"""
    public = populate(session)

    def new_document(slug: str) -> int:
        return _insert(
            session,
            "documents",
            source_id=public.source_id,
            url=f"https://kjt.shandong.gov.cn/tzgg/{slug}.html",
            url_key=f"kjt.shandong.gov.cn/tzgg/{slug}.html",
            title=f"下架测试 {slug}",
            publish_date=date(2026, 9, 21),
            first_seen_at=NOW,
            last_seen_at=NOW,
        )

    hidden, twin = new_document("hidden"), new_document("twin")
    documents = Base.metadata.tables["documents"]
    session.execute(
        documents.update()
        .where(documents.c.id == hidden)
        .values(taken_down_at=NOW, takedown_reason="rights", taken_down_by="创始人")
    )
    # twin 是 hidden 的副本（归并组），hidden 下架后它也不对外
    _insert(
        session,
        "document_merges",
        duplicate_id=twin,
        canonical_id=hidden,
        active=True,
        match_key="k",
    )
    shared = "https://kjt.shandong.gov.cn/files/shared.pdf"
    own = "https://kjt.shandong.gov.cn/files/own.pdf"
    list_url = "https://kjt.shandong.gov.cn/files/list.xlsx"
    for doc_id in (hidden, twin):
        _insert(
            session,
            "document_details",
            document_id=doc_id,
            body_text="下架文件正文，项目负责人赵六",
            fetched_at=NOW,
            status="ok",
        )
    for doc_id, position, url in ((hidden, 1, shared), (hidden, 2, own), (public.id, 1, shared)):
        _insert(
            session,
            "document_attachments",
            document_id=doc_id,
            position=position,
            name="附件",
            url=url,
            extension="pdf",
        )
    versions = {}
    for n, url in enumerate((shared, own), start=1):
        key = hashlib.sha256(url.encode()).hexdigest()
        _insert(
            session, "attachment_copies", url_hash=key, url=url, declared_type="pdf", status="ready"
        )
        versions[url] = _insert(
            session,
            "attachment_text_versions",
            sha256=str(n) * 64,
            size=10,
            status="ok",
            extractor=1,
            extracted_at=NOW,
        )
        _insert(
            session,
            "attachment_text_segments",
            version_id=versions[url],
            n=1,
            kind="p",
            text="附件文字，赵六" if url == own else "共用附件文字",
        )
        _insert(
            session,
            "attachment_text_urls",
            url_hash=key,
            url=url,
            declared_type="pdf",
            status="ok",
            version_id=versions[url],
        )
    _insert(session, "ai_checks", document_id=hidden, task="audit", model="m", checked_at=NOW)
    _insert(session, "ai_eligibility", document_id=hidden, computed_at=NOW)
    condition_list = _insert(
        session,
        "condition_lists",
        document_id=hidden,
        body_hash="h" * 64,
        model="m",
        prompt_version="1",
        extracted_at=NOW,
    )
    _insert(
        session,
        "condition_items",
        list_id=condition_list,
        position=1,
        category="其他",
        text="条件",
        original_text="条件",
        quote="原文",
        quote_offset=0,
    )
    _insert(
        session,
        "document_fields",
        document_id=hidden,
        field="support",
        value="资金",
        evidence="原文",
        evidence_offset=0,
        body_hash="h" * 64,
    )
    _insert(
        session,
        "document_deadlines",
        document_id=hidden,
        fingerprint="d" * 64,
        kind="企业申报截止",
        candidate_date=date(2026, 11, 1),
        evidence="原文",
        confidence="高",
        status="pending",
    )
    # 公开文件的截止日期指向下架文件："已变更"的必须带指向（库里的检查规则），整行不导出；
    # 其他状态的照常导出、指向清空
    changed = _insert(
        session,
        "document_deadlines",
        document_id=public.id,
        fingerprint="c" * 64,
        kind="企业申报截止",
        candidate_date=date(2026, 10, 20),
        evidence="原文",
        confidence="高",
        status="changed",
        approved_date=date(2026, 10, 20),
        change_reason="延期",
        changed_to_document_id=hidden,
    )
    pointer = _insert(
        session,
        "document_deadlines",
        document_id=public.id,
        fingerprint="p" * 64,
        kind="材料报送截止",
        candidate_date=date(2026, 10, 22),
        evidence="原文",
        confidence="高",
        status="approved",
        approved_date=date(2026, 10, 22),
        changed_to_document_id=hidden,
    )
    _insert(
        session,
        "list_parses",
        document_id=hidden,
        source_url=list_url,
        source_kind="attachment",
        status="ok",
        attempted_at=NOW,
    )
    entry = _insert(
        session,
        "list_entries",
        document_id=hidden,
        source_url=list_url,
        name="某某公司",
        normalized_name="某某公司",
        category="企业",
    )
    evidence = _insert(
        session,
        "list_evidence_sources",
        url_hash=hashlib.sha256(list_url.encode()).hexdigest(),
        url=list_url,
        status="ok",
        extractor=1,
        locator=1,
        checked_at=NOW,
    )
    _insert(
        session,
        "list_entry_locations",
        entry_id=entry,
        source_id=evidence,
        state="ok",
        rows=[],
        computed_at=NOW,
    )
    only_hidden = _insert(session, "repost_groups", strength="strong", topic="只有下架文件")
    mixed = _insert(session, "repost_groups", strength="strong", topic="也有公开文件")
    _insert(session, "repost_group_members", document_id=twin, group_id=only_hidden)
    _insert(session, "repost_group_members", document_id=hidden, group_id=mixed)
    _insert(session, "repost_group_members", document_id=public.id, group_id=mixed)
    session.commit()

    raw = _snapshot(db_engine)
    lines = gzip.decompress(raw).decode().splitlines()
    header = json.loads(lines[0])
    rows = [json.loads(line) for line in lines[1:-1]]
    gone = {hidden, twin}
    # 逐个检查所有指向通知的外键列：没有一行指向下架的文件
    pointers = {
        (t.name, fk.parent.name)
        for t in public_tables()
        for fk in t.foreign_keys
        if fk.column.table.name == "documents"
    }
    for item in rows:
        for table, column in pointers:
            if item["t"] == table:
                assert item["r"][column] not in gone, (table, column)

    def of(table: str) -> list[dict]:
        return [item["r"] for item in rows if item["t"] == table]

    assert [r["id"] for r in of("documents")] == [public.id]
    assert [r["url"] for r in of("attachment_copies")] == [shared]
    assert [r["url"] for r in of("attachment_text_urls")] == [shared]
    assert [r["id"] for r in of("attachment_text_versions")] == [versions[shared]]
    assert [r["version_id"] for r in of("attachment_text_segments")] == [versions[shared]]
    for table in ("list_evidence_sources", "list_entry_locations", "condition_items"):
        assert of(table) == [], table
    assert [r["id"] for r in of("repost_groups")] == [mixed]
    deadlines = {r["id"]: r for r in of("document_deadlines")}
    assert changed not in deadlines and deadlines[pointer]["changed_to_document_id"] is None
    assert len(deadlines) == 2  # populate 的那条和 pointer
    assert [r["document_id"] for r in of("repost_group_members")] == [public.id]
    assert "赵六" not in "\n".join(lines)
    assert header["rows_left_out"]["documents"].startswith("下架的文件 2 份")
    # 导入后条数、外键、内容都对得上
    clear_restore_target(session, db_engine)
    counts = public_snapshot.restore(db_engine, io.BytesIO(raw))
    assert counts["documents"] == 1 and counts["repost_group_members"] == 1


def test_restore_into_an_empty_database(session, db_engine) -> None:
    doc = populate(session)
    populate_private(session, doc)
    raw = _snapshot(db_engine)
    clear_restore_target(session, db_engine)
    counts = public_snapshot.restore(db_engine, io.BytesIO(raw))
    assert counts["documents"] == 1 and counts["document_details"] == 1
    assert counts["document_deadlines"] == 1 and counts["ai_checks"] == 1
    with db_engine.connect() as conn:
        assert conn.scalar(select(User.id)) is None  # 用户表还是空的
        assert conn.scalar(select(Feedback.id)) is None
        row = conn.execute(select(DocumentDeadline.__table__)).mappings().one()
        assert row["candidate_time"] == time(17, 0) and row["origin"] == "rule"
        assert row["reviewed_at"] == datetime(2026, 9, 27, 14, 34, 25, 305061, tzinfo=UTC)
        assert conn.scalar(select(Document.takedown_note)) is None
    # 公开内容表不是空的就不导入
    with pytest.raises(BackupError, match="不是空的"):
        public_snapshot.restore(db_engine, io.BytesIO(raw))


def test_formats_do_not_mix(session, db_engine) -> None:
    """整库备份和公开快照互相认不出对方的文件：本机不会误把整库恢复成测试库，反之亦然。"""
    populate(session)
    snapshot = _snapshot(db_engine)
    full = io.BytesIO()
    dump(db_engine, full, now=NOW)
    clear_restore_target(session, db_engine)
    with pytest.raises(BackupError, match="不是有据的公开快照文件"):
        public_snapshot.restore(db_engine, io.BytesIO(full.getvalue()))
    with pytest.raises(BackupError, match="不是有据的备份文件"):
        restore(db_engine, io.BytesIO(snapshot))
    with pytest.raises(BackupError, match="不是有据的公开快照文件"):
        public_snapshot.read_header(io.BytesIO(full.getvalue()))


def test_tampered_snapshot_leaves_the_target_untouched(session, db_engine) -> None:
    populate(session)
    raw = _snapshot(db_engine)
    lines = gzip.decompress(raw).splitlines(keepends=True)
    tampered = gzip.compress(
        b"".join(ln.replace("网上申报".encode(), "线下申报".encode()) for ln in lines)
    )
    header = json.loads(lines[0])
    header["revision"] = "0001"
    old_revision = gzip.compress((json.dumps(header) + "\n").encode() + b"".join(lines[1:]))
    clear_restore_target(session, db_engine)
    for data, message in (
        (tampered, "内容和备份不一致"),
        (old_revision, "迁移版本"),
        (raw[: len(raw) // 2], "损坏或不完整"),
        (gzip.compress(b"".join(lines[:-1])), "没有结尾"),
    ):
        with pytest.raises(BackupError, match=message):
            public_snapshot.restore(db_engine, io.BytesIO(data))
        with db_engine.connect() as conn:
            assert conn.scalar(select(Document.id)) is None, message
    assert public_snapshot.restore(db_engine, io.BytesIO(raw))["documents"] == 1


def test_run_uploads_and_prunes(session, db_engine) -> None:
    populate(session)
    client, seen, prefix = fake_cloud()
    report = run(db_engine, "prod-1", client=client, now=NOW)
    assert report.path == "public-snapshots/youju-public-20260929.jsonl.gz"  # 北京时间的日期
    assert seen["upload"] == {"env": "prod-1", "path": report.path}
    assert report.file_id == prefix + report.path and report.bytes > 0
    # 删掉 8–17 天前的（本来就没有的不算错），只删公开快照目录里的
    assert seen["deleted"][0] == prefix + object_path(date(2026, 9, 21))
    assert seen["deleted"][-1] == prefix + object_path(date(2026, 9, 12))
    assert all(i.startswith(prefix + "public-snapshots/") for i in seen["deleted"])
    assert len(seen["deleted"]) == 10 and report.pruned == 2


def test_cli_snapshot_and_restore(session, db_engine, tmp_path, monkeypatch, capsys) -> None:
    from youju import cli

    doc = populate(session)
    populate_private(session, doc)
    settings = SimpleNamespace(database_url=TEST_DB_URL, cloud_env=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    target = tmp_path / "youju-public.jsonl.gz"
    assert cli.main(["public-snapshot", "--output", str(target)]) == 0
    assert SECRETS["openid"] not in gzip.decompress(target.read_bytes()).decode()
    assert cli.main(["public-snapshot"]) == 1  # 没设云托管环境 ID
    assert "YOUJU_CLOUD_ENV" in capsys.readouterr().out
    assert cli.main(["restore-public", str(target)]) == 1  # 公开内容表不是空的
    assert "不是空的" in capsys.readouterr().out
    clear_restore_target(session, db_engine)
    assert cli.main(["restore-public", str(target)]) == 0
    out = capsys.readouterr().out
    assert "已导入：线上公开内容 " in out and "（北京时间），共 " in out and "documents: 1" in out
    assert "users" not in out and "feedback" not in out
