"""覆盖看板落库：导入盘点结果、按来源抓取结果重算状态、按级汇总、管理接口（人工确认、按省抽查）。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from youju.govsites.boarddb import import_inventory, refresh_states, summary
from youju.govsites.inventory import ColumnHit, DeptRow, PortalRow
from youju.models import (
    CoverageAcceptance,
    CoverageCell,
    CoverageCellSource,
    Source,
    SourceCheck,
)

from .test_admin_portal import HEADERS
from .test_admin_portal import client as client  # noqa: F401 - pytest 夹具

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
COL = "https://kjt.zj.gov.cn/col/col1229225203/index.html"


def _portals():
    return [
        PortalRow(
            "330000", "省级", "浙江", "", "", "浙江省", "https://www.zj.gov.cn/", status="已找到"
        ),
        PortalRow("330106", "区县级", "浙江", "杭州", "西湖区", "西湖区", status="导航未找到"),
    ]


def _depts():
    return [
        DeptRow(
            "330000",
            "省级",
            "浙江",
            "",
            "",
            "科技",
            dept_full_name="浙江省科学技术厅",
            site_url="https://kjt.zj.gov.cn/",
            status="已找到",
            columns=[ColumnHit("通知公告", "通知通告", COL, "https://kjt.zj.gov.cn/")],
        ),
        DeptRow(
            "330000",
            "省级",
            "浙江",
            "",
            "",
            "财政",
            site_url="https://czt.zj.gov.cn/",
            status="已找到",
            columns=[],
        ),
        DeptRow("330106", "区县级", "浙江", "杭州", "西湖区", "科技", status="导航未找到"),
    ]


def _source(session, url=COL, enabled=False):
    s = Source(
        level="省级",
        province="浙江",
        dept="科技",
        dept_full_name="浙江省科学技术厅",
        column_name="通知通告",
        url=url,
        fetch_mode="static",
        enabled=enabled,
        parser_config={},
        verification_status="试跑可读（全国覆盖 v1，未启用）",
    )
    session.add(s)
    session.commit()
    return s


def test_import_links_sources_and_refresh_computes_states(session):
    source = _source(session)
    s = import_inventory(session, _portals(), _depts())
    assert (s.divisions, s.cells_created, s.links) == (2, 3, 1)
    cell = session.scalar(
        select(CoverageCell).where(
            CoverageCell.dept == "科技", CoverageCell.division_code == "330000"
        )
    )
    assert (
        session.scalar(
            select(CoverageCellSource.source_id).where(CoverageCellSource.cell_id == cell.id)
        )
        == source.id
    )

    counts = refresh_states(session, now=NOW)
    # 导入了没抓过：待启用；部门网站没栏目：网站无此栏目；门户没找到：需人工
    assert counts == {"待启用": 1, "网站无此栏目": 1, "需人工": 1}

    # 试抓成功（来源未启用）：正式口径记待启用，试点口径算已收录
    source.last_success_at = NOW - timedelta(hours=2)
    session.add(
        SourceCheck(
            source_id=source.id,
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=2),
            status="ok",
            items_found=12,
            items_new=12,
        )
    )
    session.commit()
    assert refresh_states(session, now=NOW)["待启用"] == 1
    assert refresh_states(session, now=NOW, trial=True)["已收录"] == 1
    session.refresh(cell)
    assert cell.last_success_at is not None and cell.state == "已收录"

    # 启用以后照正式口径算，3 天没成功掉成抓取失败
    source.enabled = True
    session.commit()
    assert refresh_states(session, now=NOW)["已收录"] == 1
    assert refresh_states(session, now=NOW + timedelta(days=4))["抓取失败"] == 1


def test_reimport_keeps_human_edits_and_confirmation_wins(session):
    import_inventory(session, _portals(), _depts())
    cell = session.scalar(select(CoverageCell).where(CoverageCell.dept == "财政"))
    cell.confirmed_no_column, cell.confirmed_by, cell.note = True, "王某", "门户统一发布"
    session.commit()
    import_inventory(session, _portals(), _depts())
    session.refresh(cell)
    assert (cell.confirmed_no_column, cell.note) == (True, "门户统一发布")
    refresh_states(session, now=NOW)
    session.refresh(cell)
    assert cell.state == "网站无此栏目" and "人工确认" in cell.reason
    data = summary(session, "省级")
    prov = data["provinces"][0]
    # 人工确认的网站无此栏目不算缺口：2 格里扣掉 1 格，剩下 1 格还没收录
    assert (prov["total"], prov["rate"]) == (2, 0.0)


def test_admin_coverage_interfaces(client, session):  # noqa: F811
    import_inventory(session, _portals(), _depts())
    refresh_states(session, now=NOW)
    assert client.get("/api/admin/coverage/summary").status_code == 401
    data = client.get("/api/admin/coverage/summary", headers=HEADERS).json()
    assert data["levels"]["省级"]["total"] == 2
    cells = client.get("/api/admin/coverage/cells?province=浙江&level=省级", headers=HEADERS).json()
    assert cells["total"] == 2
    cell_id = next(c["id"] for c in cells["cells"] if c["dept"] == "科技")
    detail = client.get(f"/api/admin/coverage/cells/{cell_id}", headers=HEADERS).json()
    assert detail["columns"][0]["url"] == COL
    bad = client.post(
        f"/api/admin/coverage/cells/{cell_id}",
        headers=HEADERS,
        json={"by": "王某", "alt_source": "ftp://x"},
    )
    assert bad.status_code == 422
    ok = client.post(
        f"/api/admin/coverage/cells/{cell_id}",
        headers=HEADERS,
        json={
            "by": "王某",
            "alt_source": "https://www.zj.gov.cn/col/col1229577560/index.html",
            "note": "省政策文件库",
        },
    )
    assert ok.status_code == 200
    res = client.post(
        "/api/admin/coverage/acceptances",
        headers=HEADERS,
        json={"by": "王某", "province": "浙江", "level": "省级", "sample_size": 20, "passed": True},
    )
    assert res.status_code == 201
    assert session.scalar(select(CoverageAcceptance.passed)) is True
    data = client.get("/api/admin/coverage/summary?level=省级", headers=HEADERS).json()
    assert data["provinces"][0]["acceptance"]["passed"] is True
    assert client.get("/admin/coverage?embed=1").status_code == 200
