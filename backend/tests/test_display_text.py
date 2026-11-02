"""只在显示时做的处理（v1.display_text）。

1. 已遮号码后面紧跟着的号码也遮掉（11/2 完整测试第 5 条）：入库规则只遮带区号的座机和手机号，
   "咨询电话：（这里隐去了电话）、87687769"这样同一处接着列的不带区号的号码漏了。只认紧跟在已遮号码
   后面、用"、""/""，""或"隔开的 7–8 位号码；金额、年份、文号、编号不碰。
2. 网址换成"（这里隐去了网址）"（D-112，创始人 11/2 同意）。

只改给页面的文字（详情页正文、阅读页正文和附件、名单单行页），入库的文字、交给模型和核对 AI 原句
用的文字不变。"""

import pytest

from tests.test_readers import client, path, seed  # noqa: F401 - 夹具和造数据的函数
from youju import attachment_text as texts
from youju.ai import content
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
        (f"{MARK}、87687769、87687770或88888888", f"{MARK}、{MARK}、{MARK}或{MARK}"),
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
        "联系电话：87687769",
        f"电话：{MARK}，传真：87687770",
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
        f"{MARK}、01087687769",
        # 分隔符不在名单里
        f"{MARK}；87687769",
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
        f"咨询电话：{MARK}、{MARK}；办公室：87687770。项目编号 20260001，资金 30000000 元。"
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
    assert first["text"] == f"联系电话：{MARK}/{MARK}"
    assert first["cells"][0]["t"] == f"{MARK}、{MARK}"
    for page in (detail, reading, attachment):
        text = str(page)
        assert "87687769" not in text and "62651782" not in text and "87687770" not in text


def test_text_for_the_model_and_quote_check_is_unchanged(session):
    """交给模型、核对 AI 原句用的文字（content.load：redact_sensitive 后的原文）不受显示层影响：
    原文没变、旧结果的"正文变了"判断（段落比对）也不会因为这次改动作废。"""
    doc, _, _, _ = seed(session, body=BODY)
    loaded = content.load(session, doc.id)
    body = loaded.texts.sources[0].text
    assert "[电话已脱敏]、87687769" in body
    assert MARK not in body
    # 入库的文字也没动；遮号码的入库规则（texts.mask、redact_sensitive）照旧，只有显示层多遮这一个
    assert v1._reading_body(session, doc.id) == BODY
    assert "、87687769" in texts.mask(BODY) and "、87687769" in v1.redact_sensitive(BODY)


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
        ("（网址：https://x.gov.cn）", f"（网址：{URL}）"),
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
        # "www"在别的词里、邮箱（不在这次范围）
        "wwwroot 目录",
        "awww.example.com",
        "WWW规范",
        "邮箱：abc@www.example.com",
        "邮箱：kjc@most.gov.cn",
        # 不带 http、www 的域名不在这次范围
        "来源 kjt.shandong.gov.cn",
        # 只有"http://"没有网址；"https"后面不是"://"；"HTTP协议"
        "以 http:// 开头",
        "采用 https 协议",
        "熟悉HTTP协议及浏览器原理",
        "以 http：／／ 开头",
    ],
)
def test_other_text_is_left_alone(text):
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
    assert first["text"] == f"填报网址：{URL}。" and first["cells"][0]["t"] == URL
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
