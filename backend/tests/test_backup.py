"""数据库备份（验收 A6）：整库导出、上传到云托管对象存储、保留 30 天；恢复到迁移好的空库并核对条数。"""

import gzip
import io
import json
from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, text

from youju.backup import (
    BackupError,
    download_latest,
    dump,
    object_path,
    restore,
    run_backup,
    verify,
)
from youju.db import create_mysql_engine
from youju.models import (
    AICheck,
    AIUsage,
    Base,
    Document,
    DocumentDeadline,
    DocumentDetail,
    Feedback,
    Follow,
    Source,
    User,
)

from .conftest import TEST_DB_URL

NOW = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)  # 北京时间 9 月 29 日凌晨 4 点


def populate(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知公告",
        url="https://kjt.shandong.gov.cn/tzgg/",
        enabled=True,
        parser_config={"item_selector": "ul.list li"},
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        url="https://kjt.shandong.gov.cn/tzgg/1.html",
        url_key="kjt.shandong.gov.cn/tzgg/1.html",
        title="关于组织申报2026年度科技计划项目的通知",
        publish_date=date(2026, 9, 20),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="请各单位于2026年10月15日17:00前完成网上申报。" * 50,
            fetched_at=NOW,
            status="ok",
            publish_date=date(2026, 9, 20),
        )
    )
    session.add(
        DocumentDeadline(
            document_id=doc.id,
            fingerprint="f" * 64,
            kind="企业申报截止",
            candidate_date=date(2026, 10, 15),
            candidate_time=time(17, 0),
            evidence="请各单位于2026年10月15日17:00前完成网上申报",
            confidence="高",
            status="approved",
            reviewed_by="创始人",
            reviewed_at=datetime(2026, 9, 27, 14, 34, 25, 305061, tzinfo=UTC),
            approved_date=date(2026, 10, 15),
            approved_time=time(17, 0),
            ai_agrees=True,
        )
    )
    user = User(openid="o-backup")
    session.add(user)
    session.flush()
    session.add(Follow(user_id=user.id, name="山东科技", conditions={"region": ["山东"], "k": 1}))
    session.add(
        Feedback(user_id=user.id, document_id=doc.id, content="日期不对", check_result="pass")
    )
    session.add(
        AICheck(
            document_id=doc.id,
            task="kind",
            model="qwen-flash",
            result={"kind": "申报通知", "confidence": 0.9},
            checked_at=NOW,
        )
    )
    session.add(
        AIUsage(
            day=date(2026, 9, 28),
            task="check",
            model="qwen-flash",
            calls=3,
            errors=0,
            prompt_tokens=100,
            completion_tokens=10,
        )
    )
    session.commit()
    return doc


def clear_restore_target(session, engine, *, keep_access_logs=False):
    """恢复测试只使用指定测试库；先留住备份字节，再清空恢复目标。"""
    session.rollback()
    session.expire_all()
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for table in Base.metadata.sorted_tables:
                if keep_access_logs and table.name == "access_logs":
                    continue
                conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        finally:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.fixture
def restore_engine(db_engine):
    return db_engine


def test_dump_and_restore_round_trip(session, db_engine, restore_engine) -> None:
    populate(session)
    out = io.BytesIO()
    counts = dump(db_engine, out, now=NOW)
    assert counts["documents"] == 1 and counts["ai_usage"] == 1 and counts["follows"] == 1

    header = json.loads(gzip.decompress(out.getvalue()).splitlines()[0])
    assert header["format"] == "youju-backup" and header["revision"] and header["tables"] == counts

    clear_restore_target(session, restore_engine)
    restored = restore(restore_engine, io.BytesIO(out.getvalue()))
    assert restored == counts
    with restore_engine.connect() as conn:
        row = conn.execute(select(DocumentDeadline.__table__)).mappings().one()
        assert row["candidate_time"] == time(17, 0) and row["ai_agrees"] is True
        assert row["reviewed_at"] == datetime(2026, 9, 27, 14, 34, 25, 305061, tzinfo=UTC)
        follow = conn.execute(select(Follow.__table__)).mappings().one()
        assert follow["conditions"] == {"region": ["山东"], "k": 1}
        body = conn.scalar(select(DocumentDetail.body_text))
        assert body == "请各单位于2026年10月15日17:00前完成网上申报。" * 50

    # 目标库不是空的就不恢复
    with pytest.raises(BackupError, match="不是空的"):
        restore(restore_engine, io.BytesIO(out.getvalue()))


def test_restore_refuses_a_different_schema_version(session, db_engine, restore_engine) -> None:
    populate(session)
    out = io.BytesIO()
    dump(db_engine, out, now=NOW)
    lines = gzip.decompress(out.getvalue()).splitlines(keepends=True)
    header = json.loads(lines[0])
    header["revision"] = "0001"
    changed = gzip.compress((json.dumps(header) + "\n").encode() + b"".join(lines[1:]))
    with pytest.raises(BackupError, match="迁移版本"):
        restore(restore_engine, io.BytesIO(changed))
    with pytest.raises(BackupError, match="不是有据的备份文件"):
        restore(restore_engine, io.BytesIO(gzip.compress(b'{"format": "x"}\n')))


def fake_cloud(*, upload_code=0, cos_status=204):
    """模拟云托管开放接口服务和对象存储。"""
    seen: dict = {"deleted": [], "cos": None}
    prefix = "cloud://prod-1.7465-prod-1-1258/"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://api.weixin.qq.com/tcb/uploadfile":
            body = json.loads(request.content)
            seen["upload"] = body
            if upload_code:
                return httpx.Response(200, json={"errcode": upload_code, "errmsg": "no"})
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "url": "https://cos.example.test/upload",
                    "token": "tok",
                    "authorization": "sig",
                    "file_id": prefix + body["path"],
                    "cos_file_id": "cosid",
                },
            )
        if url == "https://cos.example.test/upload":
            seen["cos"] = request.content
            return httpx.Response(cos_status)
        if url == "http://api.weixin.qq.com/tcb/batchdeletefile":
            ids = json.loads(request.content)["fileid_list"]
            seen["deleted"] = ids
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "delete_list": [
                        {"fileid": i, "status": 0 if n < 2 else -1} for n, i in enumerate(ids)
                    ],
                },
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen, prefix


def test_run_backup_uploads_and_prunes_old_copies(session, db_engine) -> None:
    populate(session)
    client, seen, prefix = fake_cloud()
    report = run_backup(db_engine, "prod-1", client=client, now=NOW)
    assert report.path == "backups/youju-20260929.jsonl.gz"  # 北京时间的日期
    assert seen["upload"] == {"env": "prod-1", "path": report.path}
    assert report.file_id == prefix + report.path and report.bytes > 0
    cos = seen["cos"]
    for field_name, value in (
        (b"key", report.path.encode()),
        (b"Signature", b"sig"),
        (b"x-cos-security-token", b"tok"),
        (b"x-cos-meta-fileid", b"cosid"),
    ):
        assert b'name="' + field_name + b'"' in cos and value in cos
    assert b'name="file"' in cos
    # 删掉 31–40 天前的（本来就没有的不算错）
    assert seen["deleted"][0] == prefix + object_path(date(2026, 8, 29))
    assert seen["deleted"][-1] == prefix + object_path(date(2026, 8, 20))
    assert len(seen["deleted"]) == 10 and report.pruned == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"upload_code": 85088}, "errcode 85088"), ({"cos_status": 403}, "HTTP 403")],
)
def test_backup_failures_are_reported(session, db_engine, kwargs, message) -> None:
    populate(session)
    client, _, _ = fake_cloud(**kwargs)
    with pytest.raises(BackupError, match=message):
        run_backup(db_engine, "prod-1", client=client, now=NOW)


def test_cli_backup_to_file_and_restore_checks(session, db_engine, tmp_path, monkeypatch, capsys):
    from youju import cli

    populate(session)
    settings = SimpleNamespace(database_url=TEST_DB_URL, cloud_env=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    target = tmp_path / "backup.jsonl.gz"
    assert cli.main(["backup", "--output", str(target)]) == 0
    header = json.loads(gzip.decompress(target.read_bytes()).splitlines()[0])
    assert header["tables"]["documents"] == 1
    assert cli.main(["backup"]) == 1  # 没设云托管环境 ID
    assert "YOUJU_CLOUD_ENV" in capsys.readouterr().out
    assert cli.main(["restore", str(target)]) == 1  # 当前库不是空的，不恢复
    assert "不是空的" in capsys.readouterr().out
    assert session.scalar(select(Document.id)) is not None


def _dump(db_engine) -> bytes:
    out = io.BytesIO()
    dump(db_engine, out, now=NOW)
    return out.getvalue()


def _rewrite(raw: bytes, change) -> bytes:
    lines = gzip.decompress(raw).splitlines(keepends=True)
    return gzip.compress(b"".join(change(lines)))


def _empty(engine) -> bool:
    with engine.connect() as conn:
        return not any(
            conn.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables
        )


def test_broken_or_tampered_files_leave_the_target_untouched(
    session, db_engine, restore_engine
) -> None:
    populate(session)
    raw = _dump(db_engine)
    tampered = _rewrite(
        raw, lambda lines: [ln.replace("日期不对".encode(), "日期很对".encode()) for ln in lines]
    )
    extra_field = _rewrite(
        raw,
        lambda lines: [
            ln.replace(b'"name": "', b'"surprise": 1, "name": "', 1)
            if b'"t": "follows"' in ln
            else ln
            for ln in lines
        ],
    )
    cases = [
        (raw[: len(raw) // 2], "损坏或不完整"),  # 截断
        (_rewrite(raw, lambda lines: lines[:-1]), "没有结尾"),
        (tampered, "内容和备份不一致：feedback"),
        (extra_field, "表结构不一致"),
        (b"", "不是有据的备份文件"),  # 空文件
    ]
    clear_restore_target(session, restore_engine)
    for data, message in cases:
        with pytest.raises(BackupError, match=message):
            restore(restore_engine, io.BytesIO(data))
        assert _empty(restore_engine), message  # 回滚，目标库还是空的
    assert restore(restore_engine, io.BytesIO(raw))["follows"] == 1


def test_failed_restore_resets_the_session(session, db_engine) -> None:
    """失败后外键检查改回来、表锁释放，连接回到连接池也照常。"""
    populate(session)
    raw = _dump(db_engine)
    clear_restore_target(session, db_engine)
    engine = create_mysql_engine(TEST_DB_URL, pool_size=1, max_overflow=0)
    try:
        with pytest.raises(BackupError):
            restore(engine, io.BytesIO(_rewrite(raw, lambda lines: lines[:-1])))
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT @@SESSION.foreign_key_checks")) == 1
            conn.execute(text("INSERT INTO users (openid) VALUES ('o-after')"))  # 没有残留的表锁
            conn.rollback()
    finally:
        engine.dispose()


def test_upload_retries_busy_errors_but_not_setup_errors(session, db_engine) -> None:
    populate(session)
    waits = []
    statuses = iter([503, 204])
    client, seen, _ = fake_cloud()
    transport = client._transport

    def flaky(request):
        if str(request.url) == "https://cos.example.test/upload":
            return httpx.Response(next(statuses))
        return transport.handle_request(request)

    retrying = httpx.Client(transport=httpx.MockTransport(flaky))
    report = run_backup(db_engine, "prod-1", client=retrying, now=NOW, sleep=waits.append)
    assert report.file_id and waits == [5]
    client, _, _ = fake_cloud(upload_code=85088)
    waits.clear()
    with pytest.raises(BackupError, match="85088"):
        run_backup(db_engine, "prod-1", client=client, now=NOW, sleep=waits.append)
    assert waits == []  # 没开通、没登记接口这类错误不重试


def test_bad_header_is_reported_as_a_broken_file(restore_engine) -> None:
    with restore_engine.connect() as conn:
        revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
    full = {t.name: 0 for t in Base.metadata.sorted_tables}
    base = {"format": "youju-backup", "version": 1, "revision": revision}
    for header in (
        {**base, "tables": 5},
        {**base, "revision": 7, "tables": full},
        {**base, "tables": {**full, "users": -1}},
        {**base, "tables": {**full, "users": True}},
        {**base, "tables": {"users": 1}},  # 少了表
        {**base, "tables": {**full, "x": 1}},  # 多了不认识的表
    ):
        data = gzip.compress((json.dumps(header) + "\n").encode())
        with pytest.raises(BackupError, match="说明行格式不对"):
            restore(restore_engine, io.BytesIO(data))
    # 版本不同时先说版本（表名自然对不上）
    other = gzip.compress(
        (json.dumps({**base, "revision": "0099", "tables": {"x": 1}}) + "\n").encode()
    )
    with pytest.raises(BackupError, match="迁移版本"):
        restore(restore_engine, io.BytesIO(other))


def test_pruning_retries_busy_errors_and_says_the_backup_is_safe(session, db_engine) -> None:
    populate(session)
    client, _, _ = fake_cloud()
    transport = client._transport
    answers = iter([503, None])

    def flaky(request):
        if str(request.url).endswith("/tcb/batchdeletefile"):
            status = next(answers, 503)
            if status:
                return httpx.Response(status)
        return transport.handle_request(request)

    waits = []
    retrying = httpx.Client(transport=httpx.MockTransport(flaky))
    report = run_backup(db_engine, "prod-1", client=retrying, now=NOW, sleep=waits.append)
    assert report.pruned == 2 and waits == [5]

    answers = iter([])  # 一直 503
    waits.clear()
    with pytest.raises(BackupError, match="今天的备份已上传，但删除 30 天前的备份失败"):
        run_backup(db_engine, "prod-1", client=retrying, now=NOW, sleep=waits.append)
    assert waits == [5, 15]


def test_undeleted_old_backups_are_logged(caplog, monkeypatch) -> None:
    """跑一次命令行后，备份模块的日志按 INFO 输出；没删掉的旧备份写进日志。"""
    import logging

    from youju import alerts, cli
    from youju.backup import prune

    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(alert_webhook=None))
    project = logging.getLogger("youju")
    before = project.level
    try:
        assert cli.main(["alert", "测试"]) == 1
        assert logging.getLogger("youju.backup").getEffectiveLevel() == logging.INFO

        client, _, prefix = fake_cloud()
        path = object_path(date(2026, 9, 29))
        with caplog.at_level(logging.NOTSET):
            assert prune(client, "prod-1", prefix + path, path, date(2026, 9, 29)) == 2
    finally:
        project.setLevel(before)  # 不影响别的测试
    logged = [r for r in caplog.records if r.name == "youju.backup"]
    assert len(logged) == 8 and all(r.levelno == logging.INFO for r in logged)
    assert "旧备份没删掉" in logged[0].getMessage()


def test_verify_checks_backup_without_writing_database(session, db_engine) -> None:
    populate(session)
    raw = _dump(db_engine)
    counts = verify(db_engine, io.BytesIO(raw))
    assert counts["documents"] == 1 and counts["follows"] == 1
    assert session.scalar(select(Document.id)) is not None


def test_verify_reports_corruption_and_schema_mismatch(session, db_engine) -> None:
    populate(session)
    raw = _dump(db_engine)

    def change_header(lines, **changes):
        header = json.loads(lines[0])
        header.update(changes)
        lines[0] = (json.dumps(header) + "\n").encode()
        return lines

    def wrong_count(lines):
        header = json.loads(lines[0])
        header["tables"]["documents"] += 1
        return change_header(lines, tables=header["tables"])

    def bad_json(lines):
        lines[1] = b"{bad json}\n"
        return lines

    def bad_table(lines):
        for i, line in enumerate(lines[1:-1], start=1):
            if b'"t": "' in line:
                lines[i] = line.replace(b'"t": "', b'"t": "unknown_', 1)
                break
        return lines

    def bad_digest(lines):
        trailer = json.loads(lines[-1])
        trailer["sha256"]["documents"] = "0" * 64
        lines[-1] = (json.dumps(trailer) + "\n").encode()
        return lines

    cases = [
        (raw[:-8], "损坏或不完整"),
        (_rewrite(raw, lambda lines: lines[:-1]), "没有结尾"),
        (_rewrite(raw, wrong_count), "实际条数"),
        (_rewrite(raw, bad_json), "损坏或不完整"),
        (_rewrite(raw, bad_table), "表名或字段"),
        (_rewrite(raw, bad_digest), "SHA-256"),
        (_rewrite(raw, lambda lines: change_header(lines, format="other")), "格式版本"),
        (_rewrite(raw, lambda lines: change_header(lines, version=2)), "格式版本"),
        (_rewrite(raw, lambda lines: change_header(lines, revision="0099")), "迁移版本"),
    ]
    for data, message in cases:
        with pytest.raises(BackupError, match=message):
            verify(db_engine, io.BytesIO(data))


def test_download_latest_uses_open_api_and_retries(tmp_path) -> None:
    raw = gzip.compress(b"backup")
    today = date(2026, 10, 14)
    latest = object_path(date(2026, 10, 13))
    prefix = "cloud://prod-1.bucket/"
    seen = {"gets": 0, "links": 0, "paths": [], "probes": []}

    def handler(request):
        url = str(request.url)
        if url.endswith("/tcb/uploadfile"):
            path = json.loads(request.content)["path"]
            seen["probes"].append(path)
            return httpx.Response(200, json={"errcode": 0, "file_id": prefix + path})
        if url.endswith("/tcb/batchdownloadfile"):
            seen["links"] += 1
            ids = [item["fileid"] for item in json.loads(request.content)["file_list"]]
            seen["paths"] += ids
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "file_list": [
                        {
                            "fileid": name,
                            "status": 0 if name == prefix + latest else -1,
                            "download_url": "https://cos.example.test/backup",
                        }
                        for name in ids
                    ],
                },
            )
        if url == "https://cos.example.test/backup":
            seen["gets"] += 1
            return httpx.Response(503 if seen["gets"] == 1 else 200, content=raw)
        return httpx.Response(404)

    waits = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        dest = tmp_path / "latest.jsonl.gz"
        assert download_latest(client, "prod-1", dest, today=today, sleep=waits.append) == latest
    assert dest.read_bytes() == raw
    assert seen["paths"][:2] == [prefix + object_path(today), prefix + latest]
    assert seen["links"] == 2 and seen["gets"] == 2 and waits == [5]
    # 取前缀用专门的探测路径，不拿真实备份的路径申请上传链接
    assert seen["probes"] == ["backups/prefix-probe"]


def test_cli_backup_verify_file(session, db_engine, tmp_path, monkeypatch, capsys) -> None:
    from youju import cli

    populate(session)
    source = tmp_path / "backup.jsonl.gz"
    source.write_bytes(_dump(db_engine))
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(database_url=TEST_DB_URL, cloud_env=None)
    )
    assert cli.main(["backup-verify", "--file", str(source)]) == 0
    output = capsys.readouterr().out
    assert "备份校验通过" in output and "documents: 1" in output
    source.write_bytes(source.read_bytes()[:-8])
    assert cli.main(["backup-verify", "--file", str(source)]) == 1
    assert "备份校验失败" in capsys.readouterr().out
    assert cli.main(["backup-verify"]) == 1
    assert "YOUJU_CLOUD_ENV" in capsys.readouterr().out


# ---- 备份恢复演练（youju restore-drill）----
# 临时库名都用真实的北京时间加随机后缀，不写死：几个会话共用 :33306 时不会互相删库


def _drill_name(output: str) -> str:
    import re

    match = re.search(r"已建临时库 (youju_drill_\d{14}_[0-9a-f]{4})", output)
    assert match, output
    return match.group(1)


def _schemas(conn, name: str) -> int:
    return conn.scalar(
        text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:name"),
        {"name": name},
    )


def _counts(db_engine) -> dict:
    with db_engine.connect() as conn:
        return {
            table.name: conn.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }


def test_restore_drill_file_and_download(session, db_engine, tmp_path, monkeypatch, capsys):
    from youju import cli, restore_drill

    populate(session)
    source = tmp_path / "backup.jsonl.gz"
    source.write_bytes(_dump(db_engine))
    before = _counts(db_engine)
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(database_url=TEST_DB_URL, cloud_env="test")
    )

    def download(client, env, dest):
        assert env == "test"
        dest.write_bytes(source.read_bytes())
        return "backups/cloud-backup.jsonl.gz"

    monkeypatch.setattr(restore_drill, "download_latest", download)
    for arguments, filename in [
        (["--file", str(source)], source.name),
        ([], "cloud-backup.jsonl.gz"),
    ]:
        assert cli.main(["restore-drill", *arguments]) == 0
        output = capsys.readouterr().out
        name = _drill_name(output)
        assert filename in output and "documents: 1" in output and "follows: 1" in output
        # 备份时间按北京时间写（备份时间 NOW 是北京时间 9 月 29 日 4 点）
        assert "备份时间：2026-09-29 04:00:00（北京时间）" in output and "+00:00" not in output
        assert f"临时库 {name} 已删除" in output and "恢复演练成功" in output
        assert _counts(db_engine) == before
        with db_engine.connect() as conn:
            assert not _schemas(conn, name)


def test_restore_drill_checks_backup_before_creating_database(
    session, db_engine, tmp_path, monkeypatch, capsys
):
    from alembic.script import ScriptDirectory

    from youju import restore_drill
    from youju.migrations import alembic_config

    def created():
        with db_engine.connect() as conn:
            return set(
                conn.execute(text("SHOW DATABASES LIKE 'youju\\_drill\\_%'")).scalars().all()
            )

    populate(session)
    good = _dump(db_engine)
    bad = tmp_path / "bad.gz"
    bad.write_bytes(gzip.compress(b'{"format":"wrong"}\n'))
    script = ScriptDirectory.from_config(alembic_config(TEST_DB_URL))
    head = script.get_current_head()
    older = script.get_revision(head).down_revision
    older = older[0] if isinstance(older, tuple) else older

    def with_revision(revision):
        def change(lines):
            header = json.loads(lines[0])
            header["revision"] = revision
            return [json.dumps(header).encode() + b"\n", *lines[1:]]

        path = tmp_path / f"rev-{revision}.gz"
        path.write_bytes(_rewrite(good, change))
        return path

    cases = [
        (bad, "说明行格式不对"),
        (tmp_path / "missing.gz", "找不到备份文件"),
        (with_revision(older), "要等下一份备份"),
        (with_revision("zzzz_not_a_revision"), "不在现在的代码里"),
    ]
    before = created()
    for path, message in cases:
        assert restore_drill.run(TEST_DB_URL, None, path) == 1
        output = capsys.readouterr().out
        assert message in output, output
        assert "已建临时库" not in output
        assert created() == before


def test_restore_drill_refuses_unsafe_targets(monkeypatch, capsys):
    from youju import restore_drill

    monkeypatch.setattr(restore_drill, "_database_name", lambda: "youju_drill_20261104120000_abcd")
    monkeypatch.setattr(
        restore_drill, "create_mysql_engine", lambda *args: pytest.fail("不能连接或创建数据库")
    )
    assert restore_drill.run("sqlite:///test.db", None, None) == 1
    same = "mysql+pymysql://root@localhost/youju_drill_20261104120000_abcd"
    assert restore_drill.run(same, None, None) == 1
    assert "与当前库相同" in capsys.readouterr().out


def test_restore_drill_reports_undeleted_database(
    session, db_engine, tmp_path, monkeypatch, capsys
):
    from contextlib import contextmanager

    from sqlalchemy import make_url

    from youju import restore_drill

    populate(session)
    source = tmp_path / "backup.jsonl.gz"
    source.write_bytes(_dump(db_engine))
    real_engine = restore_drill.create_mysql_engine

    class Conn:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, *args, **kwargs):
            sql = str(statement)
            if sql.startswith("DROP DATABASE") and "已建" not in sql:
                raise RuntimeError("模拟删除失败")
            return self.conn.execute(statement, *args, **kwargs)

    class FailingDrop:
        def __init__(self, engine):
            self.engine = engine

        def connect(self):
            return self.engine.connect()

        @contextmanager
        def begin(self):
            with self.engine.begin() as conn:
                yield Conn(conn)

        def dispose(self):
            self.engine.dispose()

    def engine(url, **kwargs):
        made = real_engine(url, **kwargs)
        # 只换掉不带库名的那个（建库、删库用的）；临时库自己的连接照常
        return FailingDrop(made) if not make_url(url).database else made

    monkeypatch.setattr(restore_drill, "create_mysql_engine", engine)
    name = None
    try:
        assert restore_drill.run(TEST_DB_URL, None, source) == 1
        output = capsys.readouterr().out
        name = _drill_name(output)
        assert f"临时库 {name} 没删掉，请手动删除" in output
        assert "清理临时库失败" in output
    finally:
        if name:
            with db_engine.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))


def test_restore_drill_drops_stale_leftovers_only(session, db_engine, tmp_path, capsys):
    import secrets
    from datetime import timedelta

    from youju import restore_drill
    from youju.backup import BEIJING

    populate(session)
    source = tmp_path / "backup.jsonl.gz"
    source.write_bytes(_dump(db_engine))
    now = datetime.now(BEIJING)
    stale = f"youju_drill_{now - timedelta(hours=2):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"
    fresh = f"youju_drill_{now - timedelta(minutes=5):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"
    with db_engine.begin() as conn:
        for name in (stale, fresh):
            conn.execute(text(f"CREATE DATABASE `{name}`"))
    try:
        assert restore_drill.run(TEST_DB_URL, None, source) == 0
        output = capsys.readouterr().out
        assert f"发现上次没删掉的临时库 {stale}，先删掉" in output
        assert f"有一个 1 小时内建的临时库 {fresh}" in output
        with db_engine.connect() as conn:
            assert not _schemas(conn, stale)
            assert _schemas(conn, fresh)
    finally:
        with db_engine.begin() as conn:
            for name in (stale, fresh):
                conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))


def test_restore_drill_interrupted_still_drops(session, db_engine, tmp_path, monkeypatch, capsys):
    import signal

    from youju import restore_drill

    populate(session)
    source = tmp_path / "backup.jsonl.gz"
    source.write_bytes(_dump(db_engine))
    original = signal.getsignal(signal.SIGTERM)

    def stopped(engine, src):
        # 模拟实例停止：恢复到一半收到 SIGTERM
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM 应该已经转成中断")

    monkeypatch.setattr(restore_drill, "restore", stopped)
    assert restore_drill.run(TEST_DB_URL, None, source) == 1
    output = capsys.readouterr().out
    name = _drill_name(output)
    assert "演练被中断" in output and f"临时库 {name} 已删除" in output
    with db_engine.connect() as conn:
        assert not _schemas(conn, name)
    # 跑完把信号处理换回原来的
    assert signal.getsignal(signal.SIGTERM) == original


def test_restore_drill_works_with_drill_only_grant(session, db_engine, tmp_path, capsys):
    """线上 admin 账号只多一句演练库授权（deploy/mysql_accounts.sql 第 ③ 节）：
    用只有这一句权限的账号跑通，顺带证明演练不需要、也碰不到当前库。"""
    import os
    import re
    import secrets
    from pathlib import Path

    from sqlalchemy import make_url
    from sqlalchemy.exc import DBAPIError

    from youju import restore_drill

    populate(session)
    source = tmp_path / "backup.jsonl.gz"
    source.write_bytes(_dump(db_engine))
    sql = (Path(__file__).parents[1] / "deploy/mysql_accounts.sql").read_text()
    grants = re.findall(
        r"^GRANT ALL PRIVILEGES ON `youju\\_drill\\_%`\.\* TO 'youju_admin'@'%';", sql, re.M
    )
    assert len(grants) == 1
    account = {"test_user": f"youju_drill_t{secrets.token_hex(3)}"}
    password = secrets.token_urlsafe(24)
    try:
        with db_engine.begin() as conn:
            account["test_host"] = conn.scalar(text("SELECT SUBSTRING_INDEX(USER(), '@', -1)"))
            conn.execute(
                text("CREATE USER :test_user@:test_host IDENTIFIED BY :password"),
                {**account, "password": password},
            )
    except DBAPIError as exc:
        if (
            exc.orig.args[0] in (1044, 1045, 1142, 1227)
            and os.environ.get("YOUJU_REQUIRE_DB") != "1"
        ):
            pytest.skip(f"测试 root 无 CREATE USER 权限（MySQL {exc.orig.args[0]}）")
        raise
    try:
        with db_engine.begin() as conn:
            conn.execute(
                text(grants[0].replace("'youju_admin'@'%'", ":test_user@:test_host")), account
            )
        url = make_url(TEST_DB_URL).set(username=account["test_user"], password=password)
        limited = url.render_as_string(hide_password=False)
        # 这个账号读不了当前库
        engine = create_mysql_engine(limited)
        with pytest.raises(DBAPIError), engine.connect() as conn:
            conn.execute(text("SELECT COUNT(*) FROM documents"))
        engine.dispose()
        before = _counts(db_engine)
        assert restore_drill.run(limited, None, source) == 0, capsys.readouterr().out
        output = capsys.readouterr().out
        name = _drill_name(output)
        assert f"临时库 {name} 已删除" in output
        assert _counts(db_engine) == before
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("DROP USER IF EXISTS :test_user@:test_host"), account)
