from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from youju import deadline_excerpts as excerpts
from youju.models import Document, DocumentDeadline, DocumentDetail, Source

TODAY = date(2026, 10, 12)


@pytest.mark.parametrize(
    "quote",
    [
        "2026年10月20日",
        "10月20日",
        "2026-10-20",
        "2026.10.20",
        "2026/10/20",
        "２０２６年１０月２０日",
    ],
)
def test_date_written_in_quote(quote):
    assert excerpts.date_in_quote(date(2026, 10, 20), quote)
    assert not excerpts.date_in_quote(date(2026, 10, 21), quote)


def test_wrong_year_is_not_accepted():
    assert not excerpts.date_in_quote(date(2026, 10, 20), "截止时间为2027-10-20")


def test_batch_checks_current_body_conflicts_and_near_dates(session, monkeypatch):
    from zoneinfo import ZoneInfo

    from youju.api import v1

    monkeypatch.setattr(
        v1, "now", lambda: datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科技局",
        column_name="通知",
        url="https://example.gov.cn/",
        rights_level=1,
    )
    session.add(source)
    session.flush()
    cases = [
        ("有效", 8, "申报截止：２０２６年１０月２０日。", "申报截止：2026年10月20日。", None),
        ("临期", 7, "申报截止：10月19日。", "申报截止：10月19日。", None),
        ("原句已改", 9, "申报截止：10月21日。", "当前正文没有原句", None),
        ("年份错误", 8, "申报截止：2027年10月20日。", "申报截止：2027年10月20日。", None),
        ("冲突", 8, "申报截止：10月20日。", "申报截止：10月20日；10月21日。", 9),
        # D-061：机器摘录只在 8–30 天给日期
        ("第30天", 30, "申报截止：11月11日。", "申报截止：11月11日。", None),
        ("太远", 31, "申报截止：11月12日。", "申报截止：11月12日。", None),
    ]
    ids = {}
    for name, days, quote, body, second in cases:
        doc = Document(
            source_id=source.id,
            title=f"{name}项目申报通知",
            url=f"https://example.gov.cn/{name}",
            url_key=name,
            publish_date=TODAY,
            first_seen_at=datetime(2026, 10, 12),
            last_seen_at=datetime(2026, 10, 12),
        )
        session.add(doc)
        session.flush()
        ids[name] = doc.id
        session.add(
            DocumentDetail(
                document_id=doc.id, body_text=body, status="ok", fetched_at=datetime(2026, 10, 12)
            )
        )
        session.add(
            DocumentDeadline(
                document_id=doc.id,
                fingerprint=f"{name}1",
                kind="企业申报截止",
                candidate_date=TODAY + timedelta(days=days),
                evidence=quote,
                confidence="高",
                status="pending",
            )
        )
        if second:
            session.add(
                DocumentDeadline(
                    document_id=doc.id,
                    fingerprint=f"{name}2",
                    kind="企业申报截止",
                    candidate_date=TODAY + timedelta(days=second),
                    evidence="申报截止：10月21日。",
                    confidence="高",
                    status="pending",
                )
            )
    session.flush()
    views, soon, reasons = excerpts.batch(session, list(ids.values()), {}, TODAY)
    assert views[ids["有效"]][0].source == "excerpt"
    assert views[ids["有效"]][0].public(TODAY)["remainingDays"] == 8
    assert views[ids["临期"]][0].source == "unverified"
    assert soon[ids["临期"]]
    assert reasons == {
        "7 天内": 1,
        "30 天以后": 1,
        "原句不在正文里": 1,
        "日期不在原句里": 1,
        "冲突": 1,
    }
    assert views[ids["第30天"]][0].source == "excerpt"
    assert all(
        views[ids[name]][0].public(TODAY)["date"] is None
        for name in ("临期", "原句已改", "年份错误", "冲突", "太远")
    )
    detail = v1.document(ids["有效"], session)
    assert detail["deadlines"][0]["source"] == "excerpt"
    assert detail["deadlines"][0]["quote"] == "申报截止：２０２６年１０月２０日。"
    doc = session.get(Document, ids["有效"])
    old = v1._items(session, [(doc, source)])[0]
    new = v1._items(session, [(doc, source)], excerpts=True)[0]
    assert "due" not in old
    assert new["due"]["source"] == "excerpt"
    assert new["deadline"] is None and new["deadline_state"] == "unverified"
    report = v1._report_payload_excerpts(session, 30, "", "", "", 1, 20)
    assert report["summary"]["7"] == 0
    assert report["summary"]["14"] == 1
    assert report["items"][0]["source"] == "excerpt"
    assert report["unverifiedSoon"]["total"] == 1
    calendar = v1.calendar(session, excerpts=1, days=30, limit=200, offset=0)
    assert any(item["source"] == "excerpt" and not item["verified"] for item in calendar["items"])
    assert calendar["unverifiedSoon"]["total"] == 1


def test_verified_has_priority_over_excerpt():
    selected = excerpts.selected(
        [
            excerpts.View("企业申报截止", "excerpt", date(2026, 10, 20)),
            excerpts.View("材料报送截止", "verified", date(2026, 10, 22)),
        ]
    )
    assert selected.source == "verified"


def test_selected_uses_open_excerpt_before_expired_verified():
    past = excerpts.View("企业申报截止", "verified", TODAY - timedelta(days=1))
    future = excerpts.View("材料报送截止", "excerpt", TODAY + timedelta(days=8))
    assert excerpts.selected([past, future], TODAY) == future
    assert excerpts.selected([past], TODAY) == past


def test_only_rule_found_deadlines_get_excerpt_dates(session):
    """D-099 期 0：机器摘录的日期只取规则找到的三类截止；AI 单独找到的、用户线索生成的不给日期，
    但同一类型日期对不上照样算冲突；"其他"类什么都不显示、也不算 7 天内有截止。"""
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科技局",
        column_name="通知",
        url="https://example.gov.cn/",
        rights_level=1,
    )
    session.add(source)
    session.flush()
    ids = {}

    def doc(name, body, *candidates):
        item = Document(
            source_id=source.id,
            title=f"{name}项目申报通知",
            url=f"https://example.gov.cn/{name}",
            url_key=f"origin-{name}",
            publish_date=TODAY,
            first_seen_at=datetime(2026, 10, 12),
            last_seen_at=datetime(2026, 10, 12),
        )
        session.add(item)
        session.flush()
        ids[name] = item.id
        session.add(
            DocumentDetail(
                document_id=item.id, body_text=body, status="ok", fetched_at=datetime(2026, 10, 12)
            )
        )
        for number, (origin, kind, days, quote) in enumerate(candidates):
            session.add(
                DocumentDeadline(
                    document_id=item.id,
                    fingerprint=f"{name}{number}",
                    kind=kind,
                    candidate_date=TODAY + timedelta(days=days),
                    evidence=quote,
                    confidence="高",
                    status="pending",
                    origin=origin,
                )
            )

    q20, q22, q15 = "申报截止：10月20日。", "申报截止：10月22日。", "申报截止：10月15日。"
    doc("规则", q20, ("rule", "企业申报截止", 8, q20))
    doc("仅AI", q20, ("ai", "企业申报截止", 8, q20))
    doc("用户线索", q20, ("user", "企业申报截止", 8, q20))
    doc("AI另一天", q20 + q22, ("rule", "企业申报截止", 8, q20), ("ai", "企业申报截止", 10, q22))
    # AI 那条编号更小也不用它：日期取规则那条
    doc("同一天", q20, ("ai", "材料报送截止", 8, q20), ("rule", "材料报送截止", 8, q20))
    doc("其他类", q20 + q15, ("rule", "其他", 8, q20), ("ai", "其他", 3, q15))
    doc("AI临期", q15, ("ai", "企业申报截止", 3, q15))
    session.flush()
    views, soon, reasons = excerpts.batch(session, list(ids.values()), {}, TODAY)

    def shown(name):
        return [(v.kind, v.source, v.day) for v in views[ids[name]]]

    day8 = TODAY + timedelta(days=8)
    assert shown("规则") == [("企业申报截止", "excerpt", day8)]
    assert shown("仅AI") == [("企业申报截止", "unverified", None)]
    assert shown("用户线索") == [("企业申报截止", "unverified", None)]
    assert shown("AI另一天") == [("企业申报截止", "unverified", None)]
    rule_row = session.scalar(
        select(DocumentDeadline).where(
            DocumentDeadline.document_id == ids["同一天"], DocumentDeadline.origin == "rule"
        )
    )
    assert [(v.source, v.day, v.record_id) for v in views[ids["同一天"]]] == [
        ("excerpt", day8, rule_row.id)
    ]
    assert shown("其他类") == [] and not soon[ids["其他类"]]
    # AI 单独找到的临期截止不给日期，但照样提示"7 天内有截止未核对"
    assert shown("AI临期") == [("企业申报截止", "unverified", None)] and soon[ids["AI临期"]]
    assert reasons == {"不是规则找到的": 2, "冲突": 1, "7 天内": 1}
