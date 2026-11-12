"""首发前搬家（D-127）：云托管上已下架的网址带到本机——清单格式、拦截表、整组下架、按网址挡住
重新收录、核对。"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from youju import cli, takedown_carry
from youju.config import get_settings
from youju.crawler.listing import ListItem
from youju.crawler.service import save_items
from youju.models import Document, DocumentMerge, DocumentTakedownEvent, Source, TakedownBlock
from youju.takedown import is_taken_down

NOW = datetime(2026, 11, 11, 8, tzinfo=UTC)


def _source(session):
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
    return src


def _doc(session, src, n, title="辽宁省科技项目申报通知"):
    doc = Document(
        source_id=src.id,
        url=f"https://example.gov.cn/{n}.html",
        url_key=f"example.gov.cn/{n}.html",
        title=title,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    return doc


def _line(key, **changes):
    row = {
        "url_key": key,
        "origin_source_id": 7,
        "taken_down_at": "2026-10-20T08:00:00+00:00",
        "reason": "侵权投诉",
    }
    row.update(changes)
    return json.dumps(row, ensure_ascii=False)


def test_parse_keeps_only_the_four_fields():
    items = takedown_carry.parse(
        [
            _line("example.gov.cn/1.html"),
            "",
            _line("example.gov.cn/1.html", reason="其他"),  # 同一个网址只留第一条
            _line("example.gov.cn/2.html", reason="不认识的类型", origin_source_id=None),
        ]
    )
    assert [(i.url_key, i.reason, i.origin_source_id) for i in items] == [
        ("example.gov.cn/1.html", "侵权投诉", 7),
        ("example.gov.cn/2.html", "其他", None),
    ]
    assert items[0].taken_down_at == datetime(2026, 10, 20, 8, tzinfo=UTC)
    # 备注、操作人、标题可能写到投诉人或个人信息：清单里出现就整份拒绝
    for bad in (
        _line("example.gov.cn/3.html", note="投诉人某某"),
        _line("example.gov.cn/3.html", operated_by="管理员"),
        _line("", reason="其他"),
        _line("example.gov.cn/3.html", taken_down_at="昨天"),
        _line("example.gov.cn/3.html", origin_source_id="7"),
        "[1, 2]",
        "不是 JSON",
    ):
        with pytest.raises(takedown_carry.CarryError):
            takedown_carry.parse([bad])


def test_apply_blocks_urls_and_takes_down_whole_groups(session):
    src = _source(session)
    master, twin, other = _doc(session, src, 1), _doc(session, src, 2), _doc(session, src, 3)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, active=True, match_key="k")
    )
    session.commit()
    # 清单里是归并组的副本（本机收进来的那份），另一个网址本机还没抓到
    items = takedown_carry.parse([_line(twin.url_key), _line("example.gov.cn/9.html")])

    report = takedown_carry.apply(session, items)
    assert (report.listed, report.blocks_added, report.found) == (2, 2, 1)
    assert (report.taken_down, report.already_hidden) == (1, 0)
    assert is_taken_down(session, master.id) and is_taken_down(session, twin.id)
    assert not is_taken_down(session, other.id)
    events = session.scalars(select(DocumentTakedownEvent).order_by(DocumentTakedownEvent.id)).all()
    assert {e.document_id for e in events} == {master.id, twin.id}
    assert {(e.action, e.reason, e.operated_by) for e in events} == {
        ("takedown", "侵权投诉", takedown_carry.OPERATOR)
    }
    blocks = session.scalars(select(TakedownBlock).order_by(TakedownBlock.url_key)).all()
    assert [b.url_key for b in blocks] == [twin.url_key, "example.gov.cn/9.html"]
    assert blocks[0].taken_down_at == datetime(2026, 10, 20, 8, tzinfo=UTC)
    assert blocks[0].origin_source_id == 7 and blocks[0].reason == "侵权投诉"

    # 重跑：不重复写拦截表、不重复记下架
    again = takedown_carry.apply(session, items)
    assert (again.blocks_added, again.taken_down, again.already_hidden) == (0, 0, 1)
    assert session.scalar(select(func.count()).select_from(DocumentTakedownEvent)) == 2


def test_crawler_does_not_collect_blocked_urls(session):
    src = _source(session)
    session.add(
        TakedownBlock(url_key="example.gov.cn/9.html", taken_down_at=NOW, reason="侵权投诉")
    )
    session.commit()
    items = [
        ListItem(
            title="辽宁省科技项目申报通知（已下架的）",
            url="https://example.gov.cn/9.html",
            publish_date=date(2026, 10, 1),
        ),
        # http、https 不同也算同一个规范网址
        ListItem(
            title="辽宁省科技项目申报通知（已下架的）",
            url="http://example.gov.cn/9.html",
            publish_date=date(2026, 10, 1),
        ),
        ListItem(
            title="辽宁省科技项目申报通知（新的）",
            url="https://example.gov.cn/10.html",
            publish_date=date(2026, 11, 1),
        ),
    ]
    assert save_items(session, src, items, baseline=False, seen_at=NOW) == 1
    session.commit()
    assert session.scalars(select(Document.url_key)).all() == ["example.gov.cn/10.html"]


def test_check_finds_still_visible_documents(session):
    src = _source(session)
    doc = _doc(session, src, 1)
    session.commit()
    items = takedown_carry.parse([_line(doc.url_key), _line("example.gov.cn/9.html")])
    before = takedown_carry.check(session, items)
    assert (before.listed, before.blocked, before.present, before.visible) == (
        2,
        0,
        1,
        [doc.url_key],
    )
    takedown_carry.apply(session, items)
    after = takedown_carry.check(session, items)
    assert (after.blocked, after.present, after.visible) == (2, 1, [])


def test_cli_apply_runs_only_on_the_public_master(tmp_path, monkeypatch, session):
    """服务器的公开数据由本机同步过来：apply 只在本机跑；check 两边都能跑，有可见的就退出码 1。"""
    src = _source(session)
    doc = _doc(session, src, 1)
    session.commit()
    listing = tmp_path / "takedowns.jsonl"
    listing.write_text(_line(doc.url_key) + "\n", encoding="utf-8")
    monkeypatch.setenv("YOUJU_HOSTING", "self")
    monkeypatch.setenv("YOUJU_PUBLIC_REPLICA", "true")
    get_settings.cache_clear()
    try:
        assert cli.main(["takedown-carry", "apply", "--file", str(listing)]) == 1
        assert cli.main(["takedown-carry", "check", "--file", str(listing)]) == 1
    finally:
        get_settings.cache_clear()
    monkeypatch.delenv("YOUJU_PUBLIC_REPLICA")
    get_settings.cache_clear()
    try:
        assert cli.main(["takedown-carry", "apply", "--file", str(listing)]) == 0
        assert cli.main(["takedown-carry", "check", "--file", str(listing)]) == 0
    finally:
        get_settings.cache_clear()
    session.commit()  # 结束测试会话自己的事务，读到命令行那边提交的结果
    assert is_taken_down(session, doc.id)
