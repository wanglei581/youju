from datetime import date, datetime, timedelta

import pytest

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
