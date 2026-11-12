"""覆盖看板落库：导入盘点结果、按来源抓取结果重算状态、按级汇总、管理接口（人工确认、按省抽查）。"""

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from youju.api.app import audit_log
from youju.govsites.boarddb import (
    board_lock,
    enable_sources,
    import_inventory,
    latest_acceptance,
    refresh_states,
    summary,
)
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
    # 试点口径只算数、不写看板：正式看板不混进用户还看不到的来源
    assert refresh_states(session, now=NOW, trial=True)["已收录"] == 1
    session.refresh(cell)
    assert cell.state == "待启用" and cell.refreshed_at is not None

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
    assert prov["refreshed_at"] and data["levels"]["省级"]["refreshed_at"] == prov["refreshed_at"]


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


def test_enable_requires_passed_acceptance(session):
    source = _source(session)
    source.last_success_at = NOW - timedelta(hours=1)
    session.add(
        SourceCheck(
            source_id=source.id,
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(hours=1),
            status="ok",
            items_found=12,
            items_new=12,
        )
    )
    session.commit()
    import_inventory(session, _portals(), _depts())
    # 没抽查：一个都不开
    r = enable_sources(session, "浙江", "省级", "王某", now=NOW)
    assert (r.enabled, r.blocked_reason is not None) == (0, True)
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=11,
            passed=False,
        )
    )
    session.commit()
    assert enable_sources(session, "浙江", "省级", "王某", now=NOW).enabled == 0  # 没过也不开
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="全部",
            sampled_by="王某",
            sampled_at=NOW + timedelta(hours=1),
            sample_size=30,
            passed=True,
        )
    )
    session.commit()
    assert enable_sources(session, "浙江", "省级", "王某", dry_run=True, now=NOW).enabled == 1
    session.refresh(source)
    assert source.enabled is False
    r = enable_sources(session, "浙江", "省级", "王某", now=NOW)
    assert (r.enabled, r.source_ids, r.acceptance_id is not None) == (1, [source.id], True)
    session.refresh(source)
    assert source.enabled is True and f"抽查记录 #{r.acceptance_id}）后由 王某 启用" in source.note
    # 启用后（命令行接着）按正式口径重算：试抓成功、已启用的格算已收录
    refresh_states(session, now=NOW, province="浙江")
    assert _cell(session, "科技").state == "已收录"


def test_enable_skips_sources_refused_after_trial(session):
    source = _source(session)
    source.last_success_at = NOW - timedelta(days=1)
    session.add_all(
        [
            SourceCheck(
                source_id=source.id,
                started_at=NOW - timedelta(days=1),
                finished_at=NOW - timedelta(days=1),
                status="ok",
                items_found=10,
                items_new=10,
            ),
            # 后来被网站 412 拦了：看板记不能抓，启用门禁也不开（D-013）
            SourceCheck(
                source_id=source.id,
                started_at=NOW - timedelta(hours=1),
                finished_at=NOW - timedelta(hours=1),
                status="http_error",
                http_status=412,
                items_found=0,
                items_new=0,
            ),
        ]
    )
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=5,
            passed=True,
        )
    )
    session.commit()
    import_inventory(session, _portals(), _depts())
    r = enable_sources(session, "浙江", "省级", "王某", now=NOW)
    assert (r.enabled, r.skipped) == (0, 1)
    session.refresh(source)
    assert source.enabled is False


def _cell(session, dept, code="330000"):
    return session.scalar(
        select(CoverageCell).where(CoverageCell.dept == dept, CoverageCell.division_code == code)
    )


def test_confirmation_only_for_cells_without_columns(session):
    _source(session)
    import_inventory(session, _portals(), _depts())
    # 认出了栏目的格就算被标了确认，也照抓取结果算，不从分母里扣
    sci = _cell(session, "科技")
    sci.confirmed_no_column, sci.confirmed_by = True, "王某"
    session.commit()
    refresh_states(session, now=NOW)
    session.refresh(sci)
    assert sci.state == "待启用"
    # 门户没找到的需人工格（没认出栏目）可以确认
    town = _cell(session, "科技", "330106")
    town.confirmed_no_column, town.confirmed_by = True, "王某"
    session.commit()
    refresh_states(session, now=NOW)
    session.refresh(town)
    assert town.state == "网站无此栏目" and "人工确认" in town.reason


def test_refresh_links_sources_added_after_import(session):
    import_inventory(session, _portals(), _depts())
    assert refresh_states(session, now=NOW)["待启用"] == 0
    source = _source(session)  # 来源后导入：重算时按网址挂上
    assert refresh_states(session, now=NOW)["待启用"] == 1
    cell = _cell(session, "科技")
    assert (
        session.scalar(
            select(CoverageCellSource.source_id).where(CoverageCellSource.cell_id == cell.id)
        )
        == source.id
    )


def test_reimport_keeps_verify_results_when_not_given(session):
    checks = {COL: SimpleNamespace(result="读不出", reason="列表是图片")}
    import_inventory(session, _portals(), _depts(), checks)
    import_inventory(session, _portals(), _depts())  # 这次没给试读结果
    column = _cell(session, "科技").columns[0]
    assert (column["result"], column["reason"]) == ("读不出", "列表是图片")


def _zone(name, url):
    return DeptRow(
        "330000",
        "省级",
        "浙江",
        "",
        "",
        "开发区",
        dept_full_name=name,
        site_url=url,
        status="已找到",
    )


def test_zones_match_by_site_not_order(session):
    a = _zone("甲开发区管委会", "https://a.zj.gov.cn/")
    b = _zone("乙开发区管委会", "https://b.zj.gov.cn/")
    import_inventory(session, _portals(), _depts() + [a, b])
    cell_b = session.scalar(select(CoverageCell).where(CoverageCell.site_url == b.site_url))
    cell_b.note = "乙的备注"
    session.commit()
    # 顺序反过来、甲换成丙（网址 http 和末尾斜杠不同也算同一个乙）
    b2 = _zone("乙开发区管委会", "http://b.zj.gov.cn")
    c = _zone("丙开发区管委会", "https://c.zj.gov.cn/")
    s = import_inventory(session, _portals(), _depts() + [c, b2])
    assert (s.cells_created, s.cells_removed) == (1, 1)
    session.refresh(cell_b)
    assert (cell_b.dept_full_name, cell_b.note) == ("乙开发区管委会", "乙的备注")
    zones = session.scalars(
        select(CoverageCell).where(CoverageCell.dept == "开发区").order_by(CoverageCell.seq)
    ).all()
    assert [z.dept_full_name for z in zones] == ["乙开发区管委会", "丙开发区管委会"]


def test_latest_acceptance_newer_record_wins():
    old_level = {"passed": True, "sampled_at": "2026-11-01T00:00:00+00:00", "level": "省级"}
    new_all = {"passed": False, "sampled_at": "2026-11-05T00:00:00+00:00", "level": "全部"}
    accepted = {("浙江", "省级"): old_level, ("浙江", "全部"): new_all}
    assert latest_acceptance(accepted, "浙江", "省级")["passed"] is False
    accepted[("浙江", "省级")] = {**old_level, "passed": False, "sampled_at": "2026-11-06T00:00"}
    accepted[("浙江", "全部")] = {**new_all, "passed": True}
    assert latest_acceptance(accepted, "浙江", "省级")["passed"] is False
    assert latest_acceptance(accepted, "浙江", "市级")["passed"] is True
    assert latest_acceptance(accepted, "江苏", "省级") is None


def test_admin_edit_checks_confirmable_and_audits_changes(client, session, caplog):  # noqa: F811
    _source(session)
    import_inventory(session, _portals(), _depts())
    refresh_states(session, now=NOW)
    sci, fin = _cell(session, "科技"), _cell(session, "财政")
    url = "/api/admin/coverage/cells/{}"
    bad = client.post(
        url.format(sci.id), headers=HEADERS, json={"by": "王某", "confirm_no_column": True}
    )
    assert bad.status_code == 422  # 认出了栏目
    assert client.post(url.format(fin.id), headers=HEADERS, json={"by": "  "}).status_code == 422
    audit_log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="youju.audit"):
            body = {"by": " 王某 ", "confirm_no_column": True, "note": "门户统一发布"}
            res = client.post(url.format(fin.id), headers=HEADERS, json=body).json()
            again = client.post(url.format(fin.id), headers=HEADERS, json=body).json()
    finally:
        audit_log.removeHandler(caplog.handler)
    # 保存后当场重算；第二次一样的内容不算改动、不写审计
    assert res["state"] == "网站无此栏目" and res["changed"] == [
        "confirmed_at",
        "confirmed_by",
        "confirmed_no_column",
        "note",
    ]
    assert again["changed"] == []
    records = [
        json.loads(r.getMessage()) for r in caplog.records if "coverage_cell_edit" in r.getMessage()
    ]
    assert len(records) == 1
    changes = records[0]["changes"]
    assert records[0]["by"] == "王某"
    assert changes["note"] == {"before": None, "after": "门户统一发布"}
    assert changes["confirmed_no_column"] == {"before": False, "after": True}
    session.refresh(fin)
    assert (fin.confirmed_by, fin.state) == ("王某", "网站无此栏目")


def test_admin_acceptance_levels_and_audit(client, session, caplog):  # noqa: F811
    url = "/api/admin/coverage/acceptances"
    base = {"by": "王某", "sample_size": 10, "passed": True}
    assert (
        client.post(
            url, headers=HEADERS, json={**base, "province": "浙江", "level": "国家级"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, headers=HEADERS, json={**base, "province": "国家", "level": "省级"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, headers=HEADERS, json={**base, "by": " ", "province": "浙江", "level": "省级"}
        ).status_code
        == 422
    )
    audit_log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="youju.audit"):
            res = client.post(
                url,
                headers=HEADERS,
                json={**base, "province": "国家", "level": "国家级", "note": "抽 10 格"},
            )
    finally:
        audit_log.removeHandler(caplog.handler)
    assert res.status_code == 201
    record = next(
        json.loads(r.getMessage())
        for r in caplog.records
        if "coverage_acceptance" in r.getMessage()
    )
    assert (record["acceptance_id"], record["note"]) == (res.json()["id"], "抽 10 格")
    assert session.scalar(select(CoverageAcceptance.level)) == "国家级"


def test_board_commands_are_exclusive(session_factory):
    # 导入、重算、启用不同时跑：第二个拿不到锁
    with session_factory() as a, session_factory() as b:
        with board_lock(a, 0) as got_a:
            assert got_a
            with board_lock(b, 0) as got_b:
                assert not got_b
        with board_lock(b, 0) as again:
            assert again


def test_enable_verified_opens_never_crawled_readable_sources(session):
    # 本机试抓、抽查合格后在线上第一次打开：线上不先试抓没启用的来源
    source = _source(session)
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=11,
            passed=True,
        )
    )
    session.commit()
    import_inventory(session, _portals(), _depts())
    assert enable_sources(session, "浙江", "省级", "王某", now=NOW).enabled == 0
    r = enable_sources(session, "浙江", "省级", "王某", now=NOW, verified=True)
    assert (r.enabled, r.source_ids) == (1, [source.id])
    session.refresh(source)
    assert source.enabled is True


def test_enable_with_batch_counts_other_spelling_as_live(session):
    """批次里是 https、库里同一个列表是 http 写法且已经启用：算"已经在收"（和上线脚本数"已经打开"的
    口径一致），不算"库里没有"——否则上线脚本每次都停在这个省（#471 独立复核）。"""
    _source(session, url=COL.replace("https://", "http://"), enabled=True)
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=11,
            passed=True,
        )
    )
    session.commit()
    import_inventory(session, _portals(), _depts())
    r = enable_sources(
        session, "浙江", "省级", "王某", now=NOW, verified=True, only_urls=[COL], dry_run=True
    )
    assert r.enabled == 0
    assert r.missing == [f"{COL}（已经在收：库里是另一种写法）"]


def test_enable_with_batch_opens_only_listed_sources(session):
    listed = _source(session)
    other = _source(session, url="https://kjt.zj.gov.cn/zcwj/")
    depts = _depts()
    depts[0].columns.append(ColumnHit("政策文件", "政策文件", other.url, "https://kjt.zj.gov.cn/"))
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=11,
            passed=True,
        )
    )
    session.commit()
    import_inventory(session, _portals(), depts)
    gone = "https://kjt.zj.gov.cn/nothere/"
    r = enable_sources(
        session, "浙江", "省级", "王某", now=NOW, verified=True, only_urls=[listed.url, gone]
    )
    # 只开批次里的；批次里有、库里没有的报出来
    assert (r.enabled, r.source_ids) == (1, [listed.id])
    assert len(r.missing) == 1 and gone in r.missing[0]
    session.refresh(other)
    assert other.enabled is False


def test_enable_writes_list_api_marker_only_for_columns_read_via_api(session):
    """D-128：试读经列表接口读出的栏目（验证状态带"经接口读"），复检抽查合格、打开时才写上
    "经接口读"标记；普通栏目打开时不写（以后页面改成接口加载也不会自动去读）。"""
    via_api = _source(session)
    via_api.verification_status = "试跑可读（经接口读，全国覆盖 v1，未启用）"
    session.commit()
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=11,
            passed=True,
        )
    )
    session.commit()
    import_inventory(session, _portals(), _depts())
    r = enable_sources(session, "浙江", "省级", "王某", now=NOW, verified=True)
    assert r.source_ids == [via_api.id]
    session.refresh(via_api)
    assert via_api.parser_config.get("list_api_open") == "大汉jpaas"
    assert "经接口读" in via_api.note


def test_enable_does_not_mark_plain_readable_columns(session):
    plain = _source(session)
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="王某",
            sampled_at=NOW,
            sample_size=11,
            passed=True,
        )
    )
    session.commit()
    import_inventory(session, _portals(), _depts())
    enable_sources(session, "浙江", "省级", "王某", now=NOW, verified=True)
    session.refresh(plain)
    assert plain.enabled is True and "list_api_open" not in (plain.parser_config or {})
