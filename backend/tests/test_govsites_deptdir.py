"""部门目录规则：区县门户信息公开平台里的部门目录页、部门页上的栏目（大汉信息公开、山西县级平台、
数字广东），用假的页面缓存，写法照 11/9 实际看到的页面（长兴县、黎城县、始兴县）。"""

import json

from youju.crawler.adapters import find_unit_apis
from youju.crawler.fetcher import FetchResult
from youju.govsites.deptdir import (
    directory_score,
    gkmlpt_tree_categories,
    is_directory,
    jpaas_path_groups,
)
from youju.govsites.divisions import DivisionIndex, parse_tree
from youju.govsites.inventory import GOV_LOCAL_NAV, Inventory, PageInfo
from youju.govsites.links import page_links, parse_html


def _n(code: str, name: str, kind: str, level: int, children=None) -> dict:
    node = {"code": code + "000000", "name": name, "type": kind, "level": level}
    if children:
        node["children"] = children
    return node


TREE = {
    "children": [
        _n(
            "330000",
            "浙江省",
            "省",
            1,
            [_n("330500", "湖州市", "地级市", 2, [_n("330522", "长兴县", "县", 3)])],
        ),
        _n(
            "140000",
            "山西省",
            "省",
            1,
            [_n("140400", "长治市", "地级市", 2, [_n("140426", "黎城县", "县", 3)])],
        ),
        _n(
            "440000",
            "广东省",
            "省",
            1,
            [_n("440200", "韶关市", "地级市", 2, [_n("440222", "始兴县", "县", 3)])],
        ),
    ]
}
INDEX = DivisionIndex(parse_tree(TREE))


class FakeCache:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.hits = self.fetches = 0
        self.asked: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.asked.append(url)
        if url in self.pages:
            return FetchResult(url, "ok", url, 200, self.pages[url])
        return FetchResult(url, "http_error", url, 404, "", "HTTP 404")


def _page(url: str, html: str) -> PageInfo:
    doc = parse_html(html)
    return PageInfo(
        url, url, FetchResult(url, "ok", url, 200, html), doc, page_links(html, url, doc)
    )


def _depts(inv: Inventory) -> dict:
    return {(d.code, d.dept): d for d in inv.depts}


# —— 单个规则 ——


def test_directory_needs_enough_local_org_links():
    orgs = "".join(
        f'<a href="/bm/{i}/">·长兴县{name}</a>'
        for i, name in enumerate(
            ["发展和改革局", "科学技术局", "财政局", "教育局", "公安局", "民政局"]
        )
    )
    page = _page("http://www.zjcx.gov.cn/bmxxgk/", orgs + '<a href="http://x.gov.cn/">某某局</a>')
    assert directory_score(page, "http://www.zjcx.gov.cn/") == (6, 3)
    assert is_directory(page, "http://www.zjcx.gov.cn/")
    few = _page("http://www.zjcx.gov.cn/bmxxgk/", orgs[: orgs.index("教育局") - 30])
    assert not is_directory(few, "http://www.zjcx.gov.cn/")
    # 链到别的网站的机关名不算（页脚的部委、省直部门）
    assert not is_directory(page, "http://www.other.gov.cn/")


def test_jpaas_path_groups_restore_category_column_pages():
    html = """<label class="label-className">政策文件</label><ul>
      <li><a href="/xxgk/zfbmxxgk/kj/fdzdgknr/zcwj/xzbmfgfxwj/art/2026/art_a1.html">一</a></li>
      <li><a href="/xxgk/zfbmxxgk/kj/fdzdgknr/zcwj/xzbmfgfxwj/art/2025/art_a2.html">二</a></li>
      <li><a href="/xxgk/zfbmxxgk/kj/fdzdgknr/zcwj/gfxwj/art/2025/art_a3.html">三</a></li></ul>
      <label class="label-className">意见征集</label><ul>
      <li><a href="/api-gateway/jpaas-jsurvey-web-server/front/dczj/showJsurveys.do?formId=1">四</a></li></ul>
      <label class="label-className">人事信息</label><ul>
      <li><a href="http://other.gov.cn/rsxx/art/2025/art_b.html">五</a></li></ul>"""
    groups = jpaas_path_groups(html, "http://www.zjcx.gov.cn/xxgk/zfbmxxgk/kj/index.html")
    # 组里条目最多的分类目录；没有"分类目录/art/"条目的组、别的网站上的条目不还原
    assert groups == [
        (
            "政策文件",
            "http://www.zjcx.gov.cn/xxgk/zfbmxxgk/kj/fdzdgknr/zcwj/xzbmfgfxwj/index.html",
        )
    ]


def test_jpaas_groups_need_a_clear_majority_column():
    page = "http://www.gongshu.gov.cn/col/col1229461610/index.html"
    # 拱墅区写法："/col/栏目号/art/…"和"/art/年/月/日/art_栏目号_编号.html"混着，夹着共用的年报栏目
    mixed = """<label class="label-className">政策文件</label><ul>
      <li><a href="http://www.gongshu.gov.cn/col/col1229565827/art/2026/art_a.html">一</a></li>
      <li><a href="http://www.gongshu.gov.cn/art/2025/8/13/art_1229565827_1857766.html">二</a></li>
      <li><a href="http://www.gongshu.gov.cn/art/2025/6/23/art_1229565827_1852150.html">三</a></li>
      <li><a href="http://www.gongshu.gov.cn/col/col1229707689/art/2026/art_b.html">年报</a></li></ul>
      <label class="label-className">资金信息</label><ul>
      <li><a href="http://www.gongshu.gov.cn/col/col1229601867/art/2026/art_c.html">四</a></li>
      <li><a href="http://www.gongshu.gov.cn/col/col1229226098/art/2026/art_d.html">五</a></li></ul>"""
    assert jpaas_path_groups(mixed, page) == [
        ("政策文件", "http://www.gongshu.gov.cn/col/col1229565827/index.html")
    ]  # 3/4 来自同一栏目；资金信息两个栏目各一半，拿不准不还原


def test_gkmlpt_tree_categories_skip_jump_links():
    text = (
        "window._CONFIG = {\n SID: '751358',\n TREE: "
        + json.dumps(
            [
                {
                    "id": 3080,
                    "name": "政府信息公开指南",
                    "parent": 0,
                    "jump_url": None,
                    "children": [],
                },
                {"id": 3081, "name": "政府信息依申请公开", "jump_url": "https://zwgk.sg.gov.cn/x"},
                {
                    "id": 3072,
                    "name": "机构职能",
                    "jump_url": "",
                    "children": [{"id": 3076, "name": "通知公告", "jump_url": None}],
                },
            ]
        )
        + ",\n DETAIL: null }"
    )
    assert gkmlpt_tree_categories(text) == [
        ("政府信息公开指南", "gkmlpt-3080"),
        ("机构职能", "gkmlpt-3072"),
        ("通知公告", "gkmlpt-3076"),
    ]
    assert gkmlpt_tree_categories("<p>没有分类树</p>") == []


# —— 走一遍：浙江大汉信息公开（长兴县写法）——

JPAAS_DEPT = "http://www.zjcx.gov.cn/xxgk/zfbmxxgk/zxxkxjsj/index.html?number=K001"
UNIT_TAG = (
    '<script src="/cms_files/default/script/AuthorizedRead/unitbuild.js" webId="3645" '
    'url="/api-gateway/jpaas-publish-server/front/page/build/unit" '
    "queryData=\"{'parseType':'bulidstatic','webId':'3645','tplSetId':'x','pageType':'column',"
    "'tagId':'组配分类list','editType':'null','pageId':'p1'}\"></script>"
)


def _zhejiang_pages() -> dict[str, str]:
    # 显示的名字被截断，全称在 title 里（长兴县实际页面就是这样）
    dir_links = (
        '<a href="/xxgk/zfbmxxgk/zxxswj/index.html?number=K001" '
        'title="长兴县商务局(粮食和物资储备局、投资促进局)">·长兴县商务局(粮食和物资储备局、投资促进...</a>'
    )
    dir_links += "".join(
        f'<a href="http://www.zjcx.gov.cn/xxgk/zfbmxxgk/{abbr}/index.html?number=K001">·长兴县{name}</a>'
        for abbr, name in [
            ("zxxfzhggj", "发展和改革局（长兴县人民政府区域合..."),
            ("zxxkxjsj", "科学技术局"),
            ("zxxczj", "财政局(国资办)"),
            ("zxxjyj", "教育局"),
            ("zxxgaj", "公安局"),
            ("zxxmzj", "民政局"),
            ("zxxsfj", "司法局"),
        ]
    )
    unit_html = """<label class="label-className">政策文件</label><ul class="ajax-ul">
      <li><a href="http://www.zjcx.gov.cn/xxgk/zfbmxxgk/zxxkxjsj/fdzdgknr/zcwj/xzbmfgfxwj/art/2026/art_e6.html">长科〔2026〕4号</a><span>2026-09-20</span></li>
      <li><a href="http://www.zjcx.gov.cn/xxgk/zfbmxxgk/zxxkxjsj/fdzdgknr/zcwj/xzbmfgfxwj/art/2025/art_14.html">创新券办法</a><span>2024-10-12</span></li>
      </ul>"""
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.zj.gov.cn/">浙江</a>',
        "https://www.zj.gov.cn/": '<a href="/sxwz/">市县政府网站</a>',
        "https://www.zj.gov.cn/sxwz/": '<a href="https://www.huzhou.gov.cn/">湖州市</a>',
        "https://www.huzhou.gov.cn/": '<a href="/qxwz/">区县政府网站</a>',
        "https://www.huzhou.gov.cn/qxwz/": '<a href="http://www.zjcx.gov.cn/">长兴县</a>',
        "http://www.zjcx.gov.cn/": """<meta name="SiteName" content="长兴县人民政府">
            <a href="/col/col1229211282/index.html">政务公开</a>""",
        "http://www.zjcx.gov.cn/col/col1229211282/index.html": """
            <a href="/col/col1229518369/index.html">政府信息公开目录</a>
            <a href="/col/col1229211332/index.html">部门信息公开</a>""",
        "http://www.zjcx.gov.cn/col/col1229211332/index.html": dir_links,
        JPAAS_DEPT: '<meta name="SiteName" content="长兴县人民政府">' + UNIT_TAG,
    }
    api = find_unit_apis(pages[JPAAS_DEPT], JPAAS_DEPT)[0].url
    pages[api] = json.dumps({"data": {"html": unit_html}})
    return pages


def test_zhejiang_county_departments_from_jpaas_directory():
    inv = Inventory(
        INDEX, FakeCache(_zhejiang_pages()), provinces=["浙江"], workers=2, per_province=2
    )
    inv.run()
    directory = "http://www.zjcx.gov.cn/col/col1229211332/index.html"
    assert inv.directories["330522"] == [directory]
    depts = _depts(inv)
    kj = depts[("330522", "科技")]
    assert (kj.status, kj.site_url, kj.nav_source) == ("已找到", JPAAS_DEPT, directory)
    assert kj.dept_full_name == "长兴县科学技术局"
    assert [(c.kind, c.url) for c in kj.columns] == [
        (
            "政策文件",
            "http://www.zjcx.gov.cn/xxgk/zfbmxxgk/zxxkxjsj/fdzdgknr/zcwj/xzbmfgfxwj/index.html",
        )
    ]
    # 名称被截断（括号没合上）的也认；title 里有全称就记全称
    assert depts[("330522", "发改委")].status == "打不开"  # 假页面里没有这个部门页
    sw = depts[("330522", "商务")]
    assert sw.nav_text == "长兴县商务局(粮食和物资储备局、投资促进局)"
    assert depts[("330522", "财政")].site_url.endswith("/zxxczj/index.html?number=K001")


# —— 走一遍：山西县级平台（黎城县写法，部门网址 5 层深、先跳到"公开指南"）——


def _shanxi_pages() -> dict[str, str]:
    base = "http://www.sxlc.gov.cn/lczw/zfxxgk/bmxxgk/zfzcbm"
    names = [
        ("fzhggj_1", "黎城县发改工信和科技商务局"),
        ("czj_2", "黎城县财政局"),
        ("jyj_3", "黎城县教育体育局"),
        ("gaj_4", "黎城县公安局"),
        ("sfj_5", "黎城县司法局"),
        ("rlzy_6", "黎城县民政和人力资源社会保障局"),
        ("hbj_7", "市生态环境局黎城分局"),
    ]
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.shanxi.gov.cn/">山西</a>',
        "https://www.shanxi.gov.cn/": '<a href="/sxwz/">市县政府网站</a>',
        "https://www.shanxi.gov.cn/sxwz/": '<a href="http://www.sxlc.gov.cn/">黎城县</a>',
        "http://www.sxlc.gov.cn/": """<meta name="SiteName" content="黎城县人民政府门户网站">
            <a href="/zwgk/">政务公开</a><a href="/sxwz/czj/">财政局专栏</a>""",
        # 只有一句跳转的页面：盘点跟过去
        "http://www.sxlc.gov.cn/zwgk/": "<script>\n  location.replace('../lczw/');\n</script>",
        "http://www.sxlc.gov.cn/lczw/": "".join(f'<a href="{base}/{a}/">{n}</a>' for a, n in names),
    }
    for abbr, _name in names:
        pages[f"{base}/{abbr}/"] = (
            f"<script>\n  window.location.replace('./gkzn_{abbr}/index.html');\n</script>"
        )
        pages[f"{base}/{abbr}/gkzn_{abbr}/index.html"] = (
            f'<meta name="SiteName" content="黎城县人民政府官方网站">'
            f'<a href="{base}/{abbr}/gknr/">法定主动公开内容</a>'
            f'<a href="{base}/{abbr}/gknr/gzdt_1/">工作动态</a>'
        )
    # 财政局另有"通知公告"
    pages[f"{base}/czj_2/gkzn_czj_2/index.html"] += (
        f'<a href="{base}/czj_2/gknr/tzgg_9/">通知公告</a>'
    )
    return pages


def test_shanxi_county_departments_from_disclosure_home():
    inv = Inventory(
        INDEX, FakeCache(_shanxi_pages()), provinces=["山西"], workers=2, per_province=2
    )
    inv.run()
    assert inv.directories["140426"] == ["http://www.sxlc.gov.cn/lczw/"]
    depts = _depts(inv)
    base = "http://www.sxlc.gov.cn/lczw/zfxxgk/bmxxgk/zfzcbm"
    cz = depts[("140426", "财政")]
    assert cz.status == "已找到"
    assert cz.site_url == f"{base}/czj_2/gkzn_czj_2/index.html"  # 跟了跳转
    assert [c.url for c in cz.columns] == [f"{base}/czj_2/gknr/tzgg_9/"]
    # 合并部门一个网址算几类；部门在目录里但没有三类栏目的，照实记"已找到、无栏目"
    for dept in ("发改委", "工信", "科技", "商务"):
        assert depts[("140426", dept)].site_url == f"{base}/fzhggj_1/gkzn_fzhggj_1/index.html"
        assert depts[("140426", dept)].columns == []
    assert depts[("140426", "人社")].status == "已找到"
    assert depts[("140426", "生态环境")].status == "已找到"


def test_deep_department_links_only_count_on_directory_pages():
    pages = _shanxi_pages()
    # 不是目录页（机关名链接不够）上的深网址照旧不认
    pages["http://www.sxlc.gov.cn/lczw/"] = (
        '<a href="http://www.sxlc.gov.cn/lczw/zfxxgk/bmxxgk/zfzcbm/czj_2/">黎城县财政局</a>'
    )
    inv = Inventory(INDEX, FakeCache(pages), provinces=["山西"], workers=2, per_province=2)
    inv.run()
    assert "140426" not in inv.directories
    assert _depts(inv)[("140426", "财政")].status == "导航未找到"


# —— 走一遍：数字广东（始兴县写法）——


def _guangdong_pages() -> dict[str, str]:
    codes = [
        ("sgsxfgj", "始兴县发展和改革局"),
        ("sgsxgyj", "始兴县工业和信息化局"),
        ("sgsxczj", "始兴县财政局"),
        ("sgsxmzj", "始兴县民政局"),
        ("sgsxjyj", "始兴县教育局"),
        ("sgsxgaj", "始兴县公安局"),
        ("sgsxsfj", "始兴县司法局"),
    ]
    tree = json.dumps(
        [
            {"id": 3080, "name": "政府信息公开指南", "jump_url": None, "children": []},
            {"id": 3076, "name": "通知公告", "jump_url": None, "children": []},
            {"id": 3078, "name": "财政预决算", "jump_url": None, "children": []},
        ]
    )
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.gd.gov.cn/">广东</a>',
        "https://www.gd.gov.cn/": '<a href="/sxwz/">地级以上市政府网站</a>',
        "https://www.gd.gov.cn/sxwz/": '<a href="https://www.sg.gov.cn/">韶关市</a>',
        "https://www.sg.gov.cn/": '<a href="/qxwz/">县（市、区）政府网站</a>',
        "https://www.sg.gov.cn/qxwz/": '<a href="https://www.gdsx.gov.cn/">始兴县</a>',
        "https://www.gdsx.gov.cn/": """<meta name="SiteName" content="始兴县政府门户网站">
            <meta name="SiteIDCode" content="4400000001">
            <a href="https://www.gdsx.gov.cn/gkmlzl.html">信息公开部门目录</a>""",
        "https://www.gdsx.gov.cn/gkmlzl.html": "".join(
            f'<a href="https://www.gdsx.gov.cn/{c}/gkmlpt/index">{n}</a>' for c, n in codes
        ),
    }
    for c, n in codes:
        pages[f"https://www.gdsx.gov.cn/{c}/gkmlpt/index"] = (
            f'<meta name="SiteName" content="韶关市{n}">'
            f"<script>window._CONFIG = {{\n SID: '751358',\n TREE: {tree},\n DETAIL: null }}</script>"
        )
    return pages


def test_guangdong_county_departments_from_gkmlpt_directory():
    inv = Inventory(
        INDEX, FakeCache(_guangdong_pages()), provinces=["广东"], workers=2, per_province=2
    )
    inv.run()
    assert inv.directories["440222"] == ["https://www.gdsx.gov.cn/gkmlzl.html"]
    fg = _depts(inv)[("440222", "发改委")]
    assert fg.status == "已找到" and fg.dept_full_name == "韶关市始兴县发展和改革局"
    # 门户首页带的是省里的标识码（广东集约化），部门页在本门户主机上：不因此判成上级
    assert [(c.kind, c.url) for c in fg.columns] == [
        ("通知公告", "https://www.gdsx.gov.cn/sgsxfgj/gkmlpt/index#xxgk-gkmlpt-3076")
    ]
