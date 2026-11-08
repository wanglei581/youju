"""全国覆盖盘点：名称识别、导航链接、部门和栏目识别、平台特征、逐级走导航（用假的页面缓存）。"""

from datetime import date

from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import extract_items
from youju.govsites.divisions import (
    DivisionIndex,
    NameMatcher,
    clean_place_text,
    name_variants,
    parse_tree,
)
from youju.govsites.inventory import (
    GOV_LOCAL_NAV,
    Inventory,
    PageInfo,
    find_columns,
    level_chars,
    level_prefix,
    report_homepage,
)
from youju.govsites.links import nav_links, page_links, parse_html, registrable
from youju.govsites.names import column_kind, dept_categories
from youju.govsites.platform import blocker, fingerprint

TREE = {
    "children": [
        {
            "code": "330000000000",
            "name": "浙江省",
            "type": "省",
            "level": 1,
            "children": [
                {
                    "code": "330100000000",
                    "name": "杭州市",
                    "type": "地级市",
                    "level": 2,
                    "children": [
                        {"code": "330106000000", "name": "西湖区", "type": "市辖区", "level": 3},
                        {"code": "330182000000", "name": "建德市", "type": "县级市", "level": 3},
                    ],
                },
            ],
        },
        {
            "code": "110000000000",
            "name": "北京市",
            "type": "直辖市",
            "level": 1,
            "children": [{"code": "110105000000", "name": "朝阳区", "type": "市辖区", "level": 3}],
        },
        {
            "code": "420000000000",
            "name": "湖北省",
            "type": "省",
            "level": 1,
            "children": [
                {"code": "429004000000", "name": "仙桃市", "type": "县级市", "level": 3},
                {
                    "code": "422800000000",
                    "name": "恩施土家族苗族自治州",
                    "type": "自治州",
                    "level": 2,
                    "children": [],
                },
            ],
        },
        {"code": "810000000000", "name": "香港特别行政区", "type": "特别行政区", "level": 1},
        {"code": "710000000000", "name": "台湾省", "type": "省", "level": 1},
    ]
}


def _divisions():
    return parse_tree(TREE)


def test_parse_tree_fields_follow_sources_conventions():
    by_name = {d.name: d for d in _divisions()}
    assert "香港特别行政区" not in by_name and "台湾省" not in by_name
    hz = by_name["杭州市"]
    assert (hz.level, hz.province, hz.city, hz.district, hz.code) == (
        "市级",
        "浙江",
        "杭州",
        None,
        "330100",
    )
    xh = by_name["西湖区"]
    assert (xh.level, xh.city, xh.district, xh.parent_code) == (
        "区县级",
        "杭州",
        "西湖区",
        "330100",
    )
    # 直辖市的区：城市填直辖市简称；省直辖县级市：城市填自己的简称
    assert by_name["朝阳区"].city == "北京"
    assert (by_name["仙桃市"].city, by_name["仙桃市"].parent_code) == ("仙桃", "420000")
    assert by_name["恩施土家族苗族自治州"].city == "恩施土家族苗族自治州"


def test_name_variants_cover_navigation_spellings():
    assert name_variants("浙江省", "省") >= {"浙江省", "浙江"}
    assert name_variants("杭州市") >= {"杭州市", "杭州"}
    assert name_variants("恩施土家族苗族自治州") >= {"恩施州", "恩施"}
    assert name_variants("峨边彝族自治县") >= {"峨边县", "峨边"}
    assert "巴州" in name_variants("巴音郭楞蒙古自治州")
    # 简称至少两个字
    assert name_variants("沙市区") == {"沙市区", "沙市"}


def test_matcher_strips_parent_prefix_and_suffix():
    index = DivisionIndex(_divisions())
    hz = next(d for d in index.all if d.name == "杭州市")
    m = NameMatcher(index.children(hz.code), ["浙江省", "浙江", "杭州市", "杭州"])
    assert m.match("杭州市西湖区人民政府").name == "西湖区"
    assert m.match(" 建德 ").name == "建德市"
    assert m.match("西湖区门户网站").name == "西湖区"
    assert m.match("西湖区召开全体会议部署今年重点工作任务") is None
    assert clean_place_text("浙江省人民政府门户网站") == "浙江省"


def test_matcher_refuses_ambiguous_names():
    from youju.govsites.divisions import Division

    a = Division("320106", "鼓楼区", "市辖区", "区县级", "江苏", "南京", "鼓楼区", "320100")
    b = Division("320302", "鼓楼区", "市辖区", "区县级", "江苏", "徐州", "鼓楼区", "320300")
    assert NameMatcher([a, b]).match("鼓楼区") is None
    assert NameMatcher([a]).match("鼓楼区") is a


def test_page_links_reads_options_onclick_scripts_and_stray_html_end():
    html = """<html><body><div>head</div></html>
    <div class="foot">
      <select><option value="">市县网站</option><option value="http://www.hangzhou.gov.cn/">杭州市</option></select>
      <a href="javascript:;" onclick="window.open('http://www.jiande.gov.cn')">建德市</a>
      <a href="/col/col1/index.html">通知公告</a>
    </div>
    <script>var sites=[{name:"西湖区",url:"http://www.hzxh.gov.cn/"}];</script>
    </body></html>"""
    links = {(link.text, link.url) for link in page_links(html, "https://www.zj.gov.cn/")}
    assert ("杭州市", "http://www.hangzhou.gov.cn/") in links
    assert ("建德市", "http://www.jiande.gov.cn/") in links
    assert ("通知公告", "https://www.zj.gov.cn/col/col1/index.html") in links
    assert ("西湖区", "http://www.hzxh.gov.cn/") in links


def test_listing_reads_list_after_stray_html_end():
    rows = "".join(
        f'<li><a href="/zw/{i}.html">关于开展2026年度第{i}批项目申报工作的通知</a>'
        f"<span>2026-10-{i:02d}</span></li>"
        for i in range(1, 6)
    )
    html = f"<html><body><div>页头</div></html><ul class='list'>{rows}</ul></body></html>"
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=date(2026, 11, 5))
    assert len(items) == 5


def test_parse_html_expands_hanweb_datastore():
    html = (
        "<html><body><script type='text/xml'><datastore><recordset>"
        "<record><![CDATA[<li><a href='/art/2026/11/1/art_1_2.html'>某某通知</a></li>]]></record>"
        "</recordset></datastore></script></body></html>"
    )
    doc = parse_html(html)
    assert [a.get("href") for a in doc.iter("a")] == ["/art/2026/11/1/art_1_2.html"]


def test_nav_links_prefer_site_navigation_on_same_domain():
    links = page_links(
        """<a href="/wzdt/">网站地图</a><a href="/sxwz/">市县政府网站</a>
        <a href="http://other.example.com/x">政府网站导航</a>
        <a href="/news/1.html">关于政府网站工作年度报表的说明和部署安排的长新闻标题</a>
        <a href="/nb/">2025年政府网站年度报表</a>""",
        "https://www.zj.gov.cn/",
    )
    texts = [link.text for link in nav_links(links, "https://www.zj.gov.cn/")]
    assert texts == ["市县政府网站", "2025年政府网站年度报表", "网站地图"]
    assert (
        registrable("http://kjt.zj.gov.cn/a")
        == registrable("https://www.zj.gov.cn/")
        == "zj.gov.cn"
    )


def test_dept_categories():
    assert dept_categories("浙江省科学技术厅") == ["科技"]
    assert dept_categories("省经信厅") == ["工信"]
    assert set(dept_categories("建德市科学技术和工业信息化局")) == {"工信", "科技"}
    assert dept_categories("市场监督管理局（知识产权局）") == ["市场监管"]
    assert dept_categories("浙江省文化广电和旅游厅") == ["文旅"]
    assert dept_categories("杭州市数据资源局") == ["数据局"]
    assert dept_categories("杭州高新技术产业开发区管理委员会") == ["开发区"]
    assert dept_categories("浙江省人民政府办公厅") == ["政府办"]
    assert dept_categories("浙江省农业科学院") == []
    assert dept_categories("省科技厅召开全省科技工作会议") == []
    assert dept_categories("浙江省教育厅") == []


def test_column_kind():
    assert column_kind("通知公告") == ("通知公告", 0)
    assert column_kind("公示公告") == ("公示公告", 0)
    assert column_kind("规范性文件") == ("政策文件", 0)
    assert column_kind("省政府文件") == ("政策文件", 1)
    assert column_kind("政策解读") is None
    assert column_kind("关于印发某某办法的通知") is None


def test_level_prefix_and_chars():
    assert level_prefix("省科技厅") == "省"
    assert level_prefix("市场监督管理局") is None
    index = DivisionIndex(_divisions())
    by_name = {d.name: d for d in index.all}
    assert level_chars(by_name["建德市"]) == "市"
    assert level_chars(by_name["西湖区"]) == "区"
    assert level_chars(by_name["浙江省"]) == "省"


def test_fingerprint_and_blocker():
    html = """<html><head><meta name="SiteName" content="浙江省科学技术厅">
    <meta name="SiteIDCode" content="3300000018"></head><body>
    <a href="/art/2026/11/1/art_1229_1.html">a</a><a href="/art/2026/11/2/art_1229_2.html">b</a>
    <div>技术支持：南京大汉网络有限公司</div></body></html>"""
    doc = parse_html(html)
    links = [link.url for link in page_links(html, "https://kjt.zj.gov.cn/", doc)]
    fp = fingerprint(html, "https://kjt.zj.gov.cn/", doc, links)
    assert (fp.site_name, fp.site_id_code, fp.vendor) == ("浙江省科学技术厅", "3300000018", "大汉")
    assert fp.shape.startswith("art_")
    assert blocker("http_error", 412, "<html>$_ts=window['$_ts']</html>") == "瑞数防护"
    assert blocker("robots_disallowed", None, "") == "robots.txt 禁止"
    assert blocker("ok", 200, "<html>" + "正文" * 3000 + "请输入验证码</html>") is None


def _page(url: str, html: str, final: str | None = None) -> PageInfo:
    doc = parse_html(html)
    return PageInfo(
        url,
        final or url,
        FetchResult(url, "ok", final or url, 200, html),
        doc,
        page_links(html, final or url, doc),
    )


def test_find_columns_prefers_exact_names_and_more_links():
    html = """<div class="box"><h3>通知公告</h3><a class="more" href="/tzgg/">更多</a></div>
    <a href="/zcwj/">政策文件</a><a href="/zcjd/">政策解读</a>
    <a href="http://other.gov.cn/gsgg/">公示公告</a>"""
    hits = find_columns([_page("https://kjt.zj.gov.cn/", html)])
    assert hits["通知公告"].url == "https://kjt.zj.gov.cn/tzgg/"
    assert hits["政策文件"].url == "https://kjt.zj.gov.cn/zcwj/"
    # 链到别家网站的栏目（如省政策文件库）不算这个部门的
    assert "公示公告" not in hits


def test_report_homepage():
    page = _page(
        "https://www.zj.gov.cn/nb/1.html",
        "<table><tr><td>网站名称</td><td>浙江省科学技术厅门户网站</td></tr>"
        "<tr><td>首页网址</td><td>http://kjt.zj.gov.cn/</td></tr></table>",
    )
    assert report_homepage(page) == "http://kjt.zj.gov.cn/"


class FakeCache:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.hits = self.fetches = 0

    def get(self, url: str) -> FetchResult:
        if url in self.pages:
            return FetchResult(url, "ok", url, 200, self.pages[url])
        return FetchResult(url, "http_error", url, 404, "", "HTTP 404")


def test_inventory_walks_official_navigation():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.zj.gov.cn/">浙江</a><a href="http://www.beijing.gov.cn/">北京</a>',
        "https://www.zj.gov.cn/": """<meta name="SiteName" content="浙江省人民政府">
            <a href="/zwgk/zfwj/">省政府文件</a><a href="/tzgg/">通知公告</a>
            <a href="/sxwz/">市县政府网站</a><a href="/bmwz/">省直部门网站</a>""",
        "https://www.zj.gov.cn/sxwz/": '<a href="https://www.hangzhou.gov.cn/">杭州市</a>',
        "https://www.zj.gov.cn/bmwz/": """<a href="http://kjt.zj.gov.cn/">浙江省科学技术厅</a>
            <a href="http://www.hangzhou.gov.cn/kjj/">杭州市科学技术局</a>""",
        "http://kjt.zj.gov.cn/": """<meta name="SiteName" content="浙江省科学技术厅">
            <a href="/col/col1229080140/index.html">通知公告</a>
            <a href="/col/col1229080141/index.html">政策文件</a>""",
        "https://www.hangzhou.gov.cn/": """<a href="/qxwz/">区县（市）政府网站</a>
            <a href="/bm/kjj/">市科技局</a><a href="http://kjt.zj.gov.cn/">省科技厅</a>
            <a href="https://www.mee.gov.cn/">生态环境部</a>
            <a href="https://hz.zjzwfw.gov.cn/dept?deptCode=1">市商务局</a>""",
        "https://www.hangzhou.gov.cn/qxwz/": '<a href="http://www.hzxh.gov.cn/">西湖区</a>',
        "https://www.hangzhou.gov.cn/bm/kjj/": '<a href="/bm/kjj/tzgg/">通知公告</a>',
        "http://www.hzxh.gov.cn/": '<a href="/jgsz/">机构设置</a>',
        "http://www.hzxh.gov.cn/jgsz/": '<a href="/bm/kjj/">区科技局</a><a href="/bm/fgj/">区发改局</a>',
        "http://www.hzxh.gov.cn/bm/kjj/": '<a href="/bm/kjj/xxgk/">信息公开</a>',
        "http://www.hzxh.gov.cn/bm/kjj/xxgk/": '<a href="/bm/kjj/tzgg/">通知公告</a>',
        "http://www.hzxh.gov.cn/bm/fgj/": "<p>发改局</p>",
    }
    pages["https://www.gov.cn/home/2023-03/29/content_5748953.htm"] = (
        '<a href="https://www.mee.gov.cn/">生态环境部</a><a href="https://www.most.gov.cn/">科学技术部</a>'
    )
    divisions = [d for d in _divisions() if d.province == "浙江"]
    inv = Inventory(
        DivisionIndex(divisions), FakeCache(pages), provinces=["浙江"], workers=4, per_province=2
    )
    inv.run()
    portals = {p.name: p for p in inv.portals.values()}
    assert portals["浙江省"].status == "已找到"
    assert portals["杭州市"].portal_url == "https://www.hangzhou.gov.cn/"
    assert portals["杭州市"].nav_source == "https://www.zj.gov.cn/sxwz/"
    assert portals["西湖区"].portal_url == "http://www.hzxh.gov.cn/"
    assert portals["建德市"].status == "导航未找到"
    depts = {(d.code, d.dept): d for d in inv.depts}
    prov_kj = depts[("330000", "科技")]
    assert (prov_kj.site_url, prov_kj.dept_full_name, prov_kj.nav_source) == (
        "http://kjt.zj.gov.cn/",
        "浙江省科学技术厅",
        "https://www.zj.gov.cn/bmwz/",
    )
    assert {c.kind for c in prov_kj.columns} == {"通知公告", "政策文件"}
    gov = depts[("330000", "政府办")]
    assert {c.kind: c.url for c in gov.columns}["政策文件"] == "https://www.zj.gov.cn/zwgk/zfwj/"
    # 市级门户上的"省科技厅"是上级部门，不算杭州的
    hz_kj = depts[("330100", "科技")]
    assert hz_kj.site_url == "https://www.hangzhou.gov.cn/bm/kjj/"
    assert hz_kj.site_type == "门户频道"
    # 区县部门频道首页没有栏目时，沿"信息公开"再找一层
    xh_kj = depts[("330106", "科技")]
    assert [c.url for c in xh_kj.columns] == ["http://www.hzxh.gov.cn/bm/kjj/tzgg/"]
    assert depts[("330106", "发改委")].status == "已找到"
    assert depts[("330106", "商务")].status == "导航未找到"
    # 市县门户页脚里的部委不算本地部门
    assert depts[("330100", "生态环境")].status == "导航未找到"
    # 政务服务网的部门办事页不是部门网站
    assert depts[("330100", "商务")].status == "导航未找到"


def test_national_ministries_are_target_depts():
    assert dept_categories("科学技术部") == ["科技"]
    assert dept_categories("国家发展和改革委员会") == ["发改委"]
    assert dept_categories("农业农村部") == ["农业农村"]
    assert dept_categories("国家知识产权局") == ["市场监管"]
    assert dept_categories("国家数据局") == ["数据局"]
    assert dept_categories("外交部") == []


def test_list_api_only_accepts_whitelisted_same_site_endpoint():
    from youju.crawler.adapters import api_html, find_list_api

    good = """<script src="/x/unitbuild.js" url="/api-gateway/jpaas-publish-server/front/page/build/unit"
      queryData="{'pageType':'column','tagId':'当前栏目list','pageId':'1'}"></script>"""
    api = find_list_api(good, "https://kjt.zj.gov.cn/col/col1/index.html")
    assert api.url.startswith(
        "https://kjt.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit?"
    )
    assert api_html(api, '{"data":{"html":"<li>x</li>"}}') == "<li>x</li>"
    assert api_html(api, "not json") == ""
    other = good.replace("/api-gateway/jpaas-publish-server/front/page/build/unit", "/api/other")
    assert find_list_api(other, "https://kjt.zj.gov.cn/") is None
    # 页面声明到别的主机上的接口：不请求（也不改成请求本站同一路径）
    evil = good.replace('url="/api', 'url="https://evil.example.com/api')
    assert find_list_api(evil, "https://kjt.zj.gov.cn/") is None
    assert api_html(api, '{"data":"参数错误"}') == ""
    bad_query = good.replace("{'pageType'", "{['x']:'1','pageType'")
    assert find_list_api(bad_query, "https://kjt.zj.gov.cn/") is None


def test_complete_slots_adds_standard_depts_for_missing_portals():
    from youju.govsites.inventory import STANDARD_DEPTS, PortalRow, complete_slots

    portals = [
        PortalRow("330106", "区县级", "浙江", "杭州", "西湖区", "西湖区", status="导航未找到"),
        PortalRow(
            "330182",
            "区县级",
            "浙江",
            "杭州",
            "建德市",
            "建德市",
            status="不能抓",
            reason="瑞数防护",
        ),
    ]
    rows = complete_slots(portals, [])
    assert len(rows) == 2 * len(STANDARD_DEPTS)
    by_code = {(r.code, r.dept): r for r in rows}
    assert by_code[("330106", "科技")].status == "导航未找到"
    assert by_code[("330182", "科技")].status == "不能抓"
    assert "瑞数防护" in by_code[("330182", "科技")].reason


def test_county_branch_of_city_bureau_counts_for_the_county():
    from youju.govsites.inventory import _own_branch

    index = DivisionIndex(_divisions())
    xh = next(d for d in index.all if d.name == "西湖区")
    assert _own_branch("杭州市生态环境局西湖分局", xh)
    assert not _own_branch("杭州市生态环境局", xh)
    assert not _own_branch("杭州市生态环境局滨江分局", xh)
    assert dept_categories("杭州市生态环境局西湖分局") == ["生态环境"]


def test_board_states():
    from datetime import datetime, timedelta

    from youju.govsites.board import SourceState, cell_for, coverage_rate, rollup
    from youju.govsites.inventory import ColumnHit, DeptRow

    now = datetime(2026, 11, 5, 12)

    def row(*urls, status="已找到"):
        cols = [ColumnHit("通知公告", "通知公告", u, "") for u in urls]
        return DeptRow(
            "330106", "区县级", "浙江", "杭州", "西湖区", "科技", status=status, columns=cols
        )

    def src(url, ok=None, docs=0, err=None, vs=None, mode="static", last=None, http=None):
        return SourceState(url, False, mode, "unknown", ok, err, vs, docs, last, http)

    a, b = "http://a.gov.cn/1/", "http://a.gov.cn/2/"
    fresh = {
        a: src(a, now - timedelta(hours=5), 10),
        b: src(b, vs="网站防护拦截（瑞数防护，不绕过）", mode="blocked"),
    }
    c = cell_for(row(a, b), fresh, {}, now)
    assert (c.state, c.last_success_at) == ("已收录", now - timedelta(hours=5))
    assert "拒绝抓取" in c.reason
    stale = {a: src(a, now - timedelta(days=5), 10)}
    assert cell_for(row(a), stale, {}, now).state == "抓取失败"
    blocked_only = {b: fresh[b]}
    assert cell_for(row(b), blocked_only, {}, now).state == "不能抓"
    failed = src(a, err="HTTP 500", last="http_error", http=500)
    assert cell_for(row(a), {a: failed}, {}, now).state == "抓取失败"
    waf = src(a, err="HTTP 412", last="http_error", http=412)
    assert cell_for(row(a), {a: waf}, {}, now).state == "不能抓"
    robots = src(a, err="robots.txt 不允许抓取该页面", last="robots_disallowed")
    assert cell_for(row(a), {a: robots}, {}, now).state == "不能抓"
    empty = src(a, err="页面可以访问，但没有识别到列表条目", last="parse_empty")
    assert cell_for(row(a), {a: empty}, {}, now).state == "需人工"
    imported = src(a, vs="试跑可读（全国覆盖 v1，未启用）")
    assert cell_for(row(a), {a: imported}, {}, now).state == "待启用"
    script = src(a, vs="需浏览器渲染或接口（全国覆盖 v1）", mode="browser")
    assert cell_for(row(a), {a: script}, {}, now).state == "需人工"
    assert cell_for(row(), {}, {}, now).state == "网站无此栏目"
    assert cell_for(row(status="导航未找到"), {}, {}, now).state == "需人工"
    cells = [
        cell_for(row(a), fresh, {}, now),
        cell_for(row(), {}, {}, now),
        cell_for(row(b), blocked_only, {}, now),
    ]
    counter = rollup(cells)["区县级"]
    # 程序判的"网站无此栏目"确认前不从分母里扣
    assert coverage_rate(counter) == 1 / 2
    assert coverage_rate(counter, confirmed_no_column=1) == 1.0


def test_dept_names_with_service_or_work_words():
    assert dept_categories("宁波市政务服务和数据管理局") == ["数据局"]
    assert dept_categories("杭州经济技术开发区党工委（管理委员会）") == [
        "开发区"
    ] or dept_categories("杭州经济技术开发区管理委员会") == ["开发区"]
    assert dept_categories("某某高新区工作委员会") == ["开发区"]
    assert dept_categories("省政务服务中心") == []
    assert dept_categories("科技局工作动态") == []


def test_jpaas_category_tree_becomes_column_needing_api():
    from youju.crawler.adapters import jpaas_tree_categories
    from youju.govsites.verify import NEEDS_SCRIPT, ColumnCheck, check_one

    html = (
        'var treeStr = "[{#title#:#经信局#,#key#:#0#,#children#:[{#title#:#通知公告#,#key#:#52#,'
        '#children#:[]},{#title#:#政策文件及解读#,#key#:#33#}]}]";'
    )
    cats = jpaas_tree_categories(html)
    assert ("通知公告", "52") in cats and ("政策文件及解读", "33") in cats
    p = _page("http://www.dongyang.gov.cn/col/col1/index.html", "<p>部门信息公开</p>")
    p.categories = cats
    hits = find_columns([p])
    assert hits["通知公告"].url == "http://www.dongyang.gov.cn/col/col1/index.html#xxgk-52"
    assert hits["政策文件"].name == "政策文件及解读（信息公开分类）"
    col = ColumnCheck(
        "330783",
        "区县级",
        "浙江",
        "金华",
        "东阳市",
        "工信",
        "",
        "通知公告",
        "通知公告",
        hits["通知公告"].url,
        "",
        "",
        "",
    )
    assert check_one(FakeCache({}), col, date(2026, 11, 5)).result == NEEDS_SCRIPT


def test_jpaas_xxgk_groups_give_group_column_pages():
    from youju.crawler.adapters import jpaas_xxgk_groups

    html = (
        '<label class="label-className">政策法规</label><ul class="ajax-ul"><li>'
        '<a href="http://www.xiuzhou.gov.cn/col/col1229409716/art/2026/art_a1.html">某某措施</a></li></ul>'
        '<label class="label-className">通知公告</label><ul class="ajax-ul"><li>'
        '<a href="http://www.xiuzhou.gov.cn/art/2025/4/1/art_1229409846_25.html">某某通知</a></li></ul>'
    )
    groups = jpaas_xxgk_groups(html, "http://www.xiuzhou.gov.cn/col/col1229405616/index.html")
    assert groups == [
        ("政策法规", "http://www.xiuzhou.gov.cn/col/col1229409716/index.html"),
        ("通知公告", "http://www.xiuzhou.gov.cn/col/col1229409846/index.html"),
    ]


def test_shared_template_columns_are_dropped():
    from youju.govsites.inventory import ColumnHit, DeptRow, _drop_shared_columns

    def r(dept, site, *urls):
        cols = [ColumnHit("通知公告", "通知公告", u, site) for u in urls]
        return DeptRow(
            "330421",
            "区县级",
            "浙江",
            "嘉兴",
            "嘉善县",
            dept,
            site_url=site,
            status="已找到",
            columns=cols,
        )

    shared = "http://www.jiashan.gov.cn/col/col1229373860/index.html"
    rows = [
        r(
            "发改委",
            "http://www.jiashan.gov.cn/col/col1/index.html",
            shared,
            "http://www.jiashan.gov.cn/col/col11/index.html",
        ),
        r("科技", "http://www.jiashan.gov.cn/col/col2/index.html", shared),
        # 合并部门：同一网站算两类，栏目相同不算共用
        r("工信", "http://kjj.example.gov.cn/", "http://kjj.example.gov.cn/tzgg/"),
        r("科技", "http://kjj.example.gov.cn/", "http://kjj.example.gov.cn/tzgg/"),
    ]
    _drop_shared_columns(rows)
    assert [c.url for c in rows[0].columns] == ["http://www.jiashan.gov.cn/col/col11/index.html"]
    assert rows[1].columns == []
    assert rows[1].reason.startswith("部门页上只有本级共用的栏目")
    assert [c.url for c in rows[2].columns] == ["http://kjj.example.gov.cn/tzgg/"]


def test_refused_pages_are_not_refetched_by_retry(tmp_path):
    from youju.govsites.pagecache import PageCache

    class CountingFetcher:
        def __init__(self):
            self.calls = 0

        def fetch(self, url):
            self.calls += 1
            if "waf" in url:
                return FetchResult(url, "http_error", url, 412, "", "HTTP 412")
            return FetchResult(url, "network_error", url, None, "", "超时")

    f = CountingFetcher()
    cache = PageCache(tmp_path, f, fail_days=0)  # 相当于 --retry-failed
    cache.get("http://waf.example.gov.cn/")
    cache.get("http://slow.example.gov.cn/")
    cache.get("http://waf.example.gov.cn/")
    cache.get("http://slow.example.gov.cn/")
    # 412 拒绝过的不再请求；超时的照常重试
    assert f.calls == 3


def test_national_dev_zone_is_not_a_ministry():
    from youju.govsites.inventory import Inventory

    inv = Inventory(DivisionIndex(_divisions()), FakeCache({}))
    assert inv._is_ministry("国家发展和改革委员会", "https://www.ndrc.gov.cn/")
    assert not inv._is_ministry("国家级宁波经济技术开发区管理委员会", "https://www.netd.gov.cn/")
