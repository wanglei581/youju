"""龙讯返回护栏的离线测试；脚本识别和两步请求尚未完成。"""

import json
from datetime import date
from pathlib import Path

import pytest
from lxml import html as lxml_html

from youju.crawler.lonsun import (
    LonsunFormatError,
    catalog_host_allowed,
    column_spec,
    parse_catalog,
    parse_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "lonsun"
TODAY = date(2026, 11, 13)
BASE = "https://jx.ah.gov.cn/public/column/6991?type=4&action=list&catId=49949451"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_column_and_catalog_hosts():
    assert column_spec(BASE) == ("6991", "49949451")
    assert column_spec(BASE + "&keyWords=test") is None
    assert column_spec(BASE + "&catId=1") is None
    assert column_spec(BASE + "#fragment") is None
    assert catalog_host_allowed("https://czgz.wuhu.gov.cn/", "https://www.wuhu.gov.cn/a.js")
    assert not catalog_host_allowed("https://www.ah.gov.cn/", "https://www.wuhu.gov.cn/a.js")
    assert not catalog_host_allowed("https://www.ah.gov.cn/", "https://www.ah.gov.cn:444/a.js")


def test_catalog_fixture_and_bad_input():
    nodes = parse_catalog(fixture("jx_catalog.js"), "6991")
    assert any(node["id"] == 49949451 for node in nodes)
    for value in (
        'var catalogs = {6991:[{"id":1},{"id":1}]}',
        'var catalogs = {6992:[{"id":1}]}',
        "var catalogs = {6991:[oops]}",
    ):
        with pytest.raises(LonsunFormatError):
            parse_catalog(value, "6991")


def test_list_fixture_and_bom():
    items, empty = parse_response("\ufeff" + fixture("jx_api.json"), "list", BASE, TODAY)
    assert not empty
    assert len(items) == 3
    assert items[0].title.startswith("安徽省工业和信息化厅")
    assert items[0].url == "https://jx.ah.gov.cn/public/6991/150733231.html"
    assert items[0].publish_date == date(2026, 11, 6)


@pytest.mark.parametrize("name,count", [("wuhu", 8), ("luan", 6)])
def test_city_fixtures(name, count):
    items, empty = parse_response(fixture(f"{name}_api.json"), "city_page", BASE, TODAY)
    assert not empty
    assert len(items) == count


def test_list_rejects_bad_shape_and_dates():
    payload = json.loads(fixture("jx_api.json"))
    payload["extra"] = 1
    with pytest.raises(LonsunFormatError):
        parse_response(json.dumps(payload), "list", BASE, TODAY)
    del payload["extra"]
    for row in payload["data"]:
        row["publishDate"] = "bad"
    with pytest.raises(LonsunFormatError):
        parse_response(json.dumps(payload), "list", BASE, TODAY)


def test_city_rejects_extra_key_and_skips_redirect():
    payload = [{"catId": 1, "name": "跳转", "redirectLink": True}]
    items, empty = parse_response(json.dumps(payload), "city_page", BASE, TODAY)
    assert items == [] and empty
    payload[0]["unexpected"] = 1
    with pytest.raises(LonsunFormatError):
        parse_response(json.dumps(payload), "city_page", BASE, TODAY)


@pytest.mark.parametrize("name", ["jx", "wuhu", "huaibei", "luan", "chuzhou", "bengbu", "huainan"])
def test_split_real_public_tree_methods(name):
    from youju.crawler.lonsun import _public_tree_methods

    doc = lxml_html.document_fromstring(fixture(f"{name}_page.html"))
    scripts = [
        el.text or "" for el in doc.iter("script") if "function PublicTree" in (el.text or "")
    ]
    assert len(scripts) == 1
    methods = dict((name, body) for name, _args, body in _public_tree_methods(scripts[0]))
    assert {
        "bindEvt",
        "initTree",
        "init",
        "getData",
        "open",
        "getList",
        "getNewList",
    } <= methods.keys()


def test_split_public_tree_ignores_nested_punctuation():
    from youju.crawler.lonsun import _public_tree_methods

    source = r"""$.extend(PublicTree.prototype, {
      a: function () { var s = "})]"; var r = /[})]/g; /* } */ function f() { return {x: ')'}; } },
      b: function (x) { return x; }
    });"""
    assert [m[0] for m in _public_tree_methods(source)] == ["a", "b"]


def test_split_public_tree_rejects_duplicates_and_unbalanced_input():
    from youju.crawler.lonsun import _public_tree_methods

    with pytest.raises(LonsunFormatError, match="重复"):
        _public_tree_methods("$.extend(PublicTree.prototype, {a: function(){}, a: function(){}})")
    with pytest.raises(LonsunFormatError):
        _public_tree_methods("$.extend(PublicTree.prototype, {a: function(){")


def test_expression_evaluation():
    import math

    from youju.crawler.lonsun import UNKNOWN, _Environment, _evaluate, _parse_expression

    env = _Environment({"NaN": math.nan})

    def evaluate(source):
        return _evaluate(_parse_expression(source), env)

    for source, expected in [
        ("1 === '1'", False),
        ("1 == '1'", True),
        ("null == undefined", True),
        ("null === undefined", False),
        ("NaN == NaN", False),
        ("'' && x", ""),
        ("0 || 'a'", "a"),
        ("x && false", UNKNOWN),
        ("false && x", False),
        ("true || x", True),
        ("[1, 2].indexOf(2)", 1),
        ("[1, 2].indexOf('2')", -1),
        ("{'6596211_7036844': 1}['6596211' + '_' + 7036844] == 1", True),
        ("'a' + 1", "a1"),
        ("1 + 2", 3),
        ("+''", 0),
        ("+'12'", 12),
        ("'1.5' * 2", 3),
        ("x ? 'a' : 'b'", UNKNOWN),
        (r"'\''", "'"),
        (r'''"\""''', '"'),
        ("'中'", "中"),
        (r"'\x41'", "A"),
    ]:
        assert evaluate(source) == expected, source
    assert math.isnan(evaluate("+'x'"))


def test_expression_rejects_unsupported():
    from youju.crawler.lonsun import _parse_expression

    for source in ("`template`", "new X()", "x => x", "x ?? y", "x++", "x?.y", "/abc/"):
        with pytest.raises(LonsunFormatError, match="记号"):
            _parse_expression(source)


def test_statement_parser():
    from youju.crawler.lonsun import _parse_statements

    tree = _parse_statements(
        "var a = 1, b; if (a) { b = 2 } else if (c) d();\na = 3\nb = 4\nreturn\nfunction f(){}\nfor (var i = 0; i < 2; i++) {}"
    )
    assert [node[0] for node in tree] == [
        "var",
        "if",
        "expression",
        "expression",
        "return",
        "function",
        "unknown",
    ]
    assert tree[1][3][0] == "if"
    assert tree[4][1] is None
    assert tree[-1][1].startswith("for")


def test_fixture_method_bodies_parse():
    from youju.crawler.lonsun import _parse_statements, _public_tree_methods

    names = {"getData", "open", "getList", "getNewList", "preprocessData", "init"}
    for path in sorted(FIXTURES.glob("*_page.html")):
        document = lxml_html.fromstring(path.read_text(encoding="utf-8"))
        scripts = [
            s.text or ""
            for s in document.xpath("//script")
            if "PublicTree.prototype" in (s.text or "")
        ]
        assert len(scripts) == 1, path.name
        methods = dict((name, body) for name, _, body in _public_tree_methods(scripts[0]))
        for name in names & methods.keys():
            try:
                _parse_statements(methods[name])
            except LonsunFormatError as exc:
                pytest.fail(f"{path.name} {name}: {exc}")


def test_unsupported_statement_in_unvisited_branch():
    from youju.crawler.lonsun import _parse_statements

    tree = _parse_statements("if (false) { var x = new Thing(); } else { ok(); }")
    assert tree[0][0] == "if"
    assert tree[0][2][1][0][0] == "unknown"
    assert tree[0][3][1][0][0] == "expression"


@pytest.mark.parametrize(
    "name,page,expected",
    [
        ("jx", BASE, "https://jx.ah.gov.cn/49631981/6991.js"),
        (
            "wuhu",
            "https://czgz.wuhu.gov.cn/public/column/6596381?type=4&catId=6732121&action=list",
            "https://www.wuhu.gov.cn/openness/6787891/6596381.js?v=1558",
        ),
        (
            "huaibei",
            "https://sthjj.huaibei.gov.cn/public/column/50?type=4&catId=4745681&action=list",
            "https://www.huaibei.gov.cn/zwgk/4697505/50.js?v=111",
        ),
        (
            "luan",
            "https://jxj.luan.gov.cn/public/column/6608211?type=4&catId=7047503&action=list",
            "https://www.luan.gov.cn/6789941/6608211.js",
        ),
        (
            "chuzhou",
            "https://sthjj.chuzhou.gov.cn/public/column/2681525?type=4&catId=161734542&action=list",
            "https://sthjj.chuzhou.gov.cn/czxxgk/2653861/2681525.js?v=",
        ),
        (
            "bengbu",
            "https://www.bengbu.gov.cn/zfxxgk/public/column/29661?type=4&catId=8162649&action=list",
            "https://www.bengbu.gov.cn/zfxxgk/6795621/29661.js?v=50000",
        ),
        (
            "huainan",
            "https://swj.huainan.gov.cn/public/column/118319855?type=4&catId=159671403&action=list",
            "https://www.huainan.gov.cn/4964522/118319855.js?v=",
        ),
    ],
)
def test_find_lonsun_fixtures(name, page, expected):
    from youju.crawler.lonsun import UNKNOWN, find_lonsun_api

    api = find_lonsun_api(fixture(f"{name}_page.html"), page, page)
    assert api is not None
    assert api.url == expected
    if name in ("jx", "wuhu", "huainan"):
        assert (
            api.plan.label
            == {"jx": "", "wuhu": "/wuhu", "huainan": "/huainan"}[name] + "/site/label/8888"
        )
    if name == "jx":
        assert [k for k, _ in api.plan.list_params] == [
            "labelName",
            "siteId",
            "organId",
            "pageSize",
            "pageIndex",
            "isDate",
            "dateFormat",
            "length",
            "type",
            "action",
            "result",
            "isJson",
            "keyWords",
            "isRel",
            "catIds",
        ]
    if name == "luan":
        assert dict(api.plan.globals)["isSsbm"] is UNKNOWN
        updated = fixture("luan_page.html").replace("var isSsbm = true;", "")
        assert dict(find_lonsun_api(updated, page, page).plan.globals)["isSsbm"] is False


def test_find_lonsun_rejects_unsafe_variants():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html")
    variants = [
        page.replace("function PublicTree()", "function PublicTree(){} function PublicTree()", 1),
        page.replace(
            "<script>\n  var _organTree = null;",
            '<script type="text/html">\n  var _organTree = null;',
            1,
        ),
        page.replace(
            'organId: me.organId,\n      pageSize: "20"',
            'organId: unknownValue,\n      pageSize: "20"',
        ),
        page.replace("me.prefix + '/site/label/8888'", "'/site/label/9999'"),
        page.replace("me.prefix + '/site/label/8888'", "'//evil.example/site/label/8888'"),
        page.replace("/49631981/6991.js", "https://evil.example/49631981/6991.js"),
        page.replace("function PublicTree()", "PublicTree = other; function PublicTree()", 1),
    ]
    for variant in variants:
        assert find_lonsun_api(variant, BASE, BASE) is None
    assert find_lonsun_api(page, BASE, BASE + "&keyWords=x") is None
    assert find_lonsun_api(page, BASE, BASE.replace("&catId=49949451", "")) is None
    assert find_lonsun_api(page, BASE.replace("49949451", "1"), BASE) is None
    script = fixture("chuzhou_page.html").replace(" + Math.random()", " + other + Math.random()")
    column = "https://sthjj.chuzhou.gov.cn/public/column/2681525?type=4&catId=1&action=list"
    assert find_lonsun_api(script, column, column) is None
    assert find_lonsun_api("<noscript>" + page + "</noscript>", BASE, BASE) is None


PAGES = {
    "jx": "https://jx.ah.gov.cn/public/column/6991",
    "wuhu": "https://czgz.wuhu.gov.cn/public/column/6596381",
    "huaibei": "https://sthjj.huaibei.gov.cn/public/column/50",
    "luan": "https://jxj.luan.gov.cn/public/column/6608211",
    "chuzhou": "https://sthjj.chuzhou.gov.cn/public/column/2681525",
    "bengbu": "https://www.bengbu.gov.cn/zfxxgk/public/column/29661",
    "huainan": "https://swj.huainan.gov.cn/public/column/118319855",
}


def resolve_fixture(name, cat_id, *, page=None, catalog=None, base=None):
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    url = (base or PAGES[name]) + f"?type=4&catId={cat_id}&action=list"
    api = find_lonsun_api(page or fixture(f"{name}_page.html"), url, url)
    assert api is not None
    return resolve_lonsun(api, catalog or fixture(f"{name}_catalog.js"))


@pytest.mark.parametrize(
    "name,cat_id,expected",
    [
        (
            "jx",
            "49949451",
            "https://jx.ah.gov.cn/site/label/8888?labelName=publicInfoList&siteId=49631981&organId=6991&pageSize=20&pageIndex=1&isDate=true&dateFormat=yyyy-MM-dd&length=50&type=4&action=list&result=&isJson=true&keyWords=&isRel=true&catIds=&catId=49949451",
        ),
        (
            "jx",
            "49949431",
            "https://jx.ah.gov.cn/site/label/8888?labelName=publicInfoList&siteId=49631981&organId=6991&catId=49949431&num=5&pageIndex=1&pageSize=15&action=city_page&type=4&isJson=true",
        ),
        (
            "wuhu",
            "6732111",
            "https://czgz.wuhu.gov.cn/wuhu/site/label/8888?labelName=publicInfoList&siteId=6787891&organId=6596381&catId=6732111&num=5&pageIndex=1&pageSize=15&action=city_page&type=4&isJson=true",
        ),
        (
            "wuhu",
            "6732121",
            "https://czgz.wuhu.gov.cn/wuhu/site/label/8888?labelName=publicInfoList&siteId=6787891&organId=6596381&pageSize=20&pageIndex=1&isDate=true&dateFormat=yyyy-MM-dd&length=50&type=4&action=list&result=&isJson=true&keyWords=&isRel=true&catIds=&catId=6732121&catalogType=",
        ),
        (
            "huaibei",
            "14237535",
            "https://sthjj.huaibei.gov.cn/zwgk/site/label/8888?labelName=publicInfoList&siteId=4697505&organId=50&pageSize=20&pageIndex=1&isDate=true&dateFormat=yyyy-MM-dd&length=50&type=4&action=list&result=&isJson=true&keyWords=&isSetValue=true&catIds=&catId=14237535",
        ),
        (
            "chuzhou",
            "161734542",
            "https://sthjj.chuzhou.gov.cn/czxxgk/site/label/8888?labelName=publicInfoList&siteId=2653861&organId=2681525&pageSize=20&pageIndex=1&isDate=true&dateFormat=yyyy-MM-dd&length=50&type=4&action=list&result=&isJson=true&keyWords=&isSetValue=true&catIds=&catId=161734542",
        ),
        (
            "huainan",
            "159421266",
            "https://swj.huainan.gov.cn/huainan/site/label/8888?labelName=publicInfoList&siteId=4964522&organId=118319855&catId=159421266&num=5&pageIndex=1&pageSize=15&action=city_page&type=4&isJson=true",
        ),
    ],
)
def test_resolve_addresses(name, cat_id, expected):
    from youju.crawler.adapters import ListApi

    result = resolve_fixture(name, cat_id)
    assert isinstance(result, ListApi), result
    assert result.url == expected
    assert result.plan.cat_id == cat_id
    assert result.plan.mode == ("city_page" if "action=city_page" in expected else "list")


@pytest.mark.parametrize(
    "name,cat",
    [
        ("huaibei", "14237502"),
        ("luan", "7024001"),
        ("chuzhou", "170084000"),
        ("bengbu", "18180302"),
        ("bengbu", "18179389"),
    ],
)
def test_resolve_special_paths(name, cat):
    assert isinstance(resolve_fixture(name, cat), str)


def test_special_organ_and_global_conditions():
    page = fixture("huaibei_page.html").replace("'50'", "'15'").replace("/50.js", "/15.js")
    catalog = fixture("huaibei_catalog.js").replace("{50:", "{15:")
    base = PAGES["huaibei"].replace("/50", "/15")
    assert isinstance(
        resolve_fixture("huaibei", "14235048", page=page, catalog=catalog, base=base), str
    )
    assert not isinstance(
        resolve_fixture("huaibei", "14237535", page=page, catalog=catalog, base=base), str
    )
    page = fixture("luan_page.html").replace("var isSsbm = true;", "")
    result = resolve_fixture("luan", "7024001", page=page)
    assert result.plan.mode == "city_page"
    assert "catId=7024001" in result.url


def test_wuhu_special_map():
    page = fixture("wuhu_page.html").replace("6596381", "6596211")
    catalog = fixture("wuhu_catalog.js").replace("6596381:", "6596211:")
    catalog = catalog.replace(
        "[{", '[{"id":7036844,"name":"测试","parentId":0,"isParent":false},{', 1
    )
    result = resolve_fixture(
        "wuhu",
        "7036844",
        page=page,
        catalog=catalog,
        base=PAGES["wuhu"].replace("6596381", "6596211"),
    )
    assert isinstance(result, str) and "特别的读法" in result


@pytest.mark.parametrize("name,mode,count", [("huaibei", "list", 2), ("chuzhou", "list", 3)])
def test_remaining_response_fixtures(name, mode, count):
    items, empty = parse_response(fixture(f"{name}_api.json"), mode, BASE, TODAY)
    assert not empty and len(items) == count


def test_inconsistent_children_are_unknown():
    catalog = fixture("jx_catalog.js")
    nodes = parse_catalog(catalog, "6991")
    node = next(n for n in nodes if n["id"] == 49949451)
    node["isParent"] = True
    bad = "var catalogs = {6991:" + json.dumps(nodes) + "};"
    assert isinstance(resolve_fixture("jx", "49949451", catalog=bad), str)


def test_resolve_guards_tree_and_request():
    page = fixture("jx_page.html")
    assert "init" in resolve_fixture(
        "jx", "49949451", page=page.replace("me.getData();", "me.other();", 1)
    )
    changed = page.replace("// ids.push(el.id);", "ids.push(el.id);")
    if changed != page:
        result = resolve_fixture("jx", "49949431", page=changed)
        assert isinstance(result, str) and "隐去" in result
    changed = page.replace("data: me.listParams,", "data: me.listParams, extra: 1,", 1)
    assert isinstance(resolve_fixture("jx", "49949451", page=changed), str)
    changed = page.replace("me.listParams.catId = me.currNode.id;", "me.unknownAction();", 1)
    result = resolve_fixture("jx", "49949451", page=changed)
    assert isinstance(result, str) and "me.unknownAction()" in result


def test_wuhu_callback_excludes_selected_child():
    result = resolve_fixture("wuhu", "6732111")
    assert result.plan.excluded_children == ()
    page = fixture("wuhu_page.html").replace("6596381", "6605201")
    catalog = fixture("wuhu_catalog.js").replace("6596381:", "6605201:")
    catalog = catalog.replace(
        "[{",
        '[{"id":7075534,"name":"测试","parentId":0,"isParent":true},'
        '{"id":7093780,"name":"下级","parentId":7075534,"isParent":false},{',
        1,
    )
    result = resolve_fixture(
        "wuhu",
        "7075534",
        page=page,
        catalog=catalog,
        base=PAGES["wuhu"].replace("6596381", "6605201"),
    )
    assert result.plan.excluded_children == (7093780,)
    row = {
        "title": "关于测试事项的通知",
        "link": "https://example.gov.cn/a",
        "publishDate": "2026-11-01",
    }
    payload = [
        {
            "catId": 7093780,
            "name": "被排除",
            "redirectLink": False,
            "childPublicContentList": [row],
        },
        {
            "catId": 2,
            "name": "保留",
            "redirectLink": False,
            "childDataList": [
                {
                    "catId": 7093780,
                    "name": "嵌套",
                    "redirectLink": False,
                    "childPublicContentList": [{**row, "link": "https://example.gov.cn/b"}],
                }
            ],
        },
    ]
    items, empty = parse_response(
        json.dumps(payload), result.plan.mode, BASE, TODAY, result.plan.excluded_children
    )
    assert not empty and [item.url for item in items] == ["https://example.gov.cn/b"]


@pytest.mark.parametrize("change", ["slice", "link"])
def test_wuhu_callback_rejects_unknown_processing(change):
    page = fixture("wuhu_page.html")
    if change == "slice":
        page = page.replace("var isLink = [];", "var isLink = []; data = data.slice(0, 3);", 1)
    else:
        page = page.replace("var isLink = [];", "var isLink = [6732121];", 1)
    result = resolve_fixture("wuhu", "6732111", page=page)
    assert isinstance(result, str) and "页面对返回的列表另有处理" in result


def test_adapter_guard_and_follow():
    from youju.crawler.adapters import (
        LONSUN,
        api_follow,
        api_items,
        api_needs_follow,
        api_readable,
        api_refusal,
        find_list_api,
        list_api_adapter,
    )
    from youju.crawler.fetcher import FetchResult

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    assert tree and tree.adapter == LONSUN and api_needs_follow(tree)
    assert list_api_adapter(BASE) == LONSUN

    def reply(url, text="", status="ok", http=200):
        return FetchResult(url, status, url, http, text)

    assert "栏目树脚本" in api_refusal(tree, reply(tree.url, status="http_error", http=403))
    assert "栏目树脚本" in api_follow(tree, reply(tree.url, "garbage"))
    api = api_follow(tree, reply(tree.url, fixture("jx_catalog.js")))
    assert not isinstance(api, str) and not api_needs_follow(api)
    assert "403" in api_refusal(api, reply(api.url, status="http_error", http=403))
    assert api_refusal(api, reply(api.url, '<html><form><input type="password">登录</form></html>'))
    assert not api_refusal(api, reply(api.url)) and not api_readable(api, "")
    assert api_readable(api, "\ufeff" + fixture("jx_api.json"))
    assert len(api_items(api, fixture("jx_api.json"), None, TODAY)) == 3
    data = json.loads(fixture("jx_api.json"))
    for change in (lambda d: d.update(extra=1), lambda d: d.pop("total")):
        bad = data.copy()
        change(bad)
        assert api_refusal(api, reply(api.url, json.dumps(bad)))


class LonsunCache:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, same_address_only=False):
        from youju.crawler.fetcher import FetchResult

        self.calls.append((url, same_address_only))
        value = self.pages[url]
        return value if isinstance(value, FetchResult) else FetchResult(url, "ok", url, 200, value)


def lonsun_column(url=BASE):
    from youju.govsites.verify import ColumnCheck

    return ColumnCheck(
        "340000", "省级", "安徽", "", "", "经信", "经信厅", "通知", "通知", url, "", "", ""
    )


def test_verify_spotcheck_and_failures():
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.govsites.spotcheck import _first_list_item
    from youju.govsites.verify import BLOCKED, DOWN, NEEDS_SCRIPT, READABLE, check_one

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    api = api_follow(tree, FetchResult(tree.url, "ok", tree.url, 200, fixture("jx_catalog.js")))
    pages = {
        BASE: fixture("jx_page.html"),
        tree.url: fixture("jx_catalog.js"),
        api.url: fixture("jx_api.json"),
    }
    cache = LonsunCache(pages)
    checked = check_one(cache, lonsun_column(), TODAY)
    assert checked.result == READABLE and checked.items == 3
    assert cache.calls == [(BASE, False), (tree.url, True), (api.url, True)]
    assert _first_list_item(LonsunCache(pages), BASE, TODAY).publish_date
    for response, expected in [
        (FetchResult(tree.url, "http_error", tree.url, 403), BLOCKED),
        ("unexpected script", NEEDS_SCRIPT),
    ]:
        checked = check_one(LonsunCache({**pages, tree.url: response}), lonsun_column(), TODAY)
        assert checked.result == expected and "栏目树脚本" in checked.reason
    huainan = (
        "https://swj.huainan.gov.cn/public/column/118319855?type=4&catId=159421266&action=list"
    )
    tree = find_list_api(fixture("huainan_page.html"), huainan, huainan)
    api = api_follow(
        tree, FetchResult(tree.url, "ok", tree.url, 200, fixture("huainan_catalog.js"))
    )
    assert not isinstance(api, str)
    pages = {
        huainan: fixture("huainan_page.html"),
        tree.url: fixture("huainan_catalog.js"),
        api.url: FetchResult(api.url, "http_error", api.url, 404),
    }
    checked = check_one(LonsunCache(pages), lonsun_column(huainan), TODAY)
    assert checked.result == DOWN and "列表接口" in checked.reason


def test_unknown_reason_identifies_global():
    url = "https://jxj.luan.gov.cn/public/column/6608211?type=4&catId=7024001&action=list"
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult

    tree = find_list_api(fixture("luan_page.html"), url, url)
    reason = api_follow(
        tree, FetchResult(tree.url, "ok", tree.url, 200, fixture("luan_catalog.js"))
    )
    assert isinstance(reason, str) and "isSsbm" in reason


def test_service_adapter_tree_failure_and_empty_response():
    from youju.crawler.adapters import LONSUN, api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import _items_via_adapter

    html = fixture("jx_page.html")
    tree = find_list_api(html, BASE, BASE)
    page = FetchResult(BASE, "ok", BASE, 200, html)
    catalog = FetchResult(tree.url, "ok", tree.url, 200, fixture("jx_catalog.js"))
    api = api_follow(tree, catalog)

    class Fetcher:
        def __init__(self, responses):
            self.responses = responses
            self.calls = []

        def fetch(self, url, same_address_only=False):
            self.calls.append((url, same_address_only))
            return self.responses[url]

    cfg = {"list_api_open": LONSUN}
    fetcher = Fetcher({tree.url: catalog, api.url: FetchResult(api.url, "ok", api.url, 200, "")})
    result = _items_via_adapter(fetcher, page, BASE, cfg, TODAY, tree)
    assert result.api_url == tree.url and "列表接口" in result.part
    assert fetcher.calls == [(tree.url, True), (api.url, True)]
    fetcher = Fetcher({tree.url: FetchResult(tree.url, "http_error", tree.url, 403)})
    result = _items_via_adapter(fetcher, page, BASE, cfg, TODAY, tree)
    assert "栏目树脚本" in result.refused and result.api_url == tree.url


def test_lonsun_unrecognized_catalog_host_and_writes():
    from youju.crawler.lonsun import lonsun_unrecognized_reason
    from youju.govsites.verify import NEEDS_SCRIPT, check_one

    url = "https://czgz.wuhu.gov.cn/public/column/6596381?type=4&catId=6732111&action=list"
    page = fixture("wuhu_page.html").replace("www.wuhu.gov.cn/openness/", "www.ah.gov.cn/openness/")
    reason = lonsun_unrecognized_reason(page, url, url)
    assert "不在同一个政府网站域名" in reason
    assert "czgz.wuhu.gov.cn" in reason and "www.ah.gov.cn" in reason
    checked = check_one(LonsunCache({url: page}), lonsun_column(url), TODAY)
    assert checked.result == NEEDS_SCRIPT and checked.reason.startswith("待人工处理：")
    assert not catalog_host_allowed("https://example.com/", "https://example.com/a.js")


def test_platform_query_and_missing_get():
    from youju.crawler.lonsun import _platform_query

    assert _platform_query("?a=x+y&b=x%2By&c&%61=%E4%B8%AD&bad=%FF&broken=%Q1") == {
        "a": "中",
        "b": "x+y",
        "c": "",
        "bad": "%FF",
        "broken": "%Q1",
    }
    page = fixture("jx_page.html").replace(
        "if (me.currNode.children) {", "if (Ls.url.get('x') === '') {", 1
    )
    result = resolve_fixture("jx", "49949451", page=page)
    assert not isinstance(result, str) and result.plan.mode == "city_page"


@pytest.mark.parametrize(
    "status,expected", [("robots_disallowed", "blocked"), ("robots_error", "down")]
)
def test_tree_robots_in_verify_and_spotcheck(status, expected):
    from youju.crawler.adapters import fetch_api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.govsites.spotcheck import _first_list_item
    from youju.govsites.verify import BLOCKED, DOWN, check_one

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    pages = {BASE: fixture("jx_page.html"), tree.url: FetchResult(tree.url, status, error=status)}
    assert fetch_api_follow(tree, LonsunCache(pages).get)[2] == expected
    checked = check_one(LonsunCache(pages), lonsun_column(), TODAY)
    assert checked.result == (BLOCKED if expected == "blocked" else DOWN)
    assert "栏目树脚本" in checked.reason
    reasons = []
    assert _first_list_item(LonsunCache(pages), BASE, TODAY, reasons) is None
    assert "栏目树脚本" in reasons[0]
    assert ("不能抓" if expected == "blocked" else "这次没取到") in reasons[0]


@pytest.mark.parametrize("status", ["ok", "robots_disallowed", "robots_error"])
def test_catalog_cache_concurrent_reuses_success_and_refusal(status):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import CatalogCache

    cache = CatalogCache()
    calls = []
    lock = Lock()

    def fetch(url, same_address_only=False):
        with lock:
            calls.append((url, same_address_only))
        return FetchResult(url, status)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: cache.get("https://www.ah.gov.cn/tree.js", fetch), range(20))
        )
    assert len(calls) == 1 and calls[0][1]
    assert all(result is results[0] for result in results)


def test_unrecognized_reasons_for_page_and_tree():
    from youju.crawler.lonsun import lonsun_unrecognized_reason

    page = fixture("jx_page.html")
    src = '<script type="text/javascript" src="/49631981/6991.js"></script>'
    cases = [
        (page.replace(src, ""), "没找到栏目树脚本"),
        (page.replace(src, src + '<script src="/123/6991.js"></script>'), "路径不一样"),
        (page.replace("/site/label/8888", "/invalid"), "接口路径认不出"),
        (page.replace("function PublicTree()", "function PublicTree(x)", 1), "页面脚本认不出"),
    ]
    for changed, reason in cases:
        assert reason in lonsun_unrecognized_reason(changed, BASE, BASE)
    assert (
        lonsun_unrecognized_reason('<script>var x = "function PublicTree(";</script>', BASE, BASE)
        is None
    )


def test_city_flags_at_both_levels():
    payload = json.loads(fixture("luan_api.json"))
    payload[0].update(isParent=True, parent=False)
    payload[0]["childDataList"][0].update(isParent=False, parent=False)
    items, _ = parse_response(json.dumps(payload), "city_page", BASE, TODAY)
    assert len(items) == 6
    for node in (payload[0], payload[0]["childDataList"][0]):
        for key in ("isParent", "parent"):
            original = node[key]
            node[key] = "false"
            with pytest.raises(LonsunFormatError):
                parse_response(json.dumps(payload), "city_page", BASE, TODAY)
            node[key] = original


def test_county_portal_whitelist_and_verify():
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.lonsun import lonsun_unrecognized_reason
    from youju.govsites.verify import READABLE, check_one

    url = "https://www.yjq.gov.cn/public/column/6596381?type=4&catId=6732111&action=list"
    html = fixture("wuhu_page.html")
    assert find_list_api(html, url, url) is None
    reason = lonsun_unrecognized_reason(html, url, url)
    assert "www.wuhu.gov.cn" in reason and "本市" in reason
    for bad in ("www.huainan.gov.cn", "www.wuhu.gov.cn.evil.com"):
        assert find_list_api(html, url, url, catalog_host=bad) is None
    unsafe_html = html.replace("www.wuhu.gov.cn", "portal.example.com")
    assert find_list_api(unsafe_html, url, url, catalog_host="portal.example.com") is None
    tree = find_list_api(html, url, url, catalog_host="www.wuhu.gov.cn")
    api = api_follow(tree, FetchResult(tree.url, "ok", tree.url, 200, fixture("wuhu_catalog.js")))
    assert api and "city_page" in api.url
    col = lonsun_column(url)
    col.city, col.level = "芜湖", "区县级"
    cache = LonsunCache(
        {url: html, tree.url: fixture("wuhu_catalog.js"), api.url: fixture("wuhu_api.json")}
    )
    assert check_one(cache, col, TODAY).result == READABLE


def test_special_chain_reason_uses_source():
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult

    url = PAGES["luan"] + "?type=4&catId=7024001&action=list"
    html = fixture("luan_page.html").replace("var isSsbm = false;", "var other = false;")
    tree = find_list_api(html, url, url)
    reason = api_follow(
        tree, FetchResult(tree.url, "ok", tree.url, 200, fixture("luan_catalog.js"))
    )
    assert "$('.gfxwj-a').trigger(\"click\")" in reason
    assert "('call'" not in reason


@pytest.mark.parametrize(
    "change",
    [
        "me.listParams.pageSize = '10';",
        "$.extend(me.listParams, {pageSize: '10'});",
        "me.hideIds.push(1);",
        "if (true) { me.getData(); }",
        "return;",
    ],
)
def test_init_before_getdata_rejects_state_or_control(change):
    page = fixture("jx_page.html").replace(
        "me.getData();\n      // me.getReportyear",
        change + "\n      me.getData();\n      // me.getReportyear",
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


@pytest.mark.parametrize("removed", ["me.isRefresh();", "me.initTree();"])
def test_init_requires_refresh_and_tree(removed):
    page = fixture("jx_page.html").replace(removed, "", 1)
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


@pytest.mark.parametrize(
    "condition",
    [
        "me.special",
        "me.topLevelParentId",
        "Ls.missing",
        "location.missing",
        "_organTree2.missing",
        "$.missing",
        "[1].indexOf(1, 1) > -1",
        "[1] == 1",
        "[1] + 2 == 12",
    ],
)
def test_unknown_conditions_never_send_list(condition):
    page = fixture("jx_page.html").replace(
        "var url = '' + me.currNode.uri",
        "if (" + condition + ") { me.zdlyxxgkList(); }\n      var url = '' + me.currNode.uri",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


@pytest.mark.parametrize(
    "injected",
    [
        "catalogs[6991] = [];",
        "Ls.ajaxGet = evil;",
        "Ls.ajax = evil;",
        "$.ajaxSetup({});",
        "jQuery.ajaxSetup({});",
        "$.ajaxPrefilter(function(){});",
        "$.extend(PublicTree.prototype, {open: function(){}});",
    ],
)
def test_top_level_changes_rejected(injected):
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace("</body>", "<script>" + injected + "</script></body>")
    assert find_lonsun_api(page, BASE, BASE) is None


def test_cross_origin_pushstate_rejected():
    assert isinstance(resolve_fixture("jx", "49949441"), str)


@pytest.mark.parametrize("injected", ["el.parentId = 0;", "el['parentId'] = 0;"])
def test_preprocess_cannot_change_tree_parent(injected):
    page = fixture("jx_page.html").replace(
        "var str = el.name;", injected + " var str = el.name;", 1
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_unknown_initializer_call_rejected():
    page = fixture("jx_page.html").replace(
        "var url = '' + me.currNode.uri",
        "var r = me.something(); var url = '' + me.currNode.uri",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


@pytest.mark.parametrize(
    "write", ["window.flag = true;", "flag++;", "flag.push(1);", "flag[0] = 1;"]
)
def test_top_level_global_mutations_are_unknown(write):
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    page = fixture("jx_page.html").replace(
        "</body>", "<script>var flag = []; " + write + "</script></body>"
    )
    page = page.replace(
        "var url = '' + me.currNode.uri",
        "if (flag) { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
        1,
    )
    api = find_lonsun_api(page, BASE, BASE)
    assert api is None or isinstance(resolve_lonsun(api, fixture("jx_catalog.js")), str)


@pytest.mark.parametrize("change", ["el.name += 'x';", "el['link'] = 'x';"])
def test_preprocess_modified_node_fields_unknown(change):
    field = "name" if "name" in change else "link"
    page = fixture("jx_page.html").replace("var str = el.name;", change + " var str = el.name;", 1)
    page = page.replace(
        "var url = '' + me.currNode.uri",
        "if (me.currNode." + field + ") { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_extend_omits_undefined_query_value():
    from youju.crawler.adapters import ListApi

    page = fixture("jx_page.html")
    page = page.replace("data: me.listParams", "data: params", 1).replace(
        "getList: function () {\n      var me = this;\n      var tipsLoading = Ls.tipsLoading('加载中...', {times: 999999});",
        "getList: function () {\n      var me = this;\n      var tipsLoading = Ls.tipsLoading('加载中...', {times: 999999});\n      var params = $.extend({}, me.listParams, {catalogType: me.query.catalogType});",
        1,
    )
    result = resolve_fixture("jx", "49949451", page=page)
    assert isinstance(result, ListApi), result
    assert "catalogType" not in result.url


@pytest.mark.parametrize(
    "replacement",
    [
        "http://jx.ah.gov.cn/49631981/6991.js",
    ],
)
def test_https_rejects_http_script(replacement):
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace("/49631981/6991.js", replacement)
    assert find_lonsun_api(page, BASE, BASE) is None


def test_invalid_label_and_ztree_initial_value_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html")
    for changed in (
        page.replace("me.prefix + '/site/label/8888'", "'/bad space/site/label/8888'"),
        page.replace("me.ztreeData2 = catalogs[me.organId]", "me.ztreeData2 = []"),
    ):
        assert find_lonsun_api(changed, BASE, BASE) is None


def test_init_tree_rejects_deleting_second_tree_children():
    page = fixture("jx_page.html").replace(
        "$.each(_organTree.getNodes(), function (i, node) {",
        "$.each(_organTree2.getNodes(), function (i, node) {",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_mutating_global_during_initialization_is_unknown():
    page = fixture("jx_page.html").replace("</body>", "<script>var flag = [];</script></body>")
    page = page.replace("me.bindEvt();", "me.bindEvt(); flag.push(1);", 1)
    page = page.replace(
        "var url = '' + me.currNode.uri",
        "if (flag) { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_document_write_literal_disallows_space():
    from youju.crawler.lonsun import find_lonsun_api

    url = PAGES["chuzhou"] + "?type=4&catId=161734542&action=list"
    page = fixture("chuzhou_page.html").replace("2681525.js?v=", "2681525.js?v=bad value")
    assert find_lonsun_api(page, url, url) is None


def test_children_elements_use_node_rules():
    page = fixture("jx_page.html").replace(
        "var url = '' + me.currNode.uri",
        "if (me.currNode.children[0].notModeled) { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949431", page=page), str)


def test_pre_method_alias_mutation_rejected():
    page = fixture("jx_page.html").replace(
        "var me = this;\n\n      // 隐藏的栏目",
        "var me = this; var p = me.listParams; var q = p; q.pageSize = '10';\n\n      // 隐藏的栏目",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_nested_call_cannot_mutate_list_params():
    page = fixture("jx_page.html").replace(
        "preprocessData: function () {",
        "mutate: function () { this.listParams.pageSize = '10'; },\n"
        "    preprocessData: function () { this.mutate();",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_unknown_global_call_rejected():
    page = (
        fixture("jx_page.html")
        .replace(
            "function PublicTree() {",
            "function mutate(obj) { obj.listParams.pageSize = '10'; }\n  function PublicTree() {",
            1,
        )
        .replace(
            "var me = this;\n\n      // 隐藏的栏目",
            "var me = this; mutate(me);\n\n      // 隐藏的栏目",
            1,
        )
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_local_function_cannot_mutate_state():
    page = fixture("jx_page.html").replace(
        "var me = this;\n\n      // 隐藏的栏目",
        "var me = this; function mutate() { me.listParams.pageSize = '10'; } mutate();\n\n      // 隐藏的栏目",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_local_function_cannot_mutate_argument():
    page = fixture("jx_page.html").replace(
        "var me = this;\n\n      // 隐藏的栏目",
        "var me = this; function mutate(o) { o.pageSize = '10'; } mutate(me.listParams);\n\n      // 隐藏的栏目",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_top_level_var_catalogs_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace("</body>", "<script>var catalogs = {};</script></body>")
    assert find_lonsun_api(page, BASE, BASE) is None


def test_duplicate_top_level_tree_declaration_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>", "<script>var _organTree2 = null;</script></body>"
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_top_level_immediately_invoked_function_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>", "<script>(function(){ catalogs[6991] = []; })();</script></body>"
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_pre_method_object_define_properties_rejected():
    page = fixture("jx_page.html").replace(
        "preprocessData: function () {",
        "preprocessData: function () { Object.defineProperties(this.listParams, {pageSize: {value: '10'}});",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_init_done_method_cannot_mutate_state():
    page = (
        fixture("jx_page.html")
        .replace(
            "preprocessData: function () {",
            "mutate: function () { this.listParams.pageSize = '10'; return foo(); },\n    preprocessData: function () {",
            1,
        )
        .replace(
            "me.getData();\n      // me.getReportyear",
            "me.mutate().done(function () { me.getData(); });\n      // me.getReportyear",
            1,
        )
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_top_level_unrelated_window_property_allowed():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>",
        "<script>Object.defineProperties(window, {flag: {value: true}});</script></body>",
    )
    assert find_lonsun_api(page, BASE, BASE) is not None


def test_ready_callback_cannot_replace_catalog():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "<body>", "<body><script>$(function(){catalogs[6991]=[];});</script>", 1
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_ready_callback_global_mutation_is_unknown():
    page = fixture("jx_page.html").replace(
        "<body>",
        "<body><script>var flag = false; $(function(){ flag = true; });</script>",
        1,
    )
    page = page.replace(
        "var url = '' + me.currNode.uri",
        "if (flag) { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_top_level_named_function_call_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>", "<script>function mutate(){catalogs[6991]=[];} mutate();</script></body>"
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_ready_callback_cannot_call_unknown_page_function():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "<body>",
        "<body><script>function mutate(){catalogs[6991]=[];} $(function(){mutate();});</script>",
        1,
    )
    assert find_lonsun_api(page, BASE, BASE) is None


@pytest.mark.parametrize(
    "call", ["window.mutate();", "window['mutate']();", "$(function(){window['mutate']();});"]
)
def test_window_page_function_calls_rejected(call):
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>", "<script>function mutate(){catalogs[6991]=[];} " + call + "</script></body>"
    )
    assert find_lonsun_api(page, BASE, BASE) is None


@pytest.mark.parametrize("target", ["mutate", "window.mutate", "window['mutate']"])
def test_ready_callback_cannot_call_page_function_alias(target):
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "<body>",
        "<body><script>function mutate(){catalogs[6991]=[];} var f="
        + target
        + "; $(function(){f();});</script>",
        1,
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_ready_callback_cannot_call_computed_function_alias():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "<body>",
        "<body><script>function mutate(){catalogs[6991]=[];} var f=window['mu'+'tate']; $(function(){f();});</script>",
        1,
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_top_level_window_anonymous_function_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>",
        "<script>window.foreign=function(){catalogs[6991]=[]}; window.foreign();</script></body>",
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_top_level_unknown_object_method_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>",
        "<script>document.foreign=function(){catalogs[6991]=[]}; document.foreign();</script></body>",
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_top_level_computed_object_method_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>",
        "<script>document.foreign=function(){catalogs[6991]=[]}; document['foreign']();</script></body>",
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_top_level_chained_computed_method_rejected():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "</body>",
        "<script>document.foreign=function(){catalogs[6991]=[]}; document['foreign']['call'](document);</script></body>",
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_wuhu_event_timer_allows_normal_event_registration():
    from youju.crawler.lonsun import find_lonsun_api

    url = PAGES["wuhu"] + "?type=4&catId=6732111&action=list"
    page = fixture("wuhu_page.html").replace(
        "JSON.parse(JSON.stringify(catalogs[me.organId]))", "catalogs[me.organId]", 1
    )
    assert find_lonsun_api(page, url, url) is not None


def test_pre_method_unknown_document_method_rejected():
    page = (
        fixture("jx_page.html")
        .replace(
            "<body>",
            "<body><script>document.foreign=function(){document.body.innerHTML='x'};</script>",
            1,
        )
        .replace(
            "preprocessData: function () {",
            "preprocessData: function () { document.foreign();",
            1,
        )
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_unknown_global_object_method_rejected():
    page = (
        fixture("jx_page.html")
        .replace(
            "<body>",
            "<body><script>var evil={mutate:function(){document.body.innerHTML='x';}};</script>",
            1,
        )
        .replace(
            "preprocessData: function () {",
            "preprocessData: function () { evil.mutate();",
            1,
        )
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_undeclared_object_call_rejected():
    page = fixture("jx_page.html").replace(
        "preprocessData: function () {",
        "preprocessData: function () { externalObject.mutate();",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_pre_method_undeclared_object_alias_call_rejected():
    page = fixture("jx_page.html").replace(
        "preprocessData: function () {",
        "preprocessData: function () { var alias = externalObject; alias.mutate();",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_catalog_duplicates_host_path_and_query():
    from youju.crawler.lonsun import find_lonsun_api, lonsun_unrecognized_reason

    page = fixture("jx_page.html")
    src = '<script type="text/javascript" src="/49631981/6991.js"></script>'
    same = page.replace(src, src + '<script src="/49631981/6991.js?v=85"></script>')
    assert find_lonsun_api(same, BASE, BASE).url.endswith("6991.js?v=85")
    different = page.replace(
        src, src + '<script src="https://www.ah.gov.cn/49631981/6991.js"></script>'
    )
    assert "主机不一样" in lonsun_unrecognized_reason(different, BASE, BASE)


def test_navigation_parameters_and_integer_keys():
    from youju.crawler.lonsun import find_lonsun_api

    for extra in ("&nav=", "&nav=123&sub=&isParent=true", "&isParent=false"):
        assert column_spec(BASE + extra)
        assert find_lonsun_api(fixture("jx_page.html"), BASE + extra, BASE + extra)
    for extra in ("&sub=1", "&isParent=1", "&nav=x"):
        assert column_spec(BASE + extra) is None
    page = fixture("jx_page.html").replace("me.listParams = {", 'me.listParams = {"1": "x",', 1)
    assert page != fixture("jx_page.html")
    assert find_lonsun_api(page, BASE, BASE) is None


def test_display_only_each_rel_info_and_unsafe_variants():
    page = fixture("jx_page.html")
    needle = "item.index = (me.listParams.pageIndex - 1) * me.listParams.pageSize + i + 1;"
    extra = """
      if (item.catId === 49948871 && item.relInfo) {
        var arr = item.relInfo.split("|");
        for (var k = 0; k < arr.length; k++) { var tm = arr[k]; if (tm.indexOf("说明") > -1) { item.relInfo = item.relInfo.replace(tm, tm.replace("文件下载", "说明")); } }
      }
    """
    assert not isinstance(
        resolve_fixture("jx", "49949451", page=page.replace(needle, needle + extra, 1)), str
    )
    for bad in ('item.title = "改";', "data.pop();", "item.relInfo = res.data;", 'arr.push("x");'):
        result = resolve_fixture(
            "jx", "49949451", page=page.replace(needle, needle + extra + bad, 1)
        )
        assert isinstance(result, str) and "另有处理" in result


def test_catalog_cache_blocks_other_trees_on_host():
    from youju.crawler.adapters import api_refusal, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import CatalogCache

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    for first in (
        FetchResult(tree.url, "http_error", http_status=403),
        FetchResult(tree.url, "robots_disallowed"),
        FetchResult(tree.url, "redirect"),
    ):
        calls = []

        def fetch(url, *, calls=calls, first=first, **kwargs):
            calls.append(url)
            return first

        cache = CatalogCache()
        cache.get(tree.url, fetch)
        second = cache.get("https://jx.ah.gov.cn/another/2.js", fetch)
        assert len(calls) == 1
        assert "同一轮已被拒绝" in api_refusal(tree, second)
        cache.get("https://other.ah.gov.cn/another/2.js", fetch)
        assert len(calls) == 2


def test_refusal_tree_query_and_list_url():
    from datetime import UTC, datetime

    from youju.crawler.service import _refused_before

    now = datetime.now(UTC)
    config = {
        "list_api_refused": {
            "url": "https://x.gov.cn/tree.js?v=1",
            "api_url": "https://x.gov.cn/api?a=1",
            "at": now.isoformat(),
            "reason": "拒绝",
        }
    }
    assert _refused_before(config, "https://x.gov.cn/tree.js?v=2", now, tree=True)
    assert not _refused_before(config, "https://x.gov.cn/new.js?v=2", now, tree=True)
    assert _refused_before(config, "https://x.gov.cn/api?a=1", now, list_url=True)
    assert not _refused_before(config, "https://x.gov.cn/api?a=2", now, list_url=True)


def test_spotcheck_list_failures_report_reason():
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.govsites.spotcheck import _first_list_item

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    api = api_follow(tree, FetchResult(tree.url, "ok", tree.url, 200, fixture("jx_catalog.js")))
    pages = {BASE: fixture("jx_page.html"), tree.url: fixture("jx_catalog.js")}
    for response, expected in (
        (FetchResult(api.url, "http_error", http_status=403), "不能抓"),
        (FetchResult(api.url, "network_error", error="网络错误"), "这次没取到"),
        ("invalid", "认不出"),
    ):
        reasons = []
        assert (
            _first_list_item(LonsunCache({**pages, api.url: response}), BASE, TODAY, reasons)
            is None
        )
        assert expected in reasons[0] and "列表接口" in reasons[0]


def test_spotcheck_existing_source_uses_city_fallback(monkeypatch):
    from types import SimpleNamespace

    from youju.govsites import spotcheck

    source = SimpleNamespace(id=1, url=BASE, province="安徽", city="芜湖", parser_config={})

    class Session:
        def scalars(self, query):
            return [source]

        def execute(self, query):
            return SimpleNamespace(all=lambda: [])

    hosts = []

    def first(cache, url, today, reasons, *, catalog_host=None):
        hosts.append(catalog_host)

    monkeypatch.setattr(spotcheck, "_first_list_item", first)
    spotcheck.evidence_for({"栏目网址": BASE}, Session(), None, TODAY)
    assert hosts == ["www.wuhu.gov.cn"]


def test_navigation_values_are_used_in_conditions():
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    url = BASE + "&nav=&sub=&isParent=false"
    page = fixture("jx_page.html").replace(
        "me.query.action === 'list'",
        "me.query.action === 'list' && me.query.nav === '' && me.query.sub === '' && me.query.isParent === 'false'",
        1,
    )
    assert page != fixture("jx_page.html")
    tree = find_lonsun_api(page, url, url)
    assert tree is not None
    assert not isinstance(resolve_lonsun(tree, fixture("jx_catalog.js")), str)


def test_each_response_parameter_is_allowed():
    page = fixture("jx_page.html")
    page = page.replace("var data = res.data;", "", 1).replace(
        "$.each(data, function (i, item)", "$.each(res, function (i, item)", 1
    )
    result = resolve_fixture("jx", "49949451", page=page)
    assert not isinstance(result, str)


def with_page_noise(page):
    before = r"""<script>
(function() { var serMgr = document.createElement("script"); serMgr.src = "https://service.lonsun.cn/assets/cloud/boot.min.js?sid=1"; var s = document.getElementsByTagName("script")[0]; s.parentNode.insertBefore(serMgr, s); })();
(function() { var wza = document.createElement("script"); wza.src = "https://wza.lonsun.cn/assets/v2/web.min.js?sid=1"; var s = document.getElementsByTagName("script")[0]; s.parentNode.insertBefore(wza, s); })();
var _hmt = _hmt || []; (function() { var hm = document.createElement("script"); hm.src = "https://example.com/hm.js"; var s = document.getElementsByTagName("script")[0]; s.parentNode.insertBefore(hm, s); })();
</script>"""
    after = r"""<script>
$(function () { if ($('div .xxgk_lmlink').length > 0) { $('.subNav').eq(1).addClass('active'); } $('.tab').click(function () { var me = this; me.listParams = {}; $(this).html('ok'); }); $('.item').each(function (i, el) { $(el).addClass('active'); }); $.get('/tab').done(function (html) { $('#tab_0_0').html(html); }); });
document.write(unescape("%3Cspan id='_ideConac' %3E%3C/span%3E%3Cscript src='https://dcs.conac.cn/js/CAN.js' type='text/javascript'%3E%3C/script%3E"));
</script>"""
    return page.replace("<body>", "<body>" + before, 1).replace("</body>", after + "</body>", 1)


@pytest.mark.parametrize(
    "name,cat_id",
    [
        ("jx", "49949451"),
        ("wuhu", "6732111"),
        ("huaibei", "14237535"),
        ("luan", "7024001"),
        ("chuzhou", "161734542"),
        ("huainan", "159421266"),
    ],
)
def test_real_page_noise_preserves_both_steps(name, cat_id):
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    page = fixture(f"{name}_page.html")
    if name == "luan":
        page = page.replace("var isSsbm = true;", "")
    url = PAGES[name] + f"?type=4&catId={cat_id}&action=list"
    original = find_lonsun_api(page, url, url)
    noisy = find_lonsun_api(with_page_noise(page), url, url)
    assert original is not None and noisy is not None
    assert (noisy.adapter, noisy.url, noisy.base_url) == (
        original.adapter,
        original.url,
        original.base_url,
    )
    assert resolve_lonsun(noisy, fixture(f"{name}_catalog.js")) == resolve_lonsun(
        original, fixture(f"{name}_catalog.js")
    )


@pytest.mark.parametrize(
    "injected",
    [
        "catalogs[6991] = [];",
        '$.ajaxSetup({headers: {x: "y"}});',
        "Ls.ajaxGet = function(){};",
        "$('.tab').click(function(){ PublicTree.prototype.open = function(){}; });",
        "jQuery.ajaxPrefilter(function(){});",
        "Ls.url.get = function(){};",
        "catalogs[6991].push({});",
        "delete catalogs[6991];",
        "_organTree2 = null;",
        "Object.defineProperty(window, 'catalogs', {value: {}});",
        "Object.assign(window, {PublicTree: function(){}});",
    ],
)
def test_page_noise_cannot_write_dependencies(injected):
    from youju.crawler.lonsun import find_lonsun_api

    page = with_page_noise(fixture("jx_page.html")).replace(
        "</body>", "<script>(function(){" + injected + "})();</script></body>", 1
    )
    assert find_lonsun_api(page, BASE, BASE) is None


@pytest.mark.parametrize("mutation", ["flag = true", "flag += 1", "window.flag = true"])
def test_event_callback_global_mutation_is_unknown(mutation):
    page = (
        fixture("jx_page.html")
        .replace(
            "<body>",
            "<body><script>var flag = false; $('.tab').click(function(){"
            + mutation
            + ";});</script>",
            1,
        )
        .replace(
            "var url = '' + me.currNode.uri",
            "if (flag) { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
            1,
        )
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)
