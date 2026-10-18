"""来源统计（youju/entry_stats.py）：上报只在 admin 收、只按天计数、不存身份；保存期限；每周汇总。"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju import entry_stats
from youju.api import app as app_module
from youju.api import me, v1
from youju.api.entry_stats import PATH
from youju.db import get_session
from youju.models import AccessLog, EntryStat
from youju.notify import run_cleanup

BJ = ZoneInfo("Asia/Shanghai")
# 北京时间 10 月 18 日（周日）23:50，UTC 还是 18 日 15:50；按北京时间记在 18 日
NOW = datetime(2026, 10, 18, 23, 50, tzinfo=BJ)
TODAY = date(2026, 10, 18)
TOKEN = {"X-Admin-Token": "entry-stats-token"}
# 经云调用来的请求：平台注入 X-WX-SOURCE，也会带上 openid
CLOUD = {"X-WX-SOURCE": "wx_client", "X-WX-OPENID": "o-entry-user"}
MINIAPP = Path(__file__).resolve().parents[2] / "apps" / "miniapp"


@pytest.fixture
def make_client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(v1, "now", lambda: NOW)

    def build(role: str) -> TestClient:
        monkeypatch.setattr(
            app_module,
            "get_settings",
            lambda: SimpleNamespace(role=role, admin_token=TOKEN["X-Admin-Token"]),
        )
        # 线上 admin 不能打开 trust_wx_headers，也不能设开发用的 openid（config.Settings 会拒绝）
        monkeypatch.setattr(
            me,
            "get_settings",
            lambda: SimpleNamespace(
                trust_wx_headers=role == "api", dev_openid="dev-user" if role == "all" else None
            ),
        )
        return TestClient(app_module.app)

    app_module.app.dependency_overrides[get_session] = override
    yield build
    app_module.app.dependency_overrides.clear()


def stats(session) -> list[tuple]:
    session.expire_all()
    return [
        (r.day, r.scene, r.code, r.page, r.opens)
        for r in session.scalars(
            select(EntryStat).order_by(EntryStat.day, EntryStat.scene, EntryStat.code)
        )
    ]


def test_table_has_no_identity_or_time_columns():
    assert set(EntryStat.__table__.columns.keys()) == {
        "id",
        "day",
        "scene",
        "code",
        "page",
        "opens",
    }


def test_admin_counts_opens_by_day_and_logs_no_user_number(make_client, session):
    client = make_client("admin")
    detail = {"scene": 1008, "from": "gzh1103", "page": "pages/detail/index"}
    for body in (detail, detail, {"scene": 1007, "page": "pages/home/index"}):
        response = client.post(PATH, json=body, headers=CLOUD)
        assert response.status_code == 204 and response.content == b""
    assert stats(session) == [
        (TODAY, 1007, "", "pages/home/index", 1),
        (TODAY, 1008, "gzh1103", "pages/detail/index", 2),
    ]
    # 访问日志照常记（D-028），但 admin 不认身份头：没有用户编号，也没有 openid、请求内容
    logs = list(session.scalars(select(AccessLog).where(AccessLog.path == PATH)))
    assert len(logs) == 3
    assert {(row.service, row.method, row.status, row.uid) for row in logs} == {
        ("admin", "POST", 204, None)
    }
    dumped = json.dumps(
        [[str(getattr(row, c.key)) for c in AccessLog.__table__.columns] for row in logs]
    )
    assert "o-entry-user" not in dumped and "gzh1103" not in dumped and "detail" not in dumped


def test_only_admin_receives_reports_from_cloud_calls(make_client, session):
    admin = make_client("admin")
    body = {"scene": 1001, "page": "pages/home/index"}
    # 没有平台注入的 X-WX-SOURCE（不是经云调用来的）、不是 POST：admin 上都是 404
    assert admin.post(PATH, json=body).status_code == 404
    assert admin.get(PATH, headers=CLOUD).status_code == 404
    # api 上不开放：那里信任身份头，访问日志会带用户编号
    api = make_client("api")
    assert api.post(PATH, json=body, headers=CLOUD).status_code == 404
    assert stats(session) == []
    # 本机开发（一个服务两种角色）照常收，方便自测
    assert make_client("all").post(PATH, json=body).status_code == 204
    assert stats(session) == [(TODAY, 1001, "", "pages/home/index", 1)]


def test_unrecognised_values_are_counted_without_being_stored(make_client, session):
    client = make_client("admin")
    for body in (
        # 场景值不是四位数、编号像人名、页面带参数：都不存原样，只记一次打开
        {"scene": 12, "from": "Zhang San", "page": "pages/detail/index?id=5"},
        {"scene": 1047, "from": "x" * 25, "page": "pages/../me/index"},
    ):
        assert client.post(PATH, json=body, headers=CLOUD).status_code == 204
    # 大写当小写，页面前面的 / 去掉
    body = {"scene": 1047, "from": " Export ", "page": "/pages/home/index"}
    assert client.post(PATH, json=body, headers=CLOUD).status_code == 204
    assert stats(session) == [
        (TODAY, 0, "", "", 1),
        (TODAY, 1047, "", "", 1),
        (TODAY, 1047, "export", "pages/home/index", 1),
    ]


@pytest.mark.parametrize(
    "content,status",
    [
        # 多带字段（如身份、检索词）的整条拒收
        (json.dumps({"scene": 1001, "openid": "o-entry-user"}), 422),
        (json.dumps({"scene": 1001, "q": "某某科技有限公司"}), 422),
        (json.dumps({"from": "gzh1103"}), 422),
        (json.dumps({"scene": "群聊"}), 422),
        ("不是 JSON", 422),
        (json.dumps({"scene": 1001, "from": "a" * 2000}), 413),
    ],
)
def test_bad_or_large_bodies_are_refused_without_echo(make_client, session, content, status):
    response = make_client("admin").post(
        PATH, content=content.encode(), headers={**CLOUD, "content-type": "application/json"}
    )
    assert response.status_code == status
    assert "o-entry-user" not in response.text and "某某" not in response.text
    assert stats(session) == []


def test_body_limit_also_applies_without_declared_length(make_client, session):
    chunks = iter([b'{"scene": 1001, "from": "', b"a" * 1100, b'"}'])
    response = make_client("admin").post(PATH, content=chunks, headers=CLOUD)
    assert response.status_code == 413
    assert stats(session) == []


def test_new_combinations_beyond_daily_cap_share_one_row(make_client, session, monkeypatch):
    monkeypatch.setattr(entry_stats, "MAX_ROWS_PER_DAY", 2)
    client = make_client("admin")
    for code in ("a1", "a2", "a3", "a4", "a1"):
        body = {"scene": 1058, "from": code, "page": "pages/home/index"}
        assert client.post(PATH, json=body, headers=CLOUD).status_code == 204
    # 已有的组合照常加；满了以后新出现的 a3、a4 都记进"超出上限"那一行
    assert stats(session) == [
        (TODAY, 0, entry_stats.OVERFLOW_CODE, "", 2),
        (TODAY, 1058, "a1", "pages/home/index", 2),
        (TODAY, 1058, "a2", "pages/home/index", 1),
    ]


def test_cleanup_keeps_400_days(session):
    session.add_all(
        [
            EntryStat(day=TODAY - timedelta(days=401), scene=1001, opens=3),
            EntryStat(day=TODAY - timedelta(days=400), scene=1001, opens=2),
            EntryStat(day=TODAY, scene=1001, opens=1),
        ]
    )
    session.commit()
    assert run_cleanup(session, stamp=NOW)["entry_stats"] == 1
    assert [row[0] for row in stats(session)] == [TODAY - timedelta(days=400), TODAY]


def seed_weeks(session):
    rows = [
        # 上周（10/5–10/11）
        (date(2026, 10, 6), 1047, "export", "pages/home/index", 2),
        # 本周（10/12–10/18）
        (date(2026, 10, 12), 1008, "gzh1103", "pages/detail/index", 3),
        (date(2026, 10, 18), 1129, "", "pages/detail/index", 5),
        (date(2026, 10, 18), 1001, "", "pages/home/index", 1),
        (date(2026, 10, 18), 1300, "", "", 4),
        # 范围外：再早一周、"明天"（时钟误差）
        (date(2026, 10, 4), 1007, "", "pages/home/index", 9),
        (date(2026, 10, 19), 1007, "", "pages/home/index", 9),
    ]
    session.add_all(EntryStat(day=d, scene=s, code=c, page=p, opens=n) for d, s, c, p, n in rows)
    session.commit()


def test_weekly_summary_by_channel_code_page_and_scene(session):
    seed_weeks(session)
    out = entry_stats.weekly(session, today=TODAY, weeks=2)
    assert out["weeks"] == [
        {"start": date(2026, 10, 5), "end": date(2026, 10, 11), "current": False},
        {"start": date(2026, 10, 12), "end": TODAY, "current": True},
    ]
    # 爬虫单列，不算进合计和其他各表
    assert out["total"] == [2, 8] and out["crawler"] == [0, 5]
    channels = {c["name"]: c["counts"] for c in out["channels"]}
    assert channels["群聊卡片"] == [0, 3] and channels["扫小程序码"] == [2, 0]
    assert channels["最近使用、我的小程序、桌面"] == [0, 1] and channels["其他"] == [0, 4]
    assert sum(sum(c) for c in channels.values()) == 10
    assert [(c["code"], c["counts"]) for c in out["codes"]] == [
        ("", [0, 5]),
        ("gzh1103", [0, 3]),
        ("export", [2, 0]),
    ]
    assert [(p["page"], p["name"], p["counts"]) for p in out["pages"]] == [
        ("", "", [0, 4]),
        ("pages/detail/index", "通知详情", [0, 3]),
        ("pages/home/index", "首页", [2, 1]),
    ]
    assert out["scenes"][0] == {"scene": 1300, "name": "", "channel": "其他", "counts": [0, 4]}
    assert out["scenes"][1]["name"] == "群聊会话中的小程序消息卡片"
    assert out["keep_days"] == 400


def test_weekly_summary_endpoint_and_console_need_admin_token(make_client, session):
    seed_weeks(session)
    client = make_client("admin")
    assert client.get("/api/admin/entry-stats").status_code == 401
    response = client.get("/api/admin/entry-stats?weeks=2", headers=TOKEN)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["weeks"][1] == {"start": "2026-10-12", "end": "2026-10-18", "current": True}
    assert data["total"] == [2, 8]
    for weeks in (0, 27):
        assert client.get(f"/api/admin/entry-stats?weeks={weeks}", headers=TOKEN).status_code == 422
    for query in ("", "?embed=1"):
        page = client.get(f"/admin/entry-stats{query}")
        assert page.status_code == 200
        assert "/admin/embed.js" in page.text and "youjuAdminToken()" in page.text
    portal = client.get("/admin").text
    assert 'data-menu="entry-stats"' in portal and "'entry-stats':'来源统计'" in portal
    # api 服务上没有管理台
    assert make_client("api").get("/api/admin/entry-stats", headers=TOKEN).status_code == 404


def test_page_rule_covers_every_miniapp_page():
    pages = json.loads((MINIAPP / "app.json").read_text(encoding="utf-8"))["pages"]
    assert pages and all(entry_stats.clean_page(page) == page for page in pages)
    assert set(entry_stats.PAGE_NAMES) - {"pages/digest/index"} <= set(pages)
