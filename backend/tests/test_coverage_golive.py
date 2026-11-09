"""一批来源上线（coverage-golive）：试算、按顺序打开、重跑认出做过的、证据改了就停。"""

import csv
from datetime import UTC, datetime
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
    assert "step=accept lvl=PROV written=1 already=0 ok=1" in lines
    assert "step=enable prov=ZJ lvl=PROV n=2 expect=2 ok=1" in lines
    assert lines[-1] == "batch1_done=1 enabled=2 expect=2"
    assert {s.enabled for s in session.scalars(select(Source))} == {True}
    assert (Path(tmp_path) / next(p.name for p in tmp_path.glob("batch1-*.txt"))).exists()

    # 重跑：导入过的不再导入、同一份证据的抽查结论不重记、已经打开的算 done_before
    ok, lines = _run(session, out, tmp_path, apply=True)
    assert ok, lines
    assert "step=import new=0 expect=0 ok=1" in lines
    assert "step=accept lvl=PROV written=0 already=1 ok=1" in lines
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
