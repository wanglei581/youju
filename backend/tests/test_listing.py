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
    # 的 span 里。原来开头的"2026年1-6"被当成 2026-01-06 挖掉，标题剩"月某市……清理表2026-06-30"
    html = """<ul class="info-list">
      <li><a href="./zcfg/202606/t20260630_1.shtml">2026年1-6月某市财政局规范性文件清理表<span>2026-06-30</span></a></li>
      <li><a href="./zcfg/202601/t20260106_2.shtml">2025年10-12月某市财政局规范性文件清理表<span>2026-01-06</span></a></li>
      <li><a href="./zcfg/202509/t20250930_3.shtml">2025年7-9月某市财政局规范性文件清理表<span>2025-09-30</span></a></li>
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
