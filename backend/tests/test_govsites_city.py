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

    def get(self, url: str) -> FetchResult:
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
    assert nav["http://www.nanjing.gov.cn/bmpd/"] == "部门频道"
    assert nav["http://www.nanjing.gov.cn/zwgk/"] == "政务公开"
    assert nav["http://www.nanjing.gov.cn/ljgl/sjjg/1.js?num=100"] == "市直部门"
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
