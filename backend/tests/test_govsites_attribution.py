"""全国覆盖盘点的归属判断：区县门户上的上级、别处、外省部门链接不算本地部门；门户认错的否掉。

例子都来自 11/8 全国清单里查出的误配（康定市"财政厅"、新城区"发展和改革委员会"、濂溪区"科技局"、
应城市"湖北商务厅"、天津河北区认成河北省、南通区县认成南通市门户、许昌区县认成政务服务网）。
"""

from youju.crawler.fetcher import FetchResult
from youju.govsites.attribution import (
    name_verdict,
    places_for,
    portal_verdict,
    site_verdict,
    title_names,
)
from youju.govsites.divisions import DivisionIndex, parse_tree
from youju.govsites.inventory import GOV_LOCAL_NAV, Inventory, complete_slots
from youju.govsites.names import dept_categories


def _n(code: str, name: str, kind: str, level: int, children=None) -> dict:
    node = {"code": code + "000000", "name": name, "type": kind, "level": level}
    if children:
        node["children"] = children
    return node


TREE = {
    "children": [
        _n(
            "510000",
            "四川省",
            "省",
            1,
            [_n("513300", "甘孜藏族自治州", "自治州", 2, [_n("513301", "康定市", "县级市", 3)])],
        ),
        _n(
            "150000",
            "内蒙古自治区",
            "自治区",
            1,
            [
                _n("150100", "呼和浩特市", "地级市", 2, [_n("150102", "新城区", "市辖区", 3)]),
                _n("150500", "通辽市", "地级市", 2, [_n("150502", "科尔沁区", "市辖区", 3)]),
            ],
        ),
        _n(
            "420000",
            "湖北省",
            "省",
            1,
            [_n("420900", "孝感市", "地级市", 2, [_n("420981", "应城市", "县级市", 3)])],
        ),
        _n(
            "360000",
            "江西省",
            "省",
            1,
            [_n("360400", "九江市", "地级市", 2, [_n("360402", "濂溪区", "市辖区", 3)])],
        ),
        _n(
            "120000",
            "天津市",
            "直辖市",
            1,
            [_n("120101", "和平区", "市辖区", 3), _n("120105", "河北区", "市辖区", 3)],
        ),
        _n(
            "130000",
            "河北省",
            "省",
            1,
            [
                _n(
                    "130800",
                    "承德市",
                    "地级市",
                    2,
                    [_n("130802", "双桥区", "市辖区", 3), _n("130821", "承德县", "县", 3)],
                )
            ],
        ),
        _n(
            "320000",
            "江苏省",
            "省",
            1,
            [_n("320600", "南通市", "地级市", 2, [_n("320612", "通州区", "市辖区", 3)])],
        ),
        _n(
            "410000",
            "河南省",
            "省",
            1,
            [_n("411000", "许昌市", "地级市", 2, [_n("411002", "魏都区", "市辖区", 3)])],
        ),
        _n(
            "140000",
            "山西省",
            "省",
            1,
            [_n("140400", "长治市", "地级市", 2, [_n("140426", "黎城县", "县", 3)])],
        ),
        _n(
            "630000",
            "青海省",
            "省",
            1,
            [_n("632500", "海南藏族自治州", "自治州", 2, [_n("632521", "共和县", "县", 3)])],
        ),
        _n("460000", "海南省", "省", 1, [_n("469005", "文昌市", "县级市", 3)]),
        _n("110000", "北京市", "直辖市", 1),
        _n(
            "220000",
            "吉林省",
            "省",
            1,
            [
                _n("220200", "吉林市", "地级市", 2, [_n("220202", "昌邑区", "市辖区", 3)]),
                _n("220500", "通化市", "地级市", 2, [_n("220582", "集安市", "县级市", 3)]),
            ],
        ),
    ]
}

INDEX = DivisionIndex(parse_tree(TREE))
BY_NAME = {d.name: d for d in INDEX.all}


def _places(name: str):
    return places_for(INDEX, BY_NAME[name])


def _kind(text: str, where: str) -> str:
    return name_verdict(text, _places(where)).kind


class FakeCache:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.hits = self.fetches = 0

    def get(self, url: str) -> FetchResult:
        if url in self.pages:
            return FetchResult(url, "ok", url, 200, self.pages[url])
        return FetchResult(url, "http_error", url, 404, "", "HTTP 404")


def _run(pages: dict[str, str], provinces: list[str]) -> Inventory:
    inv = Inventory(INDEX, FakeCache(pages), provinces=provinces, workers=2, per_province=2)
    inv.run()
    return inv


def _depts(inv: Inventory) -> dict[tuple[str, str], object]:
    return {(d.code, d.dept): d for d in inv.depts}


# —— 名称 ——


def test_local_places_do_not_include_the_province_short_name():
    assert "湖北" not in _places("应城市").own
    assert "湖北" in _places("应城市").ancestors
    assert "湖北" in _places("湖北省").own
    assert _places("康定市").ancestor_codes == {"513300", "510000"}
    assert _places("河北区").ancestor_codes == {"120000"}


def test_name_verdict_upper_level_names():
    assert _kind("财政厅", "康定市") == "上级"  # 省级部门才叫厅
    assert _kind("省科技厅", "康定市") == "上级"
    assert _kind("四川省财政厅", "康定市") == "上级"
    assert _kind("甘孜州财政局", "康定市") == "上级"
    assert _kind("呼和浩特市发展和改革委员会", "新城区") == "上级"
    assert _kind("湖北商务厅", "应城市") == "上级"
    assert _kind("九江经济技术开发区管理委员会", "濂溪区") == "上级"
    assert _kind("国家级九江经济技术开发区管理委员会", "濂溪区") == "上级"
    assert _kind("海南州科技局", "共和县") == "上级"
    assert _kind("吉林市科技局", "昌邑区") == "上级"
    assert _kind("吉林省科技厅", "吉林市") == "上级"
    assert _kind("吉林科技厅", "吉林市") == "上级"  # 市县没有厅
    assert _kind("市生态环境局（新城分局）", "新城区") == "本级"  # 括号里写的分局
    assert _kind("自治区发展和改革委员会", "新城区") == "上级"


def test_name_verdict_local_names():
    assert _kind("四川省康定市财政局", "康定市") == "本级"
    assert _kind("内蒙古自治区通辽市科尔沁区财政局", "科尔沁区") == "本级"
    assert _kind("市财政局", "康定市") == "本级"  # 县级市自称"市"
    assert _kind("区科技局", "濂溪区") == "本级"
    assert _kind(">·濂溪区科学技术局", "濂溪区") == "本级"
    assert _kind("共和县科技局", "共和县") == "本级"
    assert _kind("海南州科技局", "海南藏族自治州") == "本级"
    assert _kind("吉林市科技局", "吉林市") == "本级"  # 本地写法"吉林"不当成省的
    assert _kind("海南省科学技术厅", "海南省") == "本级"
    # 市局派出到县的分局算县里的部门，开头的"市"不能把它排掉
    assert _kind("市生态环境局黎城分局", "黎城县") == "本级"
    assert _kind("长治市生态环境局黎城分局", "黎城县") == "本级"


def test_name_verdict_elsewhere_and_other_provinces():
    assert _kind("北京市科学技术委员会", "海南省") == "外省"
    assert _kind("中国（北京）自由贸易试验区", "海南省") == "外省"
    assert _kind("海南省科技厅", "海南藏族自治州") == "外省"
    assert _kind("河北工业和信息化厅", "河北区") == "外省"  # 简称和外省同名、又是"厅"
    assert _kind("河北区科技局", "河北区") == "本级"
    assert _kind("海南科技局", "海南藏族自治州") == "本级"
    assert _kind("通辽市财政局", "新城区") == "别处"
    assert _kind("科尔沁区财政局", "新城区") == "别处"
    assert _kind("市财政局", "濂溪区") == "上级"  # 区里写"市"是上级
    assert _kind("科技局", "濂溪区") == "不确定"  # 说不清的照常认，交给网站本身再判断


def test_service_hall_is_not_a_department():
    assert dept_categories("濂溪区政务服务数据公开大厅") == []
    assert dept_categories("区人力社保局") == ["人社"]
    assert dept_categories("长兴县文化和广电旅游体育局") == ["文旅"]
    assert dept_categories("区市监局") == ["市场监管"]
    assert dept_categories("黎城县农业农村和水利局") == ["农业农村"]
    assert dept_categories("长兴县农业农村水利局") == ["农业农村"]
    # 11/8 清单里已认出的写法不能回退（秦都区、仲巴县）
    assert dept_categories("区农业农村局和水利局") == ["农业农村"]
    assert "农业农村" in dept_categories("农业农村局和科技水利局（县乡村振兴局）")
    assert dept_categories("县农业农村和乡村振兴局") == ["农业农村"]
    assert dept_categories("浙江省农业科学院") == []
    assert dept_categories("县农业机械化服务中心") == []
    assert dept_categories("县农业机械管理局") == []  # 不是"农业农村和……局"这种合并部门
    # 括号里加挂的牌子（柯城区"经信局（商务局）"）：正名照算，牌子另给，盘点当备用候选
    from youju.govsites.names import alias_categories

    assert dept_categories("经信局（商务局）") == ["工信"]
    assert alias_categories("经信局（商务局）") == ["商务"]
    assert alias_categories("柯城区工业和信息化局（科学技术局）") == ["科技"]
    assert alias_categories("长兴县财政局(国资办)") == []
    assert alias_categories("长兴县商务局(粮食和物资储备局、投资促进局)") == []
    assert alias_categories("市场监督管理局（知识产权局）") == []
    assert alias_categories("县人民政府办公室（县外事办）") == []
    assert alias_categories("县经信局（县投资促进中心）") == ["商务"]
    # "生态环境局长兴分局"里的"局长"不是职务（长兴、长沙、长治……打头的分局）
    assert dept_categories("湖州市生态环境局长兴分局") == ["生态环境"]
    assert dept_categories("长沙市生态环境局长沙县分局") == ["生态环境"]
    assert dept_categories("局长信箱") == []
    assert dept_categories("市场监管局局长带队检查") == []


# —— 部门网站本身 ——


def _site(names, code, url, where, **kw):
    kw.setdefault("own_portal", "")
    return site_verdict(names, code, [url], _places(where), INDEX, **kw)


def test_site_verdict_prefers_the_site_name():
    v = _site(["呼和浩特市发展和改革委员会"], "", "http://fgw.huhhot.gov.cn/", "新城区")
    assert (v.kind, v.basis) == ("上级", "名称")
    v = _site(title_names("首页 - 呼和浩特市发展和改革委员会"), "", "http://x.gov.cn/", "新城区")
    assert v.kind == "上级"
    v = _site(["韶关市始兴县发展和改革局"], "", "http://x.gov.cn/", "新城区")
    assert v.kind == "不确定"  # 和本省无关的名字不认作任何一方


def test_site_verdict_code_and_domain():
    lianxi = {
        "own_portal": "http://www.lianxi.gov.cn/",
        "upper_portals": ["http://www.jiujiang.gov.cn/"],
    }
    v = _site(["首页"], "", "http://kjj.jiujiang.gov.cn/", "濂溪区", **lianxi)
    assert (v.kind, v.basis) == ("上级", "域名")
    v = _site([], "", "http://kjj.lianxi.gov.cn/", "濂溪区", **lianxi)
    assert v.kind == "不确定"
    # 标识码：前 6 位是区划代码
    assert _site([], "1501000003", "http://a.gov.cn/", "新城区").kind == "上级"
    assert _site([], "1501020005", "http://a.gov.cn/", "新城区").kind == "本级"
    assert _site([], "1300000013", "http://a.gov.cn/", "河北区").kind == "外省"
    assert _site([], "4440240002", "http://a.gov.cn/", "文昌市").kind == "不确定"  # 代码不存在
    assert _site([], "11411000005747138B", "http://a.gov.cn/", "魏都区").kind == "不确定"


def test_site_verdict_portal_channels_and_shared_parent_domain():
    # 本地门户里的部门频道：网页带的是门户的标识码（广东集约化门户首页带省里的），不往下猜
    v = _site([], "4400000001", "https://www.wenchang.gov.cn/bm/fgj/", "文昌市",
              own_portal="https://www.wenchang.gov.cn/")  # fmt: skip
    assert v.kind == "不确定"
    # 海南的市县门户在 hainan.gov.cn 下，本地和省门户同一个域名：域名规则不触发
    v = _site([], "", "http://dost.hainan.gov.cn/", "文昌市",
              own_portal="http://wenchang.hainan.gov.cn/",
              upper_portals=["https://www.hainan.gov.cn/"])  # fmt: skip
    assert v.kind == "不确定"


def test_portal_verdict_only_rejects_clear_mistakes():
    hebei = _places("河北区")
    v = portal_verdict(
        ["河北省人民政府"], "1300000013", "http://www.hebei.gov.cn/", hebei, INDEX, set()
    )
    assert v.kind == "外省"
    v = portal_verdict(
        ["天津市河北区人民政府"], "1300000013", "http://x.gov.cn/", hebei, INDEX, set()
    )
    assert v.kind == "不确定"  # 标识码乱写但网站名是本地的：照常认
    v = portal_verdict([], "", "https://www.nantong.gov.cn/", _places("通州区"), INDEX,
                       {"www.nantong.gov.cn"})  # fmt: skip
    assert v.kind == "上级"


# —— 走一遍导航 ——


def _sichuan_pages(kangding_bm: str) -> dict[str, str]:
    return {
        GOV_LOCAL_NAV: '<a href="https://www.sc.gov.cn/">四川</a>',
        "https://www.sc.gov.cn/": """<meta name="SiteName" content="四川省人民政府">
            <a href="/szwz/">市州政府网站</a><a href="/bmwz/">省直部门网站</a>""",
        "https://www.sc.gov.cn/szwz/": '<a href="https://www.gzz.gov.cn/">甘孜州</a>',
        "https://www.sc.gov.cn/bmwz/": '<a href="http://czt.sc.gov.cn/">四川省财政厅</a>',
        "http://czt.sc.gov.cn/": """<meta name="SiteName" content="四川省财政厅">
            <a href="/tzgg/">通知公告</a>""",
        "https://www.gzz.gov.cn/": """<meta name="SiteName" content="甘孜藏族自治州人民政府">
            <a href="/qxwz/">县（市）政府网站</a>""",
        "https://www.gzz.gov.cn/qxwz/": '<a href="https://www.kangding.gov.cn/">康定市</a>',
        "https://www.kangding.gov.cn/": """<meta name="SiteName" content="康定市人民政府">
            <a href="/bmwz/">部门网站</a>""",
        "https://www.kangding.gov.cn/bmwz/": kangding_bm,
        "https://www.kangding.gov.cn/czj/": """<meta name="SiteName" content="康定市人民政府">
            <a href="/czj/tzgg/">通知公告</a>""",
    }


def test_county_link_to_province_department_is_not_the_county_department():
    pages = _sichuan_pages(
        '<a href="http://czt.sc.gov.cn/">财政厅</a><a href="http://czt.sc.gov.cn/">财政局</a>'
    )
    depts = _depts(_run(pages, ["四川"]))
    assert depts[("510000", "财政")].site_url == "http://czt.sc.gov.cn/"  # 省里的照常认
    kd = depts[("513301", "财政")]
    assert kd.status == "导航未找到" and kd.site_url == ""
    assert "指向别处" in kd.reason and "四川省财政厅" in kd.reason
    # 本地自己的财政局频道在，就取它
    pages = _sichuan_pages('<a href="http://czt.sc.gov.cn/">财政局</a><a href="/czj/">市财政局</a>')
    kd = _depts(_run(pages, ["四川"]))[("513301", "财政")]
    assert kd.site_url == "https://www.kangding.gov.cn/czj/"
    assert [c.url for c in kd.columns] == ["https://www.kangding.gov.cn/czj/tzgg/"]


def _jiangxi_pages(city_kj: bool, city_kfq: bool) -> dict[str, str]:
    city_links = '<a href="/qxwz/">县（市、区）政府网站</a><a href="/bmwz/">市直部门网站</a>'
    city_bm = ""
    if city_kj:
        city_bm += '<a href="http://kjj.jiujiang.gov.cn/">九江市科学技术局</a>'
    if city_kfq:
        city_bm += '<a href="http://jkq.jiujiang.gov.cn/">九江经济技术开发区管理委员会</a>'
    return {
        GOV_LOCAL_NAV: '<a href="https://www.jiangxi.gov.cn/">江西</a>',
        "https://www.jiangxi.gov.cn/": """<meta name="SiteName" content="江西省人民政府">
            <a href="/sxwz/">设区市政府网站</a>""",
        "https://www.jiangxi.gov.cn/sxwz/": '<a href="http://www.jiujiang.gov.cn/">九江市</a>',
        "http://www.jiujiang.gov.cn/": '<meta name="SiteName" content="九江市人民政府">'
        + city_links,
        "http://www.jiujiang.gov.cn/qxwz/": '<a href="http://www.lianxi.gov.cn/">濂溪区</a>',
        "http://www.jiujiang.gov.cn/bmwz/": city_bm,
        "http://kjj.jiujiang.gov.cn/": "<title>首页</title><a href='/tzgg/'>通知公告</a>",
        "http://jkq.jiujiang.gov.cn/": """<meta name="SiteName" content="九江经济技术开发区管理委员会">
            <a href="/tzgg/">通知公告</a>""",
        "http://www.lianxi.gov.cn/": """<meta name="SiteName" content="濂溪区人民政府">
            <a href="/bmwz/">部门网站</a>""",
        "http://www.lianxi.gov.cn/bmwz/": """<a href="http://kjj.jiujiang.gov.cn/">科技局</a>
            <a href="http://jkq.jiujiang.gov.cn/">九江经济技术开发区管理委员会</a>
            <a href="http://www.lianxi.gov.cn/ZUIioPpKQ">濂溪区政务服务数据公开大厅</a>""",
    }


def test_county_link_to_city_department_on_city_domain():
    # 九江市认没认这个网址、谁先跑完都一样：网站在九江市门户的域名下、不在濂溪区的域名下
    for city_kj in (True, False):
        depts = _depts(_run(_jiangxi_pages(city_kj=city_kj, city_kfq=False), ["江西"]))
        lx = depts[("360402", "科技")]
        assert lx.status == "导航未找到" and "域名" in lx.reason
    assert ("360402", "数据局") not in depts  # 政务服务大厅不是数据局


def test_dev_zone_on_city_domain_is_not_the_county_one():
    for city_kfq in (False, True):
        depts = _depts(_run(_jiangxi_pages(city_kj=False, city_kfq=city_kfq), ["江西"]))
        assert ("360402", "开发区") not in depts
    assert depts[("360400", "开发区")].site_url == "http://jkq.jiujiang.gov.cn/"  # 九江市自己的
    # 开发区以地市名打头、网站不在地市域名下：县里的照常留（名称判不了归属）
    pages = _jiangxi_pages(city_kj=False, city_kfq=False)
    pages["http://www.lianxi.gov.cn/bmwz/"] = (
        '<a href="http://www.jjkfq.gov.cn/">九江经济技术开发区管理委员会</a>'
    )
    pages["http://www.jjkfq.gov.cn/"] = '<a href="/tzgg/">通知公告</a>'
    depts = _depts(_run(pages, ["江西"]))
    assert depts[("360402", "开发区")].site_url == "http://www.jjkfq.gov.cn/"


def test_upper_level_name_link_with_city_name_is_skipped():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.nmg.gov.cn/">内蒙古</a>',
        "https://www.nmg.gov.cn/": '<a href="/msdh/">盟市政府网站</a>',
        "https://www.nmg.gov.cn/msdh/": '<a href="http://www.huhhot.gov.cn/">呼和浩特市</a>',
        "http://www.huhhot.gov.cn/": '<a href="/qxdh/">旗县区政府网站</a>',
        "http://www.huhhot.gov.cn/qxdh/": '<a href="http://www.xinchengqu.gov.cn/">新城区</a>',
        "http://www.xinchengqu.gov.cn/": """<meta name="SiteName" content="新城区人民政府">
            <a href="/jgzl/">政府机构</a>""",
        "http://www.xinchengqu.gov.cn/jgzl/": """<a href="http://fgw.huhhot.gov.cn/">发展和改革委员会</a>
            <a href="http://wlt.nmg.gov.cn/">文化和旅游厅</a>""",
        "http://fgw.huhhot.gov.cn/": """<meta name="SiteName" content="呼和浩特市发展和改革委员会">
            <a href="/tzgg/">通知公告</a>""",
        "http://wlt.nmg.gov.cn/": '<meta name="SiteName" content="内蒙古自治区文化和旅游厅">',
    }
    depts = _depts(_run(pages, ["内蒙古"]))
    fg = depts[("150102", "发改委")]
    assert fg.status == "导航未找到" and "呼和浩特市发展和改革委员会" in fg.reason
    assert depts[("150102", "文旅")].site_url == ""  # "厅"在区里直接不当候选


def test_province_short_name_no_longer_scores_for_county():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.hubei.gov.cn/">湖北</a>',
        "https://www.hubei.gov.cn/": '<a href="/sxwz/">市州政府网站</a>',
        "https://www.hubei.gov.cn/sxwz/": '<a href="http://www.xiaogan.gov.cn/">孝感市</a>',
        "http://www.xiaogan.gov.cn/": '<a href="/xsq/">县市区政府网站</a>',
        "http://www.xiaogan.gov.cn/xsq/": '<a href="http://www.yingcheng.gov.cn/">应城市</a>',
        "http://www.yingcheng.gov.cn/": '<a href="/bm/">部门网站</a>',
        "http://www.yingcheng.gov.cn/bm/": '<a href="http://swt.hubei.gov.cn/">湖北商务厅</a>',
        "http://swt.hubei.gov.cn/": '<meta name="SiteName" content="湖北省商务厅">',
    }
    depts = _depts(_run(pages, ["湖北"]))
    assert depts[("420981", "商务")].site_url == ""


def test_tianjin_hebei_district_is_not_hebei_province():
    base = {
        GOV_LOCAL_NAV: '<a href="https://www.tj.gov.cn/">天津</a>',
        "http://www.hebei.gov.cn/": """<meta name="SiteName" content="河北省人民政府">
            <meta name="SiteIDCode" content="1300000013">
            <a href="https://kjt.hebei.gov.cn/">河北省科学技术厅</a>""",
        "https://kjt.hebei.gov.cn/": '<a href="/tzgg/">通知公告</a>',
        "http://www.tjhbq.gov.cn/": '<meta name="SiteName" content="天津市河北区人民政府">',
    }
    # 首页上的兄弟省份"河北"不认作河北区；区县导航里的"河北区"照常认
    pages = dict(base)
    pages["https://www.tj.gov.cn/"] = """<a href="http://www.hebei.gov.cn/">河北</a>
        <a href="/qxwz/">区县政府网站</a>"""
    pages["https://www.tj.gov.cn/qxwz/"] = '<a href="http://www.tjhbq.gov.cn/">河北区</a>'
    portals = {p.name: p for p in _run(pages, ["天津"]).portals.values()}
    assert portals["河北区"].portal_url == "http://www.tjhbq.gov.cn/"
    # 只有"河北"：河北区没找到，宁缺不错
    pages = dict(base)
    pages["https://www.tj.gov.cn/"] = '<a href="http://www.hebei.gov.cn/">河北</a>'
    portals = {p.name: p for p in _run(pages, ["天津"]).portals.values()}
    assert portals["河北区"].status == "导航未找到" and portals["河北区"].portal_url == ""
    # 文字对但链到了河北省网站：门户抓到后按标识码和网站名否掉，不从它找部门
    pages = dict(base)
    pages["https://www.tj.gov.cn/"] = '<a href="http://www.hebei.gov.cn/">河北区</a>'
    inv = _run(pages, ["天津"])
    hb = {p.name: p for p in inv.portals.values()}["河北区"]
    assert hb.status == "导航未找到" and hb.portal_url == ""
    assert "外省" in hb.reason and "1300000013" in hb.reason
    assert not [d for d in inv.depts if d.code == "120105"]
    filled = [d for d in complete_slots(inv.portals.values(), inv.depts) if d.code == "120105"]
    assert len(filled) == 11 and all("外省" in d.reason for d in filled)


def test_report_page_links_to_city_portal_pages_are_not_county_portals():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.jiangsu.gov.cn/">江苏</a>',
        "https://www.jiangsu.gov.cn/": '<a href="/sxwz/">市县政府网站</a>',
        "https://www.jiangsu.gov.cn/sxwz/": '<a href="https://www.nantong.gov.cn/">南通市</a>',
        "https://www.nantong.gov.cn/": """<meta name="SiteName" content="南通市人民政府">
            <a href="https://zt.nantong.gov.cn/nb/">政府网站年度报表</a>""",
        "https://zt.nantong.gov.cn/nb/": """<a href="https://www.nantong.gov.cn/ntsrmzf/xsq/content/a1.html">通州区</a>""",
    }
    portals = {p.name: p for p in _run(pages, ["江苏"]).portals.values()}
    assert portals["南通市"].portal_url == "https://www.nantong.gov.cn/"
    assert portals["通州区"].status == "导航未找到" and portals["通州区"].portal_url == ""


def test_service_hall_region_page_is_not_a_county_portal():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.henan.gov.cn/">河南</a>',
        "https://www.henan.gov.cn/": '<a href="/sxwz/">市县政府网站</a>',
        "https://www.henan.gov.cn/sxwz/": '<a href="https://www.xuchang.gov.cn/">许昌市</a>',
        "https://www.xuchang.gov.cn/": """<a href="https://www.hnzwfw.gov.cn/411002000000/?region=411002000000">魏都区</a>""",
    }
    portals = {p.name: p for p in _run(pages, ["河南"]).portals.values()}
    assert portals["魏都区"].status == "导航未找到"


def test_city_bureau_branch_counts_for_the_county():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.shanxi.gov.cn/">山西</a>',
        "https://www.shanxi.gov.cn/": '<a href="/sxwz/">市县政府网站</a>',
        "https://www.shanxi.gov.cn/sxwz/": '<a href="http://www.sxlc.gov.cn/">黎城县</a>',
        "http://www.sxlc.gov.cn/": """<meta name="SiteName" content="黎城县人民政府门户网站">
            <a href="/bm/hbj/">市生态环境局黎城分局</a>""",
        "http://www.sxlc.gov.cn/bm/hbj/": """<meta name="SiteName" content="黎城县人民政府门户网站">
            <a href="/bm/hbj/tzgg/">通知公告</a>""",
    }
    depts = _depts(_run(pages, ["山西"]))
    assert depts[("140426", "生态环境")].site_url == "http://www.sxlc.gov.cn/bm/hbj/"


def test_branch_section_on_city_bureau_site_counts_for_the_district():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.nmg.gov.cn/">内蒙古</a>',
        "https://www.nmg.gov.cn/": '<a href="/msdh/">盟市政府网站</a>',
        "https://www.nmg.gov.cn/msdh/": '<a href="http://www.huhhot.gov.cn/">呼和浩特市</a>',
        "http://www.huhhot.gov.cn/": '<a href="/qxdh/">旗县区政府网站</a>',
        "http://www.huhhot.gov.cn/qxdh/": '<a href="http://www.xinchengqu.gov.cn/">新城区</a>',
        "http://www.xinchengqu.gov.cn/": """<meta name="SiteName" content="新城区人民政府">
            <a href="/jgzl/">政府机构</a>""",
        "http://www.xinchengqu.gov.cn/jgzl/": """
            <a href="http://sthjj.huhhot.gov.cn/hbfj/xcfj/">呼和浩特市生态环境局新城分局</a>
            <a href="http://scjgj.huhhot.gov.cn/">呼和浩特市市场监督管理局新城区分局</a>""",
        # 分局的版块在市局网站上（网站名是市局的）：算新城区的
        # 页面菜单里还有市局自己的"政策文件"：那是市局的，不算分局的
        "http://sthjj.huhhot.gov.cn/hbfj/xcfj/": """<meta name="SiteName" content="呼和浩特市生态环境局">
            <a href="/hbfj/xcfj/tzgg/">通知公告</a><a href="/zfxxgk/zcwj/">政策文件</a>""",
        # 链到市局首页：不算
        "http://scjgj.huhhot.gov.cn/": '<meta name="SiteName" content="呼和浩特市市场监督管理局">',
    }
    depts = _depts(_run(pages, ["内蒙古"]))
    hb = depts[("150102", "生态环境")]
    assert hb.site_url == "http://sthjj.huhhot.gov.cn/hbfj/xcfj/"
    assert [c.url for c in hb.columns] == ["http://sthjj.huhhot.gov.cn/hbfj/xcfj/tzgg/"]
    assert depts[("150102", "市场监管")].site_url == ""


def test_county_named_branch_on_city_bureau_site():
    from youju.govsites.attribution import branch_of_own, clean_name

    xc = _places("新城区")
    assert branch_of_own("新城区生态环境分局", xc)
    assert branch_of_own("呼和浩特市生态环境局新城分局", xc)
    assert not branch_of_own("呼和浩特市生态环境局", xc)
    assert clean_name("中国（北京）自由贸易试验区") == "北京自由贸易试验区"


# —— 每类部门选哪个页面（地市一组 11/9 报的：杭州、常州、锦州、葫芦岛）——


def _jiujiang_city_pages(channel_cols: bool) -> dict[str, str]:
    return {
        GOV_LOCAL_NAV: '<a href="https://www.jiangxi.gov.cn/">江西</a>',
        "https://www.jiangxi.gov.cn/": '<a href="/sxwz/">设区市政府网站</a>',
        "https://www.jiangxi.gov.cn/sxwz/": '<a href="http://www.jiujiang.gov.cn/">九江市</a>',
        # 独立网站只在首页上（不加导航分），门户频道在"组织机构"页上（加导航分）：频道分高
        "http://www.jiujiang.gov.cn/": """<meta name="SiteName" content="九江市人民政府">
            <a href="/zzjg/">组织机构</a><a href="http://kjj.jiujiang.gov.cn/">九江市科学技术局</a>""",
        "http://www.jiujiang.gov.cn/zzjg/": """<a href="/col/col1/index.html">市科技局</a>
            <a href="/info/1798/73896.htm">市财政局</a><a href="/czj/">市财政局</a>""",
        "http://www.jiujiang.gov.cn/col/col1/index.html": (
            '<meta name="SiteName" content="九江市人民政府">'
            + ('<a href="/col/col1/tzgg/">通知公告</a>' if channel_cols else "<p>机构职能</p>")
        ),
        "http://kjj.jiujiang.gov.cn/": """<meta name="SiteName" content="九江市科学技术局">
            <a href="/tzgg/">通知公告</a>""",
        "http://www.jiujiang.gov.cn/info/1798/73896.htm": "<p>财政局简介</p>",
        "http://www.jiujiang.gov.cn/czj/": "<p>财政局</p>",
    }


def test_pick_tries_next_candidate_when_top_page_has_no_columns():
    depts = _depts(_run(_jiujiang_city_pages(channel_cols=False), ["江西"]))
    kj = depts[("360400", "科技")]
    assert kj.site_url == "http://kjj.jiujiang.gov.cn/"
    assert [c.url for c in kj.columns] == ["http://kjj.jiujiang.gov.cn/tzgg/"]
    # 分最高的频道本身有栏目：照旧取它，不多试
    depts = _depts(_run(_jiujiang_city_pages(channel_cols=True), ["江西"]))
    assert depts[("360400", "科技")].site_url == "http://www.jiujiang.gov.cn/col/col1/index.html"


def test_pick_prefers_site_pages_over_articles_on_ties():
    # 两个同分候选都认不出栏目：取栏目页，不取"机构简介"文章
    depts = _depts(_run(_jiujiang_city_pages(channel_cols=False), ["江西"]))
    assert depts[("360400", "财政")].site_url == "http://www.jiujiang.gov.cn/czj/"


# —— 第一轮独立审阅提出的几处（11/9）——


def test_portal_channel_kept_when_portal_site_name_is_the_parent():
    # 集安市门户全站的 SiteName 写着"通化市人民政府"：门户里的部门频道照常算集安市的
    pages = {
        GOV_LOCAL_NAV: '<a href="http://www.jl.gov.cn/">吉林</a>',
        "http://www.jl.gov.cn/": '<a href="/sxwz/">市县政府网站</a>',
        "http://www.jl.gov.cn/sxwz/": '<a href="http://www.ja.gov.cn/">集安市</a>',
        "http://www.ja.gov.cn/": """<meta name="SiteName" content="通化市人民政府">
            <meta name="SiteIDCode" content="2205820001"><a href="/bmwz/">部门网站</a>""",
        "http://www.ja.gov.cn/bmwz/": '<a href="/czj/">集安市财政局</a>',
        "http://www.ja.gov.cn/czj/": """<meta name="SiteName" content="通化市人民政府">
            <a href="/czj/tzgg/">通知公告</a>""",
    }
    cz = _depts(_run(pages, ["吉林"]))[("220582", "财政")]
    assert cz.site_url == "http://www.ja.gov.cn/czj/"
    assert [c.url for c in cz.columns] == ["http://www.ja.gov.cn/czj/tzgg/"]


def test_blocked_top_candidate_is_kept_not_replaced():
    pages = {
        GOV_LOCAL_NAV: '<a href="https://www.jiangxi.gov.cn/">江西</a>',
        "https://www.jiangxi.gov.cn/": """<a href="/bmwz/">省直部门网站</a>
            <a href="http://www.other.gov.cn/">科技厅</a>""",
        "https://www.jiangxi.gov.cn/bmwz/": '<a href="http://kjt.jiangxi.gov.cn/">江西省科学技术厅</a>',
        "http://www.other.gov.cn/": '<a href="/tzgg/">通知公告</a>',
    }

    class Blocked(FakeCache):
        def get(self, url):
            if url.startswith("http://kjt.jiangxi.gov.cn/"):
                return FetchResult(url, "http_error", url, 412, "", "HTTP 412")
            return super().get(url)

    inv = Inventory(INDEX, Blocked(pages), provinces=["江西"], workers=2, per_province=2)
    inv.run()
    kj = _depts(inv)[("360000", "科技")]
    assert kj.site_url == "http://kjt.jiangxi.gov.cn/" and kj.status in ("不能抓", "打不开")


def _jiujiang_county_pages(with_market_bureau: bool) -> dict[str, str]:
    pages = _jiangxi_pages(city_kj=False, city_kfq=False)
    links = '<a href="/bm/kjj/">区科学技术局（区知识产权局）</a>'
    if with_market_bureau:
        links += '<a href="/bm/scj/">区市场监督管理局</a>'
    pages["http://www.lianxi.gov.cn/bmwz/"] = links
    pages["http://www.lianxi.gov.cn/bm/kjj/"] = '<a href="/bm/kjj/tzgg/">通知公告</a>'
    pages["http://www.lianxi.gov.cn/bm/scj/"] = "<p>市场监督管理局</p>"
    return pages


def test_bracket_alias_only_when_no_proper_candidate():
    depts = _depts(_run(_jiujiang_county_pages(with_market_bureau=True), ["江西"]))
    assert depts[("360402", "市场监管")].site_url == "http://www.lianxi.gov.cn/bm/scj/"
    assert depts[("360402", "科技")].site_url == "http://www.lianxi.gov.cn/bm/kjj/"
    # 没有正牌的市场监管局：才用加挂"知识产权局"牌子的科技局
    depts = _depts(_run(_jiujiang_county_pages(with_market_bureau=False), ["江西"]))
    assert depts[("360402", "市场监管")].site_url == "http://www.lianxi.gov.cn/bm/kjj/"


def test_report_listing_is_not_a_directory_and_homepage_is_read():
    pages = _jiangxi_pages(city_kj=False, city_kfq=False)
    report = "http://www.lianxi.gov.cn/zwgk/ndbb/2025/"
    pages["http://www.lianxi.gov.cn/"] = """<meta name="SiteName" content="濂溪区人民政府">
        <a href="/zwgk/ndbb/2025/">政府网站年度报表</a>"""
    names = ["财政局", "教育局", "公安局", "民政局", "司法局", "审计局", "统计局"]
    pages[report] = "".join(
        f'<a href="/zwgk/ndbb/2025/bm/{i}.html">濂溪区{n}</a>' for i, n in enumerate(names)
    )
    pages["http://www.lianxi.gov.cn/zwgk/ndbb/2025/bm/0.html"] = (
        "<table><tr><td>首页网址</td><td>http://czj.lianxi.gov.cn/</td></tr></table>"
    )
    pages["http://czj.lianxi.gov.cn/"] = '<a href="/tzgg/">通知公告</a>'
    inv = _run(pages, ["江西"])
    assert "360402" not in inv.directories
    assert _depts(inv)[("360402", "财政")].site_url == "http://czj.lianxi.gov.cn/"


def test_branch_names_need_the_local_name_right_before_the_suffix():
    from youju.govsites.attribution import own_branch

    xc = BY_NAME["新城区"]
    assert own_branch("呼和浩特市公安局新城区分局", xc)
    assert own_branch("呼和浩特市生态环境局新城分局", xc)
    assert not own_branch("呼和浩特市公安局新城湖分局", xc)  # "岳阳楼分局"不是岳阳县的那种


def test_hosts_compare_without_ports():
    from youju.govsites.inventory import _hostname

    assert _hostname("https://www.lianxi.gov.cn:443/bm/") == "www.lianxi.gov.cn"
    v = _site([], "3604000001", "https://www.lianxi.gov.cn:443/bm/kjj/", "濂溪区",
              own_portal="http://www.lianxi.gov.cn/")  # fmt: skip
    assert v.kind == "不确定"  # 本地门户的频道（带不带端口一样）


def test_upper_portals_known_before_the_city_finishes():
    from youju.govsites.inventory import Found

    inv = Inventory(INDEX, FakeCache({}), provinces=["江西"])
    # 省门户上直接列出区县时，区县可能先于地市处理完：地市门户网址在排下级时就记下
    inv._remember_children({"360400": Found("http://www.jiujiang.gov.cn/", "九江市", "x")})
    assert inv._upper_portals(_places("濂溪区")) == ["http://www.jiujiang.gov.cn/"]


# —— 第二轮独立审阅（11/9）——


def test_branch_of_county_sharing_the_city_name():
    from youju.govsites.attribution import branch_of_own

    cd = _places("承德县")
    assert not branch_of_own("承德市生态环境局双桥分局", cd)  # 打头的是上级"承德市"
    assert branch_of_own("承德市生态环境局承德县分局", cd)
    assert branch_of_own("承德县生态环境分局", cd)
    assert branch_of_own("承德市承德县生态环境分局", cd)  # 上级名 + 本地名 + 分局
    assert not branch_of_own("承德市生态环境分局", cd)


class RedirectCache(FakeCache):
    """部分网址跳到别处（最终网址不同）。"""

    def __init__(self, pages, redirects):
        super().__init__(pages)
        self.redirects = redirects

    def get(self, url):
        if url in self.redirects:
            final = self.redirects[url]
            r = super().get(final)
            return FetchResult(url, r.status, final, r.http_status, r.text, r.error)
        return super().get(url)


def _lianxi_with(links: str, extra: dict[str, str]) -> dict[str, str]:
    pages = _jiangxi_pages(city_kj=False, city_kfq=False)
    pages["http://www.lianxi.gov.cn/bmwz/"] = links
    pages.update(extra)
    return pages


def test_local_channel_not_replaced_by_unclear_site_elsewhere():
    pages = _lianxi_with(
        '<a href="/bm/kjj/">濂溪区科学技术局</a><a href="http://www.jjkjj.gov.cn/">科技局</a>',
        {
            "http://www.lianxi.gov.cn/bm/kjj/": "<p>机构职能</p>",
            # 市局用独立域名、网站名说不清：分更低，不能顶掉本地频道
            "http://www.jjkjj.gov.cn/": '<title>首页</title><a href="/tzgg/">通知公告</a>',
        },
    )
    kj = _depts(_run(pages, ["江西"]))[("360402", "科技")]
    assert kj.site_url == "http://www.lianxi.gov.cn/bm/kjj/"


def test_candidate_fetches_are_capped():
    links = '<a href="/bm/kjj/">区科技局</a>' + "".join(
        f'<a href="/bm/kj{i}/">区科技局</a>' for i in range(8)
    )
    pages = _lianxi_with(links, {"http://www.lianxi.gov.cn/bm/kjj/": "<p>机构职能</p>"})
    cache = FakeCache(pages)
    asked: list[str] = []
    orig = cache.get

    def get(url):
        asked.append(url)
        return orig(url)

    cache.get = get
    inv = Inventory(INDEX, cache, provinces=["江西"], workers=2, per_province=2)
    inv.run()
    tried = {u for u in asked if u.startswith("http://www.lianxi.gov.cn/bm/kj")}
    assert len(tried) <= 6  # 打不开的也算次数
    assert _depts(inv)[("360402", "科技")].site_url == "http://www.lianxi.gov.cn/bm/kjj/"


def test_portal_link_redirecting_elsewhere_is_judged_by_final_site():
    pages = _lianxi_with(
        '<a href="/bm/czj/">财政局</a><a href="/bm/zwfw/">政务服务数据管理局</a>',
        {
            "http://czj.jiujiang.gov.cn/": '<meta name="SiteName" content="九江市财政局">',
            "https://www.jxzwfw.gov.cn/dept/1": "<p>办事</p>",
        },
    )
    redirects = {
        "http://www.lianxi.gov.cn/bm/czj/": "http://czj.jiujiang.gov.cn/",
        "http://www.lianxi.gov.cn/bm/zwfw/": "https://www.jxzwfw.gov.cn/dept/1",
    }
    inv = Inventory(
        INDEX, RedirectCache(pages, redirects), provinces=["江西"], workers=2, per_province=2
    )
    inv.run()
    depts = _depts(inv)
    cz = depts[("360402", "财政")]
    assert cz.status == "导航未找到" and "九江市财政局" in cz.reason
    assert ("360402", "数据局") not in depts  # 跳到政务服务网的办事页


def test_alias_used_when_all_proper_candidates_are_rejected():
    pages = _lianxi_with(
        '<a href="http://kjj.jiujiang.gov.cn/">科技局</a>'
        '<a href="/bm/gxj/">区工业和信息化局（区科学技术局）</a>',
        {"http://www.lianxi.gov.cn/bm/gxj/": '<a href="/bm/gxj/tzgg/">通知公告</a>'},
    )
    depts = _depts(_run(pages, ["江西"]))
    assert depts[("360402", "科技")].site_url == "http://www.lianxi.gov.cn/bm/gxj/"
    assert depts[("360402", "工信")].site_url == "http://www.lianxi.gov.cn/bm/gxj/"


def test_portal_topic_page_is_not_a_department_column():
    # 枣庄（地市一组 11/9 抽查）：部门页上链到门户"财政预决算公开"专题，不算部门的栏目
    pages = _lianxi_with(
        '<a href="/zwgk/jgzn/sjj/jgjj/">濂溪区数据局</a>',
        {
            "http://www.lianxi.gov.cn/zwgk/jgzn/sjj/jgjj/": """<a href="/zwgk/zt/czyjsgk/">政策文件</a>
                <a href="/zwgk/jgzn/sjj/tzgg/">通知公告</a>""",
        },
    )
    sj = _depts(_run(pages, ["江西"]))[("360402", "数据局")]
    assert [c.url for c in sj.columns] == ["http://www.lianxi.gov.cn/zwgk/jgzn/sjj/tzgg/"]


def test_department_channel_under_a_topic_path_keeps_its_columns():
    # 通化市发改委那种：部门频道本身在 /ztzl/ 下，栏目在频道目录里，照常认
    pages = _lianxi_with(
        '<a href="/ztzl/fgw/">濂溪区发展和改革委员会</a>',
        {"http://www.lianxi.gov.cn/ztzl/fgw/": '<a href="/ztzl/fgw/tzgg/">通知公告</a>'},
    )
    fg = _depts(_run(pages, ["江西"]))[("360402", "发改委")]
    assert [c.url for c in fg.columns] == ["http://www.lianxi.gov.cn/ztzl/fgw/tzgg/"]


def test_weak_top_page_can_be_replaced_by_a_department_site():
    # 潍坊发改委、池州人社（地市一组 11/9）：首选是年报列表、文章一类，后面分低、名称说不清的
    # 部门网站有能读的栏目时换成它
    pages = _jiujiang_city_pages(channel_cols=False)
    pages["http://www.jiujiang.gov.cn/zzjg/"] = (
        '<a href="/Content/show/76.html">市人社局</a><a href="/wzbb/ndbb/2025/">市财政局</a>'
    )
    pages["http://www.jiujiang.gov.cn/"] = """<meta name="SiteName" content="九江市人民政府">
        <a href="/zzjg/">组织机构</a><a href="http://rsj.jiujiang.gov.cn/">人社局</a>
        <a href="http://czj.jiujiang.gov.cn/">财政局</a>"""
    pages["http://www.jiujiang.gov.cn/Content/show/76.html"] = "<p>人社局简介</p>"
    pages["http://www.jiujiang.gov.cn/wzbb/ndbb/2025/"] = "<p>年度报表</p>"
    pages["http://rsj.jiujiang.gov.cn/"] = '<a href="/tzgg/">通知公告</a>'
    pages["http://czj.jiujiang.gov.cn/"] = '<a href="/tzgg/">通知公告</a>'
    depts = _depts(_run(pages, ["江西"]))
    assert depts[("360400", "人社")].site_url == "http://rsj.jiujiang.gov.cn/"
    assert depts[("360400", "财政")].site_url == "http://czj.jiujiang.gov.cn/"


def test_local_weak_page_not_replaced_by_unclear_site_elsewhere():
    # 独立审阅（11/9）：本地部门链到简介文章，市局独立域名的"科技局"有栏目，也不能顶掉本地的
    pages = _lianxi_with(
        '<a href="/content/2025/1001.html">濂溪区科学技术局</a>'
        '<a href="http://www.jjkjj.gov.cn/">科技局</a>',
        {
            "http://www.lianxi.gov.cn/content/2025/1001.html": "<p>科技局简介</p>",
            "http://www.jjkjj.gov.cn/": '<title>首页</title><a href="/tzgg/">通知公告</a>',
        },
    )
    kj = _depts(_run(pages, ["江西"]))[("360402", "科技")]
    assert kj.site_url == "http://www.lianxi.gov.cn/content/2025/1001.html"
