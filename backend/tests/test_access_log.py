"""网络日志：每次请求记一条（不记查询参数），每天归档到对象存储，保留 190 天。"""

import gzip
import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from youju import access_log
from youju.access_log import export_day, log_key, log_uid, object_path, run_archive
from youju.api import app as app_module
from youju.api import me
from youju.api.app import app
from youju.backup import BackupError, dump, restore
from youju.db import create_mysql_engine, get_session
from youju.models import AccessLog, AppKey

from .conftest import TEST_DB_URL
from .test_backup import fake_cloud

# 北京时间 9 月 29 日 0:30
NOW = datetime(2026, 9, 28, 16, 30, tzinfo=UTC)


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr(
        me,
        "get_settings",
        lambda: SimpleNamespace(trust_wx_headers=False, dev_openid="dev-user"),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def rows(session):
    session.expire_all()
    return list(session.scalars(select(AccessLog).order_by(AccessLog.id)))


def access_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "youju.access"]


def test_request_is_logged_without_query_and_with_pseudonymous_user(client, session, caplog):
    res = client.get("/api/v1/lists/lookup", params={"q": "某某科技有限公司"})
    assert res.status_code == 200
    [row] = rows(session)
    assert row.path == "/api/v1/lists/lookup" and row.method == "GET"
    assert row.status == 200 and row.service == "all" and row.ms >= 0
    assert row.uid == log_uid("dev-user") and len(row.uid) == 16 and row.uid != "dev-user"
    assert row.ip == "testclient" and row.detail is None
    assert abs(datetime.now(UTC) - row.at) < timedelta(minutes=1)
    # 标准输出也有一行，同样不带查询参数和 openid
    [line] = access_lines(caplog)
    assert line["saved"] is True and line["uid"] == row.uid and "detail" not in line
    assert line["path"] == "/api/v1/lists/lookup" and "dev-user" not in json.dumps(line)
    # 健康检查不记
    client.get("/health")
    assert len(rows(session)) == 1


def test_blocked_and_unauthorized_requests_are_logged(client, session, monkeypatch):
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(admin_token="t" * 32))
    assert client.get("/wp-login.php").status_code == 404
    assert client.get("/api/admin/reviews/export").status_code == 401
    got = [(r.path, r.status) for r in rows(session)]
    assert got == [("/wp-login.php", 404), ("/api/admin/reviews/export", 401)]


def test_admin_action_details_are_logged(client, session, monkeypatch):
    monkeypatch.setattr(app_module, "get_settings", lambda: SimpleNamespace(admin_token="t" * 32))
    res = client.get("/api/admin/reviews/export", headers={"X-Admin-Token": "t" * 32})
    assert res.status_code == 200
    [row] = rows(session)
    assert row.detail == {"action": "reviews_export", "count": 0}


def test_unhandled_errors_are_logged_as_500(session, monkeypatch):
    monkeypatch.setattr(app_module, "_send_in_background", lambda message: None)
    monkeypatch.setattr(
        me, "get_settings", lambda: SimpleNamespace(trust_wx_headers=False, dev_openid=None)
    )

    def broken_session():
        raise RuntimeError("boom")

    app.dependency_overrides[get_session] = broken_session
    try:
        res = TestClient(app, raise_server_exceptions=False).get("/api/v1/home")
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 500
    [row] = rows(session)
    assert (row.path, row.status, row.uid) == ("/api/v1/home", 500, None)


def test_openid_errors_do_not_break_logging(client, session, monkeypatch):
    def refuse(request):
        raise HTTPException(status_code=401)

    monkeypatch.setattr(me, "request_openid", refuse)
    assert client.get("/api/v1/home").status_code == 200
    assert rows(session)[0].uid is None


def test_write_failures_alert_and_rows_are_written_later(session, db_engine, monkeypatch, caplog):
    sent: list[str] = []
    now = {"t": 1000.0}
    monkeypatch.setattr(access_log, "_send_in_background", sent.append)
    monkeypatch.setattr(access_log, "clock", SimpleNamespace(monotonic=lambda: now["t"]))
    broken = create_mysql_engine(TEST_DB_URL.replace("/youju_test", "/youju_no_such_db"))
    entry = {
        "service": "api",
        "method": "GET",
        "path": "/api/v1/home",
        "status": 200,
        "ms": 3,
        "ip": None,
    }
    try:
        access_log.write({**entry, "openid": "dev-user"}, engine=broken)
        now["t"] += 60
        access_log.write(entry, engine=broken)
        now["t"] += 600
        access_log.write(entry, engine=broken)
    finally:
        broken.dispose()
    assert len(sent) == 2 and "上次提醒以来 2 次" in sent[1] and "现有 3 条" in sent[1]
    # 写库失败时标准输出里照样有，标明没存进库
    assert [x["saved"] for x in access_lines(caplog)] == [False, False, False]
    # 数据库恢复后，下一次写库把之前没写进去的一起补上（用户编号补算），顺序不变
    access_log.write({**entry, "path": "/api/v1/calendar"}, engine=db_engine)
    got = rows(session)
    assert [r.path for r in got] == ["/api/v1/home"] * 3 + ["/api/v1/calendar"]
    assert got[0].uid == log_uid("dev-user") and got[1].uid is None
    assert got[0].at < got[3].at and not access_log._pending


def test_pending_rows_are_capped(session, monkeypatch):
    monkeypatch.setattr(access_log, "_send_in_background", lambda message: None)
    monkeypatch.setattr(access_log, "_PENDING_MAX", 2)
    broken = create_mysql_engine(TEST_DB_URL.replace("/youju_test", "/youju_no_such_db"))
    try:
        for n in range(3):
            access_log.write({"service": "api", "method": "GET", "path": f"/{n}"}, engine=broken)
    finally:
        broken.dispose()
    # 满了丢最早的
    assert [row["path"] for row, _ in access_log._pending] == ["/1", "/2"]


def test_log_key_is_generated_once_and_kept(session, db_engine):
    key = log_key(db_engine)
    assert len(key) == 32
    assert session.scalar(select(AppKey.value).where(AppKey.name == "access_log")) == key.hex()
    access_log._key = None
    assert log_key(db_engine) == key
    # 已经有密钥（如别的实例先生成了）就用已有的
    session.execute(AppKey.__table__.delete())
    session.add(AppKey(name="access_log", value="ab" * 32))
    session.commit()
    access_log._key = None
    assert log_key(db_engine) == bytes.fromhex("ab" * 32)


def add(session, at: datetime, path: str = "/api/v1/home", logged_at: datetime | None = None):
    session.add(
        AccessLog(
            at=at,
            logged_at=logged_at or at,
            service="api",
            method="GET",
            path=path,
            status=200,
            ms=5,
            ip="1.2.3.4",
            uid="u" * 16,
        )
    )
    session.commit()


def read_archive(data: bytes) -> tuple[dict, list[dict], dict]:
    lines = gzip.decompress(data).splitlines(keepends=True)
    body = lines[1:-1]
    trailer = json.loads(lines[-1])
    assert trailer["sha256"] == hashlib.sha256(b"".join(body)).hexdigest()
    assert trailer["rows"] == len(body)
    return json.loads(lines[0]), [json.loads(x) for x in body], trailer


def test_archive_to_local_dir_keeps_today_and_drops_expired(session, db_engine, tmp_path):
    beijing_28 = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)  # 北京时间 9 月 28 日 0 点
    add(session, beijing_28 - timedelta(seconds=1), "/a")  # 27 日最后一秒
    add(session, beijing_28, "/b")  # 28 日第一秒
    add(session, beijing_28 + timedelta(hours=23), "/c")
    add(session, NOW - timedelta(minutes=20), "/today")  # 29 日 0:10
    add(session, NOW - timedelta(days=191), "/expired")
    report = run_archive(db_engine, None, output=tmp_path, now=NOW)
    assert report.expired == 1
    assert report.days == {date(2026, 9, 27): 1, date(2026, 9, 28): 2}
    header, items, _ = read_archive((tmp_path / "access-20260928.jsonl.gz").read_bytes())
    assert header["format"] == "youju-access-log" and header["day"] == "2026-09-28"
    assert [x["path"] for x in items] == ["/b", "/c"]
    assert set(items[0]) == {
        "id",
        "at",
        "logged_at",
        "service",
        "method",
        "path",
        "status",
        "ms",
        "ip",
        "uid",
        "detail",
    }
    _, items, _ = read_archive((tmp_path / "access-20260927.jsonl.gz").read_bytes())
    assert [x["path"] for x in items] == ["/a"]
    assert [r.path for r in rows(session)] == ["/today"]
    # 再跑一次没有要归档的
    assert run_archive(db_engine, None, output=tmp_path, now=NOW).days == {}


def test_yesterday_waits_until_ten_minutes_after_midnight(session, db_engine, tmp_path):
    add(session, NOW - timedelta(hours=1))  # 28 日 23:30
    early = NOW - timedelta(minutes=25)  # 29 日 0:05
    assert run_archive(db_engine, None, output=tmp_path, now=early).days == {}
    assert run_archive(db_engine, None, output=tmp_path, now=NOW).days == {date(2026, 9, 28): 1}


def test_archive_uploads_and_prunes_expired_archives(session, db_engine):
    add(session, NOW - timedelta(hours=1))
    client, seen, prefix = fake_cloud()
    report = run_archive(db_engine, "prod-1", client=client, now=NOW)
    assert report.days == {date(2026, 9, 28): 1} and report.bytes > 0
    assert b"logs/access-20260928.jsonl.gz" in seen["cos"]
    # 删掉 191–200 天前的
    assert seen["deleted"][0] == prefix + object_path(date(2026, 9, 29) - timedelta(days=191))
    assert seen["deleted"][-1] == prefix + object_path(date(2026, 9, 29) - timedelta(days=200))
    assert len(seen["deleted"]) == 10 and report.pruned == 2
    assert rows(session) == []


def test_failed_upload_keeps_rows_for_next_time(session, db_engine):
    add(session, NOW - timedelta(hours=1))
    client, _, _ = fake_cloud(cos_status=403)
    with pytest.raises(BackupError, match="上传访问日志归档失败"):
        run_archive(db_engine, "prod-1", client=client, now=NOW)
    assert len(rows(session)) == 1


def test_export_day_is_deterministic(session, db_engine):
    add(session, NOW - timedelta(hours=1))
    first, second = io.BytesIO(), io.BytesIO()
    assert export_day(db_engine, date(2026, 9, 28), first)[0] == 1
    export_day(db_engine, date(2026, 9, 28), second)
    assert first.getvalue() == second.getvalue()


def test_backup_skips_access_logs_and_restore_leaves_them(session, db_engine, tmp_path):
    add(session, NOW)
    out = io.BytesIO()
    counts = dump(db_engine, out)
    assert "access_logs" not in counts and "app_keys" in counts
    # 恢复到别的库：那个库的访问日志暂存表有记录也不算"不是空库"
    from sqlalchemy import make_url, text

    from youju.migrations import upgrade

    url = make_url(TEST_DB_URL).set(database="youju_test_restore_logs")
    server = create_mysql_engine(url.set(database=""))
    with server.begin() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS youju_test_restore_logs"))
        conn.execute(text("CREATE DATABASE youju_test_restore_logs COLLATE utf8mb4_0900_bin"))
    target_url = url.render_as_string(hide_password=False)
    upgrade(target_url)
    target = create_mysql_engine(target_url)
    try:
        with target.begin() as conn:
            conn.execute(
                AccessLog.__table__.insert(),
                {
                    "at": NOW,
                    "service": "admin",
                    "method": "GET",
                    "path": "/admin",
                    "status": 200,
                    "ms": 1,
                },
            )
        restore(target, io.BytesIO(out.getvalue()))
        with target.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(AccessLog.__table__)) == 1
    finally:
        target.dispose()
        with server.begin() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS youju_test_restore_logs"))
        server.dispose()


def test_cli_archive_and_log_uid(session, db_engine, tmp_path, monkeypatch, capsys):
    from youju import cli

    settings = SimpleNamespace(database_url=TEST_DB_URL, cloud_env=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["archive-logs"]) == 1
    assert "YOUJU_CLOUD_ENV" in capsys.readouterr().out
    add(session, datetime.now(UTC) - timedelta(days=2))
    assert cli.main(["archive-logs", "--output", str(tmp_path)]) == 0
    assert "访问日志归档：" in capsys.readouterr().out
    assert len(list(tmp_path.glob("access-*.jsonl.gz"))) == 1
    assert cli.main(["log-uid", "dev-user"]) == 0
    assert capsys.readouterr().out.strip() == log_uid("dev-user")


def test_late_rows_go_to_the_day_they_are_written(session, db_engine, tmp_path):
    """写库失败后补写的记录按写进库的时间归档：昨天已经归档过，不会再覆盖昨天的文件。"""
    add(session, NOW - timedelta(hours=1), "/early")  # 28 日 23:30 请求、当时写进库
    assert run_archive(db_engine, None, output=tmp_path, now=NOW).days == {date(2026, 9, 28): 1}
    # 28 日 23:50 的请求，29 日 0:20 才补写进库
    add(session, NOW - timedelta(minutes=40), "/late", logged_at=NOW - timedelta(minutes=10))
    later = NOW + timedelta(days=1)
    assert run_archive(db_engine, None, output=tmp_path, now=later).days == {date(2026, 9, 29): 1}
    _, items, _ = read_archive((tmp_path / "access-20260928.jsonl.gz").read_bytes())
    assert [x["path"] for x in items] == ["/early"]
    _, items, _ = read_archive((tmp_path / "access-20260929.jsonl.gz").read_bytes())
    assert [x["path"] for x in items] == ["/late"]
