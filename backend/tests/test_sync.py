"""公开数据同步（本机 → 服务器）：两个真的 MySQL 库，本机是测试库，服务器另建一个。

本机库上的触发器、变更表只在这些测试里建，测试完删掉，不影响别的测试。
"""

import contextlib
import gzip
import hashlib
import io
import json
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
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
        conn.execute(text(f"DROP TABLE IF EXISTS `{sync_server.ORDER}`"))
        conn.execute(text(f"DROP TABLE IF EXISTS `{sync_server.PENDING}`"))
    # 清表把自增起点也清了：像服务器第一次收同步时一样，把服务器自己会新建行的表抬到编号下限
    sync_server._FLOORS_READY = False
    with engine.connect() as conn:
        sync_server.ensure_floors(conn)


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


def test_emergency_takedown_survives_updates_and_keeps_record_on_delete(env):
    """服务器上的紧急下架（只在服务器上、不同步）：本机改文件不动它、文件照样不对外；本机删了
    文件，记录留着（编号置空、网址留着），别的文件照常对外。"""
    from sqlalchemy.orm import Session

    from youju.models import Document
    from youju.takedown import available_condition

    src = source(env.local)
    doc = document(env.local, src, "a")
    other = document(env.local, src, "b")
    bootstrap(env)
    key = "kjt.shandong.gov.cn/tzgg/a.html"
    insert(
        env.server,
        "emergency_takedowns",
        document_id=doc,
        url_key=key,
        reason="侵权投诉",
        note="说明",
        created_by="测试",
        created_at=NOW,
    )

    def visible():
        with Session(env.server) as s:
            query = select(Document.id).where(available_condition()).order_by(Document.id)
            return s.scalars(query).all()

    assert visible() == [other]
    with env.local.begin() as conn:
        conn.execute(T["documents"].update().where(T["documents"].c.id == doc).values(title="改了"))
    local.push(env.local, env.client)
    # 改文件不动它（写用 REPLACE、关外键检查，不置空）
    assert rows(env.server, "emergency_takedowns")[0]["document_id"] == doc
    assert visible() == [other]
    with env.local.begin() as conn:
        conn.execute(T["documents"].delete().where(T["documents"].c.id == doc))
    local.push(env.local, env.client)
    kept = rows(env.server, "emergency_takedowns")
    assert [(r["document_id"], r["url_key"], r["note"]) for r in kept] == [(None, key, "说明")]
    assert visible() == [other]


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
    # reset 也排在同一把锁上：重放的旧 reset 不会把变更流改回去
    now[0] -= 5000
    monkeypatch.setattr(sync_tables, "new_stamp", stamp)
    with pytest.raises(local.SyncError) as err:
        other().json("POST", "/reset", {"stream": "old-stream-00000", "applied_seq": 0})
    assert "409" in str(err.value)
    assert env.client.json("GET", "/state")["stream"] == stream


def test_pending_and_upload_take_the_same_turn(env, monkeypatch, tmp_path):
    """取待补、收文件划掉待补，都和写入、清理排同一把锁（_take_turn）：查文件在不在到划掉之间不会
    被清理删掉文件、又被新的一批记进待补。"""
    turns: list[int] = []
    real = sync_server._take_turn

    def spy(conn, stamp):
        turns.append(stamp)
        return real(conn, stamp)

    monkeypatch.setattr(sync_server, "_take_turn", spy)
    server_store = storage.DiskBackend(tmp_path / "server")
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
    content = b"%PDF-1.4 turn"
    path = f"attachments/{hashlib.sha256(content).hexdigest()}.pdf"
    sync_server.ensure_state(env.server)
    with env.server.begin() as conn:
        conn.execute(
            text(f"INSERT INTO `{sync_server.PENDING}` (path, noted_at) VALUES (:p, NOW())"),
            {"p": path},
        )
    # 文件还没到：取待补时留着
    assert env.client.json("POST", "/blobs/pending", {"limit": 10})["pending"] == [path]
    assert turns == [0]
    env.client.call(
        "PUT",
        "/blobs",
        params={"path": path, "sha256": hashlib.sha256(content).hexdigest()},
        body=content,
        content_type="application/octet-stream",
    )
    assert turns == [0, 0]
    assert env.client.json("POST", "/blobs/pending", {"limit": 10})["pending"] == []


def test_server_id_floor_fits_the_key_column():
    """编号下限要放得进那一列（INT 有符号最大 2147483647），上面还得留够服务器自己用的。"""
    for name, floor in sync_tables.SERVER_ID_FLOOR.items():
        column = T[name].c.id
        assert isinstance(column.type, sa.Integer) and not isinstance(column.type, sa.BigInteger)
        assert floor + 100_000_000 < 2**31 - 1


def test_server_start_raises_the_id_floor_only_when_sync_is_on(env, monkeypatch):
    """配了同步密钥的服务器启动时就把编号下限抬好（管理台送核对生成的截止日期不会先拿到小编号）；
    没配同步的（云托管）启动时什么都不做。"""
    floor = sync_tables.SERVER_ID_FLOOR["document_deadlines"]

    def auto():
        with env.server.connect() as conn:
            conn.execute(text("SET SESSION information_schema_stats_expiry = 0"))
            return conn.scalar(
                text(
                    "SELECT AUTO_INCREMENT FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'document_deadlines'"
                )
            )

    def restart():
        with env.server.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            conn.execute(text("TRUNCATE TABLE document_deadlines"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        sync_server._FLOORS_READY = False
        with TestClient(app):
            pass

    monkeypatch.delenv("YOUJU_SYNC_SECRET", raising=False)
    get_settings.cache_clear()
    restart()
    assert auto() is None or auto() < floor
    monkeypatch.setenv("YOUJU_SYNC_SECRET", SECRET)
    get_settings.cache_clear()
    restart()
    assert auto() >= floor


def test_pushed_rows_never_overwrite_server_owned_rows(env):
    """服务器自己的行（用户线索生成的截止日期）从编号下限往上分；万一本机推来的行和它同编号，
    不写、报出来，不盖掉服务器的行。"""
    src = source(env.local)
    bootstrap(env)
    doc = document(env.local, src, "a")
    local.push(env.local, env.client)
    floor = sync_tables.SERVER_ID_FLOOR["document_deadlines"]
    user_row = insert(
        env.server,
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
    assert user_row >= floor
    # 硬造一个撞号的：本机推来一行编号和服务器自己的那一行一样
    clash = insert(
        env.local,
        "document_deadlines",
        id=user_row,
        document_id=doc,
        fingerprint="r" * 64,
        kind="企业申报截止",
        candidate_date=date(2026, 12, 2),
        evidence="原文",
        confidence="高",
        origin="rule",
        status="pending",
    )
    assert clash == user_row
    with pytest.raises(local.SyncError) as err:
        local.push(env.local, env.client)
    assert "409" in str(err.value) and "编号撞了" in str(err.value)
    kept = [r for r in rows(env.server, "document_deadlines") if r["id"] == user_row]
    assert kept and kept[0]["origin"] == "user"


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
    # 推之前查过"服务器上有"、随后被清理删掉了（这里跳过补传来模拟）：这一批不写（不留指向不存在
    # 文件的行），下一轮先补传再写
    again = insert(
        env.local,
        "attachment_copies",
        url_hash="g" * 64,
        url="https://kjt.shandong.gov.cn/b.pdf",
        declared_type="pdf",
        status="ready",
        file_type="pdf",
        file_id=f"cloud://prod-env.youju-self/{path}",
        size=len(content),
        sha256=digest,
    )
    real_send = local._send_blobs
    monkeypatch.setattr(local, "_send_blobs", lambda *a, **k: (0, []))
    with pytest.raises(local.SyncError) as err:
        local.push(env.local, env.client, store=local_store)
    assert "409" in str(err.value) and "不在服务器上" in str(err.value)
    assert not [r for r in rows(env.server, "attachment_copies") if r["id"] == again]
    monkeypatch.setattr(local, "_send_blobs", real_send)
    assert local.push(env.local, env.client, store=local_store).blobs == 1
    assert server_store.exists(path)
    assert [r["id"] for r in rows(env.server, "attachment_copies")] == [again]
    # 两边都没有文件的（本机也没有，比如云托管时期抓的、还没导进来的）：照写，不卡住队列，
    # 后面的变更照常推；服务器记进待补
    lost_content = b"%PDF-1.4 lost attachment"
    lost_digest = hashlib.sha256(lost_content).hexdigest()
    lost = f"attachments/{lost_digest}.pdf"

    def pending():
        with env.server.connect() as conn:
            return list(conn.scalars(text(f"SELECT path FROM `{sync_server.PENDING}`")))

    nowhere = insert(
        env.local,
        "attachment_copies",
        url_hash="f" * 64,
        url="https://kjt.shandong.gov.cn/c.pdf",
        declared_type="pdf",
        status="ready",
        file_type="pdf",
        file_id=f"cloud://prod-env.youju-self/{lost}",
        size=len(lost_content),
        sha256=lost_digest,
    )
    report = local.push(env.local, env.client, store=local_store)
    assert report.blobs == 0 and not server_store.exists(lost)
    assert nowhere in [r["id"] for r in rows(env.server, "attachment_copies")]
    assert pending() == [lost]
    with env.local.begin() as conn:
        conn.execute(
            T["attachment_copies"]
            .update()
            .where(T["attachment_copies"].c.id == nowhere)
            .values(attempts=2)
        )
    assert local.push(env.local, env.client, store=local_store).batches == 1
    # 以后本机有了这个文件：那一行没再变，下一次推之前照样补传，待补划掉
    lost_file = tmp_path / "lost.pdf"
    lost_file.write_bytes(lost_content)
    local_store.put_file(lost, lost_file)
    report = local.push(env.local, env.client, store=local_store)
    assert report.batches == 0 and report.blobs == 1
    assert server_store.exists(lost) and pending() == []
    # 首发前从云托管直接导进服务器的（不经本机）：下一次本机来取待补时划掉
    gone = f"attachments/{'e' * 64}.pdf"
    with env.server.begin() as conn:
        conn.execute(
            text(f"INSERT INTO `{sync_server.PENDING}` (path, noted_at) VALUES (:p, NOW())"),
            {"p": gone},
        )
    server_store.put_file(gone, lost_file)
    assert local.push(env.local, env.client, store=local_store).blobs == 0
    assert pending() == []
    # 待补按页取完：排在前面、一直缺的不挡后面的；本机那份内容坏了的跳过、留在待补里，
    # 不让这次推送停下（后面的变更照常推）
    monkeypatch.setattr(local, "PENDING_PAGE", 1)
    first = f"attachments/{'0' * 64}.pdf"  # 两边都没有，按路径排在最前
    good_content = b"%PDF-1.4 found later"
    good = f"attachments/{hashlib.sha256(good_content).hexdigest()}.pdf"
    broken = f"attachments/{'f' * 64}.pdf"  # 本机有，但内容和名字对不上
    with env.server.begin() as conn:
        for path in (first, good, broken):
            conn.execute(
                text(f"INSERT INTO `{sync_server.PENDING}` (path, noted_at) VALUES (:p, NOW())"),
                {"p": path},
            )
    good_file = tmp_path / "good.pdf"
    good_file.write_bytes(good_content)
    local_store.put_file(good, good_file)
    local_store.put_file(broken, lost_file)
    with env.local.begin() as conn:
        conn.execute(
            T["attachment_copies"]
            .update()
            .where(T["attachment_copies"].c.id == nowhere)
            .values(attempts=3)
        )
    report = local.push(env.local, env.client, store=local_store)
    assert report.blobs == 1 and report.batches == 1
    assert server_store.exists(good) and not server_store.exists(broken)
    assert sorted(pending()) == sorted([first, broken])
    # 待补很多、前面一长串一直缺：照样翻到底，最后那个本机有的补传上去
    late_content = b"%PDF-1.4 at the very end"
    late = f"attachments/{hashlib.sha256(late_content).hexdigest()}.pdf"
    with env.server.begin() as conn:
        for i in range(1, 30):
            conn.execute(
                text(f"INSERT INTO `{sync_server.PENDING}` (path, noted_at) VALUES (:p, NOW())"),
                {"p": f"attachments/{'0' * 62}{i:02d}.pdf"},
            )
        conn.execute(
            text(f"INSERT INTO `{sync_server.PENDING}` (path, noted_at) VALUES (:p, NOW())"),
            {"p": late},
        )
    late_file = tmp_path / "late.pdf"
    late_file.write_bytes(late_content)
    local_store.put_file(late, late_file)
    assert local.push(env.local, env.client, store=local_store).blobs == 1
    assert server_store.exists(late) and late not in pending()
    # 这一批正好有指向本机坏文件的行：坏的不传、算"本机给不了"，服务器照写、记进待补，
    # 不卡住这一批和后面的变更
    rotten = f"attachments/{'a' * 64}.pdf"
    local_store.put_file(rotten, lost_file)  # 内容和名字对不上
    rotten_row = insert(
        env.local,
        "attachment_copies",
        url_hash="e" * 64,
        url="https://kjt.shandong.gov.cn/d.pdf",
        declared_type="pdf",
        status="ready",
        file_type="pdf",
        file_id=f"cloud://prod-env.youju-self/{rotten}",
        size=10,
        sha256="a" * 64,
    )
    report = local.push(env.local, env.client, store=local_store)
    assert report.batches == 1 and not server_store.exists(rotten)
    assert rotten_row in [r["id"] for r in rows(env.server, "attachment_copies")]
    assert rotten in pending()
    # 指向它的行删掉了：清理时待补也划掉，以后不会再补传一个没人用的文件
    with env.local.begin() as conn:
        conn.execute(
            T["attachment_copies"].delete().where(T["attachment_copies"].c.id == rotten_row)
        )
    local.push(env.local, env.client, store=local_store)
    assert rotten not in pending()

    # 本机查文件本身出错（本机用 s3 时网络或存储出错）：当作这一轮给不了，这一批照常写
    class Flaky:
        def __getattr__(self, name):
            return getattr(local_store, name)

        def exists(self, path):
            if path == good:
                raise storage.StorageError("网络断了")
            return local_store.exists(path)

    good_row = insert(
        env.local,
        "attachment_copies",
        url_hash="d" * 64,
        url="https://kjt.shandong.gov.cn/e.pdf",
        declared_type="pdf",
        status="ready",
        file_type="pdf",
        file_id=f"cloud://prod-env.youju-self/{good}",
        size=len(good_content),
        sha256=hashlib.sha256(good_content).hexdigest(),
    )
    report = local.push(env.local, env.client, store=Flaky())
    assert report.batches == 1
    assert good_row in [r["id"] for r in rows(env.server, "attachment_copies")]
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
    # 本机的默认周报方案改过（不是迁移写的原样）：不当种子行清掉，拒绝导入，免得丢
    with db_engine.begin() as conn:
        conn.execute(
            T["digest_profiles"]
            .update()
            .where(T["digest_profiles"].c.name == "全国版")
            .values(keywords=["自己加的"])
        )
    buf.seek(0)
    with pytest.raises(local.SyncError, match="默认数据改过"):
        local.import_export(db_engine, buf)
    with db_engine.begin() as conn:
        conn.execute(
            T["digest_profiles"]
            .update()
            .where(T["digest_profiles"].c.name == "全国版")
            .values(keywords=[])
        )
    # 结束行后面还有东西、gzip 校验不过（文件尾损坏）：都不导
    whole = gzip.decompress(buf.getvalue())
    with pytest.raises(local.SyncError, match="结束行后面还有内容"):
        local.import_export(db_engine, io.BytesIO(gzip.compress(whole + b'{"t": "x"}\n')))
    damaged = bytearray(buf.getvalue())
    damaged[-6] ^= 0xFF  # 改坏 CRC
    with pytest.raises(local.SyncError, match="gzip 核对没过"):
        local.import_export(db_engine, io.BytesIO(bytes(damaged)))
    assert count(db_engine, "documents") == 0
    # 导到一半出错（文件不完整）：导数据的事务整个回滚，表还是空的（种子行也还在）；自增起点在导数据
    # 之前、空表上设的，不会出现"数据进去了、重跑又说表不空"的卡住；用好的文件重跑照常
    lines = gzip.decompress(buf.getvalue()).splitlines(keepends=True)
    broken = io.BytesIO(gzip.compress(b"".join(lines[:-2])))
    with pytest.raises(local.SyncError, match="不完整"):
        local.import_export(db_engine, broken)
    assert count(db_engine, "documents") == 0 and count(db_engine, "digest_profiles") == 1
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
    # 服务器上用户线索生成的那一行从编号下限往上分；本机的起点只按公开的那部分算，两段不重叠
    floor = sync_tables.SERVER_ID_FLOOR["document_deadlines"]
    with server_engine.connect() as conn:
        user_ids = list(conn.scalars(text("SELECT id FROM document_deadlines")))
    assert user_ids and min(user_ids) >= floor and auto < floor
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


# ---- D-129（11/12）：服务器多久没收到本机的同步，超过 3 小时提醒 ----


def test_state_request_is_recorded_as_contact(env):
    """本机每次推送都先取 /state（有没有变更都一样）：验过签名的才记一次"来联系过"。"""
    assert sync_server.last_contact(env.server) is None  # 还没有进度表
    before = datetime.now(UTC).replace(microsecond=0)
    env.client.json("GET", "/state")
    with env.server.connect() as conn:
        names = set(conn.execute(text(f"SELECT name FROM `{sync_server.STATE}`")).scalars())
    assert names == {sync_server.CONTACT}  # 进度那一行照旧只有起步以后才有
    last = sync_server.last_contact(env.server)
    assert last is not None and last >= before - timedelta(seconds=1)
    # 没签名的请求不算
    with env.server.begin() as conn:
        conn.execute(text(f"DELETE FROM `{sync_server.STATE}`"))
    assert TestClient(app).get("/api/sync/v1/state").status_code == 401
    with env.server.connect() as conn:
        assert conn.scalar(text(f"SELECT COUNT(*) FROM `{sync_server.STATE}`")) == 0


def _contact_ago(engine, hours):
    when = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE `{sync_server.STATE}` SET updated_at = :u WHERE name = :n"),
            {"u": when, "n": sync_server.CONTACT},
        )


def test_sync_watch_alerts_after_three_hours_once_per_outage(env, monkeypatch, tmp_path, capsys):
    from youju import alerts, cli

    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda text: sent.append(text) or True)
    monkeypatch.setattr(cli, "SYNC_WATCH_STATE", tmp_path / "sync-watch.json")
    monkeypatch.setattr(cli, "_sync_engine", lambda: create_mysql_engine(env.server.url))

    # 不收同步的机器（云托管、本机）：跳过
    assert cli.main(["sync-watch"]) == 0
    assert "跳过" in capsys.readouterr().out

    monkeypatch.setenv("YOUJU_HOSTING", "self")
    monkeypatch.setenv("YOUJU_PUBLIC_REPLICA", "true")
    get_settings.cache_clear()
    # 还没起步：不提醒
    assert cli.main(["sync-watch"]) == 0
    assert "还没收到过" in capsys.readouterr().out and sent == []

    env.client.json("GET", "/state")
    assert cli.main(["sync-watch"]) == 0
    assert "分钟前" in capsys.readouterr().out and sent == []

    # 4 小时没来：提醒一次，退出码 20（job.sh 不再重复提醒）
    _contact_ago(env.server, 4)
    assert cli.main(["sync-watch"]) == 20
    assert len(sent) == 1 and "4 小时没收到本机的同步" in sent[0] and "sync-status" in sent[0]
    # 同一次断开，6 小时内不再发
    assert cli.main(["sync-watch"]) == 20
    assert len(sent) == 1 and "6 小时内提醒过" in capsys.readouterr().out
    # 6 小时以后还没来：再提醒一次
    noted = json.loads((tmp_path / "sync-watch.json").read_text())
    noted["alerted_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    (tmp_path / "sync-watch.json").write_text(json.dumps(noted))
    assert cli.main(["sync-watch"]) == 20 and len(sent) == 2
    # 本机又来了：照常
    env.client.json("GET", "/state")
    assert cli.main(["sync-watch"]) == 0 and len(sent) == 2
    # 提醒发不出去：退出码 1，由 job.sh 发通用提醒
    _contact_ago(env.server, 5)
    (tmp_path / "sync-watch.json").unlink()
    monkeypatch.setattr(alerts, "send_alert", lambda text: None)
    assert cli.main(["sync-watch"]) == 1
    get_settings.cache_clear()
