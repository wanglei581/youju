from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook

from youju.api import v1
from youju.models import Document, DocumentDeadline, DocumentDetail, DocumentField, Source
from youju.track import (
    CustomerProfile,
    CustomerTrack,
    TrackReport,
    export_track_excel,
    read_customer_profiles,
    track_clients,
)

BJ = ZoneInfo("Asia/Shanghai")


def test_read_profiles_and_mark_unknown_province(tmp_path: Path):
    path = tmp_path / "clients.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["客户名称", "省份", "关键词", "支持方式"])
    sheet.append(["甲公司", "北京", "专精特新; 数字化", "资金支持;未知标签"])
    sheet.append(["乙公司", "火星", "", ""])
    book.save(path)

    profiles = read_customer_profiles(path)
    assert profiles[0].keywords == ("专精特新", "数字化")
    assert profiles[0].supports == ("资金支持",)
    assert profiles[1].invalid_province


def test_export_keeps_empty_clients_and_escapes_formula(tmp_path: Path):
    output = tmp_path / "track.xlsx"
    report = TrackReport(
        [CustomerTrack(CustomerProfile("=客户", "北京"))],
        datetime(2026, 9, 30, 1, 2, tzinfo=ZoneInfo("UTC")),
    )
    assert export_track_excel(report, output) == 1
    book = load_workbook(output, data_only=False)
    assert book["跟进表"]["A3"].value == "'=客户"
    assert book["跟进表"]["B3"].value == "暂无"
    assert book["汇总"]["B3"].value == 0
    assert "2026-09-30 09:02" in book["跟进表"]["A1"].value


def test_track_uses_radar_scope_and_customer_filters(session, monkeypatch):
    stamp = datetime(2026, 9, 30, 10, 0, tzinfo=BJ)
    monkeypatch.setattr(v1, "now", lambda: stamp)
    national = Source(
        level="国家级",
        province="国家",
        dept="科技",
        dept_full_name="国家科技部",
        column_name="通知",
        url="https://example.gov.cn/national",
        rights_level=1,
    )
    beijing = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知",
        url="https://example.gov.cn/beijing",
        rights_level=1,
    )
    session.add_all([national, beijing])
    session.flush()

    def add(source, number, title, days, support="资金支持", status="approved"):
        document = Document(
            source_id=source.id,
            url=f"https://example.gov.cn/{number}",
            url_key=f"example/{number}",
            title=title,
            publish_date=date(2026, 9, 1),
            first_seen_at=stamp,
            last_seen_at=stamp,
        )
        session.add(document)
        session.flush()
        body = f"正文 {number}"
        session.add(
            DocumentDetail(
                document_id=document.id,
                body_text=body,
                issuer=source.dept_full_name,
                fetched_at=stamp,
                status="ok",
            )
        )
        session.add(
            DocumentDeadline(
                document_id=document.id,
                fingerprint=number,
                kind="企业申报截止",
                candidate_date=stamp.date() + timedelta(days=days),
                approved_date=stamp.date() + timedelta(days=days) if status == "approved" else None,
                evidence="企业申报截止",
                confidence="高",
                status=status,
            )
        )
        if support:
            session.add(
                DocumentField(
                    document_id=document.id,
                    field="support",
                    value=support,
                    approved_value=support,
                    evidence=support,
                    evidence_offset=0,
                    body_hash=sha256(body.encode()).hexdigest(),
                    status="approved",
                )
            )

    add(national, "n", "国家数字化申报通知", 5)
    add(beijing, "b", "北京专精特新申报通知", 10, "贷款贴息")
    add(beijing, "late", "北京远期申报通知", 31)
    add(beijing, "pending", "北京未核对通知", 5, status="pending")
    session.commit()

    radar = v1._report_payload(session, 30, "", "", "", 1, 100)
    report = track_clients(
        session,
        [
            CustomerProfile("北京客户", "北京"),
            CustomerProfile("关键词客户", "北京", ("专精特新",), ("贷款贴息",)),
            CustomerProfile("上海客户", "上海"),
        ],
    )
    by_name = {client.customer_name: client for client in report.clients}
    beijing_items = by_name["北京客户"].items
    # 跟进表列全部未截止项目；30 天内的部分与截止雷达同口径（验收 A2）
    assert len([item for item in beijing_items if item.remaining_days <= 30]) == radar["total"]
    assert [item.title for item in beijing_items][-1] == "北京远期申报通知"
    # 每一行的剩余天数是它自己的，不是最后一份通知的
    assert [item.remaining_days for item in beijing_items] == [5, 10, 31]
    assert [item.title for item in by_name["关键词客户"].items] == ["北京专精特新申报通知"]
    assert [item.title for item in by_name["上海客户"].items] == ["国家数字化申报通知"]
