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


def test_only_null_permission_fields_are_recognized():
    """认得出才读：roles、permissions 只有和正常返回一样是 null 才读；空列表、空串等都不认。"""
    api = _api()
    assert api_refusal(api, _got(_payload(roles=None, permissions=None))) == ""
    for other in ([], {}, "", False, 0):
        assert api_refusal(api, _got(_payload(roles=other, permissions=other))) != ""


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
    # http 升到 https、接口没变的跳转照常读（反过来降到 http 的不读，见下面的测试）
    from youju.crawler.adapters import ListApi

    plain = ListApi("大汉jpaas", api.url.replace("https://", "http://"), PAGE_URL)
    assert api_refusal(plain, _got(_payload(), final=api.url)) == ""


def test_login_captcha_or_gate_page_is_refused():
    api = _api()
    assert "要登录或验证码" in api_refusal(api, _got('<html><input type="password"></html>'))
    assert "要登录或验证码" in api_refusal(api, _got("<html>请输入验证码</html>"))
    assert "拦截" in api_refusal(api, _got("<html>访问被拒绝</html>"))
    # 认得出才读：空白、维护中、网站模板的 404 页这类不是 JSON 的返回，一律认不出、不读
    nav = "".join(f'<a href="/col/col{i}/index.html">栏目{i}</a>' for i in range(30))
    template = f"<html><div>用户登录 | 验证码</div>{nav}<p>页面不存在</p></html>"
    for text in ("", "<html>系统维护中</html>", template):
        assert "认不出" in api_refusal(api, _got(text))
        assert api_html(api, text) == ""
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

    def get(self, url: str, same_address_only=False) -> FetchResult:
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


def test_only_the_page_declared_address_and_parameters_are_requested():
    """D-128 护栏 ①：只按网页写明的地址和参数去取，不改参数、不另外构造请求。"""
    from urllib.parse import parse_qsl

    declared = [
        ("parseType", "bulidstatic"),
        ("webId", "3387"),
        ("tplSetId", "T1"),
        ("pageType", "column"),
        ("tagId", "当前栏目list"),
        ("editType", "null"),
        ("pageId", "1229225203"),
    ]
    query = "{" + ",".join(f"'{k}':'{v}'" for k, v in declared) + "}"
    page = f'<script src="/x/unitbuild.js" url="{HANWEB_JPAAS_PATH}" queryData="{query}"></script>'
    api = find_list_api(page, PAGE_URL)
    parts = urlsplit(api.url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "kjt.zj.gov.cn", HANWEB_JPAAS_PATH)
    # 参数和页面写的一模一样：不多、不少、不改（没有分页、检索、时间戳一类）
    assert parse_qsl(parts.query, keep_blank_values=True) == declared


def test_lists_stay_unread_unless_the_guard_passes():
    """D-128 护栏 ②③：只有返回明确成功、没有权限要求时才读出列表，别的一律不读。"""
    api = _api()
    assert api_html(api, _payload()) == LIST
    for text in (
        json.dumps({"data": {"html": LIST}}),  # 没有成功标志
        _payload(success=False),
        _payload(code="401"),
        _payload(roles=["dept"]),
        _payload(permissions=["read"]),
        "<html>请先登录</html>",
        "",
    ):
        assert api_html(api, text) == ""


def test_declared_address_is_kept_as_written():
    """页面写的接口地址自带参数、写成绝对网址时，原样照用，页面参数接在后面。"""
    from urllib.parse import parse_qsl

    page = (
        f'<script src="/x/unitbuild.js" url="https://kjt.zj.gov.cn{HANWEB_JPAAS_PATH}?site=zj#top" '
        "queryData=\"{'pageType':'column','tagId':'当前栏目list','pageId':'1'}\"></script>"
    )
    api = find_list_api(page, "http://kjt.zj.gov.cn/col/col1/index.html")
    parts = urlsplit(api.url)
    assert (parts.scheme, parts.path, parts.fragment) == ("https", HANWEB_JPAAS_PATH, "")
    assert parse_qsl(parts.query) == [
        ("site", "zj"),
        ("pageType", "column"),
        ("tagId", "当前栏目list"),
        ("pageId", "1"),
    ]


def test_redirect_away_from_the_declared_address_is_not_read():
    """接口跳到页面没写的地址（别的路径、参数、主机）：哪怕那边回成功，也不跟着读。"""
    api = _api()
    parts = urlsplit(api.url)
    for final in (
        api.url.replace(HANWEB_JPAAS_PATH, "/other/unit"),
        api.url + "&page=2",
        api.url.replace("kjt.zj.gov.cn", "www.zj.gov.cn"),
    ):
        assert "没写的地址" in api_refusal(api, _got(_payload(), final=final))
    # 网络不通（不管记的最后地址是哪里）：这次没取到，不记不能抓
    gone = FetchResult(api.url, "network_error", final_url=f"https://{parts.netloc}/other")
    assert api_refusal(api, gone) == ""


def test_deep_permission_fields_are_found():
    api = _api()
    deep: dict = {"roles": ["dept"]}
    for level in range(8):
        deep = {f"l{level}": deep}
    text = _payload(data={"html": LIST, "meta": deep})
    assert "roles" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def test_verify_records_one_off_list_api_failure_as_down():
    from youju.govsites.verify import DOWN, check_one

    api = _api()
    failed = FetchResult(api.url, "http_error", api.url, 502, "", "HTTP 502")
    col = check_one(_Cache({PAGE_URL: PAGE, api.url: failed}), _column(), date(2026, 11, 12))
    assert col.result == DOWN and "列表接口" in col.reason


def test_success_flag_with_login_prompt_is_not_read():
    api = _api()
    text = _payload(message="请先登录后查看")
    assert "要登录或验证码" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def test_verify_unreadable_or_empty_list_api_reply():
    from youju.govsites.verify import BLOCKED, NO_ITEMS, check_one

    api, today = _api(), date(2026, 11, 12)
    blank = check_one(
        _Cache({PAGE_URL: PAGE, api.url: "<html>系统维护中</html>"}), _column(), today
    )
    assert blank.result == BLOCKED and "认不出" in blank.reason
    empty = check_one(
        _Cache({PAGE_URL: PAGE, api.url: _payload(data={"html": ""})}), _column(), today
    )
    assert empty.result == NO_ITEMS  # 列表那段在、是空的：这会儿没有条目
    missing = check_one(_Cache({PAGE_URL: PAGE, api.url: _payload(data={})}), _column(), today)
    assert missing.result == BLOCKED  # 没有列表那段：认不出，不读
    one = _payload(data={"html": LIST.split("</li>")[0] + "</li>"})
    few = check_one(_Cache({PAGE_URL: PAGE, api.url: one}), _column(), today)
    assert few.result == NO_ITEMS and "经列表接口" in few.reason


def test_login_prompt_in_any_message_field_is_not_read():
    api = _api()
    text = _payload(message="生成成功", msg="请先登录")
    assert "要登录或验证码" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def test_https_to_http_redirect_is_not_followed_or_read():
    """https 的接口要跳到同一地址的 http：不跟、不读，按这次没读出（不记不能抓）。"""
    api = _api()
    down = FetchResult(api.url, "redirect", api.url.replace("https://", "http://"), 301, "")
    assert api_refusal(api, down) == ""
    assert api_html(api, down.text) == ""


@pytest.mark.parametrize("prompt", ["需要登录", "您没有权限查看", "未授权访问", "登录后可见"])
def test_more_login_or_permission_prompts_are_not_read(prompt):
    api = _api()
    text = _payload(message=prompt)
    assert "要登录或验证码" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def test_login_form_inside_the_list_html_is_not_read():
    api = _api()
    text = _payload(data={"html": LIST + '<form><input type="password" name="p"></form>'})
    assert "登录框" in api_refusal(api, _got(text))
    assert api_html(api, text) == ""


def _fetcher(handler, requested: list[str]) -> PoliteFetcher:
    def recording(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    return PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0.0,
        transport=httpx.MockTransport(recording),
        resolver=lambda host: ["1.2.3.4"],
    )


def test_redirect_away_from_the_declared_api_is_never_requested():
    """D-128 护栏 ①：接口跳到页面没写的地址，抓取器不去请求那个地址，返回跳转本身。"""
    api = _api()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == HANWEB_JPAAS_PATH:
            return httpx.Response(302, headers={"location": "https://kjt.zj.gov.cn/other/unit"})
        return httpx.Response(200, json=json.loads(_payload()))

    with _fetcher(handler, requested) as f:
        got = f.fetch(api.url, same_address_only=True)
    assert got.status == "redirect" and got.final_url == "https://kjt.zj.gov.cn/other/unit"
    assert not any("/other/unit" in u for u in requested)
    assert "没写的地址" in api_refusal(api, got)


def test_even_http_to_https_upgrade_is_not_followed():
    """只请求页面写明的那个地址：连 http 升 https 的跳转也不跟；只是换协议的按这次没读出。"""
    from youju.crawler.adapters import ListApi

    api = ListApi("大汉jpaas", f"http://kjt.zj.gov.cn{HANWEB_JPAAS_PATH}?pageId=1", PAGE_URL)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": api.url.replace("http://", "https://")})
        return httpx.Response(200, json=json.loads(_payload()))

    with _fetcher(handler, requested) as f:
        got = f.fetch(api.url, same_address_only=True)
    assert got.status == "redirect"
    assert not any(u.startswith("https://") for u in requested)
    assert api_refusal(api, got) == ""  # 不记不能抓
    assert api_html(api, got.text) == ""  # 也不读


def test_entities_in_page_attributes_are_decoded_like_a_browser():
    from urllib.parse import parse_qsl

    page = (
        f'<script src="/x/unitbuild.js" url="{HANWEB_JPAAS_PATH}?a=1&amp;b=2" '
        "queryData=\"{'tagId':'当前栏目list','pageId':'1&amp;2'}\"></script>"
    )
    api = find_list_api(page, PAGE_URL)
    assert parse_qsl(urlsplit(api.url).query) == [
        ("a", "1"),
        ("b", "2"),
        ("tagId", "当前栏目list"),
        ("pageId", "1&2"),
    ]


def test_login_hint_in_list_html_outside_item_links_is_not_read():
    api = _api()
    gated = _payload(data={"html": "<p>请先登录后查看</p>" + LIST})
    assert "登录或权限提示" in api_refusal(api, _got(gated))
    assert api_html(api, gated) == ""
    # 条目标题里只是提到"登录""type=password"一类字样的照常读
    titles = (
        '<li><a href="/art/1.html">关于启用短信验证码登录的通知（type=password 字段说明）</a>'
        "<span>2026-11-05</span></li>"
    )
    assert api_refusal(api, _got(_payload(data={"html": titles}))) == ""


def test_login_prompt_written_as_a_link_is_not_read():
    api = _api()
    gated = _payload(data={"html": LIST + '<a href="/login">请登录</a>'})
    assert "登录或权限提示" in api_refusal(api, _got(gated))
    encoded = _payload(data={"html": "<p>&#35831;&#20808;&#30331;&#24405;</p>" + LIST})  # 请先登录
    assert "登录或权限提示" in api_refusal(api, _got(encoded))
    # 正常通知链到外站的 login.html（标题是通知本身）：照常读
    item = (
        '<li><a href="https://qym.example.gov.cn/login.html#/live?id=1">'
        "关于举办2026年度企业服务直播活动的通知</a><span>2026-11-05</span></li>"
    )
    assert api_refusal(api, _got(_payload(data={"html": item + LIST}))) == ""


def test_data_attributes_are_not_taken_as_the_declared_api():
    page = (
        f'<script src="/x/unitbuild.js" data-url="{HANWEB_JPAAS_PATH}" '
        "data-queryData=\"{'pageType':'column','tagId':'当前栏目list','pageId':'1'}\"></script>"
    )
    assert find_list_api(page, PAGE_URL) is None


def test_strict_fetches_do_not_reuse_old_follow_mode_cache(tmp_path):
    from youju.govsites.pagecache import PageCache

    calls: list[tuple[str, bool]] = []

    class Fetcher:
        def fetch(self, url, same_address_only=False):
            calls.append((url, same_address_only))
            return FetchResult(url, "ok", url, 200, _payload())

    cache = PageCache(tmp_path, Fetcher())
    api = _api()
    cache.get(api.url)  # 以前跟着跳转抓的那种
    cache.get(api.url, same_address_only=True)  # 只取地址本身：不拿上面的旧结果
    cache.get(api.url, same_address_only=True)  # 第二次用自己的缓存
    assert calls == [(api.url, False), (api.url, True)]


def test_page_with_a_failed_unit_is_not_concluded_as_cannot_crawl():
    """一个单元被拦、另一个单元这次没取到：栏目可能在没取到的那个里，记下没取到，不下不能抓的结论。"""
    from youju.crawler.adapters import find_unit_apis
    from youju.govsites.divisions import DivisionIndex, parse_tree
    from youju.govsites.inventory import Inventory

    nav = (
        PAGE.split("<ul>")[1]
        .split("</ul>")[0]
        .replace("当前栏目list", "导航")
        .replace('id="u1"', 'id="u2"')
    )
    page = PAGE.replace("</ul>", nav + "</ul>")
    apis = [a.url for a in find_unit_apis(page, PAGE_URL)]
    assert len(apis) == 2
    failed = FetchResult(apis[1], "http_error", apis[1], 502, "", "HTTP 502")
    pages = {PAGE_URL: page, apis[0]: _payload(roles=["dept"]), apis[1]: failed}
    inv = Inventory(DivisionIndex(parse_tree({})), _Cache(pages), provinces=[])
    info = inv.page(PAGE_URL)
    assert info.api_refused and info.api_failed


def test_declarations_in_html_comments_are_ignored():
    commented = f"<!-- {PAGE} -->"
    assert find_list_api(commented, PAGE_URL) is None


def test_duplicate_attributes_take_the_first_like_a_browser():
    from urllib.parse import parse_qsl

    page = (
        f'<script src="/x/unitbuild.js" url="{HANWEB_JPAAS_PATH}" '
        "queryData=\"{'pageId':'first'}\" queryData=\"{'pageId':'second'}\"></script>"
    )
    api = find_list_api(page, PAGE_URL)
    assert parse_qsl(urlsplit(api.url).query) == [("pageId", "first")]


def test_login_prompt_split_by_tags_is_not_read():
    api = _api()
    split = _payload(data={"html": "<p>请<b>先</b>登录</p>" + LIST})
    assert "登录或权限提示" in api_refusal(api, _got(split))


def test_declarations_in_textarea_or_template_are_ignored():
    for wrap in ("<textarea>{}</textarea>", "<template>{}</template>", "<noscript>{}</noscript>"):
        assert find_list_api(wrap.format(PAGE), PAGE_URL) is None


def test_gate_links_are_judged_by_how_they_start():
    api = _api()
    long_gate = _payload(
        data={"html": LIST + '<a href="/login">请先登录后查看完整政策文件列表</a>'}
    )
    assert "登录或权限提示" in api_refusal(api, _got(long_gate))
    title = '<li><a href="/notice">关于验证码的通知</a><span>2026-11-05</span></li>'
    assert api_refusal(api, _got(_payload(data={"html": title + LIST}))) == ""


def test_string_false_or_null_permission_values_are_not_empty():
    """只有 JSON 的空值算空；写成 "false" "null" 字符串的从严拦下。"""
    api = _api()
    for value in ("false", "null", "0"):
        text = _payload(roles=value)
        assert "roles" in api_refusal(api, _got(text))
        assert api_html(api, text) == ""


def test_unrecognized_unit_counts_as_refused():
    from youju.crawler.adapters import find_unit_apis
    from youju.govsites.divisions import DivisionIndex, parse_tree
    from youju.govsites.inventory import Inventory

    nav = PAGE.split("<ul>")[1].split("</ul>")[0]
    nav = nav.replace("当前栏目list", "导航").replace('id="u1"', 'id="u2"')
    page = PAGE.replace("</ul>", nav + "</ul>")
    apis = [a.url for a in find_unit_apis(page, PAGE_URL)]
    pages = {PAGE_URL: page, apis[0]: _payload(success=False), apis[1]: "<html>系统维护中</html>"}
    info = Inventory(DivisionIndex(parse_tree({})), _Cache(pages), provinces=[]).page(PAGE_URL)
    assert info.api_refused and not info.api_failed  # 认不出的单元也算拦下


def test_style_blocks_and_click_to_login_links():
    assert find_list_api(f"<style>{PAGE}</style>", PAGE_URL) is None
    api = _api()
    gated = _payload(data={"html": LIST + '<a href="/login">&nbsp;点击登录</a>'})
    assert "登录或权限提示" in api_refusal(api, _got(gated))


def test_pseudo_tags_inside_script_code_are_ignored():
    fake = PAGE.split("<ul>")[1].split("</ul>")[0].replace('"', "\\'")
    page = f"<script>var example = '{fake}';</script>"
    assert find_list_api(page, PAGE_URL) is None
    assert find_list_api(PAGE, PAGE_URL) is not None  # 真正的声明照认


def test_any_login_word_outside_item_links_is_not_read():
    api = _api()
    gated = _payload(data={"html": "<p>请先完成登录才能查看</p>" + LIST})
    assert "登录或权限提示" in api_refusal(api, _got(gated))
    # 摘要里写着"授权运营"这类字样的不算（真实数据里有）
    summary = _payload(data={"html": "<p>审议通过了公共数据资源授权运营实施方案</p>" + LIST})
    assert api_refusal(api, _got(summary)) == ""


def test_pseudo_tags_in_other_attributes_are_ignored():
    fake = PAGE.split("<ul>")[1].split("</ul>")[0].replace('"', "&quot;")
    page = f"<div title='{fake}'>说明</div>"
    assert find_list_api(page, PAGE_URL) is None


def test_more_gate_link_wordings():
    api = _api()
    for text in ("仅登录用户可查看", "登录", "用户登录"):
        gated = _payload(data={"html": LIST + f'<a href="/login">{text}</a>'})
        assert "登录或权限提示" in api_refusal(api, _got(gated)), text


def test_only_scripts_whose_path_is_unitbuild_count():
    fake = PAGE.replace("/x/unitbuild.js", "/other.js?name=unitbuild.js")
    assert find_list_api(fake, PAGE_URL) is None


def test_gate_phrases_anywhere_but_plain_mentions_pass():
    api = _api()
    for gated in (
        LIST + '<a href="/login">查看详情请先登录</a>',
        "<p>请先完成登录才能查看</p>" + LIST,
        LIST + "<span>登录后可见</span>",
    ):
        assert "登录或权限提示" in api_refusal(api, _got(_payload(data={"html": gated}))), gated
    # 条目标题里只是提到的照读
    fine = '<li><a href="/y">关于启用短信验证码登录的通知</a><span>2026-11-05</span></li>' + LIST
    assert api_refusal(api, _got(_payload(data={"html": fine}))) == ""
    # 认得出才读：条目链接以外写着"登录"的，不是普通列表，从严不读（真实返回里一次也没有）
    summary = '<li><a href="/x">政策通知</a><span>登录系统操作指南</span></li>' + LIST
    assert "登录或权限提示" in api_refusal(api, _got(_payload(data={"html": summary})))


def test_only_the_known_reply_shape_is_read():
    """认得出才读：多了字段、提示不是"生成成功"、messageType 不对、data 里多东西，一律不读。"""
    api = _api()
    for change, why in (
        ({"extra": 1}, "多了字段"),
        ({"message": "成功"}, "提示不是"),
        ({"messageType": 0}, "messageType"),
        ({"data": {"html": LIST, "more": "x"}}, "data 里"),
        ({"traceId": 1}, "traceId"),
    ):
        text = _payload(**change)
        assert why in api_refusal(api, _got(text)), change
        assert api_html(api, text) == ""
    missing_type = json.loads(_payload())
    del missing_type["messageType"]  # 真实返回里有 3 次没有 messageType：照读
    assert api_refusal(api, _got(json.dumps(missing_type, ensure_ascii=False))) == ""


def test_login_hints_that_no_rule_lists_are_still_caught_outside_links():
    api = _api()
    for html in ("<p>登录后方能浏览</p>" + LIST, "<p>完成验证码校验</p>" + LIST):
        assert "登录或权限提示" in api_refusal(api, _got(_payload(data={"html": html}))), html


def test_entity_encoded_password_input_is_caught():
    api = _api()
    html = LIST + '<input type="&#112;assword" name="p">'
    assert "登录框" in api_refusal(api, _got(_payload(data={"html": html})))


def test_only_known_declaration_attributes_and_parameters_are_recognized():
    extra_attr = PAGE.replace('id="u1"', 'id="u1" data-x="1"')
    assert find_list_api(extra_attr, PAGE_URL) is None
    extra_param = PAGE.replace("'pageId':'1229225203'", "'pageId':'1229225203','page':'2'")
    assert find_list_api(extra_param, PAGE_URL) is None
    number = PAGE.replace("'pageId':'1229225203'", "'pageId':1229225203")
    assert find_list_api(number, PAGE_URL) is None
