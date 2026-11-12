import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from youju.crawler.adapters import (
    api_empty_list,
    api_items,
    api_items_merged,
    api_parts,
    api_readable,
    api_refusal,
    find_list_api,
)
from youju.crawler.fetcher import FetchResult
from youju.crawler.urls import host_of
from youju.govsites.report import import_status
from youju.govsites.verify import BLOCKED, DOWN, NEEDS_SCRIPT, READABLE, ColumnCheck, check_one

FIX = Path(__file__).parent / "fixtures/gkmlpt"
PAGE = (FIX / "dept_page.html").read_text(encoding="utf-8")
BODY = json.loads((FIX / "api_notice.json").read_text(encoding="utf-8"))
URL = "https://www.sg.gov.cn/fgj/gkmlpt/index"
TODAY = date(2026, 11, 12)


def api(page=PAGE, url=URL, category=8):
    return find_list_api(page, url, f"{url}#xxgk-gkmlpt-{category}")


def result(text, status="ok", http=200):
    return FetchResult(api().url, status, api().url, http, text)


def test_address_and_tree():
    assert api().url == "https://www.sg.gov.cn/fgj/gkmlpt/api/all/8?page=1&sid=751012"
    assert api(url=URL.replace("https:", "http:")).url.startswith("http://")
    # 部门页网址带默认端口的，接口地址照样带：抓取程序按同一个网站排队、隔 3 秒（D-013）
    assert (
        api(url=URL.replace(".cn/", ".cn:443/")).url
        == "https://www.sg.gov.cn:443/fgj/gkmlpt/api/all/8?page=1&sid=751012"
    )
    assert api(url=URL.replace(".cn/", ".cn:8443/")) is None  # 浏览器去的是默认端口，对不上
    assert api(url=URL.replace("https://", "https://u:p@")) is None  # 网址里带用户名密码的不认
    # 接口和部门页在抓取程序眼里是同一个网站（按它排队、隔 3 秒）
    for page_url in (URL, URL.replace(".cn/", ".cn:443/")):
        assert host_of(api(url=page_url).url) == host_of(page_url)
    assert api(category=6)
    assert not api(category=999)
    assert not api(category=24)


@pytest.mark.parametrize(
    "extra,recognised",
    [
        ("<script>window._CONFIG.SID = '9';</script>", False),
        ("<script>window._CONFIG['ENV'] = 'test';</script>", False),
        ("<script>window._CONFIG.TREE = [];</script>", False),
        ("<script>if (window._CONFIG.SID == '9') {}</script>", True),
        ("<script>window._CONFIG.isFav = 0;</script>", True),
        ("<script>var s = \"window._CONFIG.SID = '9'\";</script>", True),
        ("<script>var x = PAGE_CONFIG[0];</script>", True),
    ],
)
def test_later_config_overrides_are_not_recognised(extra, recognised):
    page = PAGE.replace("</body>", extra + "</body>")
    assert (api(page) is not None) is recognised


@pytest.mark.parametrize(
    "page,url",
    [
        (PAGE.replace("ENV: 'production'", "ENV: 'test'"), URL),
        (PAGE.replace("http://www.sg.gov.cn/fgj'", "http://evil.cn/fgj'"), URL),
        (PAGE.replace("http://www.sg.gov.cn/fgj'", "http://www.sg.gov.cn:80/fgj'"), URL),
        (PAGE, URL.replace("/fgj/gkmlpt/", "/other/gkmlpt/")),
        (PAGE.replace("window._CONFIG = {", "window._CONFIG = {}; window._CONFIG = {"), URL),
        ("<!-- " + PAGE + " -->", URL),
        (
            "<noscript>"
            + PAGE[PAGE.index("<script>") : PAGE.index("</script>") + 9]
            + "</noscript>",
            URL,
        ),
        ("<script>var s = \"window._CONFIG = { APP_URL: 'x' }\";</script>", URL),
    ],
)
def test_reject_declarations(page, url):
    assert api(page, url) is None


def test_items_and_guard():
    data = json.dumps(BODY)
    assert not api_refusal(api(), result(data))
    items = api_items(api(), data, {}, TODAY)
    assert len(items) == 5
    assert items[0].title.startswith("韶关市发展和改革局关于印发")
    assert items[0].url == "http://www.sg.gov.cn/fgj/gkmlpt/content/2/2895/post_2895358.html"
    # 发布日期取 create_time（平台列表的"发布日期"），不取 date（"成文日期"，这一条是 10 月 30 日）
    assert items[0].publish_date == date(2026, 11, 11)
    assert any("gd.gov.cn" in item.url and "sg.gov.cn" not in item.url for item in items)


@pytest.mark.parametrize(
    "change",
    [
        {"errcode": 1, "errmessage": "请先登录"},
        {"errmessage": "失败"},
        {"permissions": ["admin"]},
        {"extra": 1},
        {"classify": {"id": 9}},
        {"total": True},
        {"offset": 100},
        {"offset": None},
        {"articles": [BODY["articles"][0]] * 501},
    ],
)
def test_guard_rejects(change):
    text = json.dumps({**BODY, **change})
    assert api_refusal(api(), result(text))
    assert not api_items(api(), text, {}, TODAY)


@pytest.mark.parametrize(
    "change",
    [
        {"title": None},
        {"url": None},
        {"url": ""},
        {"url": "  \n"},
        {"url": "https://www.sg.gov.cn/fgj/gkmlpt/index"},  # 解析成部门页自己
        {"url": "https://www.sg.gov.cn:99999/a.html"},  # 端口写坏，存不进库
        {"url": "http://[bad"},  # 括号没合上，解析不了
        {"url": "http://www.sg.gov.cn/fgj/gkmlpt/index"},  # 只差 http 的部门页自己
        {"title": "<strong> </strong>"},
    ],
)
def test_bad_article_is_skipped_not_the_whole_column(change):
    body = json.loads(json.dumps(BODY))
    body["articles"][0].update(change)
    text = json.dumps(body)
    assert not api_refusal(api(), result(text))  # 一条字段不对不拖累整栏（不记不能抓）
    items = api_items(api(), text, {}, TODAY)
    assert len(items) == 4 and BODY["articles"][0]["url"] not in {i.url for i in items}


def test_no_publish_time_at_all_is_not_recognised():
    # 整份没有一条带发布时间：平台多半改了字段，认不出（不悄悄全记成没日期）
    body = json.loads(json.dumps(BODY))
    for row in body["articles"]:
        row.pop("create_time")
    text = json.dumps(body)
    assert "认不出" in api_refusal(api(), result(text)) and not api_items(api(), text, {}, TODAY)


@pytest.mark.parametrize("bad", ["123", True, -5, None])
def test_bad_publish_time_keeps_the_item_without_date(bad):
    body = json.loads(json.dumps(BODY))
    body["articles"][0]["create_time"] = bad
    items = api_items(api(), json.dumps(body), {}, TODAY)
    assert len(items) == 5 and items[0].publish_date is None


def test_items_dates_and_cleaning():
    body = json.loads(json.dumps(BODY))
    rows = body["articles"]
    rows[0].update(title="关于<strong>重要事项</strong>的公告", url=" \n/one \n", create_time=0)
    rows[1].update(url="/two", create_time=1547628416)
    rows[2].update(url="/three", create_time=int(datetime(2026, 11, 15, tzinfo=UTC).timestamp()))
    rows[3].update(url="/one")
    rows[4].update(title="补助金额<5万元且>3万元的项目公示名单", url="/five")
    items = api_items(api(), json.dumps(body), {}, TODAY)
    assert items[0].title == "关于重要事项的公告" and items[0].url.endswith("/one")
    assert items[0].publish_date is None
    assert items[1].publish_date == date(2019, 1, 16)  # 具体时刻也按北京时间算日子
    assert items[2].publish_date is None  # 比今天晚 2 天以上：当没有日期
    assert items[3].title == "补助金额<5万元且>3万元的项目公示名单"  # 不是网页标签，照留
    assert len(items) == 4  # 重复的 /one 只留一条


def test_refusal_and_empty():
    assert "登录" in api_refusal(api(), result("请先登录"))
    assert "拦截" in api_refusal(api(), result("访问被拒绝"))
    assert not api_refusal(api(), result("")) and not api_items(api(), "", {}, TODAY)
    assert api_empty_list(api(), json.dumps({**BODY, "articles": []}))


class Cache:
    def __init__(self, pages):
        self.pages, self.calls, self.same = pages, [], []

    def get(self, url, same_address_only=False):
        self.calls.append(url)
        self.same.append(same_address_only)
        value = self.pages[url]
        return value if isinstance(value, FetchResult) else FetchResult(url, "ok", url, 200, value)


def column(category=8):
    return ColumnCheck(
        "440200",
        "市级",
        "广东",
        "韶关",
        "",
        "发改",
        "发改局",
        "通知",
        "通知",
        f"{URL}#xxgk-gkmlpt-{category}",
        "",
        "",
        "",
    )


def test_verify_and_import():
    cache = Cache({URL: PAGE, api().url: json.dumps(BODY)})
    checked = check_one(cache, column(), TODAY)
    assert checked.result == READABLE and checked.reason.endswith("列表接口读取")
    assert "经接口读" in import_status(checked)
    assert cache.calls == [URL, api().url]
    assert cache.same == [False, True]  # 接口只取页面写明的地址、跳转不跟（D-128 ①）
    assert checked.latest == date(2026, 11, 11)  # 发布日期，不是成文日期
    assert check_one(Cache({URL: "<html></html>"}), column(), TODAY).result == NEEDS_SCRIPT
    for failure in [result("", "robots_disallowed"), result("", "http_error", 403)]:
        assert check_one(Cache({URL: PAGE, api().url: failure}), column(), TODAY).result == BLOCKED
    col = column()
    col.url = URL + "#xxgk-hanweb-8"
    assert check_one(Cache({}), col, TODAY).result == NEEDS_SCRIPT


def test_categories_share_department_page_cache(tmp_path):
    from youju.govsites.pagecache import PageCache

    class Fetcher:
        def __init__(self):
            self.calls = []

        def fetch(self, url, same_address_only=False):
            self.calls.append(url)
            text = PAGE if url == URL else json.dumps({**BODY, "classify": {"id": 6}})
            if url == api().url:
                text = json.dumps(BODY)
            return FetchResult(url, "ok", url, 200, text)

    fetcher = Fetcher()
    cache = PageCache(tmp_path, fetcher)
    check_one(cache, column(8), TODAY)
    check_one(cache, column(6), TODAY)
    assert fetcher.calls.count(URL) == 1


def test_readable_only_for_recognised_lists():
    assert api_readable(api(), json.dumps(BODY))
    assert api_readable(api(), json.dumps({**BODY, "articles": []}))
    assert not api_readable(api(), "")
    assert not api_readable(api(), "<html><body>系统维护中</body></html>")
    assert not api_readable(api(), json.dumps({**BODY, "extra": 1}))


@pytest.mark.parametrize("text", ["", "<html><body>系统维护中，请稍后访问</body></html>"])
def test_verify_blank_or_maintenance_reply_is_not_read(text):
    # 护栏记"这次没取到"的返回：试读记打不开，不当成读出 0 条，也不读
    checked = check_one(Cache({URL: PAGE, api().url: text}), column(), TODAY)
    assert checked.result == DOWN and checked.items == 0


@pytest.mark.parametrize(
    "reply,expected",
    [
        (FetchResult("", "http_error", "", 401, ""), BLOCKED),
        (FetchResult("", "http_error", "", 412, ""), BLOCKED),
        (FetchResult("", "http_error", "", 521, ""), BLOCKED),
        (FetchResult("", "http_error", "", 503, ""), DOWN),
        (FetchResult("", "network_error", "", None, ""), DOWN),
        # 跳到页面没写的地址：不跟着读，记不能抓
        (FetchResult("", "redirect", "https://www.sg.gov.cn/login?from=api", 302, ""), BLOCKED),
        # 只换协议的跳转：这次没读出，记打不开（不跟）
        (
            FetchResult(
                "",
                "redirect",
                "http://www.sg.gov.cn/fgj/gkmlpt/api/all/8?page=1&sid=751012",
                301,
                "",
            ),
            DOWN,
        ),
    ],
)
def test_verify_api_failures(reply, expected):
    assert check_one(Cache({URL: PAGE, api().url: reply}), column(), TODAY).result == expected


def test_verify_without_final_url_uses_the_page_address():
    # 缓存里没记最终网址时，部门页地址也不带 # 后面
    page = FetchResult(URL, "ok", None, 200, PAGE)
    cache = Cache({URL: page, api().url: json.dumps(BODY)})
    assert check_one(cache, column(), TODAY).result == READABLE
    assert cache.calls == [URL, api().url]


def test_spotcheck_reads_gkmlpt_columns_through_the_api():
    from youju.govsites.spotcheck import _first_list_item

    cache = Cache({URL: PAGE, api().url: json.dumps(BODY)})
    item = _first_list_item(cache, f"{URL}#xxgk-gkmlpt-8", TODAY)
    assert item is not None and item.publish_date == date(2026, 11, 11)
    assert cache.calls == [URL, api().url] and cache.same == [False, True]


def test_verify_login_json_is_blocked_end_to_end():
    # 接口 200 但返回要登录：试读记不能抓、不读（D-128 ②）
    login = json.dumps({"errcode": 1, "errmessage": "请先登录"})
    checked = check_one(Cache({URL: PAGE, api().url: login}), column(), TODAY)
    assert checked.result == BLOCKED and "登录" in checked.reason and checked.items == 0


def test_verify_with_fragment_in_final_url():
    page = FetchResult(URL, "ok", URL + "#xxgk-gkmlpt-8", 200, PAGE)
    cache = Cache({URL: page, api().url: json.dumps(BODY)})
    assert check_one(cache, column(), TODAY).result == READABLE
    assert cache.calls == [URL, api().url]


def test_base_url_drops_the_column_fragment():
    # 正式抓取时部门页的最终网址可能带着栏目的 # 片段：条目按不带片段的部门页解析，回链照样跳过
    column = f"{URL}#xxgk-gkmlpt-8"
    found = find_list_api(PAGE, column, column)
    assert found.base_url == URL and found.url == api().url
    body = json.loads(json.dumps(BODY))
    body["articles"][0]["url"] = "http://www.sg.gov.cn/fgj/gkmlpt/index"  # 只差 http 的回链
    items = api_items(found, json.dumps(body), {}, TODAY)
    assert len(items) == 4 and all("gkmlpt/index" not in i.url for i in items)


def test_default_port_only_redirect_is_not_a_redirect_elsewhere():
    # 只是写不写默认端口的跳转：不算"跳到页面没写的地址"（不记不能抓），照常算这次没读出
    page_url = URL.replace(".cn/", ".cn:443/")
    found = api(url=page_url)
    moved = FetchResult(found.url, "redirect", api().url, 301, "")
    assert api_refusal(found, moved) == ""
    elsewhere = FetchResult(found.url, "redirect", "https://www.sg.gov.cn/login", 302, "")
    assert api_refusal(found, elsewhere)
    # 反过来（不带端口跳到带 :443 的同一地址）也一样
    back = FetchResult(api().url, "redirect", found.url, 301, "")
    assert api_refusal(api(), back) == ""


def test_multiple_categories_and_merge():
    found = api(category="8,6")
    parts = api_parts(found)
    assert [p.category_id for p in parts] == [8, 6]
    assert [p.url for p in parts] == [api(category=8).url, api(category=6).url]
    assert not api(category="8,999")
    assert not api(category="8,8")
    assert not api(category="1,2,3,4,5,6")
    other = {**BODY, "classify": {"id": 6}, "articles": [BODY["articles"][0]]}
    rows = api_items_merged(found, [json.dumps(BODY), json.dumps(other)], None, TODAY)
    assert len(rows) == 5
    no_date = dict(BODY["articles"][0], url="http://www.sg.gov.cn/undated.html", create_time=None)
    other["articles"] = [no_date, BODY["articles"][0]]
    rows = api_items_merged(found, [json.dumps(BODY), json.dumps(other)], None, TODAY)
    assert len(rows) == 6 and rows[-1].publish_date is None
    rows = rows[:-1]
    assert [r.publish_date for r in rows] == sorted([r.publish_date for r in rows], reverse=True)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (FetchResult("", "http_error", http_status=403), BLOCKED),
        (FetchResult("", "network_error", error="超时"), DOWN),
        ("", DOWN),
    ],
)
def test_multiple_categories_verify_failure(failure, expected):
    found = api(category="8,6")
    first, second = api_parts(found)
    cache = Cache({URL: PAGE, first.url: json.dumps(BODY), second.url: failure})
    checked = check_one(cache, column("8,6"), TODAY)
    assert checked.result == expected and checked.items == 0
    assert cache.calls == [URL, first.url, second.url]


def test_multiple_categories_empty_and_spotcheck_failure():
    from youju.govsites.spotcheck import _first_list_item

    found = api(category="8,6")
    first, second = api_parts(found)
    empty = {**BODY, "articles": []}
    cache = Cache(
        {
            URL: PAGE,
            first.url: json.dumps(empty),
            second.url: json.dumps({**empty, "classify": {"id": 6}}),
        }
    )
    checked = check_one(cache, column("8,6"), TODAY)
    assert "经列表接口识别出 0 条" in checked.reason
    cache = Cache(
        {
            URL: PAGE,
            first.url: json.dumps(BODY),
            second.url: FetchResult("", "http_error", http_status=403),
        }
    )
    assert _first_list_item(cache, URL + "#xxgk-gkmlpt-8,6", TODAY) is None


def test_parent_direct_part_keeps_only_items_attached_to_the_parent():
    # "政策"父分类（8350）只取直接挂在它上面的条目，挂在子分类（解读等）上的不要；叶子 6 照读
    column = f"{URL}#xxgk-gkmlpt-p8350,6"
    found = find_list_api(PAGE, URL, column)
    parent, leaf = api_parts(found)
    assert (parent.category_id, parent.direct_only) == (8350, True)
    assert (leaf.category_id, leaf.direct_only) == (6, False)
    body = json.loads(json.dumps(BODY))
    body["classify"]["id"] = 8350
    for row, main in zip(body["articles"], (8350, 8350, 7, 6, None), strict=True):
        row["classify_main"] = main
    kept = api_items(parent, json.dumps(body), {}, TODAY)
    assert [i.url for i in kept] == [r["url"] for r in BODY["articles"][:2]]
    # p 只能在第一个编号前；单独一个父分类也认
    assert find_list_api(PAGE, URL, f"{URL}#xxgk-gkmlpt-6,p8350") is None
    assert find_list_api(PAGE, URL, f"{URL}#xxgk-gkmlpt-p8350").direct_only


def test_verify_stops_at_the_first_refused_category():
    first = api(category=8)  # 单个分类的接口地址就是合读时第一个分类的
    blocked = FetchResult(first.url, "http_error", first.url, 403, "")
    cache = Cache({URL: PAGE, first.url: blocked})
    col = column()
    col.url = f"{URL}#xxgk-gkmlpt-8,6"
    assert check_one(cache, col, TODAY).result == BLOCKED
    assert cache.calls == [URL, first.url]  # 第二个分类不再请求


def test_spotcheck_gives_nothing_when_any_category_fails():
    from youju.govsites.spotcheck import _first_list_item

    for column_url in (f"{URL}#xxgk-gkmlpt-8", f"{URL}#xxgk-gkmlpt-8,6"):
        cache = Cache(
            {URL: PAGE, api().url: FetchResult(api().url, "http_error", api().url, 503, "")}
        )
        assert _first_list_item(cache, column_url, TODAY) is None
        assert cache.calls == [URL, api().url]
