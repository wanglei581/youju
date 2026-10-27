"""找通知（问一问第一期）：一句话按规则取条件，交给现有搜索列出通知，不用模型。

依据：问一问开发设计（#328）第六节第 1 条（取条件的规则 0–7 和后面几条）、第五节
/chat/examples、第十二节"回答：找通知"那张表。

- run：取条件、搜，得出存进 ai_chat_turns.answer_json 的回答：条件、总数、列出的通知编号（按当时
  的顺序，最多 10 份）、给不给"在搜索页看全部"、提示代码；不存标题和截止。条件里从原话取出的字
  只有 keywords、docNumber；title 只记比没比中。
- render：打开对话时按存的编号重新取行（_items，截止、核对状态、能不能解读都是最新的），下架的
  不显示、份数记在 hidden；conditionsText、searchLink 现拼、不存。
- examples：/chat/examples 的示例，按北京时间每小时算一次。
- parse：第 1–7 条取条件，不查库（收录范围由调用方给）。

排序、行的格式、截止的写法都和搜索页一样（search_statement、_items(grouped, excerpts)）；
条件到搜索参数只有 search_params 一张表，自己搜和"在搜索页看全部"带的参数是同一份。

设计没写死、这里定下的几处：
- 一句里有几个时间词时，按上周、上个月这类（取先写的）、近 7 天、近 30 天、近 90 天、今年、
  往年的顺序取第一个（往年取先写的）；
- "上周"这类（上星期、上个星期、上一周、上礼拜……，_PERIOD_OF）按发布日期筛上一个星期一到
  星期日，"上个月"这类（上月、上一个月）筛上一个自然月（北京时间）；"上上周""上上个月"再往前
  一个。搜索页没有这几种，同往年不给"在搜索页看全部"；
- "近 N 日""过去 N 天""N 天内"这类和"近 N 天"一样，"这星期""这礼拜""近一个星期"这类和"这周"
  一样算近 7 天，"本季度"同"这个季度"（完整的见 _TIME_WORDS）；
- 第 2 条的细化（设计的表写"国家""国家级"认成国家部委）：后面紧跟着别的名称时（国家高新技术
  企业、国家级专精特新、国家认定企业技术中心）是名称的开头，不当地区，也不当关键词（省里发的这类
  通知，标题多半不写"国家"）；名称以局、委、部、署、办、办公室、委员会结尾的是国家机关（国家
  数据局、国家发展改革委），照旧算国家部委，名称当关键词。单独说的也是国家部委：后面是句子结尾、
  不是汉字的（标点、数字、字母；开头的引号、书名号先跳过）、地名、时间词、类型词（"认定"除外：
  国家认定企业技术中心）、问内容的词、说发文的词（发布、出台、发的……）、"和、与、及、或"，或者
  "企业""公司"以外的停用词（国家的、国家最新、国家发的申报通知）；
- 紧跟在认出的地区后面的"和、与、及、或"去掉（"国家级和省级专精特新"不留"和省级"）；
- "发布、出台、印发、下发、公布、颁布"这几个说发文的词也当停用词去掉（"上周发布的申报通知"）；
- 以"市"开头的区县和城市名连着写、"市"只写一个的（"济南市中区"＝济南市市中区），认成那个
  区县，不拆成"济南市"和关键词"中区"（同第 3 条，只认收录了的区县）；
- "××××年度"单独算一个关键词（"当标题词"），不和前后的字连成一个；晚于今年、早于 2000 年的
  "××××年"不当时间，也这样单独算一个；
- "问题像在问内容"那几个词（能不能、补贴多少等）也当停用词去掉，不然会留成关键词、一份都找不到；
- "最近一周"和"近一周"同样算近 7 天（否则会先认成"最近"，剩下"一周"当关键词）；
- 文号也认"公告2026年第5号"这种写法（年份后面跟"年"），序号按数比（056 和 56 一样）；
- 有结果才给"在搜索页看全部"（link）。

提示代码（notes）和小程序写的话（第十二节；原文在 NOTES 里。{docNumber}、{year} 照 conditions，
{uncovered} 用"、"连起来，{next_year} 是 year + 1；［…］是按钮）：

- title：按标题找到的。
- doc_number_none：收录里没有文号是"{docNumber}"的通知。文号可能抄错了，也可能我们没收录；
  可以输入标题里的几个词再找。
- uncovered：收录里没有{uncovered}的来源，查不到那里的通知。［收录范围 ›］（一个收录了的地区都
  没有时不搜，只有这一句；还有收录了的，就只搜收录了的）
- year：按发布日期找的 {year} 年发布的通知；"{year} 年度"的项目可能在 {next_year} 年才发通知，
  找不到可以改成"今年"再找。
- first_three：只按前 3 个词找的。
- more：这里列最新 10 份。（link 为真时接［在搜索页看全部 ›］，为假时同时有 narrow）
- narrow：可以加上地区、年份或更具体的词缩小范围。
- empty：按这些条件没找到。可以少写几个词，或去掉地区、年份再找；也可能这个地方还没收录，
  看"说明"里的收录范围。
- title_none：没有标题完全一样的通知，按条件也没找到。可以只输入标题里的几个词再找。
- content：找通知只帮你找到通知。想知道某份通知里的截止、材料、条件怎么写的，点那一行的
  "问这份通知"，AI 从原文里摘出相关的句子。
- photo：请输入通知标题或文号，帮你找原文。
- vague：没认出要找什么（一个条件都没有）：不去搜，请用户写上标题里的词或地区、类型、时间。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from youju.api.v1 import (
    _NOT_GUIDE,
    _PROVINCE_ORDER,
    _TZ,
    TIMES,
    _base,
    _count,
    _folds_guides,
    _grouped,
    _items,
    _keyword_ids,
    _kind_is,
    _ordered,
    search_statement,
)
from youju.models import Document, DocumentDetail, Source
from youju.regions import city_key, covered_city_scopes, covered_district_scopes, resolve_district

LIMIT = 10  # 最多列最新 10 份
TITLE_CANDIDATES = 500  # 整个标题：LIKE 出的候选超过这么多，当作没有完全一样的
KEYWORDS_MAX = 3
KEYWORD_CHARS = 20
SEARCH_Q_CHARS = 50  # 搜索页的关键词最长 50 字
EXAMPLE = "找{}近 30 天的申报通知"

NOTES = {
    "title": "按标题找到的。",
    "doc_number_none": (
        "收录里没有文号是“{docNumber}”的通知。文号可能抄错了，也可能我们没收录；"
        "可以输入标题里的几个词再找。"
    ),
    "uncovered": "收录里没有{uncovered}的来源，查不到那里的通知。",
    "year": (
        "按发布日期找的 {year} 年发布的通知；“{year} 年度”的项目可能在 {next_year} 年才发通知，"
        "找不到可以改成“今年”再找。"
    ),
    "first_three": "只按前 3 个词找的。",
    "more": "这里列最新 10 份。",
    "narrow": "可以加上地区、年份或更具体的词缩小范围。",
    "empty": (
        "按这些条件没找到。可以少写几个词，或去掉地区、年份再找；也可能这个地方还没收录，"
        "看“说明”里的收录范围。"
    ),
    "title_none": "没有标题完全一样的通知，按条件也没找到。可以只输入标题里的几个词再找。",
    "content": (
        "找通知只帮你找到通知。想知道某份通知里的截止、材料、条件怎么写的，"
        "点那一行的“问这份通知”，AI 从原文里摘出相关的句子。"
    ),
    "photo": "请输入通知标题或文号，帮你找原文。",
    # 一个条件都没认出来（只有停用词、标点、表情）：不去搜，免得把全部通知都列出来
    "vague": (
        "没认出要找什么。可以输入通知标题里的几个词，或者写上地区、类型、时间，"
        "比如“找湖北近 30 天的申报通知”。"
    ),
}

_HAN = f"{chr(0x3400)}-{chr(0x4DBF)}{chr(0x4E00)}-{chr(0x9FFF)}"
_HAN_CHAR = re.compile(f"[{_HAN}]")
_WORD = re.compile(r"[^\W_]+")  # 连着的汉字、字母、数字

# 说到拍照、图片：不去搜
_PHOTO = ("拍照", "照片", "图片", "截图", "扫一扫", "扫描")
# 问题像在问内容：照样列通知，末尾加 content 那句
_CONTENT = ("截止", "什么时候", "哪天", "材料", "条件", "要求", "能不能", "多少钱", "补贴多少")

# 第 0 条 整个标题：四条都满足才去比
_TITLE_ENDS = (
    "通知", "公告", "公示", "通告", "名单", "办法", "细则", "意见", "指南", "方案", "规定", "决定",
)  # fmt: skip
_TITLE_ASKS = ("吗", "呢", "什么", "哪", "怎么", "多少", "能不能", "是否")
_TITLE_STARTS = ("找", "查", "搜", "帮我", "请", "我想", "有没有", "看看")
_TITLE_MIN = 15  # 只留汉字、字母、数字后要超过 15 个字
# 末尾"最后一对括号"（NFKC 以后全角圆括号、方括号已是半角）
_BRACKETS = {")": "(", "]": "[", "】": "【", "〕": "〔", "〗": "〖", "}": "{"}

# 第 1 条 文号：机关简称 + 四位年份（前后可带括号，或写成"2026年第5号"）+ 序号 + "号"
_NUMBER = re.compile(r"([" + _HAN + r"]+)[(\[〔【〖{]?(\d{4})(?:[)\]〕】〗}]|年)?第?(\d{1,6})号")

# 第 2 条 省级地区：31 个，带不带"省""市""自治区"及全称都认；国家部委的几种说法
_MUNICIPALITIES = ("北京", "天津", "上海", "重庆")
_AUTONOMOUS = {
    "内蒙古": ("内蒙古自治区",),
    "广西": ("广西自治区", "广西壮族自治区"),
    "西藏": ("西藏自治区",),
    "宁夏": ("宁夏自治区", "宁夏回族自治区"),
    "新疆": ("新疆自治区", "新疆维吾尔自治区"),
}
NATIONAL = "国家部委"
_NATIONAL_WORDS = ("国家部委", "国家级", "国家", "部委", "部里")
# 这两个后面紧跟着别的名称时是名称的开头（"国家高新技术企业"），见 _national
_NAME_STARTS = ("国家", "国家级")
_JOINS = ("和", "与", "及", "或")
_OPENERS = "“‘\"'《「『【〈〔([<"  # 引号、书名号、括号的前一半（NFKC 以后全角圆括号已是半角）
_AGENCY_ENDS = ("局", "委", "部", "署", "办", "办公室", "委员会")  # 国家机关名的结尾


def _province_aliases() -> dict[str, str]:
    out = {}
    for name in _PROVINCE_ORDER:
        if name in _AUTONOMOUS:
            extra = _AUTONOMOUS[name]
        elif name in _MUNICIPALITIES:
            extra = (f"{name}市",)
        else:
            extra = (f"{name}省",)
        for alias in (name, *extra):
            out[alias] = name
    return out | dict.fromkeys(_NATIONAL_WORDS, NATIONAL)


PROVINCE_ALIASES = _province_aliases()
_AREA_BAD = re.compile(r"[/\\,，;；、|\t\n\r]")  # 和搜索页一样：地名里不能有分隔符

# 第 5 条 类型：按这个顺序，先命中的算；都没有是不限（all）
_KIND_RULES = (
    ("名单", ("名单", "公示", "名录", "拟认定", "入选")),
    ("申报通知", ("申报", "申请", "兑现", "征集", "遴选", "推荐", "认定")),
    ("政策", ("政策", "办法", "意见", "措施", "细则", "规定")),
    ("申报通知", ("通知", "指南", "项目", "资金")),
)
TYPE_WORDS = tuple(word for _, words in _KIND_RULES for word in words)
KIND_LABELS = {
    "all": "不限（不含资讯与指南）",
    "申报通知": "申报通知",
    "名单": "名单与公示",
    "政策": "政策",
}

# 第 6 条 时间：取值和搜索页的 time 一样（TIMES）
_TIME_WORDS = {
    "近 7 天": (
        "近7天", "近七天", "最近7天", "最近七天", "这周", "本周", "这几天",
        "近一周", "近1周", "最近一周", "最近1周",
        "近7日", "近七日", "最近7日", "最近七日", "这星期", "本星期", "这个星期",
        "近一星期", "最近一星期", "近一个星期", "最近一个星期", "这礼拜", "本礼拜", "这个礼拜",
        "近一个礼拜", "最近一个礼拜", "过去7天", "过去七天", "过去一周", "过去一个星期",
        "7天内", "七天内", "一周内", "这个周",
    ),
    "近 30 天": (
        "近30天", "近三十天", "最近30天", "最近三十天", "近一个月", "最近一个月", "近1个月",
        "最近1个月", "最近", "最新", "近期", "本月", "这个月",
        "近30日", "近三十日", "最近30日", "最近三十日", "过去30天", "过去三十天", "过去一个月",
        "30天内", "三十天内", "一个月内",
    ),
    "近 90 天": (
        "近90天", "近九十天", "最近90天", "最近九十天", "近三个月", "最近三个月", "近3个月",
        "最近3个月", "这个季度",
        "近90日", "近九十日", "最近90日", "最近九十日", "过去90天", "过去九十天", "过去三个月",
        "90天内", "九十天内", "三个月内", "本季度",
    ),
    "今年": ("今年", "本年"),  # "本年度"先换成"今年的年份＋年度"，到不了这里
}  # fmt: skip
_TIME_ORDER = ("近 7 天", "近 30 天", "近 90 天", "今年")  # 几个时间词都有时取第一个
_TIME_OF = {word: value for value, words in _TIME_WORDS.items() for word in words}
_PAST = {"去年": 1, "前年": 2}
# 上一个星期（一到日）、上一个自然月：（week 或 month, 往前几个），按发布日期筛，见 _period
_PERIOD_OF = {
    **dict.fromkeys(
        (
            "上周",
            "上个周",
            "上星期",
            "上个星期",
            "上一周",
            "上一星期",
            "上一个星期",
            "上礼拜",
            "上个礼拜",
        ),
        ("week", 1),
    ),
    **dict.fromkeys(
        ("上上周", "上上个周", "上上星期", "上上个星期", "上上礼拜", "上上个礼拜"), ("week", 2)
    ),
    **dict.fromkeys(("上个月", "上月", "上一个月"), ("month", 1)),
    **dict.fromkeys(("上上个月", "上上月"), ("month", 2)),
}
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)(?!年度)年?")  # 后面跟"年度"的不是时间
_YEAR_DEGREE = re.compile(r"(?<!\d)\d{4}年度")
_FIRST_YEAR = 2000  # 往年只认 2000 年以后

# 第 7 条 停用词（设计的清单）；_CONTENT 那几个是问内容的话，也去掉
STOPWORDS = (
    "公告", "通告", "文件", "找", "查", "搜", "帮我", "帮忙", "我想", "想要", "一下", "看看",
    "有没有", "有哪些", "哪些", "什么", "关于", "相关", "有关", "方面", "的", "了", "吗", "呢",
    "请问", "企业", "公司", "我们", "我司", "一份", "截止", "时间", "什么时候", "哪天", "材料",
    "条件", "要求", "能不能报", "多少",
)  # fmt: skip
# 说发文的词：也当停用词去掉（"上周发布的申报通知"）
_ISSUE = ("发布", "出台", "印发", "下发", "公布", "颁布")
# 切完剩一个字时只和这几个停用词并成一个词（"新材料""大企业"），见 _pieces
_GLUE = ("材料", "企业", "公司")
# 文号前面不算机关简称的字（"帮我找一下文号是……"）。地名不在里面："国家税务总局公告"的"国家"、
# 机关简称开头的省名都是简称的一部分，去掉了就会拿别的机关的文号顶替
_NUMBER_LEAD = ("文号", "发文字号", "字号", "编号", "是", "为")


def _alternation(words) -> re.Pattern:
    """几个词任一个；长的在前，"最近7天"不会先认成"最近"。"""
    return re.compile("|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True)))


_TIME = _alternation([*_TIME_OF, *_PAST, *_PERIOD_OF])
_DROP = _alternation([*TYPE_WORDS, *STOPWORDS, *_CONTENT, *_ISSUE])
_LEAD = tuple(
    sorted(
        {
            *STOPWORDS,
            *_TITLE_STARTS,
            *_NUMBER_LEAD,
            *_TIME_OF,
            *_PAST,
            *_PERIOD_OF,
            *TYPE_WORDS,
            *_CONTENT,
            *_ISSUE,
        },
        key=len,
        reverse=True,
    )
)


# "国家""国家级"单独说（是地区）时后面跟的词，地名另外看。"企业""公司""认定"不算（国家企业
# 技术中心、国家认定企业技术中心）
_ALONE_AFTER = tuple(
    {*STOPWORDS, *TYPE_WORDS, *_CONTENT, *_ISSUE, "发的", *_TIME_OF, *_PAST, *_PERIOD_OF, *_JOINS}
    - {"企业", "公司", "认定"}
)
_NAME_END = tuple(set(_ALONE_AFTER) - set(_JOINS))  # 名称到这些词为止（"工业和信息化部"不断开）


@dataclass(frozen=True)
class Coverage:
    """收录范围（同 /coverage）：省级地区（国家级来源记作"国家部委"）、市（省, 市）、区县。"""

    provinces: frozenset[str]
    cities: frozenset[tuple[str, str]]
    districts: frozenset[tuple[str, str, str]]


@dataclass(frozen=True)
class Parsed:
    conditions: dict[str, Any]
    number: tuple[str, int, int] | None  # 认出了文号：（机关简称、年份、序号）
    content: bool  # 像在问内容
    dropped: bool  # 词超过 3 个，只按前 3 个找


def normalize(text: str | None) -> str:
    """先 NFKC（全角转半角），再去掉所有空白。"""
    return "".join(unicodedata.normalize("NFKC", text or "").split())


def _letters(text: str | None) -> str:
    """标题比对用：NFKC 以后只留汉字、字母、数字（书名号、括号、引号、顿号、空白都去掉）。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text or "") if ch.isalnum())


def _has(text: str, words) -> bool:
    return any(word in text for word in words)


def empty_conditions() -> dict[str, Any]:
    return {
        "regions": [],  # 省级地区（含"国家部委"），只有收录了的
        "cities": [],  # "省/市"
        "districts": [],  # "省/市/区县"
        "uncovered": [],  # 认出来但没收录的省级地区
        "kind": "all",
        "time": "不限",
        "year": None,  # 往年：按发布日期筛那一年
        "period": None,  # 上周、上个月：{"from", "to"}（ISO 日期，两头都算），按发布日期筛
        "keywords": [],
        "docNumber": None,
        "title": False,  # 整个标题比中了没有（不存那句话）
    }


# ---- 第 0 条：整个标题 ----


def _strip_tail(text: str) -> str:
    """末尾的标点和最后一对括号里的字（"（试行）""（第二批）"）不算。"""

    def marks_off(value: str) -> str:
        end = len(value)
        while end and not value[end - 1].isalnum() and value[end - 1] not in _BRACKETS:
            end -= 1
        return value[:end]

    text = marks_off(text)
    if text and text[-1] in _BRACKETS:
        start = text.rfind(_BRACKETS[text[-1]])
        if start >= 0:
            text = marks_off(text[:start])
    return text


def looks_like_title(text: str) -> bool:
    """像整个标题：四条都满足（text 已经 normalize）。"""
    if "?" in text or _has(text, _TITLE_ASKS) or text.startswith(_TITLE_STARTS):
        return False
    return _strip_tail(text).endswith(_TITLE_ENDS) and len(_letters(text)) > _TITLE_MIN


def _title_ids(session: Session, text: str) -> list[int]:
    """标题规范化以后和这句完全相同的文件。标题入库没做 NFKC，候选只用句子里的汉字按顺序 LIKE
    （字母、数字有全角半角之分，不放进去），再在程序里逐个规范化、比较。"""
    target = _letters(text)
    pattern = "%" + "%".join(_HAN_CHAR.findall(text)) + "%"
    rows = session.execute(
        select(Document.id, Document.title)
        .where(Document.title.like(pattern))
        .limit(TITLE_CANDIDATES + 1)
    ).all()
    if len(rows) > TITLE_CANDIDATES:
        return []
    return [doc_id for doc_id, title in rows if _letters(title) == target]


# ---- 第 1 条：文号 ----


def _strip_lead(run: str) -> str:
    while True:
        word = next((w for w in _LEAD if run.startswith(w)), None)
        if word is None:
            return run
        run = run[len(word) :]


def doc_numbers(text: str | None) -> list[tuple[str, int, int, str]]:
    """文字里的文号：（机关简称、年份、序号、原样）。括号不比；机关前面的"找""文号是"这类字不算，
    去掉以后没有机关简称的不算文号。"""
    out = []
    for m in _NUMBER.finditer(normalize(text)):
        agency = _strip_lead(m.group(1))
        if agency:
            shown = m.group(0)[len(m.group(1)) - len(agency) :]
            out.append((agency, int(m.group(2)), int(m.group(3)), shown))
    return out


def _wide(digits: str) -> str:
    return digits.translate({ord(c): ord(c) + 0xFEE0 for c in "0123456789"})


def _number_ids(session: Session, agency: str, year: int, seq: int) -> list[int]:
    """机关、年份、序号三样都一样的文件：先用年份和序号 LIKE 出候选（入库的文号没做 NFKC，
    全角数字另 LIKE 一次），再逐个比。不拿相近的顶替。"""
    likes = {f"%{year}%{seq}号%", f"%{_wide(str(year))}%{_wide(str(seq))}号%"}
    rows = session.execute(
        select(DocumentDetail.document_id, DocumentDetail.doc_number).where(
            or_(*(DocumentDetail.doc_number.like(p) for p in likes))
        )
    ).all()
    want = (agency, year, seq)
    return [doc_id for doc_id, value in rows if any(n[:3] == want for n in doc_numbers(value))]


# ---- 第 2–7 条 ----


def coverage(session: Session) -> Coverage:
    """收录范围：有启用、且成功检查过的来源（同 /coverage、covered_city_scopes）。"""
    provinces = session.scalars(
        select(Source.province)
        .where(Source.enabled.is_(True), Source.last_success_at.is_not(None))
        .distinct()
    ).all()
    return Coverage(
        provinces=frozenset(NATIONAL if p == "国家" else p for p in provinces),
        cities=frozenset(covered_city_scopes(session)),
        districts=frozenset(covered_district_scopes(session)),
    )


def _plain(name: str | None) -> bool:
    return bool(name) and len(name) <= 32 and not _AREA_BAD.search(name)


def _area_table(covered: Coverage) -> dict[str, list[tuple[str, ...]]]:
    """能认的地名 → 意思：("province", 省)、("city", 省, 市)、("district", 省, 市, 区县)。
    市、区县只认收录了的，带不带"市"都认（同 resolve_city；区县里的县级市"昆山市"写"昆山"也认，
    "南山区"不能只写"南山"）；和省级地区同名的简称（吉林）只当省，"吉林市"才是市。"""
    table: dict[str, list[tuple[str, ...]]] = {
        alias: [("province", name)] for alias, name in PROVINCE_ALIASES.items()
    }
    for province, city in sorted(covered.cities):
        if not (_plain(province) and _plain(city)):
            continue
        short = city_key(city)
        for name in dict.fromkeys((city, short, f"{short}市")):
            if len(name) >= 2 and name not in PROVINCE_ALIASES:
                table.setdefault(name, []).append(("city", province, city))
    for province, city, district in sorted(covered.districts):
        resolved = resolve_district(province, city, district, covered.districts)
        if not (_plain(province) and _plain(city) and _plain(district)) or resolved is None:
            continue
        names = [district, district.removesuffix("市")]
        if district.startswith("市"):  # "济南市中区"：和城市名连着写，"市"只写一个
            names.append(city_key(resolved[0]) + district)
        for name in dict.fromkeys(names):
            if len(name) >= 2 and name not in PROVINCE_ALIASES:
                table.setdefault(name, []).append(("district", province, *resolved))
    return table


def _take(marks: list[str], start: int, end: int) -> None:
    marks[start:end] = "|" * (end - start)


def _pieces(run: str) -> list[tuple[int, str, bool]]:
    """把一串连着的字按类型词、停用词、问内容的词切开：（位置, 字, 是不是要去掉的词）。
    切完剩一个字、又紧挨着"材料、企业、公司"（_GLUE）的，和它并成一个词，不去掉（"新材料"不拆成
    "新"和停用词"材料"；"大企业"不拆成"大"和"企业"）。别的停用词不并（"有什么政策"不留
    "有什么"）。"""
    raw: list[tuple[int, str, bool]] = []
    pos = 0
    for m in _DROP.finditer(run):
        if m.start() > pos:
            raw.append((pos, run[pos : m.start()], False))
        raw.append((m.start(), m.group(), True))
        pos = m.end()
    if pos < len(run):
        raw.append((pos, run[pos:], False))
    out: list[tuple[int, str, bool]] = []
    i = 0
    while i < len(raw):
        at, text, drop = raw[i]
        if not drop and len(text) == 1:
            nxt = raw[i + 1] if i + 1 < len(raw) else None
            if nxt and nxt[2] and nxt[1] in _GLUE:
                out.append((at, text + nxt[1], False))
                i += 2
                continue
            if out and out[-1][2] and out[-1][1] in _GLUE:
                prev = out.pop()
                out.append((prev[0], prev[1] + text, False))
                i += 1
                continue
        out.append((at, text, drop))
        i += 1
    return out


def _national(rest: str, places: tuple[str, ...]) -> bool:
    """看"国家""国家级"是不是说国家部委（rest 是它后面的字；开头的引号、书名号先跳过）：
    - 单独说的：后面是句子结尾、不是汉字的、地名（places），或 _ALONE_AFTER 里的词；
    - 后面紧跟着国家机关名：连着的汉字到 _NAME_END 里的词为止，以 _AGENCY_ENDS 结尾（国家数据局、
      国家发展改革委）。
    别的是名称的开头（国家高新技术企业、国家级专精特新）。"""
    rest = rest.lstrip(_OPENERS)
    if not rest or not _HAN_CHAR.match(rest[0]):
        return True
    if rest.startswith(_ALONE_AFTER) or rest.startswith(places):
        return True
    end = 0
    while end < len(rest) and _HAN_CHAR.match(rest[end]) and not rest.startswith(_NAME_END, end):
        end += 1
    return rest[:end].endswith(_AGENCY_ENDS)


def _areas(text: str, marks: list[str], covered: Coverage) -> dict[str, list[str]]:
    """第 2–4 条。几个地区取并集；同名的市、区县按句子里写到的省、市缩小，缩小不了就都算。
    具体城市、区县替换同省的全省条件，具体区县替换所属城市（同搜索页 withoutCityProvinces、
    withoutDistrictCities，这样"在搜索页看全部"打开的条件和这里一样）。"""
    table = _area_table(covered)
    provinces: list[str] = []
    city_hits: list[list[tuple[str, ...]]] = []
    district_hits: list[list[tuple[str, ...]]] = []
    places = tuple(table)
    for m in _alternation(table).finditer(text):
        _take(marks, m.start(), m.end())
        if m.group() in _NAME_STARTS and not _national(text[m.end() :], places):
            continue  # 名称的开头（"国家高新技术企业"）：不当地区，也不当关键词
        if text[m.end() : m.end() + 1] in _JOINS:  # "国家级和省级"：紧跟着的"和"不留成关键词
            _take(marks, m.end(), m.end() + 1)
        meanings = table[m.group()]
        kinds = {meaning[0] for meaning in meanings}
        if "province" in kinds:
            provinces.append(meanings[0][1])
        elif "city" in kinds:
            city_hits.append([meaning[1:] for meaning in meanings if meaning[0] == "city"])
        else:
            district_hits.append([meaning[1:] for meaning in meanings])
    named = set(provinces)
    cities: list[tuple[str, ...]] = []
    for hit in city_hits:
        cities += [c for c in hit if c[0] in named] or hit
    named_cities = {(p, city_key(c)) for p, c in cities}
    districts: list[tuple[str, ...]] = []
    for hit in district_hits:
        districts += (
            [d for d in hit if (d[0], city_key(d[1])) in named_cities]
            or [d for d in hit if d[0] in named]
            or hit
        )
    parent_cities = {(p, city_key(c)) for p, c, _ in districts}
    cities = [c for c in dict.fromkeys(cities) if (c[0], city_key(c[1])) not in parent_cities]
    parents = {c[0] for c in cities} | {d[0] for d in districts}
    named_order = list(dict.fromkeys(provinces))
    return {
        "regions": [p for p in named_order if p in covered.provinces and p not in parents],
        "cities": [f"{p}/{c}" for p, c in cities],
        "districts": [f"{p}/{c}/{d}" for p, c, d in dict.fromkeys(districts)],
        "uncovered": [p for p in named_order if p not in covered.provinces],
    }


def _period(kind: str, back: int, today: date) -> dict[str, str]:
    """上周（back=1）：上一个星期一到星期日，上上周（back=2）再往前一周；上个月、上上个月同样按
    自然月。today 是北京时间的今天。"""
    if kind == "week":
        start = today - timedelta(days=today.weekday() + 7 * back)
        end = start + timedelta(days=6)
    else:
        end = today.replace(day=1) - timedelta(days=1)
        for _ in range(back - 1):
            end = end.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    return {"from": start.isoformat(), "to": end.isoformat()}


def parse(text: str, *, today: date, covered: Coverage) -> Parsed:
    """第 1–7 条：从一句话里取条件。没认出来的字当关键词，不另外猜。"""
    text = normalize(text).replace("本年度", f"{today.year}年度")
    conditions = empty_conditions()
    content = _has(text, _CONTENT)
    numbers = doc_numbers(text)
    if numbers:  # 只按文号找
        agency, year, seq, shown = numbers[0]
        conditions["docNumber"] = shown
        return Parsed(conditions, (agency, year, seq), content, False)

    marks = list(text)
    conditions |= _areas(text, marks, covered)

    windows: set[str] = set()
    past: list[tuple[int, int]] = []  # （位置, 年份）
    periods: list[tuple[int, tuple[str, int]]] = []  # （位置, (week 或 month, 往前几个)）
    tokens: list[tuple[int, str]] = []  # （位置, 关键词）
    for m in _TIME.finditer("".join(marks)):
        if m.group() in _PERIOD_OF:
            periods.append((m.start(), _PERIOD_OF[m.group()]))
        elif m.group() in _PAST:
            past.append((m.start(), today.year - _PAST[m.group()]))
        else:
            windows.add(_TIME_OF[m.group()])
        _take(marks, m.start(), m.end())
    for m in _YEAR.finditer("".join(marks)):
        year = int(m.group(1))
        if year == today.year:
            windows.add("今年")
        elif _FIRST_YEAR <= year < today.year:
            past.append((m.start(), year))
        elif m.group().endswith("年"):  # 晚于今年、早于 2000 年的不当时间，同"××××年度"当标题词
            tokens.append((m.start(), m.group()))
        else:
            continue
        _take(marks, m.start(), m.end())
    if periods:
        conditions["period"] = _period(*min(periods)[1], today)
    elif windows:
        conditions["time"] = next(t for t in _TIME_ORDER if t in windows)
    elif past:
        conditions["year"] = min(past)[1]

    for m in _YEAR_DEGREE.finditer("".join(marks)):  # "××××年度"当标题词，单独算一个
        tokens.append((m.start(), m.group()))
        _take(marks, m.start(), m.end())
    conditions["kind"] = next((kind for kind, words in _KIND_RULES if _has(text, words)), "all")
    # 问内容的提示只在那几个词单独出现时才加（"新材料"里的"材料"不算）
    content = False
    for run in _WORD.finditer("".join(marks)):
        for at, piece, drop in _pieces(run.group()):
            if not drop:
                tokens.append((run.start() + at, piece))
            elif piece in _CONTENT:
                content = True
    words = list(dict.fromkeys(t[:KEYWORD_CHARS] for _, t in sorted(tokens) if len(t) >= 2))
    conditions["keywords"] = words[:KEYWORDS_MAX]
    return Parsed(conditions, None, content, len(words) > KEYWORDS_MAX)


# ---- 搜 ----


def search_params(conditions: dict[str, Any]) -> dict[str, str]:
    """条件 → 搜索参数（search_statement 的参数和"在搜索页看全部"的链接共用这一张表）。
    kind、time 一直带上：搜索页默认是近 30 天。只有一个关键词时才有 q（多个的另外取交集）。"""
    params = {"kind": conditions["kind"], "time": conditions["time"]}
    if len(conditions["keywords"]) == 1:
        params["q"] = conditions["keywords"][0]
    for key in ("regions", "cities", "districts"):
        if conditions[key]:
            params[key] = ",".join(conditions[key])
    return params


def statement(session: Session, conditions: dict[str, Any]) -> Select:
    """第 1–7 条的条件 → 搜索语句。和搜索页同一套：search_statement，不限类型时折叠资讯与指南
    （fold=1），同一转发组只列一条（group=1）。"""
    params = search_params(conditions)
    stmt = search_statement(
        session,
        params.get("q"),
        params["kind"],
        params.get("regions"),
        None,
        None,
        params["time"],
        None,
        cities=params.get("cities"),
        districts=params.get("districts"),
    )
    if len(conditions["keywords"]) > 1:  # 每个关键词都要命中标题或文号
        for keyword in conditions["keywords"]:
            stmt = stmt.where(Document.id.in_(_keyword_ids(session, keyword)))
    if conditions["year"]:  # 搜索页没有按年份筛：在结果上再加发布日期在那一年里
        year = conditions["year"]
        stmt = stmt.where(
            Document.publish_date >= date(year, 1, 1), Document.publish_date < date(year + 1, 1, 1)
        )
    if conditions["period"]:  # 上周、上个月：搜索页也没有，同往年在结果上再加
        period = conditions["period"]
        stmt = stmt.where(
            Document.publish_date >= date.fromisoformat(period["from"]),
            Document.publish_date <= date.fromisoformat(period["to"]),
        )
    if _folds_guides(params["kind"], None):
        stmt = stmt.where(_kind_is(_NOT_GUIDE))
    return _grouped(stmt)


def _matched(session: Session, ids: list[int]) -> Select:
    """整个标题、文号比中的：kind=all、time=不限、不折叠；下架、隐藏的照搜索页排除，转发照搜索页合并。"""
    stmt = search_statement(session, None, "all", None, None, None, "不限", None)
    return _grouped(stmt.where(Document.id.in_(ids)))


def _listed(session: Session, stmt: Select) -> tuple[int, list[int]]:
    total = _count(session, stmt)
    if not total:
        return 0, []
    return total, [doc.id for doc, _ in session.execute(_ordered(stmt).limit(LIMIT)).all()]


def _expressible(conditions: dict[str, Any]) -> bool:
    """搜索页能表达同样的条件：不是整个标题、不是文号、没按往年和上周、上个月筛，关键词不超过
    1 个、不超过 50 字。"""
    keywords = conditions["keywords"]
    return (
        not conditions["title"]
        and not conditions["docNumber"]
        and conditions["year"] is None
        and conditions["period"] is None
        and conditions["time"] in TIMES
        and len(keywords) <= 1
        and all(len(k) <= SEARCH_Q_CHARS for k in keywords)
    )


def _more(total: int, link: bool) -> list[str]:
    if total <= LIMIT:
        return []
    return ["more"] if link else ["more", "narrow"]


def _answer(
    conditions: dict[str, Any], total: int, ids: list[int], notes: list[str], link: bool = False
) -> dict[str, Any]:
    return {
        "type": "find",
        "conditions": conditions,
        "total": total,
        "ids": ids,
        "link": link,
        "notes": notes,
    }


def run(session: Session, text: str, *, today: date) -> dict[str, Any]:
    """取条件、搜，返回要存的回答（格式见模块说明）。today 是北京时间的今天，认"今年""去年"、
    往年用；近 N 天、今年的范围和搜索页一样按 v1.now() 算。"""
    norm = normalize(text)
    if _has(norm, _PHOTO):
        return _answer(empty_conditions(), 0, [], ["photo"])
    parsed = parse(norm, today=today, covered=coverage(session))
    content = parsed.content

    def tail(total: int, link: bool = False) -> list[str]:
        return _more(total, link) + (["content"] if content and total else [])

    title_like = looks_like_title(norm)
    if title_like:
        ids = _title_ids(session, norm)
        total, shown = _listed(session, _matched(session, ids)) if ids else (0, [])
        if total:  # 一份都没比中（或比中的都下架了）就当普通的一句话按第 1–7 条找
            conditions = empty_conditions() | {"title": True}
            return _answer(conditions, total, shown, ["title", *tail(total)])

    conditions = parsed.conditions
    if parsed.number:
        ids = _number_ids(session, *parsed.number)
        total, shown = _listed(session, _matched(session, ids)) if ids else (0, [])
        return _answer(conditions, total, shown, tail(total) if total else ["doc_number_none"])
    if conditions["uncovered"] and not _has_area(conditions):
        return _answer(conditions, 0, [], ["uncovered"])  # 不去搜
    if _nothing(conditions):
        # 一个条件都没认出来：不去搜（不然就把全部通知都列出来了），请用户换个说法
        return _answer(conditions, 0, [], ["vague"])

    total, shown = _listed(session, statement(session, conditions))
    link = bool(total) and _expressible(conditions)
    notes = []
    if conditions["uncovered"]:
        notes.append("uncovered")
    if conditions["year"]:
        notes.append("year")
    if parsed.dropped:
        notes.append("first_three")
    if not total:
        notes.append("title_none" if title_like else "empty")
    return _answer(conditions, total, shown, notes + tail(total, link), link)


def _nothing(conditions: dict[str, Any]) -> bool:
    """没有关键词、地区、往年、上周或上个月，时间不限，类型不限：等于什么条件都没有。"""
    return (
        not conditions["keywords"]
        and not _has_area(conditions)
        and not conditions["uncovered"]
        and conditions["year"] is None
        and conditions["period"] is None
        and conditions["time"] == "不限"
        and conditions["kind"] == "all"
    )


def _has_area(conditions: dict[str, Any]) -> bool:
    return bool(conditions["regions"] or conditions["cities"] or conditions["districts"])


# ---- 读的时候 ----


def _area_label(key: str) -> str:
    """同小程序 areaLabel："山东·烟台""广东·深圳·南山区"；直辖市不重复写市名。"""
    province, city, district = ([*key.split("/"), "", ""])[:3]
    return "·".join(part for part in (province, city if city != province else "", district) if part)


def _span(period: dict[str, str]) -> str:
    """写成"2026 年 6 月 8 日至 6 月 14 日"；跨年时后一个也写年份。"""
    start, end = date.fromisoformat(period["from"]), date.fromisoformat(period["to"])
    tail = f"{end.month} 月 {end.day} 日"
    if end.year != start.year:
        tail = f"{end.year} 年 {tail}"
    return f"{start.year} 年 {start.month} 月 {start.day} 日至 {tail}"


def conditions_text(stored: dict[str, Any]) -> str:
    """条件那句（小程序写在"按这些条件找的："后面），如"地区 广东 · 类型 申报通知 · 发布 今年 ·
    标题里有 专精特新"。没去搜的（拍照、地区都没收录）和整个标题比中的为空串。"""
    conditions = stored["conditions"]
    if (
        conditions["title"]
        or "photo" in stored["notes"]
        or (conditions["uncovered"] and not _has_area(conditions))
    ):
        return ""
    if conditions["docNumber"]:
        return f"文号 {conditions['docNumber']}"
    areas = conditions["regions"] + [
        _area_label(key) for key in conditions["cities"] + conditions["districts"]
    ]
    if conditions.get("period"):
        published = f"发布于 {_span(conditions['period'])}"
    elif conditions["year"]:
        published = f"发布于 {conditions['year']} 年"
    else:
        published = "发布 " + ("不限时间" if conditions["time"] == "不限" else conditions["time"])
    parts = [
        f"地区 {'、'.join(areas) if areas else '不限'}",
        f"类型 {KIND_LABELS[conditions['kind']]}",
        published,
    ]
    if conditions["keywords"]:
        parts.append(f"标题里有 {'、'.join(conditions['keywords'])}")
    return " · ".join(parts)


def render(session: Session, stored: dict[str, Any], *, today: date) -> dict[str, Any]:
    """按存的编号重新取行给小程序：截止、核对状态、能不能解读都按现在的；下架、隐藏的不显示，
    份数记在 hidden（"有 N 份已下架，不再显示"）。total 是当时的总数。today 和 run 对应，
    行里的截止照搜索页按 v1.now() 算。缺了的项按空的算（调用方没有回答时传 {}）。"""
    conditions = empty_conditions() | (stored.get("conditions") or {})
    notes = list(stored.get("notes") or [])
    ids = [int(doc_id) for doc_id in stored.get("ids") or []]
    found = {}
    if ids:
        found = {
            doc.id: (doc, src) for doc, src in session.execute(_base().where(Document.id.in_(ids)))
        }
    rows = [found[doc_id] for doc_id in ids if doc_id in found]
    return {
        "type": "find",
        "conditions": conditions,
        "conditionsText": conditions_text({"conditions": conditions, "notes": notes}),
        "total": stored.get("total") or 0,
        "items": _items(session, rows, grouped=True, excerpts=True) if rows else [],
        "searchLink": search_params(conditions) if stored.get("link") else None,
        "notes": notes,
        "hidden": len(ids) - len(rows),
    }


# ---- 示例 ----

_EXAMPLES: dict[str, tuple[str, ...]] = {}  # 北京时间的"日期 小时" → 示例


def _province_counts(session: Session) -> list[tuple[str, int]]:
    """各已收录省份近 30 天的申报通知份数（同搜索页的口径），从多到少，同样多的按行政区划顺序。"""
    stmt = search_statement(session, None, "申报通知", None, None, None, "近 30 天", None)
    sub = stmt.with_only_columns(
        Document.id.label("doc_id"), Source.province.label("province")
    ).subquery()
    counts = session.execute(select(sub.c.province, func.count()).group_by(sub.c.province)).all()
    covered = coverage(session).provinces
    order = {name: index for index, name in enumerate(_PROVINCE_ORDER)}
    picked = [(p, n) for p, n in counts if p in order and p in covered and n]
    return sorted(picked, key=lambda pair: (-pair[1], order[pair[0]]))


def examples(session: Session, *, now: datetime) -> list[str]:
    """/chat/examples：近 30 天申报通知最多的两个已收录省份，写成"找[省份]近 30 天的申报通知"；
    每句按 run 真去查一遍，有结果才给，一句都没有就不给。按北京时间每小时算一次。"""
    stamp = now.astimezone(_TZ)
    hour = stamp.strftime("%Y-%m-%d %H")
    cached = _EXAMPLES.get(hour)
    if cached is None:  # 几个请求同时算也没关系，结果一样；只留当前这个小时的
        cached = tuple(
            sentence
            for sentence in (EXAMPLE.format(p) for p, _ in _province_counts(session)[:2])
            if run(session, sentence, today=stamp.date())["total"]
        )
        _EXAMPLES.clear()
        _EXAMPLES[hour] = cached
    return list(cached)
