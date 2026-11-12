"""公开数据同步（本机 → 服务器）：两个真的 MySQL 库，本机是测试库，服务器另建一个。

本机库上的触发器、变更表只在这些测试里建，测试完删掉，不影响别的测试。
"""

import contextlib
import gzip
import hashlib
import io
import json
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, make_url, select, text

from youju import storage
from youju.api.app import app
from youju.config import get_settings
from youju.db import create_mysql_engine
from youju.migrations import upgrade
from youju.models import Base
from youju.sync import local
from youju.sync import server as sync_server
from youju.sync import tables as sync_tables

from .conftest import TEST_DB_URL

SECRET = "z" * 40
NOW = datetime(2026, 11, 5, 8, tzinfo=UTC)
T = Base.metadata.tables


@pytest.fixture(scope="module")
def server_engine(db_engine):
    """服务器那边的库：和测试库同一个 MySQL，库名加 _srv，用迁移建表。"""
    url = make_url(TEST_DB_URL)
    name = f"{url.database}_srv"
    root = create_mysql_engine(url.set(database=""))
    with root.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        conn.execute(
            text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
        )
    server_url = url.set(database=name).render_as_string(hide_password=False)
    upgrade(server_url)
    engine = create_mysql_engine(server_url)
    yield engine
    engine.dispose()
    with root.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
    root.dispose()


def _clear(engine):
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in Base.metadata.sorted_tables:
            conn.execute(text(f"TRUNCATE TABLE `{t.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.execute(text(f"DROP TABLE IF EXISTS `{sync_server.STATE}`"))


@pytest.fixture
def env(session_factory, db_engine, server_engine, monkeypatch, tmp_path):
    """本机：测试库 + 触发器；服务器：_srv 库，经 TestClient 收同步。"""
    _clear(server_engine)
    with db_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS `{local.CHANGES}`"))
        conn.execute(text(f"DROP TABLE IF EXISTS `{local.META}`"))
    stream = local.setup(db_engine)
    monkeypatch.setenv("YOUJU_SYNC_SECRET", SECRET)
    get_settings.cache_clear()
    monkeypatch.setattr(sync_server, "get_engine", lambda: server_engine)
    # 服务器出错时按真实情况回 500（不把异常抛进测试）
    http = TestClient(app, raise_server_exceptions=False)
    client = local.Client("http://testserver", SECRET, http=http)
    yield SimpleNamespace(local=db_engine, server=server_engine, client=client, stream=stream)
    client.close()
    with db_engine.begin() as conn:
        for name in local.trigger_names():
            conn.execute(text(f"DROP TRIGGER IF EXISTS `{name}`"))
        conn.execute(text(f"DROP TABLE IF EXISTS `{local.CHANGES}`"))
        conn.execute(text(f"DROP TABLE IF EXISTS `{local.META}`"))
    get_settings.cache_clear()


def insert(engine, table, **values):
    with engine.begin() as conn:
        return conn.execute(T[table].insert().values(**values)).inserted_primary_key[0]


def source(engine, n=1):
    return insert(
        engine,
        "sources",
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name=f"通知公告{n}",
        url=f"https://kjt.shandong.gov.cn/tzgg{n}/",
        enabled=True,
        parser_config={"item_selector": "ul li"},
    )


def document(engine, source_id, slug, title="关于申报的通知"):
    doc = insert(
        engine,
        "documents",
        source_id=source_id,
        url=f"https://kjt.shandong.gov.cn/tzgg/{slug}.html",
        url_key=f"kjt.shandong.gov.cn/tzgg/{slug}.html",
        title=title,
        publish_date=date(2026, 11, 1),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    insert(
        engine,
        "document_details",
        document_id=doc,
        body_text="请于2026年12月1日前申报。" * 20,
        fetched_at=NOW,
        status="ok",
        publish_date=date(2026, 11, 1),
    )
    return doc


def rows(engine, table):
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(select(T[table]).order_by(*T[table].primary_key)).mappings()
        ]


def count(engine, table):
    with engine.connect() as conn:
        return conn.scalar(select(func.count()).select_from(T[table]))


def bootstrap(env):
    """服务器第一次收：整表全推（本机现在有的都过去，进度设好）。"""
    return local.full_push(env.local, env.client)


def test_first_full_push_then_incremental_inserts_updates_deletes(env):
    src = source(env.local)
    doc = document(env.local, src, "a")
    report = bootstrap(env)
    assert report.upserts >= 3
    assert count(env.server, "documents") == 1 and count(env.server, "document_details") == 1
    state = env.client.json("GET", "/state")
    assert state["stream"] == env.stream

    # 改
    with env.local.begin() as conn:
        conn.execute(
            T["documents"].update().where(T["documents"].c.id == doc).values(title="新标题")
        )
    doc2 = document(env.local, src, "b")
    report = local.push(env.local, env.client)
    assert report.batches == 1 and report.upserts >= 3
    assert [r["title"] for r in rows(env.server, "documents")] == ["新标题", "关于申报的通知"]
    # 本机已推的变更删掉了
    with env.local.connect() as conn:
        assert conn.scalar(text(f"SELECT COUNT(*) FROM `{local.CHANGES}`")) == 0

    # 删：本机删文件，正文是外键级联删的（不触发触发器），服务器那边照样级联
    with env.local.begin() as conn:
        conn.execute(T["documents"].delete().where(T["documents"].c.id == doc2))
    report = local.push(env.local, env.client)
    assert report.deletes == 1
    assert [r["id"] for r in rows(env.server, "documents")] == [doc]
    assert [r["document_id"] for r in rows(env.server, "document_details")] == [doc]
    # 没有新变更：什么也不推
    assert local.push(env.local, env.client).batches == 0


def test_takedown_propagates_without_notes(env):
    src = source(env.local)
    doc = document(env.local, src, "a")
    bootstrap(env)
    with env.local.begin() as conn:
        conn.execute(
            T["documents"]
            .update()
            .where(T["documents"].c.id == doc)
            .values(taken_down_at=NOW, takedown_reason="rights", takedown_note="投诉人张三来电")
        )
        conn.execute(
            T["document_takedown_events"]
            .insert()
            .values(
                document_id=doc,
                action="takedown",
                reason="rights",
                note="投诉人张三来电",
                operated_by="创始人",
                operated_at=NOW,
            )
        )
    local.push(env.local, env.client)
    (srv,) = rows(env.server, "documents")
    assert srv["taken_down_at"] is not None and srv["takedown_note"] is None
    (event,) = rows(env.server, "document_takedown_events")
    assert event["action"] == "takedown" and event["note"] == ""


def test_link_gone_status_and_cleanup_propagate(env):
    """D-123（官方失效的通知 7 天后清理）：本机标"原网页已失效"、恢复、到期清理删掉，都经同步到服务器。
    服务器据此提示哪天清理；和下架（taken_down_at）分得开。"""
    src = source(env.local)
    doc = document(env.local, src, "a")
    keep = document(env.local, src, "b")
    bootstrap(env)

    def mark(doc_id, value):
        with env.local.begin() as conn:
            conn.execute(
                T["documents"]
                .update()
                .where(T["documents"].c.id == doc_id)
                .values(link_gone_at=value)
            )

    mark(doc, NOW)
    local.push(env.local, env.client)
    gone = {r["id"]: r for r in rows(env.server, "documents")}
    assert gone[doc]["link_gone_at"] is not None and gone[doc]["taken_down_at"] is None
    assert gone[keep]["link_gone_at"] is None
    # 又能打开了（清空失效时间）也过去
    mark(doc, None)
    local.push(env.local, env.client)
    assert all(r["link_gone_at"] is None for r in rows(env.server, "documents"))
    # 到期清理：本机删掉，服务器跟着删（正文级联）
    mark(doc, NOW)
    with env.local.begin() as conn:
        conn.execute(T["documents"].delete().where(T["documents"].c.id == doc))
    local.push(env.local, env.client)
    assert [r["id"] for r in rows(env.server, "documents")] == [keep]
    assert [r["document_id"] for r in rows(env.server, "document_details")] == [keep]


def test_user_data_survives_updates_and_follows_deletes(env):
    src = source(env.local)
    doc = document(env.local, src, "a")
    bootstrap(env)
    # 服务器上的用户数据指向这份文件：订阅（级联）、纠错（置空）
    user = insert(env.server, "users", openid="o-1")
    insert(
        env.server,
        "subscriptions",
        user_id=user,
        kind="deadline",
        template_id="t",
        document_id=doc,
        status="unused",
    )
    insert(
        env.server,
        "feedback",
        user_id=user,
        document_id=doc,
        content="日期不对",
        check_result="pass",
    )
    with env.local.begin() as conn:
        conn.execute(T["documents"].update().where(T["documents"].c.id == doc).values(title="改了"))
    local.push(env.local, env.client)
    # 改文件不动用户数据（写用 REPLACE、关外键检查，不级联）
    assert count(env.server, "subscriptions") == 1
    assert rows(env.server, "feedback")[0]["document_id"] == doc
    with env.local.begin() as conn:
        conn.execute(T["documents"].delete().where(T["documents"].c.id == doc))
    local.push(env.local, env.client)
    # 删文件照单库时的外键：订阅跟着删，纠错留着、指向置空
    assert count(env.server, "subscriptions") == 0
    assert rows(env.server, "feedback")[0]["document_id"] is None


def test_failed_batch_changes_nothing_and_resumes(env, monkeypatch):
    src = source(env.local)
    bootstrap(env)
    document(env.local, src, "a")
    real = sync_server.apply
    calls = []

    def broken(engine, payload, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("服务器写到一半出错")
        return real(engine, payload, **kw)

    monkeypatch.setattr(sync_server, "apply", broken)
    monkeypatch.setattr(local.clock, "sleep", lambda s: None)
    # 第一次 500：客户端重试，第二次写成
    report = local.push(env.local, env.client)
    assert report.batches == 1 and count(env.server, "documents") == 1
    # 不是服务器认的变更流（本机重建过、别的机器）：409，不写
    stale = {"stream": "other-stream-0000", "max_seq": 1, "tables": {}}
    with pytest.raises(local.SyncError) as err:
        env.client.json("POST", "/apply", stale)
    assert "409" in str(err.value)


def test_client_stamps_always_move_forward(monkeypatch):
    """同一个客户端里每次签的时间都比上一次大（两个请求落在同一毫秒、时钟往回调一点都一样）。"""
    client = local.Client("http://testserver", SECRET, http=TestClient(app))
    sent = []
    monkeypatch.setattr(sync_tables, "new_stamp", lambda: "1731400000000")
    real = sync_tables.signature

    def spy(secret, method, target, stamp, digest):
        sent.append(int(stamp))
        return real(secret, method, target, stamp, digest)

    monkeypatch.setattr(sync_tables, "signature", spy)
    for _ in range(3):
        with contextlib.suppress(local.SyncError):
            client.json("GET", "/state")
    assert sent == [1731400000000, 1731400000001, 1731400000002]


def test_stale_or_replayed_batches_are_refused(env, monkeypatch):
    """写入按请求签名里的时间（毫秒）排先后：比服务器写过的最新一批旧、或一样的（重放、卡在半路
    晚到的请求），增量和全推都不写。本机重试会重签新时间，照常写。"""
    src = source(env.local)
    bootstrap(env)
    document(env.local, src, "a")
    now = [int(time.time() * 1000)]
    sent: list[str] = []

    def stamp():
        now[0] += 1
        sent.append(str(now[0]))
        return sent[-1]

    monkeypatch.setattr(sync_tables, "new_stamp", stamp)
    local.push(env.local, env.client)
    applied = env.client.json("GET", "/state")["applied_seq"]
    stream = local.stream_of(env.local)

    # 卡在半路晚到、或别人抓去重放的旧请求（另一个客户端，5 秒前签的，签名还有效）：
    # 比刚才写的那批旧，不写
    def other():
        return local.Client(env.client.base, SECRET, http=env.client._http)

    now[0] -= 5000
    for body in (
        {"stream": stream, "max_seq": 999999, "tables": {}},
        {"full": True, "tables": {}},
    ):
        with pytest.raises(local.SyncError) as err:
            other().json("POST", "/apply", body)
        assert "409" in str(err.value) and "旧" in str(err.value)
    assert env.client.json("GET", "/state")["applied_seq"] == applied
    # 时间往前走过了上次写的：照常写
    now[0] += 10000
    env.client.json("POST", "/apply", {"stream": stream, "max_seq": applied + 1, "tables": {}})
    replay_stamp = sent[-1]  # 刚才写成的那一批的时间
    assert env.client.json("GET", "/state")["applied_seq"] == applied + 1
    # 同一个时间原样重放：不写
    monkeypatch.setattr(sync_tables, "new_stamp", lambda: replay_stamp)
    with pytest.raises(local.SyncError) as err:
        other().json("POST", "/apply", {"stream": stream, "max_seq": applied + 2, "tables": {}})
    assert "409" in str(err.value)
    assert env.client.json("GET", "/state")["applied_seq"] == applied + 1


def test_stream_mismatch_needs_full_push(env):
    source(env.local)
    env.client.json("POST", "/reset", {"stream": "someone-else-stream", "applied_seq": 5})
    with pytest.raises(local.SyncError) as err:
        local.push(env.local, env.client)
    assert "重新起步" in str(err.value)


def test_full_push_removes_rows_the_server_should_not_have(env):
    src = source(env.local)
    keep = document(env.local, src, "keep")
    # 服务器上多一份本机没有的（比如本机重建过）
    srv_src = source(env.server)
    document(env.server, srv_src, "stale")
    # 服务器上用户线索生成的截止日期（本机没有）：全推不能当多余的删掉
    user_deadline = insert(
        env.server,
        "document_deadlines",
        document_id=keep,
        fingerprint="u" * 64,
        kind="企业申报截止",
        candidate_date=date(2026, 12, 1),
        evidence="用户填的",
        confidence="低",
        origin="user",
        status="pending",
    )
    local.full_push(env.local, env.client)
    assert [r["id"] for r in rows(env.server, "documents")] == [keep]
    assert count(env.server, "document_details") == 1
    assert [r["id"] for r in rows(env.server, "document_deadlines")] == [user_deadline]


def test_signatures_are_required(env):
    raw = TestClient(app)
    assert raw.get("/api/sync/v1/state").status_code == 401
    wrong = local.Client("http://testserver", "w" * 40, http=TestClient(app))
    with pytest.raises(local.SyncError) as err:
        wrong.json("GET", "/state")
    assert "401" in str(err.value)
    empty = sync_tables.body_digest(b"")
    stamp = str(int((time.time() - 3600) * 1000))
    sig = sync_tables.signature(SECRET, "GET", "/api/sync/v1/state", stamp, empty)
    old = raw.get(
        "/api/sync/v1/state",
        headers={
            sync_tables.HEADER_TIME: stamp,
            sync_tables.HEADER_SIGNATURE: sig,
            sync_tables.HEADER_BODY: empty,
        },
    )
    assert old.status_code == 401
    # 时间按秒写（旧的写法，不是 13 位毫秒）：不认
    stamp = str(int(time.time()))
    sig = sync_tables.signature(SECRET, "GET", "/api/sync/v1/state", stamp, empty)
    seconds = raw.get(
        "/api/sync/v1/state",
        headers={
            sync_tables.HEADER_TIME: stamp,
            sync_tables.HEADER_SIGNATURE: sig,
            sync_tables.HEADER_BODY: empty,
        },
    )
    assert seconds.status_code == 401
    # 签名对、包体被换了：不认
    stamp = sync_tables.new_stamp()
    sig = sync_tables.signature(SECRET, "POST", "/api/sync/v1/apply", stamp, empty)
    swapped = raw.post(
        "/api/sync/v1/apply",
        content=b'{"full": true, "tables": {}}',
        headers={
            sync_tables.HEADER_TIME: stamp,
            sync_tables.HEADER_SIGNATURE: sig,
            sync_tables.HEADER_BODY: empty,
        },
    )
    assert swapped.status_code == 401


def test_sync_endpoints_are_404_without_secret(monkeypatch):
    monkeypatch.delenv("YOUJU_SYNC_SECRET", raising=False)
    get_settings.cache_clear()
    assert TestClient(app).get("/api/sync/v1/state").status_code == 404
    get_settings.cache_clear()


def test_unknown_tables_and_columns_are_refused(env):
    bootstrap(env)
    for tables in (
        {"users": {"upserts": [{"id": 1, "openid": "x"}]}},
        {"sources": {"upserts": [{"id": 1, "bogus": 1}]}},
        {"documents": {"deletes": [{"id": 1}]}},
    ):
        with pytest.raises(local.SyncError) as err:
            env.client.json("POST", "/apply", {"full": True, "tables": tables})
        assert "422" in str(err.value)


def test_gzip_bomb_is_refused(env):
    body = gzip.compress(b"0" * (sync_server.MAX_PLAIN + 1024))
    with pytest.raises(local.SyncError) as err:
        env.client.call(
            "POST", "/apply", body=body, content_type="application/json"
        ) if False else (_raw_gzip(env.client, body))
    assert "400" in str(err.value)


def _raw_gzip(client, payload):
    stamp = sync_tables.new_stamp()
    target = "/api/sync/v1/apply"
    digest = sync_tables.body_digest(payload)
    headers = {
        sync_tables.HEADER_TIME: stamp,
        sync_tables.HEADER_BODY: digest,
        sync_tables.HEADER_SIGNATURE: sync_tables.signature(SECRET, "POST", target, stamp, digest),
        "Content-Encoding": "gzip",
    }
    res = client._http.post(target, content=payload, headers=headers)
    raise local.SyncError(f"服务器回 {res.status_code}")


def test_attachment_files_follow_their_rows(env, monkeypatch, tmp_path):
    local_store = storage.DiskBackend(tmp_path / "local")
    server_store = storage.DiskBackend(tmp_path / "server")
    content = b"%PDF-1.4 test attachment"
    digest = hashlib.sha256(content).hexdigest()
    path = f"attachments/{digest}.pdf"
    src_file = tmp_path / "a.pdf"
    src_file.write_bytes(content)
    local_store.put_file(path, src_file)
    # 两边在同一个进程里：服务器那边换掉它模块里的 storage，本机这边把存储直接传给 push
    monkeypatch.setattr(
        sync_server,
        "storage",
        SimpleNamespace(
            backend=lambda *a: server_store,
            valid_path=storage.valid_path,
            path_of=storage.path_of,
            StorageError=storage.StorageError,
        ),
    )
    monkeypatch.setenv("YOUJU_STORAGE_BACKEND", "disk")
    monkeypatch.setenv("YOUJU_STORAGE_DIR", str(tmp_path / "server"))
    monkeypatch.setenv("YOUJU_CLOUD_ENV", "prod-env")
    get_settings.cache_clear()
    bootstrap(env)
    copy = insert(
        env.local,
        "attachment_copies",
        url_hash="h" * 64,
        url="https://kjt.shandong.gov.cn/a.pdf",
        declared_type="pdf",
        status="ready",
        file_type="pdf",
        file_id=f"cloud://prod-env.youju-self/{path}",
        size=len(content),
        sha256=digest,
    )
    report = local.push(env.local, env.client, store=local_store)
    assert report.blobs == 1 and server_store.exists(path)
    assert rows(env.server, "attachment_copies")[0]["id"] == copy
    # 再推一次：服务器已经有了，不重复传
    with env.local.begin() as conn:
        conn.execute(
            T["attachment_copies"]
            .update()
            .where(T["attachment_copies"].c.id == copy)
            .values(attempts=1)
        )
    assert local.push(env.local, env.client, store=local_store).blobs == 0
    # 本机删掉这份副本：服务器的文件没人用了，跟着删
    with env.local.begin() as conn:
        conn.execute(T["attachment_copies"].delete().where(T["attachment_copies"].c.id == copy))
    local.push(env.local, env.client, store=local_store)
    assert not server_store.exists(path)
    # 名字和内容对不上的不收
    bad = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(local.SyncError) as err:
        env.client.call(
            "PUT",
            "/blobs",
            params={"path": f"attachments/{bad}.pdf", "sha256": digest},
            body=content,
            content_type="application/octet-stream",
        )
    assert "422" in str(err.value)


def test_export_import_bootstrap(env, server_engine, db_engine):
    """切换时：服务器（从云托管整库恢复的）导出，本机导入；下架的照样过来但备注清空，
    用户线索生成的截止日期不过来，本机的自增起点不低于服务器的。"""
    src = source(server_engine)
    doc = document(server_engine, src, "a")
    hidden = document(server_engine, src, "hidden")
    with server_engine.begin() as conn:
        conn.execute(
            T["documents"]
            .update()
            .where(T["documents"].c.id == hidden)
            .values(taken_down_at=NOW, takedown_note="投诉人张三")
        )
    insert(
        server_engine,
        "document_deadlines",
        document_id=doc,
        fingerprint="u" * 64,
        kind="企业申报截止",
        candidate_date=date(2026, 12, 1),
        evidence="用户填的",
        confidence="低",
        origin="user",
        status="pending",
    )
    # 两边的库都是迁移建的：迁移 0013 写了一行默认的周报方案（种子行），导入时不能因为它就拒绝
    for engine in (server_engine, db_engine):
        insert(engine, "digest_profiles", name="全国版", provinces=[], keywords=[])
    buf = io.BytesIO()
    counts = local.export(server_engine, buf)
    assert counts["documents"] == 2 and counts["document_deadlines"] == 0
    # 本机：先清掉 env 建的触发器写进的东西，导入要求同步的表都是空的
    buf.seek(0)
    local.import_export(db_engine, buf)
    assert [r["takedown_note"] for r in rows(db_engine, "documents")] == [None, None]
    assert count(db_engine, "digest_profiles") == 1
    assert count(db_engine, "document_deadlines") == 0
    with db_engine.connect() as conn:
        auto = conn.scalar(
            text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'document_deadlines'"
            )
        )
    assert auto >= 2
    # 导入时触发器记下的变更：mark-synced 直接把服务器进度设到这里，不再推一遍
    seq = local.mark_synced(db_engine, env.client)
    assert seq > 0 and local.push(db_engine, env.client).batches == 0
    # 不是空表不能导入
    buf.seek(0)
    with pytest.raises(local.SyncError):
        local.import_export(db_engine, buf)


def test_missing_triggers_block_push(env):
    with env.local.begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS `sync_documents_ai`"))
    with pytest.raises(local.SyncError) as err:
        local.push(env.local, env.client)
    assert "sync-setup" in str(err.value)


def test_status_reports_differences(env):
    source(env.local)
    report = local.status(env.local, env.client)
    assert report["pending_changes"] >= 1 and report["missing_triggers"] == 0
    assert report["count_diff"] == {"sources": 1}
    assert json.dumps(report, ensure_ascii=False)


def test_replica_refuses_public_admin_writes(monkeypatch):
    monkeypatch.setenv("YOUJU_HOSTING", "self")
    monkeypatch.setenv("YOUJU_PUBLIC_REPLICA", "true")
    get_settings.cache_clear()
    client = TestClient(app)
    # 没带口令的照旧先被管理接口的口令检查挡下（401）；带了口令的公开数据修改一律 409
    assert client.post("/api/admin/takedown/1/remove", json={}).status_code == 401
    token = {"x-admin-token": "t" * 40}
    res = client.post("/api/admin/takedown/1/remove", json={}, headers=token)
    assert res.status_code == 409 and "本机" in res.json()["detail"]
    assert client.post("/api/admin/deadlines/1/approve", json={}, headers=token).status_code == 409
    # 纠错处理、发票只动用户数据：照常往下走（口令不对，是 401/403，不是 409）
    assert client.post("/api/admin/feedback/1/handle", json={}, headers=token).status_code != 409
    get_settings.cache_clear()


def test_late_commit_is_not_lost(env, db_engine):
    """序号小、提交晚的变更：推的时候还看不见，不能被丢掉（推完只删推过的序号）。"""
    src = source(env.local)
    bootstrap(env)
    slow = db_engine.connect()
    trx = slow.begin()
    slow.execute(
        T["documents"]
        .insert()
        .values(
            source_id=src,
            url="https://kjt.shandong.gov.cn/tzgg/slow.html",
            url_key="kjt.shandong.gov.cn/tzgg/slow.html",
            title="提交晚的",
            publish_date=date(2026, 11, 1),
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    fast = document(env.local, src, "fast", title="先提交的")  # 序号比 slow 的大，先提交
    local.push(env.local, env.client)
    assert [r["title"] for r in rows(env.server, "documents")] == ["先提交的"]
    trx.commit()
    slow.close()
    local.push(env.local, env.client)
    assert sorted(r["title"] for r in rows(env.server, "documents")) == ["先提交的", "提交晚的"]
    assert fast


def test_rebuilt_local_database_is_refused(env):
    """本机从备份恢复、重建过（自增编号落到服务器已有的以下）：再推会盖掉服务器上别的行，拦下。"""
    src = source(env.local)
    document(env.local, src, "a")
    document(env.local, src, "b")
    bootstrap(env)
    with env.local.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        conn.execute(text("DELETE FROM document_details"))
        conn.execute(text("DELETE FROM documents"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.execute(text("ALTER TABLE documents AUTO_INCREMENT = 1"))
    with pytest.raises(local.SyncError) as err:
        local.push(env.local, env.client)
    assert "比服务器旧" in str(err.value) and "重新起步" in str(err.value)
    with pytest.raises(local.SyncError):
        local.full_push(env.local, env.client)
    assert count(env.server, "documents") == 2  # 服务器一行都没动


def test_full_push_refuses_mass_deletes_without_force(env):
    src = source(env.local)
    for n in range(260):
        insert(
            env.local,
            "source_checks",
            source_id=src,
            started_at=NOW,
            finished_at=NOW,
            status="ok",
            items_found=n,
            items_new=0,
            duration_ms=1,
        )
    bootstrap(env)
    # 本机少了 250 行、变更又没记下来（模拟两边对不上）
    with env.local.begin() as conn:
        conn.execute(text("DELETE FROM source_checks WHERE id > 10"))
        conn.execute(text(f"DELETE FROM `{local.CHANGES}`"))
    report = local.full_push(env.local, env.client, dry_run=True)
    assert report.would_delete == {"source_checks": 250}
    assert count(env.server, "source_checks") == 260
    with pytest.raises(local.SyncError) as err:
        local.full_push(env.local, env.client)
    assert "--dry-run" in str(err.value) and "--force" in str(err.value)
    local.full_push(env.local, env.client, force=True)
    assert count(env.server, "source_checks") == 10


def test_only_one_push_at_a_time(env, db_engine):
    source(env.local)
    bootstrap(env)
    with db_engine.connect() as other:
        assert other.scalar(text("SELECT GET_LOCK('youju-sync-push', 0)")) == 1
        with pytest.raises(local.SyncError) as err:
            local.push(env.local, env.client)
        assert "正在推" in str(err.value)
        other.scalar(text("SELECT RELEASE_LOCK('youju-sync-push')"))
    local.push(env.local, env.client)
