"""读取护栏（D-128）：列表接口返回不是成功、带权限要求、要登录或验证码、网站明确拒绝的，
一律记"不能抓"、不读；读列表接口和读页面一样，同一网站两次请求至少隔 3 秒。"""

import json
from datetime import date, datetime
from urllib.parse import urlsplit

import httpx
import pytest

from youju.config import Settings
from youju.crawler.adapters import (
    HANWEB_JPAAS_PATH,
    LIST_API_REFUSED,
    api_html,
    api_refusal,
    find_list_api,
)
from youju.crawler.fetcher import FetchResult, PoliteFetcher

PAGE_URL = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
PAGE = (
    f'<html><body><ul><script id="u1" src="/x/unitbuild.js" url="{HANWEB_JPAAS_PATH}" '
    "queryData=\"{'pageType':'column','tagId':'当前栏目list','pageId':'1229225203'}\">"
    "</script></ul></body></html>"
)
LIST = "".join(
    f'<li><a href="/art/2026/11/{d}/art_{d}.html">关于开展2026年度第{d}批项目申报的通知</a>'
    f"<span>2026-11-0{d}</span></li>"
    for d in (5, 4, 3)
)


def _api():
    api = find_list_api(PAGE, PAGE_URL)
    assert api is not None
    return api


def _payload(**change) -> str:
    """和网站实际返回同一个样子（11/11 实测：roles、permissions 为空，success 为 true）。"""
    body = {
        "roles": None,
        "permissions": None,
        "success": True,
        "message": "生成成功",
        "code": "200",
        "data": {"html": LIST},
        "traceId": "t1",
        "messageType": 2,
    }
    body.update(change)
    return json.dumps(body, ensure_ascii=False)


def _got(text: str, *, final: str | None = None, status: str = "ok", http: int = 200):
    api = _api()
    return FetchResult(api.url, status, final_url=final or api.url, http_status=http, text=text)


def test_normal_success_is_read():
    api = _api()
    assert api_refusal(api, _got(_payload())) == ""
    assert api_html(api, _payload()) == LIST


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"success": False, "message": ""}, "不是成功"),
        ({"success": None}, "没有成功标志"),
        ({"code": "500"}, "不是成功"),
        ({"success": False, "message": "请先登录"}, "要登录或验证码"),
        ({"roles": ["reader"]}, "roles"),
        ({"permissions": {"read": "dept"}}, "permissions"),
        ({"data": {"html": LIST, "needLogin": True}}, "data.needLogin"),
    ],
)
def test_guard_refuses_and_does_not_read(change, reason):
    api = _api()
    text = _payload(**change)
    assert reason in api_refusal(api, _got(text))
    assert api_html(api, text) == ""  # 护栏拦下的返回不读


def test_missing_success_flag_is_not_success():
    api = _api()
    text = json.dumps({"data": {"html": LIST}}, ensure_ascii=False)
    assert "不是成功" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def test_empty_permission_fields_are_fine():
    api = _api()
    for empty in (None, [], {}, "", False, 0):
        assert api_refusal(api, _got(_payload(roles=empty, permissions=empty))) == ""


@pytest.mark.parametrize("code", [401, 403, 412, 521])
def test_refusing_http_status(code):
    api = _api()
    got = FetchResult(api.url, "http_error", final_url=api.url, http_status=code, error="x")
    assert f"HTTP {code}" in api_refusal(api, got)


def test_robots_disallowed_list_api_is_refused():
    api = _api()
    got = FetchResult(api.url, "robots_disallowed", final_url=api.url, error="robots")
    assert "robots" in api_refusal(api, got)


def test_redirect_to_login_is_refused():
    api = _api()
    login = "https://kjt.zj.gov.cn/jpaas-jsam-server/login?redirect=1"
    form = '<form><input name="u"><input type="password" name="p"></form>'
    assert "跳到登录" in api_refusal(api, _got(form, final=login))
    # 跳到别处的统一认证（抓取程序不跟去非政府网站）：同样不能抓
    sso = FetchResult(api.url, "unsupported_host", final_url="https://sso.example.com/a?x=1")
    assert "跳到登录" in api_refusal(api, sso)
    # 只是换了协议、接口没变的跳转照常读
    https_api = api.url.replace("https://", "http://")
    assert api_refusal(api, _got(_payload(), final=https_api)) == ""


def test_login_captcha_or_gate_page_is_refused():
    api = _api()
    assert "要登录或验证码" in api_refusal(api, _got('<html><input type="password"></html>'))
    assert "要登录或验证码" in api_refusal(api, _got("<html>请输入验证码</html>"))
    assert "拦截" in api_refusal(api, _got("<html>访问被拒绝</html>"))
    # 空白、维护中这类不是 JSON 的返回：不读，但按"这次没读出"记失败、下次再试
    for text in ("", "<html>系统维护中</html>"):
        assert api_refusal(api, _got(text)) == ""
        assert api_html(api, text) == ""
    # 网站模板做的 404 页：页头有"用户登录""验证码"，链接很多，不算关卡页
    nav = "".join(f'<a href="/col/col{i}/index.html">栏目{i}</a>' for i in range(30))
    template = f"<html><div>用户登录 | 验证码</div>{nav}<p>页面不存在</p></html>"
    assert api_refusal(api, _got(template)) == ""
    # 失败提示写在 msg 里的也认
    assert "要登录或验证码" in api_refusal(
        api, _got(_payload(success=False, message=None, msg="请先登录"))
    )


def test_nested_permission_fields_are_found():
    api = _api()
    text = _payload(data={"html": LIST, "unit": {"meta": {"roles": ["dept"]}}})
    assert "data.unit.meta.roles" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def test_scheme_only_redirect_is_not_a_login_redirect():
    from youju.crawler.adapters import ListApi

    api = ListApi(
        "大汉jpaas", f"http://www.cas.cn{HANWEB_JPAAS_PATH}?pageId=1", "http://www.cas.cn/"
    )
    same = FetchResult(api.url, "ok", api.url.replace("http://", "https://"), 200, _payload())
    assert api_refusal(api, same) == ""
    to_auth = FetchResult(api.url, "unsupported_host", "https://auth.example.com/x?r=1")
    assert "跳到登录" in api_refusal(api, to_auth)
    # 只改了参数的跳转（加上 need_login=1）也看
    need_login = FetchResult(api.url, "ok", api.url + "&need_login=1", 200, "<html></html>")
    assert "跳到登录" in api_refusal(api, need_login)


def test_one_off_failures_are_not_refusals():
    """网络不通、超时、5xx 是"这次没取到"：照常记失败、下次再试，不记不能抓。"""
    api = _api()
    for got in (
        FetchResult(api.url, "network_error", error="超时：ReadTimeout"),
        FetchResult(api.url, "http_error", final_url=api.url, http_status=502, error="HTTP 502"),
        FetchResult(api.url, "deadline", final_url=api.url, error="到期"),
    ):
        assert api_refusal(api, got) == ""


def test_list_titles_with_login_words_are_still_read():
    api = _api()
    html = (
        '<li><a href="/art/1.html">关于启用统一身份认证和短信验证码登录的通知</a>'
        "<span>2026-11-05</span></li>"
    )
    text = _payload(data={"html": html})
    assert api_refusal(api, _got(text)) == ""
    assert api_html(api, text) == html


def test_list_api_waits_like_any_page_on_the_same_site():
    """列表接口和栏目页走同一个礼貌抓取器：同一网站两次请求之间至少 3 秒。"""
    assert Settings().per_host_interval >= 3.0
    now = {"t": 0.0}
    sleeps: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    api_path = urlsplit(_api().url).path

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == api_path:
            return httpx.Response(200, json=json.loads(_payload()))
        return httpx.Response(200, text=PAGE)

    fetcher = PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=3.0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["1.2.3.4"],
        clock=clock,
        sleep=sleep,
    )
    with fetcher as f:
        page = f.fetch(PAGE_URL)
        api = find_list_api(page.text, page.final_url or PAGE_URL)
        got = f.fetch(api.url)
    assert got.status == "ok" and api_refusal(api, got) == ""
    # robots → 等 3 秒 → 栏目页 → 等 3 秒 → 列表接口
    assert sleeps == [3.0, 3.0]


class _Cache:
    def __init__(self, pages: dict) -> None:
        self.pages = pages

    def get(self, url: str) -> FetchResult:
        value = self.pages.get(url)
        if isinstance(value, FetchResult):
            return value
        if value is None:
            return FetchResult(url, "http_error", url, 404, "", "HTTP 404")
        return FetchResult(url, "ok", url, 200, value)


def _column():
    from youju.govsites.verify import ColumnCheck

    return ColumnCheck(
        "330000", "省级", "浙江", "", "", "科技", "浙江省科学技术厅", "通知公告", "通知通告",
        PAGE_URL, "", "", "",
    )  # fmt: skip


def test_verify_marks_refused_list_api_as_blocked():
    from youju.govsites.verify import BLOCKED, READABLE, check_one

    api, today = _api(), date(2026, 11, 11)
    good = check_one(_Cache({PAGE_URL: PAGE, api.url: _payload()}), _column(), today)
    assert good.result == READABLE and good.dated == 3
    refused = check_one(
        _Cache({PAGE_URL: PAGE, api.url: _payload(roles=["dept"])}), _column(), today
    )
    assert refused.result == BLOCKED
    assert "roles" in refused.reason and "D-128" in refused.reason and refused.items == 0
    denied = FetchResult(api.url, "http_error", api.url, 403, "", "HTTP 403")
    blocked = check_one(_Cache({PAGE_URL: PAGE, api.url: denied}), _column(), today)
    assert blocked.result == BLOCKED and "HTTP 403" in blocked.reason


def test_board_shows_refused_list_api_as_cannot_crawl():
    from youju.govsites.board import SourceState, cell_for
    from youju.govsites.inventory import ColumnHit, DeptRow

    row = DeptRow(
        "330000", "省级", "浙江", "", "", "科技", status="已找到",
        columns=[ColumnHit("通知公告", "通知通告", PAGE_URL, "")],
    )  # fmt: skip
    why = "列表接口（大汉jpaas）：列表接口返回带权限要求（roles 不为空）"
    state = SourceState(
        PAGE_URL, True, "static", "blocked", None, why, "试跑可读（全国覆盖 v1，未启用）",
        0, LIST_API_REFUSED, 200,
    )  # fmt: skip
    cell = cell_for(row, {PAGE_URL: state}, {}, datetime(2026, 11, 11, 12))
    assert cell.state == "不能抓"
    assert "roles" in cell.reason  # 原因是护栏给的，不是"试跑可读"
