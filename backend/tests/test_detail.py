from datetime import date
from pathlib import Path

import pytest
from lxml import html

from tests.conftest import read_fixture_html
from youju.crawler import detail
from youju.crawler.detail import (
    MAX_ATTACHMENTS,
    _split_issuer_line,
    parse_detail,
    redact_sensitive,
)


@pytest.mark.parametrize(
    ("fixture", "url", "opening", "issuer", "number", "attachments", "published"),
    [
        (
            "detail_liaoning_344",
            "https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml",
            "辽工信投资〔2026〕130号\n各市工业和信息化局",
            "辽宁省工业和信息化厅",
            "辽工信投资〔2026〕130号",
            2,
            date(2026, 9, 24),
        ),
        (
            "detail_liaoning_343",
            "https://gxt.ln.gov.cn/gxt/tztg/2026091717062662168/index.shtml",
            "各市（不含大连）工业和信息化局",
            "辽宁省工业和信息化厅、辽宁省财政厅、国家税务总局辽宁省税务局",
            None,
            1,
            date(2026, 9, 17),
        ),
        (
            "detail_jiangsu_510",
            "https://kxjst.jiangsu.gov.cn/art/2026/9/23/art_82540_11833785.html",
            "",  # 原站只有“点击正文.pdf”，HTML 无正文。
            None,
            None,
            1,
            date(2026, 9, 23),
        ),
        (
            "detail_fujian_915",
            "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
            "各设区市（不含厦门）工信局",
            "福建省工业和信息化厅、福建省教育厅、福建省文化和旅游厅、福建省广播电视局、福建省体育局、福建省文物局",
            None,
            2,
            date(2026, 9, 24),
        ),
        (
            "detail_guangdong_1072",
            "https://gdii.gd.gov.cn/zwgk/tzgg1011/content/post_4960472.html",
            "各地级以上市人民政府",
            "广东省工业和信息化厅",
            "粤工信材料函〔2026〕53号",
            0,
            date(2026, 9, 24),
        ),
        (
            "detail_most_1493",
            "https://www.most.gov.cn/tztg/202609/t20260908_197283.html",
            "国际大科学计划—平方公里阵列射电望远镜",
            "科学技术部国际科技合作中心",
            None,
            1,
            date(2026, 9, 8),
        ),
    ],
)
def test_real_detail_pages(fixture, url, opening, issuer, number, attachments, published) -> None:
    result = parse_detail(read_fixture_html(fixture), url)
    assert result.failure_reason is None
    assert result.body_text.startswith(opening) if opening else result.body_text == ""
    assert result.issuer == issuer
    assert result.doc_number == number
    assert result.publish_date == published
    assert len(result.attachments) == attachments
    assert all(a.url.startswith("http") and a.extension for a in result.attachments)
    assert "网站首页" not in result.body_text
    assert "相关新闻" not in result.body_text


# 网页上的导航、发布信息、工具栏、二维码、弹窗等字样：正文里不能有
_PAGE_CHROME = (
    "当前位置",
    "欢迎关注",
    "即将离开",
    "扫一扫",
    "【打印】",
    "【关闭】",
    "打印本页",
    "字号",
    "字体",
    "阅读次数",
    "浏览次数",
    "上一篇",
    "下一篇",
    "相关文档",
    "关联链接",
    "分享：",
    "[返回]",
    "【市民留言】",
)
# 整行就是这几个字的（按钮、弹窗的关闭叉号）
_CHROME_LINES = {"×", "打印", "关闭", "分享", "字体", "顶部", "返回顶部", "附件:"}
# 每个样例正文的（行数，字数）：10/22 对照原网页逐页核过，正文区的段落一段不少
_BODY_SIZE = {
    "detail_hubei_kjt_70955": (59, 2878),
    "detail_hubei_kjt_67683": (53, 2462),
    "detail_hubei_jxt": (74, 4426),
    "detail_stats_pc_mobile": (3, 85),
    "detail_shanghai_stcsm": (9, 292),
    "detail_guangzhou_kjj": (44, 2442),
    # 新脱敏规则保留完整机构名，不再把“发展中心”“厅企业处”误遮为姓名。
    "detail_anhui_kjt": (9, 334),
    "detail_sichuan_kjt": (19, 1041),
    "detail_jiangsu_gxt": (4, 160),
    "detail_shandong_gxt": (7, 363),
    "detail_henan_kjt": (7, 501),
    "detail_shanghai_sheitc": (3, 318),
    "detail_qinghai_gxt": (14, 473),
    # 10/25 对照原网页核过：div.zw 里 42 段一段不少。样例留着原页面上 3 个省科技厅的公开办公座机
    # （政府办公电话，不是个人信息）；2912 是 parse_detail 输出的字数，输出时已按入库的脱敏规则
    # 把这 3 个座机换成"[电话已脱敏]"
    "detail_guangdong_kjt": (42, 2912),
}


@pytest.mark.parametrize(
    ("fixture", "url", "first", "last", "issuer", "published", "attachments"),
    [
        (
            "detail_hubei_kjt_70955",
            "https://kjt.hubei.gov.cn/kjdt/tzgg/202610/t20261020_6035881.shtml",
            "各市、州、直管市、神农架林区科技局，各扩权县（市、区）科技管理部门，东湖高新区科创局",
            "2026年10月19日",
            "湖北省科学技术厅",
            date(2026, 10, 20),
            0,
        ),
        (
            "detail_hubei_kjt_67683",
            "https://kjt.hubei.gov.cn/kjdt/tzgg/202610/t20261019_6035490.shtml",
            "各市、州、直管市、神农架林区科技局：",
            "2026年10月15日",
            "湖北省科技厅",
            date(2026, 10, 19),
            1,
        ),
        (
            "detail_hubei_jxt",
            "http://jxt.hubei.gov.cn/fbjd/zc/qtzdgkwj/gwfb/202609/t20260922_6020215.shtml",
            "各市、州、直管市、神农架林区人社局（职改办）、经信局，各企事业单位：",
            "2026年9月21日",
            "评审委员会办公室",
            date(2026, 9, 21),
            10,
        ),
        (
            # 同一份正文另有手机版，两份一样，只取一份
            "detail_stats_pc_mobile",
            "https://www.stats.gov.cn/xw/tjxw/tzgg/202609/t20260921_1965384.html",
            "经公示，《数智时代残疾人就业质量评价研究》等项目公示期满无异议",
            "2026年9月21日",
            "全国统计科学研究组织管理办公室",
            date(2026, 9, 21),
            1,
        ),
        (
            "detail_shanghai_stcsm",
            "https://stcsm.sh.gov.cn/zwgk/tzgs/gsgg/zhgsgg/20260924/47c0b4118e0540bfab43d7e9af025b14.html",
            "各有关单位：",
            "关于下达2026年度第一批科技金融保费补贴经费的通知.pdf",
            "上海市科学技术委员会",
            date(2026, 9, 24),
            2,
        ),
        (
            "detail_guangzhou_kjj",
            "http://kjj.gz.gov.cn/xxgk/zwdt/tztg/wjgg/content/post_11058221.html",
            "各有关单位：",
            "2026年10月20日",
            "广州市科学技术局",
            date(2026, 10, 20),
            0,
        ),
        (
            "detail_anhui_kjt",
            "https://kjt.ah.gov.cn/kjzx/tzgg/123510901.html",
            "各有关单位：",
            "2026年9月22日",
            None,
            date(2026, 9, 23),
            1,
        ),
        (
            "detail_sichuan_kjt",
            "https://kjt.sc.gov.cn/kjt/gstz/2026/9/18/ae78e2444766482e91538646341f90c9.shtml",
            "省级有关部门，各市（州）科技局，产学研机构等相关单位：",
            "2026年9月18日",
            "四川省科学技术厅",
            date(2026, 9, 18),
            0,
        ),
        (
            "detail_jiangsu_gxt",
            "https://gxt.jiangsu.gov.cn/art/2026/9/17/art_6281_11831217.html",
            "苏工信新材〔2026〕262号",
            "2026年9月17日",
            "江苏省工业和信息化厅",
            date(2026, 9, 17),
            0,
        ),
        (
            "detail_shandong_gxt",
            "http://gxt.shandong.gov.cn/art/2026/9/28/art_15201_10366576.html",
            "各市工业和信息化局：",
            "（此件主动公开）",
            "山东省工业和信息化厅",
            date(2026, 9, 28),
            1,
        ),
        (
            "detail_henan_kjt",
            "https://kjt.henan.gov.cn:443/2026/09-22/3431519.html",
            "各省辖市科技局，济源示范区、航空港区管委会科技主管部门",
            "2026年9月21日",
            None,
            date(2026, 9, 22),
            0,
        ),
        (
            "detail_shanghai_sheitc",
            "https://sheitc.sh.gov.cn/gg/20260914/b3cb3b63a51b423c8d303b1725299e78.html",
            "为全面落实第三轮中央生态环境保护督察",
            "2026年9月14日",
            "上海市经济和信息化委员会",
            date(2026, 9, 14),
            0,
        ),
        (
            # 正文在页面自带脚本的 Vue 数据里，网页结构里没有
            "detail_qinghai_gxt",
            "https://gxt.qinghai.gov.cn/2100879587404161025.html",
            "根据《青海省智能工厂认定管理办法》",
            "2026年9月18日",
            "青海省工业和信息化厅",
            date(2026, 9, 18),
            0,
        ),
        (
            # 通用规则取到的是网页导航（"首页 政务公开 科技资讯……"），正文在 div.zw
            "detail_guangdong_kjt",
            "https://gdstc.gd.gov.cn/zwgk_n/tzgg/content/post_4970293.html",
            "粤科函资字〔2026〕1726号",
            "2026年10月23日",
            "广东省科学技术厅",
            date(2026, 10, 23),
            0,
        ),
    ],
)
def test_body_has_no_page_chrome(fixture, url, first, last, issuer, published, attachments) -> None:
    """正文只取正文区：网页上的"当前位置"、标题和发布信息、字号和打印按钮、二维码、"相关文档"、
    离站提示弹窗等都不算正文（10/22 湖北科技厅 70955、67683 正文开头是导航、结尾是公众号和弹窗；
    同样问题的来源 10/22 抽样找出，见 PR 说明）。附件照旧从整页收。行数、字数也核对（中间漏段会变）。"""
    result = parse_detail(read_fixture_html(fixture), url)
    lines = result.body_text.splitlines()
    assert (len(lines), len(result.body_text)) == _BODY_SIZE[fixture]
    if fixture == "detail_anhui_kjt":
        assert "省高新技术发展中心 [电话已脱敏]" in result.body_text
        assert "省科技厅企业处 [电话已脱敏]" in result.body_text
    assert lines[0].startswith(first)
    assert lines[-1] == last
    assert result.body_text.count(lines[0]) == 1
    assert [c for c in _PAGE_CHROME if c in result.body_text] == []
    assert _CHROME_LINES.isdisjoint(lines)
    assert result.issuer == issuer
    assert result.publish_date == published
    assert len(result.attachments) == attachments


def test_xqing_web_box_before_outer_content() -> None:
    """四川雅安、遂宁、达州等站群的正文在 div.xqing-web-box 里；通用规则最后那条 .content 在这些页面上
    是页尾的"主办单位、地址"（11/12 雅安一批文件页存下的"正文"就是页尾）。"""
    page = (
        "<html><title>关于开展项目申报的通知</title><body><div class='nav'><a>首页</a><a>新闻</a></div>"
        "<div class='xqing-web-box'><p>各县（区）农业农村局：</p><p>现将2026年度项目申报有关事项"
        "通知如下，请按要求认真组织申报，并于12月1日前报送材料。</p><p>雅安市农业农村局</p>"
        "<p>2026年11月5日</p></div><div class='id-foot'><div class='id-bottom'><div class='content'>"
        "<p>站点地图 | 无障碍浏览 | 使用帮助</p><p>主办：雅安市农业农村局 地址：雅安市某某路1号"
        " 邮编：625000</p></div></div></div></body></html>"
    )
    result = parse_detail(page, "https://nyncj.yaan.gov.cn/xinwen/show/1.html")
    assert result.body_text.startswith("各县（区）农业农村局")
    assert "主办" not in result.body_text and "站点地图" not in result.body_text
    assert result.issuer == "雅安市农业农村局"


def test_site_body_rules_only_for_listed_hosts() -> None:
    """按网站写的正文区只对这个网站用；页面上找不到时（网站改版）照旧走通用规则。"""
    page = (
        "<html><body><div class='newsCon'><p>各有关单位：现将有关事项通知如下，请于11月10日前"
        "按要求报送申报材料。</p></div><div id='zoom'><p>栏目导航：首页 通知公告 政策文件 办事服务"
        " 互动交流 专题专栏 网站地图</p></div></body></html>"
    )
    sichuan = "https://kjt.sc.gov.cn/kjt/gstz/2026/9/18/a.shtml"
    assert parse_detail(page, sichuan).body_text.startswith("各有关单位")
    assert parse_detail(page, "https://kjt.example.gov.cn/a.html").body_text.startswith("栏目导航")
    changed = page.replace("newsCon", "newsMain")
    assert parse_detail(changed, sichuan).body_text.startswith("栏目导航")


def test_guangdong_kjt_body_in_div_zw() -> None:
    """广东科技厅（10/25 线上抽样）：通用规则只取到网页导航（"首页 政务公开 科技资讯……"共 31 字），
    这个来源的通知全都"没有正文"。正文就在页面静态 HTML 的 div.zw 里，按网站规则取。"""
    page = read_fixture_html("detail_guangdong_kjt")
    result = parse_detail(page, "https://gdstc.gd.gov.cn/zwgk_n/tzgg/content/post_4970293.html")
    assert result.body_text.startswith(
        "粤科函资字〔2026〕1726号\n省有关部门，各地级以上市科技（创）局"
    )
    assert len(result.body_text) > 500
    chrome = ("当前位置", "首页", "【打印】", "分享到", "字体")
    assert all(word in page for word in chrome)  # 网页上都有，只是不算正文
    assert [word for word in chrome if word in result.body_text] == []
    assert "关于组织申报" not in result.body_text  # 标题（zw-title）不收进正文
    assert result.doc_number == "粤科函资字〔2026〕1726号"


def test_gdstc_rule_whole_class_only_for_this_host() -> None:
    """广东科技厅的 div.zw 只对 gdstc.gd.gov.cn 用；类名按完整词匹配：发布信息和按钮所在的 zw-info、
    标题 zw-title 不算正文区（这里故意让 zw-info 的字比正文多）。页面上没有 div.zw（网站改版）时照旧
    走通用规则。"""
    page = (
        "<html><body><h3 class='zw-title'>关于组织申报某专项项目的通知</h3><div class='zw-info'>"
        "<span>时间 : 2026-10-23 17:45:03</span><span>来源 : 广东省科学技术厅</span>"
        "<span>【字体:大 中 小】</span><span>【打印】</span><div>分享到：微信 微博 QQ空间</div></div>"
        "<div class='zw'><p>各有关单位：现将有关事项通知如下，请于11月10日前按要求报送申报材料。</p>"
        "</div><div id='zoom'><p>栏目导航：首页 通知公告 政策文件 办事服务 互动交流 专题专栏 网站地图"
        "</p></div></body></html>"
    )
    gdstc = "https://gdstc.gd.gov.cn/zwgk_n/tzgg/content/post_1.html"
    assert parse_detail(page, gdstc).body_text.startswith("各有关单位")
    assert parse_detail(page, "https://kjt.example.gov.cn/a.html").body_text.startswith("栏目导航")
    # 换成别的类名：只说明会退回通用规则（真网站上通用规则取到的是页头导航）
    changed = page.replace("class='zw'", "class='zw-content'")
    assert parse_detail(changed, gdstc).body_text.startswith("栏目导航")
    # 页面上有几块 div.zw 时取字最多的一块（抽查的 4 个真页面都只有一块）
    two = page.replace(
        "<div class='zw'>",
        "<div class='zw'><p>附件：申报指南和申报书模板请在下方附件中查看。</p></div><div class='zw'>",
    )
    assert parse_detail(two, gdstc).body_text.startswith("各有关单位")


def test_body_written_in_page_script() -> None:
    """正文写在页面自带脚本的 Vue 数据里（青海工信厅）：只对列出的网站取，不执行脚本；转义按 JS 的
    规则还原；要运行脚本才知道的内容（没转义的 ${…}）、还原不了的转义不取，照旧走通用规则。"""
    page = (
        "<html><body><div class='content'><div class='article'><p>当前位置：首页 - 新闻资讯 - 通知公告</p>"
        "<div id='news-details'><div v-html='content'></div></div><p>上一篇：没有更多了</p></div>"
        "</div><script>var app = new Vue({el: '#news-details', data: {form: {}, content: `"
        "<p>各有关单位：现将有关事项通知如下，材料命名为“单位名\\`项目名”，请按要求报送。</p>"
        "<p>申报网址：https:\\/\\/gxt.example.gov.cn\\/apply，第\\u4e00批\\x41类\\u{4e8c}期，"
        "费用\\${单价}元。</p>\\n<p>青海省工业和信息化厅</p><p>2026年9月18日</p>`, other: {add: ''}}});"
        "</script></body></html>"
    )
    qinghai = "https://gxt.qinghai.gov.cn/2100879587404161025.html"
    result = parse_detail(page, qinghai)
    assert result.body_text.splitlines() == [
        "各有关单位：现将有关事项通知如下，材料命名为“单位名`项目名”，请按要求报送。",
        "申报网址：https://gxt.example.gov.cn/apply，第一批A类二期，费用${单价}元。",
        "青海省工业和信息化厅",
        "2026年9月18日",
    ]
    assert result.issuer == "青海省工业和信息化厅"
    assert "各有关单位" not in parse_detail(page, "https://gxt.example.gov.cn/1.html").body_text
    for broken in (
        page.replace("请按要求报送", "${deadline}前报送"),
        page.replace("\\u{4e8c}", "\\u{110000}"),
    ):
        assert "各有关单位" not in parse_detail(broken, qinghai).body_text


def test_editor_block_only_when_unambiguous() -> None:
    """TRS 编辑器正文区只在没有歧义时用：页面有 #zoom、编辑器块却在它外面（侧栏）的不用；几块内容
    不一样的不用（正文可能分成了几段），照旧走通用规则；几块一模一样（电脑版、手机版）的取一块。"""
    url = "https://kjt.example.gov.cn/a.html"
    notice = "<p>各有关单位：现将有关事项通知如下，请于11月10日前按要求报送申报材料。</p>"
    aside = (
        "<div class='side'><div class='TRS_UEDITOR'><p>相关解读：一图读懂本年度科技计划项目"
        "申报要点、申报条件和办理流程说明。</p></div></div>"
    )
    beside = f"<html><body><div id='zoom'>{notice}</div>{aside}</body></html>"
    assert parse_detail(beside, url).body_text.startswith("各有关单位")
    parts = (
        f"<html><body><div class='content'><div class='TRS_UEDITOR'>{notice}</div>"
        "<div class='TRS_UEDITOR'><p>第二部分：申报材料清单详见附件，请各单位认真准备并按时提交。</p>"
        "</div></div></body></html>"
    )
    body = parse_detail(parts, url).body_text
    assert "各有关单位" in body and "第二部分" in body
    copies = (
        f"<html><body><div class='pc'><div class='TRS_UEDITOR'>{notice}</div></div>"
        f"<div class='mobile'><div class='TRS_UEDITOR'>{notice}</div></div></body></html>"
    )
    assert parse_detail(copies, url).body_text.count("各有关单位") == 1
    # #zoom 里编辑器块后面除了"相关文档"这类栏目标题，还有别的字（哪怕一句）：用 #zoom 整块
    for tail, kept in (("<div>相关文档</div>", False), ("<p>本通知自公布之日起施行。</p>", True)):
        page = beside.replace(
            f"<div id='zoom'>{notice}</div>{aside}",
            f"<div id='zoom'><div class='TRS_UEDITOR'>{notice}</div>{tail}</div>",
        )
        assert ("本通知自公布之日起施行" in parse_detail(page, url).body_text) is kept
        assert "相关文档" not in parse_detail(page, url).body_text
    # #zoom 里编辑器块后面还有一段：用 #zoom 整块
    split = beside.replace(
        f"<div id='zoom'>{notice}</div>{aside}",
        f"<div id='zoom'><div class='TRS_UEDITOR'>{notice}</div><p>二、申报材料：申请书、"
        "财务审计报告和知识产权证明材料，于11月10日前报送。</p></div>",
    )
    assert "二、申报材料" in parse_detail(split, url).body_text
    # 编辑器块在 #zoom 里面的照常用
    inside = beside.replace(
        f"<div id='zoom'>{notice}</div>{aside}", f"<div id='zoom'>{aside}</div>"
    )
    assert parse_detail(inside, url).body_text.startswith("相关解读")


def test_attachment_names_and_urls_are_absolute() -> None:
    result = parse_detail(
        read_fixture_html("detail_fujian_915"),
        "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
    )
    assert [a.extension for a in result.attachments] == ["doc", "doc"]
    assert result.attachments[0].name.startswith("1.视听系统典型案例申报书")
    assert result.attachments[0].url.startswith("https://gxt.fujian.gov.cn/")


def test_attachments_are_capped() -> None:
    links = "".join(f"<p><a href='/files/{i}.xlsx'>附件{i}</a></p>" for i in range(80))
    sample = (
        "<html><body><div id='zoom'><p>关于公布有关名单的通知，全文见附件。</p>"
        f"{links}</div></body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/a.html")
    assert len(result.attachments) == MAX_ATTACHMENTS
    assert result.attachments[0].url == "https://example.gov.cn/files/0.xlsx"


def test_doc_number_variants_and_sensitive_data() -> None:
    for number in ("辽工信〔2026〕130号", "辽工信[2026]130号", "辽工信（2026）130号"):
        sample = (
            f"<html><meta name='PubDate' content='2026-09-24'><body><div id='zoom'>"
            f"<p>{number}</p><p>这是公开通知的正文，联系方式：13812345678，"
            "身份证号：110101199001011234，银行账号：6222021234567890123。</p>"
            "<p>辽宁省工业和信息化厅</p><p>2026年9月24日</p></div></body></html>"
        )
        result = parse_detail(sample, "https://example.gov.cn/a.html")
        assert result.doc_number == number
        assert "13812345678" not in result.body_text
        assert "110101199001011234" not in result.body_text
        assert "6222021234567890123" not in result.body_text
    assert redact_sensitive("联系人：张三，邮箱：zhang@example.cn") == (
        "联系人：[姓名已脱敏]，邮箱：[邮箱已脱敏]"
    )
    assert "130503670401001" not in redact_sensitive("旧身份证号：130503670401001")
    assert "6222 0212 3456 7890 123" not in redact_sensitive("银行账号：6222 0212 3456 7890 123")


# 10/20 附件读入时发现：旧规则把邮箱、座机前面连着的中文一起遮掉（截止日期、单位名都没了），
# "联系人："后面一律遮前 2–8 个汉字（遮掉单位名，姓名反倒留着）。下面每条都是原网页里见过的写法
# （名字、号码换成了假的）。
@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        # 邮箱：只遮地址本身
        (
            "加盖公章，PDF电子版于7月2日前发送至abc_x@163.com邮箱。",
            "加盖公章，PDF电子版于7月2日前发送至[邮箱已脱敏]邮箱。",
        ),
        (
            "请参训人员于9月4日（周五）17:00前将参会回执电子版发到指定邮箱abc@example.gov.cn，组织方",
            "请参训人员于9月4日（周五）17:00前将参会回执电子版发到指定邮箱[邮箱已脱敏]，组织方",
        ),
        (
            "政务邮箱user000（OA）或user.name+tag@mail.example.com.cn。",
            "政务邮箱user000（OA）或[邮箱已脱敏]。",
        ),
        # 座机前面不是自成一段的姓名：不遮（以前"日前寄至""联系电话""传真"都被当成姓名）
        ("材料请于11月20日前寄至0571-87654321", "材料请于11月20日前寄至[电话已脱敏]"),
        ("联系电话0571-87654321，传真0571-87654322", "联系电话[电话已脱敏]，传真[电话已脱敏]"),
        ("应届毕业生，38周岁及以下\n022-12345678", "应届毕业生，38周岁及以下\n[电话已脱敏]"),
        # 自成一段、以常见姓氏开头的是姓名，前面的单位名留着
        (
            "省工信厅中小企业处 张三 0351-1234567",
            "省工信厅中小企业处 [姓名已脱敏] [电话已脱敏]",
        ),
        (
            "运城市工信局中小企业科 李 四 0359-1234567",
            "运城市工信局中小企业科 [姓名已脱敏] [电话已脱敏]",
        ),
        ("张老师 0571-87654321", "[姓名已脱敏] [电话已脱敏]"),
        ("张三0571-87654321", "[姓名已脱敏][电话已脱敏]"),
        ("市科技局基础处\n王小五\n022-12345678", "市科技局基础处\n[姓名已脱敏]\n[电话已脱敏]"),
        # 同一行几个姓名合成一个
        (
            "咨询电话：省科技厅 郑十 吴九 025-12345678",
            "咨询电话：省科技厅 [姓名已脱敏] [电话已脱敏]",
        ),
        ("赵六六 孙 七 0951-1234567", "[姓名已脱敏] [电话已脱敏]"),
        ("省科技厅 张三、李四 025-12345678", "省科技厅 [姓名已脱敏] [电话已脱敏]"),
        ("联系人：张三、李四", "联系人：[姓名已脱敏]"),
        # 单位名后面紧跟着姓名
        ("省财政厅周八八 0531-12345678", "省财政厅[姓名已脱敏] [电话已脱敏]"),
        # 单位名、地名、标签不当姓名
        ("高新处 0571-87654321", "高新处 [电话已脱敏]"),
        ("济南 0531-12345678 青岛 0532-12345678", "济南 [电话已脱敏] 青岛 [电话已脱敏]"),
        ("西安市 029-12345678", "西安市 [电话已脱敏]"),
        ("石家庄 0311-87654321 武汉 027-87654321", "石家庄 [电话已脱敏] 武汉 [电话已脱敏]"),
        ("技术支持 0571-87654321", "技术支持 [电话已脱敏]"),
        ("申报系统 0571-87654321", "申报系统 [电话已脱敏]"),
        ("联 系 方 式 0571-87654321", "联 系 方 式 [电话已脱敏]"),
        # 和前面的数字、字母连着的不是自成一段
        ("A座1210高新 0571-87654321", "A座1210高新 [电话已脱敏]"),
        ("A座1210王五 0571-87654321", "A座1210王五 [电话已脱敏]"),
        # 已经遮过的座机前面也一样看（正文给出前会再过一遍）
        ("张三 [电话已脱敏]", "[姓名已脱敏] [电话已脱敏]"),
        ("省工信厅 张三 [电话已脱敏]", "省工信厅 [姓名已脱敏] [电话已脱敏]"),
        # 邮箱、座机先遮，姓名后认：姓名前后挨着号码、邮箱的，第一遍就遮（再过一遍不变）
        ("邮箱：abc@163.com张三 0571-87654321", "邮箱：[邮箱已脱敏][姓名已脱敏] [电话已脱敏]"),
        (
            "联系人：张三0571-87654321李四0571-87654322",
            "联系人：[姓名已脱敏][电话已脱敏][姓名已脱敏][电话已脱敏]",
        ),
        ("abc@163.com-def@qq.com", "[邮箱已脱敏][邮箱已脱敏]"),
        ("abc@163.com_def@qq.com", "[邮箱已脱敏][邮箱已脱敏]"),
        # 没写冒号、紧贴着姓名的"联系人张三"
        ("联系人张三 0571-87654321", "联系人[姓名已脱敏] [电话已脱敏]"),
        # 普通词、地址、自治州不当姓名
        ("值班电话：白天 0571-87654321", "值班电话：白天 [电话已脱敏]"),
        ("地址：杭州市西湖区文三路 0571-87654321", "地址：杭州市西湖区文三路 [电话已脱敏]"),
        ("甘孜 0836-1234567", "甘孜 [电话已脱敏]"),
        ("纪检 0571-87654321；商务 0571-87654322", "纪检 [电话已脱敏]；商务 [电话已脱敏]"),
        ("董事长 张三 0571-87654321", "董事长 [姓名已脱敏] [电话已脱敏]"),
        # "联系人："后面：跳过单位名，遮姓名
        ("联系人：张三，邮箱：zhang@example.cn", "联系人：[姓名已脱敏]，邮箱：[邮箱已脱敏]"),
        (
            "（联系人：市工信局汽车工业处 张三；联系电话：12345678）",
            "（联系人：市工信局汽车工业处 [姓名已脱敏]；联系电话：12345678）",
        ),
        ("联系人：省科技厅对外合作处 钱一一 孙二", "联系人：省科技厅对外合作处 [姓名已脱敏]"),
        ("联系人：王 五 李小四 赵六", "联系人：[姓名已脱敏]"),
        ("联 系 人：杜三", "联 系 人：[姓名已脱敏]"),
        (
            "联 系 人：谢秀芳\n联系电话：0571-87654321",
            "联 系 人：[姓名已脱敏]\n联系电话：[电话已脱敏]",
        ),
        # "联系人："后面两个字、以常见姓氏开头的，以单位字结尾也是姓名；别处不是
        ("联系人：王镇", "联系人：[姓名已脱敏]"),
        ("联系人：省科技厅高新处 张路", "联系人：省科技厅高新处 [姓名已脱敏]"),
        ("沙县 0598-1234567", "沙县 [电话已脱敏]"),
        # 不是这一行最后一个词的、以"州"结尾的地级市：照样不当姓名
        ("联系人：省科技厅 高校 张三", "联系人：省科技厅 高校 [姓名已脱敏]"),
        (
            "联系人：浙江省 杭州 张三 0571-87654321",
            "联系人：浙江省 杭州 [姓名已脱敏] [电话已脱敏]",
        ),
        (
            "各市联系人：杭州 张三 0571-87654321；苏州 李四 0512-87654321",
            "各市联系人：杭州 [姓名已脱敏] [电话已脱敏]；苏州 [姓名已脱敏] [电话已脱敏]",
        ),
        ("联系人：杭州", "联系人：杭州"),
        # 连写的"联系人："后面第一个词照旧不看姓氏（和以前一样）；新认的写法要以常见姓氏开头
        ("联系人：南楠", "联系人：[姓名已脱敏]"),
        ("联系人及电话：各市", "联系人及电话：各市"),
        ("联 系 人：待定", "联 系 人：待定"),
        # 几个联系人里最后那个两字的也算；冒号后换行的不用这条例外（表格下一格可能是表头、单位名）
        ("联系人：张三 王镇", "联系人：[姓名已脱敏]"),
        ("联系人：\n王镇", "联系人：\n王镇"),
        ("联系人：\n张三 王镇", "联系人：\n[姓名已脱敏] 王镇"),
        ("联系人：张三、王镇", "联系人：[姓名已脱敏]"),
        # 冒号后换行的，几个姓名也一起遮
        ("联系人：\n李四、王五", "联系人：\n[姓名已脱敏]"),
        ("联系人及电话：王五 0571-87654321", "联系人及电话：[姓名已脱敏] [电话已脱敏]"),
        ("联系人及联系电话：王五 0571-87654321", "联系人及联系电话：[姓名已脱敏] [电话已脱敏]"),
        ("联系人及联系方式：王五，0571-87654321", "联系人及联系方式：[姓名已脱敏]，[电话已脱敏]"),
        ("联系人和联系方式：王五", "联系人和联系方式：[姓名已脱敏]"),
        ("联系人电话：王五", "联系人电话：[姓名已脱敏]"),
        # 复姓
        ("联系人：欧阳小明", "联系人：[姓名已脱敏]"),
        ("欧阳小明 0571-87654321", "[姓名已脱敏] [电话已脱敏]"),
        ("省科技厅司马小明 0571-87654321", "省科技厅[姓名已脱敏] [电话已脱敏]"),
        ("联系人：刘三三 12345678", "联系人：[姓名已脱敏] 12345678"),
        ("联系人：张三028—12345678", "联系人：[姓名已脱敏]028—12345678"),
        ("联系人：阿依·买买", "联系人：[姓名已脱敏]"),
        (
            "联系人：某某市科学技术信息研究所办公室张三",
            "联系人：某某市科学技术信息研究所办公室[姓名已脱敏]",
        ),
        # 姓名后面跟着的不是姓名：不遮（以前"请于"也会被遮）
        ("联系人：张三 请于11月20日前报送", "联系人：[姓名已脱敏] 请于11月20日前报送"),
        ("联系人：Ryan LEE 先生", "联系人：Ryan LEE 先生"),
        # 只有单位名、表头：不遮（以前遮掉前 8 个字）
        ("联系人：省科技厅高新处", "联系人：省科技厅高新处"),
        ("联系人：推荐单位", "联系人：推荐单位"),
        # 到数字为止：后面的截止日期不当姓名
        ("联系人：各单位于10月20日前确定", "联系人：各单位于10月20日前确定"),
        ("联系人及电话：\n推荐单位", "联系人及电话：\n推荐单位"),
        ("联系人及电话：\n是否已 上年 上年", "联系人及电话：\n是否已 上年 上年"),
        ("联系人：\n李四四，13812345678", "联系人：\n[姓名已脱敏]，[手机号已脱敏]"),
        # 银行账号：标签后面跟"为""号码"、隔着开户行的也遮；对公账号可能超过 19 位
        ("收款账号为1202021309001234568", "收款账号为[银行账号已脱敏]"),
        ("账户号码：1202021309001234568", "账户号码：[银行账号已脱敏]"),
        (
            "开户行及账号：中国工商银行股份有限公司杭州西湖支行 1202021309001234568",
            "开户行及账号：中国工商银行股份有限公司杭州西湖支行 [银行账号已脱敏]",
        ),
        (
            "开户行：中国工商银行杭州分行 1202021309001234568",
            "开户行：中国工商银行杭州分行 [银行账号已脱敏]",
        ),
        ("账号（基本户）：1202021309001234568", "账号（基本户）：[银行账号已脱敏]"),
        ("开户账号：33050161613500000123", "开户账号：[银行账号已脱敏]"),
        ("银行账号：6222 0212 3456 7890 123", "银行账号：[银行账号已脱敏]"),
        # 后面的年份、金额、日期不算进账号（以前"2025"被一起遮掉）
        ("账号：1234567890 2025年11月5日前", "账号：[银行账号已脱敏] 2025年11月5日前"),
        ("账号：1202021309001234568 1500元", "账号：[银行账号已脱敏] 1500元"),
        ("账号：1202021309001234568 1500", "账号：[银行账号已脱敏] 1500"),
        ("账号：1202021309 2025 10 10", "账号：[银行账号已脱敏] 2025 10 10"),
        ("账号：1202021309 20251010", "账号：[银行账号已脱敏] 20251010"),
        # 写了"账号"的第一组不看后面跟的字（"日期""次日""分行""号码""时间"开头的词不是单位）
        ("账号：1234567890   元", "账号：[银行账号已脱敏]   元"),
        (
            "账号：1202021309001234568 日期：2025年10月10日",
            "账号：[银行账号已脱敏] 日期：2025年10月10日",
        ),
        ("汇款账号：1202021309001234568 次日到账", "汇款账号：[银行账号已脱敏] 次日到账"),
        ("开户账号：1202021309001234568 分行：杭州", "开户账号：[银行账号已脱敏] 分行：杭州"),
        ("账号1202021309001234568时间2025年", "账号[银行账号已脱敏]时间2025年"),
        ("银行卡号：6222 0212 3456 7890 123 号码", "银行卡号：[银行账号已脱敏] 号码"),
        ("账号：488418-246237-7124元", "账号：[银行账号已脱敏]元"),
        ("账号：6222 0212 3456 7890 1234时间2025年", "账号：[银行账号已脱敏] 1234时间2025年"),
        ("账号：1234567890 10月20日前", "账号：[银行账号已脱敏] 10月20日前"),
        ("账号：1234567890 20日前", "账号：[银行账号已脱敏] 20日前"),
        ("账号：1234567890 20日，", "账号：[银行账号已脱敏] 20日，"),
        ("账号：1234567890 5日起", "账号：[银行账号已脱敏] 5日起"),
        ("账号：1234567890 10日内", "账号：[银行账号已脱敏] 10日内"),
        # 年份隔着空格跟"年"、点号斜杠写的日期、超过 19 位才接上的金额：不并进账号
        (
            "账号：1202021309001234 2025 年 11 月 20 日前",
            "账号：[银行账号已脱敏] 2025 年 11 月 20 日前",
        ),
        ("账号：1202021309001234 1500元", "账号：[银行账号已脱敏] 1500元"),
        ("账号：1202021309001234 2025.10.10前", "账号：[银行账号已脱敏] 2025.10.10前"),
        ("账号：1202021309001234 2025/10/10前", "账号：[银行账号已脱敏] 2025/10/10前"),
        ("账号：1202021309001234 2025-10前", "账号：[银行账号已脱敏] 2025-10前"),
        ("账号：6222 0212 3456 7890 2025-1截止", "账号：[银行账号已脱敏] 2025-1截止"),
        ("账号：1202021309001234 2025-10", "账号：[银行账号已脱敏] 2025-10"),
        ("账号：1202021309001234 1999-12前", "账号：[银行账号已脱敏] 1999-12前"),
        ("账号：1202-0213-0900-1234 2025-10前", "账号：[银行账号已脱敏] 2025-10前"),
        # 整串都用横线分组的、"月份"不在 1–12 的照样接
        ("账号：1202-0213-0900-2025-10", "账号：[银行账号已脱敏]"),
        ("账号：1202 0213 0900 2025-13", "账号：[银行账号已脱敏]"),
        # 第一组 12 位以上的，和旧规则一样最多接到 19 位：后面的年份、金额、日期不并进来
        ("账号：1202021309001234 2025-13", "账号：[银行账号已脱敏] 2025-13"),
        ("账号：6222021234567890 2025 10月10日", "账号：[银行账号已脱敏] 2025 10月10日"),
        ("账号：622202123456789012 300.00元", "账号：[银行账号已脱敏] 300.00元"),
        ("账号：622202123456789012 20,000元", "账号：[银行账号已脱敏] 20,000元"),
        ("账号：622202123456789012 20日之前", "账号：[银行账号已脱敏] 20日之前"),
        ("账号：622202123456789012 25年3月", "账号：[银行账号已脱敏] 25年3月"),
        # 分组写的账号超过 19 位也照样断开：年份隔空格跟月份、带千分位小数的金额、两位数的年份
        ("账号：6222 0212 3456 7890 2025 10月10日", "账号：[银行账号已脱敏] 2025 10月10日"),
        ("账号：6222 0212 3456 7890 123 20,000元", "账号：[银行账号已脱敏] 20,000元"),
        ("账号：6222 0212 3456 7890 123 300.00 元", "账号：[银行账号已脱敏] 300.00 元"),
        ("账号：6222 0212 3456 7890 25年", "账号：[银行账号已脱敏] 25年"),
        ("账号：6222 0212 3456 7890 2025.10.10前", "账号：[银行账号已脱敏] 2025.10.10前"),
        ("账号：6222 0212 3456 7890 2025      年", "账号：[银行账号已脱敏] 2025      年"),
        # 分组写的账号和旧规则一样最多接到 19 位：后面各种写法的日期、金额都不并进来
        ("账号：6222 0212 3456 7890 1234 10月以前", "账号：[银行账号已脱敏] 1234 10月以前"),
        ("账号：6222 0212 3456 7890 1234 20日以前", "账号：[银行账号已脱敏] 1234 20日以前"),
        ("账号：6222 0212 3456 7890 2025 10月上半月", "账号：[银行账号已脱敏] 2025 10月上半月"),
        (
            "账号：6222 0212 3456 7890 2025\uff0e10\uff0e10",
            "账号：[银行账号已脱敏] 2025\uff0e10\uff0e10",
        ),
        ("账号：6222 0212 3456 7890 3000多万元", "账号：[银行账号已脱敏] 3000多万元"),
        ("账号：6222 0212 3456 7890 2025上半年", "账号：[银行账号已脱敏] 2025上半年"),
        ("账号：6222 0212 3456 7890 2025 10.10前", "账号：[银行账号已脱敏] 2025 10.10前"),
        ("账号：6222 0212 3456 789012 25.10.10前", "账号：[银行账号已脱敏] 25.10.10前"),
        ("账号：6222 0212 3456 789012 10/31前", "账号：[银行账号已脱敏] 10/31前"),
        ("账号：6222 0212 3456 789012 20 000元", "账号：[银行账号已脱敏] 20 000元"),
        ("账号：6222 0212 3456 789012 20，000元", "账号：[银行账号已脱敏] 20，000元"),
        (
            "账号：6222 0212 3456 7890 2025\u300010月10日",
            "账号：[银行账号已脱敏] 2025\u300010月10日",
        ),
        (
            "账号：6222 0212 3456 7890 2025\uff0d10\uff0d10前",
            "账号：[银行账号已脱敏] 2025\uff0d10\uff0d10前",
        ),
        ("账号：1202 0213 0900 1234 2026 年度", "账号：[银行账号已脱敏] 2026 年度"),
        ("账号：1234567890 10 月 20 日前", "账号：[银行账号已脱敏] 10 月 20 日前"),
        ("账号：1234567890 10月底前", "账号：[银行账号已脱敏] 10月底前"),
        ("账号：1234567890 10月下旬", "账号：[银行账号已脱敏] 10月下旬"),
        ("账号：1234567890 1月上旬", "账号：[银行账号已脱敏] 1月上旬"),
        ("账号：1234567890 1月内", "账号：[银行账号已脱敏] 1月内"),
        ("账号：1234567890 10月后", "账号：[银行账号已脱敏] 10月后"),
        ("账号：1234567890 10月之前", "账号：[银行账号已脱敏] 10月之前"),
        ("账号：1234567890 20日之前", "账号：[银行账号已脱敏] 20日之前"),
        ("账号：1202021309001234 2025      年", "账号：[银行账号已脱敏] 2025      年"),
        ("账号：1234567890 2025\n年11月", "账号：[银行账号已脱敏] 2025\n年11月"),
        ("账号：1234567890 1 月", "账号：[银行账号已脱敏] 1 月"),
        ("账号：1234567890 12月，", "账号：[银行账号已脱敏] 12月，"),
        ("账号：1234567890 1 月 申报", "账号：[银行账号已脱敏] 1 月 申报"),
        ("账号：1234567890 1 月\n截止申报", "账号：[银行账号已脱敏] 1 月\n截止申报"),
        ("账号：1234567890 1月起", "账号：[银行账号已脱敏] 1月起"),
        ("账号：1234567890 1月至3月", "账号：[银行账号已脱敏] 1月至3月"),
        # 一两位数后面的"月均""月份""日期""日均"是词、不是日期；不在 1–12、1–31 的不是月、日：并进账号
        ("账户：3851 2761 6565 09 月均余额", "账户：[银行账号已脱敏] 月均余额"),
        ("账户：3851 2761 6565 09 日均余额", "账户：[银行账号已脱敏] 日均余额"),
        # "年费""年均""年检""年报"也是词：前面的一两位数、四位数照样并进账号
        ("卡号：6222 0212 3456 7890 12 年费", "卡号：[银行账号已脱敏] 年费"),
        ("账户：3851 2761 6565 09 年均余额", "账户：[银行账号已脱敏] 年均余额"),
        ("开户账号：2181-0597-8465-7112-55 年检", "开户账号：[银行账号已脱敏] 年检"),
        ("卡号4904-4502-0113-2075 年报", "卡号[银行账号已脱敏] 年报"),
        ("账号：1234567890 25年底前", "账号：[银行账号已脱敏] 25年底前"),
        ("账号：6222 0212 3456 7890 2025年第一批", "账号：[银行账号已脱敏] 2025年第一批"),
        # 四位数年份后面跟"年"都是年份（"年费""年均""年检""年报"除外）；两位数的才看"年"后面接什么
        (
            "收款账号：1202 0213 0900 1234 2025年专项资金",
            "收款账号：[银行账号已脱敏] 2025年专项资金",
        ),
        ("账号：1234567890 2025年各市申报", "账号：[银行账号已脱敏] 2025年各市申报"),
        # 19 位以内也一样断开：点号写的日期、年-月、年份后面隔空格接月份、隔很多空格的"年"
        ("账号：1234567890 2025.10.10前", "账号：[银行账号已脱敏] 2025.10.10前"),
        ("账号：1234567890 2025-10前", "账号：[银行账号已脱敏] 2025-10前"),
        ("账号：1234567890 2025 10月10日", "账号：[银行账号已脱敏] 2025 10月10日"),
        ("账号：1234567890 2025      年", "账号：[银行账号已脱敏] 2025      年"),
        # 不到 12 位（旧规则不遮）的号后面跟日期、金额写法：不为了凑够位数把它们并进来
        ("账号：622202123 10月以前", "账号：622202123 10月以前"),
        ("账号：622202123 20日以前", "账号：622202123 20日以前"),
        ("账号：6222021234 5月上半月", "账号：[银行账号已脱敏] 5月上半月"),
        ("账号：6222021 2025\uff0e10\uff0e10", "账号：6222021 2025\uff0e10\uff0e10"),
        ("账号：622202 3000多万元", "账号：622202 3000多万元"),
        ("账号：6222021 2025上半年", "账号：6222021 2025上半年"),
        ("账号：1234567890 2025年费用预算", "账号：[银行账号已脱敏] 2025年费用预算"),
        # 接了几组还不到 12 位的（旧规则不遮）只看第一组：后面的金额、年份写法认不认得出都不并进来
        ("账号：622202 3000.5万元", "账号：622202 3000.5万元"),
        ("账号：622202 3000 万元", "账号：622202 3000 万元"),
        ("账号：622202 3000（万元）", "账号：622202 3000（万元）"),
        ("账号：622202123 8千元", "账号：622202123 8千元"),
        ("账号：622202 2025 上半年", "账号：622202 2025 上半年"),
        ("账号：622202 2025 .10.10", "账号：622202 2025 .10.10"),
        ("账号：622202 \uff12\uff10\uff12\uff15年", "账号：622202 \uff12\uff10\uff12\uff15年"),
        ("账号：1234567890 1月申报", "账号：[银行账号已脱敏] 1月申报"),
        # 满 12 位的："月""日"后面紧跟别的汉字（"申报"）不是日期，这一组并进账号（同旧规则）
        ("账号：12345678901 1月申报", "账号：[银行账号已脱敏]月申报"),
        ("账号：1234567890 20日申报", "账号：[银行账号已脱敏]日申报"),
        # 全角斜杠写的日期、直接跟"下半年"的年份：断开
        ("账号：1234567890123 2025\uff0f10\uff0f10", "账号：[银行账号已脱敏] 2025\uff0f10\uff0f10"),
        ("账号：1234567890123 2025下半年", "账号：[银行账号已脱敏] 2025下半年"),
        # 整串用横线分组的照样接（日期要前面隔空格才断开）
        ("账号：1202021309-2025-10-10", "账号：[银行账号已脱敏]"),
        # 没写账号标签、只有行名的不遮；手机号先遮
        (
            "中国工商银行杭州分行 1202021309001234568",
            "中国工商银行杭州分行 1202021309001234568",
        ),
        ("账号：13812345678", "账号：[手机号已脱敏]"),
        ("卡号：6222 0212 3456 7890 12月份", "卡号：[银行账号已脱敏]月份"),
        (
            "账号：6222 0212 3456 7890 98日期：2025年10月10日",
            "账号：[银行账号已脱敏]日期：2025年10月10日",
        ),
        ("账号：6222 0212 3456 7890 98日前", "账号：[银行账号已脱敏]日前"),
        ("卡号：6222 0212 3456 7890 13月底", "卡号：[银行账号已脱敏]月底"),
        # 分组写的超过 19 位的，和旧规则一样只接到 19 位（露出最后一组）；连写的整组照样遮
        ("账号：3305 0161 6135 0000 0123", "账号：[银行账号已脱敏] 0123"),
        # 新规则一位都没遮、旧规则会遮的，照旧规则遮
        ("账号：3301 20251010 1234", "账号：[银行账号已脱敏]"),
        ("账号：1202-0213-0900-1234-568", "账号：[银行账号已脱敏]"),
        (
            "开户银行：中国工商银行杭州分行 1202021309001234568",
            "开户银行：中国工商银行杭州分行 [银行账号已脱敏]",
        ),
        ("零余额账户 2026 年度预算", "零余额账户 2026 年度预算"),
        ("账号：12345", "账号：12345"),
        # 手机号后面跟着一两位小数的是金额；三位以上的照样遮；前后连着数字、号段不对的不是手机号
        ("项目产值 13812345678.50 元", "项目产值 13812345678.50 元"),
        ("项目产值 13812345678.5 元", "项目产值 13812345678.5 元"),
        ("手机：13812345678.501元", "手机：[手机号已脱敏].501元"),
        ("编号2138123456789", "编号2138123456789"),
        ("客服12812345678", "客服12812345678"),
        ("身份证号：11010519491231002X", "身份证号：[身份证号已脱敏]"),
        # 座机前面的：贴着单位名的普通词（"文件"）、不是复姓的 4 个字都不当姓名
        ("省科技厅文件 0571-87654321", "省科技厅文件 [电话已脱敏]"),
        ("张三四五 0571-87654321", "张三四五 [电话已脱敏]"),
        ("电话 13812345678。", "电话 [手机号已脱敏]。"),
        ("手机：13812345678.13900000000", "手机：[手机号已脱敏].[手机号已脱敏]"),
    ],
)
def test_redaction_masks_only_the_numbers_and_names(raw: str, masked: str) -> None:
    assert redact_sensitive(raw) == masked
    # 正文给出前会再过一遍：遮过的再过一遍不变
    assert redact_sensitive(masked) == masked


@pytest.mark.parametrize(
    "stored",
    [
        # 库里按旧规则遮过的正文：再过一遍新规则不变（不会再多吞字）
        "加盖公章，[邮箱已脱敏]邮箱。",
        "请参训人员于9月4日（周五）17:[邮箱已脱敏]，组织方",
        "材料请于11月20[姓名已脱敏][电话已脱敏]",
        "省工信厅中小企[姓名已脱敏][电话已脱敏]",
        "联系人：[姓名已脱敏]处 [电话已脱敏]；",
        "（联系人：[姓名已脱敏]业处 张三；",
        # 新认的标签后面是旧规则吞剩的半截单位名：不当姓名
        "联系人及电话：中小企[姓名已脱敏][电话已脱敏]",
        "联系人及联系方式：科技厅高[姓名已脱敏][电话已脱敏]",
        # 第一遍遮的时候后面还有姓名的：前面的"高校""党委"不当这一行最后一个词
        "联系人：省科技厅 高校 [姓名已脱敏]",
        "联系人：党委_[姓名已脱敏]",
        "联系人：科技厅高[姓名已脱敏][电话已脱敏]",
        "联系人及电话：申请、[姓名已脱敏] [电话已脱敏]",
        "联系人电话：文件、[姓名已脱敏]",
        "联 系 人：应该 [姓名已脱敏]",
        "联系人及电话：高新区关于、[姓名已脱敏]",
        # 新认的标签后面、旧规则遮过的姓名前面的词：不以常见姓氏开头的不当姓名
        "联系人及电话：日前、[姓名已脱敏][电话已脱敏]",
        "联 系 人：明日 [姓名已脱敏][电话已脱敏]",
        "联系人电话：中小企、[姓名已脱敏][电话已脱敏]",
        # 新认的标签这一行后面有旧规则遮过的姓名：中间隔着标点、数字、别的标记，整行也不认
        "联系人电话：文件，[姓名已脱敏] [电话已脱敏]",
        "联系人及电话：申请材料，[姓名已脱敏] [电话已脱敏]",
        "联 系 人：应该。[姓名已脱敏]",
        "联系人及电话：关于办理；[姓名已脱敏]",
        "联系人和联系方式：方案文本：[姓名已脱敏] [电话已脱敏]",
        "联系人及联系电话：应该提交（[姓名已脱敏]）",
        "联系人及电话：申请材料 [邮箱已脱敏]、[姓名已脱敏]",
        "联系人及电话：方案2，[姓名已脱敏] [电话已脱敏]",
    ],
)
def test_old_redacted_bodies_stay_the_same(stored: str) -> None:
    assert redact_sensitive(stored) == stored


def test_protection_page_is_rejected() -> None:
    result = parse_detail(
        "<html><title>安全验证</title><body><div id='zoom'>请输入验证码</div></body></html>",
        "https://example.gov.cn/a.html",
    )
    assert result.failure_reason == "防护页或验证码"
    assert not result.body_text


# 和泸州、漯河、商丘、常德等站群页面上一模一样的站内搜索限流弹窗（结构见 detail._SEARCH_THROTTLE_SHAPE）
_THROTTLE_DIALOG = (
    '<div id="VolidateCodeDiv" class="modal in" aria-hidden="false" style="display: none;">'
    '<div class="modal-dialog"><div class="modal-content"><div class="modal-header">'
    '<button type="button" class="close"><span aria-hidden="true">×</span></button>'
    '<h4 class="modal-title">搜索过于频繁，请输入验证码</h4></div>'
    '<div class="modal-body"><div class="container-fluid"><div style="max-height: 600px;">'
    "<input id='js-ticket-search' name='CaptchaTicket' type='hidden' data-val='true'"
    " data-val-required='请点击验证!'>"
    "<input id='js-ticketId-search' name='CaptchaTicketId' type='hidden'>"
    "<input id='js-btn-verify-search' type='button' value='点击验证'"
    " class='form-control btn btn-gray btn-block'>"
    "<span class='field-validation-valid' hidden data-valmsg-for='Ticket'"
    " id='captchaticketvalid-search' data-valmsg-replace='true'></span>"
    '<input data-val="true" data-val-captcha-url="/captcha/validate" id="SearchValidateCode"'
    ' type="hidden"></div></div></div>'
    '<div class="modal-footer"><a class="btn btn-sm btn-primary" id="confirmNotApprove"'
    ' data-ajax="true" data-id="search">确定</a></div></div></div></div>'
)
_NOTICE_TEXT = (
    "<p>各有关单位：</p><p>为做好2026年度市级科技计划项目申报工作，现将有关事项通知如下，"
    "请各单位按要求认真组织申报，并于2026年12月1日前将申报材料报送至市科技局。</p>"
    "<p>一、申报范围。本市注册的企业、高校和科研院所均可申报，每家单位限报两项。</p>"
    "<p>二、申报要求。申报材料须真实完整，逾期不予受理。</p>"
)
_NOTICE_BODY = f"<div class='conTxt'><div id='zoom'>{_NOTICE_TEXT}</div></div>"


def _page(body: str, title: str = "某局关于组织申报的通知") -> str:
    return f"<html><title>{title}</title><body>{body}</body></html>"


@pytest.fixture
def verified_throttle_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试用的两个网站当成浏览器里核实过：页面代码和 _page() 拼出来的页面一样（没有外部脚本、样式，
    也没有页面自带的脚本）。"""
    plain_page = html.fromstring(_page(_THROTTLE_DIALOG + _NOTICE_BODY))
    for host in ("kjj.example.gov.cn", "www.example.gov.cn"):
        fingerprint = detail._page_code_fingerprint(plain_page, f"https://{host}/")
        monkeypatch.setitem(detail._SEARCH_THROTTLE_VERIFIED, host, frozenset({fingerprint}))


@pytest.mark.usefixtures("verified_throttle_sites")
def test_search_throttle_dialog_is_not_a_protection_page() -> None:
    """站内搜索的限流弹窗（泸州、漯河、常德等站群每一页都带、平时隐藏）不是挡住这一页的验证：网站和
    页面样子在浏览器里核实过、弹窗整个结构一模一样、整页没有别的验证字样、正文出自核实过的正文区而且
    完整，才放（D-013：认得出才放）。"""
    result = parse_detail(
        _page(_THROTTLE_DIALOG + _NOTICE_BODY), "https://kjj.example.gov.cn/1.html"
    )
    assert result.failure_reason is None and "组织申报" in result.body_text
    assert "搜索过于频繁" not in result.body_text
    # 常德那类站的正文区（div#content.lh-wznr）也认
    changde = _NOTICE_BODY.replace("<div class='conTxt'>", "<div id='content' class='lh-wznr'>")
    assert parse_detail(
        _page(_THROTTLE_DIALOG + changde), "https://www.example.gov.cn/2.html"
    ).body_text
    # 正文区把弹窗包在里面：弹窗的字不进正文
    inside = _NOTICE_BODY.replace("<p>各有关单位：</p>", f"<p>各有关单位：</p>{_THROTTLE_DIALOG}")
    result = parse_detail(_page(inside), "https://kjj.example.gov.cn/3.html")
    assert result.failure_reason is None and "搜索过于频繁" not in result.body_text
    assert "确定" not in result.body_text and "点击验证" not in result.body_text


@pytest.mark.usefixtures("verified_throttle_sites")
def test_search_throttle_dialog_only_on_verified_sites_and_page_code() -> None:
    """弹窗只在浏览器里核实过的网站、核实过的页面样子上认：换一个网站，或外部脚本、样式、框架多一个，
    或页面自带的脚本不一样（比如按类名把弹窗显示出来），都照旧当防护页。"""
    page = _page(_THROTTLE_DIALOG + _NOTICE_BODY)
    assert parse_detail(page, "https://kjj.example.gov.cn/1.html").failure_reason is None
    other_site = parse_detail(page, "https://other.example.gov.cn/1.html")
    assert other_site.failure_reason == "防护页或验证码"
    for extra in (
        "<script src='/js/other.js'></script>",
        "<link rel='stylesheet' href='/css/other.css'>",
        "<iframe src='/frame.html'></iframe>",
        "<script>$('.in').show();</script>",
        "<style>.modal{display:block!important}</style>",
    ):
        changed = _page(extra + _THROTTLE_DIALOG + _NOTICE_BODY)
        result = parse_detail(changed, "https://kjj.example.gov.cn/1.html")
        assert result.failure_reason == "防护页或验证码", extra


def test_search_throttle_page_code_must_match_verified_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """页面代码要和核实过的那一页一样：外部资源按完整地址（含协议、参数）和顺序比，页面自带的代码原样
    比；只有页面自带脚本里核实过的那几句（_PAGE_ID_CONTEXTS）的文章编号、栏目编号每页不同、不算，
    同样的数写在别处照样要一样。"""
    verified = _page(
        "<script src='/js/a.js?v=1'></script><script src='/js/b.js'></script>"
        '<script>$.ajax({ url: "/ContentManage/Ajax/GetRelatedContent?contentId=1092076&nodeId=37'
        '&moldType=Article&count=10", data: { excludeIds: "1092076", size: 10 } });'
        ' var delay = 1; var mark = "contentId=1092076";</script>' + _THROTTLE_DIALOG + _NOTICE_BODY
    )
    url = "https://kjj.example.gov.cn/1.html"
    fingerprint = detail._page_code_fingerprint(html.fromstring(verified), url)
    monkeypatch.setitem(
        detail._SEARCH_THROTTLE_VERIFIED, "kjj.example.gov.cn", frozenset({fingerprint})
    )
    assert parse_detail(verified, url).failure_reason is None
    other_page = verified.replace(
        "contentId=1092076&nodeId=37", "contentId=1161839&nodeId=52"
    ).replace('excludeIds: "1092076"', 'excludeIds: "1161839"')
    assert parse_detail(other_page, url).failure_reason is None
    swapped = verified.replace(
        "<script src='/js/a.js?v=1'></script><script src='/js/b.js'></script>",
        "<script src='/js/b.js'></script><script src='/js/a.js?v=1'></script>",
    )
    for changed in (
        verified.replace("?v=1", "?v=2"),
        swapped,
        verified.replace("delay = 1", "delay = 2"),
        verified.replace('mark = "contentId=1092076"', 'mark = "contentId=1161839"'),
        verified.replace("var delay", "showDialog(); var delay"),
    ):
        assert parse_detail(changed, url).failure_reason == "防护页或验证码"
    http_page = parse_detail(verified, "http://kjj.example.gov.cn/1.html")
    assert http_page.failure_reason == "防护页或验证码"


_LONG_NAV = "".join(f"<p>栏目导航第{n}项</p>" for n in range(60))
_LONG_TEXT = "".join(
    f"<p>第{n}条，本办法所称项目，是指市级财政资金支持的科技计划项目。</p>" for n in range(30)
)


@pytest.mark.parametrize(
    "page",
    [
        # 标题写着验证
        _page(_THROTTLE_DIALOG + _NOTICE_BODY, title="安全验证"),
        # 弹窗外面另有验证码字样：前 500 字里，或者 500 字以后、正文区外面
        _page(_THROTTLE_DIALOG + "<p>请输入验证码后继续访问</p>" + _NOTICE_BODY),
        _page(_THROTTLE_DIALOG + _LONG_NAV + "<p>访问过于频繁，请稍后再试</p>" + _NOTICE_BODY),
        # 在取正文时会去掉的地方（导航、页尾、分享栏、noscript）或属性上（输入框提示）也算
        _page("<nav>请输入验证码</nav>" + _THROTTLE_DIALOG + _NOTICE_BODY),
        _page(_THROTTLE_DIALOG + _NOTICE_BODY + "<footer>访问过于频繁，请稍后再试</footer>"),
        _page(_THROTTLE_DIALOG + "<div class='share'>安全验证</div>" + _NOTICE_BODY),
        _page(_THROTTLE_DIALOG + "<noscript>请开启浏览器的 JavaScript</noscript>" + _NOTICE_BODY),
        _page(_THROTTLE_DIALOG + "<input placeholder='请输入验证码'>" + _NOTICE_BODY),
        # 弹窗是显示着的，或 style 后面又改成显示
        _page(_THROTTLE_DIALOG.replace("display: none;", "") + _NOTICE_BODY),
        _page(
            _THROTTLE_DIALOG.replace("display: none;", "display: none; display: block;")
            + _NOTICE_BODY
        ),
        # 结构不一样：标题、多一句说明、按钮上的字、多一张图片、少了票据框
        _page(_THROTTLE_DIALOG.replace("搜索过于频繁，", "访问过于频繁，") + _NOTICE_BODY),
        _page(
            _THROTTLE_DIALOG.replace(
                '<div class="modal-footer">', '<p>请先登录</p><div class="modal-footer">'
            )
            + _NOTICE_BODY
        ),
        _page(_THROTTLE_DIALOG.replace("value='点击验证'", "value='登录后查看'") + _NOTICE_BODY),
        _page(
            _THROTTLE_DIALOG.replace("<span aria-hidden", "<img alt='验证码'><span aria-hidden")
            + _NOTICE_BODY
        ),
        _page(_THROTTLE_DIALOG.replace("js-ticket-search", "x") + _NOTICE_BODY),
        # 页面自带的脚本、样式、事件属性和核实过的页面不一样（可能一打开就把弹窗显示出来）
        _page(_THROTTLE_DIALOG + "<script>$('#VolidateCodeDiv').show();</script>" + _NOTICE_BODY),
        _page(
            "<style>#VolidateCodeDiv{display:block!important}</style>"
            + _THROTTLE_DIALOG
            + _NOTICE_BODY
        ),
        "<html><title>通知</title><body onload=\"$('#VolidateCodeDiv').show()\">"
        + _THROTTLE_DIALOG
        + _NOTICE_BODY
        + "</body></html>",
        # 不是这个弹窗、只是页面上写着这句话
        _page("<p>搜索过于频繁，请输入验证码</p>" + _NOTICE_BODY),
        # 弹窗认得出、但正文不对：不在核实过的正文区、太短、没有句号、没有正文区、正文里另有验证码字样
        _page(_THROTTLE_DIALOG + f"<div id='zoom'>{_NOTICE_TEXT}</div>"),
        _page(
            _THROTTLE_DIALOG
            + "<div class='conTxt'><p>各有关单位：现将申报事项通知如下，请按要求申报。</p></div>"
        ),
        _page(
            _THROTTLE_DIALOG
            + "<div class='conTxt'>"
            + "".join(f"<p>第{n}项，项目名称，申报单位，联系人</p>" for n in range(12))
            + "</div>"
        ),
        _page(_THROTTLE_DIALOG),
        _page(
            _THROTTLE_DIALOG
            + "<div class='conTxt'>"
            + _LONG_TEXT
            + "<p>请输入验证码后查看全文。</p></div>"
        ),
    ],
)
@pytest.mark.usefixtures("verified_throttle_sites")
def test_other_captcha_pages_stay_protection_pages(page) -> None:
    assert parse_detail(page, "https://kjj.example.gov.cn/a.html").failure_reason == (
        "防护页或验证码"
    )


def test_dropdown_options_are_not_body() -> None:
    """下拉框里的跳转选项不算正文（11/12 漯河：正文区空着，兜底时把"省级厅委导航"一串选项当正文）。"""
    options = "".join(
        f"<option value='https://{n}.gov.cn/'>某某省第{n}厅</option>" for n in range(30)
    )
    page = (
        "<html><title>某局文件</title><body><div class='row'><select><option>--省级厅委导航--</option>"
        f"{options}</select></div><div class='conTxt'></div></body></html>"
    )
    result = parse_detail(page, "https://scjg.example.gov.cn/content_1.html")
    assert "厅委导航" not in (result.raw_body or "") and not result.body_text
    # 正文里本来就有的下拉框也不进正文，正文照取
    with_body = page.replace(
        "<div class='conTxt'></div>",
        "<div id='zoom'><p>各有关单位：</p><p>现将2026年度项目申报有关事项通知如下，请按要求组织申报。</p>"
        "<select><option>请选择</option><option>某县</option></select></div>",
    )
    result = parse_detail(with_body, "https://scjg.example.gov.cn/content_2.html")
    assert "组织申报" in result.body_text and "请选择" not in result.body_text


def test_dropdown_inside_first_body_line_keeps_fields() -> None:
    """正文第一行中间夹着下拉框：发文机关、文号、发布日期照样从正文前面的字里找（不退回整页前 700 字，
    前面 80 行导航已经超过 700 字）。"""
    nav = "".join(f"<p>栏目导航第{n}项</p>" for n in range(80))
    page = (
        f"<html><title>某局文件</title><body>{nav}<div class='meta'><p>发布机构：某某市科学技术局</p>"
        "<p>发布日期：2026-11-05</p><p>文号：某科〔2026〕12号</p></div>"
        "<div id='zoom'><p>各有关单位：现将<select><option>某县</option><option>某区</option>"
        "</select>项目申报有关事项通知如下，请按要求组织申报。</p>"
        "<p>申报材料请于本月底前报送。</p></div></body></html>"
    )
    result = parse_detail(page, "https://kjj.example.gov.cn/content_4.html")
    assert (
        result.body_text.startswith("各有关单位：现将项目申报") and "某县" not in result.body_text
    )
    assert result.issuer == "某某市科学技术局"
    assert result.doc_number == "某科〔2026〕12号"
    assert result.publish_date == date(2026, 11, 5)


def test_body_first_line_repeated_above_meta_keeps_fields() -> None:
    """正文第一句（标题）在发布信息上面也出现过一遍：按正文节点所在的位置找发布信息，不在上面那一处
    截断（截断的话发文机关、文号、发布日期都会漏掉）。"""
    page = (
        "<html><title>某局文件</title><body><h1>关于开展项目申报工作的通知</h1>"
        "<div class='meta'><p>发布机构：某某市科学技术局</p><p>发布日期：2026-11-05</p>"
        "<p>文号：某科〔2026〕12号</p></div><div id='zoom'><p>关于开展项目申报工作的通知</p>"
        "<p>各有关单位：现将项目申报有关事项通知如下，请按要求组织申报。</p></div></body></html>"
    )
    result = parse_detail(page, "https://kjj.example.gov.cn/content_5.html")
    assert result.body_text.startswith("关于开展项目申报工作的通知")
    assert result.issuer == "某某市科学技术局"
    assert result.doc_number == "某科〔2026〕12号"
    assert result.publish_date == date(2026, 11, 5)


def _issuer_page(
    title: str,
    publisher: str,
    source: str = "",
    label: str = "",
    signature: str = "",
    number: str = "",
) -> str:
    """仿政府网站文件页：ArticleTitle 是文件标题，信息栏写发布机构（和发文机关、文号），元数据写来源，
    正文末尾可有落款。"""
    meta = f"<meta name='ArticleTitle' content='{title}'>"
    meta += f"<meta name='contentSource' content='{source}'>" if source else ""
    info = f"<p>发布机构：{publisher}</p>" if publisher else ""
    info += f"<p>发文机关：{label}</p>" if label else ""
    info += f"<p>文号：{number}</p>" if number else ""
    tail = f"<p>{signature}</p><p>2026年11月5日</p>" if signature else ""
    return (
        f"<html><head><title>某某网站</title>{meta}</head><body><div class='meta'>{info}"
        "<p>发布日期：2026-11-05</p></div><div id='zoom'>"
        "<p>各有关单位：现将有关事项通知如下，请按要求认真组织实施。</p>"
        f"<p>请各单位于本月底前报送材料。</p>{tail}</div></body></html>"
    )  # fmt: skip


PZH = "攀枝花市文化广播电视和旅游局"
SC = "四川省文化和旅游厅"


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        # 标题开头的机关比"发布机构"准：市政府发文挂在办公厅网站上
        (
            _issuer_page(
                "济南市人民政府关于印发济南市2026年国民经济和社会发展计划的通知",
                "济南市人民政府办公厅",
            ),
            "济南市人民政府",
        ),
        # 转载的上级文件：标题写着"某某厅等 7 部门"，来源只写"办公室"
        (
            _issuer_page(
                "浙江省商务厅等7部门关于印发《2026年浙江省消费品以旧换新补贴实施细则》的通知",
                "绍兴市商务局", "办公室",
            ),
            "浙江省商务厅等7部门",
        ),
        # 标题里几个机关联合发文
        (
            _issuer_page(
                "湘潭市发展和改革委员会 湘潭市教育局 湘潭市财政局关于规范停车服务收费的通知",
                "湘潭市发改委",
            ),
            "湘潭市发展和改革委员会、湘潭市教育局、湘潭市财政局",
        ),
        (_issuer_page("商务部关于印发对外贸易工作要点的通知", "某某市商务局"), "商务部"),
        # 标题里不是完整机关名的不用：缺地名的简称、内设机构、新闻标题（人名、会议）
        (
            _issuer_page(
                "市政府办公室印发关于进一步加强农村集体资产监督管理的意见", "苏州市人民政府办公室"
            ),
            "苏州市人民政府办公室",
        ),
        (_issuer_page("办公室关于做好假期值班工作的通知", "某某市科学技术局"), "某某市科学技术局"),
        (
            _issuer_page("某某主持召开市政府关于安全生产工作的专题会议", "某某市人民政府办公室"),
            "某某市人民政府办公室",
        ),
        (
            _issuer_page("某某在国务院关于推进营商环境建设会议上的讲话", "某某市人民政府办公室"),
            "某某市人民政府办公室",
        ),
        (
            _issuer_page("某某市市长某某在市政府关于安全生产工作会议上的讲话",
                         "某某市人民政府办公室"),
            "某某市人民政府办公室",
        ),
        # "转发……关于""关于转发……关于"：开头不是发文机关
        (_issuer_page("转发省商务厅关于做好外贸稳增长工作的通知", "某某市商务局"), "某某市商务局"),
        (_issuer_page("关于转发财政部关于加强资金管理的通知", "某某市财政局"), "某某市财政局"),
        (
            _issuer_page("州政府办公室关于做好防汛工作的通知", "凉山彝族自治州人民政府办公室"),
            "凉山彝族自治州人民政府办公室",
        ),
        # 转载的上级文件不靠来源、文号头一个字替换（"苏府"是苏州市的文号，江苏省的简称也是"苏"），
        # 照旧用发布机构
        (_issuer_page("天府旅游名村评选管理办法", PZH, SC, number="川文旅规〔2026〕3号"), PZH),
        (
            _issuer_page("关于优化营商环境的若干措施", "苏州市人民政府办公室", "江苏省人民政府",
                         number="苏府〔2026〕1号"),
            "苏州市人民政府办公室",
        ),
        # 开头不是地名的不算：人名（"某某代表市"）、机关名里的普通词（"城乡"）
        (
            _issuer_page("某某代表市政府关于预算执行情况的报告", "某某市人民政府办公室"),
            "某某市人民政府办公室",
        ),
        (
            _issuer_page("住房和城乡建设局关于做好物业管理工作的通知", "攀枝花市住房和城乡建设局"),
            "攀枝花市住房和城乡建设局",
        ),
        # 地名名单里的：自治州简称、开发区、单字县名
        (
            _issuer_page("凉山州人民政府关于印发工作要点的通知", "凉山州经济和信息化局"),
            "凉山州人民政府",
        ),
        # 地名后面的名称不在机关名称词表里的不用（领导小组办公室这类中间是自由文字的、地名后面夹着
        # 人名的），照旧用发布机构
        (
            _issuer_page("凉山州工业经济发展领导小组办公室关于印发工作要点的通知", "凉山州经济和信息化局"),
            "凉山州经济和信息化局",
        ),
        (
            _issuer_page("北京市某某建议市政府关于加强管理的意见", "北京市人民政府办公厅"),
            "北京市人民政府办公厅",
        ),
        (
            _issuer_page("北京市财政局某某建议市政府关于加强管理的意见", "北京市财政局"),
            "北京市财政局",
        ),
        # 中央机关在地方的分支、党委机构
        (
            _issuer_page("国家税务总局龙岩市税务局关于做好纳税服务的通告", "龙岩市财政局"),
            "国家税务总局龙岩市税务局",
        ),
        (
            _issuer_page("中共泉州市委人才工作领导小组办公室关于印发人才政策的通知", "泉州市科技局"),
            "中共泉州市委人才工作领导小组办公室",
        ),
        (
            _issuer_page("中国人民银行上海分行关于做好金融服务的通知", "上海市地方金融管理局"),
            "中国人民银行上海分行",
        ),
        (
            _issuer_page("国家金融监督管理总局四川监管局关于加强保险服务的通知", "四川省财政厅"),
            "国家金融监督管理总局四川监管局",
        ),
        (
            _issuer_page("国家外汇管理局江苏省分局关于便利跨境结算的通知", "江苏省商务厅"),
            "国家外汇管理局江苏省分局",
        ),
        # 几级地名连写
        (
            _issuer_page("四川省攀枝花市东区人民政府关于开展安全检查的通知", "攀枝花市东区人民政府办公室"),
            "四川省攀枝花市东区人民政府",
        ),
        (
            _issuer_page("雅安经济技术开发区管理委员会关于印发产业扶持办法的通知", "雅安市人民政府"),
            "雅安经济技术开发区管理委员会",
        ),
        (_issuer_page("沛县人民政府关于开展安全检查的通知", "沛县人民政府办公室"), "沛县人民政府"),
        (
            _issuer_page("北京经济技术开发区管理委员会关于印发产业政策的通知", "北京市人民政府办公厅"),
            "北京经济技术开发区管理委员会",
        ),
        # 机关名里带"城乡""督导"这类词的照样是机关
        (
            _issuer_page("四川省住房和城乡建设厅关于加强物业服务管理的通知", "攀枝花市住房和城乡建设局"),
            "四川省住房和城乡建设厅",
        ),
        (
            _issuer_page("某某县人民政府教育督导委员会办公室关于开展督导评估的通知", "沛县教育局"),
            "沛县教育局",
        ),
        (
            _issuer_page("沛县教育局关于开展督导评估的通知", "沛县人民政府办公室"),
            "沛县教育局",
        ),
        # 来源和发布机构同级（只是简称）：照旧用发布机构
        (
            _issuer_page("关于规范停车服务收费的通知", "湘潭市发展和改革委员会", "市发改委"),
            "湘潭市发展和改革委员会",
        ),
        # 只写内设机构的名字（信息中心、办公室）不当发文机关
        (_issuer_page("关于印发传统工艺美术保护品种的通知", "信息中心", "消费品产业处"), None),
        (_issuer_page("政府网站工作年度报表（2025年度）", "", "办公室"), None),
        # 明写的发文机关、正文落款照旧在标题前面
        (
            _issuer_page("某某市人民政府关于开展安全检查的通知", "某某市人民政府办公室",
                         label="某某市科学技术局"),
            "某某市科学技术局",
        ),
        (
            _issuer_page("某某市人民政府关于开展安全检查的通知", "某某市人民政府办公室",
                         signature="某某市财政局"),
            "某某市财政局",
        ),
    ],
)  # fmt: skip
def test_issuer_from_title_and_publisher(page: str, expected: str | None) -> None:
    """发文机关的顺序：落款、明写的发文机关、文件标题开头的机关（要是以地名、中央机关开头的完整
    机关名）、发布机构、元数据；只写内设机构名字的不算。"""
    assert parse_detail(page, "https://www.example.gov.cn/content_6.html").issuer == expected


@pytest.mark.parametrize(
    ("broken", "how"),
    [
        (("place_names.txt", "agency_bodies.txt"), "missing"),
        (("place_names.txt",), "missing"),
        (("agency_bodies.txt",), "missing"),
        (("place_names.txt",), "not_utf8"),
        (("agency_bodies.txt",), "not_utf8"),
    ],
)
def test_issuer_without_name_lists_falls_back_to_publisher(
    monkeypatch, tmp_path, broken, how
) -> None:
    """发文机关校验用的名单读不到（没有文件、不是 UTF-8；两份都坏或只坏一份）：标题里的机关一概
    不用（中央机关也不用），照旧看发布机构，不报错。"""
    data = Path(detail.__file__).resolve().parents[1] / "data"
    for name in ("place_names.txt", "agency_bodies.txt"):
        if name not in broken:
            (tmp_path / name).write_bytes((data / name).read_bytes())
        elif how == "not_utf8":
            (tmp_path / name).write_bytes(b"\xff\xfe\x00broken")
    monkeypatch.setattr(detail, "_DATA_DIR", tmp_path)
    detail._agency_names.cache_clear()
    try:
        for title, publisher in (
            ("济南市人民政府关于印发计划的通知", "济南市人民政府办公厅"),
            ("国务院办公厅关于做好政务公开工作的通知", "某某市人民政府办公室"),
        ):
            page = _issuer_page(title, publisher)
            assert (
                parse_detail(page, "https://www.example.gov.cn/content_7.html").issuer == publisher
            )
    finally:
        detail._agency_names.cache_clear()


def test_dropdown_text_still_counts_for_protection_window() -> None:
    """防护页判断看页面前 500 字，下拉框的字照旧算在里面，范围不变（下拉框后面很远处的
    验证码字样，原来不当防护页，现在也不当）。"""
    options = "".join(f"<option>某某市第{n}个部门网站</option>" for n in range(80))
    page = (
        f"<html><title>某局文件</title><body><select>{options}</select>"
        "<p>搜索过于频繁，请输入验证码</p><div id='zoom'><p>各有关单位：</p>"
        "<p>现将2026年度项目申报有关事项通知如下，请按要求组织申报。</p></div></body></html>"
    )
    result = parse_detail(page, "https://www.example.gov.cn/content_3.html")
    assert result.failure_reason is None and "组织申报" in result.body_text


def test_density_fallback_ignores_navigation_and_redacts() -> None:
    sample = (
        "<html><body><div class='top-links'><a href='/a'>网站首页栏目导航公告通知</a>"
        "<a href='/b'>政策服务信息公开相关链接</a></div>"
        "<div class='story'><p>各单位应按规定报送申报材料，主管部门将逐项核对材料。</p>"
        "<p>申报截止2026年10月30日，联系人电话13812345678。</p></div>"
        "<div class='related'>相关新闻推荐报道</div></body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/a.html")
    assert result.failure_reason is None
    assert result.body_text.startswith("各单位应按规定")
    assert "网站首页" not in result.body_text
    assert "13812345678" not in result.body_text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("国家税务总局 辽宁省税务局", "国家税务总局辽宁省税务局"),
        ("国家金融监督管理总局 广东监管局", "国家金融监督管理总局广东监管局"),
        ("中国人民银行 江苏省分行", "中国人民银行江苏省分行"),
        ("国家外汇管理局 上海市分局", "国家外汇管理局上海市分局"),
        ("海关总署 沈阳海关", "海关总署沈阳海关"),
        ("辽宁省财政厅 国家税务总局 辽宁省税务局", "辽宁省财政厅、国家税务总局辽宁省税务局"),
        ("安 徽 省 财 政 厅", "安徽省财政厅"),
        (
            "福建省工程技术经济专业职称改革领导小组 福建省职称改革工作办公室",
            "福建省工程技术经济专业职称改革领导小组、福建省职称改革工作办公室",
        ),
    ],
)
def test_issuer_branch_names_are_not_split(line: str, expected: str) -> None:
    assert _split_issuer_line(line) == expected
    sample = (
        "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。"
        "申报单位应当按照规定提交材料。</p>"
        f"<p>{line}</p><p>2026年9月24日</p></div></body></html>"
    )
    assert parse_detail(sample, "https://example.gov.cn/a.html").issuer == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("安徽省工业和信息化厅、安 徽 省 财 政 厅", "安徽省工业和信息化厅、安徽省财政厅"),
        (
            "福建省工程技术经济专业职称改革领导小组 福建省职称改革工作办公室",
            "福建省工程技术经济专业职称改革领导小组、福建省职称改革工作办公室",
        ),
        ("国家税务总局 安徽省税务局", "国家税务总局安徽省税务局"),
        # 名称中间的排版空格不当成两个机关
        ("上海市 科学技术委员会", "上海市 科学技术委员会"),
    ],
)
def test_normalize_issuer_display(value: str, expected: str) -> None:
    from youju.issuer import normalize_issuer

    assert normalize_issuer(value) == expected


def test_attachment_links_written_in_page_scripts() -> None:
    """TRS 模板把附件链接写在脚本里（var hasFJ='<a href=...>'），页面结构里没有（10/15 查名单读不出企业名的原因）。"""
    result = parse_detail(
        read_fixture_html("detail_chongqing_kjj_script_attachments"),
        "https://kjj.cq.gov.cn/zwxx_176/tzgg/202609/t20260904_16033963.html",
    )
    assert [(a.name, a.url, a.extension) for a in result.attachments] == [
        (
            "2026年第八批入库科技型企业名单.pdf",
            "https://kjj.cq.gov.cn/zwxx_176/tzgg/202609/P020260904620028456189.pdf",
            "pdf",
        )
    ]
    result = parse_detail(
        read_fixture_html("detail_ningxia_gxt_script_attachments"),
        "https://gxt.nx.gov.cn/zwgk/fdzdgknr/glfw/gsgg/202609/t20260923_5354445.html",
    )
    assert [a.name for a in result.attachments] == [
        "附件1.2026年度认定的自治区专精特新中小企业名单.xlsx",
        "附件2.复核通过的自治区专精特新中小企业名单（第二批）.xlsx",
        "附件3.取消认定的自治区专精特新中小企业名单（第二批）.xlsx",
    ]
    assert all(
        a.extension == "xlsx" and a.url.startswith("https://gxt.nx.gov.cn/")
        for a in result.attachments
    )


def test_script_links_follow_the_same_rules() -> None:
    """脚本里的链接按同样的规则收：要是文件、外站的要标明附件、同一地址只收一次；外部脚本不看。"""
    sample = (
        "<html><head><script src='https://example.gov.cn/a.js'></script></head><body>"
        "<div id='zoom'><p>关于公布有关名单的通知，全文见附件。</p>"
        "<p><a href='/files/list.xlsx'>附件1.名单.xlsx</a></p></div>"
        '<script>var hasFJ=\'<a href="/files/list.xlsx">附件1.名单.xlsx</a><BR/>'
        '<a href="./more.pdf">附件2.补充名单.pdf</a><BR/>'
        '<a href="https://other.example.com/ref.pdf">引用的文件.pdf</a>\';'
        "var stat='<a href=\"https://tongji.example.com/track\">统计</a>';"
        "var other='<a href=\"https://other.example.com/notice-att.docx\">附件3.通知.docx</a>';</script>"
        "</body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/zw/a.html")
    assert [(a.name, a.url) for a in result.attachments] == [
        ("附件1.名单.xlsx", "https://example.gov.cn/files/list.xlsx"),
        ("附件2.补充名单.pdf", "https://example.gov.cn/zw/more.pdf"),
        ("附件3.通知.docx", "https://other.example.com/notice-att.docx"),
    ]


def test_script_attachment_escaped_slashes_and_markup():
    sample = r"""<div id="zoom">这是一份公开的名单通知，申报单位请按规定准备材料。
    <script>var a = '<a href=".\/files\/list.xlsx"><b>附件名单.xlsx</b><\/a>';</script></div>"""
    result = parse_detail(sample, "https://example.gov.cn/notice/index.html")
    assert len(result.attachments) == 1
    assert result.attachments[0].url == "https://example.gov.cn/notice/files/list.xlsx"
    assert result.attachments[0].name == "附件名单.xlsx"


def test_attachments_on_provincial_file_server() -> None:
    """附件放在本省政府网站群统一的文件服务器上（河南科技厅 kjt.henan.gov.cn → xcoss.henan.gov.cn），
    链接文字里没写"附件"（10/16：河南 2026 年第一批 11903 家入库科技型中小企业名单原来没认出来）。
    只认明确列出的文件服务器：同省别的子域、别的政府网站、非政府网站，没写"附件"的照旧不收。"""
    page = (
        "<html><body><div id='zoom'><p>现将通过公示的河南省2026年第一批入库企业名单（详见附件）予以公告。</p>"
        "<p>附件：<a href='https://xcoss.henan.gov.cn/typtfile/20260902/6f6b.pdf'>"
        "河南省2026年第一批入库科技型中小企业名单</a></p>"
        "<p>参考：<a href='https://www.henan.gov.cn/zwgk/policy.pdf'>省政府有关文件</a></p>"
        "<p>参考：<a href='https://www.most.gov.cn/xxgk/policy.pdf'>科技部有关文件</a></p>"
        "<p>另见<a href='https://other.example.com/notice.pdf'>其他网站的文件</a></p>"
        "</div></body></html>"
    )
    result = parse_detail(page, "http://kjt.henan.gov.cn/2026/09-02/3410350.html")
    assert [(a.name, a.url) for a in result.attachments] == [
        (
            "河南省2026年第一批入库科技型中小企业名单",
            "https://xcoss.henan.gov.cn/typtfile/20260902/6f6b.pdf",
        )
    ]
    # 文件服务器只对本省的页面放开：别的省的页面链过去，没写"附件"照旧不收
    other = page.replace("附件：<a", "<a")
    assert parse_detail(other, "https://kjt.hebei.gov.cn/a/1.html").attachments == []


def test_attachment_rule_counterexamples() -> None:
    """外站文件只看链接自己的文字里有没有"附件"，不看链接前面写的"附件："；
    文件服务器按主机名精确匹配，页面主机名要以"."加省份后缀结尾。"""
    page_url = "https://gxt.fujian.gov.cn/zwgk/tzgg/1.html"
    # 福建工信厅那种写法：正文"附件：1."后面链到国务院网站一份旧文件，真正的附件在页面底部
    cited = (
        "<html><body><div id='zoom'><p>根据相关文件精神，现将有关事项通知如下。</p>"
        "<p>附件：1.<a href='https://www.gov.cn/zhengce/2023/policy.pdf'>国务院有关文件</a></p>"
        "<p><a href='/attach/list.xlsx'>名单.xlsx</a></p></div></body></html>"
    )
    assert [a.url for a in parse_detail(cited, page_url).attachments] == [
        "https://gxt.fujian.gov.cn/attach/list.xlsx"
    ]
    # 链接自己的文字写了"附件"的外站文件照旧收（原有规则，没改）
    labelled = cited.replace(">国务院有关文件<", ">附件1：国务院有关文件<")
    assert "https://www.gov.cn/zhengce/2023/policy.pdf" in [
        a.url for a in parse_detail(labelled, page_url).attachments
    ]
    henan = "http://kjt.henan.gov.cn/2026/09-02/1.html"
    for href in (
        "https://xcoss.henan.gov.cn.example.com/typtfile/a.pdf",  # 仿冒主机名
        "https://xcoss.henan.gov.cn@example.com/typtfile/a.pdf",  # 用户名里写服务器名
        "https://oss.henan.gov.cn/typtfile/a.pdf",  # 同省别的子域，没列出
        "http://xcoss.henan.gov.cn:8080/typtfile/a.pdf",  # 非默认端口
    ):
        lookalike = (
            f"<html><body><div id='zoom'><p><a href='{href}'>名单</a></p></div></body></html>"
        )
        assert parse_detail(lookalike, henan).attachments == [], href
    upper = (
        "<html><body><div id='zoom'><p>"
        "<a href='https://XCOSS.HENAN.GOV.CN/typtfile/a.pdf'>名单</a></p></div></body></html>"
    )
    assert [a.url for a in parse_detail(upper, henan).attachments] == [
        "https://xcoss.henan.gov.cn/typtfile/a.pdf"
    ]
    port = upper.replace("XCOSS.HENAN.GOV.CN", "xcoss.henan.gov.cn:443")
    assert [a.url for a in parse_detail(port, henan).attachments] == [
        "https://xcoss.henan.gov.cn:443/typtfile/a.pdf"
    ]
    # 页面主机名只是以"henan.gov.cn"结尾、前面不是"."的，不算河南的网站
    assert parse_detail(upper, "https://www.zhenan.gov.cn/a/1.html").attachments == []


@pytest.mark.parametrize("label", ["账号", "联系人", "联系人：", "开户行：", "卡号\n"])
def test_long_whitespace_after_labels_does_not_hang(label: str) -> None:
    """PDF 里标签后面可能跟着一长串空格：正则不能回退到卡住（普通的 \\s*[:：]?\\s* 几万个空格就要几十秒）。"""
    import time

    started = time.perf_counter()
    for tail in ("x", "2025", "123"):
        # 不足 10 位的数字会走到"照旧规则遮"那一步，也不能卡住
        text = label + " " * 100_000 + tail
        assert redact_sensitive(text) == text
    assert time.perf_counter() - started < 2


def test_accounts_old_rule_masked_stay_masked() -> None:
    """旧规则（_BANK）会遮的账号，新规则留下的数字不能比旧规则多（红线上的号码不能比以前漏得多）。
    随机拼标签、分组、后缀。"""
    import random

    from youju.crawler.detail import _BANK
    from youju.refetch_redacted import old_redact

    rng = random.Random(20261021)
    labels = ("账号：", "银行账号 ", "卡号", "账户:", "帐号：", "银行卡号：", "开户账号：", "账号")
    tails = (
        "",
        "。",
        " 日期：2025年10月10日",
        "元",
        " 次日到账",
        " 号码",
        "时间2025年",
        " 1500",
        "，请核对",
        " 月均余额",
        " 日均余额",
        "月份",
        "日期：",
        " 年费",
        "年均余额",
        " 年检",
        "年报",
    )
    for _ in range(3000):
        size = rng.randint(10, 22)
        digits = "".join(rng.choice("0123456789") for _ in range(size))
        sep = rng.choice(("", " ", "-"))
        cut = rng.choice((4, 6, size))
        run = sep.join(digits[i : i + cut] for i in range(0, size, cut))
        text = rng.choice(labels) + run + rng.choice(tails)
        if _BANK.search(text):
            visible = sum(c.isdigit() for c in redact_sensitive(text))
            assert visible <= sum(c.isdigit() for c in old_redact(text)), text


def test_redaction_is_idempotent_on_random_mixes() -> None:
    """随机拼姓名、单位名、标签、号码、邮箱：遮过一遍的再过一遍不变。"""
    import random

    rng = random.Random(20261020)
    pieces = (
        "联系人：", "联系人及电话：", "联 系 人：", "张三", "李 四", "欧阳小明", "省工信厅中小企业处",
        "高新处", "济南", "白天", " ", "，", "、", "\n", "0571-87654321", "abc@163.com", "-", "_",
        "13812345678", "账号：1202021309001234568", "2025年10月10日前", "发送至", "[姓名已脱敏]",
        "[电话已脱敏]", "高校", "杭州", "王镇", "张路", "党委",
    )  # fmt: skip
    for _ in range(5000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(3, 15)))
        once = redact_sensitive(text)
        assert redact_sensitive(once) == once, text
