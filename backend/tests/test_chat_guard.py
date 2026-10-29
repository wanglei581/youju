"""问一问提问的本机规则：拦号码（第九节第 2 条）、带去查名单（第六节第 2 条）。

两个技能（问这份通知、找通知）调同一个函数，下面每条对两个技能都成立。"""

import pytest

from youju.chat_guard import HIDDEN_NUMBER, goes_to_lists, hide_numbers, sensitive
from youju.crawler.detail import _luhn, has_personal_numbers
from youju.lists.rules import normalize_credit_code

# 全是数字、校验位也对的信用代码，恰好又能过 Luhn
DIGIT_CREDIT_CODE = "913502001000000419"


@pytest.mark.parametrize(
    "text",
    [
        "91440300MA5ABCDE12",
        "我司信用代码是91440300MA5ABCDE12，能报吗",
        "91440300ma5abcde12",  # 小写
        "９１４４０３００ＭＡ５ＡＢＣＤＥ１２",  # 全角
        "代码：91440300MA5ABCDE13",  # 校验位不对也算（只看格式）
        DIGIT_CREDIT_CODE,
        f"{DIGIT_CREDIT_CODE[:-1]}8",  # 全是数字、抄错一位
        "52440300MJL1234567",
    ],
)
def test_credit_code(text):
    assert sensitive(text) == "credit_code"


def test_all_digit_credit_code_is_not_a_bank_card():
    """老的信用代码有全是数字的，过 Luhn 的会命中银行卡规则：先认信用代码，提示才不会写错。"""
    assert normalize_credit_code(DIGIT_CREDIT_CODE) == DIGIT_CREDIT_CODE
    assert _luhn(DIGIT_CREDIT_CODE) and has_personal_numbers(DIGIT_CREDIT_CODE)
    assert sensitive(DIGIT_CREDIT_CODE) == "credit_code"
    assert sensitive(f"信用代码{DIGIT_CREDIT_CODE}在不在名单里") == "credit_code"


@pytest.mark.parametrize(
    "text",
    [
        "X91440300MA5ABCDE12",  # 前面连着字母
        "91440300MA5ABCDE123",  # 后面连着数字（19 位）
        "91A40300MA5ABCDE12",  # 第 3–8 位要是数字
        "91440300MA5ABCDEI2",  # 第 9–18 位没有 I、O、S、V、Z
        "9I440300MA5ABCDE12",  # 第 1、2 位也没有
        "91440300MA5ABCD",  # 不够 18 位
    ],
)
def test_not_credit_code(text):
    assert sensitive(text) is None


def test_credit_code_reported_before_other_numbers():
    assert sensitive("91440300MA5ABCDE12，电话 13812345678") == "credit_code"


@pytest.mark.parametrize(
    "text",
    [
        "13812345678",
        "我的电话13812345678。",
        "138 1234 5678",
        "138-1234-5678",
        "+86 138 1234 5678",
        "8613812345678",
        "１３８１２３４５６７８",  # 全角
        "a13812345678b",
    ],
)
def test_mobile(text):
    assert sensitive(text) == "mobile"


@pytest.mark.parametrize(
    "text",
    [
        "11010519491231002X",
        "身份证11010519491231002x号",  # 小写 x
        "110105194912310021",  # 校验位不对也算（同 _ID_CARD）
        "110105 19491231 002X",  # 中间有空格
        "110105491231002",  # 15 位老号码
    ],
)
def test_id_card(text):
    """18 位身份证号的每一位都在信用代码的字符表里：符合身份证号格式的按身份证号算。"""
    assert sensitive(text) == "id_card"


@pytest.mark.parametrize(
    "text",
    [
        "4111111111111111",
        "卡号 4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "6222600260001072444",  # 19 位
        "账号：622202123456",  # "账号"后面 12 位，不看 Luhn
    ],
)
def test_bank(text):
    assert sensitive(text) == "bank"


@pytest.mark.parametrize(
    "text",
    [
        "专精特新申报什么时候截止",
        "川经信办函〔2026〕356号要交什么材料",
        "2026年第3批高新技术企业名单",
        "补贴最高500万元，研发费用占比3%",
        "咨询电话0755-12345678",  # 固定电话不拦
        "4111111111111112",  # 不过 Luhn
        "",
    ],
)
def test_no_numbers(text):
    assert sensitive(text) is None


COMPANY_WORDS = [
    "我们公司",
    "我公司",
    "我司",
    "本公司",
    "我们企业",
    "我们单位",
    "我单位",
    "咱们公司",
    "我们厂",
    "这家公司",
    "这家企业",
    "华为技术有限公司",
    "某某科技有限责任公司",
    "某某股份有限公司",
]
LIST_WORDS = ["名单", "名录", "榜"]
ASK_WORDS = ["在不在", "有没有", "是不是", "上没上", "上过", "进过", "入选", "列入"]


@pytest.mark.parametrize("company", COMPANY_WORDS)
def test_lists_each_company_word(company):
    assert goes_to_lists(f"{company}在不在专精特新名单里")
    assert not goes_to_lists(f"{company}能不能申报专精特新")


@pytest.mark.parametrize("word", LIST_WORDS)
def test_lists_each_list_word(word):
    assert goes_to_lists(f"我们公司有没有在今年的{word}上")
    assert not goes_to_lists(f"我们公司{word}的公示期多长")


@pytest.mark.parametrize("word", ASK_WORDS)
def test_lists_each_ask_word(word):
    assert goes_to_lists(f"我们公司{word}专精特新名单")
    assert not goes_to_lists(f"我们公司{word}专精特新")


@pytest.mark.parametrize(
    "text",
    [
        "华为技术有限公司在不在专精特新名单里",
        "我们公司去年是不是上过省里的什么名单",
        "我们 公司\n在不在\N{IDEOGRAPHIC SPACE}名单里",  # 空白、全角空格都去掉
        "华为技术有限公司（深圳）上没上榜",
    ],
)
def test_goes_to_lists(text):
    assert goes_to_lists(text)


@pytest.mark.parametrize(
    "text",
    [
        "找江苏今年的专精特新名单",  # 找名单类的通知
        "入选名单的企业有哪些支持",  # 在问这份通知
        "我们公司去年才在本市注册，能不能报",
        "华为在不在专精特新名单里",  # 没有企业全称，认不出就不带走
        "我们公司的名单什么时候公布",  # 没问在不在
        "",
    ],
)
def test_not_goes_to_lists(text):
    assert not goes_to_lists(text)


# ---- 隐去数字（第九节第 2 条，问这份通知交给模型之前） ----

H = HIDDEN_NUMBER


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        # 设计稿第九节第 2 条的单测
        (
            "我司去年研发投入500万，研发人员15人，能报吗",
            f"我司去年研发投入{H}万，研发人员{H}人，能报吗",
        ),
        ("第500万元", f"第{H}万元"),
        ("500号客户", f"{H}号客户"),
        ("3 个月", f"{H} 个月"),
        ("3.5 万元", f"{H} 万元"),
        ("1,000 万", f"{H} 万"),
        ("近 3 年", f"近 {H} 年"),
        ("2026 年", f"{H} 年"),  # 只写到年、年月的不算日期
        ("2026 年 11 月", f"{H} 年 {H} 月"),
        # 全角数字也认得出，前后的字一个不改
        ("５００万，１５人", f"{H}万，{H}人"),
        ("第3、4句", f"第{H}、{H}句"),  # 只认"第"+数字+句、条、款、项、段、点
        ("截止，2026.11.20", f"截止，{H}"),  # 用点连的不算日期
        ("2026-11/20", f"{H}-{H}/{H}"),  # 要用同一个"-"或"/"连
        ("2026-11-201", f"{H}-{H}-{H}"),  # 头尾要是整串数字的头尾
        ("12026年11月20日", f"{H}年11月20日"),  # 年要正好 4 位；后面的"11月20日"照样是日期
        ("½", H),  # 一个字规范化以后有两串数字：整个字换掉
        ("½5", H),  # 后面连着的数字也换掉，不漏
        # 用"-""/"连的日期前后又接着"-""/"和数字：是编号，整个换
        ("合同号2026-11-20-01", f"合同号{H}-{H}-{H}-{H}"),
        ("01-2026-11-20", f"{H}-{H}-{H}-{H}"),
        ("01- 2026-11-20", f"{H}- {H}-{H}-{H}"),  # 隔着空白也算接着
        ("01 / 2026/11/20", f"{H} / {H}/{H}/{H}"),
        ("2026-11-20 - 01", f"{H}-{H}-{H} - {H}"),
        ("2026-11-20.5", f"{H}-{H}-{H}"),  # 小数点紧挨着数字：接着的
        ("3.2026-11-20", f"{H}-{H}-{H}"),
        # 全角逗号不当千分位逗号：两个数各自换，逗号留着（独立审阅第 1 轮）
        ("我们有3，5个项目", f"我们有{H}，{H}个项目"),
        ("截止2026-11-20，3个月内能报吗", f"截止2026-11-20，{H}个月内能报吗"),
        ("2026/11/20/3", f"{H}/{H}/{H}/{H}"),
    ],
)
def test_hide_numbers(text, sent):
    assert hide_numbers(text) == sent


@pytest.mark.parametrize(
    "text",
    [
        "第 3 句",
        "第5条",
        "第\N{IDEOGRAPHIC SPACE}３\N{IDEOGRAPHIC SPACE}款",
        "第12项和第 1.5 段、第3点",
        "2026 年 11 月 20 日",
        "11月20号",
        "11 月 20 日前",
        "2026-11-20",
        "2026/11/20",
        "截止，2026年11月20日",  # 前面是逗号（全角逗号规范化以后是半角）也认得出
        "２０２６年１１月２０日截止",
        "第①条",
        "2026-11-20至2026-12-01，共两次",  # 两头都是整串的日期
        "2026-11-20。",
        "2026 - 11 - 20",  # 日期里面可以有空白
        # 小数点、逗号只有紧挨着数字才算接着：隔着空白、全角逗号隔开的日期照旧
        "2026-11-20，2026-12-01两个日期",
        "2026-11-20, 2026-12-01",
        "第1条。 2026-11-20是企业截止吗",
        "截止 2026/11/20 前",
        # 用汉字写的数字、企业名称认不出来，原样交给模型（告知里照实写）
        "三百人",
        "三点五万元",
        "一家有限责任公司能申请吗",
        "",
    ],
)
def test_hide_numbers_keeps(text):
    assert hide_numbers(text) == text
