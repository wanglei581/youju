"""服务器上的紧急下架（电脑不在线时，youju/emergency_takedown.py）：整组不对外、缓存失效、撤销、
记录不删、带回本机正式下架、本机接手后自动撤销、命令行只在服务器上能下架。"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

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
    urgent.add(session, docs[index + 1].id, reason="complaint", note="投诉人来电", operator="测试")
    hidden = client.get(path)
    assert hidden.status_code == 200, hidden.text
    assert target.title not in hidden.text
    for doc in (docs[index], docs[index + 1]):
        detail = client.get(f"/api/v1/documents/{doc.id}")
        assert detail.status_code == 404 and detail.json()["detail"] == "该文件已下架"
    assert client.get(f"/api/v1/documents/{docs[4].id}").status_code == 200
    count, visible = urgent.lift(session, target.id, note="核实后不用下", operator="测试")
    assert (count, visible) == (1, True)
    assert target.title in client.get(path).text
    assert client.get(f"/api/v1/documents/{target.id}").status_code == 200


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
    assert len(formal) == 8 and formal[4:] == lifted and formal[0] is not None


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
    rows = session.scalars(select(EmergencyTakedown)).all()
    assert [(r.document_id, r.reason, r.created_by) for r in rows] == [
        (docs[1].id, "侵权投诉", urgent.OPERATOR)
    ]


def test_lift_keeps_the_record_and_reports_formal_takedown(session, docs):
    row, ids = urgent.add(
        session, docs[1].id, reason="authority", note="主管部门来函", operator="甲"
    )
    assert sorted(ids) == sorted([docs[0].id, docs[1].id])
    assert row.created_at is not None and row.lifted_at is None
    # 同组任一份都能撤；撤了以后再撤报错
    assert urgent.lift(session, docs[0].id, note="撤", operator="乙") == (1, True)
    with pytest.raises(urgent.EmergencyError, match="没有紧急下架着的"):
        urgent.lift(session, docs[0].id)
    kept = session.scalars(select(EmergencyTakedown)).one()
    assert (kept.lifted_by, kept.lift_note, kept.reason, kept.note, kept.created_by) == (
        "乙",
        "撤",
        "主管部门要求",
        "主管部门来函",
        "甲",
    )
    assert kept.lifted_at is not None
    # 撤销紧急下架不改正式下架：本机正式下架着的照样不对外
    urgent.add(session, docs[4].id, reason="other")
    change_state(session, docs[4].id, action="takedown", note="n", operator="o", reason="其他")
    assert urgent.lift(session, docs[4].id) == (1, False)
    listed = urgent.records(session, include_lifted=True)
    assert [r.document_id for r in listed] == [docs[4].id, docs[1].id]
    assert urgent.records(session) == []
    text = "\n".join(urgent.describe_records(listed))
    assert "已撤销" in text and "主管部门来函" in text


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
    text = "\n".join(urgent.describe_records(urgent.records(session, include_lifted=True)))
    assert f"文件已删（网址 {docs[4].url_key}）" in text and "说明" in text


def test_export_carries_the_group_back_without_blocks(session, docs):
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
    assert all(set(r) == {"url_key", "origin_source_id", "taken_down_at", "reason"} for r in rows)
    assert "不带回本机的说明" not in "\n".join(lines)
    items = takedown_carry.parse(lines)
    assert len(items) == 3
    # 本机没有紧急下架的行：清掉以后当作本机应用
    session.execute(delete(EmergencyTakedown))
    session.commit()
    report = takedown_carry.apply(
        session,
        items,
        block=False,
        note=takedown_carry.EMERGENCY_NOTE,
        operator=takedown_carry.EMERGENCY_OPERATOR,
    )
    assert (report.listed, report.found, report.blocks_added) == (3, 3, 0)
    # 0、1 一组：先遇到的那份整组下架，另一份已经不可见
    assert (report.taken_down, report.already_hidden) == (2, 1)
    assert session.scalar(select(func.count()).select_from(TakedownBlock)) == 0
    events = session.execute(
        select(DocumentTakedownEvent.document_id, DocumentTakedownEvent.operated_by)
    ).all()
    assert sorted(events) == sorted(
        (doc.id, takedown_carry.EMERGENCY_OPERATOR) for doc in (docs[0], docs[1], docs[4])
    )
    assert all(is_taken_down(session, doc.id) for doc in (docs[0], docs[1], docs[4]))


def test_settle_lifts_only_what_the_master_took_over(session, docs):
    old = datetime.now(UTC) - timedelta(days=1)
    # 4：紧急下架之前本机下架过又恢复（旧记录），不算接手
    session.add(
        DocumentTakedownEvent(
            document_id=docs[4].id,
            action="takedown",
            reason="其他",
            note="",
            operated_by="o",
            operated_at=old,
        )
    )
    session.commit()
    urgent.add(session, docs[1].id, reason="complaint")  # 本机整组正式下架
    urgent.add(session, docs[2].id, reason="complaint")  # 本机正式下架后又恢复
    urgent.add(session, docs[4].id, reason="complaint")  # 本机没动
    assert urgent.settle(session) == 0
    change_state(session, docs[0].id, action="takedown", note="n", operator="o", reason="其他")
    change_state(session, docs[2].id, action="takedown", note="n", operator="o", reason="其他")
    change_state(session, docs[2].id, action="restore", note="n", operator="o")
    assert urgent.settle(session) == 2
    rows = {r.document_id: r for r in session.scalars(select(EmergencyTakedown))}
    assert rows[docs[1].id].lifted_by == urgent.SYSTEM
    assert rows[docs[2].id].lifted_by == urgent.SYSTEM
    assert rows[docs[4].id].lifted_at is None
    # 本机恢复上架的那组照本机：对外可见；本机正式下架的照样不可见；没接手的还紧急下架着
    assert not is_taken_down(session, docs[2].id)
    assert is_taken_down(session, docs[1].id) and is_taken_down(session, docs[4].id)
    assert urgent.settle(session) == 0


def test_find_by_id_url_and_title(session, docs):
    urgent.add(session, docs[1].id, reason="complaint")
    by_id = urgent.find(session, str(docs[4].id))
    assert [(f.id, f.visible) for f in by_id] == [(docs[4].id, True)]
    by_url = urgent.find(session, "https://example.gov.cn:443/2.html#x")
    assert [(f.id, f.visible) for f in by_url] == [(docs[1].id, False)]
    by_title = urgent.find(session, "申报通知")
    assert [f.id for f in by_title] == [docs[1].id, docs[0].id]
    assert urgent.find(session, "100%_") == []  # 通配符当普通字
    assert urgent.find(session, "²") == []  # 不是 ASCII 数字：当标题里的字找，不出错
    with pytest.raises(urgent.EmergencyError):
        urgent.find(session, "  ")
    text = "\n".join(urgent.describe_found(by_title))
    assert "不对外" in text and docs[0].url in text


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
        add = [*base, "add", str(docs[4].id), "--reason", "complaint", "--note", "来电"]
        assert cli.main([*add, "--by", "创始人"]) == 0
        assert "已紧急下架文件" in capsys.readouterr().out
        assert cli.main([*base, "list"]) == 0
        out = capsys.readouterr().out
        assert "侵权投诉" in out and "创始人" in out and "来电" in out and "1 条" in out
        assert cli.main([*base, "export"]) == 0
        exported = capsys.readouterr().out.splitlines()
        assert [json.loads(line)["url_key"] for line in exported] == [docs[4].url_key]
        assert cli.main([*base, "settle"]) == 0
        assert "0 条" in capsys.readouterr().out
        assert cli.main([*base, "lift", str(docs[4].id), "--note", "撤"]) == 0
        assert "现在对外可见" in capsys.readouterr().out
        assert cli.main([*base, "list", "--all"]) == 0
        assert "已撤销" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    session.commit()
    row = session.scalars(select(EmergencyTakedown)).one()
    assert (row.created_by, row.note, row.lifted_by, row.lift_note) == (
        "创始人",
        "来电",
        urgent.OPERATOR,
        "撤",
    )


def test_cli_carry_apply_emergency_origin_on_master(session, docs, tmp_path, monkeypatch, capsys):
    listing = tmp_path / "emergency-takedowns.jsonl"
    listing.write_text(
        json.dumps(
            {
                "url_key": docs[1].url_key,
                "origin_source_id": docs[1].source_id,
                "taken_down_at": "2026-11-13T02:00:00+00:00",
                "reason": "侵权投诉",
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "url_key": "example.gov.cn/missing.html",
                "origin_source_id": 1,
                "taken_down_at": "2026-11-13T02:00:00+00:00",
                "reason": "其他",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        _replica(monkeypatch, False)
        args = ["takedown-carry", "apply", "--file", str(listing), "--origin", "emergency"]
        assert cli.main(args) == 0
        out = capsys.readouterr().out
        assert "这次整组下架 1 份" in out and "本机没有的 1 个" in out
    finally:
        get_settings.cache_clear()
    session.commit()
    assert is_taken_down(session, docs[0].id) and is_taken_down(session, docs[1].id)
    assert session.scalar(select(func.count()).select_from(TakedownBlock)) == 0
    operators = set(session.scalars(select(DocumentTakedownEvent.operated_by)))
    assert operators == {takedown_carry.EMERGENCY_OPERATOR}
