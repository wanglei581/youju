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
