"""政府文件详情页解析；只保留可从页面直接确认的字段。"""

import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape as unescape_html
from itertools import chain
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from youju.crawler.dates import parse_date_detail
from youju.crawler.urls import normalize_url
from youju.issuer import normalize_issuer

_FILE_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "wps", "et", "zip", "rar", "ofd"}
# 一篇通知收录的附件个数上限：正常通知不过几个到几十个，异常页面（整站文件目录等）不全收
MAX_ATTACHMENTS = 50
_FILE_PATTERN = re.compile(r"\.(pdf|docx?|xlsx?|wps|et|zip|rar|ofd)(?:$|[?#])", re.I)
_DOC_NUMBER = re.compile(
    r"[\u4e00-\u9fff]{1,16}(?:〔\s*20\d{2}\s*〕|\[\s*20\d{2}\s*\]"
    r"|（\s*20\d{2}\s*）|\(\s*20\d{2}\s*\)|【\s*20\d{2}\s*】)\s*\d{1,5}\s*号"
)
# 手机号；后面跟着一两位小数的是金额（"13812345678.50 元"），不算
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)(?!\.\d{1,2}(?!\d))")
_ID_CARD = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]|\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
# 旧的账号写法：标签后面直接跟 12–19 位。附件副本的检查（has_personal_numbers）用它；正文用 _ACCOUNT
# （见下），_ACCOUNT 一位都没遮的才照它遮。空白、冒号用占有量词（结果和普通写法一样），"账号"后面
# 一长串空白不会卡住
_BANK = re.compile(
    r"((?:银行卡|银行账号|银行帐号|账户|账号|帐号|卡号)\s*+[:：]?+\s*+)(?:\d[ -]?){11,18}\d(?!\d)"
)
# 邮箱只认英文字母、数字和 ._%+- 组成的地址（同 api/me.py、附件文字）：以前用 \w，汉字也算，
# 从上一个标点起连着邮箱的整段中文都被遮掉（"PDF电子版于7月2日前发送至abc@163.com"整段没了）。
# 不往前看（两个邮箱用"-""_"连着写时，后一个第一遍就遮掉，遮过的再过一遍不变）；用户名最多 64 个字，
# 一长串英文字母不会让查找变慢
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
_LANDLINE = r"0\d{2,3}[-－]\d{7,8}"
_PHONE = re.compile(rf"(?<!\d){_LANDLINE}(?!\d)")
_AGENCY = re.compile(
    r"^[\u4e00-\u9fff（）()、\s]{3,100}(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)(?:（[^）]{1,20}）)?$"
)
_AGENCY_END = re.compile(
    r"(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)(?:（[^）]+）)?$"
)
_DATE = re.compile(r"20\d{2}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}\s*日?")
_JUNK_CLASS = re.compile(
    r"(?:share|related|footer|breadcrumb|toolbar|copyright|qrcode|qr-code|print|pagination)", re.I
)
_BODY_SELECTORS = (
    '//*[@id="zoom"]',
    '//*[@id="UCAP-CONTENT" or @id="contentText"]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " TRS_Editor ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article_content ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " html-content-fun ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " main-txt ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article-content ")]',
    "//article",
    '//*[contains(concat(" ", normalize-space(@class), " "), " pages_content ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article_area ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " content ")]',
)
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "br"}


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str
    extension: str


@dataclass
class DetailResult:
    body_text: str = ""
    issuer: str | None = None
    doc_number: str | None = None
    publish_date: date | None = None
    attachments: list[Attachment] = field(default_factory=list)
    failure_reason: str | None = None
    # 页面自己标明的标题：ArticleTitle 元数据、h1。只用来补全列表页截断的标题（crawler/titles.py）
    page_titles: list[str] = field(default_factory=list)
    # 脱敏前的正文：只在内存里用（一次性重抓 youju refetch-redacted 拿它核对原网页变没变），不入库
    raw_body: str = field(default="", repr=False)


def redact_sensitive(text: str) -> str:
    """入库和测试快照共用的脱敏规则；不更改原站链接。

    正文给出前（详情页、AI 解读、申报条件）还会按现行规则再过一遍，遮过的文字再过一遍不会变。
    只遮号码、邮箱、姓名本身，不连带遮旁边的字：以前邮箱、座机前面连着的中文（常常是截止日期、
    单位名）会被一起遮掉。"""
    text = _ID_CARD.sub("[身份证号已脱敏]", text)
    text = _MOBILE.sub("[手机号已脱敏]", text)
    text = _ACCOUNT.sub(_account, text)
    text = _EMAIL.sub("[邮箱已脱敏]", text)
    text = _PHONE.sub("[电话已脱敏]", text)
    # 姓名最后认：邮箱、座机先换成"[…已脱敏]"，姓名前后挨着的是"]"，第一遍和再过一遍看到的一样
    return _mask_names(text)


# ---- 银行账号 ----
# 标签："账号""账户号码""银行卡号""开户账号"……，后面可以带括号说明（"账号（基本户）："）、冒号、
# "为""是"；标签和号码中间还可以隔着开户行（"开户行及账号：中国工商银行杭州分行 1202…"）。
# 只写"开户行"、后面先是行名再是号码的也算。写法和附件文字（attachment_text）一致。
_HAN = f"{chr(0x4E00)}-{chr(0x9FFF)}"  # 汉字（基本区），放进正则的方括号里用
_ACCOUNT_WORD = r"(?:银行卡|银行[账帐]号|[账帐]户|[账帐]号|卡号)(?:号码|号|码)?"
# 空白、冒号、"为是"都用占有量词（*+ ?+）：它们和后面的字不重叠，不回退也不会少匹配；"账号"后面
# 跟着一长串空格（PDF 里有）时，普通的 \s*[:：]?\s* 要回退很多次，几万个空格就卡住
_ACCOUNT_GLUE = r"\s*+[:：]?+\s*+(?:[为是]\s*+[:：]?+\s*+)?+"
_BANK_NAME = (
    rf"(?:[{_HAN}A-Za-z（）()]{{0,30}}?"
    r"(?:银行|支行|分行|营业部|信用社|合作社|农商行|农信社|联社|储蓄所)"
    rf"|[{_HAN}A-Za-z]{{0,20}}?(?:工行|农行|中行|建行|交行|招行|邮储))"
    r"(?:[(（][^()（）\n]{1,12}[)）])?+[\s,，:：]*+(?:[为是]\s*+[:：]?+\s*+)?+"
)
_ACCOUNT = re.compile(
    rf"((?:{_ACCOUNT_WORD}(?:\s*+[(（][^()（）\n]{{1,20}}[)）])?+{_ACCOUNT_GLUE}(?:{_BANK_NAME})?"
    rf"|开户(?:银)?行{_ACCOUNT_GLUE}{_BANK_NAME}))"
    r"(\d++(?:[ -]\d++)*+)"
)
_DIGITS = re.compile(r"\d+")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_YMD = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")


def _date_from(values: list[str], i: int) -> bool:
    """第 i 组起是一个日期：8 位连写（"20251010"），或年、月、日三组（"2025 10 10"）。"""
    if len(values[i]) == 8 and _YMD.fullmatch(values[i]):
        return True
    ymd = values[i : i + 3]
    if len(ymd) < 3 or len(ymd[0]) != 4 or not all(1 <= len(v) <= 2 for v in ymd[1:]):
        return False
    return bool(_YMD.fullmatch(f"{ymd[0]}{int(ymd[1]):02d}{int(ymd[2]):02d}"))


def _account(match: re.Match) -> str:
    """标签后面的一串数字（组和组之间隔一个空格或横线），从第一组起往后接，最多接到 30 位。第二组起
    碰到这几种就不接：日期（"20251010""2025 10 10"）；年份（19xx、20xx）后面跟着"年"（中间可以
    隔空格，"账号：1234567890 2025 年11月"），或跟着"."、"/"再跟数字（"2025.10.10"）；一两位数后面
    紧跟"日"、或（可以隔空格）跟"月"；后面（可以隔空格）跟"元、万、亿"而接上就超过 19 位的（旧规则
    最多遮 19 位，本来就接不到这一组）；第一组已经 12 位以上而接上就超过 22 位的
    （"1202…568 1500"）。
    后面跟着别的字不管（"日期：""号码""时间""分行"开头的词常紧挨着账号写）。第一组不看后面跟的字。
    接上的有 10–30 位才遮，没接上的原样留着；一位都没遮、旧规则（_BANK）却会遮的，照旧规则遮——
    红线上的号码不能比以前漏得多。"""
    label, run = match.group(1), match.group(2)
    whole = match.group()
    groups = list(_DIGITS.finditer(run))
    values = [group.group() for group in groups]
    tail = match.string[match.end() : match.end() + 4]
    total = taken = 0
    for i, group in enumerate(groups):
        size = len(values[i])
        if i:
            rest = run[group.end() : group.end() + 4] if group.end() < len(run) else tail
            following = rest[:1]
            spaced = rest.lstrip(" \t\u3000")[:2]
            year = bool(_YEAR.fullmatch(values[i]))
            if (
                _date_from(values, i)
                or (
                    year
                    and (spaced[:1] == "年" or (following in (".", "/") and rest[1:2].isdigit()))
                )
                or (size <= 2 and (following == "日" or spaced[:1] == "月"))
                or (spaced[:1] in ("元", "万", "亿") and total + size > 19)
                or (len(values[0]) >= 12 and total + size > 22)
            ):
                break
        if total + size > 30:
            break
        total += size
        taken = i + 1
    if 10 <= total <= 30:
        cut = groups[taken - 1].end()
        return f"{label}[银行账号已脱敏]{run[cut:]}"
    if old := _BANK.search(whole):
        return f"{whole[: old.start()]}{old.group(1)}[银行账号已脱敏]{whole[old.end() :]}"
    return whole


# ---- 姓名 ----
# 两处当姓名遮：
# 1. "联系人："（"联系人及电话：""联 系 人："……）后面这一行，到标点、数字为止：先跳过单位名
#    （以"处、局、中心"等结尾的、5 个字以上的），第一个像姓名的词遮掉（2–4 个汉字，"张 蕾"这样
#    两个单字中间隔空格的，带"·"的）；紧跟着的（只隔空格、顿号）以常见姓氏开头的也遮（几个联系人）。
# 2. 座机号码前面（中间只隔空白，可以换行）自成一段的词（前面是开头、空白或标点，不是汉字、数字、
#    字母）：以常见姓氏开头的 2–3 个汉字（复姓到 4 个）或"张 蕾"，不以单位、地名的字结尾，就遮掉；
#    它前面同一行只隔空格、顿号的词也一样看（"省科技厅 张三 李四 025-…"）。
# 以前把座机前面连着的 2–4 个汉字一律当姓名遮（"11月20日前寄至0571-…"的"日前寄至"、
# "省工信厅中小企业处 赵 立 0351-…"的"业处 赵 立"），截止日期、单位名都没了；"联系人："后面
# 一律遮前 2–8 个汉字，"联系人：省工信厅汽车工业处 张三"遮掉的是单位，姓名反倒留着。
_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶"
    "程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付傅方白邹孟熊秦邱江尹薛闫阎段雷侯龙史陶黎"
    "贺顾毛郝龚邵万钱严覃武戴莫孔向汤常温康施文牛樊葛邢安齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧"
    "耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕单季裴霍涂成苗谷盛曲翁冉骆蓝路游辛靳管柴蒙鲍"
    "华喻祁蒲房滕屈饶解牟艾尤时穆司卓吉缪简车项芦麦褚娄窦戚岑景党宫费卜冷晏席卫米柏宗瞿桂佟"
    "应臧闵苟邬边卞姬师仇栾隋商刁沙荣巫寇桑郎甄丛仲虞敖巩佘池查麻苑迟邝封谈匡鞠惠荆冀郁胥班"
    "储栗燕楚鄢劳谌奚皮粟冼蔺楼满闻厉伊仝郜阚屠豆朴盖练廉禹井祖漆巴丰支卿狄敬扈晁芮浦戈伏鹿"
    "薄邸雍辜羊母裘亓修邰赫杭况宿鲜逯茹亢嵇湛戎勾茅揭尉檀雎吾赖洪原蔚"
)
_COMPOUND_SURNAMES = (
    "欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "令狐", "长孙",
    "宇文", "司徒", "夏侯", "轩辕", "端木", "南宫", "西门", "独孤", "申屠", "澹台", "濮阳",
    "淳于", "单于", "太史", "钟离", "宗政", "拓跋", "呼延", "百里", "东郭", "闻人", "赫连",
    "左丘", "公冶", "公羊", "万俟", "司空",
)  # fmt: skip
# 单位、地名常用的结尾（"高新处""服务中心""朝阳区"）：以这些结尾的词不当姓名
_UNIT_ENDS = (
    *"处科室局厅委办部所院站队组股会司馆社校市区县州镇乡村省署府团路街巷号楼厦",
    "中心", "银行", "支行", "分行", "公司", "集团", "基地", "园区", "协会", "学会", "商会",
    "联盟", "平台", "大道",
)  # fmt: skip
# 含这些词的是标签、表头，不是姓名（"联系电话""技术支持""推荐单位"）
_LABEL_WORDS = (
    "电话", "传真", "热线", "座机", "手机", "号码", "方式", "咨询", "联系", "办公", "值班",
    "支持", "地址", "邮箱", "邮编", "时间", "单位", "部门", "序号", "备注", "姓名",
)  # fmt: skip
# 光是称呼、前面没有姓的不是姓名（"Ryan LEE 先生"的"先生"）
_TITLES = ("先生", "女士", "老师", "同志")
# 以常见姓氏开头、常挨着电话写的普通词（"值班电话：白天 0571-…"）：不当姓名
_COMMON_WORDS = frozenset(
    (
        "白天", "周末", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
        "项目", "申报", "金融", "高新", "安全", "文化", "成果", "管理", "服务", "常年",
    )
)  # fmt: skip
# 以常见姓氏开头、不带"省、市"的省份和地级市名（"石家庄 0311-…"这样按城市列的联系电话）：
# 不当姓名。别的省市名不以这些姓氏开头，本来就不会被当成姓名；县、区名太多，不列
_PLACE_NAMES = frozenset(
    (
        "江苏", "江西", "安徽", "甘肃", "宁夏", "吉林",
        "石家庄", "唐山", "秦皇岛", "邢台", "张家口", "吕梁", "包头", "沈阳", "白山", "白城",
        "伊春", "宿迁", "宁波", "金华", "马鞍山", "安庆", "黄山", "龙岩", "宁德", "吉安",
        "安阳", "焦作", "濮阳", "许昌", "商丘", "周口", "武汉", "黄石", "荆门", "黄冈", "邵阳",
        "岳阳", "常德", "张家界", "娄底", "江门", "湛江", "揭阳", "桂林", "成都", "巴中",
        "安顺", "毕节", "曲靖", "林芝", "安康", "商洛", "金昌", "白银", "武威", "张掖",
        "石嘴山", "吴忠", "伊犁", "甘孜", "楚雄", "文山", "甘南", "黄南",
    )
)  # fmt: skip
# "联系人张三 0571-…"这样标签后面没写冒号、紧贴着姓名的：标签后面那截当姓名看（同单位名）
_PERSON_LABELS = ("联系人", "联络人", "经办人")
_CONTACT_LABEL = re.compile(
    r"联\s*+系\s*+人(?:\s*+[及和与]?+\s*+(?:联\s*+系\s*+)?+(?:电\s*+话|方\s*+式))?+"
    r"\s*+[:：][^\S\n]*+\n?+[^\S\n]*+"
)
# "联系人："后面看到这里为止：换行、标点、数字（号码）、"["（遮过的号码）
_NAME_STOP = re.compile(r"[\n，,。；;：:（(）)/【\[]|\d")
# 同一行里隔开几个姓名的：空格、顿号
_NAME_GAP = re.compile(r"(?:[^\S\n]|、)+")
_HAN_RUN = re.compile(rf"[{_HAN}·]+")
_BEFORE_PHONE = re.compile(rf"(?<!\d){_LANDLINE}(?!\d)|\[电话已脱敏\]")


def _is_han(char: str) -> bool:
    return 0x4E00 <= ord(char) <= 0x9FFF or char == "·"


def _inline_space(char: str) -> bool:
    return char.isspace() and char not in "\r\n"


def _name_like(word: str, *, after_label: bool = False) -> bool:
    """word 像不像姓名（"张 蕾"传进来时去掉了中间的空格）。after_label：紧跟"联系人："的第一个
    像姓名的词，不看姓氏，2–4 个字，带"·"的 3–12 个字；其他的要以常见姓氏开头，2–3 个字（复姓
    4 个）。以单位、地名的字结尾的，含"电话""单位"这类标签字的，光是"先生""老师"的，都不算。"""
    if (
        word in _TITLES
        or word in _COMMON_WORDS
        or word in _PLACE_NAMES
        or any(label in word for label in _LABEL_WORDS)
    ):
        return False
    surname = word[0] in _SURNAMES or word.startswith(_COMPOUND_SURNAMES)
    if word.endswith(_UNIT_ENDS):
        # "联系人："后面两个字、以常见姓氏开头的，以"路、镇、科"这类字结尾也是姓名
        # （"联系人：王镇"）；别处以单位、地名的字结尾的不当姓名（"高新处""沙县"）
        return after_label and len(word) == 2 and surname
    if after_label:
        return 2 <= len(word) <= 4 or ("·" in word and 3 <= len(word) <= 12)
    if "·" in word or not surname:
        return False
    return 2 <= len(word) <= 3 or (len(word) == 4 and word.startswith(_COMPOUND_SURNAMES))


def _glued_name(run: str) -> int | None:
    """单位名后面紧跟着姓名、中间没隔开的（"省财政厅张三""办公室李四"，还有没写冒号的"联系人张三"）：
    前面一截以单位的字或"联系人"结尾、后面 2–4 个字以常见姓氏开头，返回姓名从第几个字起；不是这样的
    返回 None。"""
    # 长的优先：复姓"司马"的"司"也是单位的字（"省科技厅司马小明"不能切成"省科技厅司|马小明"）
    for size in (4, 3, 2):
        cut = len(run) - size
        if (
            cut >= 2
            and run[:cut].endswith((*_UNIT_ENDS, *_PERSON_LABELS))
            and _name_like(run[cut:])
        ):
            return cut
    return None


def _words(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """start–end 之间的词 [(开始, 结束, 词)]：连着的汉字（可以带"·"）；两个单字中间只隔空格的
    （"张 蕾"）算一个词，词里去掉空格。"""
    runs = [(m.start(), m.end()) for m in _HAN_RUN.finditer(text, start, end)]
    words, i = [], 0
    while i < len(runs):
        a, b = runs[i]
        if b - a == 1 and i + 1 < len(runs):
            c, d = runs[i + 1]
            if d - c == 1 and all(_inline_space(char) for char in text[b:c]):
                words.append((a, d, text[a] + text[c]))
                i += 2
                continue
        words.append((a, b, text[a:b]))
        i += 1
    return words


def _contact_names(text: str) -> list[tuple[int, int]]:
    """ "联系人："后面这一行要遮的姓名 [(开始, 结束)]。"""
    spans: list[tuple[int, int]] = []
    for label in _CONTACT_LABEL.finditer(text):
        stop = _NAME_STOP.search(text, label.end())
        end = stop.start() if stop else len(text)
        # 冒号后面换了行才写的（表格的"联系人"一格，下一格才是内容，也可能是表头）：第一个词就要像
        # 姓名、以常见姓氏开头，不跳过前面的词
        next_line = "\n" in label.group()
        named = False
        for a, b, word in _words(text, label.end(), end):
            if text.startswith("[姓名已脱敏]", b):
                # 紧贴着"[姓名已脱敏]"的是旧规则吞剩的半截（"联系人及电话：中小企[姓名已脱敏]"），
                # 不是姓名：库里按旧规则遮过的正文再过一遍不能再吞
                break
            if named:
                # 第一个姓名后面只隔空格、顿号的，以常见姓氏开头的也遮（几个联系人）
                if not (_NAME_GAP.fullmatch(text[spans[-1][1] : a]) and _name_like(word)):
                    break
                spans.append((a, b))
            elif _name_like(word, after_label=not next_line):
                spans.append((a, b))
                named = True
            elif word == text[a:b] and (cut := _glued_name(word)) is not None:
                spans.append((a + cut, b))
                named = True
            elif next_line:
                break
            # 同一行的单位名（"联系人：省工信厅汽车工业处 张三"）跳过，接着往后看
    return spans


def _word_ending_at(text: str, end: int) -> tuple[int, str] | None:
    """text[:end] 末尾自成一段的词：(开始, 词)。前面要是开头、空白或标点（不是汉字、数字、字母）；
    连着的汉字（可以带"·"），或者两个单字中间隔空格的"张 蕾"（词里去掉空格）。"""
    start = end
    while start > 0 and _is_han(text[start - 1]):
        start -= 1
    if start == end or (start > 0 and text[start - 1].isalnum()):
        return None
    if end - start > 1:
        return start, text[start:end]
    gap = start
    while gap > 0 and _inline_space(text[gap - 1]):
        gap -= 1
    first = gap - 1
    if gap == start or first < 0 or not _is_han(text[first]) or text[first] == "·":
        return None
    if first > 0 and (_is_han(text[first - 1]) or text[first - 1].isalnum()):
        return None
    return first, text[first] + text[start:end]


def _phone_names(text: str) -> list[tuple[int, int]]:
    """座机号码前面要遮的姓名 [(开始, 结束)]。"""
    spans = []
    for phone in _BEFORE_PHONE.finditer(text):
        end = phone.start()
        while end > 0 and text[end - 1].isspace():
            end -= 1
        while (found := _word_ending_at(text, end)) is not None:
            start, word = found
            if not _name_like(word):
                # 单位名后面紧跟着姓名（"省财政厅张三 0531-…"）：只遮姓名
                if word == text[start:end] and (cut := _glued_name(word)) is not None:
                    spans.append((start + cut, end))
                break
            spans.append((start, end))
            # 同一行前面只隔空格、顿号的词接着看（"省科技厅 张三 李四 025-…"）
            end = start
            while end > 0 and (text[end - 1] == "、" or _inline_space(text[end - 1])):
                end -= 1
            if end == start:
                break
    return spans


def _mask_names(text: str) -> str:
    spans = sorted(set(_contact_names(text)) | set(_phone_names(text)))
    out: list[str] = []
    at = 0
    for start, end in spans:
        if start < at:  # 两条规则找到同一个词
            at = max(at, end)
            continue
        if out and _NAME_GAP.fullmatch(text[at:start]):
            at = end  # 和前一个姓名只隔空格、顿号：合成一个"[姓名已脱敏]"
            continue
        out += [text[at:start], "[姓名已脱敏]"]
        at = end
    return "".join(out) + text[at:]


# ---- 附件能不能存副本的检查（只用在这里，比正文脱敏宽：附件是原文件、没法脱敏，宁可多拦） ----
# 正文脱敏（redact_sensitive）不用这套写法：正文里年份、金额、编号连写的情况多，放宽会误伤正文。

# 全角数字、横线、空格、加号、X 换成半角再查
_HALF_WIDTH = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
    | {"\uff0d": "-", "\u3000": " ", "\uff0b": "+", "\uff38": "X", "\uff58": "x"}
)
# 数字之间任意长度的空白（空格、全角空格、制表符、换行）和横线（含各种破折号）：
# 去掉后"138 1234 5678""138    1234-5678"等连成一串再查
_DIGIT_GAP = re.compile(r"(?<=\d)[\s\-\u2010-\u2015\u2212]+(?=[\dXx])")
# 号码中间的分隔（分段写法用）
_SEP = r"[\s\-\u2010-\u2015\u2212]+"
# 连起来后的手机号，允许前面带 86（+86 138 1234 5678）
_MOBILE_JOINED = re.compile(r"(?<!\d)(?:86)?1[3-9]\d{9}(?!\d)")
# 3-4-4 分段写的手机号：和前后的数字只隔一个空格时，连起来会和别的数字连成一长串，单独再找一遍
_MOBILE_GROUPED = re.compile(rf"(?<!\d)1[3-9]\d{_SEP}\d{{4}}{_SEP}\d{{4}}(?!\d)")
# 银行卡号：16–19 位数字，前面不用有"账号"等字样，通过 Luhn 校验才算
_CARD_RUN = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_CARD_GROUPED = re.compile(rf"(?<!\d)\d{{4}}(?:{_SEP}\d{{4}}){{3}}(?:{_SEP}\d{{1,3}})?(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def has_personal_numbers(text: str) -> bool:
    """有没有合规红线列出的个人信息：身份证号、个人手机号、银行账号。附件副本是原文件，没法脱敏，
    有这些就不存副本（youju/attachments.py）。

    比正文脱敏宽：数字中间带空格、横线的也算（先去掉再查，没去掉的也查一遍，免得和旁边的数字
    连成一长串查不出），16–19 位、通过 Luhn 校验的数字串不论前面写没写"账号"都当作银行卡号。"""
    plain = text.translate(_HALF_WIDTH)
    joined = _DIGIT_GAP.sub("", plain)
    for version in (plain, joined):
        if any(p.search(version) for p in (_ID_CARD, _MOBILE_JOINED, _BANK)):
            return True
    if _MOBILE_GROUPED.search(plain):
        return True
    cards = chain(
        _CARD_RUN.finditer(plain), _CARD_RUN.finditer(joined), _CARD_GROUPED.finditer(plain)
    )
    return any(_luhn(re.sub(r"\D", "", m.group())) for m in cards)


def _plain_lines(node: etree._Element) -> list[str]:
    parts: list[str] = []

    def visit(element: etree._Element) -> None:
        if not isinstance(element.tag, str):
            return
        if element.tag.lower() in {"script", "style", "noscript", "nav", "footer"}:
            return
        marker = f"{element.get('class', '')} {element.get('id', '')}"
        if _JUNK_CLASS.search(marker):
            return
        if element.tag.lower() == "br":
            parts.append("\n")
        if element.text:
            parts.append(element.text)
        for child in element:
            visit(child)
            if child.tail:
                parts.append(child.tail)
        if element.tag.lower() in _BLOCK_TAGS:
            parts.append("\n")

    visit(node)
    return [line for raw in "".join(parts).splitlines() if (line := " ".join(raw.split()))]


def _body_node(root: etree._Element) -> etree._Element | None:
    for selector in _BODY_SELECTORS:
        nodes = root.xpath(selector)
        candidates = [n for n in nodes if len(" ".join(_plain_lines(n))) >= 20]
        if candidates:
            return max(candidates, key=lambda n: len(" ".join(_plain_lines(n))))
    return _density_body_node(root)


def _density_body_node(root: etree._Element) -> etree._Element | None:
    """站点没有常见正文容器时，按非链接文字量和段落密度选择内容块。"""
    candidates: list[tuple[float, int, etree._Element]] = []
    for node in root.xpath("//main|//article|//section|//div"):
        if _JUNK_CLASS.search(f"{node.get('class', '')} {node.get('id', '')}"):
            continue
        lines = _plain_lines(node)
        text = " ".join(lines)
        if len(text) < 40:
            continue
        link_chars = sum(len(" ".join(a.itertext())) for a in node.xpath(".//a"))
        if link_chars / max(len(text), 1) > 0.35:
            continue
        paragraphs = sum(1 for line in lines if len(line) >= 20)
        if paragraphs < 2 and len(text) < 100:
            continue
        depth = len(node.xpath("ancestor::*"))
        score = len(text) - 2 * link_chars + min(paragraphs, 20) * 12
        candidates.append((score, depth, node))
    if not candidates:
        return None
    best = max(score for score, _, _ in candidates)
    return max((c for c in candidates if c[0] >= best * 0.85), key=lambda c: c[1])[2]


def _metadata(root: etree._Element, name: str) -> str | None:
    for node in root.xpath("//meta[@name or @property]"):
        key = (node.get("name") or node.get("property") or "").lower()
        if key == name.lower():
            return (node.get("content") or "").strip() or None
    return None


def _page_titles(root: etree._Element) -> list[str]:
    """政府网站常用的 ArticleTitle 元数据（10/16 查的 8 个站点里 6 个有），再加页面的 h1
    （青海科技厅、西藏经信厅只在 h1 里写完整标题）。<title> 常带站名、栏目名或只有站名，不用。"""
    values = [_metadata(root, "ArticleTitle") or ""]
    values += [h1.text_content() for h1 in root.xpath("//h1")]
    titles = [title for value in values if (title := " ".join(value.split()))]
    # 标题入库不脱敏：带手机号、身份证号等个人信息的（正常通知标题不会有）不收，宁可不补全
    return [title for title in titles if redact_sensitive(title) == title]


def _issuer(lines: list[str], root: etree._Element, prefix: str) -> str | None:
    # 落款比站点的 ContentSource 更具体，多部门联合发文也应完整保留。
    for i in range(len(lines) - 1, max(-1, len(lines) - 15), -1):
        if not _DATE.fullmatch(lines[i]):
            continue
        agencies: list[str] = []
        for line in reversed(lines[max(0, i - 8) : i]):
            if _AGENCY.fullmatch(line) and len(line) <= 60:
                agencies.append(_split_issuer_line(line))
            elif agencies:
                break
        if agencies:
            return "、".join(reversed(agencies))
    for match in re.finditer(
        r"(?:发文机关|发文单位|发布机构|制发机关)\s*[:：]\s*([^\n]{2,80})", prefix
    ):
        value = match.group(1).strip()
        if _AGENCY_END.search(value):
            return normalize_issuer(value)
    for name in ("issuer", "publishingorganization", "contentSource"):
        value = _metadata(root, name)
        if value and len(value) <= 60 and _AGENCY_END.search(value):
            return normalize_issuer(value)
    return None


def _split_issuer_line(line: str) -> str:
    """落款中的空格可分隔联合发文机关，也可只是总局与地方分支间的排版空格。"""
    return normalize_issuer(line)


def _doc_number(lines: list[str], root: etree._Element, prefix: str) -> str | None:
    for name in ("documentNumber", "docNumber", "fileno"):
        value = _metadata(root, name)
        if value and (match := _DOC_NUMBER.search(value)):
            return "".join(match.group().split())
    for line in prefix.splitlines():
        if len(line) <= 100 and (match := _DOC_NUMBER.search(line)):
            return "".join(match.group().split())
    for line in lines[:3]:
        if (match := _DOC_NUMBER.fullmatch(line)) or (
            re.match(r"^(?:文号|发文字号|文件编号)\s*[:：]", line)
            and (match := _DOC_NUMBER.search(line))
        ):
            return "".join(match.group().split())
    return None


def _publish_date(root: etree._Element, prefix: str) -> date | None:
    for name in ("PubDate", "publishDate", "published_time", "article:published_time"):
        value = _metadata(root, name)
        if value:
            parsed, explicit_year = parse_date_detail(value)
            if explicit_year:
                return parsed
    match = re.search(
        r"(?:发布日期|发布时间|发布于|发稿时间|日期)\s*[:：]?\s*(20\d{2}[^\n]{0,20})",
        prefix,
    )
    if match:
        parsed, explicit_year = parse_date_detail(match.group(1))
        if explicit_year:
            return parsed
    return None


# 有的政府网站（TRS 模板，如重庆市科技局、宁夏科技厅和工信厅）把附件链接写在脚本的字符串里
# （var hasFJ='<a href="./P0….pdf">附件…</a><BR/>…'），浏览器运行后才显示，页面结构里没有这些链接。
# 10/15 查"查名单"读不出企业名时发现：重庆入库科技型企业名单、宁夏专精特新名单在这里。
# 只看页面自带的脚本（不看 src 引用的外部脚本），找 <a href=…>…</a> 片段，按同样的规则收附件。
_SCRIPT_ANCHOR = re.compile(
    r"""<a\s[^<>]*?href\s*=\s*\\?["']([^"'\\<>]+)\\?["'][^<>]*>(.*?)</a>""", re.I | re.S
)


def _anchors(root: etree._Element):
    """页面里的链接（href、文字），再加上写在页面自带脚本里的链接片段。"""
    for anchor in root.xpath("//a[@href]"):
        yield anchor.get("href"), " ".join(anchor.text_content().split())
    for script in root.xpath("//script[not(@src)]"):
        # 只还原常见的斜杠转义，不执行脚本或求值拼接表达式。
        source = (script.text or "").replace(r"\/", "/")
        for match in _SCRIPT_ANCHOR.finditer(source):
            href, inner = match.groups()
            yield (
                unescape_html(href),
                " ".join(unescape_html(re.sub(r"<[^>]+>", " ", inner)).split()),
            )


# 各省政府网站群统一的附件文件服务器：页面在这个省的政府网站上（主机名以后缀结尾）时，
# 链到这台服务器的文件算附件，链接文字里不写"附件"也收。只列核实过的，
# 不按 gov.cn 子域笼统放开，免得把外链误认成附件。
# - 10/16 河南：科技厅等的附件放在 xcoss.henan.gov.cn
#   （2026 年第一批 11903 家入库科技型中小企业名单）
_FILE_SERVERS = {"xcoss.henan.gov.cn": ".henan.gov.cn"}


def _on_file_server(target: str, page_host: str | None) -> bool:
    """链到本省政府网站群统一的附件文件服务器（_FILE_SERVERS，主机名精确匹配，只认默认端口），
    且页面在这个省的政府网站上（主机名以"."加省份后缀结尾）。"""
    parts = urlsplit(target)
    suffix = _FILE_SERVERS.get(parts.hostname or "")
    if suffix is None or not page_host or not page_host.lower().endswith(suffix):
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    return port is None or port == {"http": 80, "https": 443}.get(parts.scheme)


def _same_site(target: str, page_host: str | None) -> bool:
    """同一个网站：主机名相同，或者是本省的附件文件服务器（_on_file_server）。"""
    host = urlsplit(target).hostname
    if not host or not page_host:
        return False
    return host == page_host or _on_file_server(target, page_host)


def _attachments(root: etree._Element, url: str) -> list[Attachment]:
    result: list[Attachment] = []
    seen: set[str] = set()
    page_host = urlsplit(url).hostname
    for href, label in _anchors(root):
        target = normalize_url(url, href)
        if not target or target in seen:
            continue
        filename = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
        match = _FILE_PATTERN.search(target) or _FILE_PATTERN.search(label)
        if not match or match.group(1).lower() not in _FILE_EXTENSIONS:
            continue
        # 外站 PDF 往往是正文引用。只有明确标作附件、或同一个政府网站群的才收入附件题录。
        if not _same_site(target, page_host) and "附件" not in label:
            continue
        if redact_sensitive(target) != target:
            continue
        name = label or filename
        seen.add(target)
        result.append(Attachment(redact_sensitive(name), target, match.group(1).lower()))
        if len(result) >= MAX_ATTACHMENTS:
            break
    return result


def parse_detail(html_text: str, url: str) -> DetailResult:
    """解析公开 HTML；防护页返回失败原因，字段不猜测。"""
    if not html_text.strip():
        return DetailResult(failure_reason="页面内容为空")
    try:
        root = html.fromstring(html_text)
    except (etree.ParserError, ValueError) as exc:
        return DetailResult(failure_reason=f"HTML 无法解析：{exc.__class__.__name__}")
    title = " ".join(root.xpath("//title/text()"))[:200]
    body = root.xpath("//body")
    visible = "\n".join(_plain_lines(body[0])) if body else ""
    if re.search(
        r"验证码|人机验证|安全验证|访问过于频繁|异常访问|请开启.{0,12}JavaScript",
        title + visible[:500],
        re.I,
    ):
        return DetailResult(failure_reason="防护页或验证码")
    node = _body_node(root)
    if node is None:
        attachments = _attachments(root, url)
        return DetailResult(
            issuer=_issuer([], root, visible[:700]),
            doc_number=_doc_number([], root, visible[:700]),
            publish_date=_publish_date(root, visible[:700]),
            attachments=attachments,
            failure_reason=None if attachments else "未识别到正文容器",
            page_titles=_page_titles(root),
        )
    lines = _plain_lines(node)
    raw_body = "\n".join(lines)
    prefix = (
        visible.split(lines[0], 1)[0][-700:] if lines and lines[0] in visible else visible[:700]
    )
    attachments = _attachments(root, url)
    # PDF-only 页面只有下载入口，HTML 正文为空；仍保留附件题录。
    if len(raw_body) < 30 or ("点击正文" in raw_body and not any(len(line) > 60 for line in lines)):
        raw_body = ""
    return DetailResult(
        body_text=redact_sensitive(raw_body),
        raw_body=raw_body,
        issuer=_issuer(lines, root, prefix),
        doc_number=_doc_number(lines, root, prefix),
        publish_date=_publish_date(root, prefix),
        attachments=attachments,
        failure_reason=None if raw_body or attachments else "未识别到正文或附件",
        page_titles=_page_titles(root),
    )
