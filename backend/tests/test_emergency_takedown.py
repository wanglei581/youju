"""服务器上的紧急下架（电脑不在线时，youju/emergency_takedown.py）：整组不对外、不拖慢公开查询、
缓存失效、同组加锁、撤销、记录不删、带回本机正式下架、只认带回的记录自动撤销、说明不外露、
命令行只在服务器上能下架。"""

import io
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects import mysql

from youju import cli, takedown_carry
from youju import emergency_takedown as urgent
from youju.api import advisor, exports, me, v1
from youju.api import app as admin
from youju.backup import SCOPE_USER, backed_up_tables
from youju.config import Settings, get_settings
from youju.db import get_session
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentMerge,
    DocumentTakedownEvent,
    EmergencyTakedown,
    ListEntry,
    Source,
    TakedownBlock,
)
from youju.public_snapshot import EXCLUDED_TABLES, PUBLIC_TABLES
from youju.sync.tables import SYNC_TABLES
from youju.takedown import available_condition, cache_revision, change_state, is_taken_down

NOW = datetime(2026, 11, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def settings(monkeypatch):
    value = Settings(_env_file=None, admin_token="emergency-test-only", cloud_env="test-env")
    for module in (admin, advisor, me, exports):
        monkeypatch.setattr(module, "get_settings", lambda: value)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    return value


@pytest.fixture
def client(session_factory, settings):
    def override():
        with session_factory() as s:
            yield s

    admin.app.dependency_overrides[get_session] = override
    with TestClient(admin.app) as c:
        yield c
    admin.app.dependency_overrides.clear()


@pytest.fixture
def docs(session):
    """0、1 是一组申报通知（1 归到 0），2、3 是一组名单（3 归到 2），4 单独一份。"""
    src = Source(
        level="省级",
        province="辽宁",
        dept="科技",
        dept_full_name="辽宁省科技厅",
        column_name="通知",
        url="https://example.gov.cn/tz",
    )
    session.add(src)
    session.flush()
    result = []
    for n, title in enumerate(
        [
            "辽宁省科技项目申报通知",
            "辽宁省科技项目申报通知",
            "辽宁省2026年科技型中小企业名单公示",
            "辽宁省2026年科技型中小企业名单公示",
            "辽宁省科技政策管理办法",
        ],
        1,
    ):
        doc = Document(
            source_id=src.id,
            url=f"https://example.gov.cn/{n}.html",
            url_key=f"example.gov.cn/{n}.html",
            title=title,
            publish_date=NOW.date(),
            first_seen_at=NOW + timedelta(minutes=1),
            last_seen_at=NOW + timedelta(minutes=1),
        )
        session.add(doc)
        session.flush()
        result.append(doc)
    for master, twin in ((result[0], result[1]), (result[2], result[3])):
        session.add(
            DocumentMerge(
                duplicate_id=twin.id, canonical_id=master.id, active=True, match_key=master.title
            )
        )
    session.add(
        DocumentDeadline(
            document_id=result[1].id,
            fingerprint="emergency-due",
            kind="企业申报截止",
            candidate_date=NOW.date() + timedelta(days=2),
            approved_date=NOW.date() + timedelta(days=2),
            evidence="截止原句",
            status="approved",
            confidence="高",
            reviewed_by="核对人",
            reviewed_at=NOW,
        )
    )
    session.add(
        ListEntry(
            document_id=result[3].id,
            source_url=result[3].url,
            name="辽宁创新科技有限公司",
            normalized_name="辽宁创新科技有限公司",
            category="企业",
            sequence="1",
        )
    )
    session.commit()
    return result


@pytest.mark.parametrize(
    "path,index",
    [
        ("/api/v1/home", 0),
        ("/api/v1/search", 0),
        ("/api/v1/search?kind=名单", 2),
        ("/api/v1/reports/deadlines", 0),
        ("/api/v1/reports/lists", 2),
        ("/api/v1/lists/recent", 2),
        ("/api/v1/lists/lookup?q=辽宁创新科技有限公司", 2),
    ],
)
def test_hides_the_whole_group_at_once_and_lift_brings_it_back(client, docs, session, path, index):
    target = docs[index]
    before = client.get(path)
    assert before.status_code == 200, before.text
    assert target.title in before.text
    # 下的是组里的副本；不清缓存：缓存按下架版本失效
    rows = urgent.add(session, docs[index + 1].id, reason="complaint", note="来电", operator="测试")
    assert sorted(r.document_id for r in rows) == sorted([target.id, docs[index + 1].id])
    hidden = client.get(path)
    assert hidden.status_code == 200, hidden.text
    assert target.title not in hidden.text
    for doc in (docs[index], docs[index + 1]):
        detail = client.get(f"/api/v1/documents/{doc.id}")
        assert detail.status_code == 404 and detail.json()["detail"] == "该文件已下架"
    assert client.get(f"/api/v1/documents/{docs[4].id}").status_code == 200
    assert urgent.lift(session, target.id, note="核实后不用下", operator="测试") == (2, True)
    assert target.title in client.get(path).text
    assert client.get(f"/api/v1/documents/{target.id}").status_code == 200


def test_public_filter_keeps_antijoins(session, docs):
    """两段各自一个 NOT IN，选出的值不会是 NULL：MySQL 都能改写成反连接，不逐行重跑
    （合成 UNION 时 EXPLAIN 是 DEPENDENT UNION）。"""
    urgent.add(session, docs[4].id, reason="other")
    stmt = select(Document.id).where(available_condition())
    sql = str(stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "UNION" not in sql.upper()
    plan = session.execute(text("EXPLAIN " + sql)).mappings().all()
    # documents_1 是正式下架那段的文件表，emergency_takedowns_1 是紧急下架那段的表：要么并进主查询
    # 做反连接，要么先算一次存起来（MATERIALIZED）；不能是每行重跑的 DEPENDENT
    parts = [row for row in plan if row["table"] in {"documents_1", "emergency_takedowns_1"}]
    assert len(parts) == 2, plan
    assert all(row["select_type"] in {"PRIMARY", "MATERIALIZED"} for row in parts), plan
    assert any("Not exists" in (row["Extra"] or "") for row in plan), plan
    assert not [row for row in plan if "UNION" in row["select_type"]]


def test_cache_revision_unchanged_without_rows_and_moves_on_each_change(session, docs):
    # 本机从来没有紧急下架：和改之前一样（没有下架记录时是空的）
    assert cache_revision(session) == ()
    urgent.add(session, docs[4].id, reason="error")
    added = cache_revision(session)
    assert added != ()
    urgent.add(session, docs[0].id, reason="other")
    second = cache_revision(session)
    assert second != added
    urgent.lift(session, docs[4].id)
    lifted = cache_revision(session)
    assert lifted not in (added, second)
    # 有了正式下架记录：前四项是原来的下架、归并版本，紧急下架的四项接在后面、没变
    change_state(session, docs[2].id, action="takedown", note="n", operator="o", reason="其他")
    formal = cache_revision(session)
    assert len(formal) == 8 and formal[4:] == lifted[4:] and formal[0] is not None


def test_cache_revision_follows_merges_and_deletes_with_only_emergency_rows(session, docs):
    """没有正式下架记录、只有紧急下架时：归并、拆组、文件被同步删掉都要让缓存失效。"""
    urgent.add(session, docs[0].id, reason="other")
    urgent.add(session, docs[4].id, reason="other")
    assert session.scalar(select(func.count()).select_from(DocumentTakedownEvent)) == 0
    start = cache_revision(session)
    # 本机把 2 归进 0 那组（同步过来）：2 也不对外了
    merge = DocumentMerge(
        duplicate_id=docs[2].id, canonical_id=docs[0].id, active=True, match_key="later"
    )
    session.add(merge)
    session.commit()
    assert is_taken_down(session, docs[2].id)
    merged = cache_revision(session)
    assert merged != start
    # 拆开：2 又对外
    merge.active = False
    session.commit()
    assert not is_taken_down(session, docs[2].id)
    split = cache_revision(session)
    assert split != merged
    # 本机删了 4（同步删除，编号置空），还没 settle：版本也要变
    session.execute(delete(Document).where(Document.id == docs[4].id))
    session.commit()
    assert cache_revision(session) != split


def _hold_doc(session_factory, doc):
    """另一个会话只锁住这一份、写了一行紧急下架还没提交（模拟同组另一份正在下架）。
    只锁这一份：add、lift 要是只锁自己那一份，就不会等它，测试会失败。"""
    first = session_factory()
    first.scalars(select(Document.id).where(Document.id == doc.id).with_for_update()).all()
    first.add(
        EmergencyTakedown(
            document_id=doc.id,
            url_key=doc.url_key,
            batch_id=999,
            reason="其他",
            note="",
            created_by="甲",
            created_at=datetime.now(UTC),
        )
    )
    first.flush()
    return first


def _in_thread(fn):
    result = {}

    def run():
        try:
            result["value"] = fn()
        except urgent.EmergencyError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_add_waits_for_the_same_group_and_then_refuses(session_factory, docs):
    first = _hold_doc(session_factory, docs[0])
    try:

        def second():
            with session_factory() as other:
                return len(urgent.add(other, docs[1].id, reason="other"))

        thread, result = _in_thread(second)
        time.sleep(1)
        assert thread.is_alive()  # 同组另一份：等前一个提交
        first.commit()
    finally:
        first.close()
    thread.join(30)
    assert not thread.is_alive() and "已经不对外" in result.get("error", "")


def test_lift_waits_for_an_add_in_flight_and_lifts_it_too(session_factory, session, docs):
    urgent.add(session, docs[0].id, reason="other")  # 0、1 各一行
    first = _hold_doc(session_factory, docs[1])
    try:

        def lifting():
            with session_factory() as other:
                return urgent.lift(other, docs[0].id, note="撤", operator="乙")

        thread, result = _in_thread(lifting)
        time.sleep(1)
        assert thread.is_alive()  # 正在提交的同组紧急下架：等它提交完一起撤
        first.commit()
    finally:
        first.close()
    thread.join(30)
    assert not thread.is_alive() and result.get("value") == (3, True)


def test_lock_rechecks_the_group_after_locking(session, docs, monkeypatch):
    """读归并和锁文件之间同步改了归并：锁上以后再读一遍，变了就放开重来，按最新的整组记行。"""
    real = urgent._members
    calls = []

    def members(sess, document_id):
        calls.append(document_id)
        # 第一次读到的是旧的组（0、1）；之后同步把 4 并进了 0 那组
        return [docs[0].id, docs[1].id] if len(calls) == 1 else real(sess, document_id)

    session.add(
        DocumentMerge(
            duplicate_id=docs[4].id, canonical_id=docs[0].id, active=True, match_key="later"
        )
    )
    session.commit()
    monkeypatch.setattr(urgent, "_members", members)
    rows = urgent.add(session, docs[0].id, reason="other")
    assert sorted(r.document_id for r in rows) == sorted([docs[0].id, docs[1].id, docs[4].id])
    assert len(calls) >= 3
    # 一直在变：试 3 次就停下，不写
    monkeypatch.setattr(
        urgent, "_members", lambda sess, document_id: [len(calls.append(1) or calls)]
    )
    with pytest.raises(urgent.EmergencyError, match="归并关系一直在变"):
        urgent.add(session, docs[2].id, reason="other")


def test_add_refuses_hidden_unknown_and_bad_input(session, docs):
    change_state(session, docs[2].id, action="takedown", note="n", operator="o", reason="其他")
    session.commit()
    with pytest.raises(urgent.EmergencyError, match="已经不对外"):
        urgent.add(session, docs[3].id, reason="complaint")  # 组里另一份正式下架着
    urgent.add(session, docs[1].id, reason="侵权投诉")
    with pytest.raises(urgent.EmergencyError, match="已经不对外"):
        urgent.add(session, docs[0].id, reason="complaint")  # 同组已经紧急下架
    with pytest.raises(urgent.EmergencyError, match="没有编号"):
        urgent.add(session, 999999, reason="complaint")
    with pytest.raises(urgent.EmergencyError, match="原因要是"):
        urgent.add(session, docs[4].id, reason="random")
    with pytest.raises(urgent.EmergencyError, match="说明最多"):
        urgent.add(session, docs[4].id, reason="other", note="长" * 301)
    with pytest.raises(urgent.EmergencyError, match="操作人最多"):
        urgent.add(session, docs[4].id, reason="other", operator="人" * 81)
    # "系统"只给自动撤销用
    with pytest.raises(urgent.EmergencyError, match="不能写"):
        urgent.add(session, docs[4].id, reason="other", operator=urgent.SYSTEM)
    rows = session.scalars(select(EmergencyTakedown).order_by(EmergencyTakedown.id)).all()
    assert [(r.document_id, r.url_key, r.reason, r.created_by) for r in rows] == [
        (docs[0].id, docs[0].url_key, "侵权投诉", urgent.OPERATOR),
        (docs[1].id, docs[1].url_key, "侵权投诉", urgent.OPERATOR),
    ]
    # 同一次的几行：同一时间、同一个编号（第一行的 id）
    assert rows[0].created_at == rows[1].created_at
    assert rows[0].batch_id == rows[1].batch_id == rows[0].id
    with pytest.raises(urgent.EmergencyError, match="不能写"):
        urgent.lift(session, docs[0].id, operator=urgent.SYSTEM)


def test_each_member_stays_hidden_after_the_group_splits(session, docs):
    """和正式下架一样每份各记一行：本机后来拆开这一组，原来同组的副本照样不对外。"""
    urgent.add(session, docs[1].id, reason="complaint")
    merge = session.scalars(select(DocumentMerge).where(DocumentMerge.duplicate_id == docs[1].id))
    merge.one().active = False
    session.commit()
    assert is_taken_down(session, docs[0].id) and is_taken_down(session, docs[1].id)
    # 拆开以后各撤各的
    assert urgent.lift(session, docs[0].id) == (1, True)
    assert is_taken_down(session, docs[1].id)


def test_lift_keeps_the_record_and_reports_formal_takedown(session, docs):
    rows = urgent.add(session, docs[1].id, reason="authority", note="主管部门来函", operator="甲")
    assert len(rows) == 2 and rows[0].lifted_at is None
    # 同组任一份都能撤；撤了以后再撤报错
    assert urgent.lift(session, docs[0].id, note="撤", operator="乙") == (2, True)
    with pytest.raises(urgent.EmergencyError, match="没有紧急下架着的"):
        urgent.lift(session, docs[0].id)
    kept = session.scalars(select(EmergencyTakedown)).all()
    assert {(r.lifted_by, r.lift_note, r.reason, r.note, r.created_by) for r in kept} == {
        ("乙", "撤", "主管部门要求", "主管部门来函", "甲")
    }
    assert all(r.lifted_at is not None for r in kept)
    # 撤销紧急下架不改正式下架：本机正式下架着的照样不对外
    urgent.add(session, docs[4].id, reason="other")
    change_state(session, docs[4].id, action="takedown", note="n", operator="o", reason="其他")
    assert urgent.lift(session, docs[4].id) == (1, False)
    listed = urgent.records(session, include_lifted=True)
    assert [r.document_id for r in listed] == [docs[4].id, docs[1].id, docs[0].id]
    assert urgent.records(session) == []
    text_ = "\n".join(urgent.describe_records(listed))
    # 一次下架的两行合成一条；说明默认不显示
    assert f"紧急下架 #{rows[0].batch_id}  文件 #{docs[0].id}、#{docs[1].id}" in text_
    assert "已撤销" in text_ and "撤  " not in text_
    assert "主管部门来函" not in text_ and "有说明（加 --notes 看）" in text_
    shown = "\n".join(urgent.describe_records(listed, notes=True))
    assert "主管部门来函" in shown and "撤" in shown


def test_deleting_the_document_keeps_the_record(session, docs):
    """本机删了文件（同步删除）：记录留着（编号置空、网址留着），不会把别的文件都当成下架，
    不带回本机，下一次 settle 自动撤销。"""
    urgent.add(session, docs[4].id, reason="other", note="说明")
    urgent.add(session, docs[1].id, reason="complaint")
    session.execute(delete(Document).where(Document.id == docs[4].id))
    session.commit()
    row = session.scalars(
        select(EmergencyTakedown).where(EmergencyTakedown.url_key == docs[4].url_key)
    ).one()
    assert row.document_id is None and row.lifted_at is None
    visible = session.scalars(
        select(Document.id).where(available_condition()).order_by(Document.id)
    ).all()
    assert visible == [docs[2].id, docs[3].id]
    exported = [json.loads(line)["url_key"] for line in urgent.export_lines(session)]
    assert exported == [docs[0].url_key, docs[1].url_key]
    assert urgent.settle(session) == 1
    session.refresh(row)
    assert (row.lifted_by, row.lift_note) == (urgent.SYSTEM, urgent.GONE_NOTE)
    text_ = "\n".join(urgent.describe_records(urgent.records(session, include_lifted=True)))
    assert f"已删（{docs[4].url_key}）" in text_ and urgent.GONE_NOTE in text_


def test_export_carries_back_without_blocks(session, docs):
    urgent.add(session, docs[1].id, reason="complaint", note="不带回本机的说明")
    urgent.add(session, docs[4].id, reason="error")
    urgent.add(session, docs[2].id, reason="other")
    urgent.lift(session, docs[2].id)  # 撤了的不带
    lines = urgent.export_lines(session)
    rows = [json.loads(line) for line in lines]
    assert sorted(r["url_key"] for r in rows) == sorted(
        [docs[0].url_key, docs[1].url_key, docs[4].url_key]
    )
    assert {r["reason"] for r in rows} == {"侵权投诉", "内容有误"}
    keys = {"url_key", "origin_source_id", "taken_down_at", "reason", "emergency"}
    assert all(set(r) == keys for r in rows)
    batch = {r["url_key"]: r["emergency"] for r in rows}
    assert batch[docs[0].url_key] == batch[docs[1].url_key] != batch[docs[4].url_key]
    assert "不带回本机的说明" not in "\n".join(lines)
    items = takedown_carry.parse(lines)
    assert len(items) == 3 and {i.emergency for i in items} == set(batch.values())
    # 本机没有紧急下架的行：清掉以后当作本机应用
    session.execute(delete(EmergencyTakedown))
    session.commit()
    report = takedown_carry.apply(
        session,
        items,
        block=False,
        note=takedown_carry.EMERGENCY_NOTE,
        operator=takedown_carry.emergency_operator,
    )
    assert (report.listed, report.found, report.blocks_added) == (3, 3, 0)
    # 0、1 一组：先遇到的那份整组下架，另一份已经不可见
    assert (report.taken_down, report.already_hidden) == (2, 1)
    assert session.scalar(select(func.count()).select_from(TakedownBlock)) == 0
    events = session.execute(
        select(DocumentTakedownEvent.document_id, DocumentTakedownEvent.operated_by)
    ).all()
    operator = f"{takedown_carry.EMERGENCY_OPERATOR}#"
    assert sorted(events) == sorted(
        (doc.id, operator + str(batch[doc.url_key])) for doc in (docs[0], docs[1], docs[4])
    )
    assert all(is_taken_down(session, doc.id) for doc in (docs[0], docs[1], docs[4]))


def test_settle_lifts_only_what_was_carried_back(session, docs):
    def formal(doc_id, action, operator, stamp=None):
        session.add(
            DocumentTakedownEvent(
                document_id=doc_id,
                action=action,
                reason="其他" if action == "takedown" else None,
                note="",
                operated_by=operator,
                operated_at=stamp or datetime.now(UTC),
            )
        )
        session.commit()

    def carried(row):
        return f"{takedown_carry.EMERGENCY_OPERATOR}#{row.batch_id}"

    # 4：第一次紧急下架导出了清单，后来撤了；又因为新的投诉紧急下架
    first = urgent.add(session, docs[4].id, reason="other")[0]
    urgent.lift(session, docs[4].id)
    old = datetime.now(UTC) - timedelta(days=1)
    formal(docs[4].id, "takedown", "服务器紧急下架", old)  # 不带编号的：不认
    pair = urgent.add(session, docs[1].id, reason="complaint")  # 0、1：带回本机整组下架
    urgent.add(session, docs[2].id, reason="complaint")  # 2、3：本机因为别的原因下过架又恢复
    second = urgent.add(session, docs[4].id, reason="complaint")[0]
    assert urgent.settle(session) == 0
    # 第一次的旧清单晚到、才在本机应用：带的是第一次的编号，撤不了第二次
    formal(docs[4].id, "takedown", carried(first))
    assert urgent.settle(session) == 0
    for doc in (docs[0], docs[1]):
        formal(doc.id, "takedown", carried(pair[0]))
    for doc in (docs[2], docs[3]):
        formal(doc.id, "takedown", "核对台")
        formal(doc.id, "restore", "核对台")
    assert urgent.settle(session) == 2
    rows = {
        r.document_id: r
        for r in session.scalars(select(EmergencyTakedown).where(EmergencyTakedown.id != first.id))
    }
    assert {d for d, r in rows.items() if r.lifted_at is not None} == {docs[0].id, docs[1].id}
    assert rows[docs[0].id].lift_note == urgent.SETTLE_NOTE
    assert rows[docs[0].id].lifted_by == urgent.SYSTEM
    # 本机别的原因下过架又恢复的、旧清单晚到的：紧急下架照样留着
    assert is_taken_down(session, docs[2].id) and is_taken_down(session, docs[4].id)
    assert rows[docs[4].id].id == second.id and rows[docs[4].id].lifted_at is None
    formal(docs[4].id, "takedown", carried(second))
    assert urgent.settle(session) == 1
    assert urgent.settle(session) == 0


def test_find_by_id_url_and_title(session, docs):
    urgent.add(session, docs[1].id, reason="complaint")
    by_id = urgent.find(session, str(docs[4].id))
    assert [(f.id, f.visible) for f in by_id] == [(docs[4].id, True)]
    by_url = urgent.find(session, "https://example.gov.cn:443/2.html#x")
    assert [(f.id, f.visible) for f in by_url] == [(docs[1].id, False)]
    # 不带 http:// 的网址也认
    assert [f.id for f in urgent.find(session, "example.gov.cn/5.html")] == [docs[4].id]
    by_title = urgent.find(session, "申报通知")
    assert [f.id for f in by_title] == [docs[1].id, docs[0].id]
    assert urgent.find(session, "100%_") == []  # 通配符当普通字
    assert urgent.find(session, "²") == []  # 不是 ASCII 数字：当标题里的字找，不出错
    with pytest.raises(urgent.EmergencyError):
        urgent.find(session, "  ")
    text_ = "\n".join(urgent.describe_found(by_title))
    assert "不对外" in text_ and docs[0].url in text_


def test_server_only_table_is_kept_out_of_sync_and_snapshot_but_backed_up():
    assert "emergency_takedowns" in EXCLUDED_TABLES
    assert "emergency_takedowns" not in PUBLIC_TABLES | SYNC_TABLES
    assert "emergency_takedowns" in {t.name for t in backed_up_tables(SCOPE_USER)}


def _replica(monkeypatch, on: bool) -> None:
    monkeypatch.setenv("YOUJU_HOSTING", "self")
    if on:
        monkeypatch.setenv("YOUJU_PUBLIC_REPLICA", "true")
    else:
        monkeypatch.delenv("YOUJU_PUBLIC_REPLICA", raising=False)
    get_settings.cache_clear()


def test_cli_changes_only_on_the_server(session, docs, monkeypatch, capsys):
    try:
        _replica(monkeypatch, False)
        assert cli.main(["emergency-takedown", "add", str(docs[4].id), "--reason", "other"]) == 1
        assert "只在服务器" in capsys.readouterr().out
        assert cli.main(["emergency-takedown", "find", "管理办法"]) == 0
        assert f"文件 #{docs[4].id}  对外可见" in capsys.readouterr().out
        _replica(monkeypatch, True)
        base = ["emergency-takedown"]
        for bad in ("abc", "²", "0"):
            assert cli.main([*base, "add", bad, "--reason", "other"]) == 1
            assert "正整数" in capsys.readouterr().out
        assert cli.main([*base, "add", str(docs[4].id), "--reason", "bad"]) == 1
        assert "没有做：原因要是" in capsys.readouterr().out
        # 说明从标准输入读（服务器脚本在提示里问）
        monkeypatch.setattr("sys.stdin", io.StringIO("来电 要求删除\n"))
        add = [*base, "add", str(docs[4].id), "--reason", "complaint", "--note-stdin"]
        assert cli.main([*add, "--by", "创始人"]) == 0
        assert "已紧急下架文件" in capsys.readouterr().out
        assert cli.main([*base, "list"]) == 0
        out = capsys.readouterr().out
        assert "侵权投诉" in out and "创始人" in out and "1 行" in out
        assert "来电" not in out  # 说明默认不显示
        assert cli.main([*base, "list", "--notes"]) == 0
        assert "来电 要求删除" in capsys.readouterr().out
        assert cli.main([*base, "export"]) == 0
        exported = capsys.readouterr().out.splitlines()
        assert [json.loads(line)["url_key"] for line in exported] == [docs[4].url_key]
        assert cli.main([*base, "settle"]) == 0
        assert "0 条" in capsys.readouterr().out
        monkeypatch.setattr("sys.stdin", io.StringIO("撤"))
        assert cli.main([*base, "lift", str(docs[4].id), "--note-stdin"]) == 0
        assert "现在对外可见" in capsys.readouterr().out
        assert cli.main([*base, "list", "--all"]) == 0
        assert "已撤销" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    session.commit()
    row = session.scalars(select(EmergencyTakedown)).one()
    assert (row.created_by, row.note, row.lifted_by, row.lift_note) == (
        "创始人",
        "来电 要求删除",
        urgent.OPERATOR,
        "撤",
    )


def _carry_line(key, emergency=7, **extra):
    row = {
        "url_key": key,
        "origin_source_id": 1,
        "taken_down_at": "2026-11-13T02:00:00+00:00",
        "reason": "侵权投诉",
        **extra,
    }
    if emergency is not None:
        row["emergency"] = emergency
    return json.dumps(row, ensure_ascii=False)


def test_carry_list_takes_only_a_positive_emergency_number():
    items = takedown_carry.parse([_carry_line("example.gov.cn/1.html", 12)])
    assert items[0].emergency == 12
    assert takedown_carry.parse([_carry_line("example.gov.cn/1.html", None)])[0].emergency is None
    for bad in (0, -1, "12", 1.5, True):
        with pytest.raises(takedown_carry.CarryError, match="紧急下架编号不对"):
            takedown_carry.parse([_carry_line("example.gov.cn/1.html", bad)])
    with pytest.raises(takedown_carry.CarryError, match="清单以外的字段"):
        takedown_carry.parse([_carry_line("example.gov.cn/1.html", 12, note="说明")])


def test_cli_carry_apply_emergency_origin_on_master(session, docs, tmp_path, monkeypatch, capsys):
    listing = tmp_path / "emergency-takedowns.jsonl"
    listing.write_text(
        _carry_line(docs[1].url_key, 7)
        + "\n"
        + _carry_line("example.gov.cn/missing.html", 7)
        + "\n",
        encoding="utf-8",
    )
    old = tmp_path / "old.jsonl"
    old.write_text(_carry_line(docs[4].url_key, None) + "\n", encoding="utf-8")
    try:
        _replica(monkeypatch, False)
        # 不带编号的（不是 emergency-carry 打印的）：不应用
        args = ["takedown-carry", "apply", "--file", str(old), "--origin", "emergency"]
        assert cli.main(args) == 1
        assert "每行要有紧急下架的编号" in capsys.readouterr().out
        args = ["takedown-carry", "apply", "--file", str(listing), "--origin", "emergency"]
        assert cli.main(args) == 0
        out = capsys.readouterr().out
        assert "这次整组下架 1 份" in out and "本机没有的 1 个" in out
    finally:
        get_settings.cache_clear()
    session.commit()
    assert is_taken_down(session, docs[0].id) and is_taken_down(session, docs[1].id)
    assert not is_taken_down(session, docs[4].id)
    assert session.scalar(select(func.count()).select_from(TakedownBlock)) == 0
    operators = set(session.scalars(select(DocumentTakedownEvent.operated_by)))
    assert operators == {f"{takedown_carry.EMERGENCY_OPERATOR}#7"}
