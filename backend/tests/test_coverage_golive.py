"""一批来源上线（coverage-golive）：试算、按顺序打开、重跑认出做过的、证据改了就停。"""

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from youju.govsites.golive import Report, run
from youju.govsites.inventory import ColumnHit, DeptRow, PortalRow
from youju.govsites.report import COLUMN_FIELDS, write_outputs
from youju.models import CoverageAcceptance, Source

NOW = datetime(2026, 11, 14, 10, tzinfo=UTC)
A = "https://kjt.zj.gov.cn/tzgg/"
B = "https://czt.zj.gov.cn/tzgg/"
HEAD = "层级,省份,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"


def _docs(tmp_path: Path) -> Path:
    """一个省（浙江）两个省级栏目，证据都通过，批次文件按证据生成。"""
    from youju.govsites.cli import build_batch
    from youju.govsites.divisions import DivisionIndex

    out = tmp_path / "全国覆盖"
    portal = PortalRow(
        "330000", "省级", "浙江", "", "", "浙江省", "https://www.zj.gov.cn/", status="已找到"
    )

    def dept(name, url, col):
        return DeptRow(
            "330000",
            "省级",
            "浙江",
            "",
            "",
            name,
            site_url=url,
            status="已找到",
            columns=[ColumnHit("通知公告", "通知公告", col, url)],
        )

    write_outputs(
        DivisionIndex([]),
        [portal],
        [dept("科技", "https://kjt.zj.gov.cn/", A), dept("财政", "https://czt.zj.gov.cn/", B)],
        out,
    )
    (out / "栏目清单").mkdir(parents=True)
    with (out / "栏目清单" / "浙江.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMN_FIELDS)
        for dept_name, url in (("科技", A), ("财政", B)):
            row = [""] * len(COLUMN_FIELDS)
            row[0], row[1], row[4], row[5], row[6], row[7] = (
                "省级",
                "浙江",
                dept_name,
                f"浙江省{dept_name}厅",
                "通知公告",
                url,
            )
            row[11], row[19] = "试跑可读（全国覆盖 v1，未启用）", "330000"
            w.writerow(row)
    (out / "抽查" / "省级").mkdir(parents=True)
    (out / "抽查" / "省级" / "浙江.csv").write_text(
        HEAD + f"省级,浙江,{A},自动通过,,,,\n省级,浙江,{B},自动通过,,,,\n", encoding="utf-8-sig"
    )
    (out / "批次").mkdir()
    with (out / "批次" / "第1批.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMN_FIELDS)
        w.writerows(build_batch(out, ["省级"]).rows)
    return out


def _detail(tmp_path: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("batch1-*-detail.txt"))


def _run(session, out, tmp_path, apply):
    lines: list[str] = []
    report = Report(tmp_path, "batch1", echo=lines.append)
    ok = run(session, out, "第1批", ["省级"], "王某", apply, report, now=NOW, operator="协调会话")
    return ok, lines


def test_check_then_apply_then_rerun(session, tmp_path):
    out = _docs(tmp_path)
    ok, lines = _run(session, out, tmp_path, apply=False)
    assert ok and all(line.isascii() for line in lines)
    assert "step=import_check new=2 expect=2 existing=0 ok=1" in lines
    assert "step=enable_expect prov=ZJ lvl=PROV expect=2" in lines
    assert lines[-1] == "check_done=1 expect_total=2"
    assert session.scalar(select(Source.id)) is None  # 试算不写库

    ok, lines = _run(session, out, tmp_path, apply=True)
    assert ok, lines
    assert "step=import new=2 expect=2 ok=1" in lines
    assert "step=accept lvl=PROV written=1 already=0 kept_manual=0 ok=1" in lines
    assert "step=enable prov=ZJ lvl=PROV n=2 expect=2 ok=1" in lines
    assert lines[-1] == "batch1_done=1 enabled=2 expect=2"
    assert {s.enabled for s in session.scalars(select(Source))} == {True}
    assert (Path(tmp_path) / next(p.name for p in tmp_path.glob("batch1-*.txt"))).exists()
    # 审计行也写进细节文件（容器终端里跑时 stderr 不进云托管日志）
    assert "审计：" in _detail(tmp_path)

    # 重跑：导入过的不再导入、同一份证据的抽查结论不重记、已经打开的算 done_before
    ok, lines = _run(session, out, tmp_path, apply=True)
    assert ok, lines
    assert "step=import new=0 expect=0 ok=1" in lines
    assert "step=accept lvl=PROV written=0 already=1 kept_manual=0 ok=1" in lines
    assert any(
        line.startswith("step=enable_check prov=ZJ") and "expect=0 done_before=2" in line
        for line in lines
    )
    assert len(session.scalars(select(CoverageAcceptance)).all()) == 1


def test_stops_when_evidence_changed_after_batch(session, tmp_path):
    out = _docs(tmp_path)
    # 批次生成以后，创始人复看把一行改成不通过：批次和证据对不上，第一步就停，什么都不写
    (out / "抽查" / "省级" / "浙江.csv").write_text(
        HEAD
        + f"省级,浙江,{A},自动通过,,,,\n省级,浙江,{B},自动通过,,创始人,不通过（不是本厅）,复看\n",
        encoding="utf-8-sig",
    )
    ok, lines = _run(session, out, tmp_path, apply=True)
    assert not ok
    assert lines[0].startswith("step=batch") and "same_as_evidence=0" in lines[0]
    assert lines[-1].startswith("stop=1 step=batch")
    assert session.scalar(select(Source.id)) is None
    # 对不上的行写进细节文件
    assert f"批次文件里有、按证据表重算没有（或内容变了）：省级 浙江 {B}" in _detail(tmp_path)


def test_manual_hold_in_batch_stops_and_is_kept(session, tmp_path):
    """回滚后在后台补记了"没过"，又跑了脚本：第三步就停，不重记"合格"、不重开（#471 独立复核）。"""
    out = _docs(tmp_path)
    assert _run(session, out, tmp_path, apply=True)[0]
    for s in session.scalars(select(Source)):
        s.enabled = False
    session.add(
        CoverageAcceptance(
            province="浙江",
            level="省级",
            sampled_by="协调会话",
            sampled_at=NOW + timedelta(hours=1),
            sample_size=0,
            passed=False,
            note="回滚：正文抓成导航",
        )
    )
    session.commit()
    for apply in (False, True):  # 试算也报出来
        ok, lines = _run(session, out, tmp_path, apply=apply)
        assert not ok
        assert any(
            line.startswith("step=accept_check lvl=PROV") and "manual_holds=1 ok=0" in line
            for line in lines
        )
        assert lines[-1].startswith("stop=1 step=accept_check")
    assert {s.enabled for s in session.scalars(select(Source))} == {False}
    newest = session.scalars(
        select(CoverageAcceptance).order_by(CoverageAcceptance.sampled_at.desc())
    ).first()
    assert (newest.passed, newest.sampled_by) == (False, "协调会话")
    assert "后台记了没过" in _detail(tmp_path)


def test_manual_hold_outside_batch_is_not_overwritten(session, tmp_path):
    """不在本批的省（抽查没合格）后台记了"没过"：本批照常打开，这个省的结论不重记。"""
    out = _docs(tmp_path)
    (out / "抽查" / "省级" / "福建.csv").write_text(
        HEAD + "省级,福建,https://kjt.fujian.gov.cn/tzgg/,待人工,部门,王某,不通过,挂错\n",
        encoding="utf-8-sig",
    )
    session.add(
        CoverageAcceptance(
            province="福建",
            level="省级",
            sampled_by="协调会话",
            sampled_at=NOW - timedelta(days=1),
            sample_size=0,
            passed=False,
            note="先压住",
        )
    )
    session.commit()
    ok, lines = _run(session, out, tmp_path, apply=True)
    assert ok, lines
    assert "step=accept lvl=PROV written=1 already=0 kept_manual=1 ok=1" in lines
    notes = session.scalars(
        select(CoverageAcceptance.note).where(CoverageAcceptance.province == "福建")
    ).all()
    assert notes == ["先压住"]


def test_unexpected_error_prints_stop_line(session, tmp_path, monkeypatch):
    """意料外的错也打一行 stop（报出在哪一步），出错经过写进细节文件。"""
    import youju.govsites.boarddb as boarddb

    def broken(*args, **kwargs):
        raise RuntimeError("看板表坏了")

    monkeypatch.setattr(boarddb, "import_inventory", broken)
    out = _docs(tmp_path)
    ok, lines = _run(session, out, tmp_path, apply=True)
    assert not ok
    assert lines[-1].startswith("stop=1 step=coverage_import error=RuntimeError detail=")
    assert "看板表坏了" in _detail(tmp_path)
