"""龙讯（安徽集约化平台）信息公开列表适配器的离线测试：认页面、受限求值、两步请求、返回护栏。"""

import json
from datetime import date
from pathlib import Path

import pytest
from lxml import html as lxml_html

from youju.crawler.lonsun import (
    LonsunFormatError,
    catalog_host_allowed,
    column_spec,
    find_lonsun_api,
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
    # ah.gov.cn 是省的域名：底下是省里各个部门的网站，不能互相取栏目树；同一主机照常
    assert not catalog_host_allowed("https://jx.ah.gov.cn/", "https://www.ah.gov.cn/a.js")
    assert not catalog_host_allowed("https://sthjt.zj.gov.cn/", "https://www.zj.gov.cn/a.js")
    assert catalog_host_allowed("https://jx.ah.gov.cn/", "https://jx.ah.gov.cn/a.js")
    assert catalog_host_allowed("https://a.gz.gov.cn/", "https://www.gz.gov.cn/a.js")  # 广州


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
        # 页面全局变量的值一律当不知道：只声明一次、没有别处改的也一样（第四轮）
        updated = fixture("luan_page.html").replace("var isSsbm = true;", "")
        assert dict(find_lonsun_api(updated, page, page).plan.globals)["isSsbm"] is UNKNOWN


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
    # 页面全局变量的值一律当不知道：把条件里的 isSsbm 换成字面值，测特殊分支本身
    page = fixture("luan_page.html").replace("(isSsbm && ", "(false && ", 1)
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
    changed = page.replace("//ids.push(el.id);", "ids.push(el.id);")
    assert changed != page
    result = resolve_fixture("jx", "49949431", page=changed)
    assert isinstance(result, str) and "hideIds" in result
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
        (page.replace(src, src + '<script src="/123/6991.js"></script>'), "地址不一样"),
        (page.replace("/site/label/8888", "/invalid"), "接口路径认不出"),
        (page.replace("function PublicTree()", "function PublicTree(x)", 1), "页面脚本认不出"),
    ]
    for changed, reason in cases:
        assert reason in lonsun_unrecognized_reason(changed, BASE, BASE)
    assert (
        lonsun_unrecognized_reason('<script>var x = "function PublicTree(";</script>', BASE, BASE)
        is None
    )


_READY_OK = '{"status":true,"desc":"成功","data":"6732111,6732121"}'
WUHU = PAGES["wuhu"] + "?type=4&catId=6732121&action=list"
_WUHU_READY = "https://czgz.wuhu.gov.cn/public/getPublicQcsmCatIds"


def test_wuhu_list_waits_for_ready_request():
    from youju.crawler.adapters import api_parts
    from youju.crawler.lonsun import LonsunReadyPlan, LonsunRequestPlan

    api = resolve_fixture("wuhu", "6732121")
    ready, listing = api_parts(api)
    # 浏览器等 getPublicQcsmCatIds 成功才发列表请求：先发它（同一网站、页面写明的路径，平台加的
    # 防缓存随机数不加），列表请求不变
    assert ready.url == _WUHU_READY and isinstance(ready.plan, LonsunReadyPlan)
    assert listing.url == api.url and isinstance(listing.plan, LonsunRequestPlan)
    # 省经信厅这类 getData 同步跑的：没有前面那个请求
    assert api_parts(resolve_fixture("jx", "49949451")) == (resolve_fixture("jx", "49949451"),)


@pytest.mark.parametrize(
    "old,new",
    [
        ("url: '/public/getPublicQcsmCatIds',", "url: me.readyUrl,"),
        ("url: '/public/getPublicQcsmCatIds',", "url: '/public/x?a=1',"),
        (
            "var data = res.data;\n                    publicQcsmCatIds",
            "var data = res.data || '';\n                    publicQcsmCatIds",
        ),
        ("item && publicQcsmCatIds.push(item);", "item && publicQcsmCatIds.push(+item);"),
        ("return Ls.ajaxGet({", "me.x = 1; return Ls.ajaxGet({"),
    ],
)
def test_ready_request_unknown_shape_refused(old, new):
    page = fixture("wuhu_page.html")
    assert page.count(old) == 1, old
    reasons = []
    changed = page.replace(old, new, 1)
    assert find_lonsun_api(changed, WUHU, WUHU, _diagnosis=reasons) is None
    assert reasons and ("那个请求的写法认不出" in reasons[0] or "认不出" in reasons[0]), reasons


@pytest.mark.parametrize(
    "text,ok",
    [
        (_READY_OK, True),
        ('{"status":false}', True),
        ('{"status":0,"data":null}', True),
        ('{"status":"","data":5}', True),
        ("[]", True),
        ('"x"', True),
        ("\ufeff" + _READY_OK, True),
        ("\ufeff\ufeff" + _READY_OK, False),  # 浏览器只去掉开头一个 BOM
        # 浏览器的 done 回调会出错、或者根本不跑：不发列表请求
        ("null", False),
        ('{"status":true}', False),
        ('{"status":1,"data":5}', False),
        ('{"status":"ok","data":[]}', False),
        ('{"status":{},"data":null}', False),
        ("NaN", False),
        ('{"status":NaN}', False),
        ("", False),
        ("<html>系统维护</html>", False),
        ("{bad", False),
    ],
)
def test_ready_response_judged_like_browser(text, ok):
    from youju.crawler.adapters import lonsun_ready_ok

    assert lonsun_ready_ok(text) is ok


@pytest.mark.parametrize(
    "reply,expected,words",
    [
        ("denied", "BLOCKED", "前置接口：HTTP 403"),
        ("gate", "BLOCKED", "前置接口"),
        ("odd", "DOWN", "前置接口：返回读不出"),
        ("down", "DOWN", "前置接口"),
    ],
)
def test_ready_request_failure_means_no_list_request(reply, expected, words):
    from youju.crawler.fetcher import FetchResult
    from youju.govsites import verify

    api = resolve_fixture("wuhu", "6732121")
    tree = find_lonsun_api(fixture("wuhu_page.html"), WUHU, WUHU).url
    responses = {
        "denied": FetchResult(_WUHU_READY, "http_error", _WUHU_READY, 403),
        "gate": FetchResult(
            _WUHU_READY, "ok", _WUHU_READY, 200, "<html><title>安全验证</title>请输入验证码</html>"
        ),
        "odd": '{"status":true}',
        "down": FetchResult(_WUHU_READY, "http_error", _WUHU_READY, 502),
    }
    cache = LonsunCache(
        {
            WUHU: fixture("wuhu_page.html"),
            tree: fixture("wuhu_catalog.js"),
            _WUHU_READY: responses[reply],
            api.url: fixture("wuhu_api.json"),
        }
    )
    col = lonsun_column(WUHU)
    col.city, col.level = "芜湖", "市级"
    checked = verify.check_one(cache, col, TODAY)
    assert checked.result == getattr(verify, expected) and words in checked.reason, checked.reason
    # 浏览器那边前面的请求没成功就不发列表请求：我们也不发
    assert api.url not in [u for u, _ in cache.calls]


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
    ready = "https://www.yjq.gov.cn/public/getPublicQcsmCatIds"
    cache = LonsunCache(
        {
            url: html,
            tree.url: fixture("wuhu_catalog.js"),
            ready: _READY_OK,
            api.url: fixture("wuhu_api.json"),
        }
    )
    assert check_one(cache, col, TODAY).result == READABLE
    # 先发 getData 等着的那个请求，成功了才发列表请求
    assert [u for u, _ in cache.calls][-2:] == [ready, api.url]


def test_special_chain_reason_uses_source():
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult

    url = PAGES["luan"] + "?type=4&catId=7024001&action=list"
    html = fixture("luan_page.html").replace("(isSsbm && ", "(true && ", 1)
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
        # 第一个参数是深拷贝标志，目标是第二个；$.merge 把后面的数组并进第一个
        "$.extend(true, me.listParams, {pageSize: '10'});",
        "$.extend(!0, me.listParams, {pageSize: '10'});",
        "$.merge(me.hideIds, [1]);",
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
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "var url = '' + me.currNode.uri",
        "if (" + condition + ") { me.zdlyxxgkList(); }\n      var url = '' + me.currNode.uri",
        1,
    )
    url = PAGES["jx"] + "?type=4&catId=49949451&action=list"
    if condition in ("Ls.missing", "_organTree2.missing", "me.topLevelParentId"):
        # 读 Ls、栏目树对象上不认识的成员，别处用到 topLevelParentId：认页面这一步就不认
        assert find_lonsun_api(page, url, url) is None
        return
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
    "write",
    ["", "window.flag = true;", "self.flag = true;", "this.flag = true;", "flag++;", "flag = 1;"],
)
def test_page_global_values_never_inferred(write):
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    # 页面全局变量的值一律当不知道：初值是假、没有别处改的也一样；条件用到它就不读，原因写明名字
    page = fixture("jx_page.html").replace(
        "</body>", "<script>var flag = false; " + write + "</script></body>"
    )
    page = page.replace(
        "var url = '' + me.currNode.uri",
        "if (flag) { me.zdlyxxgkList(); } var url = '' + me.currNode.uri",
        1,
    )
    api = find_lonsun_api(page, BASE, BASE)
    if write:
        assert api is None or isinstance(resolve_lonsun(api, fixture("jx_catalog.js")), str)
        return
    assert api is not None
    reason = resolve_lonsun(api, fixture("jx_catalog.js"))
    assert isinstance(reason, str) and "flag" in reason and "无法确定" in reason, reason


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
    # getData 等着的方法不是真实页面那种写法：认页面这一步就不读
    assert _refused(page)


def test_top_level_window_define_refused():
    from youju.crawler.lonsun import find_lonsun_api

    # 整个改 window（defineProperties 一次定义好几个全局）：一律不认，不再逐个看定义了哪些（第四轮）
    page = fixture("jx_page.html").replace(
        "</body>",
        "<script>Object.defineProperties(window, {flag: {value: true}});</script></body>",
    )
    assert find_lonsun_api(page, BASE, BASE) is None


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
    # 同一个地址写了两次：一样，照常认
    twice = page.replace(src, src + "\n" + src, 1)
    assert find_lonsun_api(twice, BASE, BASE).url == "https://jx.ah.gov.cn/49631981/6991.js"
    # 同一路径查询串不一样、或者主机不一样：返回可能不同，浏览器几份都会执行，说不清用哪份
    variant = page.replace(src, src + '<script src="/49631981/6991.js?v=85"></script>', 1)
    assert find_lonsun_api(variant, BASE, BASE) is None
    assert "地址不一样" in lonsun_unrecognized_reason(variant, BASE, BASE)
    url = PAGES["wuhu"] + "?type=4&catId=6732121&action=list"
    wuhu = fixture("wuhu_page.html")
    src = "https://www.wuhu.gov.cn/openness/6787891/6596381.js?v=1558"
    other = src.replace("www.", "xxgk.")
    assert catalog_host_allowed(url, other)
    variant = wuhu.replace(
        f'src="{src}"></script>', f'src="{src}"></script><script src="{other}"></script>', 1
    )
    assert variant != wuhu
    assert find_lonsun_api(variant, url, url) is None
    assert "地址不一样" in lonsun_unrecognized_reason(variant, url, url)


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
        FetchResult(tree.url, "http_error", http_status=429),
        FetchResult(tree.url, "redirect", final_url="https://jx.ah.gov.cn/login"),
    ):
        calls = []

        def fetch(url, *, calls=calls, first=first, **kwargs):
            calls.append(url)
            return first

        cache = CatalogCache()
        cache.get(tree.url, fetch)
        second = cache.get("https://jx.ah.gov.cn/another/2.js", fetch)
        assert len(calls) == 1
        # 同一主机别的脚本这次不去：它自己没被拒绝，记"这次没取到"，不写不能抓的记录
        assert second.status == "site_paused" and api_refusal(tree, second) == ""
        cache.get("https://other.ah.gov.cn/another/2.js", fetch)
        assert len(calls) == 2


def test_catalog_cache_robots_does_not_block_host():
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import CatalogCache

    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return FetchResult(url, "robots_disallowed")

    # robots 不许只是那个路径不读：同一主机别的地址各按 robots 判，不算主机拒绝了我们
    cache = CatalogCache()
    cache.get("https://jx.ah.gov.cn/1/2.js", fetch)
    cache.get("https://jx.ah.gov.cn/3/4.js", fetch)
    assert len(calls) == 2 and not cache.blocked("https://jx.ah.gov.cn/x")


def test_catalog_cache_protocol_only_redirect_does_not_block():
    from youju.crawler.adapters import find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import CatalogCache

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return FetchResult(url, "redirect", final_url=url.replace("https://", "http://"))

    cache = CatalogCache()
    cache.get(tree.url, fetch)
    cache.get("https://jx.ah.gov.cn/another/2.js", fetch)
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

    source = SimpleNamespace(
        id=1, url=BASE, level="区县级", province="安徽", city="芜湖", parser_config={}
    )

    class Session:
        def scalars(self, query):
            return [source]

        def execute(self, query):
            return SimpleNamespace(all=lambda: [])

    hosts = []

    def first(cache, url, today, reasons, *, catalog_host=None, county=False):
        hosts.append((catalog_host, county))

    monkeypatch.setattr(spotcheck, "_first_list_item", first)
    spotcheck.evidence_for({"栏目网址": BASE}, Session(), None, TODAY)
    assert hosts == [("www.wuhu.gov.cn", True)]


def _spotcheck_session(source, rows=()):
    from types import SimpleNamespace

    class Session:
        def scalars(self, query):
            return [source]

        def execute(self, query):
            return SimpleNamespace(all=lambda: list(rows))

    return Session()


def test_spotcheck_respects_lonsun_hold():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from youju.crawler.roundguard import HostRoundGuard
    from youju.govsites import spotcheck

    note = {
        "url": "https://jx.ah.gov.cn/49631981/6991.js",
        "reason": "列表接口：HTTP 403：拒绝访问或要求登录",
        "at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }
    source = SimpleNamespace(
        id=1,
        url=BASE,
        level="省级",
        province="安徽",
        city="",
        parser_config={"list_api_open": "龙讯public", "list_api_refused": note},
    )
    doc = "https://jx.ah.gov.cn/art/2026/11/1/art_1.html"
    cache = LonsunCache({BASE: fixture("jx_page.html"), doc: "<html>x</html>"})
    cache.round_guard = HostRoundGuard()
    rows = [("关于开展申报的通知", doc, TODAY)]
    ev = spotcheck.evidence_for({"栏目网址": BASE}, _spotcheck_session(source, rows), cache, TODAY)
    # 经龙讯接口读的来源 30 天内被拦下过：抽查也一个请求都不发
    assert "暂不再请求" in ev["打开"] and ev["程序结论"] != spotcheck.OK
    assert cache.calls == []


def test_spotcheck_evidence_goes_through_round_guard():
    from types import SimpleNamespace

    from youju.crawler.roundguard import HostRoundGuard
    from youju.govsites import spotcheck

    source = SimpleNamespace(
        id=1, url=BASE, level="省级", province="安徽", city="", parser_config={}
    )
    doc = "https://jx.ah.gov.cn/art/2026/11/1/art_1.html"
    cache = LonsunCache({BASE: fixture("jx_page.html"), doc: "<html>x</html>"})
    cache.round_guard = HostRoundGuard()
    cache.round_guard.block(BASE)  # 同一轮这个主机拒绝过我们（别的栏目被拒）
    rows = [("关于开展申报的通知", doc, TODAY)]
    ev = spotcheck.evidence_for({"栏目网址": BASE}, _spotcheck_session(source, rows), cache, TODAY)
    # 库里有文件也不去这个网站读原文、栏目页：记这次没取到
    assert ev["打开"].startswith("这次没取到") and cache.calls == []


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
        page = page.replace("(isSsbm && ", "(false && ", 1)
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


# ---- 第二轮审阅：实例化的位置、别名逃逸、栏目树地址唯一 ----

_CREATE = "$(function () {\n    new PublicTree();\n  })"


def _jx(old=_CREATE, new=None, extra=""):
    page = fixture("jx_page.html")
    if new is not None:
        assert old in page
        page = page.replace(old, new, 1)
    if extra:
        assert "<body>" in page
        page = page.replace("<body>", "<body>" + extra, 1)
    return page


def _found(page):
    from youju.crawler.lonsun import find_lonsun_api

    return find_lonsun_api(page, BASE, BASE)


def test_public_tree_created_on_load_only():
    assert _found(_jx()) is not None
    # 就绪回调的另外两种写法照样认
    assert _found(_jx(new="$(document).ready(function () { new PublicTree(); });")) is not None
    assert _found(_jx(new="jQuery(function () { new PublicTree(); })")) is not None
    # 写在不会执行或说不准执行的位置：浏览器不会创建它，也就不发列表请求，我们也不能发
    for wrapped in (
        "$(function () { if (false) { new PublicTree(); } })",
        "if (false) { new PublicTree(); }",
        "$('#go').click(function () { new PublicTree(); })",
        "function later() { new PublicTree(); }",
        "$(function () { prepare(); new PublicTree(); })",
    ):
        assert _found(_jx(new=wrapped)) is None, wrapped
    # 创建了两次：说不清列表请求发了几回，认不出
    assert _found(_jx(new=_CREATE + "\n  $(function () { new PublicTree(); })")) is None


@pytest.mark.parametrize(
    "script",
    [
        "var x = Ls; x.ajaxGet = function () {};",
        "(function ($) { $.ajax = function () {}; })(jQuery);",
        "var j = jQuery; j.ajaxSetup({headers: {token: '1'}});",
        "jQuery.ajaxTransport('json', function () {});",
        "function keep(c) { return c; } keep(catalogs);",
        "var tree = PublicTree; tree.prototype.open = function () {};",
        "window.copy = _organTree2;",
    ],
)
def test_alias_escapes_are_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None


def test_harmless_same_names_still_recognized():
    # 别的对象上同名的属性不是这几个全局；普通的 .ajax 调用（不是改写）也照常
    script = "var obj = {}; obj.Ls = 1; obj.catalogs = []; $.ajax({url: '/x'});"
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None


WUHU = "https://czgz.wuhu.gov.cn/public/column/6596381?type=4&catId=6732121&action=list"


def _wuhu(old=None, new=None):
    page = fixture("wuhu_page.html")
    if old is not None:
        assert page.count(old) == 1
        page = page.replace(old, new, 1)
    return page


def _wuhu_found(page):
    from youju.crawler.lonsun import find_lonsun_api

    return find_lonsun_api(page, WUHU, WUHU)


def test_search_widget_on_tree_recognized_only_as_checked():
    # 芜湖把栏目树交给 zTree 模糊搜索插件：加载时只挂搜索函数，照常认
    assert _wuhu_found(_wuhu()) is not None
    hook = "zTreeObj.searchNodeLazy = searchNodeLazy;"
    for variant in (
        # 加载时就跑过滤（会隐藏、改名节点）
        "ztreeFilter(zTreeObj, ''); " + hook,
        # 把栏目树的查找函数换掉
        "zTreeObj.getNodeByParam = searchNodeLazy;",
        # 当场改栏目树结构
        "zTreeObj.removeNode(zTreeObj.getNodes()[0]); " + hook,
        # 加载时排一个延时任务
        "setTimeout(function () { ztreeFilter(zTreeObj, 'x'); }, 0); " + hook,
    ):
        assert _wuhu_found(_wuhu(hook, variant)) is None, variant
    # 栏目树交给别的方法：没核对过它做什么，不认
    call = "me.fuzzySearch(_organTree2);"
    assert _wuhu_found(_wuhu(call, "me.setSelect(_organTree2);")) is None


@pytest.mark.parametrize(
    "script",
    [
        # 读出 Ls 的成员存起来再改（别名）
        "var u = Ls.url; u.get = function () { return '1'; };",
        # 经 window、下标访问这几个全局
        "window.Ls.ajaxGet = function () {};",
        "window['catalogs'] = {};",
        "var t = window.PublicTree;",
        "self.Ls.ajaxGet = function () {};",
        "top['catalogs'] = {};",
        "var t = parent.PublicTree;",
        "frames.catalogs = {};",
        "globalThis.Ls.ajaxGet = function () {};",
        # 整个换掉，或者改我们照着推请求的成员
        "Ls = {};",
        "Ls.url.get = function () { return ''; };",
        "Ls['ajax' + 'Get'] = function () {};",
        # 栏目树对象：改结构的方法、别的脚本拿它
        "$(function () { _organTree2.removeNode(_organTree2.getNodes()[0]); });",
        "var t2 = _organTree2;",
        # catalogs 只认 PublicTree 脚本里的 catalogs[me.organId]
        "var c = catalogs['6991']; c.push({});",
        # PublicTree 的原型读出来再改
        "var proto = PublicTree.prototype; proto.open = function () {};",
    ],
)
def test_global_uses_outside_whitelist_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None


def test_harmless_global_uses_recognized():
    # 给 Ls 加个新函数、调用平台的普通函数、栏目树上只查找的方法：照常认
    script = (
        "Ls.myHelper = function (id) { return id; };"
        " Ls.tipsInfo('x');"
        " function later() { var n = _organTree2.getNodes(); $('#c').text(n.length); }"
    )
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None


def test_catalog_passed_only_to_read_only_method():
    anchor = "var currentNode = $.grep(nodes, function(node) {"
    assert _found(_jx()) is not None
    for change in (
        "nodes.push({}); ",
        "nodes[0].children = []; ",
        "delete nodes[0].children; ",
        "me.other(nodes); ",
        "keep(nodes); ",
    ):
        page = fixture("jx_page.html").replace(anchor, change + anchor, 1)
        assert page != fixture("jx_page.html")
        assert _found(page) is None, change


# ---- PublicTree 以外的脚本：可以读那几个全局对象，不能改（含经别名、经页面函数改）----

_READ_ONLY_SCRIPTS = [
    # 真实页面上常见的：判断浏览器、按名字找栏目跳转、从栏目树里挑下级编号
    "if (Ls.browser.isMobile) { $('.x').hide(); }",
    "function js() { var arr = catalogs['6991']; for (var i = 0; i < arr.length; i++) {"
    " if (arr[i].name == 'x') { location.href = '/a?catId=' + arr[i].id; } } }",
    "function pick(list, ids) { $.each(list, function (i, el) {"
    " if (ids.indexOf(el.parentId) > -1) { ids += ',' + el.id; } }); return ids; }"
    " function search(ids) { var all = catalogs['6991']; ids = pick(all, ids); go(ids); }",
    "function name(id) { var n = _organTree2.getNodeByParam('id', id, null); return n && n.name; }",
]


@pytest.mark.parametrize("script", _READ_ONLY_SCRIPTS)
def test_other_scripts_may_read_globals(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None


@pytest.mark.parametrize(
    "script",
    [
        # 读出来存进变量（别名）再改
        "function fix() { var arr = catalogs['6991']; arr[0].children = []; }",
        "var c = catalogs['6991']; $.extend(c[0], {children: []});",
        "var n = _organTree2.getNodeByParam('id', 1, null); n.children = null;",
        # 遍历时在回调里改
        "$.each(catalogs['6991'], function (i, n) { n.parentId = 0; });",
        "catalogs['6991'].forEach(function (n) { delete n.children; });",
        # 交给会改的页面函数、看不出改不改的函数
        "function grow(list) { list.push({id: 1}); } grow(catalogs['6991']);",
        "function tree(data, pid) { var obj = data[0]; obj.children = []; return [obj]; }"
        " var t = tree(catalogs['6991'], 0);",
        "someLib.process(catalogs['6991']);",
        "lookup(catalogs['6991']);",
        # 栏目树对象整个当值传
        "function s() { var treeObj = _organTree2; return treeObj.getNodes(); }",
    ],
)
def test_other_scripts_may_not_change_globals(script):
    from youju.crawler.lonsun import lonsun_unrecognized_reason

    page = _jx(extra=f"<script>{script}</script>")
    assert _found(page) is None
    assert lonsun_unrecognized_reason(page, BASE, BASE).startswith("内联脚本")


@pytest.mark.parametrize(
    "script",
    [
        # 当返回值交出去，调用方再改
        "function cat() { return catalogs['6991']; } cat().push({});",
        # $.grep、filter 交回来的还是原来的节点
        "var hit = $.grep(catalogs['6991'], function (n) { return n.id; }); hit[0].children = [];",
        "var some = catalogs['6991'].filter(function (n) { return n.id; }); some[0].parentId = 0;",
        # 存进对象属性、数组元素
        "var box = {}; box.list = catalogs['6991'];",
        "var keep = []; keep[0] = catalogs['6991'];",
        # push 进数组再改
        "var bag = []; bag.push(catalogs['6991'][0]); bag[0].children = [];",
        # 页面函数一层层往下交，最后一层改
        "function a1(x) { a2(x); } function a2(y) { y[0].children = []; } a1(catalogs['6991']);",
        # 用 arguments 取参数
        "function g() { arguments[0].push({}); } g(catalogs['6991']);",
    ],
)
def test_other_scripts_escape_routes_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None


def test_other_scripts_page_function_returning_plain_value_ok():
    # 淮南的写法：页面函数收到栏目数据只读，交回来的是编号串，不是栏目数据
    script = (
        "function pick(list, ids) { $.each(list, function (i, el) {"
        " if (ids.indexOf(el.parentId) > -1) { ids += ',' + el.id; } }); return ids; }"
        " function run(ids) { var all = catalogs['6991']; ids = pick(all, ids); show(ids); }"
        " function show(text) { $('#x').text(text); }"
    )
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None


def test_list_request_with_unknown_parameter_not_sent():
    # 令牌、签名这类参数名不在认得的名单里：不发，待人工处理
    for extra in ("accessToken: 'x',", "sign: 'abc',"):
        page = fixture("jx_page.html").replace("me.listParams = {", "me.listParams = {" + extra, 1)
        assert page != fixture("jx_page.html")
        reason = resolve_fixture("jx", "49949451", page=page)
        assert isinstance(reason, str) and "认不出的参数" in reason, extra


@pytest.mark.parametrize(
    "body,refused",
    [
        # 防护页、挑战页：不能抓
        ("<html><script>var yunsuo_session_verify='1';</script></html>", "云锁"),
        ("<html><script>$_ts=window['$_ts'];</script></html>", "瑞数"),
        # 回的是网页或 JSON，不是脚本（比如软 404）：不能抓
        ("<!DOCTYPE html><html><body><p>页面不存在</p></body></html>", "不是脚本"),
        ('{"code": 404}', "不是脚本"),
        # 还是脚本、只是格式变了：待人工处理（不算拒绝）
        ("var catalogs = {'other': []};", None),
    ],
)
def test_catalog_refusal_on_challenge_or_non_script(body, refused):
    from youju.crawler.adapters import api_refusal, find_list_api
    from youju.crawler.fetcher import FetchResult

    api = find_list_api(fixture("jx_page.html"), BASE, BASE)
    reason = api_refusal(api, FetchResult(api.url, "ok", api.url, 200, text=body))
    if refused is None:
        assert reason == ""
    else:
        assert reason.startswith("栏目树脚本：") and refused in reason


def test_catalog_cache_blocks_host_for_round():
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import CatalogCache

    calls = []

    def fetch(url, same_address_only=False):
        calls.append(url)
        return FetchResult(url, "http_error", url, 429)

    cache = CatalogCache()
    first = cache.get("https://www.example.gov.cn/1/2.js", fetch)
    assert first.http_status == 429
    # 回了 429：这一轮同一主机的别的脚本不再去
    second = cache.get("https://www.example.gov.cn/3/4.js", fetch)
    assert second.status == "site_paused" and calls == ["https://www.example.gov.cn/1/2.js"]
    cache.block("https://other.example.gov.cn/x.js")
    assert cache.blocked("https://other.example.gov.cn/y.js")


def test_page_with_base_element_not_read():
    from youju.crawler.lonsun import lonsun_unrecognized_reason

    page = fixture("jx_page.html").replace(
        "<head>", "<head><base href='https://other.ah.gov.cn/'>", 1
    )
    assert page != fixture("jx_page.html")
    assert _found(page) is None
    assert "<base>" in lonsun_unrecognized_reason(page, BASE, BASE)


# ---- 第三轮审阅：加载时会跑的回调、跳过的显示方法、建树设置和节点字段、更多全局改写 ----

_PRE = "preprocessData: function () {"


@pytest.mark.parametrize(
    "callback",
    [
        "me.listParams.type = '5';",
        "me.currCatId = '1';",
        "me.refresh = true;",
        "me.hideIds.push(49949451);",
        "$.extend(el, {parentId: 0});",
        "Object.assign(el, {parentId: 0});",
    ],
)
def test_load_time_callbacks_cannot_change_state(callback):
    # $.each 的回调在加载时就跑：在里面改依赖状态、改节点，浏览器发的请求就变了
    page = fixture("jx_page.html").replace(
        _PRE, _PRE + "\n      $.each(me.ztreeData, function (i, el) { " + callback + " });", 1
    )
    assert page != fixture("jx_page.html")
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


@pytest.mark.parametrize("line", ["me.listParams.type = '5';", "me.getList();"])
def test_skipped_ui_methods_must_not_touch_state(line):
    anchor = "getNav: function () {"
    page = fixture("jx_page.html").replace(anchor, anchor + "\n      " + line, 1)
    assert page != fixture("jx_page.html")
    reason = resolve_fixture("jx", "49949451", page=page)
    assert isinstance(reason, str) and "getNav" in reason


@pytest.mark.parametrize(
    "old,new",
    [
        ("pIdKey: 'parentId'", "pIdKey: 'pid'"),
        ("title: '_name'", "title: '_name', children: 'nodes'"),
        ("pIdKey: 'parentId'", "pIdKey: 'parentId', enable: false"),
    ],
)
def test_tree_settings_must_match(old, new):
    page = fixture("jx_page.html").replace(old, new, 1)
    assert page != fixture("jx_page.html")
    reason = resolve_fixture("jx", "49949451", page=page)
    assert isinstance(reason, str) and "建树设置" in reason


@pytest.mark.parametrize("change", ["children", "string_parent", "zero_parent"])
def test_catalog_node_fields_must_be_plain(change):
    import json as json_lib

    text = fixture("jx_catalog.js")
    prefix, payload = text.split("=", 1)
    data = json_lib.loads(payload.strip().rstrip(";").replace("{6991:", '{"6991":', 1))
    node = data["6991"][0]
    if change == "children":
        node["children"] = []
    elif change == "string_parent":
        node["parentId"] = str(node["parentId"])
    else:
        node["parentId"] = "0" + str(node["parentId"])
    bad = prefix + "= {6991:" + json_lib.dumps(data["6991"], ensure_ascii=False) + "};"
    assert isinstance(resolve_fixture("jx", "49949451", catalog=bad), str)


@pytest.mark.parametrize(
    "script",
    [
        "GLOBAL_CONTEXTPATH = '/x';",
        "$(document).ajaxSend(function (e, x, s) { s.url = '/x'; });",
        "$.ajaxSettings.beforeSend = function () {};",
        "$.fn.zTree.init = function () {};",
    ],
)
def test_more_global_request_changes_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None


def test_truncated_get_data_does_not_raise():
    from youju.crawler.adapters import find_list_api
    from youju.crawler.lonsun import lonsun_unrecognized_reason

    anchor = "        me.open();\n      }\n    },\n    getReportyear"
    page = fixture("jx_page.html").replace(
        anchor, "        me.open();\n      }\n      if (me.x)\n    },\n    getReportyear", 1
    )
    assert page != fixture("jx_page.html")
    # 坏页面、截断的页面（getData 末尾悬空一个 if）：哪一步都不能抛出去，第二步认不出、不读
    find_list_api(page, BASE, BASE)
    lonsun_unrecognized_reason(page, BASE, BASE)
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)
    # 整段脚本在语句中间结束
    cut = fixture("jx_page.html").replace("me.currNode = node;", "if (me.x", 1)
    assert find_list_api(cut, BASE, BASE) is None


def test_encoded_query_and_odd_numbers_not_read():
    from youju.crawler.lonsun import column_spec

    # 平台按 location.search 原文取参数，编码过的、带加号的认不准
    assert column_spec(BASE.replace("catId=", "%63atId=")) is None
    assert column_spec(BASE + "&nav=1+2") is None
    page = fixture("jx_page.html").replace("me.listParams = {", "me.listParams = {num: 010,", 1)
    assert page != fixture("jx_page.html")
    assert _found(page) is None or isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_deeply_nested_json_is_unrecognized():
    # 嵌套极深的 JSON（Python 解析会递归超限）：当认不出，不抛别的异常
    deep = "[" * 100000 + "]" * 100000
    with pytest.raises(LonsunFormatError):
        parse_catalog("var catalogs = {6991:" + deep + "}", "6991")
    for mode in ("list", "city_page"):
        with pytest.raises(LonsunFormatError):
            parse_response(deep, mode, BASE, TODAY)


def test_huge_number_literal_is_infinity():
    import math

    from youju.crawler.lonsun import _js_number

    # 和 JS 一样：超出浮点范围的十六进制数是 Infinity，不抛 OverflowError
    assert _js_number("0x" + "f" * 400) == math.inf
    assert _js_number("0x10") == 16.0


@pytest.mark.parametrize("obj", ["window", "self", "top", "parent", "frames", "globalThis"])
def test_tree_script_writes_through_global_objects_refused(obj):
    # PublicTree 自己的脚本里经 window、self、top 这类全局对象改依赖的全局：一样不认
    for write in (f"{obj}.catalogs = {{}};", f"{obj}.PublicTree = function () {{}};"):
        assert _found(_jx(new=write + "\n  " + _CREATE)) is None, write


def test_document_write_random_must_go_into_query():
    from youju.crawler.lonsun import find_lonsun_api

    url = PAGES["chuzhou"] + "?type=4&catId=161734542&action=list"
    page = fixture("chuzhou_page.html")
    assert find_lonsun_api(page, url, url) is not None
    # 随机数直接拼进路径：浏览器请求的地址每次都不一样，从不请求我们照写法去取的那个
    changed = page.replace("2681525.js?v=' + Math.random()", "2681525.js' + Math.random()", 1)
    assert changed != page
    assert find_lonsun_api(changed, url, url) is None


# ---- 第四轮：加载时的代码只许改认得的对象（me、本方法新建的空对象或数组、核对过的树一节点）----

# findParentId、setSelect 必须和模板一字不差（推算 setSelect 会不会出错要靠它们），往里插什么都
# 不认；测加载时的规则改往 bindEvt（也是 getData 之前跑的方法）里插
_BIND_TOP = "bindEvt: function () {\n      var me = this;"
_FIND_PARENT_TOP = _BIND_TOP


def _refused(page, cat_id="49949451"):
    """第一步就不认，或者第二步给出原因：都算没读。"""
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    url = PAGES["jx"] + f"?type=4&catId={cat_id}&action=list"
    api = find_lonsun_api(page, url, url)
    return api is None or isinstance(resolve_lonsun(api, fixture("jx_catalog.js")), str)


def _jx_find_parent(extra):
    """在 getData 之前跑的方法里遍历栏目数据，回调里插 extra（item 是栏目节点）。"""
    page = fixture("jx_page.html")
    assert page.count(_BIND_TOP) == 1
    loop = (
        " var idMap = {}; $.each(me.ztreeData2, function (i, item) { idMap[item.id] = true; "
        + extra
        + " });"
    )
    return page.replace(_BIND_TOP, _BIND_TOP + loop, 1)


@pytest.mark.parametrize(
    "extra",
    [
        # 遍历栏目数据时改节点：浏览器里 49949451 就有了下级，发的是下级汇总
        "if (item.id === 49949461) { item.parentId = 49949451; }",
        "item.children = [];",
        "item.uri = 'https://x.example/';",
        "item['parentId'] = 1;",
        "$.extend(item, {parentId: 1});",
        "delete item.parentId;",
        # 经别名拿到的 me 或它的字段
        "var that = me; that.currCatId = '49949431';",
        "var p = me['listParams']; p.pageSize = '100';",
        "var p = (me.listParams); p.pageSize = '100';",
        "$.extend(true, me.listParams, {pageSize: '100'});",
        # 本方法新建的对象里存了节点、再经它改：不只加一层属性的不认
        "idMap[item.id] = item; idMap[49949461].parentId = 1;",
        # 树二的节点经查找方法拿到再改
        "var n = _organTree2.getNodeByParam('id', 1, null); n.isParent = true;",
    ],
)
def test_load_time_writes_to_unknown_objects_refused(extra):
    result = resolve_fixture("jx", "49949451", page=_jx_find_parent(extra))
    assert isinstance(result, str), result


@pytest.mark.parametrize(
    "extra",
    [
        "me.open = function () {};",
        "this.getData = function () {};",
        "me.getList = me.getNewList;",
        "me['getNewList'] = null;",
    ],
)
def test_overriding_public_tree_methods_refused(extra):
    # 实例属性会盖住原型方法：浏览器跑的就不是我们照着走的那个了
    result = resolve_fixture("jx", "49949451", page=_jx_find_parent(extra))
    assert isinstance(result, str) and "方法" in result, result


@pytest.mark.parametrize(
    "extra",
    ["var that = this;", "$.proxy(function () {}, me);", "me = {};", "var list = [me];"],
)
def test_me_as_value_refused(extra):
    page = fixture("jx_page.html")
    assert page.count(_FIND_PARENT_TOP) == 1
    result = resolve_fixture(
        "jx", "49949451", page=page.replace(_FIND_PARENT_TOP, _FIND_PARENT_TOP + " " + extra, 1)
    )
    assert isinstance(result, str), result
    # 回调里的 me 也是同一个；回调里的 this 不是 me，不管
    assert _refused(_jx_find_parent("return me;"))
    assert not _refused(_jx_find_parent("var el = this;"))


def test_constructor_alias_and_ui_method_writes_refused():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html")
    ctor = "function PublicTree() {\n    var me = this;"
    for extra in ("var that = this; that.currCatId = '49949431';", "var x = me; x.prefix = '/x';"):
        assert find_lonsun_api(page.replace(ctor, ctor + " " + extra, 1), BASE, BASE) is None
    # getData 以后跳过的显示方法（setMeta）里经别名改请求参数
    meta = "setMeta: function () {\n      var me = this;"
    assert meta in page
    for extra in ("var p = me.listParams; p.pageSize = '100';", "var n = me.currNode; n.id = 1;"):
        changed = page.replace(meta, meta + " " + extra, 1)
        assert isinstance(resolve_fixture("jx", "49949451", page=changed), str), extra


def test_user_event_handlers_may_write_but_triggering_on_load_refused():
    # 点了才跑的处理函数里改节点不碍事（加载时不跑）
    handler = "$('#x').on('click', function () { var n = _organTree2.getNodes()[0]; n.isParent = true; });"
    page = _jx_find_parent(handler)
    assert not isinstance(resolve_fixture("jx", "49949451", page=page), str)
    page = _jx_find_parent("$('#x').click(function () { item.parentId = 1; });")
    assert not isinstance(resolve_fixture("jx", "49949451", page=page), str)
    # 加载时替用户点一下：处理函数就在加载时跑了
    for trigger in ("$('#x').trigger('click');", "$('#x').click();", "$('#x').focus();"):
        assert isinstance(resolve_fixture("jx", "49949451", page=_jx_find_parent(trigger)), str)
    # focus、load 这类加载时就可能触发的事件不算"点了才跑"
    load = "$(window).on('load', function () { item.parentId = 1; });"
    assert isinstance(resolve_fixture("jx", "49949451", page=_jx_find_parent(load)), str)


def test_tree1_callback_only_for_verified_tree1():
    page = fixture("jx_page.html")
    tree1 = "$.each(_organTree.getNodes(), function (i, node) {"
    assert page.count(tree1) == 1
    assert not isinstance(resolve_fixture("jx", "49949451", page=page), str)
    for changed in (
        # 遍历的是树二
        page.replace(
            tree1 + "\n        delete node.children;",
            "$.each(_organTree2.getNodes(), function (i, node) {\n        node.isParent = true;",
            1,
        ),
        # 树一的回调里把参数换成树二的节点
        page.replace(tree1, tree1 + " node = _organTree2.getNodes()[0];", 1),
        page.replace(
            tree1, tree1 + " _organTree2.getNodes().forEach(function (node) { node.x = 1; });", 1
        ),
        # _organTree 不止赋值一次
        page.replace(tree1, "_organTree = _organTree2; " + tree1, 1),
    ):
        assert changed != page
        assert _refused(changed)


@pytest.mark.parametrize(
    "script",
    [
        "$.merge(catalogs[6991], [{id: 1, parentId: 49949451}]);",
        "$.uniqueSort(catalogs[6991]);",
        "$(catalogs[6991][0]).prop('parentId', 1);",
        "$.fn.zTree.getZTreeObj('organ_catalog_tree2');",
        "var z = $.fn.zTree; z.getZTreeObj('organ_catalog_tree2');",
        "$.fn['zTree'].init();",
        "Array.prototype.indexOf = function () { return -1; };",
        "var ap = Array.prototype; ap.indexOf = function () { return -1; };",
        "JSON.parse = function () { return []; };",
        "Object.defineProperty(Array.prototype, 'indexOf', {value: 1});",
        "encodeURIComponent = function (s) { return s; };",
        "var o = {}; o.__proto__ = null;",
    ],
)
def test_other_script_builtin_ztree_and_jquery_mutations_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None


def test_other_script_harmless_reads_still_recognized():
    script = (
        "function f() { return Array.prototype.slice.call(arguments); }"
        " $.each(catalogs[6991], function (i, n) { $('#c').text(n.name); });"
    )
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None


def test_initTree_ztree_obj_lookup_refused():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html").replace(
        "me.setSelect();",
        "$.each($.fn.zTree.getZTreeObj('organ_catalog_tree2').getNodes(), function (i, n) {"
        " n.children = null; }); me.setSelect();",
        1,
    )
    assert find_lonsun_api(page, BASE, BASE) is None


def test_undefined_request_values_not_sent():
    from youju.crawler.adapters import ListApi

    # 平台用 $.extend(true, …) 合并请求设置，值是 undefined 的键浏览器不带
    page = fixture("jx_page.html").replace(
        "me.listParams.catId = me.currNode.id;",
        "me.listParams.catId = me.currNode.id; me.listParams.keyWords = me.query.keyWords;",
        1,
    )
    result = resolve_fixture("jx", "49949451", page=page)
    assert isinstance(result, ListApi), result
    assert "keyWords" not in result.url
    # 对象、数组这类值认不出
    page = fixture("jx_page.html").replace(
        "me.listParams.catId = me.currNode.id;",
        "me.listParams.catId = me.currNode.id; me.listParams.keyWords = me.currNode;",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), str)


def test_deep_json_in_catalog_response_is_not_an_exception():
    from youju.crawler.adapters import api_follow, api_refusal, find_list_api
    from youju.crawler.fetcher import FetchResult

    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    deep = "[" * 100000 + "]" * 100000
    for text in ("var catalogs = {6991:" + deep + "}", deep):
        result = FetchResult(tree.url, "ok", tree.url, 200, text)
        assert isinstance(api_follow(tree, result), str)
        api_refusal(tree, result)  # 不抛异常


def test_big_unrelated_script_is_fast():
    import time

    # 67KB 无关赋值：以前每处赋值都重建整份记号列表，第一步要一分多钟
    script = "".join(f"var a{i} = {i}; a{i} = a{i} + 1;\n" for i in range(2500))
    assert len(script) > 60000
    started = time.monotonic()
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None
    assert time.monotonic() - started < 20


class GuardedLonsunCache(LonsunCache):
    """带这次运行主机护栏的缓存（PageCache 自带 round_guard）。"""

    def __init__(self, pages):
        from youju.crawler.roundguard import HostRoundGuard

        super().__init__(pages)
        self.round_guard = HostRoundGuard()


def test_verify_and_spotcheck_share_host_refusals():
    from youju.crawler.adapters import api_follow, find_list_api
    from youju.crawler.fetcher import FetchResult
    from youju.govsites.spotcheck import _first_list_item
    from youju.govsites.verify import BLOCKED, DOWN, check_one

    second = BASE.replace("49949451", "49949431")
    tree = find_list_api(fixture("jx_page.html"), BASE, BASE)
    catalog = FetchResult(tree.url, "ok", tree.url, 200, fixture("jx_catalog.js"))
    api = api_follow(tree, catalog)
    second_api = api_follow(find_list_api(fixture("jx_page.html"), second, second), catalog)
    pages = {
        BASE: fixture("jx_page.html"),
        second: fixture("jx_page.html"),
        tree.url: fixture("jx_catalog.js"),
        api.url: FetchResult(api.url, "http_error", api.url, 403),
        second_api.url: fixture("jx_api.json"),
    }
    cache = GuardedLonsunCache(pages)
    assert check_one(cache, lonsun_column(), TODAY).result == BLOCKED
    # 列表接口被拒以后，同一主机的别的栏目这次不去：记打不开（这次没取到），不当成它拒绝了
    checked = check_one(cache, lonsun_column(second), TODAY)
    assert checked.result == DOWN and "同一轮这个主机拒绝过我们" in checked.reason
    assert all(url != second for url, _ in cache.calls)
    # 抽查同样
    cache = GuardedLonsunCache(pages)
    reasons = []
    assert _first_list_item(cache, BASE, TODAY, reasons) is None
    assert _first_list_item(cache, second, TODAY, reasons) is None
    assert "不能抓" in reasons[0] and "这次没取到" in reasons[1]
    assert all(url != second for url, _ in cache.calls)
    # 栏目页回挑战页：同一主机别的栏目页也不去
    challenge = "<html><script>var yunsuo_session_verify='1';</script></html>"
    cache = GuardedLonsunCache({**pages, BASE: challenge})
    assert check_one(cache, lonsun_column(), TODAY).result == BLOCKED
    assert check_one(cache, lonsun_column(second), TODAY).result == DOWN
    assert [url for url, _ in cache.calls] == [BASE]


def test_spotcheck_catalog_host_bound_to_city():
    from types import SimpleNamespace

    from youju.govsites.spotcheck import _catalog_host_for

    def source(city, configured=None):
        config = {"lonsun_catalog_host": configured} if configured else {}
        return SimpleNamespace(province="安徽", city=city, parser_config=config)

    assert _catalog_host_for(source("芜湖"), {}) == "www.wuhu.gov.cn"
    assert _catalog_host_for(source("芜湖", "www.wuhu.gov.cn"), {}) == "www.wuhu.gov.cn"
    # 配错（别的城市的门户）不认
    assert _catalog_host_for(source("芜湖", "www.huainan.gov.cn"), {}) is None
    assert _catalog_host_for(source("合肥"), {}) is None
    assert _catalog_host_for(None, {"省份": "安徽", "城市": "淮南"}) == "www.huainan.gov.cn"


@pytest.mark.parametrize(
    "extra",
    [
        # 模块脚本浏览器会执行（在 DOM 就绪前），我们不分析
        '<script type="module">window.GLOBAL_CONTEXTPATH = "/other";</script>',
        '<script type="module" src="/x.js"></script>',
        # 别的 JavaScript 类型照样执行：照普通脚本分析
        '<script type="text/ecmascript">catalogs[6991] = [];</script>',
        '<script type=" Application/X-JavaScript ">catalogs[6991] = [];</script>',
        '<script language="javascript">catalogs[6991] = [];</script>',
        # 元素属性里的脚本
        "<img src='x.png' onerror=\"GLOBAL_CONTEXTPATH = '/x'\">",
        '<a href="javascript:catalogs[6991] = []">x</a>',
        '<div onclick="Ls.ajaxGet = null"></div>',
        "<iframe srcdoc=\"<script>parent.GLOBAL_CONTEXTPATH='/x'</script>\"></iframe>",
        # 整个改 window、经别名改全局
        "<script>$.extend(window, {GLOBAL_CONTEXTPATH: '/other'});</script>",
        "<script>Object.assign(window, {GLOBAL_CONTEXTPATH: '/other'});</script>",
        "<script>window['GLOBAL_' + 'CONTEXTPATH'] = '/x';</script>",
        "<script>var w = window; w.GLOBAL_CONTEXTPATH = '/x';</script>",
        "<script>(function (w) { w.GLOBAL_CONTEXTPATH = '/x'; })(window);</script>",
        "<script>$(window).prop('GLOBAL_CONTEXTPATH', '/x');</script>",
        "<script>top['GLOBAL_CONTEXTPATH'] = '/x';</script>",
    ],
)
def test_executed_code_we_do_not_analyze_refused(extra):
    assert _found(_jx(extra=extra)) is None, extra


@pytest.mark.parametrize(
    "extra",
    [
        # 不执行的：模板、写错的 type、支持模块的浏览器跳过的 nomodule
        '<script type="text/html">catalogs[6991] = [];</script>',
        '<script type="text/javascriapt">catalogs[6991] = [];</script>',
        "<script nomodule>catalogs[6991] = [];</script>",
        # 用户点了才跑、又不碰我们跟踪的东西的属性脚本，常见的 window 用法
        '<a href="javascript:void(0)" onclick="return false">x</a>',
        "<input type='text' onmouseover=\"this.style.color='red'\">",
        "<script>$(window).on('scroll', function () {}); $(window).scrollTop();</script>",
        "<script>var self = {}; function f(a) { return a; } f(self);</script>",
        "<script>if (typeof window !== 'undefined' && top !== self) { top.location = self.location; }</script>",
    ],
)
def test_harmless_scripts_and_attributes_still_recognized(extra):
    assert _found(_jx(extra=extra)) is not None, extra


def test_round_guard_cached_refusal_not_reused_for_other_sources():
    from youju.crawler.fetcher import FetchResult
    from youju.crawler.service import CatalogCache

    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return FetchResult(url, "http_error", url, 403)

    cache = CatalogCache()
    tree = "https://www.wuhu.gov.cn/openness/6787891/6596381.js?v=1558"
    assert cache.get(tree, fetch).http_status == 403
    # 别的栏目共用同一个栏目树：被拒的是第一个来源去的那一次，后面的没去，记"这次没取到"
    again = cache.get(tree, fetch)
    assert again.status == "site_paused" and calls == [tree]
    # 取到过的正常结果照常共用（不用再去）
    ok = CatalogCache()
    good = FetchResult(tree, "ok", tree, 200, "var catalogs = {1:[]}")
    ok.get(tree, lambda url, **kwargs: good)
    ok.block("https://www.wuhu.gov.cn/other.js")
    assert ok.get(tree, fetch) is good


_PROTO = "$.extend(PublicTree.prototype, {\n    bindEvt: function () {"
_TWEAK = "tweak: function () {\n      var me = this;\n      me.listParams.siteId = '999';\n    },"
_PRE_TOP = "preprocessData: function () {\n      var me = this;"


def _jx_with_tweak(call):
    page = fixture("jx_page.html")
    assert page.count(_PROTO) == 1 and page.count(_PRE_TOP) == 1
    page = page.replace(
        _PROTO, _PROTO.replace("{\n    bindEvt", "{\n    " + _TWEAK + "\n    bindEvt")
    )
    return page.replace(_PRE_TOP, _PRE_TOP + " " + call, 1)


@pytest.mark.parametrize(
    "call",
    [
        # 回调里调、按字符串下标调、括号包着调、先存进变量：都在加载时跑
        "$.each([1], function () { me.tweak(); });",
        "me['tweak']();",
        "(me.tweak)();",
        "var f = me.tweak; f.call(me);",
        "setTimeout(function () { me.tweak(); }, 0);",
    ],
)
def test_load_time_method_references_followed(call):
    assert _refused(_jx_with_tweak(call)), call


def test_load_time_reference_to_modeled_method_or_computed_name_refused():
    page = fixture("jx_page.html")
    for call in ("$.each([1], function () { me.getList(); });", "var k = 'tweak'; me[k]();"):
        assert _refused(page.replace(_PRE_TOP, _PRE_TOP + " " + call, 1)), call
    # 用户点了才跑的处理函数里调别的方法照常
    click = "$('#x').on('click', function () { me.getList(); });"
    assert not _refused(page.replace(_PRE_TOP, _PRE_TOP + " " + click, 1))


def test_constructor_callbacks_and_other_methods_refused():
    page = fixture("jx_page.html")
    init = "\n    me.init();\n  }"
    assert page.count(init) == 1
    for extra in (
        "$.each([1], function () { me.listParams.siteId = '999'; });",
        "me.bindEvt();",
        "(me.preprocessData)();",
    ):
        changed = page.replace(init, "\n    " + extra + init, 1)
        assert _found(changed) is None, extra


@pytest.mark.parametrize(
    "script",
    [
        # 经查找方法的返回值直接改树二的节点
        "$(function () { _organTree2.getNodeByParam('id', 49949451, null).children = null; });",
        "_organTree2.getNodes()[0].isParent = false;",
        # 别的 JavaScript 类型、改发请求用的内置对象
        '<script type="text/ecmascript">PublicTree.prototype.getList = function () {};</script>',
        "XMLHttpRequest.prototype.open = function () {};",
        "var X = XMLHttpRequest.prototype; X.send = null;",
        # 词法：if (…) 后面的 / 是正则；} 后面的 / 认不准
        "if (1) /'/.test(''); PublicTree.prototype.getList = 0; //'",
        "function f() {} /'/.test(''); PublicTree.prototype.getList = 0; //'",
    ],
)
def test_more_hidden_writes_refused(script):
    extra = script if script.startswith("<script") else f"<script>{script}</script>"
    assert _found(_jx(extra=extra)) is None, script


def test_writes_through_calls_in_load_time_methods():
    page = fixture("jx_page.html")
    select = _BIND_TOP
    assert page.count(select) == 1
    for extra in (
        "setTimeout(function () { _organTree2.getNodeByParam('id', 49949451, null).children = null; }, 0);",
        "me.getParams().pageSize = '100';",
    ):
        assert _refused(page.replace(select, select + " " + extra, 1)), extra
    # 对 split、slice 这类新建的数组原地排序不碰原数据：照常认
    fine = "var ys = '2020,2021'.split(',').sort().reverse();"
    assert not _refused(page.replace(select, select + " " + fine, 1))


def test_ui_method_using_other_methods_refused():
    page = fixture("jx_page.html")
    meta = "setMeta: function () {\n      var me = this;"
    for extra in ("(me.getList)();", "me['getNewList']();", "var f = me.open;"):
        assert _refused(page.replace(meta, meta + " " + extra, 1)), extra


def test_county_sources_need_configured_portal_even_under_city_domain():
    from youju.crawler.lonsun import find_lonsun_api, lonsun_unrecognized_reason

    page = fixture("wuhu_page.html")
    url = "https://county.wuhu.gov.cn/public/column/6596381?type=4&catId=6732111&action=list"
    script = "https://www.wuhu.gov.cn/openness/6787891/6596381.js?v=1558"
    # 省、市级来源：同一个市政府域名下的门户可以读（协调会话 11/13 口径 2）
    assert catalog_host_allowed(url, script)
    assert find_lonsun_api(page, url, url) is not None
    # 区县级来源：跨主机只认配置的本市白名单门户（口径 3），同城域名后缀不放行
    assert not catalog_host_allowed(url, script, county=True)
    assert find_lonsun_api(page, url, url, county=True) is None
    reason = lonsun_unrecognized_reason(page, url, url, county=True)
    assert "区县站" in reason
    assert catalog_host_allowed(url, script, "www.wuhu.gov.cn", county=True)
    assert find_lonsun_api(page, url, url, catalog_host="www.wuhu.gov.cn", county=True)
    # 同一主机的照常
    assert catalog_host_allowed(url, url.replace("/public/column", "/x.js"), county=True)


@pytest.mark.parametrize(
    "script,recognized",
    [
        # 函数参数只在函数里遮蔽：外面的 self 是窗口
        ("function f(self) {} var w = self; w.GLOBAL_CONTEXTPATH = '/other';", False),
        ("function f() { var self = 1; } top.GLOBAL_CONTEXTPATH = '/x'; frames['a'] = 1;", False),
        ("function g(a, self) { return self; } g(1, self);", False),
        # 在自己的作用域里当普通变量用的照常
        ("function g() { var self = this; return self; }", True),
        ("var self = {}; function h(x) { return x; } h(self);", True),
        ("function k(parent) { return [parent]; }", True),
        ("var a = 1, self = {}; self.x = 1; [self];", True),
    ],
)
def test_global_object_names_are_lexically_scoped(script, recognized):
    assert (_found(_jx(extra=f"<script>{script}</script>")) is not None) is recognized, script


def test_deferred_getdata_with_load_time_triggers_refused():
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("wuhu_page.html")
    assert _wuhu_found(page) is not None  # 芜湖：getData 放在 .done 回调里推迟着跑
    for extra in (
        "<script>$(function () { $('#gk-navigation a:first').trigger('click'); });</script>",
        "<script>setTimeout(function () { document.getElementById('x').click(); }, 0);</script>",
        "<body onload=\"$('.subNav:first').click()\">",
    ):
        changed = (
            page.replace("</body>", extra + "</body>", 1)
            if extra.startswith("<script")
            else page.replace("<body", extra[:-1] + " data-x='1'", 1)
        )
        assert changed != page
        assert find_lonsun_api(changed, WUHU, WUHU) is None, extra
    # 用户点了才跑的处理函数里触发的照常
    harmless = "<script>$('#x').on('click', function () { $('#y').trigger('click'); });</script>"
    assert _wuhu_found(page.replace("</body>", harmless + "</body>", 1)) is not None
    # getData 同步跑的页面（省经信厅）：别的脚本加载时触发事件不碍事
    jx = _jx(extra="<script>$(function () { $('.subNav:first').trigger('click'); });</script>")
    assert _found(jx) is not None


def _jx_catalog_with(nodes_prefix):
    catalog = fixture("jx_catalog.js")
    head = "var catalogs = {6991:["
    assert catalog.startswith(head)
    return head + nodes_prefix + catalog[len(head) :]


def test_set_select_error_before_getdata_means_no_request():
    from youju.crawler.adapters import ListApi

    # 补齐祖先以后只有一个顶层上级：照常认
    assert isinstance(resolve_fixture("jx", "49949451"), ListApi)
    # 前面多一个上级不在树里的栏目：findParentId 取第一个顶层上级（99），本栏目往上找不到它，
    # setSelect 在 getData 之前出错，浏览器不发列表请求
    stray = '{"id":11,"name":"别处","parentId":99,"isParent":false,"uri":"","link":""},'
    reason = resolve_fixture("jx", "49949451", catalog=_jx_catalog_with(stray))
    assert isinstance(reason, str) and "setSelect" in reason
    # 顶层栏目在树一里直接找到，不往上找：照常
    top = resolve_fixture("jx", "49949431", catalog=_jx_catalog_with(stray))
    assert not isinstance(top, str) or "setSelect" not in top
    # 调用包在 try 里（淮北那样）：出错也不碍事
    page = fixture("jx_page.html").replace(
        "me.setSelect();", "try { me.setSelect(); } catch (e) {}", 1
    )
    guarded = resolve_fixture("jx", "49949451", page=page, catalog=_jx_catalog_with(stray))
    assert isinstance(guarded, ListApi), guarded
    # 上级关系绕圈：栏目树本身就认不出（浏览器的 setSelect 也走不出来）
    loop = (
        '{"id":7,"name":"甲","parentId":8,"isParent":true,"uri":"","link":""},'
        '{"id":8,"name":"乙","parentId":7,"isParent":true,"uri":"","link":""},'
    )
    reason = resolve_fixture("jx", "49949451", catalog=_jx_catalog_with(loop))
    assert isinstance(reason, str) and ("循环" in reason or "绕圈" in reason)


def test_set_select_frequent_variant_and_unknown_shapes():
    from youju.crawler.adapters import ListApi

    # 淮北取出现最多的顶层上级：多出来的一个零星上级不碍事
    page = fixture("huaibei_page.html")
    assert not isinstance(resolve_fixture("huaibei", "14237535", page=page), str)
    # setSelect、findTopLevelParentNode、findParentId 改了写法：算不出，不读
    jx = fixture("jx_page.html")
    for old, new in (
        ("var currCatId = me.currCatId;", "var currCatId = me.currCatId; var x = 1;"),
        ("return currentNode;", "return currentNode || null;"),
        (
            "me.topLevelParentId = topLevelParentIds[0];",
            "me.topLevelParentId = topLevelParentIds[1];",
        ),
    ):
        assert jx.count(old) == 1, old
        result = resolve_fixture("jx", "49949451", page=jx.replace(old, new, 1))
        assert isinstance(result, str) and "写法认不出" in result, (old, result)
    assert isinstance(resolve_fixture("jx", "49949451"), ListApi)


# ---- 第七轮独立 Claude：推算 setSelect 的三个前提——findParentId 先跑、topLevelParentId 别处
# 不写、树一就是整棵栏目树 ----

_JX_INIT = "me.findParentId();\n      me.initTree();"


@pytest.mark.parametrize(
    "new",
    [
        # 不调 findParentId：浏览器里 topLevelParentId 是 undefined，从 49949451 往上找会出错
        "me.initTree();",
        # 挪到 initTree（它调 setSelect）后面
        "me.initTree();\n      me.findParentId();",
    ],
)
def test_set_select_needs_find_parent_before_init_tree(new):
    page = fixture("jx_page.html")
    assert page.count(_JX_INIT) == 1
    result = resolve_fixture("jx", "49949451", page=page.replace(_JX_INIT, new, 1))
    assert isinstance(result, str) and "findParentId" in result, result


def test_set_select_init_tree_called_from_other_method_refused():
    # 别的方法加载时调 initTree：那时 findParentId 可能还没跑
    page = fixture("jx_page.html")
    assert page.count(_BIND_TOP) == 1
    assert _refused(page.replace(_BIND_TOP, _BIND_TOP + " me.initTree();", 1))


@pytest.mark.parametrize(
    "where,code",
    [
        ("initTree", "me.topLevelParentId = 1;"),
        ("initTree", "me['topLevelParentId'] = 1;"),
        ("script", "var o = {topLevelParentId: 1};"),
        ("attribute", "window.topLevelParentId = 1"),
    ],
)
def test_top_level_parent_only_from_find_parent(where, code):
    page = fixture("jx_page.html")
    if where == "initTree":
        assert page.count("me.setSelect();") == 1
        page = page.replace("me.setSelect();", code + " me.setSelect();", 1)
    elif where == "script":
        page = _jx(extra=f"<script>{code}</script>")
    else:
        page = _jx(extra=f'<a href="#" onclick="{code}">x</a>')
    assert _found(page) is None


_CHUZHOU_TREE1 = (
    "_organTree = $.fn.zTree.init($('#organ_catalog_tree'), ztree_settings, me.ztreeData);"
)
_CHUZHOU_COLOR = "var color = '#0e76cd';"


def _chuzhou_top(page):
    """滁州 161734542：整棵树的顶层栏目，上级不是 findParentId 取的第一个顶层上级。"""
    from youju.crawler.lonsun import find_lonsun_api, resolve_lonsun

    url = PAGES["chuzhou"] + "?type=4&catId=161734542&action=list"
    api = find_lonsun_api(page, url, url)
    return None if api is None else resolve_lonsun(api, fixture("chuzhou_catalog.js"))


def test_set_select_top_column_found_in_whole_tree_one():
    from youju.crawler.adapters import ListApi

    # 树一是整棵栏目树建的：这个顶层栏目直接找到，不往上找，照常读
    assert isinstance(_chuzhou_top(fixture("chuzhou_page.html")), ListApi)


@pytest.mark.parametrize(
    "old,new,why",
    [
        # 树一不是照真实写法用整棵栏目树建的：浏览器在树一里找不到这个栏目，往上找时出错
        (_CHUZHOU_TREE1, _CHUZHOU_TREE1.replace("me.ztreeData", "[]"), "建树一"),
        (_CHUZHOU_TREE1, _CHUZHOU_TREE1.replace("ztree_settings", "{}"), "建树一"),
        (_CHUZHOU_TREE1, "var " + _CHUZHOU_TREE1, ""),  # 局部变量遮住全局的 _organTree
        ("initTree: function () {", "initTree: function (_organTree) {", "遮住了 _organTree"),
        # 建树一到 setSelect 之间另外动了树一
        (
            "me.setSelect();",
            "_organTree.removeNode(_organTree.getNodes()[0]); me.setSelect();",
            "建树一到 setSelect 之间",
        ),
        ("node.isParent = false;", "node.isParent = false; node.id = 0;", "建树一到 setSelect"),
        # 建树设置里的显示回调（建树时就跑）：调别的脚本、别的方法，往页面里写脚本，替用户触发事件
        (_CHUZHOU_COLOR, _CHUZHOU_COLOR + " hack();", "显示回调 fontCss"),
        (_CHUZHOU_COLOR, _CHUZHOU_COLOR + " me.getReportyear();", "显示回调 fontCss"),
        (
            _CHUZHOU_COLOR,
            _CHUZHOU_COLOR + " $('body').append('<scr' + 'ipt>x()</' + 'script>');",
            "",
        ),
        (_CHUZHOU_COLOR, _CHUZHOU_COLOR + " $('#x').trigger('custom');", ""),
        (_CHUZHOU_COLOR, _CHUZHOU_COLOR + " _organTree = null;", ""),
        # 显示回调里读空值的成员：建树时就出错，浏览器不发列表请求
        (_CHUZHOU_COLOR, _CHUZHOU_COLOR + " var x = null; x.foo();", "显示回调 fontCss"),
    ],
)
def test_set_select_tree_one_changed_refused(old, new, why):
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("chuzhou_page.html")
    assert page.count(old) == 1, old
    changed = page.replace(old, new, 1)
    result = _chuzhou_top(changed)
    assert result is None or isinstance(result, str), new
    if why:
        # 不读的原因正是这一条（不是别的规则顺带拦下的）
        url = PAGES["chuzhou"] + "?type=4&catId=161734542&action=list"
        reasons = []
        found = find_lonsun_api(changed, url, url, _diagnosis=reasons)
        assert why in (result if isinstance(result, str) else " ".join(reasons)), (
            result,
            reasons,
            found,
        )


def test_set_select_guarded_needs_no_tree_one_check():
    from youju.crawler.adapters import ListApi

    # setSelect 包在 try 里：出错也接着跑 getData，树一怎么建都不碍事
    page = fixture("chuzhou_page.html")
    page = page.replace("me.setSelect();", "try { me.setSelect(); } catch (e) {}", 1)
    page = page.replace(_CHUZHOU_TREE1, _CHUZHOU_TREE1.replace("me.ztreeData", "[]"), 1)
    assert isinstance(_chuzhou_top(page), ListApi)


def test_set_select_preprocess_changing_ids_refused():
    # 树一按 id 找栏目：preprocessData 改了节点的 id，算不出
    page = fixture("jx_page.html")
    old = "el._name = el.name;"
    assert page.count(old) == 1
    result = resolve_fixture("jx", "49949451", page=page.replace(old, old + " el.id = +el.id;", 1))
    assert isinstance(result, str) and "id" in result, result


def test_url_params_after_push_state_follow_new_url():
    # open 先 pushState 到栏目自己的地址（后面加 &nav=3），再读网址参数：浏览器读的是新地址
    page = fixture("jx_page.html")
    old = "if (me.currNode.children) {"
    assert page.count(old) == 1
    result = resolve_fixture(
        "jx", "49949451", page=page.replace(old, "if (Ls.url.get('nav') === '3') {", 1)
    )
    assert not isinstance(result, str) and result.plan.mode == "city_page", result
    # 栏目地址里带着的参数也按新地址读
    uri = '"uri":"/public/column/6991?type=4&catId=49949451&action=list"'
    catalog = fixture("jx_catalog.js")
    assert catalog.count(uri) == 1
    tagged = catalog.replace(uri, uri[:-1] + '&x=1"', 1)
    changed = page.replace(old, "if (Ls.url.get('x') === '') {", 1)
    result = resolve_fixture("jx", "49949451", page=changed, catalog=tagged)
    assert not isinstance(result, str) and result.plan.mode == "list", result


@pytest.mark.parametrize("tail", ["&q=a b", "&q=\\\\x.example", "&q=中"])
def test_push_state_target_with_odd_characters_refused(tail):
    uri = '"uri":"/public/column/6991?type=4&catId=49949451&action=list"'
    catalog = fixture("jx_catalog.js")
    result = resolve_fixture(
        "jx", "49949451", catalog=catalog.replace(uri, uri[:-1] + tail + '"', 1)
    )
    assert isinstance(result, str) and "pushState" in result, result


@pytest.mark.parametrize(
    "src", ["\\\\x.ah.gov.cn/49631981/6991.js", " /49631981/6991.js", "/49631981/6991.js\t"]
)
def test_catalog_src_with_odd_characters_refused(src):
    from youju.crawler.lonsun import find_lonsun_api

    page = fixture("jx_page.html")
    old = 'src="/49631981/6991.js"'
    assert page.count(old) == 1
    reasons = []
    changed = page.replace(old, f'src="{src}"', 1)
    assert find_lonsun_api(changed, BASE, BASE, _diagnosis=reasons) is None
    assert reasons and "栏目树脚本地址" in reasons[0], reasons


def test_legacy_octal_escapes_not_guessed():
    from youju.crawler.lonsun import LonsunFormatError, _string_literal

    assert _string_literal("'a\\0b'") == "a\0b"
    for text in ("'\\1'", "'\\01'", "'\\7x'"):
        with pytest.raises(LonsunFormatError):
            _string_literal(text)


@pytest.mark.parametrize(
    "guard",
    [
        "try { me.setSelect(); } finally {}",  # 没有 catch：异常照样往外抛
        "try { me.setSelect(); } catch (e) { throw e; }",
        "try { me.setSelect(); } catch (e) { me.foo(); }",
        "try { me.setSelect(); } catch (e) {} finally { x.y(); }",
        # 包在 try 里另起的函数：回调跑的时候 try 早结束了
        "try { setTimeout(function () { me.setSelect(); }, 0); } catch (e) {}",
        # catch 里调的 noData 不是真实页面那种写法（jx 页面没有 noData）
        "try { me.setSelect(); } catch (e) { me.noData(); }",
    ],
)
def test_set_select_try_must_really_swallow(guard):
    stray = '{"id":11,"name":"别处","parentId":99,"isParent":false,"uri":"","link":""},'
    page = fixture("jx_page.html").replace("me.setSelect();", guard, 1)
    url = PAGES["jx"] + "?type=4&catId=49949451&action=list"
    api = find_lonsun_api(page, url, url)
    result = None if api is None else resolve_lonsun_text(api, _jx_catalog_with(stray))
    assert result is None or isinstance(result, str), result


def resolve_lonsun_text(api, catalog):
    from youju.crawler.lonsun import resolve_lonsun

    return resolve_lonsun(api, catalog)


def test_set_select_swallowed_by_catch_like_real_pages():
    from youju.crawler.adapters import ListApi

    # 淮北那样：catch 里 console.log、调真实写法的 noData，异常被吞掉、接着发列表请求
    stray = '{"id":11,"name":"别处","parentId":99,"isParent":false,"uri":"","link":""},'
    for guard in (
        "try { me.setSelect(); } catch (e) {}",
        "try { me.setSelect(); } catch (err) { console.log('e', err); }",
    ):
        page = fixture("jx_page.html").replace("me.setSelect();", guard, 1)
        result = resolve_fixture("jx", "49949451", page=page, catalog=_jx_catalog_with(stray))
        assert isinstance(result, ListApi), (guard, result)
    assert isinstance(resolve_fixture("huaibei", "14237535"), ListApi)


@pytest.mark.parametrize(
    "tail,condition",
    [
        ("&x=1&x=2", "Ls.url.get('x') === '1'"),  # 同名参数：平台取第一个
        ("&x=a+b", "Ls.url.get('x') === 'a b'"),  # 平台先把 + 换成空格
        ("&x=%31", "Ls.url.get('x') === '1'"),
        ("&u=a?x=1", "Ls.url.get('x') === '1'"),  # 平台的正则在参数值里也找得到 ?x=
        ("&catId=49949461", "Ls.url.get('catId') === '49949461'"),
    ],
)
def test_push_state_ambiguous_params_unknown(tail, condition):
    uri = '"uri":"/public/column/6991?type=4&catId=49949451&action=list"'
    catalog = fixture("jx_catalog.js").replace(uri, uri[:-1] + tail + '"', 1)
    page = fixture("jx_page.html").replace("if (me.currNode.children) {", f"if ({condition}) {{", 1)
    result = resolve_fixture("jx", "49949451", page=page, catalog=catalog)
    assert isinstance(result, str) and "pushState 改的网址参数认不准" in result, result


@pytest.mark.parametrize(
    "where,code",
    [
        ("script", "$('head').append('<script>catalogs={6991:[]};<\\/script>');"),
        ("script", "$('#a').html('<scr' + 'ipt>x()</scr' + 'ipt>');"),
        ("script", "$('<script>x()</script>').appendTo('body');"),
        ("script", "$('body').append(unescape('%3Cscript%3Ex()%3C/script%3E'));"),
        ("script", "$.globalEval('x()');"),
        ("script", "jQuery.getScript('/a.js');"),
        ("script", "$('#a').load('/frag.html');"),
        ("script", "var code = 'x()'; setTimeout(code, 10);"),
        ("attribute", "eval('x()')"),
    ],
)
def test_hidden_code_refused(where, code):
    if where == "script":
        page = _jx(extra=f"<script>{code}</script>")
    else:
        page = _jx(extra=f'<img src="/a.png" onload="{code}">')
    assert _found(page) is None, code


def test_harmless_load_and_timers_still_fine():
    for code in (
        "$(window).load(function () { $('#a').show(); });",
        "setTimeout(function () {}, 5);",
    ):
        assert _found(_jx(extra=f"<script>{code}</script>")) is not None, code


@pytest.mark.parametrize("src", ["/49631981\\6991.js", "/49631981/6991.js ", "/496319\n81/6991.js"])
def test_catalog_src_normalized_like_browser(src):
    # 浏览器规整以后也是栏目树脚本：和正常写法同时出现时不能绕过"几处地址不一样"
    page = fixture("jx_page.html").replace("</body>", f'<script src="{src}"></script></body>', 1)
    reasons = []
    assert find_lonsun_api(page, BASE, BASE, _diagnosis=reasons) is None
    assert reasons and "栏目树脚本地址" in reasons[0], reasons


def test_preprocess_grep_member_needs_strings():
    from youju.crawler.adapters import ListApi

    page = fixture("jx_page.html")
    old = "me.hideIds = ids;"
    assert page.count(old) == 1
    page = page.replace(
        old,
        old
        + " var named = $.grep(me.ztreeData, function (el) { return el.uri.indexOf('x') > -1; });",
        1,
    )
    assert isinstance(resolve_fixture("jx", "49949451", page=page), ListApi)
    broken = '{"id":12,"name":"甲","parentId":49938311,"isParent":false,"uri":null,"link":""},'
    reason = resolve_fixture("jx", "49949451", page=page, catalog=_jx_catalog_with(broken))
    assert isinstance(reason, str) and "uri" in reason, reason


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_catalog_and_list_json_strict(constant):
    # 栏目树、列表返回只按 JSON 解析：Python 自带的 json 还认 NaN、Infinity，JSON 里没有
    catalog = fixture("jx_catalog.js").replace(
        '"isParent":false', f'"extra":{constant},"isParent":false', 1
    )
    assert catalog != fixture("jx_catalog.js")
    with pytest.raises(LonsunFormatError):
        parse_catalog(catalog, "6991")
    reply = fixture("jx_api.json").replace('"total":', f'"extra":{constant},"total":', 1)
    assert reply != fixture("jx_api.json")
    with pytest.raises(LonsunFormatError):
        parse_response(reply, "list", BASE, TODAY)


def test_guarded_set_select_still_checks_ancestor_loop():
    from youju.crawler.adapters import ListApi

    # 淮北：setSelect 包在 try 里。祖先节点的上级是自己：浏览器卡在 findTopLevelParentNode 的循环里
    # （catch 接不住死循环），到不了 getData
    node = '"id":14237484,"name":"政策","parentId":4737901'
    catalog = fixture("huaibei_catalog.js")
    assert catalog.count(node) == 1
    looped = catalog.replace(node, node.replace("4737901", "14237484"), 1)
    reason = resolve_fixture("huaibei", "14237535", catalog=looped)
    assert isinstance(reason, str) and "绕圈" in reason, reason
    assert isinstance(resolve_fixture("huaibei", "14237535"), ListApi)


@pytest.mark.parametrize(
    "code",
    [
        'window.setTimeout("x()", 1);',
        "this.setTimeout('x()', 1);",
        'window.setInterval("x()", 5);',
        "window['setTimeout']('x()', 1);",
        "setTimeout.call(window, 'x()', 1);",
        "Function.call(null, 'PublicTree.prototype.getList = function () {}')();",
        "var F = Function;",
        "window.Function('x()')();",
        "var s = document.createElement('script'); s.text = 'x()'; document.body.appendChild(s);",
        "var s = document.createElement('script');"
        " s.appendChild(document.createTextNode('x()')); document.body.appendChild(s);",
        "window['ev' + 'al']('x()');",
        "location.href = 'javascript:x()';",
        "window.location.assign('javascript:x()');",
        "window.open('javascript:x()');",
        "$('head').append('<base href=\"https://x.example/\">');",
        "document.write('<base href=\"/x/\">');",
    ],
)
def test_more_hidden_code_refused(code):
    assert _found(_jx(extra=f"<script>{code}</script>")) is None, code


def test_ordinary_script_elements_and_links_still_fine():
    for code in (
        "var hm = document.createElement('script'); hm.src = 'https://a.example/hm.js';"
        " document.getElementsByTagName('head')[0].appendChild(hm);",
        "$('#a').attr('href', 'javascript:void(0)');",
        "var cfg = { links: 'javascript:void(0)' };",
    ):
        assert _found(_jx(extra=f"<script>{code}</script>")) is not None, code


def test_push_state_state_argument_must_be_null():
    # 前两个参数浏览器要结构化克隆：带函数的对象会抛 DataCloneError，open 中断、不发列表请求
    page = fixture("jx_page.html").replace(
        "window.history.pushState(null, null, url);",
        "window.history.pushState(me.currNode, null, url);",
        1,
    )
    result = resolve_fixture("jx", "49949451", page=page)
    assert isinstance(result, str) and "pushState" in result, result


def test_url_get_name_with_regex_chars_unknown():
    page = fixture("jx_page.html").replace(
        "if (me.currNode.children) {", "if (Ls.url.get('x.y') === '') {", 1
    )
    result = resolve_fixture("jx", "49949451", page=page)
    assert isinstance(result, str) and "参数名认不准" in result, result


@pytest.mark.parametrize(
    "code",
    [
        # 前面带 window.、this.、document.defaultView. 的改写一样算
        "window.XMLHttpRequest.prototype.send = function (b) {};",
        "window.encodeURIComponent = function (x) { return x; };",
        "window.JSON.parse = null;",
        "window.window.GLOBAL_CONTEXTPATH = '/x';",
        "document.defaultView.GLOBAL_CONTEXTPATH = '/x';",
        "this.GLOBAL_CONTEXTPATH = '/x';",
        "(function (r) { r.GLOBAL_CONTEXTPATH = '/x'; })(this);",
        "this.catalogs = {6991: []};",
        "window.jQuery.param = function () { return ''; };",
        # 脚本顶层用 var、function 声明这些名字
        "var GLOBAL_CONTEXTPATH = '/x';",
        "var XMLHttpRequest = function () {};",
        "function encodeURIComponent(x) { return x; }",
        # 插进页面会自己跑的、动态加载的代码
        "$('body').append('<img src=x onerror=x()>');",
        "import('data:text/javascript,x()');",
        "var s = document.createElement('script'); s.src = 'data:text/javascript,x()';",
        "var s = document.createElement('script'); s['text'] = 'x()';",
    ],
)
def test_global_rewrites_with_prefixes_refused(code):
    assert _found(_jx(extra=f"<script>{code}</script>")) is None, code


def test_shadowed_self_index_is_fine():
    # self 在那里是局部变量：按算出来的名字取它的成员不算取 window 的
    code = "function f() { var self = {a: 1}; return self['a' + '']; }"
    assert _found(_jx(extra=f"<script>{code}</script>")) is not None


def test_guarded_set_select_still_needs_known_shapes():
    # 淮北：setSelect 包在 try 里。findTopLevelParentNode 的循环改成找不到就停在原地：上级不在树里时
    # 浏览器转不出来，catch 接不住，到不了 getData
    page = fixture("huaibei_page.html")
    old = "return node.id === currentNode.parentId;\n                })[0];"
    assert page.count(old) == 1
    changed = page.replace(old, old[:-1] + " || currentNode;", 1)
    node = '"id":14237484,"name":"政策","parentId":4737901'
    catalog = fixture("huaibei_catalog.js").replace(node, node.replace("4737901", "999999"), 1)
    reason = resolve_fixture("huaibei", "14237535", page=changed, catalog=catalog)
    assert isinstance(reason, str) and "写法认不出" in reason, reason


def test_ready_reply_with_nan_names_ready_request():
    from youju.crawler.adapters import api_parts, api_refusal
    from youju.crawler.fetcher import FetchResult

    ready = api_parts(resolve_fixture("wuhu", "6732121"))[0]
    reply = FetchResult(ready.url, "ok", ready.url, 200, '{"status":NaN}')
    assert api_refusal(ready, reply).startswith("前置接口")


def test_preprocess_name_member_needs_string_names():
    # jx 的 preprocessData 读 el.name.length：哪个节点 name 是 null，浏览器在这里就出错
    broken = '{"id":12,"name":null,"parentId":49938311,"isParent":false,"uri":"","link":""},'
    reason = resolve_fixture("jx", "49949451", catalog=_jx_catalog_with(broken))
    assert isinstance(reason, str) and "name" in reason and "preprocessData" in reason


@pytest.mark.parametrize(
    "script",
    [
        "history.replaceState(null, '', '?type=4&action=list');",
        "window.history.pushState(null, '', location.pathname);",
        "history['replaceState'](null, '', '?');",
        "Ls.url.updateSearchParam('catId', '1');",
        "$(function () { history.replaceState(null, '', '?catId=1'); });",
    ],
)
def test_url_rewrites_before_constructor_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None, script


def test_url_rewrites_in_constructor_refused_but_navigation_and_handlers_fine():
    page = fixture("jx_page.html")
    ctor = "function PublicTree() {\n    var me = this;"
    changed = page.replace(ctor, ctor + " history.replaceState(null, '', '?type=4');", 1)
    assert _found(changed) is None
    # 用户点了才跑的、跳走离开页面的：不改这一页读到的网址
    for script in (
        "$('#x').on('click', function () { history.pushState(null, '', '?a=1'); });",
        "function go(v) { if (v) { location.href = '/search?q=' + v; } }",
    ):
        assert _found(_jx(extra=f"<script>{script}</script>")) is not None, script


@pytest.mark.parametrize(
    "script",
    [
        "var a = 1; <!-- '\nPublicTree.prototype.getList = 0; //'",
        "var t = `${PublicTree.prototype.getList = 0}`;",
        "var o = {in: 4}; var q = o.in / 2; PublicTree.prototype.getList = 0; var r = 3 / 4;",
        "var 别名 = window; 别名.GLOBAL_CONTEXTPATH = '/x';",
        "var \\u0061 = window; a.GLOBAL_CONTEXTPATH = '/x';",
        "eval('PublicTree.prototype.getList = 0');",
        "window['eval']('x');",
        "new Function('PublicTree.prototype.getList = 0')();",
        "[].constructor.constructor('x')();",
        "setTimeout('PublicTree.prototype.getList = 0', 0);",
        "document.write('<script>PublicTree.prototype.getList = 0<\\/script>');",
        "document.write(unescape('%3Cscript%3Ex%3D1%3C/script%3E'));",
        "$.param = function () { return 'token=1'; };",
        "$.extend({ grep: function () { return []; } });",
        "jQuery.extend(true, { param: null });",
    ],
)
def test_code_we_cannot_see_refused(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is None, script


@pytest.mark.parametrize(
    "script",
    [
        "var re = new RegExp(`([?&])${paramName}=[^&#]*(&|#|$)`);",
        "document.write(unescape(\"%3Cspan id='_ideConac'%3E%3C/span%3E%3Cscript src='https://example.gov.cn/a.js' type='text/javascript'%3E%3C/script%3E\"));",
        "$.fn.myPlugin = function () { return this; };",
        "var params = $.extend({}, {a: 1}, {b: 2});",
    ],
)
def test_common_harmless_code_still_recognized(script):
    assert _found(_jx(extra=f"<script>{script}</script>")) is not None, script


def test_constructor_reads_catalog_after_organ_id():
    page = fixture("jx_page.html")
    assert "me.organId = '6991';" in page
    moved = page.replace("me.organId = '6991';", "", 1).replace(
        "me.prefix = '';", "me.prefix = ''; me.organId = '6991';", 1
    )
    # 只要 organId 还在 ztreeData 之前就照常；挪到后面不认
    ztree = "me.ztreeData = "
    head, tail = moved.split(ztree, 1)
    after = (
        head.replace("me.organId = '6991';", "", 1)
        + ztree
        + tail.replace("me.init();", "me.organId = '6991'; me.init();", 1)
    )
    assert _found(after) is None


def test_catalog_script_must_have_run_before_construction():
    page = fixture("jx_page.html")
    src = '<script type="text/javascript" src="/49631981/6991.js"></script>'
    assert page.count(src) == 1 and page.count(_CREATE) == 1
    top = "new PublicTree();"
    # 就绪回调里创建：defer 的栏目树脚本在 DOM 解析完之前跑过，来得及
    deferred = page.replace(src, src.replace("<script ", "<script defer "), 1)
    assert _found(deferred) is not None
    # 脚本顶层创建：defer 的、写在后面的栏目树脚本那时还没跑，浏览器不发列表请求
    assert _found(deferred.replace(_CREATE, top, 1)) is None
    moved = page.replace(src, "", 1).replace("</body>", src + "</body>", 1)
    assert _found(moved) is not None  # 就绪回调里创建：写在后面也来得及
    assert _found(moved.replace(_CREATE, top, 1)) is None
    # async 的什么时候跑说不准
    assert _found(page.replace(src, src.replace("<script ", "<script async "), 1)) is None
    # 脚本顶层创建的一律不认（157 个真实页面都在就绪回调里创建；顶层创建时原型方法、平台脚本、
    # 栏目树脚本是不是都已经跑过，先后认不准），栏目树脚本写在前面也一样
    reasons = []
    assert find_lonsun_api(page.replace(_CREATE, top, 1), BASE, BASE, _diagnosis=reasons) is None
    assert reasons and "顶层创建" in reasons[0], reasons


def test_huge_is_parent_number_does_not_raise():
    big = "1" + "0" * 400
    catalog = fixture("jx_catalog.js").replace('"isParent":false', '"isParent":' + big, 1)
    assert catalog != fixture("jx_catalog.js")
    result = resolve_fixture("jx", "49949451", catalog=catalog)  # 不抛 OverflowError
    assert result is not None
