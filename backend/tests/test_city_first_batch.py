"""地市第一批来源（D-086 ④）：清单里的栏目导入以后全部停用，不抓取、不进收录范围和城市筛选，
小程序和 api 都看不到；创始人同意开哪几个城市，才把那几行改成"已验证（……）"再导入。"""

import csv
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju import cli
from youju.api import v1
from youju.api.app import app
from youju.config import DEFAULT_SOURCES_CSV
from youju.crawler import service
from youju.db import get_session
from youju.models import Source
from youju.sources.importer import import_sources

BATCH = DEFAULT_SOURCES_CSV.parent / "地市来源第一批_2026-10-23.csv"
CANDIDATES = DEFAULT_SOURCES_CSV.parent / "地市来源候选_R1.5_v1.csv"
# D-061 第五节"已核实 19 个城市可接"
VERIFIED_CITIES = {
    *("广州", "深圳", "东莞", "苏州", "武汉", "青岛", "长沙", "郑州", "西安", "厦门"),
    *("常州", "福州", "南昌", "石家庄", "南宁", "沈阳", "长春", "贵阳", "大连"),
}
# 2026-10-23 本机试抓时 robots 不让抓（青岛工信取不到 robots.txt、大连科技明确禁止；青岛科技的
# 原文链接是 http，http 的 robots.txt 取不到），或者正文读不出（沈阳两局）：都不进第一批
NOT_READY = {
    "http://gxj.qingdao.gov.cn/tzgg/",
    "https://kjj.dl.gov.cn/col/col4901/index.html",
    "https://qdstc.qingdao.gov.cn/kjdt/tzgg/",
    "https://kjj.shenyang.gov.cn/kjgz/tzgg/",
    "https://jxw.shenyang.gov.cn/tzgg/",
}
NOW = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_batch_rows_come_from_verified_candidates():
    batch = read(BATCH)
    assert len(batch) == 31
    assert {row["城市"] for row in batch} <= VERIFIED_CITIES
    assert all(row["层级"] == "市级" and row["城市"] and row["栏目URL"] for row in batch)
    assert all(row["验证状态"].startswith("待开") for row in batch)
    urls = [row["栏目URL"] for row in batch]
    assert len(set(urls)) == len(urls)
    assert not NOT_READY & set(urls)
    assert all(row["部门全称"].startswith(row["城市"] + "市") for row in batch)
    # 每个网址只在一份清单里：两份清单都管同一个网址时，先后导入会互相改回启用状态
    others = {row["栏目URL"] for path in (CANDIDATES, DEFAULT_SOURCES_CSV) for row in read(path)}
    assert not others & set(urls)


def test_batch_imports_switched_off(session):
    summary = import_sources(session, BATCH)
    assert (summary.created, summary.enabled, summary.disabled) == (31, 0, 31)
    sources = session.scalars(select(Source)).all()
    assert {(s.level, s.enabled, s.fetch_mode, s.rights_level) for s in sources} == {
        ("市级", False, "static", 1)
    }
    assert all(s.city and s.parser_config == {} for s in sources)
    # 和候选清单先后、反复导入都不会打开，也不会多出或改动第一批的来源
    import_sources(session, CANDIDATES)
    again = import_sources(session, BATCH)
    assert (again.created, again.updated, again.enabled) == (0, 31, 0)
    assert not session.scalars(select(Source).where(Source.enabled.is_(True))).all()


def test_opening_takes_an_explicit_status_change_per_row(session, tmp_path):
    """开的办法：把要开的那几行改成"已验证（……）"再导入一次；没改的行照样关着。"""
    batch = read(BATCH)
    batch[0]["验证状态"] = "已验证（创始人同意开，测试）"
    path = tmp_path / "opened.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(batch[0]))
        writer.writeheader()
        writer.writerows(batch)
    import_sources(session, BATCH)
    summary = import_sources(session, path)
    assert (summary.created, summary.enabled) == (0, 1)
    opened = session.scalars(select(Source).where(Source.enabled.is_(True))).all()
    assert [s.url for s in opened] == [batch[0]["栏目URL"]]


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(v1, "now", lambda: NOW)
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_switched_off_batch_is_invisible_and_not_crawled(client, session, monkeypatch):
    import_sources(session, DEFAULT_SOURCES_CSV)
    import_sources(session, BATCH)
    province = session.scalar(
        select(Source).where(Source.level == "省级", Source.province == "广东", Source.enabled)
    )
    province.last_success_at = NOW
    # 就算有人手动抓过一次（记下了成功时间），停用的也不算收录
    guangzhou = session.scalar(select(Source).where(Source.city == "广州"))
    guangzhou.last_success_at = NOW
    session.commit()

    body = client.get("/api/v1/coverage").json()
    assert body["cities"] == []
    columns = {c["name"] for r in body["regions"] for c in r["columns"]}
    assert f"{province.dept_full_name} · {province.column_name}" in columns
    city_columns = {
        f"{s.dept_full_name} · {s.column_name}"
        for s in session.scalars(select(Source).where(Source.level == "市级"))
    }
    assert not city_columns & columns
    res = client.get("/api/v1/search", params={"cities": "广东/广州"})
    assert res.status_code == 422

    # 定时抓取（youju crawl，不带 --include-disabled）不会选中第一批的任何栏目
    picked: list[int] = []

    def fake_crawl(factory, ids, fetcher, **kwargs):
        picked.extend(ids)
        return []

    monkeypatch.setattr(service, "crawl_sources", fake_crawl)
    assert cli.main(["crawl"]) == 0
    batch_ids = set(session.scalars(select(Source.id).where(Source.level == "市级")))
    assert picked and len(batch_ids) == 31
    assert not batch_ids & set(picked)
    # 调试用的 --include-disabled 也不抓"待开"的（抓了就有文件，停用也不会下架）；
    # 别的停用来源照旧能用它手动检查
    picked.clear()
    assert cli.main(["crawl", "--include-disabled"]) == 0
    others = set(session.scalars(select(Source.id).where(Source.level != "市级")))
    assert not batch_ids & set(picked)
    assert set(picked) == others


def test_opening_some_cities_keeps_the_rest_out(client, session, tmp_path):
    """只开第一波几个城市时，收录范围只列这几个城市，没开的城市照样不能筛选。"""
    wave = {"广州", "深圳", "东莞", "武汉"}
    batch = read(BATCH)
    for row in batch:
        if row["城市"] in wave:
            row["验证状态"] = "已验证（创始人同意开，测试）"
    path = tmp_path / "wave1.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(batch[0]))
        writer.writeheader()
        writer.writerows(batch)
    import_sources(session, BATCH)
    summary = import_sources(session, path)
    assert summary.enabled == 8
    for source in session.scalars(select(Source)):
        source.last_success_at = NOW  # 开了的抓成功过；没开的就算有成功时间也不算
    session.commit()

    cities = client.get("/api/v1/coverage").json()["cities"]
    assert {(c["province"], c["city"], c["sources"]) for c in cities} == {
        ("广东", "广州", 2),
        ("广东", "深圳", 2),
        ("广东", "东莞", 2),
        ("湖北", "武汉", 2),
    }
    assert client.get("/api/v1/search", params={"cities": "广东/广州"}).status_code == 200
    assert client.get("/api/v1/search", params={"cities": "福建/厦门"}).status_code == 422
