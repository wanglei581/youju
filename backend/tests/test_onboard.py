"""来源接入 Agent（D-023）：试跑栏目网址 → 通用规则识别条目、AI 补登记信息和抓取规则 → 人确认后接入。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.ai.client import ChatClient
from youju.api import app as app_module
from youju.api import v1
from youju.api.app import app
from youju.crawler.fetcher import FetchResult
from youju.db import get_session, lock_name
from youju.models import AIUsage, Source
from youju.sources import onboard
from youju.sources.onboard import page_facts, trial, valid_config

from .conftest import read_fixture_html

URL = "https://kw.beijing.gov.cn/col/col736/index.html"
SITE = "北京市科学技术委员会、中关村科技园区管理委员会"
RULE = {"item_selector": "div.title-list-box ul.list li", "link_selector": "a.fl"}
HEADERS = {"X-Admin-Token": "secret"}


class FakeFetcher:
    def __init__(self, html="", status="ok", error=None):
        self.html, self.status, self.error = html, status, error
        self.fetched: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        return FetchResult(
            url, self.status, final_url=url, http_status=200, text=self.html, error=self.error
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def fake_ai(replies):
    """replies：按调用顺序返回的 JSON 对象；用完后重复最后一个。"""
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        reply = replies[min(len(calls), len(replies)) - 1]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8},
            },
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen-flash",
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    return client, calls


def beijing():
    return read_fixture_html("beijing_kw_tzgg")


def test_page_facts_read_site_and_column_names() -> None:
    facts = page_facts(beijing())
    assert (facts.site_name, facts.column_name) == (SITE, "通知公告")
    assert "通知公告" in facts.title and "<script" not in facts.text


def test_generic_rules_and_page_names_without_ai() -> None:
    result = trial(URL, FakeFetcher(beijing()), None)
    assert result.status == "ok" and len(result.generic) >= 3
    assert result.ai_config is None and result.ai_note is None
    p = result.proposal
    assert (p.dept_full_name, p.column_name, p.level, p.province) == (SITE, "通知公告", "", "")


def test_ai_proposal_is_checked_against_the_page() -> None:
    good = {
        "level": "省级",
        "province": "北京",
        "dept": "科技",
        "dept_full_name": "北京市科学技术委员会",
        "column_name": "通知公告",
    }
    client, calls = fake_ai([good])
    p = trial(URL, FakeFetcher(beijing()), client).proposal
    assert (p.level, p.province, p.dept, p.dept_full_name, p.column_name) == (
        "省级",
        "北京",
        "科技",
        "北京市科学技术委员会",
        "通知公告",
    )
    # 只发网页本身：标题、网站名、栏目名、列表标题
    assert SITE in calls[0]["messages"][1]["content"] and len(calls) == 1

    made_up = {
        "level": "国家级",  # 和省份对不上
        "province": "北京",
        "dept": "科技",
        "dept_full_name": "北京市科技局",  # 网页里没写
        "column_name": "最新动态",  # 网页里没写
    }
    client, _ = fake_ai([made_up])
    p = trial(URL, FakeFetcher(beijing()), client).proposal
    assert (p.level, p.province) == ("", "")
    assert (p.dept_full_name, p.column_name) == (SITE, "通知公告")  # 退回网页自己写的


def test_ai_rule_is_used_only_when_it_finds_dated_items(monkeypatch) -> None:
    # 通用规则认不出时自动请 AI 写规则，试跑识别出至少 3 条带日期的才采用
    real = onboard.extract_items
    monkeypatch.setattr(
        onboard,
        "extract_items",
        lambda html, url, config, **kw: real(html, url, config, **kw) if config else [],
    )
    meta = {"level": "省级", "province": "北京"}
    client, calls = fake_ai([meta, {**RULE, "reason": "正文列表"}])
    result = trial(URL, FakeFetcher(beijing()), client)
    assert result.generic == [] and result.ai_config == RULE and len(result.ai_items) >= 3
    assert all(item.publish_date for item in result.ai_items)
    outline = calls[1]["messages"][1]["content"]
    assert "title-list-box" in outline and "<script" not in outline and len(outline) < 8200

    for bad, note in (
        ({"item_selector": "ul[", "link_selector": "a"}, "不是合法的 CSS 选择器"),
        ({"item_selector": "div.footer li"}, "只识别出 0 条"),
    ):
        client, _ = fake_ai([meta, bad])
        result = trial(URL, FakeFetcher(beijing()), client)
        assert result.ai_config is None and note in result.ai_note


def test_ai_rule_on_request_even_when_generic_rules_work() -> None:
    client, calls = fake_ai([{}, RULE])
    result = trial(URL, FakeFetcher(beijing()), client, want_ai_rule=True)
    assert len(result.generic) >= 3 and result.ai_config == RULE and len(calls) == 2
    client, calls = fake_ai([{}])
    assert trial(URL, FakeFetcher(beijing()), client).ai_config is None and len(calls) == 1


def test_fetch_refusals_stop_the_trial() -> None:
    client, calls = fake_ai([{}])
    refused = FakeFetcher(status="robots_disallowed", error="robots.txt 不允许抓取该页面")
    result = trial(URL, refused, client)
    assert result.status == "robots_disallowed" and result.generic == [] and calls == []


def test_valid_config_accepts_only_css_selectors() -> None:
    assert valid_config(RULE) == RULE
    assert valid_config({"item_selector": " ul li ", "date_selector": ""}) == {
        "item_selector": "ul li"
    }
    for bad in (
        {"link_selector": "a"},  # 缺 item_selector
        {"item_selector": "ul li", "script": "x"},  # 多余的键
        {"item_selector": "ul li" * 50},  # 太长
        {"item_selector": "ul["},  # 不合法
        {"item_selector": 3},
        "ul li",
    ):
        assert valid_config(bad) is None, bad


@pytest.fixture
def api(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    monkeypatch.setattr(v1, "now", lambda: datetime(2026, 9, 28, 10, 0, tzinfo=UTC))
    monkeypatch.setattr(app_module, "_trial_hosts", {})
    fetcher = FakeFetcher(beijing())
    monkeypatch.setattr(app_module, "_trial_fetcher", lambda: fetcher)
    monkeypatch.setattr("youju.ai.get_chat_client", lambda: None)
    yield TestClient(app), fetcher
    app.dependency_overrides.clear()


def test_trial_endpoint_checks_the_url_and_is_polite(api, session) -> None:
    client, fetcher = api
    post = lambda url: client.post(  # noqa: E731
        "/api/admin/sources/trial", headers=HEADERS, json={"url": url}
    )
    assert post("https://example.com/news/").status_code == 422  # 只接政府网站
    assert post("ftp://kw.beijing.gov.cn/x").status_code == 422
    assert post("https://[kw.beijing.gov.cn/x").status_code == 422  # 格式不对不报 500
    assert post("https://kw.beijing.gov.cn:99999/x").status_code == 422
    res = post("  https://KW.beijing.gov.cn/col/col736/index.html#top  ")
    assert res.status_code == 200
    body = res.json()
    assert body["url"] == URL  # 去掉空白和 #片段、域名小写
    assert body["status"] == "ok" and body["generic_count"] >= 3 and len(body["generic"]) <= 10
    assert body["proposal"]["dept_full_name"] == SITE and body["ai_enabled"] is False
    assert body["generic_token"] and body["ai_token"] is None
    assert post(URL).status_code == 429  # 同一网站 10 秒内只试跑一次
    assert fetcher.fetched == [URL]
    assert client.post("/api/admin/sources/trial", json={"url": URL}).status_code == 401

    session.add(
        Source(
            level="省级",
            province="北京",
            dept="科技",
            dept_full_name=SITE,
            column_name="通知公告",
            url="https://kw.beijing.gov.cn/col/col737/",
            parser_config={},
        )
    )
    session.commit()
    # 目录和 index.html、http 和 https 算同一个栏目
    assert post("http://kw.beijing.gov.cn/col/col737/index.html").status_code == 409


def test_trial_endpoint_records_ai_usage(api, session, monkeypatch) -> None:
    client, _ = api
    ai, _ = fake_ai([{"level": "省级", "province": "北京"}, RULE])
    monkeypatch.setattr("youju.ai.get_chat_client", lambda: ai)
    body = client.post(
        "/api/admin/sources/trial", headers=HEADERS, json={"url": URL, "ai_rule": True}
    ).json()
    assert body["ai_enabled"] is True and body["proposal"]["province"] == "北京"
    assert body["ai_config"] == RULE and body["ai_token"] and body["generic_token"]
    usage = session.scalar(select(AIUsage).where(AIUsage.task == "onboard"))
    assert (usage.calls, usage.prompt_tokens) == (2, 80)


BASE = {
    "url": URL,
    "level": "省级",
    "province": "北京",
    "dept": "科技",
    "dept_full_name": SITE,
    "column_name": "通知公告",
    "parser_config": RULE,
    "reviewed_by": "创始人",
}


def test_create_source_needs_a_passed_trial_of_the_same_url_and_rule(api, session) -> None:
    client, _ = api
    token = app_module._trial_token(URL, RULE)

    def create(**changes):
        body = {**BASE, "trial_token": token, **changes}
        return client.post("/api/admin/sources", headers=HEADERS, json=body)

    assert create(province="北京市").status_code == 422  # 要从列表里选
    assert create(level="国家级").status_code == 422  # 国家级只对应"国家"
    assert create(parser_config={"item_selector": "ul["}).status_code == 422
    assert create(parser_config={"item_selector": "ns|li"}).status_code == 422
    assert create(url="https://example.com/list/").status_code == 422
    assert create(reviewed_by="   ").status_code == 422  # 去掉空白后再校验
    assert create(dept_full_name="  ").status_code == 422
    # 没试跑、凭证是别的网址或别的规则的、凭证过期，都不能接入
    assert create(trial_token="1.abc" * 3).status_code == 422
    assert create(parser_config={}).status_code == 422  # 凭证是给 AI 规则的，不能换成通用规则
    other = app_module._trial_token("https://kw.beijing.gov.cn/col/col999/", RULE)
    assert create(trial_token=other).status_code == 422
    assert create(url=URL + "#x").status_code == 201  # 同一网址（去掉 #片段后）
    assert create().status_code == 409  # 已经接入过


def test_create_source_after_a_person_confirms(api, session, monkeypatch) -> None:
    client, _ = api
    trial_body = client.post("/api/admin/sources/trial", headers=HEADERS, json={"url": URL}).json()
    body = {**BASE, "parser_config": {}, "trial_token": trial_body["generic_token"]}
    res = client.post("/api/admin/sources", headers=HEADERS, json=body)
    assert res.status_code == 201
    source = session.get(Source, res.json()["id"])
    assert (source.url, source.dept_full_name, source.enabled, source.fetch_mode) == (
        URL,
        SITE,
        True,
        "static",
    )
    assert source.parser_config == {} and source.rights_level == 1
    assert (
        source.verification_status.startswith("已验证") and "创始人" in source.verification_status
    )

    # 试跑结果 30 分钟内有效
    later = "https://kw.beijing.gov.cn/col/col740/"
    token = app_module._trial_token(later, {})
    real = app_module.clock.time
    monkeypatch.setattr(app_module.clock, "time", lambda: real() + 1801)
    body = {**BASE, "url": later, "parser_config": {}, "trial_token": token}
    assert client.post("/api/admin/sources", headers=HEADERS, json=body).status_code == 422


def test_create_waits_for_another_onboarding(api, session, db_engine, monkeypatch) -> None:
    """查重和写入串行做：别人正在接入时（持有命名锁）等不到就返回 503，不会并发写进重复来源。"""
    from sqlalchemy import text

    client, _ = api
    monkeypatch.setattr(app_module, "_CREATE_LOCK_WAIT", 0)
    body = {**BASE, "trial_token": app_module._trial_token(URL, RULE)}
    lock = {"name": lock_name("youju_source_create")}
    with db_engine.connect() as other:
        assert other.execute(text("SELECT GET_LOCK(:name, 0)"), lock).scalar() == 1
        assert client.post("/api/admin/sources", headers=HEADERS, json=body).status_code == 503
        other.execute(text("SELECT RELEASE_LOCK(:name)"), lock)
    assert client.post("/api/admin/sources", headers=HEADERS, json=body).status_code == 201


def test_lock_is_released_when_create_fails(api, session, db_engine) -> None:
    """接入失败（已经接入过返回 409）后命名锁也会释放，别人能接着接入。"""
    from sqlalchemy import text

    client, _ = api
    body = {**BASE, "trial_token": app_module._trial_token(URL, RULE)}
    assert client.post("/api/admin/sources", headers=HEADERS, json=body).status_code == 201
    assert client.post("/api/admin/sources", headers=HEADERS, json=body).status_code == 409
    lock = {"name": lock_name("youju_source_create")}
    with db_engine.connect() as other:
        assert other.execute(text("SELECT GET_LOCK(:name, 0)"), lock).scalar() == 1
        other.execute(text("SELECT RELEASE_LOCK(:name)"), lock)


@pytest.mark.parametrize(
    "level,city,province",
    [
        ("市级", None, "山东"),
        ("市级", "   ", "山东"),
        ("省级", "青岛", "山东"),
        ("国家级", "青岛", "国家"),
        ("市级", "青岛", "国家"),
        ("市级", "城" * 33, "山东"),
    ],
)
def test_city_onboard_rejects_inconsistent_location(api, level, city, province):
    client, _ = api
    body = {
        **BASE,
        "level": level,
        "province": province,
        "city": city,
        "trial_token": app_module._trial_token(URL, RULE),
    }
    assert client.post("/api/admin/sources", headers=HEADERS, json=body).status_code == 422


def test_city_onboard_persists_trimmed_city(api, session):
    client, _ = api
    body = {
        **BASE,
        "level": "市级",
        "province": "山东",
        "city": " 青岛 ",
        "trial_token": app_module._trial_token(URL, RULE),
    }
    response = client.post("/api/admin/sources", headers=HEADERS, json=body)
    assert response.status_code == 201
    assert session.get(Source, response.json()["id"]).city == "青岛"
    source = client.get("/api/sources").json()[0]
    assert (source["level"], source["city"]) == ("市级", "青岛")


def test_city_proposal_only_keeps_city_from_page():
    facts = page_facts("<title>青岛市科学技术局 通知公告</title>")
    raw = {"level": "市级", "province": "山东", "city": "青岛"}
    assert onboard._checked(raw, facts).city == "青岛"
    assert onboard._checked({**raw, "city": "济南"}, facts).city == ""
    assert onboard._checked({**raw, "level": "省级"}, facts).city == ""
