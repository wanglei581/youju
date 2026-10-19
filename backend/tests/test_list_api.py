"""栏目列表由页面脚本从公开接口取数（湖南工信厅 10/10 改版，D2）：接口配置、请求、解析和抓取。

fixtures/api/ 下两份是 2026-10-19 按页面脚本同样的参数请求接口拿到的真实响应，
去掉了正文摘录（content，可能含联系人和电话），只留索引号、来源处室等公开的元数据。
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import func, select

from youju import cli
from youju.crawler.fetcher import FetchResult, PoliteFetcher
from youju.crawler.list_api import (
    API_EMPTY_MESSAGE,
    API_NOT_JSON_MESSAGE,
    HUNAN_SEARCH,
    ApiTrial,
    api_refusal,
    api_request,
    parse_api_items,
    read_api_response,
    trial_api,
    trial_problem,
    valid_api_config,
)
from youju.crawler.listing import ListItem
from youju.crawler.service import check_source, crawl_sources
from youju.models import Document, Source, SourceCheck
from youju.sources import onboard

TODAY = date(2026, 10, 19)
API = Path(__file__).parent / "fixtures" / "api"
TZGG = "https://gxt.hunan.gov.cn/gxt/xxgk_71033/tzgg/index.html"
GSGG = "https://gxt.hunan.gov.cn/gxt/xxgk_71033/gsgg01/index.html"
TZGG_CONFIG = {"list_api": HUNAN_SEARCH, "channel_id": "71043"}
GSGG_CONFIG = {"list_api": HUNAN_SEARCH, "channel_id": "b3490a5474f54dc6b592c446321edebc"}
TZGG_API = "https://api.hunan.gov.cn/search/common/search/71043"
GSGG_API = "https://api.hunan.gov.cn/search/common/search/b3490a5474f54dc6b592c446321edebc"
# 浏览器打开栏目页时 list_all.js 发出的表单（jQuery 编码），2026-10-19 照页面脚本核对
BROWSER_FORM = (
    "datas%5B0%5D%5Bkey%5D=status&datas%5B0%5D%5Bvalue%5D=4&datas%5B0%5D%5Bjoin%5D=and"
    "&datas%5B0%5D%5BqueryType%5D=term&datas%5B1%5D%5Bkey%5D=publishedTime"
    "&datas%5B1%5D%5Bsort%5D=true&datas%5B1%5D%5Border%5D=desc"
    "&datas%5B1%5D%5BqueryType%5D=term&page=1&_pageSize=20&_isAgg=true"
)


def api_text(name: str) -> str:
    return (API / f"{name}.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "page_url", "column_dir", "first"),
    [
        (
            "hunan_gxt_tzgg_71043",
            TZGG,
            "/gxt/xxgk_71033/tzgg/",
            (
                "关于2026年度全省经济系列高级和正高级经济师网上面试答辩的通知",
                "https://gxt.hunan.gov.cn/gxt/xxgk_71033/tzgg/202610/t20261016_34082931.html",
                date(2026, 10, 16),
            ),
        ),
        (
            "hunan_gxt_gsgg01",
            GSGG,
            "/gxt/xxgk_71033/gsgg01/",
            (
                "关于公开招募“长沙银行杯·创客湖南”特训营授课专家和赋能服务机构的公告",
                "https://gxt.hunan.gov.cn/gxt/xxgk_71033/gsgg01/202610/t20261016_34082527.html",
                date(2026, 10, 16),
            ),
        ),
    ],
)
def test_real_responses_give_title_url_and_date(name, page_url, column_dir, first) -> None:
    items = parse_api_items(api_text(name), page_url, TODAY)
    assert len(items) == 20
    assert (items[0].title, items[0].url, items[0].publish_date) == first
    # 站内路径按栏目页补全到 gxt.hunan.gov.cn，不是接口所在的 api.hunan.gov.cn
    assert all(i.url.startswith("https://gxt.hunan.gov.cn" + column_dir) for i in items)
    assert all(i.publish_date for i in items)
    # 和页面一样按发布时间倒序
    dates = [i.publish_date for i in items]
    assert dates == sorted(dates, reverse=True)


def test_request_is_the_same_as_the_page_script() -> None:
    request = api_request(TZGG_CONFIG)
    assert request is not None
    assert request.url == TZGG_API
    assert list(request.form) == parse_qsl(BROWSER_FORM)
    gsgg = api_request(GSGG_CONFIG)
    assert (
        gsgg.url == "https://api.hunan.gov.cn/search/common/search/b3490a5474f54dc6b592c446321edebc"
    )


@pytest.mark.parametrize("config", [None, {}, {"item_selector": "ul li"}])
def test_sources_without_list_api_fetch_the_page(config) -> None:
    assert api_request(config) is None


@pytest.mark.parametrize(
    "config",
    [
        {"list_api": "other_site", "channel_id": "71043"},
        {"list_api": HUNAN_SEARCH},
        {"list_api": HUNAN_SEARCH, "channel_id": ""},
        {"list_api": HUNAN_SEARCH, "channel_id": "71043/../x"},
        {"list_api": HUNAN_SEARCH, "channel_id": "7" * 65},
        {"list_api": HUNAN_SEARCH, "channel_id": 71043},
        {"list_api": HUNAN_SEARCH, "channel_id": "71043", "item_selector": "ul li"},
    ],
)
def test_bad_list_api_config_is_rejected_not_ignored(config) -> None:
    assert valid_api_config(config) is None
    # 写错了不能悄悄退回抓栏目页：那样只会每轮"没识别到条目"，看不出是配置的问题
    with pytest.raises(ValueError):
        api_request(config)


def test_unexpected_responses_give_no_items() -> None:
    for text in ("", "<html>维护中</html>", "[]", '{"data": {}}', '{"data": {"results": {}}}'):
        assert parse_api_items(text, TZGG, TODAY) == []


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        # api.hunan.gov.cn 网关对要口令的路径就是这样回的（HTTP 200）
        ('{"msg":"invalid token","code":401}', True),
        ('{"code":"403","msg":"forbidden"}', True),
        ('{"code":401,"data":{"results":[]}}', True),
        ('{"code":500,"msg":"error"}', False),
        ('{"data":{"results":[]}}', False),
        ("[]", False),
        ("<html>维护中</html>", False),
    ],
)
def test_only_explicit_refusals_count_as_access_denied(text, refused) -> None:
    message = api_refusal(text)
    assert (message is not None) == refused
    if refused:
        assert "不绕过" in message and "D-013" in message


def test_items_are_cleaned_deduplicated_and_dated_from_url_when_needed() -> None:
    results = [
        "不是对象",
        {"title": "", "url": "/gxt/a.html"},
        {"title": "缺网址的条目标题", "url": None},
        {
            "title": "关于<b>开展</b>专项申报 工作的通知",
            "url": "/gxt/xxgk_71033/tzgg/202610/t20261015_1.html",
        },
        {
            "title": "重复网址只留第一次出现的",
            "url": "/gxt/xxgk_71033/tzgg/202610/t20261015_1.html",
        },
        {
            "title": "接口没给发布时间时用网址里的日期",
            "url": "/gxt/xxgk_71033/tzgg/202610/t20261012_2.html",
        },
        {
            "title": "外站链接照收",
            "url": "https://www.miit.gov.cn/a/b.html",
            "publishedTimeStr": "2026-10-11 09:00:00",
        },
        {"title": "javascript 链接不收", "url": "javascript:void(0)"},
        # 写坏的网址只跳过这一条，不连累整批（以前会抛异常、整个栏目记成内部错误）
        {"title": "主机名括号不配对的网址", "url": "http://[gxt.hunan.gov.cn/a.html"},
        {"title": "端口超出范围的网址", "url": "https://gxt.hunan.gov.cn:99999/b.html"},
    ]
    items = parse_api_items(json.dumps({"data": {"results": results}}), TZGG, TODAY)
    assert [(i.title, i.url, i.publish_date) for i in items] == [
        (
            "关于 开展 专项申报 工作的通知",
            "https://gxt.hunan.gov.cn/gxt/xxgk_71033/tzgg/202610/t20261015_1.html",
            date(2026, 10, 15),
        ),
        (
            "接口没给发布时间时用网址里的日期",
            "https://gxt.hunan.gov.cn/gxt/xxgk_71033/tzgg/202610/t20261012_2.html",
            date(2026, 10, 12),
        ),
        ("外站链接照收", "https://www.miit.gov.cn/a/b.html", date(2026, 10, 11)),
    ]


# ---- 抓取器：表单 POST 照样遵守 robots、限速，接口跳转不跟随 ----


def public_dns(host: str) -> list[str]:
    return ["1.2.3.4"]


def make_fetcher(handler, **kwargs) -> PoliteFetcher:
    return PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0.0,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
        **kwargs,
    )


def test_form_is_posted_after_robots_check() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/robots.txt":
            # api.hunan.gov.cn 的 robots.txt 实际返回 200 和一段 JSON（不是规则），按 RFC 9309 视为没有限制
            return httpx.Response(200, text='{"msg":"invalid token","code":401}')
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert "YoujuBot" in request.headers["user-agent"]
        assert request.content.decode() == BROWSER_FORM
        return httpx.Response(
            200,
            text=api_text("hunan_gxt_tzgg_71043"),
            headers={"content-type": "application/json; charset=utf-8"},
        )

    request = api_request(TZGG_CONFIG)
    with make_fetcher(handler) as fetcher:
        result = fetcher.fetch(request.url, form=request.form)
    assert result.status == "ok"
    assert seen == [("GET", "/robots.txt"), ("POST", "/search/common/search/71043")]
    assert len(parse_api_items(result.text, TZGG, TODAY)) == 20


def test_form_post_obeys_robots_disallow() -> None:
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")
        posted.append(request.url.path)
        return httpx.Response(200, text="{}")

    request = api_request(TZGG_CONFIG)
    with make_fetcher(handler) as fetcher:
        result = fetcher.fetch(request.url, form=request.form)
    assert result.status == "robots_disallowed"
    assert posted == []


def test_form_post_does_not_follow_redirects() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://api.hunan.gov.cn/login"})

    request = api_request(TZGG_CONFIG)
    with make_fetcher(handler) as fetcher:
        result = fetcher.fetch(request.url, form=request.form)
    assert (result.status, result.http_status) == ("http_error", 302)
    assert result.error == "HTTP 302，接口跳转到 https://api.hunan.gov.cn/login，没有跟随"
    assert requested == [TZGG_API]


def test_form_post_respects_per_host_interval() -> None:
    now = [0.0]
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="{}")

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    fetcher = PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=3.0,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
        clock=lambda: now[0],
        sleep=sleep,
    )
    with fetcher:
        for config in (TZGG_CONFIG, GSGG_CONFIG):
            request = api_request(config)
            assert fetcher.fetch(request.url, form=request.form).status == "ok"
    # robots.txt、两次接口请求，每两次之间等满 3 秒
    assert slept == [3.0, 3.0]


# ---- 抓取流程：配了列表接口的来源请求接口，条目链接按栏目页补全 ----


class FakeFetcher:
    def __init__(self, responses: dict[str, list[FetchResult]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple | None]] = []

    def fetch(self, url: str, form=None) -> FetchResult:
        self.calls.append((url, None if form is None else tuple(form)))
        return self.responses[url].pop(0)


def make_source(session, url: str = TZGG, config: dict | None = None, column: str = "文件通知"):
    source = Source(
        level="省级",
        province="湖南",
        dept="工信",
        dept_full_name="湖南省工业和信息化厅",
        column_name=column,
        url=url,
        fetch_mode="static",
        enabled=True,
        parser_config=dict(TZGG_CONFIG) if config is None else config,
    )
    session.add(source)
    session.commit()
    return source


def api_ok(text: str) -> FetchResult:
    return FetchResult(TZGG_API, "ok", final_url=TZGG_API, http_status=200, text=text)


def test_check_source_with_list_api_saves_items_under_the_column_page(session) -> None:
    source = make_source(session)
    fetcher = FakeFetcher({TZGG_API: [api_ok(api_text("hunan_gxt_tzgg_71043"))]})
    check = check_source(session, source, fetcher, today=TODAY)
    assert (check.status, check.items_found, check.items_new) == ("ok", 20, 20)
    assert fetcher.calls == [(TZGG_API, api_request(TZGG_CONFIG).form)]
    urls = session.scalars(select(Document.url).where(Document.source_id == source.id)).all()
    assert len(urls) == 20
    assert all(u.startswith("https://gxt.hunan.gov.cn/gxt/xxgk_71033/tzgg/") for u in urls)
    # 来源网址仍是栏目页（用户看到的、相对链接的基准都不变）
    assert session.get(Source, source.id).url == TZGG


def test_list_api_that_returns_nothing_is_reported_as_such(session) -> None:
    source = make_source(session)
    fetcher = FakeFetcher({TZGG_API: [api_ok('{"data":{"total":0,"results":[]}}')]})
    check = check_source(session, source, fetcher, today=TODAY)
    assert (check.status, check.error) == ("parse_empty", API_EMPTY_MESSAGE)
    assert source.consecutive_failures == 1


def denied_with_items() -> str:
    data = json.loads(api_text("hunan_gxt_tzgg_71043"))
    return json.dumps({"code": 403, "msg": "forbidden", **data}, ensure_ascii=False)


@pytest.mark.parametrize(
    "text", ['{"msg":"invalid token","code":401}', "denied_with_items"], ids=["plain", "with_items"]
)
def test_list_api_refusal_is_recorded_as_access_denied(session, text) -> None:
    source = make_source(session)
    body = denied_with_items() if text == "denied_with_items" else text
    fetcher = FakeFetcher({TZGG_API: [api_ok(body)]})
    check = check_source(session, source, fetcher, today=TODAY)
    assert check.status == "access_denied" and check.items_found == 0
    assert "停用" in check.error and "D-013" in check.error
    # 和其他失败一样累计，连续失败时照常提醒，由人确认后停用；明确拒绝的回复里夹着条目也不收
    assert source.consecutive_failures == 1
    assert session.scalar(select(func.count(Document.id))) == 0


class ChangingFetcher(FakeFetcher):
    """抓取期间（网络请求还没回来时）另一个连接改了来源配置，模拟同时有人在跑 source-list-api。"""

    def __init__(self, responses, session_factory, source_id, configs) -> None:
        super().__init__(responses)
        self.session_factory, self.source_id, self.configs = session_factory, source_id, configs

    def fetch(self, url: str, form=None) -> FetchResult:
        if not self.calls:
            self.first_call_at = datetime.now(UTC)
        if self.configs:
            with self.session_factory() as other:
                other.get(Source, self.source_id).parser_config = self.configs.pop(0)
                other.commit()
        return super().fetch(url, form)


def test_config_changed_during_the_check_is_rechecked_with_the_new_config(
    session, session_factory
) -> None:
    source = make_source(session, config={})
    page = FetchResult(TZGG, "ok", final_url=TZGG, http_status=200, text="<html></html>")
    fetcher = ChangingFetcher(
        {TZGG: [page], TZGG_API: [api_ok(api_text("hunan_gxt_tzgg_71043"))]},
        session_factory,
        source.id,
        [dict(TZGG_CONFIG)],
    )
    before = datetime.now(UTC)
    check = check_source(session, source, fetcher, today=TODAY)
    # 旧配置（抓栏目页）的结果作废，按新配置请求接口；只记一条检查，起始时间是整次检查的开头
    assert [url for url, _ in fetcher.calls] == [TZGG, TZGG_API]
    assert before <= check.started_at <= fetcher.first_call_at
    assert (check.status, check.items_found) == ("ok", 20)
    assert session.scalar(select(func.count(SourceCheck.id))) == 1
    assert source.consecutive_failures == 0


def test_config_that_keeps_changing_is_an_error_not_a_stale_result(session, session_factory):
    source = make_source(session, config={})
    page = FetchResult(TZGG, "ok", final_url=TZGG, http_status=200, text="<html></html>")
    api = api_ok(api_text("hunan_gxt_tzgg_71043"))
    fetcher = ChangingFetcher(
        {TZGG: [page, page], TZGG_API: [api, api]},
        session_factory,
        source.id,
        [dict(TZGG_CONFIG), {}, dict(TZGG_CONFIG)],
    )
    with pytest.raises(RuntimeError):
        check_source(session, source, fetcher, today=TODAY)
    session.rollback()
    assert len(fetcher.calls) == 3
    assert session.scalar(select(func.count(SourceCheck.id))) == 0
    assert session.scalar(select(func.count(Document.id))) == 0


def test_bad_list_api_config_is_an_internal_error_not_a_page_fetch(session_factory) -> None:
    with session_factory() as session:
        source = make_source(session, config={"list_api": HUNAN_SEARCH, "channel_id": "a/b"})
        source_id = source.id
    fetcher = FakeFetcher({})
    [summary] = crawl_sources(session_factory, [source_id], fetcher)
    assert summary.status == "internal_error"
    assert fetcher.calls == []
    with session_factory() as session:
        assert session.scalar(select(SourceCheck.status)) == "internal_error"


# ---- youju source-list-api：先试跑接口，够条数且加 --apply 才写入 ----


@pytest.fixture
def run_cli(monkeypatch, session_factory):
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    holder: dict = {}

    class TrialFetcher(FakeFetcher):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def fetch(self, url, form=None):
            hook = getattr(run, "before_fetch", None)
            if hook:
                hook()
            return super().fetch(url, form)

    def use(responses):
        holder["fetcher"] = TrialFetcher(responses)

    monkeypatch.setattr(onboard, "trial_fetcher", lambda: holder["fetcher"])

    def run(*argv, responses=None):
        use(responses or {})
        return cli.main(["source-list-api", *argv])

    run.fetcher = lambda: holder["fetcher"]
    return run


def test_cli_dry_run_then_apply(run_cli, session, capsys) -> None:
    source = make_source(session, config={})
    responses = {TZGG_API: [api_ok(api_text("hunan_gxt_tzgg_71043"))]}
    assert run_cli(str(source.id), "--channel", "71043", responses=responses) == 0
    out = capsys.readouterr().out
    assert "识别出 20 条，带日期 20 条；接口返回的栏目名：文件通知" in out
    assert "没有写数据库" in out
    assert out.strip().endswith(f"result: source={source.id} items=20 dated=20 written=no")
    session.expire_all()
    assert session.get(Source, source.id).parser_config == {}

    responses = {TZGG_API: [api_ok(api_text("hunan_gxt_tzgg_71043"))]}
    assert run_cli(str(source.id), "--channel", "71043", "--apply", responses=responses) == 0
    assert capsys.readouterr().out.strip().endswith("items=20 dated=20 written=yes")
    session.expire_all()
    assert session.get(Source, source.id).parser_config == TZGG_CONFIG


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('{"data":{"results":[]}}', "列表接口可以访问，但没有返回列表条目"),
        ("two_rows", "带日期的条目少于 3 条"),
    ],
)
def test_cli_refuses_to_write_when_trial_finds_too_few_items(
    run_cli, session, capsys, body, reason
) -> None:
    source = make_source(session, config={})
    text = api_json([row(0), row(1)]) if body == "two_rows" else body
    responses = {TZGG_API: [api_ok(text)]}
    assert run_cli(str(source.id), "--channel", "71043", "--apply", responses=responses) == 1
    out = capsys.readouterr().out
    assert reason in out and "没有写入配置" in out
    session.expire_all()
    assert session.get(Source, source.id).parser_config == {}


def test_cli_refuses_a_channel_of_another_column(run_cli, session, capsys) -> None:
    # 条目都在栏目目录下、栏目编号也回显一致，但栏目名对不上（来源登记的是"公示公告"）
    source = make_source(session, url=TZGG, config={}, column="公示公告")
    responses = {TZGG_API: [api_ok(api_text("hunan_gxt_tzgg_71043"))]}
    assert run_cli(str(source.id), "--channel", "71043", "--apply", responses=responses) == 1
    out = capsys.readouterr().out
    assert (
        "接口返回的栏目名（文件通知）里没有来源登记的“公示公告”，请核对栏目编号；没有写入配置"
        in out
    )
    assert out.strip().endswith("written=no")
    session.expire_all()
    assert session.get(Source, source.id).parser_config == {}


def test_cli_refuses_items_outside_the_column_directory(run_cli, session, capsys) -> None:
    # 栏目名、栏目编号都对得上，但条目都在别的目录（如填了另一个栏目页的编号）
    source = make_source(session, url=TZGG, config={}, column="公示公告")
    responses = {GSGG_API: [api_ok(api_text("hunan_gxt_gsgg01"))]}
    channel = GSGG_CONFIG["channel_id"]
    assert run_cli(str(source.id), "--channel", channel, "--apply", responses=responses) == 1
    out = capsys.readouterr().out
    assert "过半条目不在栏目页所在目录（/gxt/xxgk_71033/tzgg/）下" in out
    assert "没有写入配置" in out
    session.expire_all()
    assert session.get(Source, source.id).parser_config == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parser_config", {"item_selector": "ul.list li"}),
        ("column_name", "公示公告"),
        ("url", GSGG),
    ],
)
def test_cli_does_not_overwrite_a_source_changed_during_the_trial(
    run_cli, session, session_factory, capsys, field, value
) -> None:
    source = make_source(session, config={})
    responses = {TZGG_API: [api_ok(api_text("hunan_gxt_tzgg_71043"))]}

    def change_meanwhile():
        with session_factory() as other:
            setattr(other.get(Source, source.id), field, value)
            other.commit()

    run_cli.before_fetch = change_meanwhile
    assert run_cli(str(source.id), "--channel", "71043", "--apply", responses=responses) == 1
    assert "试跑期间来源被改过或删除了，没有写入" in capsys.readouterr().out
    session.expire_all()
    changed = session.get(Source, source.id)
    assert getattr(changed, field) == value
    assert changed.parser_config == (value if field == "parser_config" else {})


def test_cli_validates_channel_and_source_and_can_clear(run_cli, session, capsys) -> None:
    source = make_source(session)
    assert run_cli(str(source.id), "--channel", "71043;drop") == 2
    assert run_cli("999", "--channel", "71043") == 1
    assert run_cli.fetcher().calls == []
    # 去掉列表接口不用试跑；不加 --apply 不写
    assert run_cli(str(source.id), "--clear") == 0
    session.expire_all()
    assert session.get(Source, source.id).parser_config == TZGG_CONFIG
    assert run_cli(str(source.id), "--clear", "--apply") == 0
    assert run_cli.fetcher().calls == []
    session.expire_all()
    assert session.get(Source, source.id).parser_config == {}
    assert session.scalar(select(func.count(SourceCheck.id))) == 0
    capsys.readouterr()


def test_cli_clear_leaves_other_parsing_rules_alone(run_cli, session, capsys) -> None:
    source = make_source(session, config={"item_selector": "ul.list li"})
    assert run_cli(str(source.id), "--clear", "--apply") == 2
    assert "这个来源没有配列表接口，不用去掉" in capsys.readouterr().out
    session.expire_all()
    assert session.get(Source, source.id).parser_config == {"item_selector": "ul.list li"}


def test_trial_api_reports_fetch_failures() -> None:
    fetcher = FakeFetcher(
        {TZGG_API: [FetchResult(TZGG_API, "robots_disallowed", error="robots.txt 不允许抓取")]}
    )
    trial = trial_api(TZGG, TZGG_CONFIG, fetcher, TODAY)
    assert (trial.status, trial.error, trial.items, trial.channel_names) == (
        "robots_disallowed",
        "robots.txt 不允许抓取",
        [],
        (),
    )


def test_trial_api_reports_a_refusal() -> None:
    fetcher = FakeFetcher({TZGG_API: [api_ok('{"msg":"invalid token","code":401}')]})
    trial = trial_api(TZGG, TZGG_CONFIG, fetcher, TODAY)
    assert trial.status == "access_denied" and "invalid token" in trial.error
    assert trial_problem(trial, "文件通知", 3) == trial.error
    fetcher = FakeFetcher({TZGG_API: [api_ok(denied_with_items())]})
    trial = trial_api(TZGG, TZGG_CONFIG, fetcher, TODAY)
    assert (trial.status, trial.items) == ("access_denied", [])
    # 接口直接回 HTTP 401、403 也是拒绝访问
    for code in (401, 403):
        failed = FetchResult(TZGG_API, "http_error", http_status=code, error=f"HTTP {code}")
        trial = trial_api(TZGG, TZGG_CONFIG, FakeFetcher({TZGG_API: [failed]}), TODAY)
        assert trial.status == "access_denied" and f"HTTP {code}" in trial.error


def test_trial_problem_checks_count_and_column_name() -> None:
    def item(n: int, day: date | None = TODAY) -> ListItem:
        return ListItem(f"第{n}份通知的标题", f"https://gxt.hunan.gov.cn/gxt/{n}.html", day)

    good = [item(n) for n in range(3)]
    assert trial_problem(ApiTrial("ok", None, good, ("文件通知",)), "文件通知", 3) is None
    undated = [item(0), item(1), item(2, day=None)]
    assert trial_problem(ApiTrial("ok", None, undated, ("文件通知",)), "文件通知", 3) == (
        "带日期的条目少于 3 条"
    )
    assert "请核对栏目编号" in trial_problem(ApiTrial("ok", None, good, ()), "文件通知", 3)
    failed = ApiTrial("parse_empty", "过半条目不在栏目页所在目录……", [], ())
    assert trial_problem(failed, "文件通知", 3) == "过半条目不在栏目页所在目录……"


def api_json(results: list, channel: str | None = "71043") -> str:
    data: dict = {"results": results}
    if channel is not None:
        data["channelId"] = channel
    return json.dumps({"data": data}, ensure_ascii=False)


def row(n: int, path: str = "/gxt/xxgk_71033/tzgg/", channel: str = "71043", host: str = ""):
    return {
        "title": f"第{n}份通知的标题",
        "url": f"{'https://' + host if host else ''}{path}{n}.html",
        "publishedTimeStr": "2026-10-15 10:00:00",
        "channelId": channel,
        "channelName": "文件通知",
    }


def read(text: str, page_url: str = TZGG):
    status, error, items, names = read_api_response(text, page_url, TZGG_CONFIG, TODAY)
    return status, error, len(items), names


@pytest.mark.parametrize(
    ("name", "page_url", "config", "column"),
    [
        ("hunan_gxt_tzgg_71043", TZGG, TZGG_CONFIG, "文件通知"),
        ("hunan_gxt_gsgg01", GSGG, GSGG_CONFIG, "公示公告"),
    ],
)
def test_real_responses_pass_the_checks_for_their_own_column(name, page_url, config, column):
    status, error, items, names = read_api_response(api_text(name), page_url, config, TODAY)
    assert (status, error, len(items), names) == ("ok", None, 20, (column,))
    trial = ApiTrial(status, error, items, names)
    assert trial_problem(trial, column, 3) is None


def test_every_response_is_checked_before_saving() -> None:
    good = [row(n) for n in range(3)]
    assert read(api_text("hunan_gxt_tzgg_71043")) == ("ok", None, 20, ("文件通知",))
    assert read(api_json(good)) == ("ok", None, 3, ("文件通知",))
    # 回显的栏目编号和配置不一致（或者没回显）：整份不收
    status, error, count, _ = read(api_json(good, channel="99999"))
    assert (status, count) == ("parse_empty", 0)
    assert error == "列表接口回显的栏目编号（99999）和配置的 71043 不一致，这次不入库"
    assert "回显的栏目编号（无）" in read(api_json(good, channel=None))[1]
    # 每条自带的栏目编号不是这个栏目的，只丢这一条；全丢光了按没有条目算
    assert read(api_json([*good, row(9, channel="88888")]))[:3] == ("ok", None, 3)
    assert read(api_json([row(9, channel="88888")]))[:3] == ("parse_empty", API_EMPTY_MESSAGE, 0)
    # 一条都没回、也没回显时按没有条目算；回显了别的栏目编号时照样报不一致
    assert read(api_json([], channel=None))[:3] == ("parse_empty", API_EMPTY_MESSAGE, 0)
    assert "回显的栏目编号（99999）" in read(api_json([], channel="99999"))[1]
    # 回的不是 JSON（可能是登录页、验证页）：不猜，写明可能性，请人核对
    assert read("<html><title>统一身份认证</title></html>")[:3] == (
        "parse_empty",
        API_NOT_JSON_MESSAGE,
        0,
    )
    assert "D-013" in API_NOT_JSON_MESSAGE
    # 外站转载的条目少于一半时照收；恰好一半不算过半
    assert read(api_json([*good, row(9, path="/xxgk/other/")]))[2] == 4
    half = [*good[:2], row(8, path="/xxgk/other/"), row(9, path="/xxgk/other/")]
    status, error, count, _ = read(api_json(half))
    assert (status, count) == ("parse_empty", 0) and error.startswith("过半条目")
    assert error.endswith("这次不入库")
    # 路径一样、主机不同的（别的网站的同名目录）不算在栏目目录下
    other_host = [row(n, host="kjt.hunan.gov.cn") for n in range(3)]
    assert read(api_json(other_host))[0] == "parse_empty"
    # 栏目页在网站根目录时，任何站内路径都"在目录下"，核对不了，不收
    for root in ("https://gxt.hunan.gov.cn/", "https://gxt.hunan.gov.cn/index.html"):
        status, error, count, _ = read(api_json(good), page_url=root)
        assert (status, count) == ("parse_empty", 0) and "根目录" in error
    # 明确拒绝访问
    assert read('{"msg":"invalid token","code":401}')[0] == "access_denied"


def test_check_source_rejects_a_response_for_another_channel(session) -> None:
    source = make_source(session)
    other = api_json([row(n) for n in range(3)], channel="99999")
    check = check_source(session, source, FakeFetcher({TZGG_API: [api_ok(other)]}), today=TODAY)
    assert (check.status, check.items_found) == ("parse_empty", 0)
    assert "不一致，这次不入库" in check.error
    assert session.scalar(select(func.count(Document.id))) == 0


@pytest.mark.parametrize("code", [401, 403])
def test_list_api_http_refusal_is_access_denied(session, code) -> None:
    source = make_source(session)
    failed = FetchResult(TZGG_API, "http_error", http_status=code, error=f"HTTP {code}")
    check = check_source(session, source, FakeFetcher({TZGG_API: [failed]}), today=TODAY)
    assert (check.status, check.http_status) == ("access_denied", code)
    assert "D-013" in check.error


def test_list_api_redirect_is_not_followed_and_asks_for_a_check(session) -> None:
    source = make_source(session)
    moved = FetchResult(
        TZGG_API,
        "http_error",
        http_status=302,
        error="HTTP 302，接口跳转到 https://api.hunan.gov.cn/login，没有跟随",
    )
    check = check_source(session, source, FakeFetcher({TZGG_API: [moved]}), today=TODAY)
    assert check.status == "http_error"
    assert check.error.startswith("HTTP 302，接口跳转到 https://api.hunan.gov.cn/login")
    assert "可能要求登录" in check.error and "D-013" in check.error


def test_page_sources_keep_plain_http_errors(session) -> None:
    # 抓栏目页的老路径不变：403 仍记 http_error（ADR 0002 第 4 条照旧由人停用）
    source = make_source(session, config={})
    failed = FetchResult(TZGG, "http_error", http_status=403, error="HTTP 403")
    check = check_source(session, source, FakeFetcher({TZGG: [failed]}), today=TODAY)
    assert (check.status, check.error) == ("http_error", "HTTP 403")
