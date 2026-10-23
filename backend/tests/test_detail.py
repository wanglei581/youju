from datetime import date

import pytest

from tests.conftest import read_fixture_html
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
    "detail_anhui_kjt": (9, 338),
    "detail_sichuan_kjt": (19, 1041),
    "detail_jiangsu_gxt": (4, 160),
    "detail_shandong_gxt": (7, 363),
    "detail_henan_kjt": (7, 501),
    "detail_shanghai_sheitc": (3, 318),
    "detail_qinghai_gxt": (14, 473),
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
    ],
)
def test_body_has_no_page_chrome(fixture, url, first, last, issuer, published, attachments) -> None:
    """正文只取正文区：网页上的"当前位置"、标题和发布信息、字号和打印按钮、二维码、"相关文档"、
    离站提示弹窗等都不算正文（10/22 湖北科技厅 70955、67683 正文开头是导航、结尾是公众号和弹窗；
    同样问题的来源 10/22 抽样找出，见 PR 说明）。附件照旧从整页收。行数、字数也核对（中间漏段会变）。"""
    result = parse_detail(read_fixture_html(fixture), url)
    lines = result.body_text.splitlines()
    assert (len(lines), len(result.body_text)) == _BODY_SIZE[fixture]
    assert lines[0].startswith(first)
    assert lines[-1] == last
    assert result.body_text.count(lines[0]) == 1
    assert [c for c in _PAGE_CHROME if c in result.body_text] == []
    assert _CHROME_LINES.isdisjoint(lines)
    assert result.issuer == issuer
    assert result.publish_date == published
    assert len(result.attachments) == attachments


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
    # #zoom 里编辑器块后面还有 20 字以上（可能是正文的后半段）：用 #zoom 整块
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


def test_protection_page_is_rejected() -> None:
    result = parse_detail(
        "<html><title>安全验证</title><body><div id='zoom'>请输入验证码</div></body></html>",
        "https://example.gov.cn/a.html",
    )
    assert result.failure_reason == "防护页或验证码"
    assert not result.body_text


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
