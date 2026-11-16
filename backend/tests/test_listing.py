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


def test_tianjin_truncated_link_text_with_date_inside_uses_title_attribute() -> None:
    # 天津科技局（工信局同一模板）：<a title="完整标题">截断的标题...<span class="time">日期</span></a>。
    # 文字结尾是日期，曾看不出截断，入库的是"……"结尾的标题（10/16 验收缺陷 1）
    items = extract_items(
        read_fixture_html("tianjin_kjt_tzgg"),
        "https://kxjs.tj.gov.cn/ZWGK4143/TZGG2079/",
        today=TODAY,
    )
    by_page = {i.url.rsplit("/", 1)[-1]: i for i in items}
    assert len(items) == 20
    assert not [i.title for i in items if i.title.endswith(("...", "…"))]
    assert [
        (by_page[name].title, by_page[name].publish_date)
        for name in ("t20260831_7362409.html", "t20260818_7353950.html", "t20260817_7353261.html")
    ] == [
        (
            "市科技局关于组织申报国家重点研发计划“战略性科技创新合作”重点专项2026年度联合研发与示范项目的通知",
            date(2026, 8, 31),
        ),
        (
            "关于对《天津市推动生物制造创新发展行动计划（2026-2030年）（征求意见稿）》公开征求意见的通知",
            date(2026, 8, 17),
        ),
        (
            "关于组织申报国家重点研发计划“城镇可持续发展关键技术与装备”重点专项2026年度项目的通知",
            date(2026, 8, 17),
        ),
    ]
    # 发布日期仍取链接里写的日期，不改用网址里的（上面第二条网址是 0818，这条是 0819）
    assert by_page["t20260819_7354595.html"].publish_date == date(2026, 8, 18)


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


def test_archived_topic_links_are_not_documents() -> None:
    # 湖南工信厅的列表里夹着专题专栏的归档入口，不是文件
    html = """
    <ul>
      <li><a href="/gxt/xxgk/tzgg/202609/t20260924_1.html">关于开展2026年度专项申报工作的通知</a>
          <span>2026-09-24</span></li>
      <li><a href="/gxt/ztzl/2026hnlh/index.html">2026湖南两会（已归档</a><span>2026-09-20</span></li>
      <li><a href="/gxt/xxgk/tzgg/202609/t20260920_2.html">关于公布第二批专精特新中小企业名单的通知</a>
          <span>2026-09-20</span></li>
    </ul>
    """
    items = extract_items(html, "https://gxt.hunan.gov.cn/gxt/xxgk/tzgg/", today=TODAY)
    assert [i.title for i in items] == [
        "关于开展2026年度专项申报工作的通知",
        "关于公布第二批专精特新中小企业名单的通知",
    ]


def test_title_mentioning_archive_in_the_middle_is_kept() -> None:
    html = """
    <ul>
      <li><a href="/a/1.html">关于科技计划项目材料已归档情况的通知</a><span>2026-09-24</span></li>
      <li><a href="/a/2.html">关于开展2026年度专项申报工作的通知</a><span>2026-09-23</span></li>
    </ul>
    """
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.title for i in items] == [
        "关于科技计划项目材料已归档情况的通知",
        "关于开展2026年度专项申报工作的通知",
    ]


def test_markup_title_attribute_is_used_when_link_text_is_generic() -> None:
    # 链接文字只是"详情"时，用去掉标签后的 title 属性
    html = """
    <ul>
      <li><span>关于开展2026年度专项申报工作的通知</span>
          <a href="/b/1.html" title="<b>关于开展2026年度高新技术企业认定工作的通知</b>">详情</a>
          <span>2026-09-24</span></li>
      <li><span>第二条</span>
          <a href="/b/2.html" title="<b>关于公布第二批专精特新中小企业名单的通知</b>">详情</a>
          <span>2026-09-23</span></li>
    </ul>
    """
    items = extract_items(html, "https://kjt.example.gov.cn/tzgg/", today=TODAY)
    assert [i.title for i in items] == [
        "关于开展2026年度高新技术企业认定工作的通知",
        "关于公布第二批专精特新中小企业名单的通知",
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


def test_date_inside_link_text_does_not_hide_truncation() -> None:
    # 链接文字两端的日期不算标题：去掉后 title 属性是它的完整写法就用 title 属性，日期照样取链接里写的
    html = """<ul>
      <li><a href="/a/1.html" title="关于开展2026年度科技型中小企业评价工作的通知">关于开展2026年度科技型中小…<span>2026-09-20</span></a></li>
      <li><a href="/a/2.html" title="关于组织申报2026年度省重点研发计划项目的通知">关于组织申报2026年度省重点<span>2026-09-19</span></a></li>
      <li><a href="/a/3.html" title="关于公布第三批专精特新中小企业认定名单的公示">[2026-09-18] 关于公布第三批专精特新...</a></li>
      <li><a href="/a/4.html">关于征集2026年项目需求的通知 <span>2026-09-15</span></a></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于开展2026年度科技型中小企业评价工作的通知", date(2026, 9, 20)),
        # 截断时没加省略号：title 属性以链接文字开头、更长，也是完整写法
        ("关于组织申报2026年度省重点研发计划项目的通知", date(2026, 9, 19)),
        # 开头写日期的，以前取了 title 属性却丢了日期，网址里又没有日期，整条被丢掉
        ("关于公布第三批专精特新中小企业认定名单的公示", date(2026, 9, 18)),
        ("关于征集2026年项目需求的通知", date(2026, 9, 15)),
    ]


def test_title_attribute_not_completing_link_text_with_date_is_ignored() -> None:
    # 链接里写着日期、title 属性却不是链接文字的完整写法（"详情"这类无关文字），或者自己也带日期时，
    # 照旧用链接文字：宁可标题截断（补详情时再补全），也不要无关标题、丢条目或取错日期（PR 审阅反例）
    html = """<ul>
      <li><a href="/a/1.html" title="详情">关于开展2026年度专项申报工作的通知……<span>2026-09-20</span></a></li>
      <li><a href="/a/2.html" title="点击此处查看这条信息的详细内容">关于开展专项工作的通知<span>2026-09-19</span></a></li>
      <li><a href="/a/3.html" title="关于发布产业实施方案的公示（2026-01-01）">关于发布产业实施方案的公示...<span>2026-09-18</span></a></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=TODAY)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于开展2026年度专项申报工作的通知……", date(2026, 9, 20)),
        ("关于开展专项工作的通知", date(2026, 9, 19)),
        ("关于发布产业实施方案的公示...", date(2026, 9, 18)),
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


def test_ningxia_decoration_removed_from_text_attribute_and_json():
    import json

    title = "关于公开征求《宁夏回族自治区技术研发项目管理办法》意见的通知"
    html = f'<ul><li><span>2026-10-08</span><a href="./202610/t20261008_5365720.html"><em>》</em> {title}</a></li></ul>'
    url = "https://kjt.nx.gov.cn/kjdt/tzgg/"
    assert (
        extract_items(html, url, {"item_selector": "li", "date_selector": "span"})[0].title == title
    )
    with_attr = html.replace("<em>》</em>", "》").replace(
        "<a href=", f'<a title="》 {title}" href='
    )
    assert (
        extract_items(with_attr, url, {"item_selector": "li", "date_selector": "span"})[0].title
        == title
    )
    payload = {
        "articleList": [
            {
                "title": "》 " + title,
                "url": "./202610/t20261008_5365720.html",
                "publishTime": "2026-10-08",
            }
        ]
    }
    script = "<script>var listData = " + json.dumps(payload, ensure_ascii=False) + ";</script>"
    assert extract_items(script, url)[0].title == title

    from youju.crawler.titles import clean_title

    assert clean_title("《项目申报指南》") == "《项目申报指南》"
    assert clean_title("关于《项目指南》的通知") == "关于《项目指南》的通知"
    assert clean_title("》 》 关于《项目指南》的通知") == "关于《项目指南》的通知"


def test_clean_title_drops_body_text_stuck_after_the_title():
    """上海市经信委的列表把正文开头放进了链接文字（10/12 从线上的结果表里发现）：
    标题后面跟着空白，再跟称呼或正文第一句。剪掉后面，只留标题。"""
    from youju.crawler.titles import clean_title

    cases = {
        # 称呼
        "上海市经济信息化委关于发布重点领域新材料应用需求清单的通知 有关单位： 为做好新材料保障重点产业发展工作，"
        "更好发挥“揭榜挂帅”机制作用": "上海市经济信息化委关于发布重点领域新材料应用需求清单的通知",
        # 标题里的书名号内也有“通知”：从后面那个“通知”剪
        "关于转发《工业和信息化部办公厅关于开展2026年生物制造标志性产品征集工作的通知》的通知 有关单位： 根据《工业和"
        "信息化部办公厅关于开展2026年生物制造标志性产品征集工作的通知》": "关于转发《工业和信息化部办公厅关于开展"
        "2026年生物制造标志性产品征集工作的通知》的通知",
        "关于征集前沿材料应用场景创新成果推荐目录相关内容的通知 各有关单位：为推广实现工程化产业化": "关于征集前沿"
        "材料应用场景创新成果推荐目录相关内容的通知",
        # 正文第一句
        "关于第四批上海工业遗产拟认定名单的公示 为贯彻落实《国家工业遗产管理办法》的文件精神和工作要求": "关于第四批"
        "上海工业遗产拟认定名单的公示",
        "第15批《上海市创新产品推荐目录》公示 根据《上海市经济信息化委关于开展第15批上海市创新产品推荐目录编制申报"
        "工作的通知》": "第15批《上海市创新产品推荐目录》公示",
        "关于征求《上海市智能网联汽车测试与示范实施办法（修订稿）（征求意见稿）》 意见的公告 为抢抓智能网联汽车产业"
        "发展机遇，全力打通技术落地": "关于征求《上海市智能网联汽车测试与示范实施办法（修订稿）（征求意见稿）》 意见的公告",
    }
    for raw, expected in cases.items():
        assert clean_title(raw) == expected

    # 不该动的：几个机关联合发文（机关名之间有空格）、文种词后面的批次说明、正常标题、标题里的“为”字
    untouched = [
        "广东省工业和信息化厅 广东省科学技术厅 广东省财政厅 国家税务总局广东省税务局关于开展2026年度享受增值税加计"
        "抵减政策的先进制造业企业名单制定工作的通知",
        "关于组织申报2026年度省重点研发计划项目的通知 （第二批）",
        "关于组织开展2026年度绿色制造名单申报工作的通知",
        "关于第三轮中央生态环境保护督察 “高耗能设备淘汰不到位”问题事项整改情况的公示",
        "关于开展为企服务专项行动的通知",
        "关于公布2026年度专精特新中小企业名单的通知 附件",
        "通知 各有关单位：标题太短的不处理",
    ]
    for title in untouched:
        assert clean_title(title) == title


def test_title_attribute_with_publish_date_after_the_title_drops_the_date() -> None:
    # 济南人社 col18578（地市一组 11/12）：title 属性是"标题 + 空格 + 发布日期"，行里另写日期。
    # 曾整个用 title 属性，入库标题后面都带着"2026年11月05日"
    html = """<ul>
      <li><a href="/a/1.html" title="济南市劳动能力鉴定委员会劳动能力鉴定结论书送达公告 2026年11月05日">
        济南市劳动能力鉴定委员会劳动能力鉴定结论书送达公告</a><span>[2026-11-05]</span></li>
      <li><a href="/a/2.html" title="济南市人力资源和社会保障局2025年度法治政府建设报告 2026-01-16">
        济南市人力资源和社会保障局2025年度法治政府建设报告</a><span>[2026-01-16]</span></li>
      <li><a href="/a/3.html" title="关于对拟入选高技能人才培训基地名单进行公示的通知 2026年10月30日">
        关于对拟入选高技能人才培训基地名单进行...</a><span>[2026-10-30]</span></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=date(2026, 11, 12))
    assert [(i.title, i.publish_date) for i in items] == [
        ("济南市劳动能力鉴定委员会劳动能力鉴定结论书送达公告", date(2026, 11, 5)),
        ("济南市人力资源和社会保障局2025年度法治政府建设报告", date(2026, 1, 16)),
        # 链接文字截断时，用 title 属性的完整标题，同样不带日期
        ("关于对拟入选高技能人才培训基地名单进行公示的通知", date(2026, 10, 30)),
    ]


def test_title_attribute_keeps_dates_that_belong_to_the_title() -> None:
    # 只去掉"空格 + 完整年月日"、而且剩下的正是链接文字时才去：括号里的日期、
    # 标题中间的日期都是标题的一部分
    html = """<ul>
      <li><a href="/a/1.html" title="国务院政策例行吹风会文字实录（2026年11月5日）">
        国务院政策例行吹风会文字实录</a><span>2026-11-05</span></li>
      <li><a href="/a/2.html" title="关于调整2026年11月5日起执行的收费标准的通知">
        关于调整2026年11月5日起执行的收费标准的通知</a><span>2026-11-04</span></li>
      <li><a href="/a/3.html" title="政策例行吹风会文字实录 2026年11月5日">
        政策例行吹风会文字实录</a><span>2026-11-06</span></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=date(2026, 11, 12))
    assert [i.title for i in items] == [
        "国务院政策例行吹风会文字实录（2026年11月5日）",
        "关于调整2026年11月5日起执行的收费标准的通知",
        # 结尾的日期和这一行的发布日期不同：可能是会议日期，照旧留着（Codex 审阅 11/12）
        "政策例行吹风会文字实录 2026年11月5日",
    ]


def test_publish_date_written_only_in_the_title_attribute_is_still_used() -> None:
    # 行里、网址里都没写日期，只有 title 属性结尾、悬浮提示里写着：照旧用它当发布日期，
    # 标题不带它（Codex 审阅 11/12：只清标题不留日期会把整条漏掉）
    html = """<ul>
      <li><a href="/a/1.html" title="关于开展专项申报工作的通知 2026-11-05">关于开展专项申报工作的通知</a></li>
      <li><a href="/a/2.html" title="关于公布第三批认定名单的公示 2026年11月04日">关于公布第三批认定名单的公示</a></li>
      <li><a href="/a/3.html" title="标题：关于征集2026年项目需求的通知&#xD;&#xA;点击数：12&#xD;&#xA;发表时间：2026-11-03">关于征集2026年项目需求的通知</a></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=date(2026, 11, 12))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于开展专项申报工作的通知", date(2026, 11, 5)),
        ("关于公布第三批认定名单的公示", date(2026, 11, 4)),
        ("关于征集2026年项目需求的通知", date(2026, 11, 3)),
    ]


def test_tooltip_publish_date_wins_over_a_date_ending_the_title() -> None:
    # 悬浮提示里的标题以会议日期结尾、另写着发表时间，行里没写日期：发布日期取发表时间，
    # 标题里的会议日期留着（Codex 审阅第二轮 11/12）
    tip = "标题：政策吹风会文字实录 2026年11月5日&#xD;&#xA;点击数：0&#xD;&#xA;发表时间：2026-11-06"
    html = f"""<ul>
      <li><a href="/a/1.html" title="{tip}">政策吹风会文字实录 2026年11月5日</a></li>
      <li><a href="/a/2.html" title="标题：关于开展专项申报工作的通知&#xD;&#xA;点击数：3&#xD;&#xA;发表时间：2026-11-04">关于开展专项申报工作的通知</a></li>
      <li><a href="/a/3.html" title="标题：关于公布第三批认定名单的公示&#xD;&#xA;点击数：5&#xD;&#xA;发表时间：2026-11-03">关于公布第三批认定名单的公示</a></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/a/", today=date(2026, 11, 12))
    assert [(i.title, i.publish_date) for i in items] == [
        ("政策吹风会文字实录 2026年11月5日", date(2026, 11, 6)),
        ("关于开展专项申报工作的通知", date(2026, 11, 4)),
        ("关于公布第三批认定名单的公示", date(2026, 11, 3)),
    ]


def test_tooltip_in_title_attribute_gives_only_the_title() -> None:
    # 漯河、本溪、商丘、泸州一类平台（地市一组 11/9、11/12）：title 属性是悬浮提示
    # "标题：……（换行）点击数：0（换行）发表时间：……"，曾整段入库
    html = """<ul>
      <li><a href="/content_101.html" title="标题：本溪市发展和改革委员会关于宣布废止和失效部分行政规范性文件的通知&#xD;&#xA;点击数：0&#xD;&#xA;发表时间：2026-07-01">本溪市发展和改革委员会关于宣布废止和失效部分行政...</a><span>2026-07-01</span></li>
      <li><a href="/content_102.html" title="标题：政策解读：《四川省高标准农田工程设施管护实施办法》&#xD;&#xA;点击数：12&#xD;&#xA;发表时间：2026-03-03">政策解读：《四川省高标准农田工程设施管护实施办法》</a><span>2026-03-03</span></li>
      <li><a href="/content_103.html" title="标题：关于开展专项申报工作的通知
点击数：110
发表时间：2026-09-22">关于开展专项申报工作的通知</a><span>2026-09-22</span></li>
    </ul>"""
    items = extract_items(html, "https://example.gov.cn/", today=date(2026, 11, 12))
    assert [i.title for i in items] == [
        "本溪市发展和改革委员会关于宣布废止和失效部分行政规范性文件的通知",
        "政策解读：《四川省高标准农田工程设施管护实施办法》",
        "关于开展专项申报工作的通知",
    ]


def test_clean_title_takes_the_title_out_of_a_tooltip() -> None:
    # 历史题录重算（reclassify）也用它：库里已经整段入库的提示文字，按同一规则改回标题
    from youju.crawler.titles import clean_title

    assert (
        clean_title("标题：关于开展专项申报工作的通知 点击数：110 发表时间：2026-09-22")
        == "关于开展专项申报工作的通知"
    )
    # 只剩"标题："的提示文字（泸州一类平台的 title 属性只写"标题：……"）也去掉：11/12 试抓库 35,525 条
    # 标题里以"标题：""主题："开头的 194 条全是这种残留，正文标题不这样写
    assert clean_title("标题：关于开展专项申报工作的通知") == "关于开展专项申报工作的通知"
    assert (
        clean_title("标题：关于开展专项申报工作的通知 点击数：很多 发表时间：2026-09-22")
        == "关于开展专项申报工作的通知 点击数：很多 发表时间：2026-09-22"
    )
    # 不是以"标题："开头的照旧不动
    kept = "关于开展专项申报工作的通知 点击数：110 发表时间：2026-09-22"
    assert clean_title(kept) == kept


def test_clean_title_drops_list_artifacts() -> None:
    # 地市一组 11/12 复核查出、#482 之后还在的几种列表残留（重新解析缓存页面核对过）
    from youju.crawler.titles import clean_title

    for raw, expected in (
        # 临沂人社：链接文字"主题：……（换行）日期：……"，有的夹着零宽空格
        (
            "主题：2026年重点企业线上招聘会（第三十九期） 日期：2026-11-09",
            "2026年重点企业线上招聘会（第三十九期）",
        ),
        (
            "主题：\u200b 某市人力资源和社会保障局等14部门关于打造“就业之家”的实施意见 日期：2026-09-30",
            "某市人力资源和社会保障局等14部门关于打造“就业之家”的实施意见",
        ),
        # 武汉财政：日期拆成两块写在标题前面
        (
            "09-30 2026 关于超长期特别国债资金全流程管理接口联调安排的通知",
            "关于超长期特别国债资金全流程管理接口联调安排的通知",
        ),
        # 苏州文旅：网址里的 32 位编号粘在标题后面
        (
            "市政府关于印发苏州市促进入境旅游若干措施的通知d17c290e3d3f43d8b6eb78420021603b",
            "市政府关于印发苏州市促进入境旅游若干措施的通知",
        ),
    ):
        assert clean_title(raw) == expected
    # 标题本身带的日期、年份、编号不动
    for kept in (
        "关于调整2026年11月5日起执行的收费标准的通知",
        "2026-2028年数字经济发展行动方案",
        "政策例行吹风会文字实录 2026年11月5日",
        "关于公布第abc123批名单的通知",
        "关于某某工作的通知（日期：2026-11-09）",
    ):
        assert clean_title(kept) == kept


def test_without_publish_date_drops_only_the_publish_date() -> None:
    # 历史题录重算：标题末尾的日期和这份文件的发布日期相同才去掉（和列表页 _without_attr_date 一样）
    from youju.crawler.titles import without_publish_date

    assert (
        without_publish_date(
            "济南市劳动能力鉴定委员会劳动能力鉴定结论书送达公告 2026年11月05日", date(2026, 11, 5)
        )
        == "济南市劳动能力鉴定委员会劳动能力鉴定结论书送达公告"
    )
    assert (
        without_publish_date(
            "2026年淄博市大数据局部门政府购买服务指导性目录2026年04月24日", date(2026, 4, 24)
        )
        == "2026年淄博市大数据局部门政府购买服务指导性目录"
    )
    assert (
        without_publish_date("关于开展某某专项工作的通知 2026-11-05", date(2026, 11, 5))
        == "关于开展某某专项工作的通知"
    )
    # 和发布日期不同（纪要写的会议日期）、没有发布日期、去掉后太短的，都不动
    for title, published in (
        ("政策例行吹风会文字实录 2026年11月5日", date(2026, 11, 6)),
        ("政策例行吹风会文字实录 2026年11月5日", None),
        ("通知 2026年11月05日", date(2026, 11, 5)),
        ("某某工作通知 2026年13月45日", date(2026, 11, 5)),
    ):
        assert without_publish_date(title, published) == title


def test_interpretation_link_after_the_title_is_not_another_item() -> None:
    # 龙岩各局政策文件（地市一组 11/12）：每份文件后面跟一个红字"(查看政策解读)"，链到解读栏目。
    # 曾当成另一条入库，库里每个栏目有 15–20 条标题就是"(查看政策解读)"
    html = """<ul class="zx_ul">
      <li><span>2026-09-20</span><a href="./202609/t20260920_1.htm" title="龙岩市发展和改革委员会等四部门关于优化中心城区停车收费政策的通告">龙岩市发展和改革委员会等四部门关于优化中心城区停车收费政策的通告</a><a href="http://fgw.longyan.gov.cn/zwgk/zcjd/202609/t20260920_2.htm" style="color:#F00">(查看政策解读)</a></li>
      <li><span>2026-07-01</span><a href="./202607/t20260723_3.htm" title="龙岩市人民政府关于印发龙岩市营商环境建设规划的通知">龙岩市人民政府关于印发龙岩市营商环境建设规划的通知</a><a href="http://fgw.longyan.gov.cn/zwgk/zcjd/202607/t20260723_4.htm" style="color:#F00">（查看政策解读）</a></li>
      <li><span>2026-06-29</span><a href="./202606/t20260629_5.htm" title="关于开展2026年度价格监测工作的通知">关于开展2026年度价格监测工作的通知</a></li>
      <li><span>2026-06-20</span><a href="../zcjd/202606/t20260620_6.htm" title="《龙岩市某某管理办法》政策解读">《龙岩市某某管理办法》政策解读</a><a href="./202606/t20260618_7.htm" style="color:#F00">(查看政策文件)</a></li>
    </ul>"""
    items = extract_items(html, "http://fgw.longyan.gov.cn/zwgk/zcwj/", today=date(2026, 11, 12))
    assert [(i.title, i.publish_date) for i in items] == [
        ("龙岩市发展和改革委员会等四部门关于优化中心城区停车收费政策的通告", date(2026, 9, 20)),
        ("龙岩市人民政府关于印发龙岩市营商环境建设规划的通知", date(2026, 7, 1)),
        ("关于开展2026年度价格监测工作的通知", date(2026, 6, 29)),
        # 反方向的"(查看政策文件)"也不算一条
        ("《龙岩市某某管理办法》政策解读", date(2026, 6, 20)),
    ]


def test_zero_width_space_in_the_link_does_not_turn_title_dates_into_the_row_date() -> None:
    # 襄阳政府规章列表（11/12 重放缓存发现）：链接里夹着零宽空格，标题去掉了它、行文字没去，找行日期时
    # 挖不掉标题，标题里的"2017年4月26日"被当成行日期，这一条落进别的列表块、整条丢了
    html = """<table>
      <tr><td>1</td><td><a href="./zfl/202512/t20251224_1.shtml"><p>某市气象灾害防御办法</p>
        <span>（2025年12月12日市人民政府第129次常务会议审议通过）</span></a></td></tr>
      <tr><td>2</td><td><a href="./zfl/202411/t20241112_2.shtml"><p>某市市区公共租赁住房管理办法\u200b</p>
        <span>（2017年4月26日某市人民政府令第3号公布 根据2024年11月12日决定修正）</span></a></td></tr>
      <tr><td>3</td><td><a href="./zfl/202301/t20230106_3.shtml"><p>某市城市绿化管理办法</p>
        <span>(2023年1月4日某市人民政府令第11号公布)</span></a></td></tr>
    </table>"""
    items = extract_items(
        html, "http://xxgk.example.gov.cn/szf/zfxxgk/zc/", today=date(2026, 11, 12)
    )
    assert [(i.url.rsplit("/", 1)[-1], i.publish_date) for i in items] == [
        ("t20251224_1.shtml", date(2025, 12, 24)),
        ("t20241112_2.shtml", date(2024, 11, 12)),
        ("t20230106_3.shtml", date(2023, 1, 6)),
    ]
    assert "\u200b" not in items[1].title


def test_month_range_at_the_start_of_the_title_is_not_the_publish_date() -> None:
    # 襄阳财政"规范性文件"列表（#501 留下的）：标题开头是"2026年1-6月"这类月份区间，发布日期写在链接里
    # 的 span 里。原来开头的"2026年1-6"被当成 2026-01-06 挖掉，标题剩"月某市……清理表2026-06-30"。
    # 网址里不带日期，日期只能从 span 里来
    html = """<ul class="info-list">
      <li><a href="./zcfg/info_1.shtml">2026年1-6月某市财政局规范性文件清理表<span>2026-06-30</span></a></li>
      <li><a href="./zcfg/info_2.shtml">2025年10-12月某市财政局规范性文件清理表<span>2026-01-06</span></a></li>
      <li><a href="./zcfg/info_3.shtml">2025年7-9月某市财政局规范性文件清理表<span>2025-09-30</span></a></li>
    </ul>"""
    items = extract_items(html, "http://czj.example.gov.cn/zwgk/zc/", today=date(2026, 11, 13))
    assert [(i.title, i.publish_date) for i in items] == [
        ("2026年1-6月某市财政局规范性文件清理表", date(2026, 6, 30)),
        ("2025年10-12月某市财政局规范性文件清理表", date(2026, 1, 6)),
        ("2025年7-9月某市财政局规范性文件清理表", date(2025, 9, 30)),
    ]


def test_title_attribute_with_publish_date_glued_to_the_title_drops_the_date() -> None:
    # 淄博大数据局（地市一组 11/12）：title 属性是"标题 + 发布日期"，中间没有空格，行里另写同一个日期
    html = """<ul>
      <li><a href="/art/2026/4/24/art_1.html" title="2026年淄博市大数据局部门政府购买服务指导性目录2026年04月24日">        2026年淄博市大数据局部门政府购买服务指导性目录    </a><span>        2026-04-24    </span></li>
      <li><a href="/art/2026/8/25/art_2.html" title="关于组织开展数据流通服务典型案例征集工作的通知2026年08月25日">关于组织开展数据流通服务典型案例征集工作的通知</a><span>2026-08-25</span></li>
      <li><a href="/art/2026/7/30/art_3.html" title="2026年“数据要素×”大赛拟获奖名单公示2026年07月30日">2026年“数据要素×”大赛拟获奖名单公示</a><span>2026-07-30</span></li>
    </ul>"""
    items = extract_items(
        html, "https://dsj.zibo.gov.cn/col/col10910/index.html", today=date(2026, 11, 12)
    )
    assert [(i.title, i.publish_date) for i in items] == [
        ("2026年淄博市大数据局部门政府购买服务指导性目录", date(2026, 4, 24)),
        ("关于组织开展数据流通服务典型案例征集工作的通知", date(2026, 8, 25)),
        ("2026年“数据要素×”大赛拟获奖名单公示", date(2026, 7, 30)),
    ]


def test_bracket_tags_in_link_text_stay() -> None:
    # 链接文字里的方括号标签照旧保留：栏目名和"【一图读懂】""[征求意见稿]""[已废止]"这类说明是什么、
    # 现在怎样的标签分不开，去掉可能把解读、征求意见稿读成正式文件（临沂人社 title="主题：……"那种也不去）
    html = """<ul class="list">
      <li><a href="./202609/t20260920_1.html" title="某市促进民营经济发展若干措施">【一图读懂】某市促进民营经济发展若干措施</a><span>2026-09-20</span></li>
      <li><a href="./202609/t20260921_2.html" title="主题：关于开展第三批项目申报的通知 日期：2026-09-21">[公示公告]关于开展第三批项目申报的通知</a><span>2026-09-21</span></li>
      <li><a href="./202609/t20260922_3.html" title="主题：某市企业技术中心管理办法 日期：2026-09-22">[征求意见稿]某市企业技术中心管理办法</a><span>2026-09-22</span></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/zcjd/", today=date(2026, 11, 16))
    assert [i.title for i in items] == [
        "【一图读懂】某市促进民营经济发展若干措施",
        "[公示公告]关于开展第三批项目申报的通知",
        "[征求意见稿]某市企业技术中心管理办法",
    ]


def test_luzhou_popup_in_the_title_cell_does_not_hide_the_row() -> None:
    # 泸州政府文件：标题格里除了文件链接，还藏着一个"政策解读"弹层（display: none），里面是解读的链接。
    # 原来找"一行"时把弹层里的链接当成别的文件，走不到写日期的格子，这一行整行漏掉；解读另有栏目收
    html = """<table class="list"><tbody>
      <tr><td class="number"><span>01</span></td><td class="title">
        <a href="/zw/zcwjs/szfwj/lsfbf/content_1160526" title="标题：某市人民政府办公室关于印发《某市深入实施“人工智能+”行动方案》的通知">某市人民政府办公室关于印发《某市深入实施“人工智能+”行动方案》的通知</a>
        <div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><ul class="address-list">
          <li><a href="/zw/zcwjs/zcjd/yspjd/content_1160550">音频解读：《某市深入实施“人工智能+”行动方案》的政策解读</a></li>
          <li><a href="/zw/zcwjs/zcjd/wzjd/content_1160548">《某市深入实施“人工智能+”行动方案》的政策解读</a></li>
        </ul></div></div></td><td class="refno">某府办发〔2026〕11号</td><td class="date">2026-10-20</td><td class="date">2026-10-23</td></tr>
      <tr><td class="number"><span>02</span></td><td class="title">
        <a href="/zw/zcwjs/szfwj/lsfbf/content_1158411" title="标题：关于某县2026年第1批次土地征收的批复">关于某县2026年第1批次土地征收的批复</a>
        </td><td class="refno">某府函〔2026〕90号</td><td class="date">2026-09-28</td><td class="date">2026-09-30</td></tr>
      <tr><td class="number"><span>03</span></td><td class="title">
        <a href="/zw/zcwjs/szfwj/lsfg/content_1157502" title="标题：某市人民政府关于印发《某市重点项目管理办法》的通知">某市人民政府关于印发《某市重点项目管理办法》的通知</a>
        <div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><ul class="address-list">
          <li><a href="/zw/zcwjs/zcjd/wzjd/content_1157601">《某市重点项目管理办法》的政策解读</a></li>
        </ul></div></div></td><td class="refno">某府规〔2026〕1号</td><td class="date">2026-09-18</td><td class="date">2026-09-20</td></tr>
    </tbody></table>"""
    items = extract_items(
        html, "https://www.example.gov.cn/zw/zcwjs/szfwj", today=date(2026, 11, 16)
    )
    assert [(i.title, i.publish_date) for i in items] == [
        ("某市人民政府办公室关于印发《某市深入实施“人工智能+”行动方案》的通知", date(2026, 10, 23)),
        ("关于某县2026年第1批次土地征收的批复", date(2026, 9, 30)),
        ("某市人民政府关于印发《某市重点项目管理办法》的通知", date(2026, 9, 20)),
    ]


def test_luzhou_popup_before_the_link_keeps_the_row_in_the_list() -> None:
    # 泸州农业农村局：日期写在行首的 span 里，弹层在文件链接前面，提示文字里也写着发表时间。原来这一行
    # 找不到行、单算一组，整行漏掉
    html = """<ul class="list">
      <li class=""><span class="date">2026-10-21</span>
        <a href="https://nyncj.example.gov.cn/zwgk/lzyj/zcjd/content_1135667" class="address">政策解读</a>
        <a href="/zwgk/lzyj/zcfg/content_1135665" title="标题：某省某某管理办法的通知&#xD;&#xA;点击数：0&#xD;&#xA;发表时间：2026-10-21">某省某某管理办法的通知</a></li>
      <li class=""><span class="date">2026-09-27</span>
        <div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><ul class="address-list">
          <li><a href="/zwgk/lzyj/zcjd/content_962600">关于《某市某某实施方案》的政策解读</a></li>
          <li><a href="/rdwdzsk/zczx/content_1072882">某市某某实施方案问答</a></li>
        </ul></div></div>
        <a href="/zwgk/lzyj/zcfg/content_1061358" title="标题：某市某某实施方案的通知&#xD;&#xA;点击数：0&#xD;&#xA;发表时间：2026-09-27">某市某某实施方案的通知</a></li>
      <li class=""><span class="date">2026-08-27</span>
        <a href="/zwgk/lzyj/zcfg/content_1083382" title="标题：关于某某工作的通知&#xD;&#xA;点击数：0&#xD;&#xA;发表时间：2026-08-27">关于某某工作的通知</a></li>
    </ul>"""
    items = extract_items(html, "https://nyncj.example.gov.cn/zwgk/lzyj", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("某省某某管理办法的通知", date(2026, 10, 21)),
        ("某市某某实施方案的通知", date(2026, 9, 27)),
        ("关于某某工作的通知", date(2026, 8, 27)),
    ]


def test_date_inside_a_luzhou_popup_is_not_the_row_date() -> None:
    # 弹层里链着解读、写着解读的日期：不是这一行的发布日期
    html = """<table class="list"><tbody>
      <tr><td class="title"><a href="1.html">关于项目申报工作的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="2.html">相关政策解读说明</a><span>2026-09-01</span></div></div></td><td>2026-10-21</td></tr>
      <tr><td class="title"><a href="3.html">关于公布第二批名单的通知</a></td><td>2026-10-20</td></tr>
      <tr><td class="title"><a href="4.html">关于征集典型案例的通知</a></td><td>2026-10-19</td></tr>
    </tbody></table>"""
    items = extract_items(html, "http://example.gov.cn/tzgg/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报工作的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]


def test_luzhou_popup_row_takes_the_date_the_old_way_first() -> None:
    # 有解读弹层的行原来找不到行，日期靠提示文字、链接文字、网址：这些地方有日期就照旧用（行里写的
    # 可能是成文日期），都没有（原来整行漏掉）才用行里写的。先看提示文字里的发表时间
    html = """<ul class="list">
      <li><a href="/a/1.html" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="/b/1.html">关于项目申报工作的政策解读</a></div></div><span>2026-09-01</span></li>
      <li><a href="/a/2.html" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="/b/2.html">关于第二批名单的政策解读</a></div></div><span>2026-09-02</span></li>
      <li><a href="/a/3.html" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="/b/3.html">关于典型案例征集的政策解读</a></div></div><span>2026-09-03</span></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]

    # 网址里带着日期
    html = """<ul class="list">
      <li><a href="/a/202610/t20261021_1.html">关于项目申报的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="/b/1.html">关于项目申报工作的政策解读</a></div></div><span>2026-09-01</span></li>
      <li><a href="/a/202610/t20261020_2.html">关于公布第二批名单的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="/b/2.html">关于第二批名单的政策解读</a></div></div><span>2026-09-02</span></li>
      <li><a href="/a/202610/t20261019_3.html">关于征集典型案例的通知</a><div class="address"><span>政策解读</span><div class="address-popup" style="display: none;"><a href="/b/3.html">关于典型案例征集的政策解读</a></div></div><span>2026-09-03</span></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]


def test_luzhou_popup_row_goes_with_the_other_rows() -> None:
    # 别的行日期只写在提示文字里（照原来按链接分组），只有带弹层的那一行另写着行内日期：这一行照样
    # 和别的行算一张列表，日期用提示文字里的
    html = """<ul class="list">
      <li><a href="/a/1.html" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a></li>
      <li><a href="/a/2.html" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a><div class="address-popup" style="display: none;"><a href="/b/2.html">关于第二批名单的政策解读</a></div><span class="date">2026-09-20</span></li>
      <li><a href="/a/3.html" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a></li>
      <li><a href="/a/4.html" title="标题：关于开展第三批项目申报的通知 点击数：0 发表时间：2026-10-18">关于开展第三批项目申报的通知</a></li>
    </ul>"""
    today = date(2026, 11, 16)
    items = extract_items(html, "http://example.gov.cn/a/", today=today)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
        ("关于开展第三批项目申报的通知", date(2026, 10, 18)),
    ]
    # 那一行的提示文字里没写发表时间（原来整行漏掉）：用行里写的日期，照样和别的行算一张列表
    html = html.replace(
        ' title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20"', ""
    )
    items = extract_items(html, "http://example.gov.cn/a/", today=today)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 9, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
        ("关于开展第三批项目申报的通知", date(2026, 10, 18)),
    ]


def test_dated_links_inside_luzhou_popups_are_not_items() -> None:
    # 弹层里的解读链接也带着各自的发表时间：它们不当条目（解读另有栏目收），列表照旧是三份正文
    html = """<ul class="list">
      <li><a href="/a/1.html" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a><div class="address-popup" style="display: none;"><a href="/b/1.html" title="标题：关于项目申报工作的政策解读 点击数：0 发表时间：2026-10-25">关于项目申报工作的政策解读</a></div><span>2026-10-21</span></li>
      <li><a href="/a/2.html" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a><div class="address-popup" style="display: none;"><a href="/b/2.html" title="标题：关于第二批名单的政策解读 点击数：0 发表时间：2026-10-26">关于第二批名单的政策解读</a></div><span>2026-10-20</span></li>
      <li><a href="/a/3.html" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a><div class="address-popup" style="display: none;"><a href="/b/3.html" title="标题：关于典型案例征集的政策解读 点击数：0 发表时间：2026-10-27">关于典型案例征集的政策解读</a></div><span>2026-10-19</span></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [i.title for i in items] == [
        "关于项目申报的通知",
        "关于公布第二批名单的通知",
        "关于征集典型案例的通知",
    ]


def test_luzhou_popup_repeating_a_visible_document_follows_main() -> None:
    # 同一行两份看得见的文件，弹层里又链着第二份：那是看得见的文件，不算弹层里的别的文件，
    # 这一行照原来读（两份各用提示文字里的发表时间）
    html = """<ul class="list">
      <li><a href="/a/1.html" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a><a href="/a/1b.html" title="标题：关于项目申报的补充说明 点击数：0 发表时间：2026-10-22">关于项目申报的补充说明</a><div class="address-popup" style="display: none;"><a href="/a/1b.html">关于项目申报的补充说明</a></div><span>2026-09-01</span></li>
      <li><a href="/a/2.html" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a><a href="/a/2b.html" title="标题：关于第二批名单的补充说明 点击数：0 发表时间：2026-10-23">关于第二批名单的补充说明</a><div class="address-popup" style="display: none;"><a href="/a/2b.html">关于第二批名单的补充说明</a></div><span>2026-09-02</span></li>
      <li><a href="/a/3.html" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a><a href="/a/3b.html" title="标题：关于典型案例的补充说明 点击数：0 发表时间：2026-10-24">关于典型案例的补充说明</a><div class="address-popup" style="display: none;"><a href="/a/3b.html">关于典型案例的补充说明</a></div><span>2026-09-03</span></li>
    </ul>"""
    dates = {
        i.url: i.publish_date
        for i in extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    }
    assert dates == {
        "http://example.gov.cn/a/1.html": date(2026, 10, 21),
        "http://example.gov.cn/a/1b.html": date(2026, 10, 22),
        "http://example.gov.cn/a/2.html": date(2026, 10, 20),
        "http://example.gov.cn/a/2b.html": date(2026, 10, 23),
        "http://example.gov.cn/a/3.html": date(2026, 10, 19),
        "http://example.gov.cn/a/3b.html": date(2026, 10, 24),
    }


def test_attachments_in_a_luzhou_popup_are_read_as_before() -> None:
    # 弹层里只有附件（各自写着发表时间）：附件不算别的文件，照原来读，不当附带链接跳过
    # （这种写法 main 读出的是附件那一块）
    rows = "".join(
        f'<li><a href="/a{n}.htm" title="标题：关于项目申报工作的通知{n} 点击数：0 发表时间：2026-10-2{n}">'
        f"关于项目申报工作的通知{n}</a>"
        f'<div class="address-popup" style="display:none">'
        f'<a href="/a{n}.pdf" title="标题：关于项目申报工作的附件{n} 点击数：0 发表时间：2026-10-2{n}">'
        f"关于项目申报工作的附件{n}</a>"
        f'<a href="/b{n}.pdf" title="标题：关于项目申报工作的材料{n} 点击数：0 发表时间：2026-10-2{n}">'
        f"关于项目申报工作的材料{n}</a></div></li>"
        for n in range(1, 4)
    )
    urls = {
        i.url
        for i in extract_items(
            f"<ul>{rows}</ul>", "http://example.gov.cn/", today=date(2026, 11, 16)
        )
    }
    assert {f"http://example.gov.cn/{x}{n}.pdf" for x in "ab" for n in range(1, 4)} <= urls


def test_popup_beside_a_wrapped_document_link_is_read_as_before() -> None:
    # 文件链接外面多套了一层（<span><a>…</a></span>），弹层不在文件链接的父元素里：不算附带链接，
    # 行和弹层里的链接都照原来读（这种写法 main 读出的是弹层里带发表时间的解读）
    rows = "".join(
        f'<li><span><a href="/a/{n}.html">关于开展第{n}批项目申报工作的通知</a></span>'
        f'<div class="address-popup" style="display:none"><a href="/b/{n}.html" '
        f'title="标题：关于第{n}批项目申报工作的政策解读 点击数：0 发表时间：2026-10-2{n}">'
        f"关于第{n}批项目申报工作的政策解读</a></div></li>"
        for n in range(1, 4)
    )
    urls = {
        i.url
        for i in extract_items(
            f"<ul>{rows}</ul>", "http://example.gov.cn/a/", today=date(2026, 11, 16)
        )
    }
    assert {f"http://example.gov.cn/b/{n}.html" for n in range(1, 4)} <= urls


def test_same_document_pdf_in_a_luzhou_popup_follows_main() -> None:
    # 弹层里只是同一份文件的 PDF：附件照原来的规矩属于同一行、不算别的文件，日期照原来取行里写的
    html = """<ul class="list">
      <li><a href="/a/1.html" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-09-01">关于项目申报的通知</a><div class="address-popup" style="display: none;"><a href="/a/1.pdf">关于项目申报的通知（PDF版）</a></div><span>2026-10-21</span></li>
      <li><a href="/a/2.html" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-09-02">关于公布第二批名单的通知</a><div class="address-popup" style="display: none;"><a href="/a/2.pdf">关于公布第二批名单的通知（PDF版）</a></div><span>2026-10-20</span></li>
      <li><a href="/a/3.html" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-09-03">关于征集典型案例的通知</a><div class="address-popup" style="display: none;"><a href="/a/3.pdf">关于征集典型案例的通知（PDF版）</a></div><span>2026-10-19</span></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]


def test_other_hidden_containers_are_read_as_before() -> None:
    # 只认泸州弹层的确切写法（class="address-popup"、藏着）：别的藏起来的容器照原来读，
    # 和不藏的写法读出来一样；class 对、却没藏着的也一样
    html = """<ul class="list">
      <li><a href="/a/1.html">关于项目申报的通知</a><div class="card" style="display:none"><a href="/b/1.html">关于项目申报工作的政策解读</a><span>2026-09-01</span></div><span>2026-10-21</span></li>
      <li><a href="/a/2.html">关于公布第二批名单的通知</a><div class="card" style="display:none"><a href="/b/2.html">关于第二批名单的政策解读</a><span>2026-09-02</span></div><span>2026-10-20</span></li>
      <li><a href="/a/3.html">关于征集典型案例的通知</a><div class="card" style="display:none"><a href="/b/3.html">关于典型案例征集的政策解读</a><span>2026-09-03</span></div><span>2026-10-19</span></li>
    </ul>"""
    today = date(2026, 11, 16)
    shown = extract_items(
        html.replace(' style="display:none"', ""), "http://example.gov.cn/a/", today=today
    )
    assert extract_items(html, "http://example.gov.cn/a/", today=today) == shown
    not_hidden = html.replace('class="card" style="display:none"', 'class="address-popup"')
    assert extract_items(not_hidden, "http://example.gov.cn/a/", today=today) == shown


def test_assistant_link_is_dropped_only_next_to_its_visible_document() -> None:
    import lxml.html

    from youju.crawler.listing import _Page

    def documents(html: str) -> list[bool]:
        # 每个链接算不算条目候选：是文件链接、又不是附带链接
        doc = lxml.html.document_fromstring(html)
        page = _Page(doc, "http://example.gov.cn/a/", date(2026, 11, 16))
        links = [(a, page.link(a)) for a in doc.xpath("//a")]
        return [link is not None and not page.companion(a, link[1]) for a, link in links]

    ai = (
        '<a href="/a/1.htm?assistant=true" class="tertonAIReaderFlag glwj" '
        'style="display: none; color: #0059a9;"> | AI阅读</a>'
    )
    # 看得见的文件链接旁边藏着的"同一网址加 assistant=true"：不是文件
    assert documents(f'<li><a href="/a/1.htm">关于项目申报工作的通知</a>{ai}</li>') == [True, False]
    # 网址里原来就有参数时用 &assistant=true
    assert documents(
        '<li><a href="/a/show.asp?id=1">关于项目申报工作的通知</a>'
        '<a href="/a/show.asp?id=1&amp;assistant=true" class="tertonAIReaderFlag" '
        'style="display:none"> | AI阅读</a></li>'
    ) == [True, False]
    # 同一份文件的链接本身也藏着、不在附带链接的父元素里、或者网址对不上：照旧算文件
    doc = '<a href="/a/1.htm">关于项目申报工作的通知</a>'
    hidden_doc = '<a href="/a/1.htm" style="display:none">关于项目申报工作的通知</a>'
    assert documents(f"<li>{hidden_doc}{ai}</li>") == [True, True]
    assert documents(f"<li>{doc}<span>{ai}</span></li>") == [True, True]
    # 父元素里套在下一层的也算同一格子
    assert documents(f"<li><span>{doc}</span>{ai}</li>") == [True, False]
    other_doc = '<a href="/a/2.htm">关于项目申报工作的通知</a>'
    assert documents(f"<li>{other_doc}{ai}</li>") == [True, True]
    # 链接没藏着（style 里最后生效的是 block）：照旧算文件
    assert documents(
        '<li><a href="/a/1.htm">关于项目申报工作的通知</a>'
        '<a href="/a/1.htm?assistant=true" class="tertonAIReaderFlag" '
        'style="display:none;display:block"> | AI阅读</a></li>'
    ) == [True, True]
    # 没有阅读助手的 class：不是这种附带链接，照旧算文件（可能是另一份文件，如补充通知）
    assert documents(
        '<li><a href="/a/1.htm">关于项目申报工作的通知</a>'
        '<a href="/a/1.htm?assistant=true" style="display:none">关于项目申报工作的补充通知</a></li>'
    ) == [True, True]


def test_rows_with_an_assistant_link_keep_the_date_they_had() -> None:
    # 原来这种行把"| AI阅读"当成别的文件、找不到行，日期靠提示文字、链接文字、网址：这些地方有日期
    # 就照旧用（行里写的可能是成文日期），都没有才用行里写的
    html = """<ul class="list">
      <li><span>2026-09-01</span><a href="/a/1.htm" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a><a href="/a/1.htm?assistant=true" class="tertonAIReaderFlag" style="display:none">| AI阅读</a></li>
      <li><span>2026-09-02</span><a href="/a/2.htm" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a><a href="/a/2.htm?assistant=true" class="tertonAIReaderFlag" style="display:none">| AI阅读</a></li>
      <li><span>2026-09-03</span><a href="/a/3.htm" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a><a href="/a/3.htm?assistant=true" class="tertonAIReaderFlag" style="display:none">| AI阅读</a></li>
    </ul>"""
    today = date(2026, 11, 16)
    items = extract_items(html, "http://example.gov.cn/a/", today=today)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]
    # 提示文字里没写、网址里也没有日期：用行里写的（原来整行漏掉）
    plain = html.replace(" 点击数：0 发表时间：2026-10-21", "").replace(
        " 点击数：0 发表时间：2026-10-20", ""
    )
    plain = plain.replace(" 点击数：0 发表时间：2026-10-19", "")
    items = extract_items(plain, "http://example.gov.cn/a/", today=today)
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 9, 1)),
        ("关于公布第二批名单的通知", date(2026, 9, 2)),
        ("关于征集典型案例的通知", date(2026, 9, 3)),
    ]


def test_lists_keep_their_page_order_when_they_score_the_same() -> None:
    # 带附带链接的行等别的行分完才归块；归完以后列表块照原来按页面上的先后排，评分一样时取靠前的那块。
    # main 读的也是第一张表（外加三条"| AI阅读"）
    html = """<div class="a"><ul>
      <li><span>2026-10-21</span><a href="/a/202610/t20261021_1.htm">关于项目申报的通知</a><a href="/a/202610/t20261021_1.htm?assistant=true" class="tertonAIReaderFlag" style="display:none">| AI阅读</a></li>
      <li><span>2026-10-20</span><a href="/a/202610/t20261020_2.htm">关于公布第二批名单的通知</a><a href="/a/202610/t20261020_2.htm?assistant=true" class="tertonAIReaderFlag" style="display:none">| AI阅读</a></li>
      <li><span>2026-10-19</span><a href="/a/202610/t20261019_3.htm">关于征集典型案例的通知</a><a href="/a/202610/t20261019_3.htm?assistant=true" class="tertonAIReaderFlag" style="display:none">| AI阅读</a></li>
    </ul></div>
    <div class="b"><ul>
      <li><span>2026-10-21</span><a href="/a/4.htm">关于开展第三批项目申报的通知</a></li>
      <li><span>2026-10-20</span><a href="/a/5.htm">关于公布第三批名单的通知</a></li>
      <li><span>2026-10-19</span><a href="/a/6.htm">关于征集优秀案例的通知</a></li>
    </ul></div>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [i.title for i in items] == [
        "关于项目申报的通知",
        "关于公布第二批名单的通知",
        "关于征集典型案例的通知",
    ]


def test_publish_date_kept_in_a_hidden_span_is_still_the_date() -> None:
    # 苏州工信首页：发布日期写在藏起来的 span 里（没有链接），照旧是这一条的日期
    html = """<ul>
      <li><h4><a href="/ggl/202610/a1.shtml" title="某市先导产业促进中心2025年度项目支出绩效自评">某市先导产业促进中心2025年度项目支出绩效自评</a><span class="time" style=" display:none;"><em>2026-10-08</em></span></h4></li>
      <li><h4><a href="/ggl/202610/a2.shtml" title="某市企业服务中心2025年度单位决算公开">某市企业服务中心2025年度单位决算公开</a><span class="time" style=" display:none;"><em>2026-10-09</em></span></h4></li>
      <li><h4><a href="/ggl/202610/a3.shtml" title="关于公示化工重点监测点认定审核意见的通知">关于公示化工重点监测点认定审核意见的通知</a><span class="time" style=" display:none;"><em>2026-10-29</em></span></h4></li>
    </ul>"""
    items = extract_items(html, "http://gxj.example.gov.cn/ggl/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("某市先导产业促进中心2025年度项目支出绩效自评", date(2026, 10, 8)),
        ("某市企业服务中心2025年度单位决算公开", date(2026, 10, 9)),
        ("关于公示化工重点监测点认定审核意见的通知", date(2026, 10, 29)),
    ]


def test_link_that_is_itself_hidden_is_not_a_document() -> None:
    # 福建省政府最新文件：每行文件链接后面藏着一个"| AI阅读"链接（同一网址加 ?assistant=true，
    # 链接本身 display:none）。它不是文件，原来会和文件一起读成条目
    html = """<ul>
      <li class="inc_list_li"><span class="longdate"> 2026-11-03 </span> <a href="/zwgk/zxwj/szfwj/202611/t20261103_1.htm" title="某省人民政府关于某市2026年度第三批次农用地转用的批复"> 某省人民政府关于某市2026年度第三批次农用地转用的批复 </a> <a href="/zwgk/zxwj/szfwj/202611/t20261103_1.htm?assistant=true" class="tertonAIReaderFlag" style="display: none;"> | AI阅读</a></li>
      <li class="inc_list_li"><span class="longdate"> 2026-10-09 </span> <a href="/zwgk/zxwj/szfwj/202610/t20261009_2.htm" title="某省人民政府关于某县2026年度农用地转用的批复"> 某省人民政府关于某县2026年度农用地转用的批复 </a> <a href="/zwgk/zxwj/szfwj/202610/t20261009_2.htm?assistant=true" class="tertonAIReaderFlag" style="display: none;"> | AI阅读</a></li>
      <li class="inc_list_li"><span class="longdate"> 2026-10-08 </span> <a href="/zwgk/zxwj/szfwj/202610/t20261008_3.htm" title="某省人民政府办公厅关于印发某某方案的通知"> 某省人民政府办公厅关于印发某某方案的通知 </a> <a href="/zwgk/zxwj/szfwj/202610/t20261008_3.htm?assistant=true" class="tertonAIReaderFlag" style="display: none;"> | AI阅读</a></li>
    </ul>"""
    items = extract_items(html, "http://www.example.gov.cn/zwgk/zxwj/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("某省人民政府关于某市2026年度第三批次农用地转用的批复", date(2026, 11, 3)),
        ("某省人民政府关于某县2026年度农用地转用的批复", date(2026, 10, 9)),
        ("某省人民政府办公厅关于印发某某方案的通知", date(2026, 10, 8)),
    ]


def test_display_none_in_a_comment_or_custom_property_is_not_hidden() -> None:
    # "display:block;/* display:none */"、"--display:none"、"display:none bogus"、字符串里的都不算藏着
    from lxml import html as lhtml

    from youju.crawler.listing import _hidden

    for style, hidden in (
        ("display:block;/* display:none */", False),
        ("--display:none", False),
        ("color:red; display : none", True),
        ("display:none;display:block", False),
        ("display:none !important;display:block", True),
        ("display:none!important", True),
        # 看不懂的 display 值（写错的、var() 这类浏览器可能认的）：拿不准，当没藏
        ("display:none bogus", False),
        ("display:none bogus;display:none", False),
        ("display:block;display:none bogus", False),
        ("display:none;display:var(--d,block)", False),
        ("display:none;display:env(x)", False),
        # 名字转义过的拿不准；all 把 display 也重置了
        ("display:none;disp\\lay:block", False),
        ("display:none;all:initial", False),
        ("display:none;all:unset !important", False),
        ("all:initial;display:none", True),
        # 字符串、括号（包括 url(…)）里的分号不是分隔；没写完的字符串到结尾
        ('display:block;--x:";display:none;"', False),
        ("--x:(;display:none)", False),
        ("background:url(a;display:none)", False),
        ('--x:"abc;display:none', False),
        ('display:none;--x:"a;b"', True),
        (" display:none;", True),
        ("display:\tnone\n", True),
        ("DISPLAY:NONE !IMPORTANT", True),
        # 不间断空格不是 CSS 的空白：名字或值就不对了
        ("\xa0display:none", False),
        ("display:\xa0none", False),
    ):
        assert _hidden(lhtml.fromstring(f'<div style="{style}">x</div>')) is hidden, style


def test_hidden_full_title_link_with_the_same_url_as_a_visible_detail_link_is_kept() -> None:
    # 看得见的只写"详情"、藏着的写着完整标题，两个网址一模一样：藏着的照旧当文件（只有多出参数的才算附带链接）
    html = """<ul class="list">
      <li><a href="/a/1.html">详情</a><a href="/a/1.html" style="display:none">关于项目申报工作的通知</a><span>2026-10-21</span></li>
      <li><a href="/a/2.html">详情</a><a href="/a/2.html" style="display:none">关于公布第二批名单的通知</a><span>2026-10-20</span></li>
      <li><a href="/a/3.html">详情</a><a href="/a/3.html" style="display:none">关于征集典型案例的通知</a><span>2026-10-19</span></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报工作的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]


def test_hidden_link_with_another_parameter_is_another_document() -> None:
    # 藏着的链接比看得见的多出别的参数（同一编号下的另一个版本），各有标题和发表时间：照 main 都读
    html = """<ul class="list">
      <li><a href="/show.asp?id=3" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a><a href="/show.asp?id=3&amp;ver=2" style="display:none" title="标题：关于项目申报的通知（第二版） 点击数：0 发表时间：2026-10-22">关于项目申报的通知（第二版）</a></li>
      <li><a href="/show.asp?id=4" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a></li>
      <li><a href="/show.asp?id=5" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a></li>
    </ul>"""
    got = {
        (i.title, i.publish_date)
        for i in extract_items(html, "http://example.gov.cn/list.asp", today=date(2026, 11, 16))
    }
    assert ("关于项目申报的通知（第二版）", date(2026, 10, 22)) in got
    assert ("关于项目申报的通知", date(2026, 10, 21)) in got


def test_only_date_of_the_row_inside_a_hidden_card_is_still_read() -> None:
    # 藏起来的悬浮卡片里有链接，也写着这一行唯一的日期：弹层外面找不到日期时照原来读
    html = """<ul class="list">
      <li><a href="/a/1.html">关于项目申报工作的通知</a><div style="display:none"><a href="/a/1.html#share">分享</a><span>发布日期：2026-10-21</span></div></li>
      <li><a href="/a/2.html">关于公布第二批名单的通知</a><div style="display:none"><a href="/a/2.html#share">分享</a><span>发布日期：2026-10-20</span></div></li>
      <li><a href="/a/3.html">关于征集典型案例的通知</a><div style="display:none"><a href="/a/3.html#share">分享</a><span>发布日期：2026-10-19</span></div></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报工作的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]


def test_hidden_card_linking_to_the_same_file_keeps_its_date() -> None:
    # 藏起来的悬浮卡片里是同一份文件的链接（网址一样），写着这一行唯一的发布日期，网址里带的是更早的日期：
    # 卡片不算别的文件，日期照 main 用卡片里的
    html = """<ul class="list">
      <li><a href="/a/20260901_1.html">关于项目申报工作的通知</a><div style="display:none"><a href="/a/20260901_1.html">关于项目申报工作的通知</a><span>2026-10-21</span></div></li>
      <li><a href="/a/20260902_2.html">关于公布第二批名单的通知</a><div style="display:none"><a href="/a/20260902_2.html">关于公布第二批名单的通知</a><span>2026-10-20</span></div></li>
      <li><a href="/a/20260903_3.html">关于征集典型案例的通知</a><div style="display:none"><a href="/a/20260903_3.html">关于征集典型案例的通知</a><span>2026-10-19</span></div></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报工作的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
    ]


def test_popup_row_in_a_tooltip_dated_list_stays_in_the_list() -> None:
    # 每行日期都只写在提示文字里（商丘那类平台），第 2 行带泸州式的解读弹层：照样和别的行算一张列表
    html = """<ul class="list">
      <li><a href="/a/1.html" title="标题：关于项目申报的通知 点击数：0 发表时间：2026-10-21">关于项目申报的通知</a></li>
      <li><a href="/a/2.html" title="标题：关于公布第二批名单的通知 点击数：0 发表时间：2026-10-20">关于公布第二批名单的通知</a><div class="address-popup" style="display: none;"><a href="/b/2.html">关于第二批名单的政策解读</a></div></li>
      <li><a href="/a/3.html" title="标题：关于征集典型案例的通知 点击数：0 发表时间：2026-10-19">关于征集典型案例的通知</a></li>
      <li><a href="/a/4.html" title="标题：关于开展第三批项目申报的通知 点击数：0 发表时间：2026-10-18">关于开展第三批项目申报的通知</a></li>
    </ul>"""
    items = extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    assert [(i.title, i.publish_date) for i in items] == [
        ("关于项目申报的通知", date(2026, 10, 21)),
        ("关于公布第二批名单的通知", date(2026, 10, 20)),
        ("关于征集典型案例的通知", date(2026, 10, 19)),
        ("关于开展第三批项目申报的通知", date(2026, 10, 18)),
    ]


def test_hidden_tab_panels_are_still_read() -> None:
    # 整块藏着的页签（每个页签是一张列表）不是"文件旁边的弹层"：里面的条目照 main 读
    rows1 = "".join(
        f'<li><a href="/a/x{i}.html">关于开展第{i}批项目申报的通知</a><span>2026-10-{10 + i}</span></li>'
        for i in range(1, 4)
    )
    rows2 = "".join(
        f'<li><a href="/a/y{i}.html">关于公布第{i}批名单的通知</a><span>2026-09-{10 + i}</span></li>'
        for i in range(1, 4)
    )
    html = f"""<div class="tabs"><div class="panel"><ul class="list">{rows1}</ul></div>
      <div class="panel" style="display:none"><ul class="list">{rows2}</ul></div></div>"""
    urls = {
        i.url for i in extract_items(html, "http://example.gov.cn/a/", today=date(2026, 11, 16))
    }
    assert {"http://example.gov.cn/a/x1.html", "http://example.gov.cn/a/y1.html"} <= urls
