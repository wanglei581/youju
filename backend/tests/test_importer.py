import csv
from pathlib import Path

import pytest
from sqlalchemy import func, select

from youju.config import DEFAULT_SOURCES_CSV
from youju.models import Source
from youju.sources.importer import import_sources, mode_for

CSV_HEADER = "层级,省份,部门,部门全称,栏目名称,栏目URL,HTTP状态,页面日期数,最新日期样例,验证状态,验证日期,备注\n"


def write_csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "sources.csv"
    path.write_text(CSV_HEADER + "\n".join(rows) + "\n", encoding="utf-8-sig")
    return path


def test_mode_for_statuses() -> None:
    assert mode_for("已验证（列表页，含近期日期）") == ("static", True)
    # 地市第一批：试跑通过、登记好了，但创始人同意前不抓取（D-086 ④）
    assert mode_for("待开（2026-10-23 本机试抓通过）") == ("static", False)
    assert mode_for("需浏览器渲染或接口（页面由脚本加载）") == ("browser", False)
    # 网站防护（如 412 挑战页）视为拒绝：停用，不用浏览器渲染绕过
    assert mode_for("需浏览器渲染（网站防护拦截脚本访问）") == ("blocked", False)
    assert mode_for("网站防护拦截脚本访问（尊重，不绕过）") == ("blocked", False)
    assert mode_for("已访问（日期较少，需人工确认是否列表页）") == ("manual", False)
    assert mode_for("本机无法访问，需排查") == ("manual", False)
    assert mode_for("网站拒绝爬虫标识访问（尊重，不伪装）") == ("blocked", False)
    assert mode_for("robots 禁止抓取（尊重）") == ("blocked", False)
    assert mode_for("重复入口（与通知公告栏目内容相同，停用）") == ("manual", False)
    assert mode_for("未知状态") == ("manual", False)


def test_import_is_idempotent(session, tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        [
            "省级,北京,科技,北京市科学技术委员会,通知公告,https://kw.beijing.gov.cn/zwgk/tzgg/,200,20,2026-09-23,已验证（列表页，含近期日期）,2026-09-26,",
            "省级,甘肃,科技,甘肃省科学技术厅,通知公告,https://kjt.gansu.gov.cn/x,412,0,,需浏览器渲染（网站防护拦截脚本访问）,2026-09-26,地址由 agy 提供",
        ],
    )
    first = import_sources(session, path)
    assert (first.created, first.updated, first.enabled, first.disabled) == (2, 0, 1, 1)
    second = import_sources(session, path)
    assert (second.created, second.updated) == (0, 2)
    assert session.scalar(select(func.count(Source.id))) == 2
    gansu = session.scalar(select(Source).where(Source.province == "甘肃"))
    assert gansu.fetch_mode == "blocked"
    assert gansu.enabled is False
    assert gansu.note == "地址由 agy 提供"


def test_reimport_keeps_health(session, tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        [
            "省级,北京,科技,北京市科学技术委员会,通知公告,https://kw.beijing.gov.cn/zwgk/tzgg/,200,20,,已验证（列表页，含近期日期）,2026-09-26,"
        ],
    )
    import_sources(session, path)
    source = session.scalar(select(Source))
    source.health, source.consecutive_failures = "failing", 3
    session.commit()
    import_sources(session, path)
    session.refresh(source)
    assert (source.health, source.consecutive_failures) == ("failing", 3)


def test_real_source_list_imports(session) -> None:
    summary = import_sources(session, DEFAULT_SOURCES_CSV)
    assert summary.created >= 90
    assert summary.enabled >= 70
    # 同一个列表页只启用一个入口
    enabled = session.scalars(select(Source.url).where(Source.enabled.is_(True))).all()
    assert "http://kjt.shandong.gov.cn/col/col359318/index.html" not in enabled
    provinces = set(session.scalars(select(Source.province).where(Source.level == "省级")))
    assert len(provinces) == 31


def test_overlong_source_url_is_rejected_with_line_number(session, tmp_path: Path) -> None:
    """来源网址在数据库里有唯一索引和长度上限：超长的直接报错并指出行号，不静默跳过。"""
    long_url = "https://kw.beijing.gov.cn/" + "a" * 600
    path = write_csv(
        tmp_path,
        [
            f"省级,北京,科技,北京市科学技术委员会,通知公告,{long_url},200,20,,已验证（列表页，含近期日期）,,"
        ],
    )
    with pytest.raises(ValueError, match="第 2 行"):
        import_sources(session, path)


@pytest.mark.parametrize(
    "level,city,valid",
    [
        ("市级", "青岛", True),
        ("市级", "", False),
        ("省级", "青岛", False),
        ("省级", "", True),
        ("国家级", "青岛", False),
    ],
)
def test_import_optional_city_column(session, tmp_path, level, city, valid):
    path = tmp_path / "city.csv"
    path.write_text(
        "层级,省份,城市,部门,部门全称,栏目名称,栏目URL\n"
        f"{level},山东,{city},科技,测试机关,通知公告,https://test.gov.cn/list/\n",
        encoding="utf-8",
    )
    if not valid:
        with pytest.raises(ValueError, match="第 2 行"):
            import_sources(session, path)
        return
    import_sources(session, path)
    source = session.scalar(select(Source))
    assert source.city == (city or None)
    import_sources(session, path)
    assert session.scalar(select(Source)).city == (city or None)


def test_city_candidate_manifest_stays_disabled(session):
    """补城市字段不能让历史候选的“已验证”标签悄悄开启抓取。
    10/23 起进了第一批的 34 行移到第一批清单（test_city_first_batch.py），这里只剩 21 行。"""
    path = DEFAULT_SOURCES_CSV.parent / "地市来源候选_R1.5_v1.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 21
    assert all(row["层级"] == "市级" and row["城市"] for row in rows)
    assert sum(not row["栏目URL"] for row in rows) == 2
    summary = import_sources(session, path)
    assert (summary.created, summary.enabled, summary.disabled) == (19, 0, 19)
    assert not session.scalars(select(Source).where(Source.enabled.is_(True))).all()
    by_url = {source.url: source for source in session.scalars(select(Source))}
    for row in rows:
        if row["栏目URL"]:
            source = by_url[row["栏目URL"]]
            assert (source.level, source.province, source.city) == (
                "市级",
                row["省份"],
                row["城市"],
            )
    second = import_sources(session, path)
    assert (second.created, second.updated, second.enabled) == (0, 19, 0)
