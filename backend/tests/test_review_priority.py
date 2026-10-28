"""核对台的 AI 预审：排序和标签、总览里的核对进度、AI 优先补跑快到期的候选。

红线：截止日期必须人工点"通过"才对外显示。这里每条路径都要验证不会把候选改成通过。
"""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju import cli
from youju.ai.hints import Hint, deadline_hint, order_key, review_rank
from youju.ai.pipeline import run_ai_checks
from youju.api import app as admin
from youju.api import v1
from youju.db import get_session
from youju.extract.service import extract_existing
from youju.models import AICheck, Document, DocumentDeadline, DocumentMerge, Source

from .test_ai_pipeline import add_doc, ai_item, fake_ai

HEADERS = {"X-Admin-Token": "secret"}
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TODAY = NOW.date()


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    admin.app.dependency_overrides[get_session] = override
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(role="all", admin_token="secret", advisor_enabled=False),
    )
    monkeypatch.setattr(v1, "now", lambda: NOW)
    with TestClient(admin.app) as test_client:
        yield test_client
    admin.app.dependency_overrides.clear()


def _source(session) -> Source:
    src = session.scalar(select(Source))
    if src is None:
        src = Source(
            level="省级",
            province="北京",
            dept="科技",
            dept_full_name="北京市科学技术委员会",
            column_name="通知公告",
            url="https://kw.beijing.gov.cn/tz/",
            enabled=True,
            parser_config={},
        )
        session.add(src)
        session.flush()
    return src


def _doc(session, key: str) -> Document:
    doc = Document(
        source_id=_source(session).id,
        url=f"https://kw.beijing.gov.cn/{key}.html",
        url_key=f"kw.beijing.gov.cn/{key}.html",
        title=f"关于开展申报工作的通知（{key}）",
        publish_date=date(2026, 9, 1),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    return doc


def _candidate(session, key, due, *, kind="企业申报截止", evidence=None, origin="rule", **extra):
    doc = _doc(session, key)
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=key.ljust(64, "x"),
        kind=kind,
        candidate_date=due,
        evidence=evidence
        if evidence is not None
        else f"请于{due.year}年{due.month}月{due.day}日前申报",
        confidence="高",
        status=extra.pop("status", "pending"),
        origin=origin,
        **extra,
    )
    session.add(row)
    session.flush()
    return row


def _ai(session, row, found):
    """AI 对这条候选所在文件的截止抽取结果：found 是 [(日期, 时刻, 类型)]。"""
    session.add(
        AICheck(
            document_id=row.document_id,
            task="deadlines",
            model="fake",
            result=[
                {
                    "kind": kind,
                    "date": due.isoformat(),
                    "time": clock.strftime("%H:%M") if clock else None,
                    "evidence": row.evidence,
                    "confidence": "高",
                }
                for due, clock, kind in found
            ],
            checked_at=datetime.now(UTC),
        )
    )


@pytest.fixture
def queue(session):
    """各种情况的待核对候选（今天是 2026-09-28），返回 {名字: 候选编号}。"""
    d = date
    rows = {}
    agree = "agree"

    def add(name, due, ai=None, **kw):
        row = _candidate(session, name, due, **kw)
        if ai == agree:
            _ai(session, row, [(due, None, row.kind)])
        elif ai is not None:
            _ai(session, row, ai)
        rows[name] = row

    add("suggest", d(2026, 10, 3), agree)  # 30 天内、建议通过
    add("day30", d(2026, 10, 28), agree)  # 第 30 天、建议通过
    add("mismatch", d(2026, 10, 1), [(d(2026, 10, 9), None, "企业申报截止")])  # AI 没找到这一天
    add("kind", d(2026, 10, 2), [(d(2026, 10, 2), None, "材料报送截止")])  # 同一天、类型不同
    add("reopened", d(2026, 10, 4), agree, audit_state="reopened", audit_note="质检存疑")
    add("ai_only", d(2026, 10, 5), origin="ai")
    add("no_date_in_evidence", d(2026, 10, 10), agree, evidence="申报截止时间另行通知")
    add("unchecked", d(2026, 10, 20))
    add("day31", d(2026, 10, 29))  # 第 31 天
    add("later", d(2026, 11, 15), agree)  # 30 天以后，即使一致也不排到前面
    add("expired", d(2026, 9, 27), agree)  # 昨天，已过期
    add("checked_empty", d(2026, 10, 12), [])  # AI 查过、一条也没找到
    session.commit()
    return {name: row.id for name, row in rows.items()}


PRIORITY = [
    # 30 天内、建议通过：按截止日期
    "suggest",
    "day30",
    # 30 天内其余：按截止日期
    "mismatch",
    "kind",
    "reopened",
    "ai_only",
    "no_date_in_evidence",
    "checked_empty",
    "unchecked",
    # 30 天以后：按截止日期
    "day31",
    "later",
    # 已过期
    "expired",
]


def _list(client, **params):
    response = client.get("/api/admin/deadlines", headers=HEADERS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(session_factory):
    with session_factory() as s:
        return sorted(
            (r.id, r.status, r.approved_date, r.reviewed_by)
            for r in s.scalars(select(DocumentDeadline))
        )


def test_priority_order_and_labels(client, queue, session_factory) -> None:
    before = _snapshot(session_factory)
    items = _list(client, order="priority", due="all", limit=200)
    names = {v: k for k, v in queue.items()}
    assert [names[i["id"]] for i in items] == PRIORITY
    labels = {names[i["id"]]: (i["hint"], i["hint_text"]) for i in items}
    assert labels["suggest"] == ("suggest", "建议通过")
    assert labels["day30"] == ("suggest", "建议通过")
    assert labels["later"] == ("suggest", "建议通过")  # 标签照给，只是不排到前面
    assert labels["expired"] == ("suggest", "建议通过")
    assert labels["mismatch"] == ("mismatch", "规则与 AI 不一致")
    assert labels["checked_empty"] == ("mismatch", "规则与 AI 不一致")
    assert labels["kind"] == ("kind_mismatch", "类型不一致：AI 认为是材料报送截止")
    assert labels["reopened"][0] == "agree"  # 质检退回的必须人重新核对，不建议通过
    assert labels["no_date_in_evidence"] == ("agree", "AI 一致，但证据句里没写这个日期")
    assert labels["ai_only"] == ("ai_only", "仅 AI 找到")
    assert labels["unchecked"] == ("unchecked", "AI 未核")
    assert labels["day31"] == ("unchecked", "AI 未核")
    # 默认的 30 天内筛选：同样的顺序，只是没有 30 天以后和已过期的
    soon = _list(client, order="priority", due="30")
    assert [names[i["id"]] for i in soon] == PRIORITY[:9]
    # 7 天内（D-061，核对台的默认）：今天 9/28，10/5 及以前截止的，顺序不变
    week = _list(client, order="priority", due="7")
    assert [names[i["id"]] for i in week] == ["suggest", "mismatch", "kind", "reopened", "ai_only"]
    count = client.get("/api/admin/deadlines/count?due=7", headers=HEADERS).json()
    assert count == {"pending": 5}
    # 按编号的旧顺序照旧可用，也带标签
    by_id = _list(client, due="all", limit=200)
    assert [i["id"] for i in by_id] == sorted(queue.values(), reverse=True)
    assert {i["id"]: i["hint"] for i in by_id} == {i["id"]: i["hint"] for i in items}
    # 只读：列表不改任何候选
    assert _snapshot(session_factory) == before


def test_priority_paging_misses_nothing_while_reviewing(client, queue, session_factory) -> None:
    names = {v: k for k, v in queue.items()}
    seen, after = [], None
    while True:
        params = {"order": "priority", "due": "all", "limit": 2}
        page = _list(client, **params | ({"after": after} if after else {}))
        if not page:
            break
        seen += [names[i["id"]] for i in page]
        after = page[-1]["order_key"]
        # 别人同时核对掉了一条还没翻到的：后面的不漏、也不重复
        if len(seen) == 2:
            with session_factory() as s:
                row = s.get(DocumentDeadline, queue["unchecked"])
                row.status = "rejected"
                s.commit()
    assert seen == [name for name in PRIORITY if name != "unchecked"]


def test_paging_parameters_must_match_order(client, queue) -> None:
    for params in (
        {"order": "priority", "before_id": 5},
        {"order": "id", "after": "1|2026-10-01|000000000001"},
        {"order": "priority", "after": "1|2026-10-01"},
        {"order": "sideways"},
    ):
        response = client.get("/api/admin/deadlines", headers=HEADERS, params=params)
        assert response.status_code == 422, params


def test_twin_url_uses_ai_result_of_the_group(client, session) -> None:
    # 候选在同一通知的另一个网址上，AI 只查了主文件：照样按整组的 AI 结果判断
    master = _doc(session, "master")
    row = _candidate(session, "twin", date(2026, 10, 9))
    session.add(
        DocumentMerge(
            duplicate_id=row.document_id, canonical_id=master.id, active=True, match_key="k"
        )
    )
    session.add(
        AICheck(
            document_id=master.id,
            task="deadlines",
            model="fake",
            result=[{"kind": "企业申报截止", "date": "2026-10-09", "time": None}],
            checked_at=datetime.now(UTC),
        )
    )
    # AI 出错的记录不算查过
    other = _candidate(session, "errored", date(2026, 10, 9))
    session.add(
        AICheck(
            document_id=other.document_id,
            task="deadlines",
            model="fake",
            result=None,
            error="AI 服务暂时不可用",
            checked_at=datetime.now(UTC),
        )
    )
    session.commit()
    items = {i["id"]: i for i in _list(client, order="priority")}
    assert items[row.id]["hint"] == "suggest" and items[row.id]["document_id"] == master.id
    assert items[other.id]["hint"] == "unchecked"


def _row(**kw):
    base = {
        "id": 7,
        "origin": "rule",
        "kind": "企业申报截止",
        "candidate_date": date(2026, 10, 15),
        "candidate_time": None,
        "evidence": "请于2026年10月15日前申报",
        "audit_state": None,
    }
    return SimpleNamespace(**(base | kw))


def test_hint_edge_cases() -> None:
    found = [(date(2026, 10, 15), time(17, 0), "企业申报截止")]
    no_clock = [(date(2026, 10, 15), None, "企业申报截止")]
    # 一边没写时刻：日期一致，但不算同一刻，不建议直接通过（D-099 期 0）
    assert deadline_hint(_row(), found) == Hint("agree", "日期一致，但时刻只有一边写了")
    assert deadline_hint(_row(candidate_time=time(17, 0)), no_clock).code == "agree"
    # 时刻只有一边写了、类型也不同：按类型不一致提示
    other_kind = [(date(2026, 10, 15), time(17, 0), "材料报送截止")]
    assert deadline_hint(_row(), other_kind).code == "kind_mismatch"
    assert deadline_hint(_row(), no_clock).code == "suggest"
    assert deadline_hint(_row(candidate_time=time(17, 0)), found).code == "suggest"
    # 两边都写了时刻但不同：不一致
    assert deadline_hint(_row(candidate_time=time(12, 0)), found).code == "mismatch"
    # 证据句里写的是别的年份：不建议通过
    assert deadline_hint(_row(evidence="请于2025年10月15日前申报"), no_clock).code == "agree"
    # 证据句没写年份：月日对得上就算写着这个日期
    assert deadline_hint(_row(evidence="请于10月15日前申报"), no_clock).code == "suggest"
    assert deadline_hint(_row(evidence=""), no_clock).code == "agree"
    assert deadline_hint(_row(), None).code == "unchecked"
    assert deadline_hint(_row(), []).code == "mismatch"
    assert deadline_hint(_row(origin="ai"), found).code == "ai_only"
    # 无日期（数据库不允许，防御性处理）：不建议通过，排在最后
    assert deadline_hint(_row(candidate_date=None), found).code == "mismatch"
    suggest, other = Hint("suggest", ""), Hint("unchecked", "")
    assert review_rank(None, suggest, TODAY) == 4
    assert review_rank(date(2026, 9, 27), suggest, TODAY) == 3  # 昨天：已过期
    assert review_rank(TODAY, suggest, TODAY) == 0  # 今天截止：还在 30 天内
    assert review_rank(date(2026, 10, 28), suggest, TODAY) == 0
    assert review_rank(date(2026, 10, 28), other, TODAY) == 1
    assert review_rank(date(2026, 10, 29), suggest, TODAY) == 2
    keys = [
        order_key(_row(id=3, candidate_date=None), other, TODAY),
        order_key(_row(id=9, candidate_date=date(2026, 9, 1)), suggest, TODAY),
        order_key(_row(id=2, candidate_date=date(2026, 12, 1)), other, TODAY),
        order_key(_row(id=5, candidate_date=date(2026, 10, 2)), other, TODAY),
        order_key(_row(id=4, candidate_date=date(2026, 10, 2)), other, TODAY),
        order_key(_row(id=8, candidate_date=date(2026, 10, 9)), suggest, TODAY),
    ]
    assert sorted(keys) == [keys[5], keys[4], keys[3], keys[2], keys[1], keys[0]]
    assert keys[0] == "4|9999-12-31|000000000003"


def test_overview_shows_review_progress(client, session, queue) -> None:
    src_doc = session.get(DocumentDeadline, queue["later"]).document_id
    approved = _candidate(
        session,
        "approved",
        date(2026, 10, 6),
        status="approved",
        approved_date=date(2026, 10, 6),
        reviewed_by="创始人",
    )
    # 同一通知另一个网址上相同的已通过候选：只算一条
    twin_doc = _doc(session, "approved-copy")
    session.add(
        DocumentMerge(
            duplicate_id=twin_doc.id,
            canonical_id=approved.document_id,
            active=True,
            match_key="k",
        )
    )
    session.add(
        DocumentDeadline(
            document_id=twin_doc.id,
            fingerprint="approved-copy".ljust(64, "x"),
            kind=approved.kind,
            candidate_date=approved.candidate_date,
            evidence=approved.evidence,
            confidence="高",
            status="approved",
            approved_date=approved.approved_date,
            reviewed_by="创始人",
        )
    )
    _candidate(session, "rejected", date(2026, 10, 7), status="rejected", reviewed_by="创始人")
    _candidate(
        session,
        "changed",
        date(2026, 10, 8),
        status="changed",
        approved_date=date(2026, 10, 8),
        change_reason="延期",
        changed_to_document_id=src_doc,
    )
    session.commit()
    data = client.get("/api/admin/overview", headers=HEADERS).json()
    assert data["deadline_review"] == {
        "reviewed": 3,
        "approved": 1,
        "rejected": 1,
        "changed": 1,
        "pending": len(queue),
        "pending_30": 9,  # 今天到第 30 天
    }
    assert data["todos"]["deadlines"] == len(queue) and data["todos"]["deadlines_30"] == 9
    assert data["data"]["approved_deadlines"] == 1
    page = client.get("/admin").text
    assert "deadline_review" in page and "30 天内待审" in page


def test_ai_check_due_first_checks_soon_deadlines_before_new_files(session) -> None:
    now = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)  # 北京时间 9 月 28 日上午
    soon = add_doc(session, 1)  # 编号最小：按原来的顺序最后才轮到
    far = add_doc(session, 2, body="请各单位于2026年12月30日前完成网上申报。")
    newest = add_doc(session, 3, body="请各单位于2026年12月31日前完成网上申报。")
    extract_existing(session)
    client, _ = fake_ai(
        deadlines=[ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。")]
    )
    report = run_ai_checks(session, client, limit=1, now=now, due_first=30)
    assert report.documents == 1 and report.due_soon_documents == 1
    checked = {(c.document_id, c.task) for c in session.scalars(select(AICheck))}
    assert checked == {(soon.id, "kind"), (soon.id, "deadlines")}
    # 快到期的都补跑过了：同一参数再跑就照原来的顺序，新文件先查
    report = run_ai_checks(session, client, limit=1, now=now, due_first=30)
    assert report.due_soon_documents == 0
    assert {c.document_id for c in session.scalars(select(AICheck))} == {soon.id, newest.id}
    # 不带参数：和原来一样，新发现的优先
    report = run_ai_checks(session, client, limit=5, now=now)
    assert {c.document_id for c in session.scalars(select(AICheck))} == {soon.id, newest.id, far.id}
    # 只写了 AI 记录和"AI 是否一致"，候选都还是待核对
    rows = session.scalars(select(DocumentDeadline)).all()
    assert rows and all(r.status == "pending" and r.approved_date is None for r in rows)
    agreed = [r for r in rows if r.candidate_date == date(2026, 10, 15) and r.origin == "rule"]
    assert agreed and all(r.ai_agrees is True for r in agreed)


def test_ai_check_due_first_targets_master_of_twin_and_skips_expired(session) -> None:
    now = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)  # 10 月 15 日的已过期，10 月 20 日的还在 30 天内
    master = add_doc(session, 1, body="请各单位于2026年10月15日前完成网上申报。")
    twin = add_doc(session, 2, body="纸质材料于2026年10月20日前报送至科技厅。")
    expired = add_doc(session, 3, body="请各单位于2026年10月16日前完成网上申报。")
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="t")
    )
    session.commit()
    extract_existing(session, document_ids=[twin.id, expired.id])
    client, _ = fake_ai(deadlines=[])
    report = run_ai_checks(session, client, limit=1, now=now, due_first=30)
    # 候选在归并组的另一个网址上：查主文件；已过期的不优先（不然按编号新的先查，会先查它）
    assert report.due_soon_documents == 1
    assert {c.document_id for c in session.scalars(select(AICheck))} == {master.id}


def test_ai_check_without_key_skips_quietly(monkeypatch, capsys) -> None:
    import youju.config

    monkeypatch.setattr(
        youju.config, "get_settings", lambda: SimpleNamespace(ai_api_key=None, ai_model="m")
    )

    def no_database():
        raise AssertionError("没配密钥时不应连数据库")

    monkeypatch.setattr(cli, "session_factory", no_database)
    assert cli.main(["ai-check", "--limit", "600", "--due-first", "30"]) == 0
    assert "跳过 AI 检查" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["ai-check", "--due-first", "-1"])


def test_no_path_approves_a_candidate(client, session, session_factory) -> None:
    """AI 补跑、优先列表、总览，一路走下来没有一条候选被改成通过。"""
    add_doc(session)
    extract_existing(session)
    ai, _ = fake_ai(
        deadlines=[
            ai_item("2026-10-15", "请各单位于2026年10月15日前完成网上申报。"),
            ai_item("2026-10-20", "纸质材料于2026年10月20日前报送至科技厅。", label="材料报送截止"),
        ]
    )
    run_ai_checks(session, ai, now=datetime(2026, 9, 28, 2, 0, tzinfo=UTC), due_first=30)
    items = _list(client, order="priority", due="all")
    assert items and all(i["hint"] == "suggest" for i in items if i["origin"] == "rule")
    _list(client, due="all")
    assert client.get("/api/admin/overview", headers=HEADERS).status_code == 200
    with session_factory() as s:
        rows = s.scalars(select(DocumentDeadline)).all()
        assert rows and {r.status for r in rows} == {"pending"}
        assert all(r.approved_date is None and r.reviewed_by is None for r in rows)


def test_queue_lists_only_application_notices(client, session_factory) -> None:
    """D-061：被类型规则剔出申报通知的文件（职称、验收、征求意见等），候选不进核对队列；类型改回来就回来。"""
    with session_factory() as s:
        kept = _candidate(s, "apply", date(2026, 10, 3))
        other = _candidate(s, "other", date(2026, 10, 3))
        s.get(Document, other.document_id).kind_override = "其他"
        s.commit()
        kept_id, other_id, doc_id = kept.id, other.id, other.document_id
    ids = [i["id"] for i in _list(client, order="priority", due="7")]
    assert ids == [kept_id]
    assert client.get("/api/admin/deadlines/count?due=all", headers=HEADERS).json() == {
        "pending": 1
    }
    with session_factory() as s:
        s.get(Document, doc_id).kind_override = "申报通知"
        s.commit()
    ids = {i["id"] for i in _list(client, order="priority", due="7")}
    assert ids == {kept_id, other_id}
    # 只读：候选本身一直是待核对
    with session_factory() as s:
        assert s.get(DocumentDeadline, other_id).status == "pending"
