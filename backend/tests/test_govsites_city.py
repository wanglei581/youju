"""地市级导航补充规则（govsites/navrules.py 和 inventory 里的接入点）：不联网、不连数据库。"""

from youju.crawler.fetcher import FetchResult
from youju.govsites.divisions import Division, DivisionIndex
from youju.govsites.inventory import Inventory
from youju.govsites.links import Link
from youju.govsites.navrules import city_nav_links, device_redirect, labeled_scripts

NANJING = """<html><head><title>中国南京</title></head><body><script type="text/javascript">
    var yd = "/zgnjsjb/index.html";//移动端地址
    var pc = "/index.html";//PC端地址
    var userAgentInfo = navigator.userAgent;
    var Agents = ["Android","iPhone"]; var tags = false;
    for (var v = 0; v < Agents.length; v++) { if (userAgentInfo.indexOf(Agents[v]) > 0){ tags =true; break; } }
    if(tags){
        window.location.href=yd;
    }else{
        window.location.href=pc;
    }
</script></body></html>"""

XUZHOU = """<html><head><title></title></head><script type="text/javascript">
 var targetProtocol = "https:";
if (window.location.protocol != targetProtocol)
  window.location.href = targetProtocol +
  window.location.href.substring(window.location.protocol.length); </script></html>"""

YIYANG = """<html><head><title>益阳市人民政府门户网站</title></head><script language="javaScript">
var host = location.hostname;
if(host=="www.yiyang.gov.cn" || host=="yiyang.gov.cn")
{
    document.location.href="yiyang/index.htm";
}
if(host=="jyzx.yiyang.gov.cn")
{
    document.location.href="ggzyjy/index.htm";
}
</script><body></body></html>"""


def test_device_redirect_picks_pc_target() -> None:
    assert device_redirect(NANJING, "http://www.nanjing.gov.cn/") == (
        "http://www.nanjing.gov.cn/index.html"
    )


def test_device_redirect_protocol_switch() -> None:
    assert device_redirect(XUZHOU, "http://www.xz.gov.cn/") == "https://www.xz.gov.cn/"
    # 已经是 https 的不再跳
    assert device_redirect(XUZHOU, "https://www.xz.gov.cn/") is None


def test_device_redirect_by_host() -> None:
    assert device_redirect(YIYANG, "http://www.yiyang.gov.cn/") == (
        "http://www.yiyang.gov.cn/yiyang/index.htm"
    )
    assert device_redirect(YIYANG, "http://jyzx.yiyang.gov.cn/") == (
        "http://jyzx.yiyang.gov.cn/ggzyjy/index.htm"
    )


def test_device_redirect_refuses_mobile_only_and_other_sites() -> None:
    mobile_only = '<script>var m = "/m/index.html"; location.href = m;</script>'
    assert device_redirect(mobile_only, "http://www.a.gov.cn/") is None
    elsewhere = '<script>window.location.href = "http://www.example.com/";</script>'
    assert device_redirect(elsewhere, "http://www.a.gov.cn/") is None
    assert device_redirect("<p>正文</p>", "http://www.a.gov.cn/") is None


AHUI = """<dl class="select"><dt aria-label="市直部门，折叠区域@enter">市直部门</dt>
<dd><ul><!--StartLabelHtml_linkList_b55--><script>var ll_6787701 = 100;</script>
<script charset="utf-8" src="https://www.wuhu.gov.cn/ljgl/sylj/sjjg/6787701.js?num=100"></script>
</ul></dd></dl>
<dl><dt>县区政府</dt><dd><script src="/ljgl/sylj/xqzf/6787711.js?num=100"></script></dd></dl>
<dl><dt>友情链接</dt><dd><script src="/ljgl/sylj/yqlj/6787721.js?num=100"></script></dd></dl>
<span>政府部门</span><script src="/assets/js/common.min.js"></script>
<span>市直部门</span><script src="http://other.gov.cn/x.js"></script>"""


def test_labeled_scripts_by_menu_name() -> None:
    got = labeled_scripts(AHUI, "https://www.wuhu.gov.cn/")
    assert got == [
        ("https://www.wuhu.gov.cn/ljgl/sylj/sjjg/6787701.js?num=100", "市直部门"),
        ("https://www.wuhu.gov.cn/ljgl/sylj/xqzf/6787711.js?num=100", "县区政府"),
    ]


def test_city_nav_links() -> None:
    links = [
        Link("部门频道", "https://www.zg.gov.cn/bmpd/list.html", "a"),
        Link("政务公开", "https://www.zg.gov.cn/zwgk/list.html", "a"),
        Link("市政府工作机构", "https://www.zg.gov.cn/dept.html", "a"),
        Link("四川省政府部门网站", "http://www.sc.gov.cn/bm/", "a"),
        Link("部门频道", "https://www.zg.gov.cn/bmpd/list.html", "a"),
        Link("关于进一步做好市直部门政务公开工作的通知全文", "https://www.zg.gov.cn/a/1.html", "a"),
        Link("机构职能", "https://www.zg.gov.cn/jgzn/list.html", "a"),
    ]
    got = city_nav_links(
        links, "https://www.zg.gov.cn/", skip={"https://www.zg.gov.cn/jgzn/list.html"}
    )
    assert [link.text for link in got] == ["部门频道", "市政府工作机构", "政务公开"]


class FakeCache:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.hits = self.fetches = 0

    def get(self, url: str, same_address_only=False) -> FetchResult:
        if url in self.pages:
            return FetchResult(url, "ok", url, 200, self.pages[url])
        return FetchResult(url, "http_error", url, 404, "", "HTTP 404")


def _inventory(pages: dict[str, str]) -> Inventory:
    divisions = [
        Division("320000", "江苏省", "省", "省级", "江苏", None, None, None),
        Division("320100", "南京市", "地级市", "市级", "江苏", "南京", None, "320000"),
    ]
    return Inventory(DivisionIndex(divisions), FakeCache(pages), provinces=["江苏"], workers=2)


def test_city_portal_follows_redirect_and_reads_menu_scripts() -> None:
    pages = {
        "http://www.nanjing.gov.cn/": NANJING,
        "http://www.nanjing.gov.cn/index.html": """<title>南京市人民政府</title>
            <a href="/bmpd/">部门频道</a><a href="/zwgk/">政务公开</a>
            <dl><dt>市直部门</dt><dd><script src="/ljgl/sjjg/1.js?num=100"></script></dd></dl>""",
        "http://www.nanjing.gov.cn/bmpd/": '<a href="http://fgw.nanjing.gov.cn/">南京市发展和改革委员会</a>',
        "http://www.nanjing.gov.cn/zwgk/": '<a href="/zwgk/bm/">部门信息公开</a>',
        "http://www.nanjing.gov.cn/ljgl/sjjg/1.js?num=100": (
            "document.write('<li><a href=\"http://kjj.nanjing.gov.cn/\">南京市科学技术局</a></li>');"
        ),
    }
    inv = _inventory(pages)
    home = inv.page("http://www.nanjing.gov.cn/")
    assert home.final_url == "http://www.nanjing.gov.cn/index.html"
    home.level_hint = "市级"
    nav = {p.url: p.nav_text for p in inv._nav_pages(home)}
    assert nav["http://www.nanjing.gov.cn/bmpd/"] == "地市补充·列表页"
    assert nav["http://www.nanjing.gov.cn/zwgk/"] == "地市补充·列表页"
    assert nav["http://www.nanjing.gov.cn/ljgl/sjjg/1.js?num=100"] == "地市补充·菜单脚本"
    # 省级、区县级门户不加部门列表页的叫法、不按菜单名挑脚本（只改地市；区县级照旧按文件名看页脚脚本）
    home.level_hint = "区县级"
    nav = {p.url: p.nav_text for p in inv._nav_pages(home)}
    assert "http://www.nanjing.gov.cn/bmpd/" not in nav
    assert nav.get("http://www.nanjing.gov.cn/ljgl/sjjg/1.js?num=100") == "页脚脚本"


def test_device_redirect_only_follows_understood_conditions() -> None:
    base = "http://www.x.gov.cn/"
    disabled = "<script>if('9103'=='9102'){location.href='/a.html';}</script>"
    assert device_redirect(disabled, base) is None
    enabled = "<script>if('9102'=='9102'){location.href='/a.html';}</script>"
    assert device_redirect(enabled, base) == "http://www.x.gov.cn/a.html"
    unknown = "<script>if(window.flag){location.href='/a.html';}</script>"
    assert device_redirect(unknown, base) is None
    by_var = '<script>var u = "/index.html"; window.location.href = u;</script>'
    assert device_redirect(by_var, base) == "http://www.x.gov.cn/index.html"


def test_https_fallback_only_for_dropped_http() -> None:
    from youju.govsites.navrules import https_fallback

    dropped = "robots.txt 暂时无法获取（RemoteProtocolError: Server disconnected without sending）"
    assert https_fallback("http://www.fuzhou.gov.cn/", "robots_error", dropped) == (
        "https://www.fuzhou.gov.cn/"
    )
    assert https_fallback("https://www.fuzhou.gov.cn/", "robots_error", dropped) is None
    assert https_fallback("http://www.x.gov.cn/", "http_error", "HTTP 412") is None
    assert https_fallback("http://www.x.gov.cn/", "robots_error", "HTTP 521") is None


def test_page_switches_to_https_when_http_is_dropped() -> None:
    class DroppingCache(FakeCache):
        def get(self, url: str, same_address_only=False) -> FetchResult:
            if url.startswith("http://www.fuzhou.gov.cn"):
                return FetchResult(url, "robots_error", url, None, "", "RemoteProtocolError: x")
            return super().get(url)

    divisions = [Division("350000", "福建省", "省", "省级", "福建", None, None, None)]
    inv = Inventory(
        DivisionIndex(divisions), DroppingCache({"https://www.fuzhou.gov.cn/": "<a>x</a>"})
    )
    p = inv.page("http://www.fuzhou.gov.cn/")
    assert p.result.status == "ok" and p.final_url == "https://www.fuzhou.gov.cn/"


def test_supplement_pages_do_not_outrank_existing_dept_sites() -> None:
    """补充导航页上的门户部门频道不能顶掉原有导航找到的部门独立网站（11/9 杭州）。"""
    import re

    from youju.govsites.navrules import CITY_NAV_NOTE, CITY_SCRIPT_NOTE

    for note in (CITY_NAV_NOTE, CITY_SCRIPT_NOTE):
        assert not re.search(r"部门|省直|市直|区直|县直|机构|单位", note)


def test_zhou_shi_in_city_names_is_not_a_nav_word() -> None:
    from youju.govsites.links import nav_links

    links = [
        Link("杭州市稳岗就业", "https://www.hangzhou.gov.cn/col/1/index.html", "a"),
        Link("福州市医疗卫生", "https://www.hangzhou.gov.cn/col/2/index.html", "a"),
        Link("各州市政府网站", "https://www.hangzhou.gov.cn/col/3/index.html", "a"),
        Link("州市网站", "https://www.hangzhou.gov.cn/col/4/index.html", "a"),
        Link("部门网站", "https://www.hangzhou.gov.cn/col/5/index.html", "a"),
    ]
    got = [link.text for link in nav_links(links, "https://www.hangzhou.gov.cn/")]
    assert got == ["各州市政府网站", "州市网站", "部门网站"]


def test_specific_page() -> None:
    from youju.govsites.navrules import specific_page

    assert specific_page("https://www.linyi.gov.cn/typt/zfxxgknb.htm")
    assert specific_page("http://www.baoding.gov.cn/mlbd-1472-386944.html")
    # 只用于省找地市：带参数的是门户里的某个频道（淄博 ?open=zfxxgknb）
    assert specific_page("https://www.zibo.gov.cn/?open=zfxxgknb")
    assert specific_page("https://www.x.gov.cn/art/zfxxgknb.jsp?id=3")
    assert not specific_page("http://www.yibin.gov.cn/index.html")
    assert not specific_page("https://www.ezhou.gov.cn/sy/")
    assert not specific_page("https://www.lf.gov.cn/Category_5/Index.aspx")
    assert not specific_page("http://www.jining.gov.cn/")


def test_child_portal_link_to_a_report_page_takes_site_root() -> None:
    divisions = [
        Division("370000", "山东省", "省", "省级", "山东", None, None, None),
        Division("371300", "临沂市", "地级市", "市级", "山东", "临沂", None, "370000"),
    ]
    inv = Inventory(DivisionIndex(divisions), FakeCache({}), provinces=["山东"], workers=2)
    from youju.govsites.inventory import PageInfo

    nav = PageInfo(
        "http://www.shandong.gov.cn/col/1/index.html",
        "http://www.shandong.gov.cn/col/1/index.html",
        FetchResult("http://www.shandong.gov.cn/col/1/index.html", "ok", None, 200, ""),
        links=[Link("临沂市", "https://www.linyi.gov.cn/typt/zfxxgknb.htm", "a")],
    )
    found = inv._find_children(divisions[0], [divisions[1]], [nav])
    assert found["371300"].url == "https://www.linyi.gov.cn/"
    assert "取网站首页" in found["371300"].text


def test_link_back_to_own_portal_page_is_not_cut_to_a_child_root() -> None:
    """入口补充页（别的主机）上地市名链回省门户自己的某一页：不截成"地市首页"，照旧跳过。"""
    from youju.govsites.inventory import PageInfo

    divisions = [
        Division("370000", "山东省", "省", "省级", "山东", None, None, None),
        Division("371300", "临沂市", "地级市", "市级", "山东", "临沂", None, "370000"),
    ]
    inv = Inventory(DivisionIndex(divisions), FakeCache({}), provinces=["山东"], workers=2)
    seed = PageInfo(
        "http://fgw.shandong.gov.cn/col/2/index.html",
        "http://fgw.shandong.gov.cn/col/2/index.html",
        FetchResult("http://fgw.shandong.gov.cn/col/2/index.html", "ok", None, 200, ""),
        links=[Link("临沂市", "https://www.shandong.gov.cn/typt/zfxxgknb.htm", "a")],
    )
    found = inv._find_children(divisions[0], [divisions[1]], [seed], {"www.shandong.gov.cn"})
    assert "371300" not in found


def test_channel_pages_that_look_like_articles() -> None:
    from youju.govsites.inventory import _site_like

    assert _site_like("https://www.suining.gov.cn/dept/7e713e2e3afdf388b9649e06a874f92b.html", "")
    assert not _site_like("https://www.x.gov.cn/xxgk/7e713e2e3afdf388b9649e06a874f92b.shtml", "")
    uuid = "a096dbcd-95dc-40df-9de0-1d1ab13d0969"
    assert _site_like(f"https://www.sjz.gov.cn/columns/{uuid}/", "")
    assert _site_like(f"https://www.sjz.gov.cn/columns/{uuid}/index.html", "")
    assert not _site_like(f"https://www.sjz.gov.cn/columns/{uuid}/202611/123.html", "")


def test_wrapped_redirect_links_are_dropped() -> None:
    from youju.govsites.links import page_links

    html = (
        '<a href="http://libs.dg.gov.cn/code/xhtml/loading.html?from=%E5%8F%91%E6%94%B9">发改</a>'
        '<a href="http://dgdp.dg.gov.cn/gkmlpt/index">东莞市发展和改革局</a>'
    )
    got = [(link.text, link.url) for link in page_links(html, "https://www.dg.gov.cn/")]
    assert got == [("东莞市发展和改革局", "http://dgdp.dg.gov.cn/gkmlpt/index")]


def test_autonomous_prefecture_short_form() -> None:
    from youju.govsites.divisions import name_variants

    names = name_variants("湘西土家族苗族自治州", "自治州")
    assert {"湘西自治州", "湘西州", "湘西"} <= names


def test_county_link_to_a_specific_page_is_not_cut_to_site_root() -> None:
    """区县门户常挂在地市网站的子目录、参数页下：省网站上链到区县的具体页不截成网站首页。"""
    from youju.govsites.inventory import PageInfo

    divisions = [
        Division("370000", "山东省", "省", "省级", "山东", None, None, None),
        Division("371300", "临沂市", "地级市", "市级", "山东", "临沂", None, "370000"),
        Division("371302", "兰山区", "市辖区", "区县级", "山东", "临沂", "兰山区", "371300"),
    ]
    inv = Inventory(DivisionIndex(divisions), FakeCache({}), provinces=["山东"], workers=2)
    nav = PageInfo(
        "http://www.shandong.gov.cn/col/1/index.html",
        "http://www.shandong.gov.cn/col/1/index.html",
        FetchResult("http://www.shandong.gov.cn/col/1/index.html", "ok", None, 200, ""),
        links=[Link("兰山区", "https://www.linyi.gov.cn/lsq/zfxxgknb.htm", "a")],
    )
    found = inv._find_children(divisions[0], divisions[1:], [nav])
    assert found["371302"].url == "https://www.linyi.gov.cn/lsq/zfxxgknb.htm"


def test_redirect_script_on_a_content_page_is_not_followed() -> None:
    """跳转规则只对几乎没有正文的页面生效：正常内容页里的手机版跳转脚本不跟。"""
    from youju.govsites.inventory import _script_redirect

    body = "<p>" + "通知正文内容" * 60 + "</p>"
    page = f"<html><body>{body}{NANJING[NANJING.index('<script') :]}</body></html>"
    result = FetchResult("http://www.nanjing.gov.cn/a.html", "ok", None, 200, page)
    assert _script_redirect(result, result.url) is None
    tiny = FetchResult("http://www.nanjing.gov.cn/", "ok", None, 200, NANJING)
    assert _script_redirect(tiny, tiny.url) == "http://www.nanjing.gov.cn/index.html"


def test_https_refusal_is_kept_as_blocked() -> None:
    """http 断开、https 回 412：以 https 的拒绝为准，记不能抓（成都），不记打不开。"""

    class Cache(FakeCache):
        def get(self, url: str, same_address_only=False) -> FetchResult:
            if url.startswith("http://www.chengdu.gov.cn"):
                return FetchResult(url, "robots_error", url, None, "", "RemoteProtocolError: x")
            if url.startswith("https://www.chengdu.gov.cn"):
                return FetchResult(url, "http_error", url, 412, "", "HTTP 412")
            return super().get(url)

    divisions = [Division("510000", "四川省", "省", "省级", "四川", None, None, None)]
    inv = Inventory(DivisionIndex(divisions), Cache({}))
    p = inv.page("http://www.chengdu.gov.cn/")
    assert p.result.http_status == 412 and p.block == "HTTP 412 拒绝访问"


def test_device_redirect_negated_host_and_concatenated_var() -> None:
    neg = """<script>var host = location.hostname;
if (host != "www.x.gov.cn")
{ location.href = "/other/index.html"; }</script>"""
    assert device_redirect(neg, "http://www.x.gov.cn/") is None
    assert device_redirect(neg, "http://m.x.gov.cn/") == "http://m.x.gov.cn/other/index.html"
    concat = '<script>var pc = "/index" + ".html"; window.location.href = pc;</script>'
    assert device_redirect(concat, "http://www.x.gov.cn/") is None


def test_intro_article_lists_are_demoted() -> None:
    """ "机构简介"页上的部门链接都是简介文章：这页不给找部门加分（锦州），年度报表页照旧。"""
    from youju.govsites.navrules import CITY_NAV_NOTE, demote_weak_lists

    intro = """<a href="http://www.jz.gov.cn/info/1798/73887.htm">市发展和改革委员会</a>
        <a href="http://www.jz.gov.cn/info/1798/73889.htm">市科学技术局</a>
        <a href="http://www.jz.gov.cn/info/1798/73895.htm">市财政局</a>"""
    sites = """<a href="https://fgw.jz.gov.cn/">市发展和改革委员会</a>
        <a href="https://kjj.jz.gov.cn/">市科学技术局</a><a href="https://czj.jz.gov.cn/">市财政局</a>"""
    pages = {
        "http://www.jz.gov.cn/": "<title>锦州</title>",
        "http://www.jz.gov.cn/jgjj1.htm": intro,
        "http://www.jz.gov.cn/bm.htm": sites,
        "http://www.jz.gov.cn/nb.htm": intro,
    }
    inv = _inventory(pages)
    got = [inv.page(u) for u in pages]
    got[1].nav_text, got[2].nav_text, got[3].nav_text = "机构简介", "部门网站", "政府网站年度报表"
    demote_weak_lists(got)
    assert got[1].nav_text == CITY_NAV_NOTE
    assert got[2].nav_text == "部门网站"
    assert got[3].nav_text == "政府网站年度报表"


def test_guide_links_dropped_when_homepage_link_is_on_the_same_page() -> None:
    """镇江：正文链到部门的信息公开指南、页脚链到部门首页——去掉指南那条。"""
    from youju.govsites.navrules import prune_weak_links

    html = """<a href="http://fgw.zhenjiang.gov.cn/fgw/zfxxgkzn/xxgk_tt.shtml">镇江市发展和改革委员会</a>
        <a href="http://kjj.zhenjiang.gov.cn/kjj/zfxxgkzn/xxgk_tt.shtml">镇江市科学技术局</a>
        <a href="http://fgw.zhenjiang.gov.cn/">市发改委</a>"""
    inv = _inventory({"http://www.zhenjiang.gov.cn/x.shtml": html})
    p = inv.page("http://www.zhenjiang.gov.cn/x.shtml")
    prune_weak_links(p)
    assert [link.url for link in p.links] == [
        "http://kjj.zhenjiang.gov.cn/kjj/zfxxgkzn/xxgk_tt.shtml",
        "http://fgw.zhenjiang.gov.cn/",
    ]


def test_dept_channel_pages_are_not_weak_targets() -> None:
    from youju.govsites.navrules import _weak_target

    assert not _weak_target("https://www.suining.gov.cn/dept/7e713e2e3afdf388b9649e06a874f92b.html")
    assert _weak_target("http://www.jz.gov.cn/info/1798/73887.htm")
    assert _weak_target("http://fgw.zhenjiang.gov.cn/fgw/zfxxgkzn/xxgk_tt.shtml")


def test_weak_links_only_pruned_for_the_same_site() -> None:
    """泸州：商务会展局的简介页链接不能因为另有一个"商务"类机构（口岸办）的首页链接就被剪掉。"""
    from youju.govsites.navrules import prune_weak_links

    html = """<a href="https://www.luzhou.gov.cn/jg/swhzj/jgjj/">泸州市商务和会展局</a>
        <a href="http://kab.luzhou.gov.cn/">泸州市口岸和物流办公室</a>"""
    inv = _inventory({"https://www.luzhou.gov.cn/jg/": html})
    p = inv.page("https://www.luzhou.gov.cn/jg/")
    prune_weak_links(p)
    assert len(p.links) == 2


def test_shared_portal_host_keeps_other_departments_intro_links() -> None:
    """各部门共用门户域名：发改委有频道链接，不能顺带剪掉只有简介链接的科技局。"""
    from youju.govsites.navrules import prune_weak_links

    html = """<a href="https://www.x.gov.cn/bm/fgw/">某市发展和改革委员会</a>
        <a href="https://www.x.gov.cn/bm/fgw/jgjj/">某市发展和改革委员会</a>
        <a href="https://www.x.gov.cn/bm/kjj/jgjj/">某市科学技术局</a>"""
    inv = _inventory({"https://www.x.gov.cn/jg/": html})
    p = inv.page("https://www.x.gov.cn/jg/")
    prune_weak_links(p)
    assert [link.url for link in p.links] == [
        "https://www.x.gov.cn/bm/fgw/",
        "https://www.x.gov.cn/bm/kjj/jgjj/",
    ]
