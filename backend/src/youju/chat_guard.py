"""问一问提问的本机规则（问一问开发设计第九节第 2 条、第六节第 2 条）。前两条两个技能共用：

- sensitive：像不像带了号码（统一社会信用代码、手机号、身份证号、银行卡号）。像就拦下（422
  CHAT_SENSITIVE，不建行、不保存、不扣次数）。只在本机比，不发给任何人；
- goes_to_lists：是不是在问某家企业在不在名单里。是就带去查名单页（422 CHAT_GO_LISTS）；
- hide_numbers：问这份通知交给模型之前，把问题里的阿拉伯数字隐去（admin 交给模型时现做，存下的、
  显示的、送微信检查的都是原话）。
"""

from __future__ import annotations

import re
import unicodedata
from itertools import chain
from typing import Literal

from youju.crawler.detail import (
    _BANK,
    _CARD_GROUPED,
    _CARD_RUN,
    _DIGIT_GAP,
    _ID_CARD,
    _MOBILE_GROUPED,
    _MOBILE_JOINED,
    _luhn,
)
from youju.lists.rules import _CREDIT_ALPHABET

Sensitive = Literal["credit_code", "mobile", "id_card", "bank"]

# 统一社会信用代码：18 位，第 1、2 位和第 9–18 位取自信用代码的字符表（数字和除 I、O、S、V、Z
# 以外的大写字母），第 3–8 位是数字，前后不再连着字母数字。只看格式，不验校验位（抄错一位的也拦）
_CREDIT_CHAR = f"[{_CREDIT_ALPHABET}]"
_CREDIT_CODE = re.compile(
    rf"(?<![0-9A-Z]){_CREDIT_CHAR}{{2}}[0-9]{{6}}{_CREDIT_CHAR}{{10}}(?![0-9A-Z])"
)

# 问某家企业在不在名单里（第六节第 2 条的完整清单），下面三组各有一个才算：
# 说的是某一家企业。"股份有限公司"含"有限公司"；"有限责任公司"不含这四个字，单列
_COMPANY_WORDS = (
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
    "有限公司",
    "有限责任公司",
)
# 问的是名单
_LIST_WORDS = ("名单", "名录", "榜")
# 问在不在上面
_ASK_WORDS = ("在不在", "有没有", "是不是", "上没上", "上过", "进过", "入选", "列入")


def _credit_codes(plain: str) -> list[str]:
    """像信用代码的段（plain 已转半角、转大写）。

    同时符合身份证号格式（_ID_CARD：地区码、出生日期）的不算：18 位身份证号的每一位都在信用代码
    的字符表里，只看格式的话所有身份证号都会被认成信用代码，提示就写错了。和 lists/rules.py 的
    normalize_credit_code 一样，像身份证号的不当信用代码。"""
    return [m.group() for m in _CREDIT_CODE.finditer(plain) if not _ID_CARD.fullmatch(m.group())]


def sensitive(text: str) -> Sensitive | None:
    """带了哪种号码；都没有返回 None。

    先转半角（NFKC）、转大写，先认信用代码：认出来就按信用代码拦，找通知的提示要指去查名单。
    老的信用代码有全是数字的，银行卡的规则（16–19 位、过 Luhn）会命中其中约十分之一，所以信用
    代码一定先认；认出来直接返回，后面查手机号、身份证号、银行卡号的规则不会再看到它。

    其余号码照 crawler/detail.py 的 has_personal_numbers：数字中间带空格、横线的去掉再查，没去掉的
    也查一遍；手机号允许前面带 86 和 3-4-4 分段；身份证号用 _ID_CARD；银行卡号是 16–19 位、通过
    Luhn 校验的数字（4-4-4-4 分段的也算），或"账号""卡号"等字样后面跟 12–19 位。"""
    plain = unicodedata.normalize("NFKC", text).upper()
    if _credit_codes(plain):
        return "credit_code"
    joined = _DIGIT_GAP.sub("", plain)
    both = (plain, joined)
    if any(_MOBILE_JOINED.search(v) for v in both) or _MOBILE_GROUPED.search(plain):
        return "mobile"
    if any(_ID_CARD.search(v) for v in both):
        return "id_card"
    if any(_BANK.search(v) for v in both):
        return "bank"
    cards = chain(
        _CARD_RUN.finditer(plain), _CARD_RUN.finditer(joined), _CARD_GROUPED.finditer(plain)
    )
    if any(_luhn(re.sub(r"\D", "", m.group())) for m in cards):
        return "bank"
    return None


def goes_to_lists(text: str) -> bool:
    """是不是在问某家企业在不在名单里，是就带去查名单页（查名单只收企业全称，D-091 ③）。

    先 NFKC、去空白，再看三组词：说的是某一家企业（_COMPANY_WORDS），问的是名单（_LIST_WORDS），
    问在不在上面（_ASK_WORDS），各有一个才算。只认这一种说法，认不出的照常走自己的技能，不另外猜：
    "找江苏今年的专精特新名单"是找名单类的通知，"入选名单的企业有哪些支持"是在问这份通知。"""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    return all(
        any(word in compact for word in words)
        for words in (_COMPANY_WORDS, _LIST_WORDS, _ASK_WORDS)
    )


# 隐去数字（第九节第 2 条；D-093 第五节"不把企业的财务、研发数据和客户名称交给模型"，
# Codex 第 44–47 轮）
HIDDEN_NUMBER = "[数字已隐去]"
# 一串阿拉伯数字：数字中间夹着的小数点、千分位逗号算在里面
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# 不换的两种（完整清单）：① 序号，"第"+数字+"句、条、款、项、段、点"；② 写到哪一天的日期：
# 4 位+年+1–2 位+月+1–2 位+日/号、1–2 位+月+1–2 位+日/号，或 4 位、1–2 位、1–2 位用同一个"-"或"/"
# 连起来。中间可以有空白；只写到年、年月的不算。日期的头尾要是整串数字的头尾（前面不接着数字，
# 也不接着"数字+小数点或逗号"；"-""/"连的后面不再接着数字）
_KEEP = re.compile(
    r"第\s*\d+(?:[.,]\d+)*\s*[句条款项段点]"
    r"|(?<!\d)(?<!\d[.,])(?:"
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{4}\s*([-/])\s*\d{1,2}\s*\1\s*\d{1,2}(?![.,]?\d)"
    r")"
)


def hide_numbers(text: str) -> str:
    """问这份通知交给模型的问题：每一串阿拉伯数字换成"[数字已隐去]"，前后的字都留着（问一问开发
    设计第九节第 2 条的完整规则）。只有两种不换：序号（"第 3 句""第5条"）、写到哪一天的日期
    （"2026 年 11 月 20 日""11月20号""2026-11-20"）；"第500万元""500号客户""3 个月""2026 年"
    里的数字都换。

    先 NFKC 再认（全角数字、全角空格也认得出），换的时候只换原文里那串数字对应的字，别的字一个
    不改（"，"还是"，"）。用汉字写的数字（"三百人""三点五万元"）、企业名称认不出来，原样留着，
    告知里照实写（设计稿第一节第 3 件，10/29 创始人按推荐定了，D-106 ③）。"""
    pieces = [unicodedata.normalize("NFKC", char) for char in text]
    plain = "".join(pieces)
    owner = [at for at, piece in enumerate(pieces) for _ in piece]  # plain 的每个字是原文第几个字
    keep = [match.span() for match in _KEEP.finditer(plain)]
    out, last = [], 0
    for match in _NUMBER.finditer(plain):
        first, end = match.span()
        if any(a <= first and end <= b for a, b in keep):
            continue
        start, stop = owner[first], owner[end - 1] + 1
        if start < last:  # 原文一个字规范化以后有两串数字（比如"½"是"1⁄2"）：这个字已经整个换掉
            continue
        out.extend((text[last:start], HIDDEN_NUMBER))
        last = stop
    out.append(text[last:])
    return "".join(out)
