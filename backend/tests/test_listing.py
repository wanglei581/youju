from datetime import date
from urllib.parse import urlsplit

import pytest

from tests.conftest import read_fixture_html
from youju.crawler.listing import extract_items

TODAY = date(2026, 9, 26)

# 真实政府网站列表页快照（2026-09-26 抓取），覆盖几种常见网站模板
REAL_PAGES = [
    ("beijing_kw_tzgg", "https://kw.beijing.gov.cn/zwgk/tzgg/", 20),
    ("jiangsu_kjt_tzgg", "https://kxjst.jiangsu.gov.cn/col/col82540/index.html", 50),
    ("shandong_kjt_tzgg", "http://kjt.shandong.gov.cn/col/col13360/index.html", 50),
    ("guangdong_gxt_tzgg", "https://gdii.gd.gov.cn/zwgk/tzgg1011/index.html", 15),
    ("sichuan_kjt_gstz", "https://kjt.sc.gov.cn/kjt/gstz/newschild.shtml", 10),
    ("most_tztg", "https://www.most.gov.cn/tztg/", 15),
    ("stats_zxfb", "https://www.stats.gov.cn/sj/zxfb/", 10),
    ("henan_kjt_tzgg", "https://kjt.henan.gov.cn/tzgg/", 10),
    ("tianjin_kjt_tzgg", "https://kxjs.tj.gov.cn/ZWGK4143/TZGG2079/", 15),
    ("hubei_kjt_tzgg", "https://kjt.hubei.gov.cn/kjdt/tzgg/", 10),
]


@pytest.mark.parametrize(("name", "page_url", "min_items"), REAL_PAGES)
def test_real_list_pages(name: str, page_url: str, min_items: int) -> None:
    items = extract_items(read_fixture_html(name), page_url, today=TODAY)
    assert len(items) >= min_items
    assert all(i.publish_date is not None for i in items), "列表条目都应有发布日期"
    assert len({i.url for i in items}) == len(items), "同一网址只保留一次"
    host = urlsplit(page_url).netloc
    same_host = sum(urlsplit(i.url).netloc == host for i in items)
    assert same_host / len(items) >= 0.8, "条目应主要来自本站"
    assert all(len(i.title) >= 6 for i in items)
    assert not any(i.title in ("首页", "下一页", "更多") for i in items)
    # 列表是最近更新过的：最新条目不早于 2026 年
    assert max(i.publish_date for i in items) >= date(2026, 1, 1)


def test_picks_main_list_not_sidebar() -> None:
    html = """
    <html><body>
      <div class="side"><ul>
        <li><a href="/hot/1.html">侧栏热点新闻标题一则</a><span>2026-09-01</span></li>
        <li><a href="/hot/2.html">侧栏热点新闻标题二则</a><span>2026-09-02</span></li>
      </ul></div>
      <div class="main"><ul class="list">
        <li><a href="/a/1.html" title="关于开展2026年度专项申报工作的通知">关于开展2026年度专项…</a>
            <span>2026-09-20</span></li>
        <li><a href="/a/2.html">关于公布第三批认定名单的公示</a><span>2026-09-18</span></li>
        <li><a href="/a/3.html">关于征集2026年项目需求的通知</a><span>2026-09-15</span></li>
      </ul></div>
      <div class="pager"><a href="index_1.html">下一页</a><a href="index_9.html">尾页</a></div>
    </body></html>
    """
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.url for i in items] == [
        "https://kjt.example.gov.cn/a/1.html",
        "https://kjt.example.gov.cn/a/2.html",
        "https://kjt.example.gov.cn/a/3.html",
    ]
    # 链接文字被截断时，使用 title 属性里的完整标题
    assert items[0].title == "关于开展2026年度专项申报工作的通知"
    assert items[0].publish_date == date(2026, 9, 20)


def test_table_rows_and_date_in_other_cell() -> None:
    html = """
    <table><tbody>
      <tr><td><a href="/t/1.html">2026年度高新技术企业认定工作的通知</a></td><td>2026/09/10</td></tr>
      <tr><td><a href="/t/2.html">第二批科技型中小企业入库名单公示</a></td><td>2026/09/08</td></tr>
    </tbody></table>
    """
    items = extract_items(html, "https://example.gov.cn/list/", today=TODAY)
    assert [(i.url.rsplit("/", 1)[-1], i.publish_date) for i in items] == [
        ("1.html", date(2026, 9, 10)),
        ("2.html", date(2026, 9, 8)),
    ]


def test_title_containing_date_is_not_publish_date() -> None:
    html = """
    <ul>
      <li><a href="/a.html">关于2026年9月30日前提交申报材料的通知</a><span>2026-09-01</span></li>
      <li><a href="/b.html">关于公布认定结果的通知文件发布</a><span>2026-08-28</span></li>
    </ul>
    """
    items = extract_items(html, "https://example.gov.cn/", today=TODAY)
    assert items[0].publish_date == date(2026, 9, 1)


def test_date_only_in_url() -> None:
    html = """
    <ul>
      <li><a href="./202609/t20260921_1.html">关于印发产业发展若干措施的通知</a></li>
      <li><a href="./202609/t20260919_2.html">关于征集优秀案例的通知全文</a></li>
    </ul>
    """
    items = extract_items(html, "https://example.gov.cn/zcwj/", today=TODAY)
    assert [i.publish_date for i in items] == [date(2026, 9, 21), date(2026, 9, 19)]


def test_jcms_datastore_records_are_expanded() -> None:
    html = """
    <html><body><div id="list">
    <script type="text/xml"><datastore><recordset>
    <record><![CDATA[<li><a href="/art/2026/9/23/art_1.html" title="关于组织开展科技副总选派工作的通知">关于组织开展…</a><span>2026-09-23</span></li>]]></record>
    <record><![CDATA[<li><a href="/art/2026/9/21/art_2.html">2026年度省基础研究计划拟立项目公示</a><span>2026-09-21</span></li>]]></record>
    </recordset></datastore></script>
    </div></body></html>
    """
    items = extract_items(html, "https://kxjst.example.gov.cn/col/col1/index.html", today=TODAY)
    assert [i.title for i in items] == [
        "关于组织开展科技副总选派工作的通知",
        "2026年度省基础研究计划拟立项目公示",
    ]


def test_parser_config_overrides_heuristic() -> None:
    html = """
    <div class="news"><div class="row"><a href="/x/1.html">第一条信息标题内容</a><em>2026-09-02</em></div></div>
    <div class="news"><div class="row"><a href="/x/2.html">第二条信息标题内容</a><em>2026-09-01</em></div></div>
    """
    config = {"item_selector": "div.row", "date_selector": "em"}
    items = extract_items(html, "https://example.gov.cn/", config=config, today=TODAY)
    assert [i.publish_date for i in items] == [date(2026, 9, 2), date(2026, 9, 1)]


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", "not html at all"])
def test_empty_or_broken_pages(html: str) -> None:
    assert extract_items(html, "https://example.gov.cn/", today=TODAY) == []


def test_real_page_with_embedded_json_list() -> None:
    """江西科技厅：列表写在脚本变量 listData.articleList 里，由前端渲染。"""
    url = "https://kjt.jiangxi.gov.cn/jxskxjst/col/col27029/index.html"
    items = extract_items(read_fixture_html("jiangxi_kjt_tzgg"), url, today=TODAY)
    assert len(items) >= 10
    assert all(i.publish_date for i in items)
    assert all(i.url.startswith("https://kjt.jiangxi.gov.cn/jxskxjst/") for i in items)


def test_embedded_json_with_nested_url_string() -> None:
    html = """<html><body><div id="app"></div><script>
    var listData = { articleList: [
      {"title": "关于组织申报2026年度技术创新中心的通知", "pubDate": "2026-09-24 17:08",
       "urls": "{\\"pc\\":\\"/site/col/1/content/content_1.html\\"}"},
      {"showTitle": "第三批科技型中小企业入库名单公示", "publishTime": "2026-09-20",
       "url": "/site/col/1/content/content_2.html"}
    ]};
    </script></body></html>"""
    items = extract_items(html, "https://example.gov.cn/site/col/1/index.html", today=TODAY)
    assert [(i.url.rsplit("/", 1)[-1], i.publish_date) for i in items] == [
        ("content_1.html", date(2026, 9, 24)),
        ("content_2.html", date(2026, 9, 20)),
    ]


def test_base_href_is_used_for_relative_links() -> None:
    html = """<html><head><base href="/sub/"></head><body><ul>
      <li><a href="col/1/a.html">关于发布申报指南的通知全文</a><span>2026-09-20</span></li>
      <li><a href="col/1/b.html">关于公布认定结果的通知全文</a><span>2026-09-19</span></li>
    </ul></body></html>"""
    items = extract_items(html, "https://example.gov.cn/other/index.html", today=TODAY)
    assert items[0].url == "https://example.gov.cn/sub/col/1/a.html"
