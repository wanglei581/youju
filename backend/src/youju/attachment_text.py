"""给 AI 用的附件文字（D-079、D-080；《附件读入方案》第三节第 1 条、第六节第 1 步）。

这一步只取文字、存好、量清楚，AI 还不读，用户看不到变化。

- 取文字（extract）：只取看得见的文字（隐藏文字、修订里删掉的字、域代码、批注、页眉页脚不取），
  按阅读顺序一段一行；表格一行一段，每一格记下列号、所在列的表头和文字；每段记下出处
  （PDF 第几页、工作表和行号、第几个表格的第几行、第几张幻灯片）。带公章等图片的文件照样取字，
  图片不取。不用附件副本查个人信息的 attachments.text_of：它遇到图片就停，取的文字也不分段。
- 格式：副本的 7 种（doc、docx、xls、xlsx、ppt、pptx、pdf）加上 wps、et（按文件头判断实际格式，
  wps 多半是 doc、et 多半是 xls）。zip、rar、ofd 不下载、不读。
- 存法：附件（原网站链接）→ 内容版本（SHA-256）→ 文字，三张表（models.AttachmentText*）。
  内容相同的几个链接共用一份文字；原网站删了、内容变了，链接改指新版本或清空，没有链接指向的
  旧版本连同文字删掉（撤下旧文字）。
- 号码：存进数据库之前，先用和正文同一个函数（crawler.detail.redact_sensitive）遮掉，再把它漏掉的
  分开写的号码（"138 1234 5678"、隔开写的身份证号和银行卡号、"账号："后面隔开写的数字）也遮掉。
  邮箱在这之前先只遮地址本身：正文函数的邮箱写法会把邮箱前面连着的中文（常是截止日期）一起遮掉。
- 范围跟附件副本一样（attachments.candidates）：申报通知近 180 天、政策近 90 天，一级公开来源、
  没下架，名单类不读。附件副本的定时任务下载时顺带取文字；AI 关着也照常跑。
"""

from __future__ import annotations

import bisect
import io
import logging
import re
import struct
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from lxml import etree
from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from youju.crawler import detail as body_rules
from youju.models import AttachmentTextSegment, AttachmentTextUrl, AttachmentTextVersion
from youju.text_lines import layout_wrapped, line_width

log = logging.getLogger("youju.attachment_text")

# 取文字的规则版本：规则改了（怎么分段、哪些字算看得见、怎么遮号码）就加 1，已取的会按新规则重取
EXTRACTOR_VERSION = 1
# 除了副本的 7 种，再下载这两种只取文字（小程序打不开，不存副本）
TEXT_ONLY_TYPES = ("wps", "et")
# 一份附件最多取这么多字（遮号码以后、表格按"格 | 格"连起来算），超过的按"太大"记，不存
MAX_CHARS = 300_000
# 一段最长这么多字，更长的在句末切开（和正文分段 interpret.split_paragraphs 一样）
SEGMENT_MAX = 1000
_PDF_PAGES = 300
# 解压后的 XML 合计上限（防压缩炸弹），和附件副本的检查一样；要整份载入内存解析的单个部件
# 上限（20MB 的正文 XML 解析后要占几百 MB 内存，字数也早超过 MAX_CHARS 了）
_XML_MAX = 100 * 1024 * 1024
_TREE_MAX = 20 * 1024 * 1024
# 扫描页（见 _pdf_page）：不到这么多字、图片占了这么大比例的页面；或者图片盖住了几乎整页、
# 字是识别层
_SCAN_PAGE_CHARS = 10
_SCAN_PAGE_SOME_COVER = 0.3
_SCAN_PAGE_COVER = 0.9
_PDF_MAX_EDGES = 5000
# 没有字、曲线却有这么多的页：字被转成了线条，按扫描页算
_OUTLINED_CURVES = 100
# 页码只在页面上下这么宽的边上认
_MARGIN = 0.1

# 取不出、下载不了的原因（models.ATTACHMENT_TEXT_REASONS），统计和以后的结果说明按这里写
REASONS = {
    "scan": "扫描件或图片，取不出文字",
    "empty": "文件里没有文字",
    "broken": "文件损坏或加密",
    "format": "格式不支持",
    "too_large": "太大",
    "webpage": "下载到的是网页，不是文件",
    "gone": "原网站已没有这个附件",
    "unavailable": "原网站下载不了",
}


class Unreadable(Exception):
    """这份内容取不出文字：reason 是 REASONS 里的代码，detail 写具体情况。"""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class Segment:
    n: int
    kind: str  # p：段落；head：表头行；row：表格的其他行
    text: str
    loc: dict | None = None
    cells: list[dict] | None = None


@dataclass
class Extraction:
    file_type: str | None
    segments: list[Segment] = field(default_factory=list)
    reason: str | None = None
    detail: str | None = None
    pages: int | None = None
    scanned_pages: int | None = None
    masked: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is None

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.segments)


# ---- 遮号码 ----

_HALF = body_rules._HALF_WIDTH
# 一串数字：组和组之间一个空格、一条横线（破折号也算）或一个换行（clean 已把连续空白收成一个）
_SEP = "[ \n\\-\u2010-\u2015\u2212]"
# 小数（"13812345678.50 元"）、点号隔开的版本号和日期不当号码，也不和前面的数连成一串
# （"1 138 1234 5678 9.5"里的 9.5 是另一个数）。每组的数字不回退（++），免得把"4500.5"切成"450"
_RUN = re.compile(
    rf"(?<![\dA-Za-z])(?<!\d\.)\d++(?!\.\d)(?:{_SEP}\d++(?!\.\d))*[Xx]?(?![\dA-Za-z])"
)
_GROUP = re.compile(r"\d+[Xx]?")
# 跟着这些字的一组数字是日期、金额，不算进前面的号码（"账号：1234567890 2025年11月5日前"）
_UNITS = "年月日元万亿号个次%时分秒"
# 号码前面的标签：紧挨着，中间只隔冒号、空格、"为""是"。账号标签后面可以带括号说明（"账号（工商
# 银行杭州分行）："），和号码中间还可以隔着开户行（"开户行及账号：中国工商银行杭州分行 1202…"）；
# 只写"开户行""开户银行"、后面是行名再是号码的也算账号标签。有标签的怎么分组都认
_ACCOUNT_WORDS = (
    "银行卡号", "银行账号", "银行帐号", "开户账号", "开户帐号", "账户号码", "帐户号码", "账户号",
    "帐户号", "账号码", "帐号码", "卡号码", "银行卡", "账户", "帐户", "账号", "帐号", "卡号",
)  # fmt: skip
_ACCOUNT_LABEL = "(?:" + "|".join(r"\s*".join(w) for w in _ACCOUNT_WORDS) + ")"
_LABEL_END = r"[\s:：]*(?:[为是][\s:：]*)?"
# 开户行的名字：以银行、支行、农商行、信用联社……结尾（前面最多 30 个字，可以有英文字母，如
# "ICBC杭州分行"），或者是"工行、建行"这样的简称；后面可以带括号（"（营业部）"）、冒号、"为/是"
# （"……杭州分行：1202…"）
_BANK = (
    r"(?:[\u4e00-\u9fffA-Za-z（）()]{0,30}?"
    r"(?:银行|支行|分行|营业部|信用社|合作社|农商行|农信社|联社|储蓄所)"
    r"|[\u4e00-\u9fffA-Za-z]{0,20}?(?:工行|农行|中行|建行|交行|招行|邮储))"
    r"(?:[(（][^()（）]{1,12}[)）])?[\s,，:：]*(?:[为是][\s:：]*)?"
)
_NOTE = r"(?:\s*[(（][^()（）]{1,20}[)）])?"  # 标签后面括号里的说明
_OPEN_BANK = r"开\s*户\s*(?:银\s*)?行"
_LABELS = {
    "account": re.compile(
        f"(?:{_ACCOUNT_LABEL}{_NOTE}{_LABEL_END}(?:{_BANK})?|{_OPEN_BANK}{_LABEL_END}{_BANK})$"
    ),
    "phone": re.compile(
        r"(?:手\s*机(?:\s*号\s*码?)?|电\s*话|联\s*系\s*方\s*式|tel|mobile|phone)[\s:：.]*"
        r"(?:[为是][\s:：]*)?$",
        re.I,
    ),
    "id": re.compile(r"(?:身\s*份\s*证(?:\s*号\s*码?)?|证\s*件\s*号\s*码?)" + _LABEL_END + "$"),
}
_BEFORE = 48  # 往前看多少个字找标签、金额词（标签后面隔着开户行时要看得远一点）
# 表格里当"账号"标签的格：整格是"……账号""……卡号""……账户号（码）""……账户"（前面最多 8 个字，
# 如"开户银行及账号""收款单位开户账号""银行账户"，字间可以有空格），后面可以带"/卡号"、括号说明
# （"（基本户）""（必填）"）、星号、冒号。"账户名称""账户类型""各单位银行账户信息表"这类后面还有
# 别的字的不算，"基本账户""基本存款账户""零余额账户（人民币）"这类账户类型的值也不算
_ACCOUNT_CORE = r"(?:[账帐]\s*(?:户\s*)?号(?:\s*码)?|卡\s*号(?:\s*码)?|[账帐]\s*户)"
_ACCOUNT_CELL = re.compile(
    r"^[*＊]?\s*(?:[\u4e00-\u9fff]\s*){0,8}"
    + _ACCOUNT_CORE
    + r"(?:\s*[/／、]\s*"
    + _ACCOUNT_CORE
    + r")?(?:\s*[(（][^()（）]{1,12}[)）])?\s*[*＊]?[\s:：]*$"
)
_ACCOUNT_TYPE = re.compile(
    "(?:"
    + "|".join(r"\s*".join(w) for w in ("基本", "一般", "专用", "临时", "零余额"))
    + r")\s*(?:存\s*款\s*)?[账帐]\s*户(?:\s*[(（][^()（）]{1,12}[)）])?[\s*＊:：]*$"
)
# 表格里当"身份证号""手机"标签的格（前面最多 6 个字，如"法人身份证号""联系人电话"；后面可以带
# 括号说明、星号、冒号）：这一列往下、同一行右边那格按身份证号、手机号的标签认（PDF 窄列折行写成
# "1101011990030 71234"也能认）
_ID_CELL = re.compile(
    r"^[*＊]?\s*(?:[\u4e00-\u9fff]\s*){0,6}"
    r"(?:身\s*份\s*证(?:\s*号(?:\s*码)?)?|证\s*件\s*号(?:\s*码)?)"
    r"(?:\s*[(（][^()（）]{1,12}[)）])?\s*[*＊]?[\s:：]*$"
)
_PHONE_CELL = re.compile(
    r"^[*＊]?\s*(?:[\u4e00-\u9fff]\s*){0,6}"
    r"(?:手\s*机(?:\s*号(?:\s*码)?)?|电\s*话(?:\s*号(?:\s*码)?)?|联\s*系\s*方\s*式)"
    r"(?:\s*[(（][^()（）]{1,12}[)）])?\s*[*＊]?[\s:：]*$"
)
# 没写标签的号码：一整串数字（两边都不是数字）要能整个分成几段，每段是常见分法的身份证号、手机号、
# 卡号，或者序号（1–3 位）、日期（"2025 10 10""20251010"）、座机号码（"0571 87654321"），才遮
# 号码那几段（"1 138 1234 5678"、身份证号和手机号两列挨着）；分不完的整串不遮，免得把金额、编号
# 拼成号码（"合计 1350 820 4500"）
_PLAIN_ID = {(18,), (6, 8, 4), (6, 4, 4, 4), (4, 4, 4, 4, 2), (15,), (6, 6, 3)}
_PLAIN_MOBILE = {(11,), (3, 4, 4), (13,), (2, 11), (2, 3, 4, 4)}
_PARTS = 5  # 一段最多几组（4-4-4-4-2 的身份证号、带尾巴的卡号）
_MOBILE = re.compile(r"(?:86)?1[3-9]\d{9}")
_DATE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
# 金额：前面紧挨着这些词（中间只能有"（万元）"这样的括号、"为""约"、冒号、空格）、或后面跟着
# "元、万、亿"的一整串数字，没写标签时只遮身份证号（"补助对象：张三 138 1234 5678"不算金额）
_AMOUNT_BEFORE = re.compile(
    r"(?:金额|合计|预算|经费|资金|标准|补助|奖励|数量|共计|小计|总计|投资|收入)"
    r"\s*(?:[(（][^()（）\d]{0,6}[)）])?\s*(?:为|是|约|达|共)?[\s:：]*$"
)
# 邮箱只认英文字母、数字和 ._%+- 组成的地址，先遮掉：正文规则（redact_sensitive）的写法会把
# 邮箱前面连着的中文一起遮掉（\w 也算汉字，"于7月2日前发送至abc@163.com"整段变成"[邮箱已脱敏]"，
# 截止日期就没了）。先遮掉以后正文规则找不到 @，不会再吞字
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "联 系 人：张三"：PDF、表格里常把标签字隔开写，正文规则（"联系人："）认不出
_SPACED_CONTACT = re.compile(r"(联\s*系\s*人\s*[:：]\s*)[\u4e00-\u9fff·]{2,8}")
# 小数："13812345678.50""0.5"（前后都不是数字、小数点）
_DECIMAL = re.compile(r"((?<![\d.])\d+\.\d+(?![\d.]))")


def _account_cell(text: str, *, types: bool = False) -> bool:
    """这一格是不是账号标签。"基本存款账户"这类账户类型的词，types=True 时也当标签：表格里这一列
    往下按账号认（列名常这样写，两层表头的第二层也是；"账户类型"列下面只有账户类型的值，多认了也
    不会遮错）；"名目 | 内容"的表里左边那格。"""
    return bool(_ACCOUNT_CELL.match(text)) and (types or not _ACCOUNT_TYPE.search(text))


def _cell_label(text: str, *, types: bool = False) -> str | None:
    """表格里这一格是不是标签：账号（_account_cell）、身份证号、手机（_ID_CELL、_PHONE_CELL）。"""
    if _account_cell(text, types=types):
        return "account"
    if _ID_CELL.match(text):
        return "id"
    return "phone" if _PHONE_CELL.match(text) else None


def _is_card(values: list[str]) -> bool:
    """没写"账号"的卡号：连写 16–19 位，或四位一组（最后一组可以 1–3 位）；开头像卡号
    （3、4、5、6、8、9；年份、编号多半是 1、2 开头），过 Luhn；每组都是整百整千的是金额。"""
    digits = "".join(values)
    if not 16 <= len(digits) <= 19 or digits[0] not in "345689" or not body_rules._luhn(digits):
        return False
    if len(values) == 1:
        return True
    sizes = [len(v) for v in values]
    if sizes[:4] != [4, 4, 4, 4] or len(values) > 5 or (len(values) == 5 and sizes[4] > 3):
        return False
    return not all(v.endswith("00") for v in values[:4])


def _plain_label(values: list[str], amount: bool) -> str | None:
    """没写标签的一段：是常见分法的身份证号、手机号、卡号才遮；金额前后文里只认身份证号。"""
    digits = "".join(values).upper()
    sizes = tuple(len(v) for v in values)
    if sizes in _PLAIN_ID and body_rules._ID_CARD.fullmatch(digits):
        return "[身份证号已脱敏]"
    if "X" in digits or amount:
        return None
    if sizes in _PLAIN_MOBILE and _MOBILE.fullmatch(digits):
        return "[手机号已脱敏]"
    if _is_card(values):
        return "[银行账号已脱敏]"
    return None


def _ymd(year: str, month: str, day: str) -> bool:
    """ "2025 10 10""2025-1-5"这样分成年、月、日三组写的日期。"""
    if not (len(year) == 4 and len(month) <= 2 and len(day) <= 2):
        return False
    return bool(_DATE.fullmatch(f"{year}{int(month):02d}{int(day):02d}"))


def _filler(values: list[str]) -> bool:
    """可以夹在号码前后、不用遮的一段：序号（1–3 位）、日期（"2025 10 10""20251010"）、
    座机号码（"0571 87654321"）。"""
    if not all(v.isdigit() for v in values):
        return False
    sizes = tuple(len(v) for v in values)
    if sizes in ((1,), (2,), (3,)):
        return True
    if len(values) == 3:
        return _ymd(*values)
    if sizes == (8,):
        return bool(_DATE.fullmatch(values[0]))
    return len(values) == 2 and values[0][0] == "0" and sizes[0] in (3, 4) and sizes[1] in (7, 8)


def _labeled_label(values: list[str], label: str) -> str | None:
    """前面写了标签的：这几组怎么分组都认。"""
    digits = "".join(values).upper()
    if label == "id":
        ok = len(digits) in (15, 18) and body_rules._ID_CARD.fullmatch(digits)
        return "[身份证号已脱敏]" if ok else None
    if "X" in digits:
        return None
    if label == "phone":
        return "[手机号已脱敏]" if _MOBILE.fullmatch(digits) else None
    # 账号：10–30 位
    return "[银行账号已脱敏]" if 10 <= len(digits) <= 30 else None


def _date_at(values: list[str], j: int) -> bool:
    """第 j 组起是一个日期（"2025 10 10""20251010"）。"""
    if len(values[j]) == 8 and values[j].isdigit() and _DATE.fullmatch(values[j]):
        return True
    three = values[j : j + 3]
    return len(three) == 3 and all(v.isdigit() for v in three) and _ymd(*three)


def _labeled(
    plain: str, groups: list[tuple[int, int]], values: list[str], label: str, start: int
) -> list[tuple[int, str]]:
    """写了标签的：从第 start 组起到第几组能认成号码（_labeled_label），[(到第几组前, 遮成什么)]，
    从短到长。后面紧跟"年、月、元"等的一组不接（"账号：1234567890 2025年11月5日前"）。账号还不接：
    日期（"账号：1202… 2025-10-10"）；第一组连写已经 12 位以上、接上以后超过 22 位的（"1202…568
    1500"；PDF 格子里折成两行的"1202021309001 234568"照样接上）。"""
    out = []
    for j in range(start, len(groups)):
        total = sum(len(v) for v in values[start : j + 1])
        if total > 30:
            break
        if (
            j > start
            and label == "account"
            and (_date_at(values, j) or (len(values[start]) >= 12 and total > 22))
        ):
            break
        after = plain[groups[j][1] : groups[j][1] + 3].lstrip(" ")[:1]
        if j > start and after and after in _UNITS:
            break
        found = _labeled_label(values[start : j + 1], label)
        if found:
            out.append((j + 1, found))
    return out


def _split(
    plain: str, groups: list[tuple[int, int]], values: list[str], label: str | None, amount: bool
) -> list[tuple[int, int, str]]:
    """把一整串分成几段：号码（写了标签的按标签认，_labeled；没写的 _plain_label）、序号日期座机
    （_filler）。手机、身份证标签只管第一段，账号标签每一段都管（账号列一格写了两个账号）。能整个
    分完的，取遮住的数字最多、段数最少的分法。分不完的：没写标签的整串不遮；写了标签的从左往右，
    能按标签认成号码的取最长一段，认不出的组跳过（"1202… 2025年起"）。
    返回 [(第几组起, 到第几组前, 遮成什么)]。"""
    n = len(values)
    labeled = [
        _labeled(plain, groups, values, label, i)
        if label and (i == 0 or label == "account")
        else []
        for i in range(n)
    ]
    # best[i]：从第 i 组到最后的最好分法 (遮住几个数字, -段数, 要遮的段)
    best: list[tuple[int, int, list] | None] = [None] * (n + 1)
    best[n] = (0, 0, [])
    for i in range(n - 1, -1, -1):
        options = list(labeled[i])
        for j in range(i + 1, min(n, i + _PARTS) + 1):
            found = _plain_label(values[i:j], amount)
            if found or _filler(values[i:j]):
                options.append((j, found))
        for j, found in options:
            if best[j] is None:
                continue
            masked, count, parts = best[j]
            if found:
                masked += sum(len(v) for v in values[i:j])
                parts = [(i, j, found), *parts]
            if best[i] is None or (masked, count - 1) > best[i][:2]:
                best[i] = (masked, count - 1, parts)
    if best[0] is not None:
        return best[0][2]
    parts, i = [], 0
    while i < n:
        if labeled[i]:
            end, found = labeled[i][-1]
            parts.append((i, end, found))
            i = end
        else:
            i += 1
    return parts


def _only_dates(values: list[str]) -> bool:
    """整串是日期："20180520-20261009"（每组 8 位），或"2018 05 20 2026 10 09"（年、月、日
    三组一段）。"""
    if not all(v.isdigit() for v in values):
        return False
    if all(len(v) == 8 and _DATE.fullmatch(v) for v in values):
        return True
    return len(values) % 3 == 0 and all(_ymd(*values[i : i + 3]) for i in range(0, len(values), 3))


def _number_spans(plain: str, column: str | None) -> list[tuple[int, int, str]]:
    """plain 里要遮的号码：[(开始, 结束, 遮成什么)]。一串一串地看（组和组之间只隔一个空格、横线或
    换行）：
    - 前面写了"账号""手机""身份证"这类标签（_LABELS），或 column 给了（表格里这一列上面、左边那格
      写着"账号""身份证号""手机"）：按标签认，怎么分组都行（_labeled）；
    - 没写标签的：整串能分成号码和序号、日期、座机（_split）才遮号码那几段；前面紧挨着"合计、
      预算、金额"之类、后面跟着"元、万、亿"的是金额，只遮身份证号；
    - 整串是日期（"2018-05-20—2026-10-09""20180520-20261009"）的，哪一段都不当号码。"""
    spans = []
    for run in _RUN.finditer(plain):
        groups = [
            (run.start() + g.start(), run.start() + g.end()) for g in _GROUP.finditer(run.group())
        ]
        values = [plain[a:b] for a, b in groups]
        if _only_dates(values):
            continue
        before = plain[max(0, run.start() - _BEFORE) : run.start()]
        # 格子里自己写了标签的按格子里的认（手机号那一列里写着"开户行：…… 1202…"），没写的用列标签
        label = next((k for k, p in _LABELS.items() if p.search(before)), None) or column
        unit = plain[run.end() : run.end() + 3].lstrip(" ")[:1]
        amount = bool(_AMOUNT_BEFORE.search(before)) or unit in ("元", "万", "亿")
        spans += [
            (groups[i][0], groups[j - 1][1], found)
            for i, j, found in _split(plain, groups, values, label, amount)
        ]
    return spans


def _replace(text: str, spans: list[tuple[int, int, str]]) -> str:
    out, at = [], 0
    for start, end, label in sorted(spans):
        if start < at:  # 和前一个重叠：前一个已经遮了
            continue
        out += [text[at:start], label]
        at = end
    out.append(text[at:])
    return "".join(out)


def mask(text: str, *, column: str | None = None) -> str:
    """遮掉身份证号、手机号、银行账号（还有正文规则一并遮的联系人、邮箱、座机）。

    1. 先只遮邮箱地址本身（_EMAIL，免得正文规则把邮箱前面的中文一起遮掉）；
    2. 再遮分开写的号码（_number_spans）：写了"账号""手机""身份证"这类标签的（column：表格里
       这一列上面、左边那格写着的标签，"account""id""phone"）怎么分组都认；没写的整串能分成常见分法的号码和
       序号、日期、座机才遮（免得把一串金额、编号拼成号码）；
    3. 用正文的规则（redact_sensitive，连写的号码、"账号："后的号码、联系人、座机）；小数原样
       留着，不交给它（"13812345678.50 元"小数点前面的 11 位会被它当成手机号）；
    4. 再遮标签字隔开写的"联 系 人："。
    全角数字先按半角找（一个字换一个字，位置不变）。"""
    text = _EMAIL.sub("[邮箱已脱敏]", text)
    text = _replace(text, _number_spans(text.translate(_HALF), column))
    parts = _DECIMAL.split(text)  # 单数位是小数
    text = "".join(p if i % 2 else body_rules.redact_sensitive(p) for i, p in enumerate(parts))
    return _SPACED_CONTACT.sub(r"\1[姓名已脱敏]", text)


# ---- 认格式（wps、et 要按文件头认出实际格式） ----

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_MAIN = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "ppt/presentation.xml": "pptx",
}
_OLE_MAIN = {"WordDocument": "doc", "Workbook": "xls", "Book": "xls", "PowerPoint Document": "ppt"}
_HTML = re.compile(
    rb"^\s*(?:<!doctype\s+html|<html|<head|<body|<meta|<script|<\?xml[^>]*>\s*<html)", re.I
)


def sniff(data: bytes) -> str:
    """按文件头和内部结构认出实际格式（7 种之一）；认不出抛 Unreadable 并写明原因。"""
    if not data:
        raise Unreadable("broken", "下载到的内容是空的")
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, ValueError, OSError, EOFError) as exc:
            raise Unreadable("broken", "压缩结构损坏") from exc
        found = {kind for name, kind in _OOXML_MAIN.items() if name in names}
        if len(found) == 1:
            return found.pop()
        if "OFD.xml" in names:
            raise Unreadable("format", "OFD 版式文件")
        raise Unreadable("format", "压缩包或认不出的 Office 文件")
    if data.startswith(_OLE):
        import olefile

        try:
            with olefile.OleFileIO(data) as ole:
                roots = {entry[0] for entry in ole.listdir(streams=True, storages=True)}
        except Exception as exc:  # noqa: BLE001 - 外部文件可能损坏，解析器会抛多种异常
            raise Unreadable("broken", "复合文档结构损坏") from exc
        if "EncryptedPackage" in roots or "EncryptionInfo" in roots:
            raise Unreadable("broken", "加密的 Office 文件")
        found = {kind for name, kind in _OLE_MAIN.items() if name in roots}
        if len(found) == 1:
            return found.pop()
        raise Unreadable("format", "认不出的旧版 Office 或 WPS 文件")
    if data.startswith(b"Rar!"):
        raise Unreadable("format", "RAR 压缩包")
    if data.lstrip()[:5] == b"{\\rtf":
        raise Unreadable("format", "RTF 文件")
    if _HTML.match(data[:1024]) or b"<html" in data[:512].lower():
        raise Unreadable("webpage", "下载到的是网页")
    raise Unreadable("format", "认不出的格式")


# ---- 分段 ----

# 换行以外的空白（含制表符、全角空格、不换行空格）都收成一个空格；零宽字符去掉
_SPACES = re.compile(r"[^\S\n]+")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
_NEWLINES = re.compile(r" ?\n[ \n]*")
_SENTENCE_END = re.compile(r"[。！？!?；;]")
# PDF 页眉页脚里的页码（只在页面上下边上的才删，见 _pdf_page）
_PAGE_NUMBER = re.compile(
    r"^(?:[-\u2012-\u2015\uff0d\s]*\d{1,4}[-\u2012-\u2015\uff0d\s]*|第\s*\d+\s*页(?:\s*[,，/、]?\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+)$"
)
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]")


# 私用区里的符号字体字符（Wingdings 等）：勾选框、项目符号换成看得见的样子，其余去掉
_PUA = re.compile(r"[\ue000-\uf8ff]")
_PUA_SYMBOLS = {
    0xF0A8: "□",
    0xF0A3: "□",
    0xF06F: "□",
    0xF0FE: "☑",
    0xF0FD: "☒",
    0xF052: "☑",
    0xF0FC: "✓",
    0xF0A1: "○",
    0xF06C: "●",
    0xF06E: "■",
    0xF0A7: "▪",
    0xF0B7: "•",
    0xF0D8: "➢",
}


def _symbol(match: re.Match) -> str:
    return _PUA_SYMBOLS.get(ord(match.group()), "")


def clean(text: str) -> str:
    """看得见的样子：换行以外的空白（含全角空格）收成一个空格，段内换行保留一个，首尾的空白
    （含全角空格缩进）去掉；都是线性时间（一长串全角空格也不会慢）。
    一行一个字的竖排（表格里常见的"资、格、审、核"各占一行）接成一行。"""
    text = _PUA.sub(_symbol, _ZERO_WIDTH.sub("", text)).replace("\r", "\n")
    text = _NEWLINES.sub("\n", _SPACES.sub(" ", text)).strip(" \n")
    lines = text.split("\n")
    if len(lines) > 1 and all(len(line) == 1 and _CJK.match(line) for line in lines):
        return "".join(lines)
    return text


def _join_lines(lines: Iterable[str]) -> str:
    """单元格里折行的几行接起来：中文和中文之间不加空格。"""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out and not (_CJK.match(out[-1]) and _CJK.match(line[0])):
            out += " "
        out += line
    return out


def _split_long(text: str) -> list[str]:
    parts = []
    while len(text) > SEGMENT_MAX:
        cuts = list(_SENTENCE_END.finditer(text[:SEGMENT_MAX]))
        cut = cuts[-1].end() if cuts else SEGMENT_MAX
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


@dataclass
class _Cell:
    col: int
    text: str
    span: int = 1


@dataclass
class _Row:
    cells: list[_Cell]
    loc: dict
    header: bool = False  # 文件自己标了"标题行重复"（Word 的 tblHeader）


class _Builder:
    """按阅读顺序收段落和表格行，边收边遮号码、数字数（超过 MAX_CHARS 抛 Unreadable）。"""

    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self.chars = 0
        self.masked = 0

    def _add(self, kind: str, text: str, loc: dict | None, cells: list[dict] | None) -> None:
        self.chars += len(text)
        if self.chars > MAX_CHARS:
            raise Unreadable("too_large", f"取出的文字超过 {MAX_CHARS // 10000} 万字")
        self.segments.append(Segment(len(self.segments) + 1, kind, text, loc or None, cells))

    def para(self, text: str, loc: dict | None = None) -> None:
        text = clean(text)
        if not text:
            return
        masked = mask(text)
        if masked != text:
            self.masked += 1
        for part in _split_long(masked):
            self._add("p", part, loc, None)

    def table(
        self,
        rows: list[_Row],
        *,
        headers: dict[int, str] | None = None,
        repeated: bool = False,
        columns: dict[int, str] | None = None,
    ) -> tuple[dict[int, str], str, dict[int, str]]:
        """一张表：先认表头行（_header_rows），表头行记成 head，表头下面各行的每一格记下所在列的
        表头（表头上面的表名、填写行不记）。PDF 跨页的续表：headers 给了就不再认表头，按它记；
        repeated：续表第一行是每页重复的表头，记成 head；columns：上一页认出的标签列。
        返回 (表头, 表头行的文字, 标签列)，续表接着用。

        标签列不靠认出表头：哪一行有一格是"账号""开户银行及账号""身份证号""联系电话"这类标签
        （_cell_label；跨三列以上的表名不算），这一列以下各格的号码都按这个标签认（账号不过 Luhn、
        折行写开的号码也遮）；同一行左边那格是这类标签的也一样。"""
        rows = [r for r in rows if any(clean(c.text) for c in r.cells)]
        if not rows:
            return headers or {}, "", columns or {}
        if headers is not None:
            head = {0} if repeated else set()
            first = len(head)  # 续表：表头行（重复的那一行）以下都记表头
        else:
            head = _header_rows(rows)
            headers = {}
            for i in head:
                for cell in rows[i].cells:
                    text = mask(clean(cell.text))
                    if not text:
                        continue
                    for col in range(cell.col, cell.col + max(cell.span, 1)):
                        headers[col] = f"{headers[col]}/{text}" if col in headers else text
            first = max(head) + 1 if head else len(rows)
        # 这一列从第几行以下按什么标签认
        label_from = {col: (-1, kind) for col, kind in (columns or {}).items()}
        for index, row in enumerate(rows):
            for cell in row.cells:
                text = clean(cell.text)
                kind = _cell_label(text, types=True) if cell.span < 3 else None
                for col in range(cell.col, cell.col + max(cell.span, 1)) if kind else ():
                    label_from.setdefault(col, (index, kind))
        head_text = ""
        for i, row in enumerate(rows):
            cells, changed, left, left_col = [], False, "", -1
            for cell in row.cells:
                text = clean(cell.text)
                if not text:
                    continue
                header = headers.get(cell.col) if i >= first else None
                since, kind = label_from.get(cell.col, (len(rows), None))
                # 左边那格写着标签的先按它认（比列标签离得近）。有表头的列里的账户类型词是值
                # （"账户类型"列里的"基本账户"），右边那格（项目编号）不按账号认；"名目 | 内容"的
                # 表（没有列表头）里是标签
                left_kind = _cell_label(left, types=left_col not in headers)
                masked = mask(text, column=left_kind or (kind if i > since else None))
                changed |= masked != text
                item = {"c": cell.col, "t": masked}
                if header:
                    item["h"] = header
                cells.append(item)
                left, left_col = text, cell.col
            if changed:
                self.masked += 1
            row_text = " | ".join(c["t"] for c in cells)
            if i in head:
                head_text = row_text
            self._add("head" if i in head else "row", row_text, row.loc, cells)
        return headers, head_text, {col: kind for col, (_since, kind) in label_from.items()}


_NUMBER = re.compile(r"[\d.,%\s\-]+")
_YEAR = re.compile(r"(?:19|20)\d{2}")


def _header_rows(rows: list[_Row]) -> set[int]:
    """哪几行是表头。文件自己标了（Word 的"标题行重复"）就用标的；没标的，在前 6 行里找第一个
    像表头的行：两格以上有字、中间没有空格、每格不超过 30 字、没有一格只是数字（年份除外）、
    右边到了下面各行（取中位数）九成以上的宽度、后面还有行。不像的跳过：表名（只有一格有字）、
    "项目名称 □ 申报单位 □"这种中间有空格的填写行、长的说明文字、"1 | 甲公司"这种数据行、
    "填报单位 | 填报日期"这种只占一部分列的行。都不像就没有表头（填写表、数据表常常这样）。
    整张表只有两列的（"项目名称 | 某某项目"这种名目加内容）不认列表头。"""
    marked = {i for i, r in enumerate(rows) if r.header}
    if marked:
        return marked
    widths = [
        max((c.col + max(c.span, 1) for c in r.cells if clean(c.text)), default=0) for r in rows
    ]
    if max(widths) <= 2:
        return set()  # 两列的表多半是"名目 | 内容"，第一行也是内容，不认列表头
    for i, row in enumerate(rows[:6]):
        if i == len(rows) - 1:
            break
        filled = [(c, clean(c.text)) for c in row.cells if clean(c.text)]
        covered = {col for c, _ in filled for col in range(c.col, c.col + max(c.span, 1))}
        if len(filled) < 2 or covered != set(range(min(covered), max(covered) + 1)):
            continue
        if any(len(t) > 30 or (_NUMBER.fullmatch(t) and not _YEAR.fullmatch(t)) for _, t in filled):
            continue  # 说明文字、"1 | 甲公司"这种数据行（年份格"2025"可以是表头）
        below = sorted(widths[i + 1 :])
        if max(covered) + 1 < 0.9 * below[len(below) // 2]:
            continue  # 右边没到下面各行的宽度（左上角空着的表头照样算）
        return {i}
    return set()


@dataclass
class _Found:
    """一种格式取完的结果：段落，加上判断"是不是扫描件"要用的图片个数、大小和 PDF 页数。"""

    builder: _Builder
    pictures: int = 0
    picture_bytes: int = 0
    pages: int | None = None
    scanned: int | None = None


# ---- 新版 Office（docx、xlsx、pptx） ----

_W_NS = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_R_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _ns(el) -> str:
    return etree.QName(el).namespace or "" if isinstance(el.tag, str) else ""


def _is_w(el, *names: str) -> bool:
    return isinstance(el.tag, str) and _ns(el) in _W_NS and _local(el) in names


def _w_attr(el, name: str) -> str | None:
    for ns in _W_NS:
        value = el.get(f"{{{ns}}}{name}")
        if value is not None:
            return value
    return el.get(name)


def _w_child(el, name: str):
    for child in el:
        if _is_w(child, name):
            return child
    return None


def _on(el) -> bool:
    """w:vanish 这类开关：有这个元素且没写成 false、0、off。"""
    return el is not None and (_w_attr(el, "val") or "true").lower() not in ("false", "0", "off")


def _choice(el):
    """mc:AlternateContent 只看第一个 mc:Choice（没有就看 mc:Fallback），两份是同一内容。"""
    for child in el:
        if _ns(child) == _MC and _local(child) == "Choice":
            return child
    for child in el:
        if _ns(child) == _MC and _local(child) == "Fallback":
            return child
    return None


class _Zip:
    """读 Office 文件里的部件：解压后的合计大小有上限（防压缩炸弹）；要整份载入内存解析的部件
    （正文、工作簿、幻灯片等）每个不超过 _TREE_MAX，工作表、共享字符串边读边解析。"""

    def __init__(self, data: bytes) -> None:
        self.zf = zipfile.ZipFile(io.BytesIO(data))
        self.names = set(self.zf.namelist())
        self.total = 0

    def count(self, size: int) -> None:
        self.total += size
        if self.total > _XML_MAX:
            raise Unreadable("too_large", "解压后超过 100MB")

    def read(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        with self.zf.open(name) as fh:
            raw = fh.read(_TREE_MAX + 1)
        if len(raw) > _TREE_MAX:
            raise Unreadable("too_large", f"文件里的一个部件解压后超过 {_TREE_MAX // 2**20}MB")
        self.count(len(raw))
        return raw

    def xml(self, name: str):
        raw = self.read(name)
        return None if raw is None else etree.fromstring(raw, _PARSER)

    def iterparse(self, name: str) -> Iterator:
        """边读边解析（工作表、共享字符串可能很大），读到的字节照样计入合计上限。"""
        if name not in self.names:
            return
        with self.zf.open(name) as fh:
            yield from etree.iterparse(
                _Counted(fh, self), events=("end",), resolve_entities=False, huge_tree=False
            )

    def media(self) -> tuple[int, int]:
        """包里的图片个数和合计大小（字很少、图很大时算扫描件）。"""
        infos = [i for i in self.zf.infolist() if "/media/" in f"/{i.filename}"]
        return len(infos), sum(i.file_size for i in infos)

    def rels(self, part: str) -> dict[str, str]:
        """部件的关系：编号 → 目标部件的完整路径。"""
        folder, _, base = part.rpartition("/")
        root = self.xml(f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels")
        out: dict[str, str] = {}
        if root is None:
            return out
        for rel in root:
            target, rid = rel.get("Target") or "", rel.get("Id") or ""
            if rel.get("TargetMode") == "External" or not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else _join(folder, target)
            out[rid] = path
        return out

    def main_part(self, default: str) -> str:
        """包的主部件：_rels/.rels 里登记的、和默认路径在同一个文件夹的那个；没登记用默认路径。"""
        folder = default.split("/")[0]
        for target in self.rels("").values():
            if target in self.names and target.endswith(".xml") and target.split("/")[0] == folder:
                return target
        return default


class _Counted:
    """给 iterparse 读的文件：读到的字节计入 _Zip 的合计上限。"""

    def __init__(self, fh, owner: _Zip) -> None:
        self.fh, self.owner = fh, owner

    def read(self, size: int = -1) -> bytes:
        chunk = self.fh.read(size if size and size > 0 else 1 << 16)
        self.owner.count(len(chunk))
        return chunk


def _join(folder: str, target: str) -> str:
    parts = [p for p in folder.split("/") if p] if folder else []
    for piece in target.split("/"):
        if piece == "..":
            if parts:
                parts.pop()
        elif piece and piece != ".":
            parts.append(piece)
    return "/".join(parts)


def _docx(data: bytes) -> list:
    zf = _Zip(data)
    part = zf.main_part("word/document.xml")
    root = zf.xml(part)
    if root is None:
        raise Unreadable("broken", "找不到正文部件")
    body = next((el for el in root if _is_w(el, "body")), None)
    builder = _Builder()
    tables = [0]
    for item in _docx_blocks(body if body is not None else root):
        if isinstance(item, str):
            builder.para(item)
        else:
            tables[0] += 1
            for row in item:
                row.loc = {"table": tables[0], **row.loc}
            builder.table(item)
    return _Found(builder, *zf.media())


def _docx_blocks(parent) -> Iterator[str | list[_Row]]:
    """正文、内容控件、文本框里的块，按顺序：段落给文字，表格给行。"""
    for el in parent:
        if _is_w(el, "p"):
            text, boxes = _docx_para(el)
            yield text
            for box in boxes:
                yield from _docx_blocks(box)
        elif _is_w(el, "tbl"):
            yield _docx_table(el)
        elif _is_w(el, "sdt"):
            content = _w_child(el, "sdtContent")
            if content is not None:
                yield from _docx_blocks(content)
        elif _is_w(el, "customXml", "smartTag", "ins", "moveTo"):
            yield from _docx_blocks(el)
        elif _ns(el) == _MC and _local(el) == "AlternateContent":
            choice = _choice(el)
            if choice is not None:
                yield from _docx_blocks(choice)


def _docx_para(p) -> tuple[str, list]:
    out: list[str] = []
    boxes: list = []

    def visit(el, hidden: bool) -> None:
        for child in el:
            if not isinstance(child.tag, str):
                continue
            name, ns = _local(child), _ns(child)
            if ns == _MC and name == "AlternateContent":
                choice = _choice(child)
                if choice is not None:
                    visit(choice, hidden)
                continue
            if ns == _M:
                if name == "t" and not hidden:
                    out.append(child.text or "")
                else:
                    visit(child, hidden)
                continue
            if name == "txbxContent" and ns in _W_NS:
                boxes.append(child)
                continue
            if ns not in _W_NS:
                # 图形（w:drawing 里的 wp:、a:、wps: 等）：只找里面的文本框
                visit(child, hidden)
                continue
            if name in ("del", "moveFrom", "pPr", "rPr", "instrText", "delText", "delInstrText"):
                continue
            if name == "r":
                props = _w_child(child, "rPr")
                run_hidden = hidden or (
                    props is not None
                    and (_on(_w_child(props, "vanish")) or _on(_w_child(props, "specVanish")))
                )
                visit(child, run_hidden)
                continue
            if hidden:
                continue
            if name == "t":
                out.append(child.text or "")
            elif name in ("tab", "ptab"):
                out.append(" ")
            elif name == "br":
                out.append(" " if _w_attr(child, "type") in ("page", "column") else "\n")
            elif name == "cr":
                out.append("\n")
            elif name == "noBreakHyphen":
                out.append("-")
            elif name == "sym":
                code = _int((_w_attr(child, "char") or "").strip(), -1, 16)
                if 0 <= code < 0x100:  # 也有不带 F0 前缀的写法
                    code += 0xF000
                out.append(_PUA_SYMBOLS.get(code, ""))
            else:
                visit(child, hidden)

    visit(p, False)
    return "".join(out), boxes


def _docx_cell_text(tc) -> str:
    parts: list[str] = []
    for item in _docx_blocks(tc):
        if isinstance(item, str):
            parts.append(item)
        else:  # 单元格里套的表格：一行接成一句
            parts.extend(" | ".join(clean(c.text) for c in r.cells if clean(c.text)) for r in item)
    return "\n".join(p for p in parts if p.strip())


def _docx_rows(tbl) -> Iterator:
    for el in tbl:
        if _is_w(el, "tr"):
            yield el
        elif _is_w(el, "sdt"):
            content = _w_child(el, "sdtContent")
            if content is not None:
                yield from _docx_rows(content)
        elif _is_w(el, "customXml"):
            yield from _docx_rows(el)


def _docx_cells(tr) -> Iterator:
    for el in tr:
        if _is_w(el, "tc"):
            yield el
        elif _is_w(el, "sdt"):
            content = _w_child(el, "sdtContent")
            if content is not None:
                yield from _docx_cells(content)
        elif _is_w(el, "customXml"):
            yield from _docx_cells(el)


def _int(value: str | None, default: int, base: int = 10) -> int:
    try:
        return int(value, base) if value is not None else default
    except ValueError:
        return default


def _docx_table(tbl) -> list[_Row]:
    rows: list[_Row] = []
    for number, tr in enumerate(_docx_rows(tbl), 1):
        props = _w_child(tr, "trPr")
        col = 0
        header = False
        if props is not None:
            before = _w_child(props, "gridBefore")
            col = _int(_w_attr(before, "val"), 0) if before is not None else 0
            header = _on(_w_child(props, "tblHeader"))
        cells = []
        for tc in _docx_cells(tr):
            tc_props = _w_child(tc, "tcPr")
            span, merged = 1, False
            if tc_props is not None:
                grid = _w_child(tc_props, "gridSpan")
                span = max(1, _int(_w_attr(grid, "val"), 1)) if grid is not None else 1
                vmerge = _w_child(tc_props, "vMerge")
                merged = vmerge is not None and (_w_attr(vmerge, "val") or "continue") != "restart"
            cells.append(_Cell(col, "" if merged else _docx_cell_text(tc), span))
            col += span
        rows.append(_Row(cells, {"row": number}, header))
    return rows


# xlsx 内置的日期、时间格式编号（含中文地区的 27 到 36、50 到 58）
_DATE_FORMATS = {*range(14, 23), *range(27, 37), *range(45, 48), *range(50, 59)}
_FORMAT_NOISE = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')


def _date_format(code: str) -> bool:
    """自定义格式是不是日期时间：去掉引号里的字、转义字符和方括号里的设置后，还有 y、m、d、h、s。"""
    code = _FORMAT_NOISE.sub("", code).lower()
    return code != "general" and any(ch in code for ch in "ymdhs")


def _sheet_number(value: str, date_style: bool, percent: bool, epoch: datetime) -> str:
    """单元格里存的数：日期、时间、百分比按格式写；整数照原样写出——按十进制精确读，不经过浮点数
    （存成数字的 18 位卡号、身份证号转成浮点会改掉末几位），写成科学计数法的也展开。"""
    try:
        exact = Decimal(value.strip())
    except InvalidOperation:
        return value
    if not exact.is_finite():
        return value
    number = float(exact)
    if date_style and 0 <= number < 2958466:
        # 按秒取整：17:30 存成 0.72916666… 天，直接换算会变成 17:29:59.999
        moment = epoch + timedelta(seconds=round(number * 86400))
        if number < 1:
            return moment.strftime("%H:%M")
        if moment.hour or moment.minute:
            return moment.strftime("%Y-%m-%d %H:%M")
        return moment.strftime("%Y-%m-%d")
    if percent:
        return f"{number * 100:.10g}%"
    if exact == exact.to_integral_value():
        # 整数一律原样写出：存成数字的卡号、身份证号写成科学计数法，遮号码就认不出了
        # 指数写法可能只有几个字却展开成巨量零；必须在格式化分配内存前检查长度。
        digits = 1 if exact.is_zero() else max(1, exact.adjusted() + 1)
        if digits + int(exact.is_signed()) > MAX_CHARS:
            raise Unreadable("too_large", "单元格数字展开后超过文字上限")
        return f"{exact.to_integral_value():f}"
    return f"{number:.10g}"


def _col_index(ref: str) -> int:
    col = 0
    for ch in ref:
        if not ch.isalpha():
            break
        col = col * 26 + (ord(ch.upper()) - 64)
    return col - 1


def _xlsx(data: bytes) -> list:
    zf = _Zip(data)
    part = zf.main_part("xl/workbook.xml")
    book = zf.xml(part)
    if book is None:
        raise Unreadable("broken", "找不到工作簿部件")
    rels = zf.rels(part)
    ns_main = etree.QName(book).namespace
    epoch = datetime(1899, 12, 30)
    props = book.find(f"{{{ns_main}}}workbookPr")
    if props is not None and props.get("date1904") in ("1", "true"):
        epoch = datetime(1904, 1, 1)
    shared = _shared_strings(zf, rels)
    styles = _cell_styles(zf, rels)
    builder = _Builder()
    sheets = book.find(f"{{{ns_main}}}sheets")
    for sheet in sheets if sheets is not None else []:
        if sheet.get("state") in ("hidden", "veryHidden"):
            continue
        rid = next((sheet.get(f"{{{ns}}}id") for ns in _R_NS if sheet.get(f"{{{ns}}}id")), None)
        target = rels.get(rid or "")
        if not target:
            continue
        name = sheet.get("name") or ""
        rows = _xlsx_sheet(zf, target, shared, styles, epoch)
        for row in rows:
            row.loc = {"sheet": name, **row.loc}
        builder.table(rows)
    return _Found(builder, *zf.media())


# Excel 把 XML 里不能写的字写成 _xHHHH_（回车是 _x000D_，"_x" 本身写成 _x005F_x）
_XLSX_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _unescape(text: str) -> str:
    return _XLSX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text) if "_x" in text else text


def _si_text(si) -> str:
    """共享字符串：连起各段文字，不要注音（rPh）。"""
    out = []
    for el in si.iter():
        if _local(el) == "t" and _local(el.getparent()) != "rPh":
            out.append(el.text or "")
    return _unescape("".join(out))


def _shared_strings(zf: _Zip, rels: dict[str, str]) -> list[str]:
    """共享字符串（边读边解析）。合计超过 MAX_CHARS 的两倍就不往下读了：表格本身也一定超。"""
    target = next((t for t in rels.values() if t.endswith("sharedStrings.xml")), None)
    out: list[str] = []
    chars = 0
    for _event, el in zf.iterparse(target) if target else ():
        if _local(el) == "si":
            text = _si_text(el)
            out.append(text)
            chars += len(text)
            if chars > 2 * MAX_CHARS:
                raise Unreadable("too_large", f"取出的文字超过 {MAX_CHARS // 10000} 万字")
            el.clear()
    return out


def _cell_styles(zf: _Zip, rels: dict[str, str]) -> list[tuple[bool, bool]]:
    """每个单元格样式：(是日期时间格式, 是百分比格式)。"""
    target = next((t for t in rels.values() if t.endswith("styles.xml")), None)
    root = zf.xml(target) if target else None
    if root is None:
        return []
    custom = {}
    for el in root.iter():
        if _local(el) == "numFmt":
            custom[_int(el.get("numFmtId"), -1)] = el.get("formatCode") or ""
    out = []
    xfs = next((el for el in root if _local(el) == "cellXfs"), None)
    for xf in xfs if xfs is not None else []:
        fmt = _int(xf.get("numFmtId"), 0)
        code = custom.get(fmt, "")
        is_date = fmt in _DATE_FORMATS or (fmt in custom and _date_format(code))
        out.append((is_date, fmt in (9, 10) or "%" in code))
    return out


def _xlsx_sheet(zf, target, shared, styles, epoch) -> list[_Row]:
    hidden_cols: set[int] = set()
    rows: list[_Row] = []
    merges: list[tuple[int, int, int, int]] = []
    chars = 0
    number = 0  # 行号：有 r 属性按它，没有的接着上一行数（单元格的列同理）
    for _event, el in zf.iterparse(target):
        name = _local(el)
        if name == "col" and el.get("hidden") in ("1", "true"):
            for col in range(_int(el.get("min"), 1), _int(el.get("max"), 0) + 1):
                hidden_cols.add(col - 1)
        elif name == "row":
            number = _int(el.get("r"), number + 1)
            if el.get("hidden") not in ("1", "true"):
                cells, col = [], -1
                for c in el:
                    if _local(c) != "c":
                        continue
                    ref = c.get("r")
                    col = _col_index(ref) if ref else col + 1
                    if col < 0 or col in hidden_cols:
                        continue
                    text = _xlsx_value(c, shared, styles, epoch)
                    if text.strip():
                        cells.append(_Cell(col, text))
                        chars += len(text)
                if cells:
                    rows.append(_Row(cells, {"row": number}))
                if chars > MAX_CHARS:
                    raise Unreadable("too_large", f"取出的文字超过 {MAX_CHARS // 10000} 万字")
            el.clear()
        elif name == "mergeCell":
            ref = el.get("ref") or ""
            if ":" in ref:
                a, b = ref.split(":", 1)
                merges.append((_row_index(a), _row_index(b), _col_index(a), _col_index(b)))
    if merges:
        spans = {(r1, c1): c2 - c1 + 1 for r1, _r2, c1, c2 in merges}
        for row in rows:
            for cell in row.cells:
                cell.span = spans.get((row.loc["row"], cell.col), 1)
    return rows


def _row_index(ref: str) -> int:
    digits = re.sub(r"\D", "", ref)
    return int(digits) if digits else 0


def _xlsx_value(c, shared, styles, epoch) -> str:
    kind = c.get("t") or "n"
    value = None
    inline = None
    for child in c:
        name = _local(child)
        if name == "v":
            value = child.text or ""
        elif name == "is":
            inline = _si_text(child)
    if kind == "inlineStr":
        return inline or ""
    if value is None:
        return ""
    if kind == "s":
        index = _int(value, -1)
        return shared[index] if 0 <= index < len(shared) else ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    if kind in ("str", "e"):
        return _unescape(value)
    if kind == "d":  # ISO 日期
        return value.removesuffix("T00:00:00").replace("T", " ")[:16]
    style = _int(c.get("s"), 0)
    is_date, percent = styles[style] if 0 <= style < len(styles) else (False, False)
    return _sheet_number(value, is_date, percent, epoch)


def _pptx(data: bytes) -> list:
    zf = _Zip(data)
    part = zf.main_part("ppt/presentation.xml")
    pres = zf.xml(part)
    if pres is None:
        raise Unreadable("broken", "找不到演示文稿部件")
    rels = zf.rels(part)
    builder = _Builder()
    slide_ids = [el for el in pres.iter() if _local(el) == "sldId"]
    for number, sld in enumerate(slide_ids, 1):
        rid = next((sld.get(f"{{{ns}}}id") for ns in _R_NS if sld.get(f"{{{ns}}}id")), None)
        target = rels.get(rid or "")
        slide = zf.xml(target) if target else None
        if slide is None or slide.get("show") in ("0", "false"):
            continue
        tables = 0
        for item in _pptx_items(slide):
            if isinstance(item, str):
                builder.para(item, {"slide": number})
            else:
                tables += 1
                for row in item:
                    row.loc = {"slide": number, "table": tables, **row.loc}
                builder.table(item)
    return _Found(builder, *zf.media())


def _a_para(p) -> str:
    out = []
    for el in p.iter():
        name = _local(el)
        if name == "t" and _ns(el) == _A:
            out.append(el.text or "")
        elif name == "br" and _ns(el) == _A:
            out.append("\n")
    return "".join(out)


def _pptx_items(slide) -> Iterator[str | list[_Row]]:
    tree = next((el for el in slide.iter() if _local(el) == "spTree"), None)
    if tree is None:
        return

    def walk(parent) -> Iterator[str | list[_Row]]:
        for el in parent:
            name = _local(el)
            if _ns(el) == _MC and name == "AlternateContent":
                choice = _choice(el)
                if choice is not None:
                    yield from walk(choice)
            elif name == "sp":
                body = next((c for c in el.iter() if _local(c) == "txBody"), None)
                if body is not None:
                    for p in body:
                        if _local(p) == "p" and _ns(p) == _A:
                            yield _a_para(p)
            elif name == "grpSp":
                yield from walk(el)
            elif name == "graphicFrame":
                tbl = next((c for c in el.iter() if _local(c) == "tbl" and _ns(c) == _A), None)
                if tbl is not None:
                    yield _pptx_table(tbl)

    yield from walk(tree)


def _pptx_table(tbl) -> list[_Row]:
    rows = []
    for number, tr in enumerate((el for el in tbl if _local(el) == "tr"), 1):
        cells, col = [], 0
        for tc in (el for el in tr if _local(el) == "tc"):
            span = max(1, _int(tc.get("gridSpan"), 1))
            merged = tc.get("hMerge") in ("1", "true") or tc.get("vMerge") in ("1", "true")
            text = "\n".join(_a_para(p) for p in tc.iter() if _local(p) == "p" and _ns(p) == _A)
            cells.append(_Cell(col, "" if merged else text, span))
            col += span
        rows.append(_Row(cells, {"row": number}))
    return rows


# ---- PDF ----


def _pdf(data: bytes) -> list:
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 第三方解析器对损坏、加密文件会抛多种异常
        raise Unreadable("broken", "PDF 打不开（损坏或加密）") from exc
    items: list[tuple] = []  # ("line", 文字, 页) 或 ("table", 行, 页)
    scanned = 0
    with pdf:
        pages = len(pdf.pages)
        if pages > _PDF_PAGES:
            raise Unreadable("too_large", f"PDF 超过 {_PDF_PAGES} 页")
        for number, page in enumerate(pdf.pages, 1):
            try:
                found = _pdf_page(page)
            finally:
                page.close()  # 释放这一页解析出的对象
            lines, tables, is_scan = found
            scanned += is_scan
            entries = [(top, "line", text) for top, text in lines]
            entries += [(top, "table", rows) for top, rows in tables]
            entries.sort(key=lambda e: e[0])
            items += [(kind, value, number) for _top, kind, value in entries]
    builder = _Builder()
    texts = [value for kind, value, _ in items if kind == "line"]
    width = line_width("\n".join(texts))
    tables = 0
    last: dict | None = None  # 上一张表：竖线位置、表号、行数、表头、标签列
    current, current_page, physical = "", None, ""
    for index, (kind, value, number) in enumerate(items):
        if kind == "table":
            if current:
                builder.para(current, {"page": current_page})
                current, physical = "", ""
            value, xs = value
            # 跨页的续表：上一页最后是一张表、这一页一开始就是表、竖线位置一样（差 2pt 以内）。
            # 不看第一行像不像表头：续表第一行常常就是没有数字的数据
            follows = (
                last is not None
                and index > 0
                and items[index - 1][0] == "table"
                and items[index - 1][2] == number - 1
                and len(last["xs"]) == len(xs)
                and all(abs(a - b) <= 2 for a, b in zip(last["xs"], xs, strict=True))
            )
            first_row = " | ".join(t for c in value[0].cells if (t := clean(c.text)))
            # Word"标题行重复"导出的 PDF：每页开头重复上一页的表头，记成表头行
            repeated = follows and bool(last["head_text"]) and mask(first_row) == last["head_text"]
            if not follows:
                tables += 1
                last = {
                    "xs": xs,
                    "number": tables,
                    "rows": 0,
                    "headers": None,
                    "head_text": "",
                    "columns": {},
                }
            for row in value:
                row.loc = {
                    "page": number,
                    "table": last["number"],
                    "row": last["rows"] + row.loc["row"],
                }
            last["rows"] += len(value)
            headers, head_text, columns = builder.table(
                value,
                headers=last["headers"] if follows else None,
                repeated=repeated,
                columns=last["columns"],
            )
            last["headers"], last["columns"] = headers, columns
            last["head_text"] = last["head_text"] or head_text
            continue
        if current and layout_wrapped(physical, value, width):
            current = current.rstrip() + value.lstrip()
        else:
            if current:
                builder.para(current, {"page": current_page})
            current, current_page = value, number
        physical = value
    if current:
        builder.para(current, {"page": current_page})
    return _Found(builder, pages=pages, scanned=scanned)


_OCR_FONT = re.compile(r"OCR|GlyphLess", re.I)
_INVISIBLE = re.compile(rb"(?<![\d.])3\s+Tr\b")


def _ocr_layer(page) -> bool:
    """这页的字是不是扫描件上看不见的识别层：字体名带 OCR、GlyphLess（Acrobat、Tesseract
    的识别层），或者页面内容把文字设成不可见（3 Tr，ABBYY 等识别软件这样写）。"""
    if any(_OCR_FONT.search(c.get("fontname") or "") for c in page.chars):
        return True
    from pdfminer.pdftypes import resolve1

    try:
        return any(
            _INVISIBLE.search(resolve1(stream).get_data())
            for stream in page.page_obj.contents or []
        )
    except Exception:  # noqa: BLE001 - 内容流解不开就按看得见的字算
        return False


def _pdf_page(page) -> tuple[list, list, bool]:
    """一页：表格外的文字行（去掉页码行）、表格（按行按格）、是不是扫描页。

    扫描页：几乎没有文字、图片占了三成以上的页面；或者整页（九成以上）被图片盖住、上面的字是
    看不见的识别层（扫描件自动识别出的字，错字多，不取）。只有几个字加一枚公章、一个徽标的页
    （"特此通知"），整页底图上是正常文字的页（演示文稿导出的 PDF），字照样取。"""
    images = page.images
    if images:
        x0, top, x1, bottom = page.bbox
        cover = sum(
            max(0.0, min(im["x1"], x1) - max(im["x0"], x0))
            * max(0.0, min(im["bottom"], bottom) - max(im["top"], top))
            for im in images
        )
        area = page.width * page.height
        if len(page.chars) < _SCAN_PAGE_CHARS and cover >= area * _SCAN_PAGE_SOME_COVER:
            return [], [], True
        if cover >= area * _SCAN_PAGE_COVER and _ocr_layer(page):
            return [], [], True
    if not page.chars and len(page.curves) >= _OUTLINED_CURVES:
        return [], [], True  # 字被转成了线条（导出时"文字转曲"），取不出
    page = page.dedupe_chars()  # 为了加粗把同一个字重画一遍的，只留一个
    try:
        # 线条特别多的页（图纸、图表）找表格可能很慢：不找，只取文字行（9/28 数据里最多 1200 条）
        found = page.find_tables() if len(page.edges) <= _PDF_MAX_EDGES else []
    except Exception:  # noqa: BLE001 - 找表格失败就当没有表格，照样取文字
        found = []
    boxes = [t.bbox for t in found]

    def outside(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        x = (obj["x0"] + obj["x1"]) / 2
        y = (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)

    rest = page.filter(outside) if boxes else page
    lines = []
    top_margin, bottom_margin = page.height * _MARGIN, page.height * (1 - _MARGIN)
    for line in rest.extract_text_lines(layout=False, strip=True, return_chars=False):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        in_margin = line["top"] < top_margin or line["bottom"] > bottom_margin
        if in_margin and _PAGE_NUMBER.fullmatch(text):
            continue  # 页眉页脚里的页码；正文里单独一行的数字（"2025"）照留
        lines.append((line["top"], text))
    tables = []
    for table in found:
        rows = []
        for number, values in enumerate(table.extract(), 1):
            cells: list[_Cell] = []
            for col, value in enumerate(values):
                if value is None:  # 合并单元格占的位置
                    if cells and cells[-1].col + cells[-1].span == col:
                        cells[-1].span += 1
                    continue
                cells.append(_Cell(col, _join_lines(value.split("\n"))))
            rows.append(_Row(cells, {"row": number}))
        # 表格竖线的位置（取整到 1pt）：下一页开头的表格竖线一样，才是续表
        xs = tuple(sorted({round(c[0]) for c in table.cells} | {round(c[2]) for c in table.cells}))
        tables.append((table.bbox[1], (rows, xs)))
    return lines, tables, False


# ---- 旧版 Word、WPS（复合文档里的 WordDocument 流，[MS-DOC]） ----

_DOC_MIN_FIB = 0x00C1  # Word 97 及以后；更早的 Word 6/95 格式不读


def _doc(data: bytes) -> list:
    import olefile

    with olefile.OleFileIO(data) as ole:
        word = ole.openstream("WordDocument").read()
        if len(word) < 0x200:
            raise Unreadable("broken", "WordDocument 数据流不完整")
        nfib, flags = struct.unpack_from("<H", word, 2)[0], struct.unpack_from("<H", word, 0x0A)[0]
        if nfib < _DOC_MIN_FIB:
            raise Unreadable("format", "Word 95 及更早的格式")
        if flags & 0x0100:
            raise Unreadable("broken", "加密的 Word 文件")
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise Unreadable("broken", "缺少 Table 数据流")
        table = ole.openstream(table_name).read()
        # 图片存在 Data 流里：它的大小当作图片合计大小
        picture_bytes = ole.get_size("Data") if ole.exists("Data") else 0
    fib = _Fib(word)
    builder = _Builder()
    table_rows = _DocTable(builder)
    paragraphs, pictures = _doc_paragraphs(word, table, fib, _pieces(table, fib))
    for text, mark, props in paragraphs:
        if props is not None and (props.get("in_table") or props.get("itap", 0) >= 1):
            table_rows.add(text, mark, props)
        else:
            table_rows.close()
            builder.para(text)
    table_rows.close()
    return _Found(builder, pictures, picture_bytes)


class _DocTable:
    """旧版 Word 的表格：单元格以 0x07 结尾，一行以带"行尾"属性（TTP）的 0x07 结尾；表格里套的
    表格（itap ≥ 2）单元格以带"内层单元格"属性的段落标记结尾、行以带"内层行尾"属性的结尾，
    接成一句放进外面那一格（[MS-DOC] 2.4.3）。"""

    def __init__(self, builder: _Builder) -> None:
        self.builder = builder
        self.count = 0
        self.rows: list[_Row] = []
        self.cells: list[_Cell] = []
        self.parts: list[str] = []
        self.inner_cells: list[str] = []
        self.inner_parts: list[str] = []

    def add(self, text: str, mark: str, props: dict) -> None:
        if (props.get("itap", 1) or 1) > 1:
            if props.get("inner_ttp"):
                self._inner_row()
            elif props.get("inner_cell"):
                self.inner_parts.append(text)
                self.inner_cells.append(" ".join(p for p in self.inner_parts if p.strip()))
                self.inner_parts = []
            else:
                self.inner_parts.append(text)
            return
        self._inner_row()
        if mark != "\x07":
            self.parts.append(text)  # 单元格里的一段
        elif props.get("ttp"):
            self._row()
        else:
            self.parts.append(text)
            self.cells.append(_Cell(len(self.cells), "\n".join(p for p in self.parts if p.strip())))
            self.parts = []

    def _inner_row(self) -> None:
        if self.inner_parts:
            self.inner_cells.append(" ".join(p for p in self.inner_parts if p.strip()))
            self.inner_parts = []
        if self.inner_cells:
            self.parts.append(" | ".join(c for c in self.inner_cells if c))
            self.inner_cells = []

    def _row(self) -> None:
        if self.parts:  # 没有结尾标记的最后一格（文件不规范时）
            self.cells.append(_Cell(len(self.cells), "\n".join(p for p in self.parts if p.strip())))
            self.parts = []
        if self.cells:
            self.rows.append(_Row(self.cells, {}))
            self.cells = []

    def close(self) -> None:
        self._inner_row()
        self._row()
        if self.rows:
            self.count += 1
            for index, row in enumerate(self.rows, 1):
                row.loc = {"table": self.count, "row": index}
            self.builder.table(self.rows)
            self.rows = []


class _Fib:
    """FIB 里要用的几项：正文字数（ccpText），Clx、字符属性表（PlcBteChpx）和段落属性表
    （PlcBtePapx）在 Table 流里的位置。"""

    def __init__(self, word: bytes) -> None:
        pos = 0x20
        csw = struct.unpack_from("<H", word, pos)[0]
        pos += 2 + csw * 2
        cslw = struct.unpack_from("<H", word, pos)[0]
        lw = pos + 2
        self.ccp_text = struct.unpack_from("<i", word, lw + 3 * 4)[0]
        pos = lw + cslw * 4
        count = struct.unpack_from("<H", word, pos)[0]
        blob = pos + 2
        if count < 34:
            raise Unreadable("broken", "FIB 不完整")

        def pair(index: int) -> tuple[int, int]:
            return struct.unpack_from("<II", word, blob + index * 8)

        self.fc_clx, self.lcb_clx = pair(33)
        self.fc_chpx, self.lcb_chpx = pair(12)
        self.fc_papx, self.lcb_papx = pair(13)


@dataclass
class _Piece:
    cp_start: int
    cp_end: int
    offset: int  # 在 WordDocument 流里的字节位置
    compressed: bool  # 一个字一个字节（cp1252）；否则 UTF-16LE 两个字节


def _pieces(table: bytes, fib: _Fib) -> list[_Piece]:
    """文字分块表（Clx 里的 PlcPcd）：正文按字号分成几块，各在 WordDocument 流的哪里。
    分块上的属性修改（Prm，快速保存才有，9/28 数据里没有）不看。"""
    clx = table[fib.fc_clx : fib.fc_clx + fib.lcb_clx]
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:  # Prc：跳过
        pos += 3 + struct.unpack_from("<h", clx, pos + 1)[0]
    if pos >= len(clx) or clx[pos] != 0x02:
        raise Unreadable("broken", "文字分块表（Clx）损坏")
    lcb = struct.unpack_from("<I", clx, pos + 1)[0]
    plc = clx[pos + 5 : pos + 5 + lcb]
    n = (lcb - 4) // 12
    if n <= 0 or len(plc) < lcb:
        raise Unreadable("broken", "文字分块表（PlcPcd）损坏")
    cps = struct.unpack_from(f"<{n + 1}i", plc, 0)
    pieces = []
    for i in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
        compressed = bool(fc & 0x40000000)
        fc &= 0x3FFFFFFF
        pieces.append(_Piece(cps[i], cps[i + 1], fc // 2 if compressed else fc, compressed))
    return pieces


def _fkp_pages(word: bytes, plc: bytes):
    """PlcBteChpx / PlcBtePapx 指向的一页页 FKP（每页 512 字节，最后一个字节是条数）：
    逐页给 (这一页, 条数, 各条的起止字节位置)。"""
    n = (len(plc) - 4) // 8
    if n <= 0:
        return
    for pn in struct.unpack_from(f"<{n}I", plc, 4 * (n + 1)):
        page = word[(pn & 0x3FFFFF) * 512 : (pn & 0x3FFFFF) * 512 + 512]
        if len(page) < 512:
            continue
        crun = page[511]
        yield page, crun, struct.unpack_from(f"<{crun + 1}I", page, 0)


def _papx_runs(word: bytes, table: bytes, fib: _Fib) -> list[tuple[int, int, dict]]:
    """段落属性：[(起始字节, 结束字节, {in_table, ttp, itap, inner_cell, inner_ttp})]，
    按起始字节排好。"""
    runs = []
    for page, crun, fcs in _fkp_pages(word, table[fib.fc_papx : fib.fc_papx + fib.lcb_papx]):
        for i in range(crun):
            offset = page[4 * (crun + 1) + 13 * i] * 2
            props: dict = {}
            if offset:
                cb = page[offset]
                if cb:
                    grp = page[offset + 1 : offset + 1 + 2 * cb - 1]
                else:
                    grp = page[offset + 2 : offset + 2 + 2 * page[offset + 1]]
                for sprm, operand in _sprm_items(grp[2:]):  # 前两个字节是样式编号
                    if sprm in _PARA_SPRMS and operand:
                        props[_PARA_SPRMS[sprm]] = (
                            struct.unpack("<i", operand)[0] if sprm == _SPRM_ITAP else operand[0]
                        )
            runs.append((fcs[i], fcs[i + 1], props))
    runs.sort(key=lambda r: r[0])
    return runs


def _hidden_runs(word: bytes, table: bytes, fib: _Fib) -> list[tuple[int, int]]:
    """看不见的字：字符属性里标了修订删除（sprmCFRMarkDel）或隐藏（sprmCFVanish、
    sprmCFSpecVanish）的 [(起始字节, 结束字节)]，按起始字节排好。样式里定的隐藏不看。"""
    runs = []
    for page, crun, fcs in _fkp_pages(word, table[fib.fc_chpx : fib.fc_chpx + fib.lcb_chpx]):
        for i in range(crun):
            offset = page[4 * (crun + 1) + i] * 2
            if not offset:
                continue
            grp = page[offset + 1 : offset + 1 + page[offset]]
            if any(
                sprm in _HIDDEN_SPRMS and operand and operand[0] in (0x01, 0x81)
                for sprm, operand in _sprm_items(grp)
            ):
                runs.append((fcs[i], fcs[i + 1]))
    runs.sort()
    return runs


# 段落属性里要用的几个（[MS-DOC] 2.6.2）
_SPRM_ITAP = 0x6649
_PARA_SPRMS = {
    0x2416: "in_table",
    0x2417: "ttp",
    _SPRM_ITAP: "itap",
    0x244B: "inner_cell",
    0x244C: "inner_ttp",
}
# 字符属性：修订删除、隐藏、特殊隐藏（[MS-DOC] 2.6.1；操作数 1 是开，0x81 是"和样式相反"，
# 样式一般不隐藏，也按开算）
_HIDDEN_SPRMS = (0x0800, 0x083C, 0x0818)


def _sprm_items(grp: bytes):
    """属性修改列表（grpprl）里逐个给 (sprm, 操作数)。操作数长度按 sprm 的 spra 位算
    （[MS-DOC] 2.2.5.1）。"""
    pos = 0
    while pos + 2 <= len(grp):
        sprm = struct.unpack_from("<H", grp, pos)[0]
        pos += 2
        spra = sprm >> 13
        if spra in (0, 1):
            size = 1
        elif spra in (2, 4, 5):
            size = 2
        elif spra == 3:
            size = 4
        elif spra == 7:
            size = 3
        elif sprm == 0xD608 and pos + 2 <= len(grp):  # sprmTDefTable：前两个字节是长度
            size = struct.unpack_from("<H", grp, pos)[0] + 1
        elif sprm == 0xC615 and pos < len(grp):  # sprmPChgTabs
            if grp[pos] == 255:
                return
            size = 1 + grp[pos]
        elif pos < len(grp):
            size = 1 + grp[pos]
        else:
            return
        operand = grp[pos : pos + size]
        pos += size
        if len(operand) < size:
            return
        yield sprm, operand


def _doc_paragraphs(word, table, fib, pieces) -> tuple[list[tuple[str, str, dict | None]], int]:
    """正文按段落标记切开：([(段落文字, 结尾标记, 段落属性)], 图片个数)。结尾标记是 0x0D（段落）、
    0x07（单元格或行尾）或 0x0C（分页、分节）。域只留结果；修订删除、隐藏的字不取；图片（0x01、
    0x08 占位）去掉、只计数（只有图片没有字时算扫描件）。

    字的位置按 UTF-16 码元算：扩展区汉字（如"𫐄"）占两个码元、4 个字节，按解码后的字数算
    会让后面的段落标记对不上段落属性。"""
    try:
        runs = _papx_runs(word, table, fib)
    except (struct.error, IndexError):
        runs = []
    try:
        hidden = _hidden_runs(word, table, fib)
    except (struct.error, IndexError):
        hidden = []
    starts = [r[0] for r in runs]
    hidden_starts = [r[0] for r in hidden]

    def props_at(fc: int) -> dict | None:
        i = bisect.bisect_right(starts, fc) - 1
        if i >= 0 and runs[i][0] <= fc < runs[i][1]:
            return runs[i][2]
        return None

    def is_hidden(fc: int) -> bool:
        i = bisect.bisect_right(hidden_starts, fc) - 1
        return i >= 0 and hidden[i][0] <= fc < hidden[i][1]

    out: list[tuple[str, str, dict | None]] = []
    buf: list[str] = []
    pictures = 0
    fields: list[bool] = []  # 每层域：True 表示已经到了结果部分
    limit = fib.ccp_text
    for piece in pieces:
        if piece.cp_start >= limit:
            break
        count = min(piece.cp_end, limit) - piece.cp_start
        if count <= 0:
            continue
        for fc, ch in _piece_chars(word, piece, count):
            if ch == "\x13":
                fields.append(False)
                continue
            if ch == "\x14":
                if fields:
                    fields[-1] = True
                continue
            if ch == "\x15":
                if fields:
                    fields.pop()
                continue
            if fields and not all(fields):
                continue  # 域代码，不是看得见的字
            if ch in "\r\x07\x0c":
                props = props_at(fc) if runs else None
                if runs and ch == "\x07" and props is None:
                    props = {"in_table": 1}
                out.append(("".join(buf), ch, props))
                buf = []
            elif hidden and is_hidden(fc):
                continue  # 修订删除、隐藏的字
            elif ch in "\x0b\x0e":
                buf.append("\n")
            elif ch == "\x1e":
                buf.append("-")
            elif ch in "\x01\x08":
                pictures += 1
            elif ord(ch) < 0x20 and ch not in "\t\n":
                continue
            else:
                buf.append(ch)
    if buf:
        out.append(("".join(buf), "\r", None))
    if not runs:
        # 段落属性表读不出：表格结构没法分，单元格当段落
        out = [(text, "\r", None) for text, _mark, _props in out]
    return out, pictures


def _piece_chars(word: bytes, piece: _Piece, count: int) -> Iterator[tuple[int, str]]:
    """一个分块里的字：(在 WordDocument 流里的字节位置, 字)。压缩的分块一个字一个字节（cp1252），
    其余按 UTF-16LE 一个码元两个字节，代理对（扩展区的字）合成一个字、位置按第一个码元。"""
    if piece.compressed:
        raw = word[piece.offset : piece.offset + count]
        for i, ch in enumerate(raw.decode("cp1252", "replace")):
            yield piece.offset + i, ch
        return
    raw = word[piece.offset : piece.offset + 2 * count]
    units = struct.unpack(f"<{len(raw) // 2}H", raw[: len(raw) // 2 * 2])
    i = 0
    while i < len(units):
        unit = units[i]
        if 0xD800 <= unit <= 0xDBFF and i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF:
            yield (
                piece.offset + 2 * i,
                chr(0x10000 + ((unit - 0xD800) << 10) + units[i + 1] - 0xDC00),
            )
            i += 2
        else:
            yield piece.offset + 2 * i, chr(unit) if not 0xD800 <= unit <= 0xDFFF else "\ufffd"
            i += 1


# ---- 旧版 Excel、WPS 表格（xlrd） ----


def _xls(data: bytes) -> list:
    import xlrd

    try:
        book = xlrd.open_workbook(
            file_contents=data, formatting_info=True, on_demand=True, logfile=io.StringIO()
        )
    except xlrd.XLRDError as exc:
        if "encrypt" in str(exc).lower():
            raise Unreadable("broken", "加密的表格文件") from exc
        book = xlrd.open_workbook(file_contents=data, on_demand=True, logfile=io.StringIO())
    builder = _Builder()
    for index in range(book.nsheets):
        sheet = book.sheet_by_index(index)
        if getattr(sheet, "visibility", 0):
            book.unload_sheet(index)
            continue
        spans = {(r1, c1): c2 - c1 for r1, _r2, c1, c2 in getattr(sheet, "merged_cells", [])}
        rows = []
        chars = 0
        for r in range(sheet.nrows):
            info = sheet.rowinfo_map.get(r) if hasattr(sheet, "rowinfo_map") else None
            if info is not None and getattr(info, "hidden", 0):
                continue
            cells = []
            for c in range(sheet.ncols):
                col_info = sheet.colinfo_map.get(c) if hasattr(sheet, "colinfo_map") else None
                if col_info is not None and getattr(col_info, "hidden", 0):
                    continue
                text = _xls_value(book, sheet.cell(r, c))
                if text.strip():
                    cells.append(_Cell(c, text, max(1, spans.get((r, c), 1))))
                    chars += len(text)
            if cells:
                rows.append(_Row(cells, {"sheet": sheet.name, "row": r + 1}))
            if chars > MAX_CHARS:
                raise Unreadable("too_large", f"取出的文字超过 {MAX_CHARS // 10000} 万字")
        builder.table(rows)
        book.unload_sheet(index)
    return _Found(builder)


def _xls_value(book, cell) -> str:
    import xlrd

    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            moment = xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
            return str(cell.value)
        if cell.value < 1:
            return moment.strftime("%H:%M")
        return moment.strftime("%Y-%m-%d %H:%M" if moment.hour or moment.minute else "%Y-%m-%d")
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "")
    percent = False
    try:
        xf = book.xf_list[cell.xf_index]
        percent = "%" in book.format_map[xf.format_key].format_str
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    return _sheet_number(str(cell.value), False, percent, datetime(1899, 12, 30))


# ---- 旧版 PowerPoint ----


def _ppt(data: bytes) -> list:
    """旧版 PowerPoint（[MS-PPT]）：占位符里的字在文字列表（SlideListWithText，实例 0）里，按
    幻灯片顺序，每张从 SlidePersistAtom 开始；不在占位符里的文本框，字在各张幻灯片（Slide 容器）的
    绘图里，按幻灯片容器出现的顺序接在那张后面。母版、备注里的字不取。"""
    import olefile

    with olefile.OleFileIO(data) as ole:
        stream = ole.openstream("PowerPoint Document").read()
        picture_bytes = ole.get_size("Pictures") if ole.exists("Pictures") else 0
    slides: list[list[str]] = []
    boxes: list[list[str]] = []

    def text_of(kind: int, body: int, stop: int) -> list[str]:
        raw = stream[body:stop]
        text = raw.decode("utf-16-le" if kind == 0x0FA0 else "latin-1", "replace")
        return text.replace("\x0b", "\n").split("\r")

    def walk(start: int, end: int, where: str) -> None:
        pos = start
        while pos + 8 <= end:
            ver_inst, kind, size = struct.unpack_from("<HHI", stream, pos)
            body, stop = pos + 8, min(pos + 8 + size, end)
            if ver_inst & 0xF == 0xF:  # 容器
                if kind == 0x0FF0:  # SlideListWithText：实例 0 是幻灯片，1 母版，2 备注
                    if ver_inst >> 4 == 0:
                        walk(body, stop, "list")
                elif kind == 0x03EE:  # Slide
                    boxes.append([])
                    walk(body, stop, "slide")
                elif kind not in (0x03F8, 0x03F0):  # 母版、备注不看
                    walk(body, stop, where)
            elif where == "list" and kind == 0x03F3:  # SlidePersistAtom：下一张幻灯片
                slides.append([])
            elif kind in (0x0FA0, 0x0FA8):  # TextCharsAtom、TextBytesAtom
                if where == "list" and slides:
                    slides[-1].extend(text_of(kind, body, stop))
                elif where == "slide":
                    boxes[-1].extend(text_of(kind, body, stop))
            pos = stop if stop > pos else end

    walk(0, len(stream), "")
    builder = _Builder()
    for number in range(1, max(len(slides), len(boxes)) + 1):
        texts = (slides[number - 1] if number <= len(slides) else []) + (
            boxes[number - 1] if number <= len(boxes) else []
        )
        for text in texts:
            builder.para(text, {"slide": number})
    return _Found(builder, int(picture_bytes > 0), picture_bytes)


_EXTRACTORS = {
    "docx": _docx,
    "xlsx": _xlsx,
    "pptx": _pptx,
    "pdf": _pdf,
    "doc": _doc,
    "xls": _xls,
    "ppt": _ppt,
}


# 取一份文件最多用这么多秒（极端的 PDF 可能解析很久）：到了按"太大"记，附件任务不至于卡住
EXTRACT_SECONDS = 180


@contextmanager
def _time_limit(seconds: int) -> Iterator[None]:
    """限时（SIGALRM）：只在主线程里限，admin 的定时任务和命令行都在主线程；别的线程里不限。
    到时在解析代码里抛 Unreadable；一次跑很久的 C 代码（lxml 解析一整个部件）要等它返回才停，
    所以部件大小另有上限（_TREE_MAX）。"""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(_signum, _frame):
        raise Unreadable("too_large", f"取文字超过 {seconds} 秒")

    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# 字很少、图很大的：多半是扫描件加了个标题（"附件2"加一整页扫描图），也按扫描件记
_FEW_CHARS = 20
_BIG_PICTURES = 150 * 1024


def extract(data: bytes) -> Extraction:
    """取出给 AI 用的文字。取不出时 Extraction.reason 写原因（不抛异常）。"""
    try:
        file_type = sniff(data)
    except Unreadable as exc:
        return Extraction(None, reason=exc.reason, detail=exc.detail)
    try:
        with _time_limit(EXTRACT_SECONDS):
            found = _EXTRACTORS[file_type](data)
    except Unreadable as exc:
        return Extraction(file_type, reason=exc.reason, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 - 第三方解析器对损坏文件会抛多种异常
        log.info("附件取文字出错（%s）：%s", file_type, exc.__class__.__name__)
        return Extraction(
            file_type, reason="broken", detail=f"文件解析出错（{exc.__class__.__name__}）"
        )
    builder = found.builder
    result = Extraction(
        file_type,
        builder.segments,
        pages=found.pages,
        scanned_pages=found.scanned,
        masked=builder.masked,
    )
    pictures = found.pictures or found.scanned
    if not result.segments:
        if pictures:
            result.reason, result.detail = "scan", "只有图片，取不出文字（多半是扫描件）"
        else:
            result.reason, result.detail = "empty", "文件里没有文字"
    elif result.chars < _FEW_CHARS and (
        found.picture_bytes >= _BIG_PICTURES or (found.scanned or 0) > 0
    ):
        result.reason = "scan"
        result.detail = f"只有 {result.chars} 个字，其余是图片（多半是扫描件）"
    if result.reason:
        result.segments, result.masked = [], 0
    return result


# ---- 存进三张表 ----


def url_key(url: str) -> str:
    """链接的 SHA-256（和 attachments.url_hash 一样），唯一索引建在它上面。"""
    return sha256(url.encode()).hexdigest()


@dataclass
class TextReport:
    # 新取的内容（新内容，或按新规则重取）
    extracted: int = 0
    # 链接改指已经取过的同一内容（别的链接有一样的文件）
    reused: int = 0
    # 内容没变，不用重取
    unchanged: int = 0
    # 处理完是"取不出、下载不了"的链接，按原因
    failed: Counter = field(default_factory=Counter)
    # 网络、服务器暂时出错：有文字的保留，下次随附件副本的重试再取
    kept: int = 0
    # 撤下了旧文字的链接（原网站删了、内容变了、下载到的是网页等）
    retracted: int = 0
    # 超出范围删掉的链接、没有链接指向而删掉的内容版本
    removed: int = 0
    versions_removed: int = 0
    # 取文字这一步出错（数据库等），这一个链接这轮没记上，副本照常
    errors: int = 0
    # 还没取（或按旧规则取的）、这轮没轮到的
    due: int = 0


# 下载失败（attachments.Failure 的代码）→ 附件文字的原因；临时问题不在这里，保留原有文字
_FETCH_REASONS = {
    "too_large": "too_large",
    "gone": "gone",
    "http": "unavailable",
    "robots": "unavailable",
    "host": "unavailable",
    "network": "unavailable",
    "empty": "broken",
}


# 网络等临时问题没取到：隔这么久再为取文字下载一次（附件副本自己的重试照旧）
TEXT_RETRY = timedelta(hours=24)


def _is_webpage(data: bytes) -> bool:
    try:
        sniff(data)
    except Unreadable as exc:
        return exc.reason == "webpage"
    return False


def due(
    session: Session, keys: Iterable[str], now: datetime, *, retry_now: bool = False
) -> set[str]:
    """这些链接里还没按现在的规则取过文字的（没取过、按旧规则取的），要下载、取文字。上次遇到
    网络等临时问题的，到 retry_at 才算（retry_now 不等）。"""
    known = {
        key: (extractor, retry_at)
        for key, extractor, retry_at in session.execute(
            select(
                AttachmentTextUrl.url_hash,
                AttachmentTextUrl.extractor,
                AttachmentTextUrl.retry_at,
            )
        )
    }
    out = set()
    for key in keys:
        extractor, retry_at = known.get(key, (0, None))
        if extractor != EXTRACTOR_VERSION and (retry_now or retry_at is None or retry_at <= now):
            out.add(key)
    return out


def record(
    session: Session,
    url: str,
    declared: str,
    now: datetime,
    report: TextReport,
    *,
    content: bytes | None = None,
    failure=None,
) -> None:
    """记下这次下载的结果并提交。content：下载到的文件（取文字，内容相同的共用一份；下载到的是
    网页按下载失败算）；failure：下载失败（attachments.Failure）：临时问题保留原有文字、隔
    TEXT_RETRY 再试，其他的撤下旧文字。"""
    key = url_key(url)
    row = session.scalar(select(AttachmentTextUrl).where(AttachmentTextUrl.url_hash == key))
    if row is None:
        row = AttachmentTextUrl(url_hash=key, url=url, declared_type=declared, status="pending")
        session.add(row)
    row.declared_type = declared
    before = (row.version_id, row.status, row.reason)
    fresh = False
    webpage = content is not None and _is_webpage(content)
    if content is None or webpage:
        if not webpage and (failure is None or failure.transient):
            # 临时问题：有文字的保留，规则版本也不改（没取过的仍是待取，按旧规则取的仍要重取），
            # 隔 TEXT_RETRY 再试
            report.kept += 1
            row.retry_at = now + TEXT_RETRY
            session.commit()
            return
        row.version_id = None
        if webpage:
            # 服务器返回的错误页、登录页等：和下载失败一样撤下旧文字，不为它存内容版本
            # （每次的网页可能都不一样，存了会越积越多）
            row.status, row.reason, row.detail = "failed", "webpage", REASONS["webpage"]
        else:
            row.status, row.reason = "failed", _FETCH_REASONS.get(failure.code, "unavailable")
            row.detail = (failure.detail or "")[:300] or None
    else:
        version, fresh = _version(session, content, now, row)
        if fresh:
            report.extracted += 1
        elif before[0] == version.id:
            report.unchanged += 1
        else:
            report.reused += 1
        row.version_id = version.id
        row.status = "ok" if version.status == "ok" else "failed"
        row.reason, row.detail = version.reason, version.detail
    after = (row.version_id, row.status, row.reason)
    if after != before or (content is not None and fresh):
        row.changed_at = now  # 指向的版本、状态变了，或者同一份内容按新规则重取了
        if before[0] is not None and after[0] != before[0]:
            report.retracted += 1
    if row.status == "failed":
        report.failed[row.reason] += 1
    row.checked_at = now
    row.extractor = EXTRACTOR_VERSION
    row.retry_at = None
    session.commit()


# 正在取文字的内容版本：取之前先把这句记进版本、提交。进程在取的时候没了（内存不足被杀、超时
# 被停，也可能是部署重启），下次再取一次；连着两次都没取完，就不再取、按损坏记，附件任务不会
# 每轮都卡在同一个文件上
_IN_PROGRESS = "正在取文字（第 1 次）"
_IN_PROGRESS_AGAIN = "正在取文字（第 2 次，上次取到一半进程中断）"


def _version(
    session: Session, content: bytes, now: datetime, row: AttachmentTextUrl
) -> tuple[AttachmentTextVersion, bool]:
    """这份内容的版本：取过（按现在的规则）就直接用；没取过或按旧规则取的，取文字、存段落
    （第二个值为 True），指向它的链接状态跟着改。"""
    digest = sha256(content).hexdigest()
    version = session.scalar(
        select(AttachmentTextVersion).where(AttachmentTextVersion.sha256 == digest)
    )
    if version is not None and version.extractor == EXTRACTOR_VERSION:
        return version, False
    if version is not None and version.detail == _IN_PROGRESS_AGAIN:
        result = Extraction(
            version.file_type,
            reason="broken",
            detail="连着两次取文字时进程中断（可能太大或太复杂），按损坏处理，内容变了再取",
        )
    else:
        if version is None:
            version = AttachmentTextVersion(
                sha256=digest, size=len(content), status="failed", extractor=0, extracted_at=now
            )
            session.add(version)
        version.detail = _IN_PROGRESS_AGAIN if version.detail == _IN_PROGRESS else _IN_PROGRESS
        session.flush()
        # 链接先指向它、记成待取：进程中途没了，这个版本不会被当成没人用的清理掉；规则版本也清掉，
        # 下一轮马上再取（内容变了的链接原来按现在的规则取过，不清就要等到副本的例行复查）
        row.version_id, row.status, row.reason = version.id, "pending", None
        row.extractor, row.retry_at = 0, None
        session.commit()
        result = extract(content)
    session.execute(
        delete(AttachmentTextSegment).where(AttachmentTextSegment.version_id == version.id)
    )
    version.file_type = result.file_type
    version.size = len(content)
    version.status = "ok" if result.ok else "failed"
    version.reason = result.reason
    detail = result.detail
    if result.ok and result.scanned_pages:
        detail = f"{result.scanned_pages} 页是扫描页，这些页的字没取到"
    version.detail = detail[:300] if detail else None
    version.extractor = EXTRACTOR_VERSION
    version.segments = len(result.segments)
    version.chars = result.chars
    version.pages = result.pages
    version.scanned_pages = result.scanned_pages
    version.masked = result.masked
    version.extracted_at = now
    session.flush()
    if result.segments:
        session.execute(
            AttachmentTextSegment.__table__.insert(),
            [
                {
                    "version_id": version.id,
                    "n": s.n,
                    "kind": s.kind,
                    "text": s.text,
                    "loc": s.loc,
                    "cells": s.cells,
                }
                for s in result.segments
            ],
        )
    # 共用这份内容的其他链接：状态、原因跟着新结果改，记一次变化（第 2 步按它重算）
    status = "ok" if result.ok else "failed"
    for other in session.scalars(
        select(AttachmentTextUrl).where(
            AttachmentTextUrl.version_id == version.id, AttachmentTextUrl.id != row.id
        )
    ):
        other.status, other.reason, other.detail = status, version.reason, version.detail
        other.extractor, other.retry_at = EXTRACTOR_VERSION, None  # 不用再为它重新下载
        other.changed_at = now
    return version, True


def cleanup(session: Session, live: set[str], report: TextReport) -> None:
    """删掉超出范围的链接（附件所在文件超过保留期、下架、改成名单类等），再删掉没有链接指向的
    内容版本和它的文字。"""
    stale = [
        row_id
        for row_id, key in session.execute(select(AttachmentTextUrl.id, AttachmentTextUrl.url_hash))
        if key not in live
    ]
    for start in range(0, len(stale), 500):
        chunk = stale[start : start + 500]
        session.execute(delete(AttachmentTextUrl).where(AttachmentTextUrl.id.in_(chunk)))
    report.removed += len(stale)
    orphans = list(
        session.scalars(
            select(AttachmentTextVersion.id).where(
                ~exists().where(AttachmentTextUrl.version_id == AttachmentTextVersion.id)
            )
        )
    )
    for start in range(0, len(orphans), 200):
        chunk = orphans[start : start + 200]
        session.execute(
            delete(AttachmentTextSegment).where(AttachmentTextSegment.version_id.in_(chunk))
        )
        session.execute(delete(AttachmentTextVersion).where(AttachmentTextVersion.id.in_(chunk)))
    report.versions_removed += len(orphans)
    session.commit()


# ---- 读：每份通知的附件给 AI 读得了没有（统计用，第 2 步解读、问答也从这里取） ----

# 不在 REASONS 里、读的时候才算出来的几种：还没取、通知较早（超出范围）、不是申报通知或政策、
# 附件所在的那份文件不是一级公开来源或已下架（合并转载时详情页可能列组里另一份的附件）
STATES = {
    "ok": "能读",
    "pending": "还没取",
    "old": "这份通知较早，附件没读",
    "kind": "这类文件不读附件",
    "source": "附件所在的那份转载不是一级公开来源或已下架，没读",
    **REASONS,
}


@dataclass
class Reading:
    position: int
    name: str
    url: str
    ext: str
    # ok，或 STATES 里不能读的原因
    state: str
    detail: str | None = None
    version_id: int | None = None
    sha256: str | None = None
    file_type: str | None = None
    chars: int = 0
    segments: int = 0
    pages: int | None = None
    scanned_pages: int | None = None
    checked_at: datetime | None = None


def readings(session: Session, doc_id: int, now: datetime) -> list[Reading]:
    """这份通知详情页列出的附件（合并转载的，和详情页一样取正文最长那份的附件），每个附件读得了
    没有、读到的是哪个内容版本。范围按附件所在的那份文件算，和附件定时任务下载的范围
    （attachments.candidates）一样：一级公开来源、没下架、申报通知近 180 天或政策近 90 天。
    这份通知本身能不能公开（下架、来源级别）由调用方先查。"""
    from sqlalchemy import func

    from youju import attachments
    from youju.api import v1  # 延迟引入：v1 引入较多，admin 的定时任务用不到这里
    from youju.models import Document, Source
    from youju.takedown import available_condition

    detail = v1._details(session, [doc_id]).get(doc_id)
    items = sorted(detail.attachments, key=lambda a: a.position) if detail else []
    if not items:
        return []
    owner = session.execute(
        select(
            func.coalesce(Document.kind_override, Document.rule_kind),
            Document.publish_date,
            Document.first_seen_at,
            Source.rights_level,
        )
        .join(Source, Source.id == Document.source_id)
        .where(Document.id == detail.document_id, available_condition())
    ).first()
    public = owner is not None and owner.rights_level == 1
    kind = owner[0] if owner is not None else None
    recent = public and attachments.is_recent(owner[1], owner[2], now, kind=kind or "")
    rows = {
        r.url_hash: r
        for r in session.scalars(
            select(AttachmentTextUrl).where(
                AttachmentTextUrl.url_hash.in_({url_key(a.url) for a in items})
            )
        )
    }
    versions = {
        v.id: v
        for v in session.scalars(
            select(AttachmentTextVersion).where(
                AttachmentTextVersion.id.in_({r.version_id for r in rows.values() if r.version_id})
            )
        )
    }
    out = []
    for a in items:
        reading = Reading(a.position, a.name, a.url, (a.extension or "").lower(), "pending")
        row = rows.get(url_key(a.url))
        version = versions.get(row.version_id) if row and row.version_id else None
        if reading.ext not in attachments.DOWNLOAD_TYPES:
            reading.state = "format"
        elif not attachments._government_url(a.url):
            reading.state, reading.detail = "unavailable", "不是政府网站的链接"
        elif not public:
            reading.state = "source"
        elif kind not in attachments.COPY_KINDS:
            reading.state = "kind"
        elif not recent:
            reading.state = "old"
        elif row is None or row.status == "pending":
            reading.state = "pending"
        else:
            reading.state = "ok" if row.status == "ok" else row.reason or "unavailable"
            reading.detail = row.detail
            reading.checked_at = row.checked_at
        if version is not None and reading.state not in ("old", "kind", "source"):
            reading.version_id, reading.sha256 = version.id, version.sha256
            reading.file_type, reading.chars = version.file_type, version.chars
            reading.segments, reading.pages = version.segments, version.pages
            reading.scanned_pages = version.scanned_pages
        out.append(reading)
    return out


def segments(session: Session, version_id: int) -> list[AttachmentTextSegment]:
    """一个内容版本的文字，按段号排。"""
    return list(
        session.scalars(
            select(AttachmentTextSegment)
            .where(AttachmentTextSegment.version_id == version_id)
            .order_by(AttachmentTextSegment.n)
        )
    )
