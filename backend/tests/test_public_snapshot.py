"""公开内容快照：只导出公开内容表，用户数据、密钥、日志不出线上；本机导入后条数、外键、内容都对得上。"""

import gzip
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
    doc.taken_down_at = NOW
    doc.takedown_reason = "rights"
    doc.takedown_note = SECRETS["takedown"]
    doc.taken_down_by = "创始人"
    doc.restored_at = NOW
    doc.restore_note = SECRETS["restore"]
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
    events = [json.loads(ln)["r"] for ln in lines[1:-1] if '"t": "document_takedown_events"' in ln]
    assert events[0]["note"] == "" and events[0]["reason"] == "rights"
    # 线上库本身没被改动
    session.expire_all()
    assert session.scalar(select(Document.takedown_note)) == SECRETS["takedown"]


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
