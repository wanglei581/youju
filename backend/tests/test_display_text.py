"""只在显示时做的处理（v1.display_text）。

1. 已遮号码后面紧跟着的号码也遮掉（11/2 完整测试第 5 条）：入库规则只遮带区号的座机和手机号，
   "咨询电话：（这里隐去了电话）、87687769"这样同一处接着列的不带区号的号码漏了。只认紧跟在已遮号码
   后面、用"、""/""，""或"隔开的 7–8 位号码；金额、年份、文号、编号不碰。
2. 网址换成"（这里隐去了网址）"（D-112，创始人 11/2 同意）。

只改给页面的文字（详情页正文、阅读页正文和附件、名单单行页），入库的文字、交给模型和核对 AI 原句
用的文字不变。"""

import re

import pytest

from tests.test_readers import client, path, seed  # noqa: F401 - 夹具和造数据的函数
from youju import attachment_text as texts
from youju.ai import content, interpret
from youju.ai.model_text import model_text
from youju.api import v1
from youju.models import AttachmentTextSegment

MARK = "（这里隐去了电话）"


@pytest.mark.parametrize(
    "shown, expected",
    [
        # 测试记录里的两处原文（入库标记的写法）
        (
            "1.省综合业务咨询电话：[电话已脱敏]、87687769。",
            f"1.省综合业务咨询电话：{MARK}、{MARK}。",
        ),
        ("联系部门：安徽省科技厅企业处 电话:[电话已脱敏]/62651782", f"电话:{MARK}/{MARK}"),
        # 页面说法的写法、手机号标记后面也认
        (f"电话：{MARK}，87687769", f"电话：{MARK}，{MARK}"),
        ("电话：[手机号已脱敏]或87687769", f"电话：{MARK}或{MARK}"),
        # 一个接一个、半角逗号、全角斜杠、前后有空格
        # 连着 3 个同样的标记合成一个（独立审阅），"或"后面的照旧
        (f"{MARK}、87687769、87687770或88888888", f"{MARK}或{MARK}"),
        (f"{MARK},87687769", f"{MARK},{MARK}"),
        (f"{MARK}／62651782", f"{MARK}／{MARK}"),
        (f"{MARK} / 62651782", f"{MARK} / {MARK}"),
        # 7 位、带分机、号段
        (f"{MARK}、8768776。", f"{MARK}、{MARK}。"),
        (f"{MARK}、87687769转801，", f"{MARK}、{MARK}，"),
        (f"{MARK}、87687769-801）", f"{MARK}、{MARK}）"),
        (f"{MARK}、87687769-87687770", f"{MARK}、{MARK}"),
        # 句末的英文句点照样算
        (f"{MARK}、87687769.", f"{MARK}、{MARK}."),
    ],
)
def test_numbers_right_after_a_masked_phone_are_masked(shown, expected):
    assert expected in v1.display_text(shown)
    assert "8768" not in v1.display_text(shown) and "6265" not in v1.display_text(shown)


@pytest.mark.parametrize(
    "text",
    [
        # 不紧跟在已遮号码后面的：照旧（入库规则不遮不带区号的号码）
        "邮编：250001、87687769",
        f"{MARK}。87687769",
        # 金额、小数
        f"{MARK}，30000000元",
        f"{MARK}、5000000万元",
        f"{MARK}、87687769.5",
        f"{MARK}，2000000％",
        # 年份、日期、文号、编号
        f"{MARK}，2026年11月20日前报送",
        f"{MARK}，20261120前报送",
        f"{MARK}、粤科函产字〔2026〕1737号",
        f"{MARK}、2026001号",
        f"{MARK}、第3批",
        # 位数不对、以 0 或 1 开头的不是本地号码
        f"{MARK}、876877690",
        f"{MARK}、123456",
        f"{MARK}、12345678",
        # 分隔符不在名单里
        f"{MARK}87687769",
    ],
)
def test_other_numbers_are_left_alone(text):
    assert v1.display_text(text) == v1.display_marks(text)


def test_display_text_is_idempotent_and_only_touches_marked_text():
    text = (
        "咨询电话：[电话已脱敏]、87687769；办公室：87687770。项目编号 20260001，资金 30000000 元。"
    )
    once = v1.display_text(text)
    assert v1.display_text(once) == once
    assert once == (
        # 同一句电话标签后面的号码（"办公室：87687770"）也遮（11/2 复测）；编号、金额不碰
        f"咨询电话：{MARK}、{MARK}；办公室：{MARK}。项目编号 20260001，资金 30000000 元。"
    )
    plain = "没有遮过的号码：87687769、87687770"
    assert v1.display_text(plain) == plain


BODY = (
    "各有关单位：请于11月20日前报送材料。\n\n"
    "六、联系方式\n1.省综合业务咨询电话：[电话已脱敏]、87687769。\n"
    "2.材料报送地址：某市某路171号。" + "申报说明。" * 60
)


def test_detail_and_reading_pages_mask_the_second_number(client, session):  # noqa: F811
    doc, att, _, version = seed(session, body=BODY)
    row = session.query(AttachmentTextSegment).filter_by(version_id=version.id, n=1).one()
    row.text = "联系电话：[电话已脱敏]/62651782"
    row.cells = [{"c": 1, "t": "[电话已脱敏]、87687770", "h": "电话"}]
    session.commit()
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert f"咨询电话：{MARK}、{MARK}。" in detail["body"]["text"]
    reading = client.get(path(doc)).json()
    joined = "".join(s["text"] for s in reading["segments"])
    assert f"咨询电话：{MARK}、{MARK}。" in joined
    attachment = client.get(path(doc, att)).json()
    first = next(s for s in attachment["segments"] if s["n"] == 1)
    assert first["text"] == f"{MARK}、{MARK}"  # 有格子的行，text 用遮过的格子拼（11/2 复测）
    assert first["cells"][0]["t"] == f"{MARK}、{MARK}"
    for page in (detail, reading, attachment):
        text = str(page)
        assert "87687769" not in text and "62651782" not in text and "87687770" not in text


def test_text_for_the_model_masks_like_the_page_with_store_marks(session):
    """交给模型、核对 AI 原句用的文字（content.load）：入库脱敏之后，按页面那套规则补遮号码，标记写成
    入库的写法（D-114 独立审阅：协议写了发送前隐去电话）；页面上的说法不进模型的文字。"""
    doc, _, _, _ = seed(session, body=BODY)
    loaded = content.load(session, doc.id)
    body = loaded.texts.sources[0].text
    assert "[电话已脱敏]、[电话已脱敏]" in body and "87687769" not in body
    assert MARK not in body
    # 入库的文字没动；11/18 起入库规则（texts.mask、redact_sensitive）也遮联系电话那一行里不带区号的
    # 号码，和显示层一个口径
    assert v1._reading_body(session, doc.id) == BODY
    second = "咨询电话：[电话已脱敏]、[电话已脱敏]。"
    assert second in texts.mask(BODY) and second in v1.redact_sensitive(BODY)


# ---- 网址（D-112，创始人 11/2 同意）：显示时换成"（这里隐去了网址）"，和隐去电话一个写法 ----
URL = "（这里隐去了网址）"


@pytest.mark.parametrize(
    "shown, expected",
    [
        # 测试记录第 1 条里的三处原文：网址外面只剩一对括号，连括号一起换
        (
            "企业登录“火炬高技术产业开发中心平台”（https://hjrz.chinatorch.org.cn/login），填写",
            f"企业登录“火炬高技术产业开发中心平台”{URL}，填写",
        ),
        (
            "服务外包信息管理应用（https://www.mofcom.gov.cn/fzlm/swbywxttypta/index.html）进行",
            f"服务外包信息管理应用{URL}进行",
        ),
        (
            "注册会计师行业统一监管平台（http://acc.mof.gov.cn）。",
            f"注册会计师行业统一监管平台{URL}。",
        ),
        # 半角括号、括号里有空格、查询参数
        ("系统(https://x.gov.cn/a?id=1&t=2)填报", f"系统{URL}填报"),
        ("系统（ https://x.gov.cn/apply ）填报", f"系统{URL}填报"),
        # www. 开头、后面紧跟汉字；大写；"http："写成全角冒号
        ("请登录www.most.gov.cn进行注册", f"请登录{URL}进行注册"),
        ("网址：HTTPS://KJT.EXAMPLE.GOV.CN/A", f"网址：{URL}"),
        ("网址：http：//www.example.gov.cn/。", f"网址：{URL}。"),
        # 网址后面紧跟标点：标点留着
        ("访问https://x.gov.cn。", f"访问{URL}。"),
        ("见https://x.gov.cn/a.html，下一句", f"见{URL}，下一句"),
        ("见 https://x.gov.cn/a.html.", f"见 {URL}."),
        ("见https://x.gov.cn/a.html;下一句", f"见{URL};下一句"),
        ("见https://x.gov.cn/a.html：下一句", f"见{URL}：下一句"),
        # 括号里还有别的字：括号留着，只换网址
        # 括号里只剩网址标记的收成一个（独立审阅）
        ("（网址：https://x.gov.cn）", URL),
        ("（详见https://x.gov.cn）", f"（详见{URL}）"),
        # 两个网址
        ("https://a.gov.cn、https://b.gov.cn", f"{URL}、{URL}"),
        # 网址被换行、空格断开（网页和 PDF 里常见）：后半截一起换，不露出来
        (
            "在江苏数字科技平台（https://kxjst.\njiangsu.gov.cn/js-home/home）提交",
            f"在江苏数字科技平台{URL}提交",
        ),
        ("管理平台”(http://xx. miit.gov.cn)报送", f"管理平台”{URL}报送"),
        (
            "请通过政务外网登录http://59.1. 238.12/gywh,在线完成",
            f"请通过政务外网登录{URL},在线完成",
        ),
        # 换行断在字母中间也接上
        ("平台（https://kxjst.ji\nangsu.gov.cn/js-home/home）上提交", f"平台{URL}上提交"),
        # 断开处后面不像网址的（下一行是"2."、"附件"、英文单词）不接
        ("见https://x.gov.cn.\n2.材料报送地址", f"见{URL}.\n2.材料报送地址"),
        ("见https://x.gov.cn/\n附件：1.申报书", f"见{URL}\n附件：1.申报书"),
        ("见https://x.gov.cn/ 2026年起", f"见{URL} 2026年起"),
        ("见https://x.gov.cn\nWord 版", f"见{URL}\nWord 版"),
        # 整个网址的标点都是全角的
        ("已在中国政府采购网（http：／／www．ccgp．gov．cn）登记", f"已在中国政府采购网{URL}登记"),
        (
            "在云南省科技厅网站http：／／kjt．yn．gov．cn进行公示。",
            f"在云南省科技厅网站{URL}进行公示。",
        ),
        # 一行里还有已遮的电话：两样都按页面说法写
        ("电话：[电话已脱敏]、87687769，网址：www.x.gov.cn", f"电话：{MARK}、{MARK}，网址：{URL}"),
    ],
)
def test_urls_are_hidden_when_shown(shown, expected):
    assert v1.display_text(shown) == expected
    assert v1.display_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        # 文号、编号、数字
        "粤科函产字〔2026〕1737号",
        "版本 V1.0，增长 20.5%",
        # "www"在别的词里
        "wwwroot 目录",
        "awww 和 wwwx",
        "WWW规范",
        # 只有"http://"没有网址；"https"后面不是"://"；"HTTP协议"
        "以 http:// 开头",
        "采用 https 协议",
        "熟悉HTTP协议及浏览器原理",
        "以 http：／／ 开头",
    ],
)
def test_other_text_is_left_alone(text):
    assert v1.display_text(text) == text


# ---- 不带 http、www 的域名（11/2 F12）：同样换成"（这里隐去了网址）" ----
@pytest.mark.parametrize(
    "shown, expected",
    [
        ("来源 kxjst.jiangsu.gov.cn", f"来源 {URL}"),
        # 带路径的连路径一起
        ("登录 hjrz.chinatorch.org.cn/login 填报", f"登录 {URL} 填报"),
        ("网址：kjt.shandong.gov.cn/art/2026/10/1/a.html。", f"网址：{URL}。"),
        # 外面只剩一对括号：连括号一起换
        ("安徽省工业和信息化厅网站（jx.ah.gov.cn），选择", f"安徽省工业和信息化厅网站{URL}，选择"),
        ("“企业微课”平台（qiyeweike.miitec.cn）填写", f"“企业微课”平台{URL}填写"),
        # 各种后缀；后面紧跟汉字、标点
        (
            "见 china.com.cn、abc.org.cn、x.net.cn、tsinghua.edu.cn",
            f"见 {URL}",  # 连着 4 个同样的标记合成一个（独立审阅）
        ),
        ("见example.com和example.net、example.org。", f"见{URL}和{URL}、{URL}。"),
        ("全角点 kjt．yn．gov．cn 也认", f"全角点 {URL} 也认"),
        ("awww.example.com", URL),
    ],
)
def test_bare_domains_are_hidden_when_shown(shown, expected):
    assert v1.display_text(shown) == expected
    assert v1.display_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        # 附件名、表名
        "附件1.docx",
        "附件：表2.xlsx、申报书.pdf",
        # 小数、编号、缩写、版本号
        "增长 3.5 个百分点",
        "No.1 和 No.12",
        "e.g. 示例",
        "版本 V2.0、v3.1.4",
        # 文号里的点
        "鲁科字〔2026〕12.3号",
        "第1.2.3条",
        # 后缀大写的技术名词、别的后缀
        "熟悉 ASP.NET 和 .NET 框架",
        "Node.js、socket.io、Vue.js",
        # 后缀后面还接着字母数字的不是这几个后缀
        "x.cnn、y.company、z.org2",
    ],
)
def test_text_like_domains_is_left_alone(text):
    assert v1.display_text(text) == text


# ---- 邮箱（11/2 F14）：全角"＠"写的也遮成"（这里隐去了邮箱）"，和入库时遮的写法一样 ----
MAIL = "（这里隐去了邮箱）"


@pytest.mark.parametrize(
    "shown, expected",
    [
        ("电子邮箱：jxkjjcc＠kjt.jiangxi.gov.cn", f"电子邮箱：{MAIL}"),
        ("报送至指定邮箱：jjsbgs＠51kehui.com。", f"报送至指定邮箱：{MAIL}。"),
        # 邮箱标记外面的括号收掉（11/2 复测）
        ("发送至指定邮箱（kjtpgzx＠163.com）。", f"发送至指定邮箱{MAIL}。"),
        # 域名里的点写成全角的；"＠"两边有空格
        ("邮箱：cyec＠kjt．jiangxi．gov．cn", f"邮箱：{MAIL}"),
        ("邮箱：cyec ＠ kjt.jiangxi.gov.cn", f"邮箱：{MAIL}"),
        # 本地部分入库时被当成号码遮了的
        ("于10月12日前发送至[电话已脱敏]＠163.com", f"于10月12日前发送至{MAIL}"),
        ("邮箱：（这里隐去了电话）@163.com", f"邮箱：{MAIL}"),
        # 半角的（入库一般已经遮过）、域名里写错成长横线的也遮，域名不再当网址换
        ("邮箱：abc@www.example.com", f"邮箱：{MAIL}"),
        ("邮箱：kjc@most.gov.cn", f"邮箱：{MAIL}"),
        ("邮箱：chongjian@sd—ms.cn", f"邮箱：{MAIL}"),
    ],
)
def test_emails_are_hidden_when_shown(shown, expected):
    assert v1.display_text(shown) == expected
    assert v1.display_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "＠全体参赛单位",
        "使用 @ 符号",
        "Copyright @ 2022 版权所有",
        "电子邮箱：（这里隐去了邮箱）",
    ],
)
def test_other_at_signs_are_left_alone(text):
    assert v1.display_text(text) == text


URL_BODY = (
    "各有关单位：\n\n"
    "一、申报方式\n企业登录“火炬高技术产业开发中心平台”（https://hjrz.chinatorch.org.cn/login）填报。"
    "\n二、联系方式\n咨询电话：[电话已脱敏]、87687769。" + "申报说明。" * 60
)


def test_detail_reading_and_attachment_pages_hide_urls(client, session):  # noqa: F811
    doc, att, _, version = seed(session, body=URL_BODY)
    row = session.query(AttachmentTextSegment).filter_by(version_id=version.id, n=1).one()
    row.text = "填报网址：http://acc.mof.gov.cn。"
    row.cells = [{"c": 1, "t": "www.most.gov.cn", "h": "网址"}]
    session.commit()
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert f"开发中心平台”{URL}填报。" in detail["body"]["text"]
    reading = client.get(path(doc)).json()
    assert f"开发中心平台”{URL}填报。" in "".join(s["text"] for s in reading["segments"])
    attachment = client.get(path(doc, att)).json()
    first = next(s for s in attachment["segments"] if s["n"] == 1)
    assert first["text"] == URL and first["cells"][0]["t"] == URL  # 有格子的行，text 用遮过的格子拼
    for page in (detail["body"], reading["segments"], attachment["segments"]):
        assert "http" not in str(page) and "www." not in str(page)
    # 交给模型、核对原句的文字照旧带着网址；入库的文字没动
    model_text = content.load(session, doc.id).texts.sources[0].text
    assert "https://hjrz.chinatorch.org.cn/login" in model_text
    assert v1._reading_body(session, doc.id) == URL_BODY


def test_list_row_cells_hide_urls_and_masked_phones():
    """名单单行页的格子文字（/lists/record 的 cells）走同一个显示层。"""
    assert v1.display_text(v1.redact_sensitive("网址 https://x.gov.cn/a")) == f"网址 {URL}"
    shown = v1.display_text(v1.redact_sensitive("电话 0531-12345678、87687769"))
    assert shown == f"电话 {MARK}、{MARK}"


# "联系电话：86263938"这样不带区号、前面也没有已遮号码的本地号码（11/2 F15）：紧跟在"电话：""手机："
# "传真："标签后面的 7–8 位号码显示时也遮，后面紧跟着的号码照第 1 条一起遮
@pytest.mark.parametrize(
    "shown, expected",
    [
        ("联系电话：86263938。", f"联系电话：{MARK}。"),
        ("电 话：86263938", f"电 话：{MARK}"),
        ("咨询电话: 86263938", f"咨询电话: {MARK}"),
        ("传真：86263939转801", f"传真：{MARK}"),
        ("电话号码：6626393", f"电话号码：{MARK}"),
        ("联系电话：86263938、86263939", f"联系电话：{MARK}、{MARK}"),
        # 下面两条原来属于"别的号码不碰"（F5 只管紧跟在已遮号码后面的），F15 起标签后面的也遮
        ("联系电话：87687769", f"联系电话：{MARK}"),
        (f"电话：{MARK}，传真：87687770", f"电话：{MARK}，传真：{MARK}"),
    ],
)
def test_local_numbers_after_a_phone_label_are_masked(shown, expected):
    assert v1.display_text(shown) == expected


@pytest.mark.parametrize(
    "text",
    [
        "电话：20261101",  # 像日期
        "电话：86263938元",  # 金额
        "电话：8626.5",  # 小数
        "电话会议：86263938",  # 标签后面不是冒号
        "电话：见附件",
        "电话：12345",  # 位数不够
        "手机：123456789012",  # 位数太多
        "联系人：86263938",  # 不是电话标签
    ],
)
def test_other_text_after_phone_labels_is_left_alone(text):
    assert v1.display_text(text) == text


# Codex 审阅（11/2）：两段后缀的域名大写也认（"KJT.JIANGSU.GOV.CN"），一段后缀的照旧只认小写
# （"ASP.NET"不算）；全角"＠"邮箱的用户名带下划线也遮
@pytest.mark.parametrize(
    "shown, expected",
    [
        ("见 KJT.JIANGSU.GOV.CN 网站", "见 （这里隐去了网址） 网站"),
        ("见 Kjt.Jiangsu.Gov.Cn", "见 （这里隐去了网址）"),
        ("联系邮箱：abc_def＠kjt.gov.cn。", "联系邮箱：（这里隐去了邮箱）。"),
    ],
)
def test_upper_case_domains_and_underscore_emails_are_hidden(shown, expected):
    assert v1.display_text(shown) == expected


@pytest.mark.parametrize("text", ["ASP.NET 开发", "EXAMPLE.COM", "附件1.DOCX"])
def test_upper_case_text_like_domains_is_left_alone(text):
    assert v1.display_text(text) == text


# 正文里入库时漏遮的联系人姓名（Codex 审阅，11/2）：显示时按附件那套补遮，另加"电话：""联系电话："
# 标签前面隔着标点、空格的那个词；和标签连在一起的部门、事项名（"储备课题技术电话""纪检监察组联系电话"）不碰
@pytest.mark.parametrize(
    "shown, expected",
    [
        (
            f"（联系人：电力处 梁弢；联系电话：{MARK}）",
            f"（联系人：电力处 （这里隐去了联系人）；联系电话：{MARK}）",
        ),
        (f"科技处 廉瑞， 联系电话：{MARK}；", f"科技处 （这里隐去了联系人）， 联系电话：{MARK}；"),
        (f"王娟 联系电话：{MARK}", f"（这里隐去了联系人） 联系电话：{MARK}"),
        (f"电力处 梁弢。联系电话：{MARK}", f"电力处 （这里隐去了联系人）。联系电话：{MARK}"),
    ],
)
def test_body_names_before_a_phone_label_are_masked(shown, expected):
    assert v1.display_body_text(shown) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"AI赋能生物育种储备课题技术电话：{MARK}",
        f"驻科技厅纪检监察组联系电话：{MARK}",
        f"单位电话：{MARK}",
        f"科技处，电话：{MARK}",
        f"地址：河西区友谊路1号，电话：{MARK}",
        f"请按时报送。联系电话：{MARK}",
        f"本项工作由高新处负责。联系电话：{MARK}",
    ],
)
def test_body_words_that_are_not_names_are_left_alone(text):
    assert v1.display_body_text(text) == v1.display_text(text)


def test_detail_and_reading_pages_mask_body_names(client, session):  # noqa: F811
    body = BODY.replace(
        "2.材料报送地址", "3.联系人：电力处 梁弢；联系电话：[电话已脱敏]\n2.材料报送地址"
    )
    doc, _att, _, _version = seed(session, body=body)
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert "电力处 （这里隐去了联系人）；联系电话" in detail["body"]["text"]
    reading = client.get(path(doc)).json()
    joined = "".join(s["text"] for s in reading["segments"])
    assert "电力处 （这里隐去了联系人）；联系电话" in joined
    assert "梁弢" not in str(detail) and "梁弢" not in joined


# 独立审阅（11/2）：入库时漏掉的号码写法和标记的整理
CONTACT = "（这里隐去了联系人）"
URL = "（这里隐去了网址）"


@pytest.mark.parametrize(
    "shown, expected",
    [
        # 区号用破折号、带括号、隔空格
        ("联系电话：022—58832980", f"联系电话：{MARK}"),
        ("联系电话：（0591）87882899（传真）", f"联系电话：{MARK}（传真）"),
        ("电话：0591 87882899", f"电话：{MARK}"),
        ("省自然基金委：0371—65099390 省科技厅", f"省自然基金委：{MARK} 省科技厅"),
        # 写在已遮联系人后面的本地号码
        (
            f"北京市经济和信息化局 {CONTACT}：55520803，",
            f"北京市经济和信息化局 {CONTACT}：{MARK}，",
        ),
        (f"规划处 {CONTACT} 87811397 规划处", f"规划处 {CONTACT} {MARK} 规划处"),
        # 已遮号码后面隔着空格、分号的号码
        (f"联系电话：{MARK}；86263938", f"联系电话：{MARK}；{MARK}"),
        # 同一处连着 3 个以上同样的标记只留一个；两个的照旧
        (f"技术支持：{MARK}、{MARK}、{MARK}、{MARK}。", f"技术支持：{MARK}。"),
        (f"联系电话：{MARK} 3903039 3903040", f"联系电话：{MARK}"),
        (f"咨询电话：{MARK}、{MARK}。", f"咨询电话：{MARK}、{MARK}。"),
        # 括号里只剩网址标记的收成一个
        ("通过“系统”（网址：https://xm.example.gov.cn/login）申报", f"通过“系统”{URL}申报"),
        # 标签不是网址类的照留（Codex 第 4 轮）
        (f"（申报入口：{URL}）", f"（申报入口：{URL}）"),
        # 去了括号以后才连在一起的，一次就合并好（处理两次结果一样，Codex 第 4 轮）
        (f"{URL}、（网址：{URL}）、{URL}", URL),
        # 括号套了几层都一次去掉（Codex 第 5 轮）
        (f"（（{URL}））", URL),
        (f"见（网址：（（{URL}）））", f"见{URL}"),
    ],
)
def test_more_phone_forms_and_tidy_marks(shown, expected):
    assert v1.display_text(shown) == expected
    assert v1.display_text(v1.display_text(shown)) == v1.display_text(shown)


@pytest.mark.parametrize(
    "text",
    [
        "2026—2028年",
        "1999—2025",
        "编号：0123—4567890",
        "邮编：0532 2661000",
        "（0532）",
        f"{CONTACT} 20261101前报送",
        f"{CONTACT}5000000元",
    ],
)
def test_numbers_that_are_not_phones_stay(text):
    assert v1.display_text(text) == v1.display_marks(text)


def test_names_before_area_code_phones_are_masked_in_body():
    shown = f"革委工业处 常文明，0531—51783107，{MARK}"
    assert v1.display_body_text(shown) == f"革委工业处 {CONTACT}，{MARK}，{MARK}"
    assert (
        v1.display_body_text("18674 李宗仁 0451—82659737 省财政")
        == f"18674 {CONTACT} {MARK} 省财政"
    )


# 11/2 复测：电话类标签同一句里的号码、减号和国际写法、已遮号码前面的号码、标记整理
MAIL_MARK = "（这里隐去了邮箱）"


@pytest.mark.parametrize(
    "shown, expected",
    [
        ("技术支持：83124114", f"技术支持：{MARK}"),
        ("业务咨询：83124079、83124035", f"业务咨询：{MARK}、{MARK}"),
        (
            "联系电话：业务部门：市科技局科技人才科，65119071；监督部门：市科技局监督处，65233293。",
            f"联系电话：业务部门：市科技局科技人才科，{MARK}；监督部门：市科技局监督处，{MARK}。",
        ),
        ("联系方式：海口市科技局，66530123", f"联系方式：海口市科技局，{MARK}"),
        ("65119071、（这里隐去了电话）", f"{MARK}、{MARK}"),
        ("电话：0912−3261033", f"电话：{MARK}"),
        ("电话：8610-88828972", f"电话：{MARK}"),
        (f"发送至指导邮箱（{MAIL_MARK}）", f"发送至指导邮箱{MAIL_MARK}"),
        ("系统（网址：https://a.gov.cn/x i https://a.gov.cn/y）", "系统（这里隐去了网址）"),
    ],
)
def test_more_phone_contexts_and_marks(shown, expected):
    assert v1.display_text(shown) == expected
    assert v1.display_text(v1.display_text(shown)) == v1.display_text(shown)


@pytest.mark.parametrize(
    "text",
    [
        "电话会议：86263938",
        "咨询：请于2026年11月20日前报送",
        "项目编号：20261234",
        "咨询电话：8008205114",  # 400/800 是单位的服务热线，10 位，不遮
        "申报金额：50000000元",
    ],
)
def test_text_near_phone_words_that_is_not_a_phone_stays(text):
    assert v1.display_text(text) == text


def test_phone_columns_and_sure_names_in_attachment_rows():
    rows = v1.display_attachment_row(
        [("序号", "1"), ("单位", "滨海新区工信局"), ("联系人", "吴老师"), ("联系电话", "63001576")]
    )
    assert rows == ["1", "滨海新区工信局", "（这里隐去了联系人）", MARK]
    # 少见姓：姓名列整格是名字也遮；"科员"（职务）、"韩语"不当姓名
    assert v1.display_attachment_row([("姓名", "和文丹"), ("职务", "科员"), ("电话", MARK)]) == [
        "（这里隐去了联系人）",
        "科员",
        MARK,
    ]
    assert v1.display_attachment_row([("姓名", "韩语"), ("语种", "韩语")]) == ["韩语", "韩语"]
    # 空白表格的栏目名：上一格写着"姓名"不算姓名列；姓名列里的栏目名也不遮（11/2 独立审阅）
    blank = [("列1", "姓名"), ("列2", "性别"), ("列3", "年龄"), ("列4", "学历"), ("列5", "序号")]
    assert v1.display_attachment_row(blank) == ["姓名", "性别", "年龄", "学历", "序号"]
    assert v1.display_attachment_row([("姓名", "学历"), ("姓名", "民族")]) == ["学历", "民族"]


@pytest.mark.parametrize(
    "shown, expected",
    [
        (f"联系人：丘珞珈，电话：{MARK}", f"联系人：（这里隐去了联系人），电话：{MARK}"),
        (
            "产业发展促进中心吴艳丽、（这里隐去了联系人）",
            "产业发展促进中心（这里隐去了联系人）、（这里隐去了联系人）",
        ),
        # 从原文起步：后一个名字在同一行里才遮成标记，前一个也要遮到（11/2 独立审阅）
        (
            "产业发展促进中心吴艳丽、吴丹，55521164",
            "产业发展促进中心（这里隐去了联系人）、（这里隐去了联系人），（这里隐去了电话）",
        ),
        ("联系人：暂无", "联系人：暂无"),
        ("联系人：本单位", "联系人：本单位"),
        # 空白表格的栏目名、"社会发展"不当名字；"中国科协高博"只遮后两个字
        (
            "单位负责人： 统计负责人： 填表人： 联系电话：",
            "单位负责人： 统计负责人： 填表人： 联系电话：",
        ),
        ("部门： 负责人： 日期：", "部门： 负责人： 日期："),
        ("社会发展，（这里隐去了联系人），", "社会发展，（这里隐去了联系人），"),
        (
            "联系人：中国科协高博、（这里隐去了联系人）",
            "联系人：中国科协（这里隐去了联系人）、（这里隐去了联系人）",
        ),
    ],
)
def test_sure_names_after_labels_and_before_masked_contacts(shown, expected):
    assert v1.display_body_text(shown) == expected
    assert v1.display_attachment_text(shown) == expected


def test_scripts_in_attachment_names_are_cut():
    name = "附件2.联系人信息表.xlsdocument.getElementById('Appendix').style.display='block';"
    assert v1.display_attachment_name(name) == "附件2.联系人信息表.xls"
    assert v1.display_attachment_name("附件1：申报书.docx") == "附件1：申报书.docx"


# Codex 第 7 轮：国家码写法的编号、金额和 400/800 不遮；服务热线、值班列也是电话列；按整句判断
@pytest.mark.parametrize(
    "text",
    [
        "项目编号：8610-88828972",
        "金额：8610-88828972元",
        "服务电话：+86 400-8205114",
        "咨询电话：400-820-5114",
        "电话：800 8205114",
    ],
)
def test_intl_like_codes_amounts_and_hotlines_stay(text):
    assert v1.display_text(text) == text


def test_hotline_and_duty_columns_are_phone_columns():
    rows = v1.display_attachment_row(
        [("单位", "某局"), ("服务热线", "63001576"), ("值班电话", "63001577")]
    )
    assert rows == ["某局", MARK, MARK]


def test_phone_context_runs_to_the_end_of_the_sentence():
    long_tail = "，".join(f"第{i}项事务说明" for i in range(12))
    shown = f"咨询电话：{long_tail}，65119071？下一句编号 66001234"
    out = v1.display_text(shown)
    assert "65119071" not in out and "66001234" in out


# ---- 11/3：正文里还漏着的几种号码写法（只在电话语境里认）----
P = "（这里隐去了电话）"
C = "（这里隐去了联系人）"


@pytest.mark.parametrize(
    "shown, expected",
    [
        # 已遮号码的下一行接着写的（含分机）
        (
            f"联系电话：{P}转8002\n4682138转8002\n邮箱：（这里隐去了邮箱）",
            f"联系电话：{P}转8002\n{P}\n邮箱：（这里隐去了邮箱）",
        ),
        # 单位名后面隔着冒号、逗号、空格
        (
            "（产业政策处：51782598，轻工纺织产业处：51782822）",
            f"（产业政策处：{P}，轻工纺织产业处：{P}）",
        ),
        ("业务部门：某局某科，65227947", f"业务部门：某局某科，{P}"),
        # "联系方式"标题下面几行
        (
            "联系方式：\n资源配置与管理处 67515789\n机关纪委 67600056\n联系地址：某市某路1号",
            f"联系方式：\n资源配置与管理处 {P}\n机关纪委 {P}\n联系地址：某市某路1号",
        ),
        (
            "联系电话：\n某某项目\n65337138\n某某专项\n65300221",
            f"联系电话：\n某某项目\n{P}\n某某专项\n{P}",
        ),
        # 人名后面的号码：括号连号码一起换，名字也遮；分行写的要名字单独一行、上一行是单位
        ("请电询王小明（55521177）或李小红（55520811）。", f"请电询{C}{P}或{C}{P}。"),
        ("节能处\n刘小明\n55520947\n申报平台", f"节能处\n{C}\n{P}\n申报平台"),
        # "致电"后面同一句的
        ("请有意向的企业提前致电86168249咨询。", f"请有意向的企业提前致电{P}咨询。"),
        # 区号连写的 11–12 位（以前"已遮号码、01087687769"这样的也不遮，11/3 起遮）
        ("联系电话：05388933778", f"联系电话：{P}"),
        (f"咨询电话：{P}、01087687769", f"咨询电话：{P}、{P}"),
        # 一行只有一串号码
        ("88827981、88828830", f"{P}、{P}"),
    ],
)
def test_more_phone_contexts(shown, expected):
    assert v1.display_body_text(shown) == expected
    assert v1.display_body_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2060502技术创新服务 200.00 200.00",  # 预算科目编码
        "网站访问总量（单位：次）\n7206482",  # 访问量
        "吉林省某某设计院 91220101 MA0Y5F26",  # 统一社会信用代码被空格拆开
        "笔试成绩\n面试成绩\n最终成绩\n3000129",  # 成绩表
        "项目编号：2026001，金额：3000000元，截至20261120。",  # 编号、金额（以前把"金额"当成人名）
        "支出列“2060203自然科学基金”",
        "某某项目\n65337138",  # 不在联系方式标题下面
        # agy 审阅举的例子：已遮号码下一行开头的科目编码、像标题的整句、常用词后面的数、一串 7 位编码
        "（一）联系电话保持畅通，请勿关机\n某某 85213456",
        "（一）联系电话：保持畅通\n某某 85213456",
        "成绩 85213456",
        "金额（2060502）",
        "2060501、2060502",
    ],
)
def test_more_phone_rules_leave_other_numbers(text):
    assert v1.display_body_text(text) == text


def test_next_line_number_must_stand_alone():
    # 隔着换行接着写的号码，下一行只有号码才算（agy 审阅）
    assert (
        v1.display_body_text("电话：88888888\n2060502技术创新服务")
        == f"电话：{P}\n2060502技术创新服务"
    )
    # Windows 换行（\r\n）也认：交给模型的文字没有先统一换行
    raw = "电话：010-88888888\r\n87687769\r\n"
    assert v1.model_body_text(v1.redact_sensitive(raw)) == "电话：[电话已脱敏]\r\n[电话已脱敏]\r\n"
    # 标题行冒号后面本身列着号码的照样当标题
    text = f"联系电话：某项目 {P}\n另一项目 55520751\n联系邮箱：某"
    assert v1.display_body_text(text) == f"联系电话：某项目 {P}\n另一项目 {P}\n联系邮箱：某"


def test_phone_label_cell_masks_the_next_cell():
    row = [("列1", C), ("列2", "联系方式:"), ("列3", "05922182100")]
    assert v1.display_attachment_row(row) == [C, "联系方式:", P]
    assert v1.display_attachment_row([("列1", "申报总额"), ("列2", "30000000")]) == [
        "申报总额",
        "30000000",
    ]


def test_more_phone_rules_reach_the_model_text():
    # 交给模型的文字走同一套规则，标记写成入库的写法
    assert v1.model_body_text("产业政策处：51782598") == "产业政策处：[电话已脱敏]"


# 独立审阅第 1 轮补的几种（11/3）
@pytest.mark.parametrize(
    "shown, expected",
    [
        ("某某处\n55520803\n下一行", f"某某处\n{P}\n下一行"),  # 单位名下一行单独写的
        ("电话：0531-8888 8888", f"电话：{P}"),  # 区号后面本地号码中间隔空格
        ("传真：0531 8888 8888", f"传真：{P}"),
        (f"联系人：{C}/55520803", f"联系人：{C}/{P}"),  # 已遮联系人后面斜杠隔开
        (f"{C}，1381234567", f"{C}，{P}"),  # 已遮联系人后面 10 位的手机号（原文少一位）
    ],
)
def test_more_phone_contexts_round_two(shown, expected):
    assert v1.display_body_text(shown) == expected


@pytest.mark.parametrize(
    "text",
    [
        "成交额 32345678 元",  # 号码后面隔空格接"元"
        "资金 30000000 元。",
        "某局，2060502技术创新服务",  # 单位名后面的数紧接汉字，是编码
        "某某研究院\n3000000",  # 单位名下一行的 7 位数
    ],
)
def test_more_phone_rules_round_two_leave_other_numbers(text):
    assert v1.display_body_text(text) == text


def test_lone_number_cells_in_contact_tables():
    # 没写列名的通讯录表：左边一格是单位名（可带括号），这一格只有一个 8 位号码
    assert v1.display_attachment_row(
        [("", "1"), ("", "中关村某某园"), ("", "88888888"), ("", "某区某路9号")]
    ) == [
        "1",
        "中关村某某园",
        P,
        "某区某路9号",
    ]
    assert v1.display_attachment_row([("", "某某委员会（某区）"), ("", "88888888")]) == [
        "某某委员会（某区）",
        P,
    ]
    # 同一行已经有遮掉的联系人
    assert v1.display_attachment_row(
        [("单位", "某某局"), ("联系人", "张小明"), ("列3", "88888888")]
    ) == [
        "某某局",
        C,
        P,
    ]
    # 列名是金额的、左边不是单位名的不遮
    assert v1.display_attachment_row([("", "某某有限公司"), ("申请金额（元）", "30000000")]) == [
        "某某有限公司",
        "30000000",
    ]
    assert v1.display_attachment_row([("", "某某项目"), ("", "30000000")]) == [
        "某某项目",
        "30000000",
    ]


def test_space_before_a_word_after_the_number_still_masks():
    # 隔着空格只认钱和百分比："电话：号码 人事处"照样遮（11/3 独立审阅第 2 轮）
    assert v1.display_body_text("电话：88888888 人事处") == f"电话：{P} 人事处"
    assert v1.display_body_text(f"{C} 88888888 项目办") == f"{C} {P} 项目办"


def test_contact_table_cell_with_extensions_and_company_amounts():
    row = [("", "4"), ("", "某某委员会"), ("", "88888888/801/802"), ("", "某路")]
    assert v1.display_attachment_row(row) == ["4", "某某委员会", P, "某路"]
    # 左边是公司的不当通讯录（多是金额表）
    assert v1.display_attachment_row([("", "某某有限公司"), ("", "50000000")]) == [
        "某某有限公司",
        "50000000",
    ]


def test_common_words_on_a_phone_line_are_not_names():
    # 11/18：号码遮了以后，这一行按电话行把像姓名的词都遮掉；"查阅"这类普通词不能当成姓名
    shown = v1._clean_reading_body("操作指南可在网站查阅，技术支持：0123-87654321。")
    assert shown == f"操作指南可在网站查阅，技术支持：{MARK}。"


EMAIL_MARK = "（这里隐去了邮箱）"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("材料发到test＠foo－bar.com。", "材料发到[邮箱已脱敏]。"),
        ("材料发到test@foo—bar.com。", "材料发到[邮箱已脱敏]。"),
        ("材料发到abcdef @126.com。", "材料发到[邮箱已脱敏]。"),
        # 手机号当邮箱名的：入库照旧先遮成手机号标记（同一行的姓名按电话行遮），页面上并成邮箱；这次
        # 新认写法的数字用户名邮箱同样只遮用户名（QQ 号的标记，隐私合规那位第八轮）
        ("材料发到13812345678@163.com。", "材料发到[手机号已脱敏]@163.com。"),
        ("材料发到87654321＠qq.com。", "材料发到[QQ号已脱敏]＠qq.com。"),
        ("材料发到87654321 @qq.com。", "材料发到[QQ号已脱敏] @qq.com。"),
    ],
)
def test_emails_masked_alike_by_store_and_page(raw: str, stored: str) -> None:
    # 11/18 Codex 第三轮：域名里的全角横线、破折号，"@"前面隔一个空格，入库规则和页面一样认
    assert v1.redact_sensitive(raw) == stored
    assert v1.display_body_text(stored) == f"材料发到{EMAIL_MARK}。"
    assert v1.display_body_text(raw) == f"材料发到{EMAIL_MARK}。"
    assert v1.model_body_text(stored) == "材料发到[邮箱已脱敏]。"


def test_name_on_an_email_line_with_a_mobile_name():
    # 11/18 独立 Claude 确认轮：手机号当邮箱名的那一行，详情页、交给模型的文字照旧遮姓名（和主干一样）
    raw = "高新处 张某，邮箱：13812345678@163.com。"
    assert v1.display_body_text(v1._clean_reading_body(raw)) == (
        f"高新处 {CONTACT}，邮箱：{EMAIL_MARK}。"
    )
    assert (
        v1.model_body_text(v1.redact_sensitive(raw)) == "高新处 [姓名已脱敏]，邮箱：[邮箱已脱敏]。"
    )


QQ_MARK = "（这里隐去了QQ号）"


@pytest.mark.parametrize(
    ("raw", "page", "model"),
    [
        # 11/18 独立 Claude 确认轮：分机后面紧跟"-"和 7–8 位的是下一个号码；分机后面接着姓名
        (
            "联系人：张某 0123-87654321-0123-87654322",
            f"联系人：{CONTACT} {MARK}-{MARK}",
            "联系人：[姓名已脱敏] [电话已脱敏]-[电话已脱敏]",
        ),
        (
            "联系电话：65432109转801张某",
            f"联系电话：{MARK}{CONTACT}",
            "联系电话：[电话已脱敏][姓名已脱敏]",
        ),
        # "QQ号："不截断前面的电话标签
        (
            "联系电话：0123-87654321（QQ号：123456789）、65432109",
            f"联系电话：{MARK}（QQ号：{QQ_MARK}）、{MARK}",
            "联系电话：[电话已脱敏]（QQ号：[QQ号已脱敏]）、[电话已脱敏]",
        ),
        # QQ 群号的另两种写法
        ("培训QQ群：群号123456789", f"培训QQ群：群号{QQ_MARK}", "培训QQ群：群号[QQ号已脱敏]"),
        (
            "交流QQ群，群号为：123456789",
            f"交流QQ群，群号为：{QQ_MARK}",
            "交流QQ群，群号为：[QQ号已脱敏]",
        ),
        # 独立 Claude 第五轮：带分机的日期；新加写法里"00"开头的区号；新加的姓开头的常见词
        (
            "联系电话：0123-87654321，受理时间：20231108-1",
            f"联系电话：{MARK}，受理时间：20231108-1",
            "联系电话：[电话已脱敏]，受理时间：20231108-1",
        ),
        ("批次 0001—1234567", "批次 0001—1234567", "批次 0001—1234567"),
        ("暴雨，电话：0123-87654321", f"暴雨，电话：{MARK}", "暴雨，电话：[电话已脱敏]"),
        ("索取 13812345678", f"索取 {MARK}", "索取 [电话已脱敏]"),
        # 独立 Claude 第五轮：号码当邮箱名、写成"＠"或"@"两边带空格的，前面的姓名照样遮（正文、附件）
        (
            "业务咨询：王某，87654321＠qq.com",
            f"业务咨询：{CONTACT}，{EMAIL_MARK}",
            "业务咨询：[姓名已脱敏]，[邮箱已脱敏]",
        ),
        ("王某 13856785678＠163.com", f"{CONTACT} {EMAIL_MARK}", "[姓名已脱敏] [邮箱已脱敏]"),
    ],
)
def test_review_cases_on_every_path(raw: str, page: str, model: str) -> None:
    assert v1.display_body_text(v1._clean_reading_body(raw)) == page  # 详情页
    assert v1.display_body_text(v1._clean_reading_body(raw, redact=texts.mask)) == page  # 阅读页
    assert v1.display_attachment_text(texts.mask(raw)) == page  # 附件
    assert v1.model_body_text(v1.redact_sensitive(raw)) == model  # 交给模型


@pytest.mark.parametrize(
    ("raw", "page"),
    [
        # 独立 Claude 第二轮：区号后面 9–16 位的编号、400/800 热线的后半截，入库、页面都不当电话
        ("受理号：0123-1234567890", "受理号：0123-1234567890"),
        ("申报批次 0001-123456789", "申报批次 0001-123456789"),
        ("联系电话：65432109，0123-202311080001", f"联系电话：{MARK}，0123-202311080001"),
        ("咨询电话：400—2345678", "咨询电话：400—2345678"),
        ("联系电话：65432109、400—2345678", f"联系电话：{MARK}、400—2345678"),
        # 原网页写错、紧跟在电话类标签后面的长号码照样遮
        ("技术支持电话：0123-8123456781234567", f"技术支持电话：{MARK}"),
    ],
)
def test_long_codes_and_hotlines_on_store_and_page(raw: str, page: str) -> None:
    assert v1.display_body_text(v1._clean_reading_body(raw, redact=texts.mask)) == page
    assert v1.display_body_text(v1.redact_sensitive(raw)) == page


def test_qq_mark_on_the_page_for_the_model_and_in_ai_quotes():
    # 11/18 起入库遮 QQ 号：页面上写"（这里隐去了QQ号）"；交给模型的写回入库的标记，处理两次一样；
    # AI 引用落在这一句上，照旧按"原句里有已脱敏的内容"不用
    stored = v1.redact_sensitive("技术咨询QQ群：123456789、987654321。\n请于6月10日前报送。")
    assert stored == "技术咨询QQ群：[QQ号已脱敏]、[QQ号已脱敏]。\n请于6月10日前报送。"
    assert v1.display_body_text(stored) == (
        "技术咨询QQ群：（这里隐去了QQ号）、（这里隐去了QQ号）。\n请于6月10日前报送。"
    )
    model = v1.model_body_text(stored)
    assert model == stored and v1.model_body_text(model) == model
    loaded = content.Texts.from_paragraphs(interpret.split_paragraphs(model))
    assert content.locate(loaded, "P001", "技术咨询QQ群") == (None, "redacted")
    assert content.locate(loaded, "P002", "请于6月10日前报送")[1] is None


# 11/18 独立 Claude 第六轮：入库先把号码换成了标记（电话、手机号、QQ 号、用户名是一串数字的邮箱），
# 页面上按原始数字遮姓名的规则认不到。每种号码写法 × 每条"号码旁边的姓名"规则，详情页、阅读页、
# 附件、交给模型四条路都要把姓名和号码遮掉（字母用户名的邮箱不当号码，见下一条）
_NUMBERS = {
    "座机": ("0123-65432109", "65432109"),
    "本地号码": ("65432109", "65432109"),
    "手机号": ("13812345678", "13812345678"),
    "QQ号": ("QQ：123456789", "123456789"),
    "号码当邮箱名": ("87654321＠qq.com", "87654321"),
    "手机号当邮箱名": ("13812345678＠qq.com", "13812345678"),
}
_NAME_RULES = {
    "隔逗号": "王某，{n}",
    "隔顿号": "王某、{n}",
    "隔分号": "王某；{n}",
    "隔斜杠": "王某/{n}",
    "隔空格": "王某 {n}",
    "致电": "请致电王老师 {n}",
    "整行": "王某，{n}，手机同号",
}


@pytest.mark.parametrize("number", list(_NUMBERS))
@pytest.mark.parametrize("rule", list(_NAME_RULES))
def test_names_next_to_masked_numbers_on_every_path(number: str, rule: str) -> None:
    written, digits = _NUMBERS[number]
    raw = _NAME_RULES[rule].format(n=written)
    for where, out in (
        ("详情页", v1.display_body_text(v1._clean_reading_body(raw))),
        ("阅读页", v1.display_body_text(v1._clean_reading_body(raw, redact=texts.mask))),
        ("附件", v1.display_attachment_text(texts.mask(raw))),
        ("交给模型", v1.model_body_text(v1.redact_sensitive(raw))),
    ):
        assert "王某" not in out and "王老师" not in out and digits not in out, (where, out)


@pytest.mark.parametrize(
    "text",
    [
        # 只写着邮箱的一句话不算电话行，里面的"申请表""盖章版"这类词不当姓名；长词的尾巴不当人名
        "请把申请表（附件2）发到邮箱abc@qq.com。",
        "请示（盖章版）、证书（盖章版）发到邮箱abc@qq.com，纸质版寄某某处。",
        "测试应用场景资源 0123-65432109",
    ],
)
def test_masked_numbers_do_not_turn_common_words_into_names(text: str) -> None:
    shown = v1.display_body_text(v1._clean_reading_body(text))
    assert "联系人" not in shown, shown


@pytest.mark.parametrize(
    "raw",
    [
        "简历：hr@abc.com",
        "申报书：abc@163.com",
        # 隔空格、同一行还写着"手机"的：页面上"号码前的人名""整行有号码"两条规则也不认邮箱标记
        "简历 hr@abc.com",
        "申报书，abc@163.com，手机同号",
    ],
)
def test_word_before_a_letter_email_is_not_a_name(raw: str) -> None:
    # 正确性那位第六轮：字母用户名的邮箱不当号码线索，前面的词哪一层都不当姓名
    for where, out in (
        ("详情页", v1.display_body_text(v1._clean_reading_body(raw))),
        ("阅读页", v1.display_body_text(v1._clean_reading_body(raw, redact=texts.mask))),
        ("附件", v1.display_attachment_text(texts.mask(raw))),
        ("交给模型", v1.model_body_text(v1.redact_sensitive(raw))),
    ):
        assert out.startswith(raw[:2]) and "联系人" not in out, (where, out)


@pytest.mark.parametrize(
    "raw",
    [
        # 独立 Claude 第七轮：这次入库新遮的号码前面粘着别的字的人名，主干页面按原始数字遮了（规则
        # ③），现在入库照同一个函数遮；四条路都不能露名字、号码和区号
        "联系人：某某大学张老师 87654321",
        "详询张老师 87654321",
        "详询王某 87654321＠qq.com",
        "王某 邮件：87654321＠qq.com",
        "如有疑问，可咨询某某大学张老师 87654321。",
        "报名请联系某某学院李老师 87654321",
        "有关事宜请与某某厂办王先生 87654321联系",
        "详询王某 123456789＠qq.com",
        "王某，电子邮件：87654321＠qq.com",
        "王某 电子邮件：87654321 @qq.com",
        "联系电话：李某-0123 87654321",
        "电话：Tel.0123 87654321",
        # 正确性那位第八轮：单位名后面连写的人名，靠新遮的本地号码按规则 ③ 补遮（入库的临时标记）
        "联系人：某某单位王小明 87654321",
    ],
)
def test_names_before_newly_masked_numbers_on_every_path(raw: str) -> None:
    for where, out in (
        ("详情页", v1.display_body_text(v1._clean_reading_body(raw))),
        ("阅读页", v1.display_body_text(v1._clean_reading_body(raw, redact=texts.mask))),
        ("附件", v1.display_attachment_text(texts.mask(raw))),
        ("交给模型", v1.model_body_text(v1.redact_sensitive(raw))),
    ):
        assert not re.search(r"张老师|李老师|王先生|王某|李某|\d{4,}", out), (where, out)


def _on_every_path(raw: str) -> list[tuple[str, str]]:
    """一段原文在详情页、阅读页、附件、交给模型（model_text）四条路上的写法。"""
    return [
        ("详情页", v1.display_body_text(v1._clean_reading_body(raw))),
        ("阅读页", v1.display_body_text(v1._clean_reading_body(raw, redact=texts.mask))),
        ("附件", v1.display_attachment_text(texts.mask(raw))),
        ("交给模型", model_text(raw)),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        # 正确性那位第七轮：入库先遮了号码旁边的姓名，页面往前看被联系人标记挡住。标记当姓名看，跳过
        # 它接着往前看（附件那两条、规则 ③）；"联系张三主任"入库认"姓名+职务"
        "省科技厅 张三处长 李某，0123-65432109",
        "省科技厅 张三处长 李某，电话：0123-65432109",
        "请联系张三主任 李某 （0123）65432109",
        "咨询：刘老师，李某/65432109",
        "刘老师  李某/65432109",
        "刘老师 李某（65432109",
        "咨询：张 三 李某/65432109",
        "刘老师、李某/0123-65432109",
        "刘老师、李某/13812345678",
    ],
)
def test_names_behind_masked_contacts_on_every_path(raw: str) -> None:
    for where, out in _on_every_path(raw):
        assert not re.search(r"张三|张 三|李某|刘老师|处长|主任|\d{5,}", out), (where, out)


def test_skipping_masked_contacts_does_not_cut_a_long_unit_name() -> None:
    # 跳过联系人标记往前看的词要是整个词：长单位名的后几个字不当词，"联系人："那一段不从词中间截开
    # （11/19 全部正文对比看到过长单位名被截开、后半截前面的字当成人名遮掉）
    raw = "联 系 人：某某局高原测试数据服务站 张某、李某联系电话：0123-65432109"
    for where, out in _on_every_path(raw):
        assert "某某局高原测试数据服务站" in out and "张某" not in out and "李某" not in out, (
            where,
            out,
        )


def test_rows_under_a_contact_label_go_on_past_masked_names() -> None:
    # "联系人："写在行尾、下面一行一个"姓名 电话"的一直遮：入库遮成联系人标记的那一行也算
    raw = "联系人：\n李某 0123-65432109\n王五"
    for where, out in _on_every_path(raw):
        if where != "附件":  # 附件只看标签下面那一行（见 v1 附件规则 3）
            assert "王五" not in out, (where, out)


def test_qq_marks_count_as_numbers_for_the_page_rules() -> None:
    # "标记对页面规则透明"：QQ 号入库先遮成了标记，页面上看左邻右舍的规则把它当号码看
    # 联系方式标题后面写着 QQ 号（主干按原始数字认），下面一行单独的号码照样遮
    for where, out in _on_every_path("联系方式：QQ 23456789\n65432109"):
        assert "65432109" not in out and "23456789" not in out, (where, out)
    # 紧跟在 QQ 号标记后面、写在它前面的本地号码
    assert v1.display_text("QQ：（这里隐去了QQ号）、65432109") == f"QQ：（这里隐去了QQ号）、{MARK}"
    assert v1.display_text("65432109、（这里隐去了QQ号）") == f"{MARK}、（这里隐去了QQ号）"
    # 表格一行里有遮掉的 QQ 号：单独一个号码的格子也遮
    row = v1.display_attachment_row([("QQ", "[QQ号已脱敏]"), ("备注", "65432109")])
    assert row == ["（这里隐去了QQ号）", MARK]


def test_quotes_mask_names_before_masked_numbers() -> None:
    # 原句只走显示层（shown_quote：没有整行遮姓名、附件那几条），遮成标记的号码前面的人名靠规则 ③
    # 的标记版遮；中间隔着联系人标记的跳过它接着看（正确性那位第七轮）
    assert v1.shown_quote("请致电王老师 [电话已脱敏]") == f"请致电（这里隐去了联系人） {MARK}"
    assert v1.shown_quote("请致电王老师 [QQ号已脱敏]") == (
        "请致电（这里隐去了联系人） （这里隐去了QQ号）"
    )
    assert v1.shown_quote("咨询：刘老师，[姓名已脱敏]/[电话已脱敏]") == (
        f"咨询：（这里隐去了联系人），（这里隐去了联系人）/{MARK}"
    )
    # 和标记紧挨着的：标记开头的括号当隔开的括号
    assert v1.shown_quote("刘老师[姓名已脱敏] [电话已脱敏]") == (
        f"（这里隐去了联系人）（这里隐去了联系人） {MARK}"
    )
    # 姓加称呼前面要是单位、学校名才认（正确性那位第八轮）："值班老师""相关同志"不当人名
    assert v1.shown_quote("某某大学张老师 [电话已脱敏]") == f"某某大学（这里隐去了联系人） {MARK}"
    assert v1.shown_quote("值班老师 [电话已脱敏]") == f"值班老师 {MARK}"
    assert v1.shown_quote("请联系相关同志，[电话已脱敏]") == f"请联系相关同志，{MARK}"


@pytest.mark.parametrize(
    "raw",
    [
        # 页面那套遮了号码旁边的姓名以后，入库规则再看又认出后面的号码（标记后面隔一个换行）：
        # 过到不再变为止，结果再交给它不变（正确性那位第七轮）
        "联系人：刘老师/李某\n65432109",
        "联系人：刘老师，李某\n65432109",
        "咨询：刘老师，李某/65432109",
    ],
)
def test_model_text_runs_until_nothing_changes(raw: str) -> None:
    once = model_text(raw)
    assert model_text(once) == once
    assert not re.search(r"刘老师|李某|65432109", once), once


def test_model_text_uses_both_the_store_and_the_page_rules() -> None:
    # 入库不遮、页面那套遮的（"联系电话："后面没写号码的姓名）；页面那套不管、入库遮的（证件号、
    # 数字用户名的邮箱前面隔着"电子邮件："的姓名）：交给模型的都遮
    # 页面整行遮联系人时句末的"。"会跟着姓名一起换掉（主干就这样，合并后另改），这里不把它当期望值
    assert model_text("联系电话：张某、李某。").startswith("联系电话：[姓名已脱敏]、[姓名已脱敏]")
    assert model_text("身份证号：110101199001011234") == "身份证号：[身份证号已脱敏]"
    assert model_text("王某，电子邮件：87654321＠qq.com") == "[姓名已脱敏]，电子邮件：[邮箱已脱敏]"


@pytest.mark.parametrize(
    "raw",
    [
        "党员QQ群：12345678",
        "管理员QQ：1234567",
        "简历：1234567@qq.com",
        "请联系高级工程师 0123-65432109",
    ],
)
def test_role_words_next_to_masked_numbers_are_not_names(raw: str) -> None:
    for where, out in _on_every_path(raw):
        assert out.startswith(raw[:2]) and "联系人" not in out and "姓名" not in out, (where, out)


@pytest.mark.parametrize(
    ("raw", "paths"),
    [
        # 隐私合规那位第八轮：这次新认写法的数字用户名邮箱（全角"＠"、"@"旁边带空格），主干页面上
        # 看到的是原始数字，"整行有号码""标签下一行"两条规则把它当号码、遮旁边的姓名。现在入库只把
        # 用户名遮成号码的标记，这两条照样认，最后并成邮箱。附件那条路主干只认"标签下一行"
        ("电话咨询：87654321＠qq.com（张某）", ("详情页", "阅读页", "交给模型")),
        ("张某 电话/邮箱：87654321 @qq.com", ("详情页", "阅读页", "交给模型")),
        ("联系人：\n87654321＠qq.com 张某", ("详情页", "阅读页", "附件", "交给模型")),
    ],
)
def test_names_next_to_new_digit_emails_on_every_path(raw: str, paths: tuple[str, ...]) -> None:
    for where, out in _on_every_path(raw):
        assert "87654321" not in out and "邮箱" in out, (where, out)
        if where in paths:
            assert "张某" not in out, (where, out)


@pytest.mark.parametrize(
    ("raw", "paths"),
    [
        # 隐私合规那位第八轮：主干入库就遮的"0xxx-xxxxxxx"前面是编号、会议号这类标签的照旧遮，旁边的
        # 姓名和主干一样遮（附件那条路主干只认号码前面的词）
        ("编号：0123-87654321", ()),
        ("张某 文号 0123-87654321", ("详情页", "阅读页", "附件", "交给模型")),
        ("会议码：0123-87654321（张某）", ("详情页", "阅读页", "交给模型")),
    ],
)
def test_main_style_landlines_after_code_labels_on_every_path(
    raw: str, paths: tuple[str, ...]
) -> None:
    for where, out in _on_every_path(raw):
        assert "87654321" not in out, (where, out)
        if where in paths:
            assert "张某" not in out, (where, out)


@pytest.mark.parametrize(
    "raw",
    [
        # 正确性那位第八轮：姓加称呼前面是普通词的不当姓名（上一轮放成"前面不论什么字"，多遮了），
        # 公文里常见的说法哪条路都不遮
        "如有疑问，请联系相关同志，联系电话：0123-87654321",
        "请联系有关同志 0123-87654321",
        "请联系值班老师，电话：0123-87654321",
        "值班老师 0123-87654321",
        "请联系带班老师 0123-87654321",
        "工作人员 0123-87654321",
        "值班人员：0123-87654321",
        "联系人员：0123-87654321",
        # "简历""申请表"这类身份词、文书名，号码前面、"请联系"后面都不当姓名
        "简历 12345678@qq.com",
        "简历，12345678@qq.com",
        "请将简历、12345678@qq.com",
        "申请表 12345678@qq.com",
        "申报书，12345678@qq.com",
        "请联系班主任 0123-87654321",
        "请联系管理员 0123-87654321",
        # 主干写法（半角"@"）的数字邮箱主干入库就遮成邮箱，页面上看不到号码：前面的词不按规则 ③ 认
        "相关同志 12345678@qq.com",
        "值班老师 12345678@qq.com",
    ],
)
def test_common_phrases_next_to_numbers_are_not_names(raw: str) -> None:
    for where, out in _on_every_path(raw):
        assert "联系人" not in out.replace("联系人员", "") and "姓名" not in out, (where, out)


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        # 正确性那位第八轮：经过姓名遮成标记的"姓名 电话"行往下接的，只接本身也是"姓名 电话"行或只有
        # 一两个像姓名的词的行；附件说明、正文别的行不当联系人那一行
        ("联系人：\n张三 0123-65432109\n\n附件：1. 申报书", "附件：1. 申报书"),
        ("联系人及电话：\n张三 0123-65432109\n附件：申报书", "附件：申报书"),
        (
            "联系人：\n张三，0123-65432109\n高新技术企业 黄金 周期 严格",
            "高新技术企业 黄金 周期 严格",
        ),
        ("联系人：\n张三 0123-65432109\n申请表", "申请表"),
    ],
)
def test_lines_after_masked_contact_rows_stay(raw: str, kept: str) -> None:
    for where, out in _on_every_path(raw):
        assert out.endswith(kept) and "张三" not in out, (where, out)
