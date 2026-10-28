"""问一问提问的本机规则：拦号码（第九节第 2 条）、带去查名单（第六节第 2 条）。

两个技能（问这份通知、找通知）调同一个函数，下面每条对两个技能都成立。"""

import pytest

from youju.chat_guard import goes_to_lists, sensitive
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
