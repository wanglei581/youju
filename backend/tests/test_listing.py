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
    # 以下三个在 PR #10 审阅后加入：列表分块显示、旧规则只识别到 1 条、旧规则误取机构职能列表
    ("fujian_kjt_tzgg", "https://kjt.fujian.gov.cn/xxgk/tzgg/", 100),
    ("sichuan_jxt_tz", "https://jxt.sc.gov.cn/scjxt/wjfb/common_list.shtml", 10),
    ("hunan_kjt_tzgg", "https://kjt.hunan.gov.cn/kjt/xxgk/tzgg/index.html", 8),
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


def test_markup_in_title_attribute_is_not_used_as_title() -> None:
    # 山西省科技厅把索引号、发布时间写成 HTML 放在 title 属性里，真正的标题是链接文字
    html = """
    <table><tbody>
      <tr><td><a href="/t/1.html" title="<p><b>索引号：000014349/2026-04994</b> </p><p><b>发布时间：</b>2026.09.15</p>">
        关于征集省级科技成果转化引导基金储备项目的通知</a></td><td>2026.09.15</td></tr>
      <tr><td><a href="/t/2.html" title="<p><b>索引号：000014349/2026-04995</b></p>">
        关于开展2026年度科技型中小企业评价工作的通知</a></td><td>2026.09.14</td></tr>
    </tbody></table>
    """
    items = extract_items(html, "https://kjt.shanxi.gov.cn/list/", today=TODAY)
    assert [i.title for i in items] == [
        "关于征集省级科技成果转化引导基金储备项目的通知",
        "关于开展2026年度科技型中小企业评价工作的通知",
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


# 以下用例来自 PR #10 的红队审阅（grok）：每一条都曾让"新增"判断出错


def test_sidebar_with_more_items_and_same_class_is_not_main_list() -> None:
    hot = "".join(
        f'<li><a href="/hot/{n}.html">侧栏热点新闻标题第{n}则</a><span>2026-09-2{n}</span></li>'
        for n in range(1, 9)
    )
    html = f"""<html><body>
      <div class="main"><ul class="list">
        <li><a href="/tzgg/202609/t20260920_1.html">关于开展专项申报工作的通知</a><span>2026-09-20</span></li>
        <li><a href="/tzgg/202609/t20260918_2.html">关于公布第三批认定名单的公示</a><span>2026-09-18</span></li>
        <li><a href="/tzgg/202609/t20260915_3.html">关于征集2026年项目需求的通知</a><span>2026-09-15</span></li>
      </ul></div>
      <div class="side-box"><ul class="list">{hot}</ul></div>
    </body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.url.rsplit("/", 1)[-1] for i in items] == [
        "t20260920_1.html",
        "t20260918_2.html",
        "t20260915_3.html",
    ]


def test_unmarked_sidebar_loses_to_list_under_column_directory() -> None:
    # 侧栏没有 side、hot 之类的标记时，看条目是否位于本栏目目录下
    hot = "".join(
        f'<li><a href="/xwzx/{n}.html">其他栏目的热点新闻第{n}则</a><span>2026-09-2{n}</span></li>'
        for n in range(1, 7)
    )
    html = f"""<html><body>
      <div class="left"><ul>
        <li><a href="/tzgg/1.html">关于开展专项申报工作的通知</a><span>2026-09-20</span></li>
        <li><a href="/tzgg/2.html">关于公布第三批认定名单的公示</a><span>2026-09-18</span></li>
        <li><a href="/tzgg/3.html">关于征集2026年项目需求的通知</a><span>2026-09-15</span></li>
      </ul></div>
      <div class="right"><ul>{hot}</ul></div>
    </body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/index.html", today=TODAY)
    assert {i.url.rsplit("/", 2)[-2] for i in items} == {"tzgg"}


def test_attachment_links_stay_in_their_row() -> None:
    html = """<ul>
      <li><a href="/a/1.html">关于开展专项申报工作的通知</a>
          <a href="/files/1.doc">附件：申报书模板下载</a><span>2026-09-20</span></li>
      <li><a href="/a/2.html">关于公布第三批认定名单的公示</a>
          <a href="/files/2.pdf">第三批认定名单（正式版）</a><span>2026-09-18</span></li>
      <li><a href="/a/3.html">关于征集2026年项目需求的通知</a><span>2026-09-15</span></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [(i.url.rsplit("/", 1)[-1], i.publish_date) for i in items] == [
        ("1.html", date(2026, 9, 20)),
        ("2.html", date(2026, 9, 18)),
        ("3.html", date(2026, 9, 15)),
    ]


def test_row_with_long_summary_keeps_its_date() -> None:
    summary = "为贯彻落实创新驱动发展战略，进一步做好专项申报工作，现将有关事项通知如下。" * 6
    html = f"""<ul>
      <li><a href="/a/1.html">关于开展2026年度专项申报工作的通知</a><p>{summary}</p><span>2026-09-20</span></li>
      <li><a href="/a/2.html">关于公布第三批认定名单的公示</a><span>2026-09-18</span></li>
      <li><a href="/a/3.html">关于征集2026年项目需求的通知</a><span>2026-09-15</span></li>
    </ul>"""
    assert len(summary) > 180
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [i.publish_date for i in items] == [
        date(2026, 9, 20),
        date(2026, 9, 18),
        date(2026, 9, 15),
    ]


def test_short_date_at_end_of_anchor_text() -> None:
    html = """<ul>
      <li><a href="/a/1.html">关于开展专项申报工作的通知[09-20]</a></li>
      <li><a href="/a/2.html">关于公布第三批认定名单的公示[09-18]</a></li>
      <li><a href="/a/3.html">关于征集2026年项目需求的通知 09-15</a></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于开展专项申报工作的通知", date(2026, 9, 20)),
        ("关于公布第三批认定名单的公示", date(2026, 9, 18)),
        ("关于征集2026年项目需求的通知", date(2026, 9, 15)),
    ]


def test_full_date_in_url_beats_short_date_in_row() -> None:
    # 行里只写"01-15"，今年推断会得到 2026-01-15；网址里写明了 2025 年
    html = """<ul>
      <li><a href="/tzgg/202501/t20250115_1.html">关于开展专项申报工作的通知</a><span>01-15</span></li>
      <li><a href="/tzgg/202509/t20250910_2.html">关于公布第三批认定名单的公示</a><span>09-10</span></li>
      <li><a href="/tzgg/3.html">关于征集2026年项目需求的通知</a><span>09-15</span></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/tzgg/", today=TODAY)
    assert [i.publish_date for i in items] == [
        date(2025, 1, 15),
        date(2025, 9, 10),
        date(2026, 9, 15),
    ]


def test_document_number_is_not_a_date_and_slash_dates_work() -> None:
    html = """<table>
      <tr><td><a href="/a/1.html">关于印发产业扶持办法的通知</a></td><td>成科规〔2026〕1-2号</td><td>09-20</td></tr>
      <tr><td><a href="/a/2.html">关于公布第三批认定名单的公示</a></td><td>川科函〔2026〕12号</td><td>09/18</td></tr>
      <tr><td><a href="/a/3.html">关于征集2026年项目需求的通知</a></td><td>无</td><td>09.15</td></tr>
    </table>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [i.publish_date for i in items] == [
        date(2026, 9, 20),
        date(2026, 9, 18),
        date(2026, 9, 15),
    ]


def test_script_list_wins_when_page_links_do_not_look_like_a_list() -> None:
    html = """<html><body>
      <div class="hot"><ul><li><a href="/hot/1.html">侧栏里唯一一条热点新闻</a><span>2026-09-01</span></li></ul></div>
      <div id="app"></div>
      <script>var listData = {articleList: [
        {"title": "关于开展专项申报工作的通知", "pubDate": "2026-09-20", "url": "/a/1.html"},
        {"title": "关于公布第三批认定名单的公示", "pubDate": "2026-09-18", "url": "/a/2.html"},
        {"title": "关于征集2026年项目需求的通知", "pubDate": "2026-09-15", "url": "/a/3.html"}
      ]};</script>
    </body></html>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [i.url.rsplit("/", 1)[-1] for i in items] == ["1.html", "2.html", "3.html"]


def test_script_arrays_with_generic_keys_are_ignored() -> None:
    # name、time 这类字段常见于导航数据，不能当成文件
    html = """<html><body><div id="app"></div><script>
      var nav = [
        {"name": "政府信息公开指南", "url": "/zwgk/zn.html", "time": "2020-01-01"},
        {"name": "政府信息公开制度", "url": "/zwgk/zd.html", "time": "2020-01-01"},
        {"name": "政府信息公开年报", "url": "/zwgk/nb.html", "time": "2020-01-01"}
      ];
    </script></body></html>"""
    assert extract_items(html, "https://example.gov.cn/", today=TODAY) == []


def _rows(prefix: str, days: range) -> str:
    return "".join(
        f'<li><a href="/tzgg/202609/t202609{d:02d}_{prefix}{d}.html">关于开展第{d}项专项申报工作的通知</a>'
        f"<span>2026-09-{d:02d}</span></li>"
        for d in days
    )


def test_list_split_into_repeated_blocks_is_merged() -> None:
    # 福建科技厅、工信厅：一个列表每 5 条放进一个相同结构的块里
    blocks = "".join(
        f'<div class="list_base gl1"><ul>{_rows("b", range(start, start - 5, -1))}</ul></div>'
        for start in (25, 20, 15)
    )
    hot = "".join(
        f'<li><a href="/xwzx/{n}.html">其他栏目的热点新闻第{n}则</a><span>2026-09-2{n}</span></li>'
        for n in range(1, 8)
    )
    html = f"""<html><body>
      <div class="gl">{blocks}</div>
      <div class="right"><ul>{hot}</ul></div>
    </body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert len(items) == 15
    assert all("/tzgg/" in i.url for i in items)
    # 按页面顺序返回
    assert [i.publish_date.day for i in items] == list(range(25, 10, -1))


def test_blocks_with_different_wrappers_are_not_merged() -> None:
    # 结构不同的两块（正文与侧栏）即使都在本栏目目录下，也不合并
    html = f"""<html><body>
      <div class="list"><ul>{_rows("m", range(25, 19, -1))}</ul></div>
      <div class="recent"><ul>{_rows("r", range(9, 4, -1))}</ul></div>
    </body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.publish_date.day for i in items] == list(range(25, 19, -1))


def test_sibling_lists_under_one_parent_are_one_list() -> None:
    # 生态环境部：手机版只显示前 5 条，其余条目放在另一个带不同样式的 ul 里
    html = f"""<html><body><div class="tab">
      <ul>{_rows("a", range(25, 20, -1))}</ul>
      <ul class="mobile_none pc_line">{_rows("b", range(20, 11, -1))}</ul>
    </div></body></html>"""
    items = extract_items(html, "https://www.example.gov.cn/tzgg/", today=TODAY)
    assert len(items) == 14


# 以下用例来自 PR #10 第二轮红队审阅（grok）


def test_sibling_recommend_list_is_not_merged_into_main_list() -> None:
    html = f"""<html><body><div class="content">
      <ul>{_rows("m", range(25, 22, -1))}</ul>
      <ul class="recommend">{_rows("r", range(19, 15, -1))}</ul>
    </div></body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.publish_date.day for i in items] == [25, 24, 23]


def test_same_wrapper_in_other_directory_is_not_merged() -> None:
    # 正文和侧栏都放在 div.box 里：侧栏条目不在本栏目目录下，不合并
    side = "".join(
        f'<li><a href="/zwgk/xwzx/{n}.html">其他栏目的热点新闻第{n}则</a><span>2026-09-1{n}</span></li>'
        for n in range(1, 7)
    )
    html = f"""<html><body><div class="wrap">
      <div class="box"><ul>{_rows("m", range(25, 22, -1))}</ul></div>
      <div class="box"><ul>{side}</ul></div>
    </div></body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.publish_date.day for i in items] == [25, 24, 23]


def test_active_tab_class_does_not_split_tabs() -> None:
    html = f"""<html><body><div class="tabs">
      <div class="tab-pane active"><ul>{_rows("a", range(25, 22, -1))}</ul></div>
      <div class="tab-pane"><ul>{_rows("b", range(20, 16, -1))}</ul></div>
    </div></body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert len(items) == 7


def test_page_url_without_trailing_slash() -> None:
    hot = "".join(
        f'<li><a href="/zwgk/xwzx/{n}.html">其他栏目的热点新闻第{n}则</a><span>2026-09-1{n}</span></li>'
        for n in range(1, 7)
    )
    main = "".join(
        f'<li><a href="/zwgk/tzgg/{d}.html">关于开展第{d}项专项申报工作的通知</a><span>2026-09-{d}</span></li>'
        for d in (25, 24, 23)
    )
    html = f"""<html><body>
      <div class="left"><ul>{main}</ul></div>
      <div class="right"><ul>{hot}</ul></div>
    </body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/zwgk/tzgg", today=TODAY)
    assert [i.publish_date.day for i in items] == [25, 24, 23]


def test_script_list_beats_small_sidebar_from_other_directory() -> None:
    hot = "".join(
        f'<li><a href="/xwzx/{n}.html">其他栏目的热点新闻第{n}则</a><span>2026-09-0{n}</span></li>'
        for n in range(1, 4)
    )
    rows = ",".join(
        f'{{"title": "关于开展第{d}项专项申报工作的通知", "pubDate": "2026-09-{d}", "url": "/tzgg/{d}.html"}}'
        for d in (25, 24, 23, 22)
    )
    html = f"""<html><body><div class="right"><ul>{hot}</ul></div><div id="app"></div>
      <script>var listData = {{articleList: [{rows}]}};</script></body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.url.rsplit("/", 1)[-1] for i in items] == ["25.html", "24.html", "23.html", "22.html"]


def test_script_list_in_column_beats_bigger_recommend_array() -> None:
    rec = ",".join(
        f'{{"title": "推荐阅读的其他栏目文章第{n}篇", "pubDate": "2026-09-1{n}", "url": "/xwzx/{n}.html"}}'
        for n in range(1, 5)
    )
    rows = ",".join(
        f'{{"title": "关于开展第{d}项专项申报工作的通知", "pubDate": "2026-09-{d}", "url": "/tzgg/{d}.html"}}'
        for d in (25, 24, 23)
    )
    html = f"""<html><body><div id="app"></div>
      <script>var recommend = [{rec}]; var listData = {{articleList: [{rows}]}};</script></body></html>"""
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.url.rsplit("/", 1)[-1] for i in items] == ["25.html", "24.html", "23.html"]


def test_amount_after_date_does_not_replace_it() -> None:
    html = """<ul>
      <li><a href="/a/1.html">关于下达第一批专项补助资金的通知</a><span>09-20</span><span>10.25万元</span></li>
      <li><a href="/a/2.html">关于下达第二批专项补助资金的通知</a><span>09-18</span><span>8.50万元</span></li>
      <li><a href="/a/3.html">关于下达第三批专项补助资金的通知</a><span>09-15</span><span>12.05万元</span></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [i.publish_date for i in items] == [
        date(2026, 9, 20),
        date(2026, 9, 18),
        date(2026, 9, 15),
    ]
