"""政府文件详情页解析；只保留可从页面直接确认的字段。"""

import bisect
import functools
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from html import unescape as unescape_html
from itertools import chain
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree, html

from youju.crawler.dates import parse_date_detail
from youju.crawler.urls import normalize_url
from youju.issuer import normalize_issuer

logger = logging.getLogger(__name__)

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
# "@""."也认全角的"＠""．"（11/18：有的网站这样写，以前漏遮）。写法和显示层（api/v1.py 的
# _EMAIL_ANY）对齐（11/18 Codex 第三轮）：域名里的横线也认"－""—"（"test＠foo－bar.com"）；"@"
# 前后可以各隔一个空格（"abc @126.com"）。手机号当邮箱名的（"1381234…@163.com"）照旧先遮成手机号
# 标记，页面上、交给模型时再并成邮箱（显示层 _hide_emails）：同一行的姓名按电话行遮，入库就并成
# 邮箱的话这一行的姓名会露出来（11/18 独立 Claude 确认轮）
_EMAIL = re.compile(
    r"(?P<user>[A-Za-z0-9._%+-]{1,64})[ \u3000]?[@＠][ \u3000]?"
    r"[A-Za-z0-9.．—－-]{1,253}[.．][A-Za-z]{2,63}"
)
# 用户名是一串数字的邮箱（QQ 号、号码当邮箱名："王某、8765…＠qq.com"）遮姓名时当号码看，前面的姓名
# 照样遮；"简历：hr@abc.com"这类字母用户名的整个遮成邮箱，不当号码（11/18 独立 Claude 第六轮）。
# 按主干页面上看到的分两种（标记对页面规则透明，11/19 隐私合规那位第八轮）：
# - 主干入库就遮的写法（_MAIN_EMAIL：半角"@"、前后不带空格）：页面上本来看不到号码，同一遍里
#   先换成临时标记 _NUMBER_EMAIL_MARK，遮完姓名再换回"[邮箱已脱敏]"；
# - 这次新认的写法（全角"＠"、"@"旁边带空格、域名里有全角的点或横线）：主干入库不遮，页面上的规则
#   看到的是原始数字。只把用户名遮成号码的标记，"＠域名"留着（同手机号当邮箱名的
#   "[手机号已脱敏]@…"），页面上"整行有号码""标签下一行"这类规则照样把它当号码认，号码后面、
#   隔着标签的姓名也遮（"电话咨询：8765…＠qq.com（张某）"），最后再并成邮箱（显示层
#   _hide_emails）。用 QQ 号的标记：页面上它算号码，但光有它不算电话行（v1._PHONE_LINE），和主干
#   看到原始数字时一样
_NUMBER_EMAIL_MARK = "[号码邮箱已脱敏]"
_MAIN_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")


def _email_mark(match: re.Match[str]) -> str:
    user = match.group("user")
    if not user.isdigit():
        return "[邮箱已脱敏]"
    if _MAIN_EMAIL.fullmatch(match.group()):
        return _NUMBER_EMAIL_MARK
    return _NEW_QQ_MARK + match.group()[len(user) :]


# 座机：区号加号码，写法和显示层（api/v1.py 的 _AREA_PHONE、_INTL_PHONE）对齐，整个号码一起遮（只遮
# 号码、把区号和分机留在标记旁边的话，页面上反倒比以前多露出区号、分机）：
# - 区号：0 开头，后面接各种横线、破折号（"012—1234…"，可以带空格），或者写在括号里
#   （"（0123）8765…"）。主干原有的"0xxx-""0xxx－"照旧，新加的这几种写法区号限 0[1-9]（同
#   _LOCAL_NUMBER；"第0001—1234…号""批次 0001—1234…"是编号，11/18 独立 Claude 第五轮）。
#   区号和号码只隔空格的（"0123 8765…"）不一定是电话，按下面不带区号的号码那样要有线索才认
# - 国家码写法（"86123-2345…""+86 123 2345…"）照显示层的 _INTL_PHONE、intl()：本地号码 2–9 开头，
#   后面紧接着金额单位、"号"这类字的不算（"合计 86 120 2345678元""第86 123 8765…号"，11/18 独立
#   Claude 确认轮）
# - 号码：7–8 位，可以带分机（"-801""转801"），可以写成"1234 5678"；原网页写错、多了几位或两个号码
#   连写的，区号后面最多 16 位也算（long）。这种长的也可能是编号（"受理号：0123-1234…""申报批次
#   0001-1234…"，独立 Claude 第二轮），要紧跟在电话类标签后面（"电话：0123-6123…"）、前面不是
#   "受理号""批次"这类标签才认（_mask_landlines）
# 11/18 以前入库只认"-""－"连着区号的写法
# 横线：半角、全角横线，连字符（U+2010、U+2011），数字横线（U+2012），短破折号"–"，长破折号"—"，
# 横杠"―"，减号"−"，逐个写出来
_DASHES = "-－\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
# 分机（"-801""转801"）。后面紧跟着"-"和 7–8 位的不是分机，是下一个号码的区号
# （"0123-8765…-0123-8765…""0123-8765…转0123-8765…"，11/18 独立 Claude 确认轮）
_EXT = r"(?:[-－]|转)\d{1,6}(?!\d)(?![-－]\d{7,8}(?!\d))"
_LANDLINE = (
    rf"(?:(?:[（(](?P<paren>0\d{{2,3}})[）)][^\S\n]*+"
    rf"|(?P<area>0\d{{2,3}})(?P<sep>[^\S\n]*+[{_DASHES}]{{1,2}}[^\S\n]*+))"
    r"(?:\d{7,8}(?:" + _EXT + r")?|[2-9]\d{2,3}[^\S\n]\d{4}|(?P<long>\d{9,16}))"
    rf"|(?<![A-Za-z+＋])[+＋]?86[^\S\n]*+(?:[{_DASHES}][^\S\n]*+)?0?(?![48]00)[1-9]\d{{1,2}}"
    rf"(?:[^\S\n]*+[{_DASHES}][^\S\n]*+|[^\S\n]++)[2-9]\d{{6,7}}(?:" + _EXT + r")?"
    r"(?![元万亿千百%％‰年月日号个人次家项份字条批期届级名位件页])(?![^\S\n]+[元万亿%％‰]))"
)
_PHONE = re.compile(rf"(?<!\d){_LANDLINE}(?!\d)")
# "编号""会议号"这类标签（"会议编号：012—1234…""腾讯会议号：2345…""会议ID：…"）后面的号码不是电话。
# 单独的"会议"不算："会议…"后面也可能是电话，放过去就漏遮（协调会话 11/18 定）
# （11/18 Codex 第三轮）：座机（_mask_landlines）、不带区号的号码（_NOT_PHONE_LABEL）都不认。
# 页面那一层（api/v1.py 的 _CODE_BEFORE）这次不动：页面上只加不减（协调会话 11/18 定），会议号
# 在页面上照旧遮，合并后另外设计
_CODE_LABELS = (
    r"编号|代码|文号|序号|邮编|批号|编码|账号|卡号|会议(?:号码?|ID|码)|(?<![A-Za-z])(?:ID|Id|id)"
    r"|密码"
)
_CODE_BEFORE = re.compile(rf"(?:{_CODE_LABELS})[^\S\n]*+[:：]?[^\S\n]*+\Z")
# 联系电话那一类的行里不带区号（或者区号只隔空格）的 7–8 位号码（"电话：[电话已脱敏]、6543…"
# "电话：6543…""政策咨询：某某，6543…""请致电6543…咨询"，11/18 漏遮）。写法和显示层对齐（api/v1.py
# 的 _PHONE_CONTEXT、_PHONE_TAIL、_NUMBER_BEFORE_MARK、_is_phone_number），要有线索才认：
# - 标签（_FAR_CUE）：同一行里、号码前面有"电话：""咨询：""联系方式：""联系人："这类带冒号的标签，
#   或者"致电"，中间没隔句末标点，也没隔"编号：""会议号："这类别的标签（_LABEL_STOP）；
# - 紧挨着的（_NEAR_CUE、_MARK_NAME_BEFORE）：号码前面只隔分隔符、空白就是遮过的号码、姓名，或者
#   "某老师"，或者这一次认出来的上一个号码（"[电话已脱敏]、6543…、6543…"）；中间还可以夹一个姓名
#   （"某市：张某 [电话已脱敏] 李某 6543…"）；
# - 后面的：号码后面只隔一个分隔符（和一个姓名）就是遮过的电话，或者是这一次认出来的号码
#   （"谢某，6543…；杨某，[手机号已脱敏]"，_LIST_GAP）。
# 不认的：后面紧接着金额、年月日、"台、家、件"这类单位的（隔着空白的只认钱和百分比）；8 位像日期的；
# 前面紧挨着"编号""受理号""会议号""邮编""预算""人民币"这类标签的（_NOT_PHONE_LABEL）；前后紧挨着
# 英文字母的（代码）；1 开头的（国内座机不以 1 开头）；直接贴着遮过的标记的；前面是"/"、"/"前面
# 又不是遮过的号码、姓名或别的号码的（日期、比例）。号码可以带分机（"-801""转801"），和显示层一样
# 整个遮；"台、张"这类单位只看号码本身后面（"8765…转801张某"的"张"是姓）。
# 标签按行、按句找，不按字数：号码换成标记以后再过一遍，看到的线索一样。
_LOCAL_NUMBER = re.compile(
    r"(?:(?<![0-9A-Za-z])(?P<area>0[1-9]\d{1,2}[^\S\n]+)|(?<![\d.．\-－A-Za-z]))(?P<num>[2-9]\d{6,7})"
    r"(?![\dA-Za-z.．年月日元万亿千百%％‰人个件户家项名位次条份号吨米台部辆架艘套张本册篇场座栋间只"
    r"头株棵亩倍岁批期届级页字])"
    r"(?![^\S\n]+[元万亿%％‰])"
    r"(?:" + _EXT + r"(?![A-Za-z.．]))?"
)
_FAR_CUE = re.compile(
    r"(?:电[^\S\n]*话|手[^\S\n]*机|传[^\S\n]*真|座[^\S\n]*机|固[^\S\n]*话|热[^\S\n]*线|值[^\S\n]*班"
    r"|技术支持|咨[^\S\n]*询|联[^\S\n]*系(?:[^\S\n]*方[^\S\n]*式|[^\S\n]*人)?|联[^\S\n]*络[^\S\n]*人"
    r"|经[^\S\n]*办[^\S\n]*人)(?:号码?)?[^\S\n]*[:：]"
    r"|致[^\S\n]*电"
)
# 可以隔一个换行（表格里一格一行："[姓名已脱敏]⏎6543…"，同显示层的 _PHONE_TAIL）
_NEAR_CUE = re.compile(
    r"(?:\[(?:新?电话|手机号|姓名)已脱敏\]|[\u4e00-\u9fff](?:老师|先生|女士|同志))"
    r"[^\S\n]*+(?:[、，,；;/／：:或和及][^\S\n]*+)?(?:\n[^\S\n]*+)?\Z"
)
# 遮过的电话后面隔一个姓名再写号码（"[电话已脱敏] 李某 6543…"）
_MARK_NAME_BEFORE = re.compile(
    r"\[(?:新?电话|手机号)已脱敏\](?:[^\S\n]*+[，,、；;/][^\S\n]*+|[^\S\n]++)"
    r"([\u4e00-\u9fff·]{2,6})[^\S\n]*+[，,：:]?[^\S\n]*+\Z"
)
# 上一个号码和这一个中间只隔这些（"6543…、6543…"，可以隔一个换行）
_NUMBER_GAP = re.compile(r"[^\S\n]*+(?:[、，,；;/／或和及][^\S\n]*+)?(?:\n[^\S\n]*+)?")
_PHONE_MARK = re.compile(r"\[(?:新?电话|手机号)已脱敏\]")
# 号码和后面那个遮过的电话（或这一次认出来的号码）中间只隔这些：分隔符，加上可能有的一个姓名
_LIST_GAP = re.compile(
    r"(?:[^\S\n]*+[，,、；;/][^\S\n]*+|[^\S\n]++)"
    r"(?:([\u4e00-\u9fff·]{2,6}|\[姓名已脱敏\])[^\S\n]*+[，,：:]?[^\S\n]*+)?"
)
# 号码前面紧挨着这类标签的不是电话：编号、会议号、代码、"某某号"（"电话号""手机号""传真号"，
# 还有 QQ 规则管的"QQ号""群号"除外）
# 一类，金额、数量、币种一类（"项目编号为2345…""受理号2345…""腾讯会议号：2345…""预算2345…"
# "人民币2345…"）；标签和号码中间可以隔着"为、是、约、达、共、计"、冒号、空白
_NOT_PHONE_WORDS = (
    rf"{_CODE_LABELS}|邮政编码|序列号|批次|(?<![话机线真群QqＱ])号"
    r"|金额|资金|预算|投资|总额|合计|共计|总计|小计|费用|经费|补助|补贴|奖励|资助|规模|产值|营收|收入"
    r"|销售额|利润|税收|纳税|数量|总数|总量|数额|人数|面积|容量|产能|产量|价格|单价|总价|余额|资本"
    r"|股本|额度|限额|人民币|美元|欧元|港元|港币|￥|¥"
)
_NOT_PHONE_LABEL = re.compile(
    rf"(?:{_NOT_PHONE_WORDS})[^\S\n]*+(?:[为是约达共计][^\S\n]*+)?[:：]?[^\S\n]*+\Z"
)
# 电话类标签管到哪：同一行、同一句里，到下一个上面这类标签（"编号：""会议号：""金额："）为止
# （"电话：1234，腾讯会议号：2345…"的会议号不算，11/18 Codex 第三轮）。单位、姓名写成的标签不算：
# "联系电话：业务部门：某某科，6543…""致电某某服务处：6543…""张老师：6543…"这样写的是电话
# （全部正文里有 6 处）。页面那一层（api/v1.py 的 _mask_context_numbers）这次不动，同上
_LABEL_STOP = re.compile(rf"(?:{_NOT_PHONE_WORDS})[^\S\n]*+[:：]")
_CUE_REACH = 30
# 电话类标签紧挨在号码前面，中间只隔空白（"电话：0123-6123…""致电0123-…"）
_CUE_RIGHT_BEFORE = re.compile(rf"(?:{_FAR_CUE.pattern})[^\S\n]*+\Z")
# 400/800 热线（"400—2345…""800 2345…"）：单位的服务热线，后半截不当本地号码（同显示层的
# _HOTLINE_HEAD；独立 Claude 第二轮）
_HOTLINE_HEAD = re.compile(rf"(?<!\d)[48]00[^\S\n]*[{_DASHES}]?[^\S\n]*\Z")
# 被空格拆开的统一社会信用代码（"91220101 MA0Y5F26" / "91220101 MA0Y5F261X"：
# 前段是没有区号和分机的 8 位信用代码前缀，后段隔空白跟着字母开头且含数字的 8–10 位后缀），
# 不是电话（"致电87654321 ABCD"、"致电91220101 1A234567" 和紧跟电话标签的写法照常遮蔽）
_CREDIT_HEAD = re.compile(r"(?:2[19]|3[1-59]|4[19]|5[1-39]|6[19]|7[129]|8[19]|9[1-39])\d{6}")
_CODE_AFTER = re.compile(r"[^\S\n]+[A-Z](?=[A-Z0-9]*\d)[A-Z0-9]{7,9}(?![A-Za-z0-9])")
# "/"前面是这些标记的，"/"后面的号码才认（"[电话已脱敏]/6543…""联系人：[姓名已脱敏]/6543…"）
_SLASH_MARKS = ("[电话已脱敏]", "[新电话已脱敏]", "[手机号已脱敏]", "[姓名已脱敏]")
_SENTENCE_END = re.compile(r"[。！？!?]")
# QQ 号、QQ 群号（"技术咨询QQ群：1234…""QQ：…"，11/18 漏遮）：标签后面的号码，一串里接着写的几个
# （"QQ群：1234…、5678…"）一起遮。"QQ"前面、号码后面紧挨着英文字母或数字的不认：统一社会信用代码
# 里也有字母 Q（"…0000QQ1234567X"），名单解析拿 redact_sensitive 有没有改动判断代码和机构名
_QQ = re.compile(
    r"((?<![A-Za-z0-9])(?:QQ|qq|Qq|ＱＱ)[^\S\n]*+(?:[\u4e00-\u9fff]{0,4}?群)?[^\S\n]*+(?:号码|号)?"
    r"[^\S\n]*+[:：]?[^\S\n]*+"
    # 冒号、逗号后面再写一遍"群号（为）"的（"QQ群：群号1234…""QQ群，群号为：1234…"）
    r"(?:[，,]?[^\S\n]*+群号码?[^\S\n]*+[为是]?[^\S\n]*+[:：]?[^\S\n]*+)?)"
    r"(\d{5,11}(?:[^\S\n]*+[、，,；;/或和及][^\S\n]*+\d{5,11})*)(?![\dA-Za-z])"
)
_QQ_NUMBER = re.compile(r"\d{5,11}")
_AGENCY = re.compile(
    r"^[\u4e00-\u9fff（）()、\s]{3,100}(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)(?:（[^）]{1,20}）)?$"
)
_AGENCY_END = re.compile(
    r"(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)(?:（[^）]+）)?$"
)
# 发文机关的几处来源（_issuer 按这个顺序找）：
# - 页面上明写的"发文机关、发文单位、制发机关"；
# - 文件标题开头的机关："某某市人民政府关于……""某某厅等 7 部门关于印发……"。标题比"发布机构"一栏准：
#   市政府发文挂在办公厅网站上、转载上级文件时，"发布机构"写的是挂文件的单位；
# - "发布机构"一栏：挂文件的单位，自己发的文件就是发文机关。
_ISSUER_LABEL = re.compile(r"(?:发文机关|发文单位|制发机关)\s*[:：]\s*([^\n]{2,80})")
_PUBLISHER_LABEL = re.compile(r"发布机构\s*[:：]\s*([^\n]{2,80})")
_TITLE_ISSUER = re.compile(
    r"^\s*(?P<org>[\u4e00-\u9fff（）()、\s]{2,90}?"
    r"(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行|小组)(?:（[^）]{1,20}）)?)"
    r"(?P<etc>\s*等\s*\d{1,2}\s*个?部门)?\s*(?:关于|印发|转发)"
)
# 标题里的机关要是完整的机关名：去掉开头的地名（省级区划名、data/place_names.txt 里的地级县级
# 区划名）以后，剩下的要在机关名称词表（data/agency_bodies.txt）里；"中共"开头的剩下要是党委机构；
# 中央机关看全称名单。"张三代表市政府""北京市财政局张三建议市政府""住房和城乡建设局"都不算
_PROVINCE = (
    r"(?:北京市|天津市|上海市|重庆市|内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区"
    r"|新疆维吾尔自治区|(?:河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南"
    r"|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾)省)"
)
_PROVINCE_HEAD = re.compile(rf"^{_PROVINCE}")
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
# 地级市简称后面跟这些的也算地名开头（"雅安经济技术开发区管理委员会""南京海关"）
_ZONE_WORDS = ("经济技术开发区", "高新技术产业开发区", "高新区", "经开区", "开发区", "新区", "海关")
# "中共某某市"后面的党委机构
_PARTY_BODIES = frozenset(
    (
        "委", "委办公室", "委办公厅", "委组织部", "委宣传部", "委统战部", "委统一战线工作部",
        "委政法委员会", "委机构编制委员会办公室", "委全面深化改革委员会办公室",
        "委财经委员会办公室", "委人才工作领导小组办公室", "委军民融合发展委员会办公室",
        "委网络安全和信息化委员会办公室", "委外事工作委员会办公室", "委农村工作领导小组办公室",
    )
)  # fmt: skip

# 中央机关的全称（"中共"去掉以后比；国务院各部见 _MINISTRY）
_CENTRAL_AGENCIES = frozenset(
    (
        "中央", "中央办公厅", "中央组织部", "中央宣传部", "中央统战部",
        "中央网络安全和信息化委员会办公室", "中央全面深化改革委员会办公室",
        "中央财经委员会办公室", "国务院", "国务院办公厅", "国务院国有资产监督管理委员会",
        "国务院国资委", "国家发展和改革委员会", "国家发展改革委", "国家民族事务委员会",
        "国家民委", "国家林业和草原局", "国家林草局", "国家税务总局", "国家市场监督管理总局",
        "市场监管总局", "国家知识产权局", "国家统计局", "国家能源局", "国家数据局",
        "国家医疗保障局", "国家卫生健康委员会", "国家卫生健康委", "国家金融监督管理总局",
        "国家外汇管理局", "国家文物局", "国家粮食和物资储备局", "国家中医药管理局",
        "国家药品监督管理局", "国家乡村振兴局", "国家体育总局", "国家广播电视总局",
        "国家新闻出版署", "国家国防科技工业局", "国家邮政局", "中国人民银行",
    )
)  # fmt: skip

# 省级区划的简称：中央机关在地方的分支常这样写（"国家金融监督管理总局四川监管局""中国人民银行上海
# 分行"）；直辖市简称后面跟开发区、海关的也算地名开头
_MUNICIPALITIES = ("北京", "天津", "上海", "重庆")
_PROVINCE_SHORT = frozenset(
    (
        *_MUNICIPALITIES, "内蒙古", "广西", "西藏", "宁夏", "新疆", "河北", "山西",
        "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
        "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    )
)  # fmt: skip
# 中央机关在地方的分支：父机关 + 地名 + 这几种名称（"国家税务总局龙岩市税务局"）
_BRANCH_BODIES = {
    "国家税务总局": ("税务局",),
    "国家金融监督管理总局": ("监管局", "监管分局"),
    "中国人民银行": ("分行", "中心支行"),
    "国家外汇管理局": ("分局",),
}
_MINISTRY = re.compile(
    r"^(?:(?:外交|国防|教育|科学技术|工业和信息化|公安|国家安全|民政|司法|财政|人力资源和社会保障"
    r"|人力资源社会保障|自然资源|生态环境|住房和城乡建设|住房城乡建设|交通运输|水利|农业农村|商务"
    r"|文化和旅游|退役军人事务|应急管理)部|审计署|海关总署)$"
)
# 机关名里不会有的字：会议、讲话这类新闻标题的字（"某某主持召开市政府……会议"），职务（"某某市
# 市长某某在……"），"关于""转发"（"转发省商务厅关于……""关于转发财政部关于……"）
_NOT_ORG = re.compile(
    r"主持|召开|出席|会见|会谈|调研|考察|视察|看望|慰问|会议|座谈|讲话|指出|强调|听取|部署"
    r"|带队|走访|批示|签署|致信|致电|发表|同志|关于|转发|印发|贯彻|落实|在|代表"
    r"|市长|县长|区长|省长|州长|部长|厅长|局长|书记|主任|主席|总理|委员长"
)
# 只写了部门内设机构的名字，不是发文机关（"发布机构：信息中心"、来源写"办公室"）
_GENERIC_ISSUERS = frozenset(
    {"信息中心", "办公室", "网站管理中心", "政务公开办公室", "政务服务中心", "大数据中心"}
)
_DATE = re.compile(r"20\d{2}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}\s*日?")
# qr_container：常见的"扫一扫在手机打开当前页"二维码块（四川科技厅、江苏工信厅放在正文区里）
_JUNK_CLASS = re.compile(
    r"(?:share|related|footer|breadcrumb|toolbar|copyright|qrcode|qr-code|qr_container|print"
    r"|pagination)",
    re.I,
)
# TRS 编辑器写出的正文区，比下面的通用规则准：#zoom、.content 等常把文章头的"当前位置"、发布信息、
# 工具栏和页尾的二维码、"相关文档"一起框进来（10/22 湖北科技厅等，见测试样例）。只在没有歧义时用
# （_editor_node）
_EDITOR = '//*[contains(concat(" ", normalize-space(@class), " "), " TRS_UEDITOR ")]'
# 页面自己标的正文区：有它的页面，编辑器块要在它里面才用
_MARKED_BODY = '//*[@id="zoom" or @id="UCAP-CONTENT" or @id="contentText"]'
# 正文标记里、编辑器块外面，整行只有这些字的是网页上的栏目标题（10/23 抽样：海南科技厅 #zoom 里
# 编辑器块后面的"相关文档"），不妨碍用编辑器块
_EDITOR_SIDE_LABELS = frozenset({"相关文档", "相关链接", "关联链接"})
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
    # 几个站群的正文标记（上海 ivs_content，广州、苏州、黑龙江等 zoomcon，安徽 j-fontContent，
    # 四川雅安、遂宁、达州等 xqing-web-box）：排在最后的 .content 前面，只在前面的规则都没匹配上
    # 时才用（.content 常是整页外框；11/12 雅安一批文件页的 .content 是页尾的"主办单位、地址"）
    '//*[@id="ivs_content"]',
    '//*[@id="zoomcon"]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " j-fontContent ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " xqing-web-box ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " content ")]',
)
# 个别网站的正文区用的是自家类名（别的网站同名元素未必是正文），只对这个网站用（主机名精确匹配），
# 排在通用规则前面。通用规则在这些网站上会把整页外框、文章头的发布信息和工具栏、页尾的按钮当成正文
# （10/22 抽样，见测试样例）。网站改版、页面上找不到这里写的元素时，照旧走通用规则。
_SITE_BODY_SELECTORS = {
    # 四川科技厅：通用规则选到整页外框（"当前位置"、发布机构、分享）
    "kjt.sc.gov.cn": '//*[contains(concat(" ", normalize-space(@class), " "), " newsCon ")]',
    # 江苏工信厅：文章头的"【字体：大 中 小】""打印"
    "gxt.jiangsu.gov.cn": '//*[contains(concat(" ", normalize-space(@class), " "), " nscont ")]',
    # 山东工信厅：文章头的浏览次数，页尾的"字体""分享""打印"
    "gxt.shandong.gov.cn": (
        '//*[contains(concat(" ", normalize-space(@class), " "), " news-body ")]'
    ),
    # 河南科技厅：文章头的阅读次数、"分享："
    "kjt.henan.gov.cn": '//*[contains(concat(" ", normalize-space(@class), " "), " articl_u2 ")]',
    # 上海经信委：文章头的"字号：小 中 大"
    "sheitc.sh.gov.cn": '//*[contains(concat(" ", normalize-space(@class), " "), " text-main ")]',
    # 广东科技厅：通用规则取到网页导航（"首页 政务公开 科技资讯……"），正文在 div.zw（10/25 抽样）。
    # 按完整类名匹配：标题 zw-title、发布信息和"【打印】""分享到"所在的 zw-info 是别的元素
    "gdstc.gd.gov.cn": '//*[contains(concat(" ", normalize-space(@class), " "), " zw ")]',
}
# 防护页：标题和正文前 500 字里有这些字的，整页当成验证码、限流或要开脚本的挡页，不取（D-013）
_PROTECTION = re.compile(
    r"验证码|人机验证|安全验证|访问过于频繁|异常访问|请开启.{0,12}JavaScript", re.I
)
# 站内搜索的限流弹窗（D-013：认得出才放）。11/12 地市正文门：泸州、漯河、商丘、常德等站群的每一页都
# 带一个平时隐藏的弹窗——div#VolidateCodeDiv，style 写着 display:none，标题（.modal-title）只有
# "搜索过于频繁，请输入验证码"，里面是搜索用的验证票据输入框 input#js-ticket-search。它是站内搜索
# 被限流时才弹出的，不是挡住这一页的验证，这些站的文件页因此都被当成了防护页。判断防护页时只把完全
# 是这个样子的弹窗拿掉；拿掉以后这一页还要取到完整的正文（有句子），不然照旧当防护页。别的写法一律
# 照旧当防护页、不取
_SEARCH_THROTTLE_TEXT = ("×", "搜索过于频繁，请输入验证码", "确定")
# 弹窗的整个结构（每个元素的标签、id、class、type、value，按出现顺序）：11/12 在 4 个站群的 219 处
# 看到的都是这一个样子，多一个元素、少一个、属性上的字（按钮的"点击验证"）不一样，都不认
_SEARCH_THROTTLE_SHAPE = (
    ("div", "VolidateCodeDiv", "modal in", "", ""),
    ("div", "", "modal-dialog", "", ""),
    ("div", "", "modal-content", "", ""),
    ("div", "", "modal-header", "", ""),
    ("button", "", "close", "button", ""),
    ("span", "", "", "", ""),
    ("h4", "", "modal-title", "", ""),
    ("div", "", "modal-body", "", ""),
    ("div", "", "container-fluid", "", ""),
    ("div", "", "", "", ""),
    ("input", "js-ticket-search", "", "hidden", ""),
    ("input", "js-ticketId-search", "", "hidden", ""),
    ("input", "js-btn-verify-search", "form-control btn btn-gray btn-block", "button", "点击验证"),
    ("span", "captchaticketvalid-search", "field-validation-valid", "", ""),
    ("input", "SearchValidateCode", "", "hidden", ""),
    ("div", "", "modal-footer", "", ""),
    ("a", "confirmNotApprove", "btn btn-sm btn-primary", "", ""),
)
# 放过弹窗的页面，正文要出自这类站核实过的正文区（或在它里面）：11/12 翻过来的 118 份都是
_VERIFIED_BODY = (
    '//div[contains(concat(" ", normalize-space(@class), " "), " conTxt ")]'
    ' | //div[@id="content"][contains(concat(" ", normalize-space(@class), " "), " lh-wznr ")]'
)
_SEARCH_THROTTLE_DIALOG = '//div[@id="VolidateCodeDiv"][.//input[@id="js-ticket-search"]]'
# 浏览器里核实过的网站和页面样子：网站 → 页面代码的指纹（_page_code_fingerprint）。11/12 泸州、
# 漯河、常德 22 个网站的 36 个样子，每个都在浏览器里打开过一页：弹窗没有显示出来、页面上看得到的
# 字里没有验证码一类的字、正文区正常显示（核对表见 PR #485）。别的网站、别的样子（外部脚本、样式
# 多一个少一个，页面自带的脚本、样式、事件属性有一处不一样）都不认这个弹窗，照旧当防护页。
# 兜底：每月在浏览器里按 PR #485 的做法复核一次；取详情时这些网站一遇到防护页，这一轮就停取这个
# 网站（detail_service），要复核
_SEARCH_THROTTLE_VERIFIED: dict[str, frozenset[str]] = {
    "czj.luohe.gov.cn": frozenset({"26b3dbf05a6943a8"}),
    "czj.luzhou.gov.cn": frozenset({"3e0d86007ee3353b", "da9f063cb7dd4f2f"}),
    "drc.luohe.gov.cn": frozenset({"c68b23a5d3bdb294"}),
    "fgw.luzhou.gov.cn": frozenset({"a14d9bdbf340e4d2", "d06406ee756a0ced"}),
    "gxj.luohe.gov.cn": frozenset({"db6f75d8850c5814"}),
    "hrss.luohe.gov.cn": frozenset({"13ca381f2244e471"}),
    "jxj.luzhou.gov.cn": frozenset({"34993d8a3389c627", "53d1ad1cd685d078", "6ac8a40fcc661a62"}),
    "kawlb.luzhou.gov.cn": frozenset({"9aeb5830f187ad15"}),
    "kjj.luohe.gov.cn": frozenset({"a341e43255813b31"}),
    "kjj.luzhou.gov.cn": frozenset({"bf8587b7edb7391f", "db21170b400079d9"}),
    "luohelvyou.luohe.gov.cn": frozenset({"82abca0dffabc7ae"}),
    "nyncj.luzhou.gov.cn": frozenset({"973f428253becf73", "ac1100e3e7c31c78", "e24dd3c4dd7b0432"}),
    "rsj.luzhou.gov.cn": frozenset({"a71031f2537a6123"}),
    "scjg.luohe.gov.cn": frozenset({"1645d3da962f3eb0"}),
    "scjgj.luzhou.gov.cn": frozenset({"c88e6061371a23b9", "e754315787e29882"}),
    "sthjj.luohe.gov.cn": frozenset({"416b74733e1401a8"}),
    "sthjj.luzhou.gov.cn": frozenset({"7dbf126d3b49e3fd", "8797b47f44dc5af2", "c40f34ee5e2f4b4a"}),
    "swj.luohe.gov.cn": frozenset(
        {"7e93775eab9fe810", "89e475064876dda2", "919cf8f5e8a30e04", "e3e1a3a561ca5fe9"}
    ),
    "wtgj.luzhou.gov.cn": frozenset({"0fa96aaa738f41c8"}),
    "www.changde.gov.cn": frozenset({"df4caf7c4c084e78"}),
    "www.luohe.gov.cn": frozenset({"b7112ed612b2044f", "f9870def54889ce0"}),
    "www.luzhou.gov.cn": frozenset({"6366cf401679cae4"}),
}
# 算页面代码指纹时归一的每页编号，只在页面自带脚本里这几句核实过的写法中、只换编号：上一篇/下一篇
# 两种接口、相关文章接口里的栏目编号和文章编号，相关文章排除的本篇编号，评论区块的文章编号。11/12
# 核实过的 36 个样子、124 份页面上，同一样子的页面代码只在这几处不同
_PAGE_ID_CONTEXTS = (
    re.compile(
        r"(/contentmanage/ajax/(?:getprevandnextcontent|getpreandnextpublicinformation)\?nodeId=)"
        r"\d+(&contentId=)\d+(&)"
    ),
    re.compile(r"(/ContentManage/Ajax/GetRelatedContent\?contentId=)\d+(&nodeId=)\d+(&moldType=)"),
    re.compile(r'(data:\s*\{\s*excludeIds:\s*")\d+(",\s*size:)'),
    re.compile(r'(parameters:\s*"\{\s*contentId:)\d+(,\s*moldType:)'),
)
# 算指纹时记下地址的 <link>：样式表和预加载的脚本、样式
_CODE_LINKS = {"stylesheet", "import", "preload", "modulepreload"}
# 放过弹窗的页面整页查挡页字样时也看的属性：按钮上的字、图片说明、输入框提示等
_WORD_ATTRS = ("value", "alt", "title", "placeholder", "aria-label")
# 放过弹窗以后要求的"完整正文"：100 字以上、有句号、正文里没有验证码一类的字（11/12 翻过来的
# 120 份文件页，正文都在 133 字以上，119 份有句号）
_COMPLETE_MIN = 100
# 个别网站放在正文区里的按钮（XPath 相对正文区），取正文时跳过
_SITE_JUNK = {
    # 上海经信委：正文区末尾"[返回]"（history.back）和"【市民留言】"两个链接所在的块
    "sheitc.sh.gov.cn": './/*[a[contains(@href, "history.back")]]',
}
# 正文不在页面结构里、写在页面自带脚本的 Vue 数据里（content: `<p>…</p>`），浏览器运行脚本后才显示到
# <div v-html="content"> 的网站。10/22 青海工信厅（铭飞 MCMS 模板）收录的"正文"只有导航、标题和
# "上一篇""下一篇"。只对列出的网站从脚本里取这段 HTML，不执行脚本。
_SCRIPT_BODY_HOSTS = {"gxt.qinghai.gov.cn"}
_VUE_NAME = re.compile(r"[A-Za-z_$][\w$]*")
# 没转义的 ${（前面没有反斜杠，或是成对的反斜杠）：要运行脚本才知道的内容
_TEMPLATE_INTERPOLATION = re.compile(r"(?<!\\)(?:\\\\)*\$\{")
# 模板字符串里的转义（按 JS 的规则还原）：\n、\t 等，\uXXXX、\u{…}、\xHH，行尾的反斜杠接下一行；
# 别的"\字符"就是这个字符（\`、\$、\\、\/ 等）
_TEMPLATE_ESCAPE = re.compile(r"\\(u\{[0-9A-Fa-f]+\}|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|\r\n|[\s\S])")
_TEMPLATE_SIMPLE = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "\n": "",
    "\r": "",
    "\r\n": "",
    "\u2028": "",
    "\u2029": "",
}
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
    单位名）会被一起遮掉。

    一遍遮完再过一遍，直到不再变：遮了姓名，同一行后面的号码才认得出是电话（"联系人：\n张三 6543…"
    的姓名第一遍才遮），一遍遮不完的在这里遮完，所以再过一遍一定不变。每一遍只加标记、不去掉标记，
    一定会停；实际最多三遍，没有要遮的文字只走一遍。"""
    while True:
        once = _redact_once(text)
        if once == text or once.count("已脱敏]") <= text.count("已脱敏]"):
            return once
        text = once


def _redact_once(text: str) -> str:
    text = _ID_CARD.sub("[身份证号已脱敏]", text)
    text = _MOBILE.sub("[手机号已脱敏]", text)
    text = _ACCOUNT.sub(_account, text)
    text = _EMAIL.sub(_email_mark, text)
    text = _mask_landlines(text)
    text = _mask_qq(text)
    text = _mask_local_numbers(text)
    # 姓名最后认：邮箱、座机先换成"[…已脱敏]"，姓名前后挨着的是"]"，第一遍和再过一遍看到的一样
    text = _mask_names(text)
    # 这一遍新遮的号码（临时标记），前面还没遮的人名照主干页面规则 ③ 遮：入库规则先认（几个连写的
    # 姓名合成一个标记），这条补它没认出来的。最后换回正常的标记
    news = [m.start() for m in _NEW_MARKS.finditer(text)]
    text = _apply_spans(text, [(*span, _NAME_MARK) for span in _page_names_before(text, news)])
    for temp, mark in _TEMP_MARKS:
        text = text.replace(temp, mark)
    return text


_NAME_MARK = "[姓名已脱敏]"
# 这一遍新遮、主干入库不遮的号码先换成临时标记（主干不认的座机写法、不带区号的本地号码、QQ 号，
# 数字用户名的邮箱见 _email_mark），遮完姓名再换回来：认电话标记的几条规则（_NEAR_CUE、
# _PHONE_MARK、_BEFORE_PHONE 这些）也认它们
_NEW_PHONE_MARK = "[新电话已脱敏]"
_NEW_QQ_MARK = "[新QQ号已脱敏]"
_TEMP_MARKS = (
    (_NEW_PHONE_MARK, "[电话已脱敏]"),
    (_NEW_QQ_MARK, "[QQ号已脱敏]"),
    (_NUMBER_EMAIL_MARK, "[邮箱已脱敏]"),
)
# 主干入库不遮、主干页面上看到原始数字的号码（_page_names_before 补遮前面的人名）。主干写法的数字
# 邮箱（_NUMBER_EMAIL_MARK）主干入库就遮成了邮箱，页面上看不到号码，不算：前面的姓名只按入库规则
# 认（"请将简历、1234…@qq.com"的"将简历"不当姓名，11/19 正确性那位第八轮）
_NEW_MARKS = re.compile(f"{re.escape(_NEW_PHONE_MARK)}|{re.escape(_NEW_QQ_MARK)}")
# 主干入库就遮的座机写法（区号接"-""－"，后面 7–8 位）；_MAIN_PHONE 是主干找它的写法（前后不连着
# 数字）
_MAIN_LANDLINE = re.compile(r"0\d{2,3}[-－]\d{7,8}")
_MAIN_PHONE = re.compile(r"(?<!\d)0\d{2,3}[-－]\d{7,8}(?!\d)")


def _page_names_before(text: str, starts: list[int]) -> list[tuple[int, int]]:
    """这次入库新遮、主干入库不遮的号码（不带区号的本地号码、主干不认的座机写法、QQ 号、数字用户名的
    邮箱），号码前面的人名：主干页面上按原始数字认出来遮掉（v1._name_before，号码前 2–3 个字像人名
    就遮，规则 ③）。现在入库先换成了标记，页面上那条认不到，所以入库时照同一个函数、照样宽地把前面的
    人名遮掉（11/18 独立 Claude 第五到七轮）。"简历""党员"这类身份、用途词（_ROLE_WORDS）不当姓名
    （11/19 正确性那位第八轮）。starts：号码在 text 里的起点。延迟引入 v1。"""
    if not starts:
        return []
    from youju.api import v1

    spans = [span for start in starts if (span := v1._name_before(text, start)) is not None]
    return [(a, b) for a, b in spans if text[a:b] not in _ROLE_WORDS]


def _apply_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    """把 [(开始, 结束, 标记)] 换成标记；按位置排，和前一段重叠的不换。"""
    if not spans:
        return text
    out: list[str] = []
    at = 0
    for start, end, mark in sorted(spans):
        if start < at:
            continue
        out += [text[at:start], mark]
        at = end
    return "".join(out) + text[at:]


def _mask_landlines(text: str) -> str:
    """座机换成标记。照原样留着的：新加的写法（括号、破折号、横线两边带空格）区号是"00"开头的；
    前面紧挨着"编号""会议号"这类标签的新加写法（_CODE_BEFORE）；区号后面 9–16 位的（long）不紧跟在
    电话类标签后面（_CUE_RIGHT_BEFORE）或者前面是"受理号""批次"这类标签的。"""
    spans: list[tuple[int, int, str]] = []
    for match in _PHONE.finditer(text):
        start = match.start()
        window = max(0, start - _CUE_REACH)
        paren, code = match.group("paren"), match.group("paren") or match.group("area")
        if code and code[1] == "0" and (paren or match.group("sep") not in ("-", "－")):
            continue
        if _CODE_BEFORE.search(text, window, start):
            # 只放过这次新加的写法：主干入库就遮的"0xxx-xxxxxxx"（_MAIN_PHONE）照旧遮，放过的话旁边
            # 的姓名跟着露出来（"会议码：0123-8765…（张某）""张某 文号 0123-8765…"），比主干少遮
            # （11/19 隐私合规那位第八轮）
            main = _MAIN_PHONE.search(text, start)
            if main is None or main.start() >= match.end():
                continue
        if match.group("long") and (
            _NOT_PHONE_LABEL.search(text, window, start)
            or not _CUE_RIGHT_BEFORE.search(text, window, start)
        ):
            continue
        # 主干入库就遮的写法照旧，别的写法是这次新遮的
        new = not _MAIN_LANDLINE.fullmatch(match.group())
        spans.append((start, match.end(), _NEW_PHONE_MARK if new else "[电话已脱敏]"))
    return _apply_spans(text, spans)


def _label_finder(text: str) -> Callable[[int], bool]:
    """给一个函数：位置 start 前面同一行、同一句里有没有电话类标签（_FAR_CUE），中间没隔
    "编号：""会议号："这类别的标签（_LABEL_STOP）。行首、句末标点、标签的位置先记下来，
    长行里号码多也只看一遍；按行、按句找，不按字数，号码换成标记以后再过一遍看到的一样。"""
    lines = [m.start() for m in re.finditer("\n", text)]
    stops = [m.start() for m in _SENTENCE_END.finditer(text)]
    cues = [m.end() for m in _FAR_CUE.finditer(text)]
    others = [m.start() for m in _LABEL_STOP.finditer(text)]

    def labeled(start: int) -> bool:
        line_start = lines[i - 1] + 1 if (i := bisect.bisect_left(lines, start)) else 0
        k = bisect.bisect_right(cues, start) - 1
        if k < 0 or cues[k] < line_start:
            return False
        j = bisect.bisect_left(stops, cues[k])
        o = bisect.bisect_left(others, cues[k])
        return not (j < len(stops) and stops[j] < start) and not (
            o < len(others) and others[o] < start
        )

    return labeled


def _mask_qq(text: str) -> str:
    return _QQ.sub(lambda m: m.group(1) + _QQ_NUMBER.sub(_NEW_QQ_MARK, m.group(2)), text)


def _mask_local_numbers(text: str) -> str:
    """联系电话那一类的行里不带区号的 7–8 位号码（规则见 _LOCAL_NUMBER 上面）。

    一次定下来：先从左往右看标签和紧挨着的线索（上一个号码这一次要遮的，也算遮过的号码），再从右往左
    看后面的线索（后面那个号码这一次要遮的，也算遮过的电话），"6543…，6543…，[电话已脱敏]"这样的
    一串一次遮完，再过一遍不会变。行首、标签、句末标点的位置先记下来，长行里号码多也只看一遍。"""
    # 没当座机遮的（_mask_landlines：前面是"编号""会议号"这类标签的，区号后面太长、又不紧跟电话类
    # 标签的），里面的号码也不认（"会议编号：012—2345…"的"2345…"）。座机这时都已经遮了，还认得出的
    # 就是这种
    kept = [m.span() for m in _PHONE.finditer(text)]
    kept_starts = [a for a, _ in kept]
    found = []
    for match in _LOCAL_NUMBER.finditer(text):
        start = match.start()
        k = bisect.bisect_right(kept_starts, start) - 1
        if (
            # 直接贴着遮过的标记、没有分隔的不算
            text.endswith("]", 0, start)
            or _NOT_PHONE_LABEL.search(text, max(0, start - _CUE_REACH), start)
            # 8 位像日期的（只看分机前面那一段："受理时间：20231108-1"；带区号的是电话，"电话：
            # 0123 2023…"照样遮）
            or (not match.group("area") and _YMD.fullmatch(match.group("num")))
            or (k >= 0 and kept[k][1] > start)
            or _HOTLINE_HEAD.search(text, max(0, start - 8), start)
            or (
                not match.group("area")
                and match.end("num") == match.end()
                and _CREDIT_HEAD.fullmatch(match.group("num")) is not None
                and _CODE_AFTER.match(text, match.end()) is not None
                and not _CUE_RIGHT_BEFORE.search(text, max(0, start - 16), start)
            )
        ):
            continue
        found.append(match)
    if not found:
        return text
    ends = {match.end() for match in found}
    candidates = []
    for match in found:
        # "/"（两边可以有空格）后面的：斜杠前面是遮过的电话、姓名或者另一个号码才认
        # （"[电话已脱敏]/6543…""6543…/6543…"）；"比例1/ 2345…"不认
        lead = match.start()
        while lead > 0 and _inline_space(text[lead - 1]):
            lead -= 1
        if lead > 0 and text[lead - 1] in "/／":
            back = lead - 1
            while back > 0 and _inline_space(text[back - 1]):
                back -= 1
            if not (text.endswith(_SLASH_MARKS, 0, back) or back in ends):
                continue
        candidates.append(match)
    labeled = _label_finder(text)

    def listed_after(prev_end: int, start: int) -> bool:
        """上一个号码（这一次要遮的）和这一个中间只隔分隔符，或者分隔符加一个姓名。"""
        if _NUMBER_GAP.fullmatch(text, prev_end, start):
            return True
        gap = _LIST_GAP.fullmatch(text, prev_end, start)
        return gap is not None and _listed_name(gap.group(1))

    masked = [False] * len(candidates)
    for i, match in enumerate(candidates):
        start = match.start()
        window = max(0, start - _CUE_REACH)
        named = _MARK_NAME_BEFORE.search(text, window, start)
        masked[i] = (
            labeled(start)
            or _NEAR_CUE.search(text, window, start) is not None
            or (named is not None and _listed_name(named.group(1)))
            or (i > 0 and masked[i - 1] and listed_after(candidates[i - 1].end(), start))
        )
    for i in range(len(candidates) - 1, -1, -1):
        if masked[i]:
            continue
        gap = _LIST_GAP.match(text, candidates[i].end())
        if not gap or (gap.group(1) and not _listed_name(gap.group(1))):
            continue
        nxt = gap.end()
        masked[i] = _PHONE_MARK.match(text, nxt) is not None or (
            i + 1 < len(candidates) and candidates[i + 1].start() == nxt and masked[i + 1]
        )
    spans = [
        (match.start(), match.end(), _NEW_PHONE_MARK)
        for match, hide in zip(candidates, masked, strict=True)
        if hide
    ]
    return _apply_spans(text, spans)


def _listed_name(word: str | None) -> bool:
    """号码串里夹着的那个词是姓名（或遮过的姓名）。"""
    return word == "[姓名已脱敏]" or (word is not None and _name_like(word))


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
# 号码串里某一组后面跟着的字，从这一组的末尾往后匹配（空白用占有量词，一长串空格也不卡）
_SPACES = r"\s*+"  # 空格、制表符、全角空格、换行（PDF 里"2025"和"年"常被换行隔开）
# 四位数年份后面："年"（"2025 年11月""2025年专项资金""2026 年度"都是年份；只有"年费""年均""年检"
# "年报"可能是账号后面的词——"…2075 年检"——这几个不算，"年费用"算），或者点号斜杠写的日期
# （"2025.10.10"）
_YEAR_AFTER = re.compile(rf"{_SPACES}年(?!费(?!用)|均|检|报)|[./．／]\d|[上下]半年")
# 两位数的年份："年"后面要接着日期（"25年3月""25年底前"；"12 年费""09 年均余额"的一两位数是账号的）
_SHORT_YEAR_AFTER = re.compile(
    rf"{_SPACES}年(?:\d|[底初中末前后起止至内之度第]|[上下]半|(?![{_HAN}]))"
)
# 一两位数后面的"月""日"，后面接着的是日期才算（"10月20日""10月底前""1月起""1 月 申报""20日前"
# "20日之前""20日，"）："月""日"后面紧跟的不是汉字，或者是这几个字；"月均""月份""日期""日均"
# 是词，这一组照样并进账号
_MONTH_AFTER = re.compile(
    rf"{_SPACES}月(?:\d|[底初中末前后起止至内之以]|[上下][旬半]|(?![{_HAN}]))"
)
_DAY_AFTER = re.compile(rf"日(?:[前后起止至内之以]|(?![{_HAN}]))")


def _date_from(values: list[str], i: int) -> bool:
    """第 i 组起是一个日期：8 位连写（"20251010"），或年、月、日三组（"2025 10 10"）。"""
    if len(values[i]) == 8 and _YMD.fullmatch(values[i]):
        return True
    ymd = values[i : i + 3]
    if len(ymd) < 3 or len(ymd[0]) != 4 or not all(1 <= len(v) <= 2 for v in ymd[1:]):
        return False
    return bool(_YMD.fullmatch(f"{ymd[0]}{int(ymd[1]):02d}{int(ymd[2]):02d}"))


def _account(match: re.Match) -> str:
    """标签后面的一串数字（组和组之间隔一个空格或横线）：第一组整组算（连写的长账号，最多 30 位；
    和日期连写、中间没空格的分不开，整串算）；第二组起和旧规则一样最多接到 19 位为止（分组写的
    20 位以上账号和以前一样露出最后一组——再往后接，后面的日期、金额写法太多，认不全就会吞字），
    19 位以内碰到这几种也不接（"隔空白"指空格、制表符、全角空格、换行）：
    - 日期（"20251010""2025 10 10"；前面隔的是空格才算，整串用横线连写的"1202021309-2025-10-10"
      照样接）；
    - 年份（19xx、20xx）后面（可以隔空白）跟着"年"（"账号：1234567890 2025 年11月"
      "2025年专项资金"；"年费""年均""年检""年报"除外，"年费用"不除外），或跟着"."、"/"（全角
      也算）再跟数字（"2025.10.10"），或直接跟"上半年""下半年"，或下一组是跟着"月"的月份
      （"2025 10月10日"，"月"的判断同下），或前面隔空格、后面是横线和最后一组月份
      （"… 2025-10前"；整串都用横线分组的照样接）；
    - 一两位数后面（可以隔空白）跟"年"、"年"后面接着日期（数字、不是汉字，或"底初中末前后起止至
      内之度第"、"上半""下半"："25年3月""25年底前"；"12 年费"不算）；
    - 一两位数后面跟着日期：紧跟"日"（1–31，"日"后面是"前后起止至内之以"或不是汉字："20日前"
      "20日以前""20日，"），或（可以隔空白）跟"月"（1–12，"月"后面紧跟的不是汉字，或者是"底初中
      末前后起止至内之以"、"上旬""下旬""上半""下半"："10月20日""10月底前""1 月 申报""1月起"）——
      "日期""日均""月均""月份"是词，不算；
    后面跟着别的字不管（"日期：""号码""时间""分行"开头的词常紧挨着账号写）。第一组不看后面跟的字。
    只有第一组的 10–30 位就遮；接了几组的要满 12 位（和旧规则一样：不到 12 位的旧规则本来就不遮，
    后面那几组多半是日期、金额，"622202 3000多万元"），不满就只看第一组。没接上的原样留着；一位都
    没遮、旧规则（_BANK）却会遮的，照旧规则遮——红线上的号码不能比以前漏得多。"""
    label, run = match.group(1), match.group(2)
    whole, text, base = match.group(), match.string, match.start(2)
    groups = list(_DIGITS.finditer(run))
    values = [group.group() for group in groups]
    total = taken = 0
    for i, group in enumerate(groups):
        size = len(values[i])
        if i:
            # 这一组在原文里的结尾：后面是分隔符和下一组，或者（最后一组）号码串后面的正文
            end = base + group.end()
            if (
                (_date_from(values, i) and run[group.start() - 1] == " ")
                or (
                    _YEAR.fullmatch(values[i])
                    and (
                        _YEAR_AFTER.match(text, end)
                        # 下一组是月份、后面跟着"月"和日期（"2025 10月10日"）
                        or (
                            i + 1 < len(groups)
                            and len(values[i + 1]) <= 2
                            and 1 <= int(values[i + 1]) <= 12
                            and _MONTH_AFTER.match(text, base + groups[i + 1].end())
                        )
                        or (
                            run[group.start() - 1] == " "
                            and text[end : end + 1] == "-"
                            and i + 2 == len(groups)
                            and len(values[i + 1]) <= 2
                            and 1 <= int(values[i + 1]) <= 12
                        )
                    )
                )
                or (size <= 2 and _SHORT_YEAR_AFTER.match(text, end))
                or (size <= 2 and 1 <= int(values[i]) <= 31 and _DAY_AFTER.match(text, end))
                or (size <= 2 and 1 <= int(values[i]) <= 12 and _MONTH_AFTER.match(text, end))
                or total + size > 19
            ):
                break
        if total + size > 30:
            break
        total += size
        taken = i + 1
    if taken > 1 and total < 12:
        # 接了几组还不到 12 位（旧规则本来就不遮）：后面那几组多半是日期、金额，只看第一组
        taken, total = 1, len(values[0])
    if 10 <= total <= 30:
        cut = groups[taken - 1].end()
        return f"{label}[银行账号已脱敏]{run[cut:]}"
    if old := _BANK.search(whole):
        return f"{whole[: old.start()]}{old.group(1)}[银行账号已脱敏]{whole[old.end() :]}"
    return whole


# ---- 姓名 ----
# 两处当姓名遮：
# 1. "联系人："（"联系人及电话：""联 系 人："……）后面这一行，到标点、数字为止：先跳过单位名
#    （以"处、局、中心"等结尾的、5 个字以上的），第一个像姓名的词遮掉（2–4 个汉字——连写的"联系人："
#    后面不看姓氏、和以前一样，新认的写法要以常见姓氏开头；"张 蕾"这样两个单字中间隔空格的；带"·"
#    的少数民族姓名）；紧跟着的（只隔空格、顿号）以常见姓氏开头的也遮（几个联系人）。
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
    # 11/18：联系人名单里"索""濮""暴"开头的姓名漏遮
    "索濮暴"
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
# 职务（"请联系张三主任""办公室李处长"，见 _titled_cut）：同页面那一层的 _TITLE_END 里称呼以外的那些
_JOB_TITLES = ("处长", "科长", "主任", "部长", "局长", "所长", "站长", "经理", "专员", "工程师")
# 以常见姓氏开头、常挨着电话写的普通词、部门简称、职务（"值班电话：白天 0571-…""商务 0571-…"）：
# 不当姓名
_COMMON_WORDS = frozenset(
    (
        "白天", "周末", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
        "项目", "申报", "金融", "高新", "安全", "文化", "成果", "管理", "服务", "常年",
        "纪检", "商务", "卫健", "卫生", "文旅", "司法", "应急", "安监", "林业", "林草", "武装",
        "董事长", "文件", "方案", "方面", "安排", "申请", "费用", "程序",
        # 11/18：新加的姓"暴""索""濮"开头的常见词（"暴雨，电话：…""索取 138…"；"濮阳"在地名里）
        "暴雨", "暴雪", "暴风", "暴风雨", "暴风雪", "暴露", "暴力", "暴发", "暴涨", "暴跌", "暴增",
        "暴恐", "暴乱", "暴利", "暴晒", "暴热", "暴动", "暴行", "暴徒", "暴政", "暴躁", "暴富",
        "索取", "索引", "索赔", "索要", "索道", "索性", "索偿", "索还", "索回", "索贿", "索价",
        "索求", "索票",
    )
)  # fmt: skip
# 常写在"QQ群：""QQ：""邮箱：""电话："前面、以常见姓氏开头的身份、用途词和文书名（"党员QQ群："
# "管理员QQ：""简历：[邮箱]""申请表 [邮箱]"，11/18、11/19 独立 Claude 第七、八轮的合成用例，本机库里
# 没有）：号码前面的这些词不当姓名
# （_phone_names、_glued_name）。不放进 _COMMON_WORDS：网站标题里的人名（govsites.privacy："党员
# 张小明出席会议"）、名单（lists.body）也用 _name_like，那边照旧
_ROLE_WORDS = frozenset(
    (
        "党员", "党建", "管理员", "简历", "简报", "简章", "成员", "应聘", "申请人", "毕业生",
        "应届", "班长", "班主任", "车主", "房东", "商户", "游客", "关注",
        "申请表", "申请书", "申报书", "申报表", "原件", "盖章版",
    )
)  # fmt: skip
# 职务前面以常见姓氏开头、不是姓名的词（"常务副主任""高级工程师""车间主任""班主任"，见 _titled_cut）
_TITLE_PREFIXES = frozenset(("常务", "常务副", "高级", "车间", "金牌", "文秘", "安环", "班"))
# 以常见姓氏开头、不带"省、市"的省份和地级市名（"石家庄 0311-…"这样按城市列的联系电话）：
# 不当姓名。别的省市名不以这些姓氏开头，本来就不会被当成姓名；县、区名太多，不列。以"州"结尾的
# 地级市（"杭州"）一般按单位字结尾排除，"联系人："后面最后一个词的两字例外（见 _name_like）不看
# 单位字，所以也列上
_PLACE_NAMES = frozenset(
    (
        "江苏", "江西", "安徽", "甘肃", "宁夏", "吉林",
        "石家庄", "唐山", "秦皇岛", "邢台", "张家口", "吕梁", "包头", "沈阳", "白山", "白城",
        "伊春", "宿迁", "宁波", "金华", "马鞍山", "安庆", "黄山", "龙岩", "宁德", "吉安",
        "安阳", "焦作", "濮阳", "许昌", "商丘", "周口", "武汉", "黄石", "荆门", "黄冈", "邵阳",
        "岳阳", "常德", "张家界", "娄底", "江门", "湛江", "揭阳", "桂林", "成都", "巴中",
        "安顺", "毕节", "曲靖", "林芝", "安康", "商洛", "金昌", "白银", "武威", "张掖",
        "石嘴山", "吴忠", "伊犁", "甘孜", "楚雄", "文山", "甘南", "黄南",
        "徐州", "常州", "苏州", "杭州", "温州", "宿州", "池州", "郑州", "荆州", "惠州", "梅州",
        "柳州", "贺州", "兰州",
    )
)  # fmt: skip
# "联系人张三 0571-…"这样标签后面没写冒号、紧贴着姓名的：标签后面那截当姓名看（同单位名）
_PERSON_LABELS = ("联系人", "联络人", "经办人")
_CONTACT_LABEL = re.compile(
    r"联\s*+系\s*+人(?:\s*+[及和与]?+\s*+(?:联\s*+系\s*+)?+(?:电\s*+话|方\s*+式))?+"
    r"\s*+[:：][^\S\n]*+\n?+[^\S\n]*+"
)
# 旧规则认的"联系人："写法（三个字连写，冒号前可以有空白）
_CLASSIC_LABEL = re.compile(r"联系人\s*+[:：]")
# "联系人："后面看到这里为止：换行、标点、数字（号码）、"["（遮过的号码）
_NAME_STOP = re.compile(r"[\n，,。；;：:（(）)/【\[]|\d")
# 同一行里隔开几个姓名的：空格、顿号
_NAME_GAP = re.compile(r"(?:[^\S\n]|、)+")
_HAN_RUN = re.compile(rf"[{_HAN}·]+")
# 遮姓名时座机都已经遮了（_mask_landlines）；还认得出的座机写法是没当电话的编号，前面的字不当姓名。
# 遮成标记的号码都算：电话、手机号、QQ 号，还有用户名是一串数字的邮箱（见 _email_mark）。以前号码
# 先露着、页面上当号码遮，姓名跟着遮；现在入库先换成了标记，前面的姓名照样要遮（11/18 独立 Claude
# 第五、六轮）。字母用户名的邮箱不算
_BEFORE_PHONE = re.compile(r"\[(?:新?电话|手机号|新?QQ号|号码邮箱)已脱敏\]")
# 姓名和号码中间隔着的"，电话：""，""电话：""（"（"某某，电话：[电话已脱敏]""某老师，[手机号已脱敏]"
# "某某、某某（[电话已脱敏]，某中心）"，11/18 漏遮）：从号码往前去掉这一截，再看前面那个词。
# 隔开符也认"、；/"，标签也认"QQ：""邮箱："，前面可以带空格（"王某、[邮箱已脱敏]""王某 QQ：
# [QQ号已脱敏]"，11/18 独立 Claude 第六轮）
_NAME_TO_PHONE = re.compile(
    r"[^\S\n]*+(?:[，,（(、；;/／][^\S\n]*+)?"
    r"(?:(?:(?:联系)?(?:电话|手机|座机|固话)(?:号码)?|(?:QQ|qq|Qq|ＱＱ)群?(?:号码?)?|(?:电子)?(?:邮箱|邮件))"
    r"[^\S\n]*+)?[:：]?[^\S\n]*+$"
)


def _is_han(char: str) -> bool:
    return 0x4E00 <= ord(char) <= 0x9FFF or char == "·"


def _inline_space(char: str) -> bool:
    return char.isspace() and char not in "\r\n"


def _name_like(
    word: str, *, after_label: bool = False, any_surname: bool = False, last: bool = False
) -> bool:
    """word 像不像姓名（"张 蕾"传进来时去掉了中间的空格）：以常见姓氏开头（复姓也认），2–3 个字
    （复姓 4 个）；after_label——紧跟"联系人："的第一个像姓名的词——2–4 个字，带"·"的少数民族
    姓名（3–12 个字）只在这里认、不看姓氏。any_surname：连写的"联系人："（旧规则认的写法，后面
    2–8 个汉字一律遮）后面那个词也不看姓氏（"联系人：南楠"，和以前一样）；本次新认的写法（"联系人
    及电话：""联 系 人："）后面要以常见姓氏开头（"联系人及电话：日前"的"日前"不遮）。
    以单位、地名的字结尾的，含"电话""单位"这类标签字的，光是"先生""老师"的，都不算。
    last：这个词是"联系人："后面同一行（冒号后没换行）的最后一个词（到标点、数字、换行为止）。"""
    if (
        word in _TITLES
        or word in _COMMON_WORDS
        or word in _PLACE_NAMES
        or any(label in word for label in _LABEL_WORDS)
    ):
        return False
    surname = word[0] in _SURNAMES or word.startswith(_COMPOUND_SURNAMES)
    if word.endswith(_UNIT_ENDS):
        # "联系人："这一行的最后一个词、两个字、以常见姓氏开头的，以"路、镇、科"这类字结尾也是姓名
        # （"联系人：王镇""联系人：省科技厅高新处 张路"）；别处以单位、地名的字结尾的不当姓名
        # （"高新处""沙县"，"联系人：省科技厅 高校 张三"的"高校"）
        return last and len(word) == 2 and surname
    if "·" in word:
        return after_label and 3 <= len(word) <= 12
    if after_label and (surname or any_surname):
        return 2 <= len(word) <= 4
    if not surname:
        return False
    return 2 <= len(word) <= 3 or (len(word) == 4 and word.startswith(_COMPOUND_SURNAMES))


# 单位名、地名后面紧跟着姓名时，前面那截的结尾：单位的字、"联系人"这类标签，还有不以单位的字结尾的
# 地区名（"某盟张某某"，11/18 漏遮；以"盟"结尾的、下面另写的两个地区名同理）
_GLUE_HEADS = (*_UNIT_ENDS, *_PERSON_LABELS, "盟", "大兴安岭", "神农架")
# 姓加称呼（"张老师""王同志"）前面那截的结尾：单位、地区的字（_GLUE_HEADS），或者学校（"某某大学
# 张老师 [电话]"）；"联系""致电"这类词后面的另算（_glued_name）。"相关同志""有关同志""值班老师"
# 前面是普通词，不当姓名（主干不遮；11/19 正确性那位第八轮：上一轮放成"前面不论什么字都认"，多遮
# 了）。页面那一层（v1._whole_or_called）同一套
_TITLED_HEADS = (*_GLUE_HEADS, "大学", "中学", "小学")


# 号码前面写着的"联系""致电"这类词（同页面那一层的 _CALL_VERB_BEFORE）
_CALL_VERBS = ("联系", "致电", "电询", "拨打", "咨询", "询问", "找")


def _titled_cut(run: str) -> int | None:
    """联系类词、单位名后面紧跟着"姓名+职务"的（"请联系张三主任 [电话]""办公室李处长"，11/18 独立
    Claude 第七轮：主干页面上按"张三主任 李某"连着认出来遮，现在入库先遮了李某，页面上"联系张三主任"
    拆不出姓名）：姓名从第几个字起，姓名连职务一起遮；不是这样的返回 None。姓名是像姓名的 2–4 个字，
    或者单独一个常见姓氏（"李处长"）。"""
    title = next((t for t in _JOB_TITLES if run.endswith(t)), None)
    if title is None:
        return None
    rest = run[: -len(title)]
    for size in (4, 3, 2, 1):
        cut = len(rest) - size
        if cut < 2 or not rest[:cut].endswith((*_GLUE_HEADS, *_CALL_VERBS)):
            continue
        name = rest[cut:]
        if name in _TITLE_PREFIXES:
            continue
        if (size > 1 and _name_like(name)) or (size == 1 and name in _SURNAMES):
            return cut
    return None


def _glued_name(run: str) -> int | None:
    """单位名后面紧跟着姓名、中间没隔开的（"省财政厅张三""办公室李四"，还有没写冒号的"联系人张三"）：
    前面一截以单位的字或"联系人"结尾、后面 2–4 个字以常见姓氏开头，返回姓名从第几个字起；不是这样的
    返回 None。后面 4 个字是两个连写的两字姓名的也算（"某市张某李某"）；"联系"后面、学校名后面紧跟着
    姓加称呼的也算（"请联系张老师 [电话]""某某大学张老师"）。11/18 这三种漏遮。姓名后面带职务的见
    _titled_cut。"""
    if (cut := _titled_cut(run)) is not None:
        return cut
    # 长的优先：复姓"司马"的"司"也是单位的字（"省科技厅司马小明"不能切成"省科技厅司|马小明"）
    for size in (4, 3, 2):
        cut = len(run) - size
        if cut < 2:
            continue
        head, tail = run[:cut], run[cut:]
        if tail in _ROLE_WORDS:  # "请联系班主任""某某支部党员"
            continue
        if head.endswith(_GLUE_HEADS) and (
            _name_like(tail) or (size == 4 and _name_like(tail[:2]) and _name_like(tail[2:]))
        ):
            return cut
        # 姓加称呼前面以单位、地区的字或学校结尾的也认（"某某大学张老师 [电话]"，_TITLED_HEADS）；
        # "联系""致电"这类词后面紧跟着的姓名也认（"请致电张三 [电话]"，11/18 独立 Claude 第六、
        # 七轮）
        titled = size == 3 and tail[0] in _SURNAMES and tail[1:] in _TITLES
        if titled and head.endswith(_TITLED_HEADS):
            return cut
        if head.endswith(_CALL_VERBS) and size <= 3 and (_name_like(tail) or titled):
            return cut
    return None


def _spaced_glued_name(text: str, end: int) -> int | None:
    """单位名后面紧跟着中间隔空格写的两字姓名（"某厅张 某"，11/18 漏遮）：姓名从哪里起；
    不是这样的返回 None。只认空格两边各一个字的。"""
    if end < 2 or not _is_han(text[end - 1]) or not _inline_space(text[end - 2]):
        return None
    gap = end - 1
    while gap > 0 and _inline_space(text[gap - 1]):
        gap -= 1
    start = gap
    while start > 0 and _is_han(text[start - 1]):
        start -= 1
    if gap - start < 3 or (start > 0 and text[start - 1].isalnum()):
        return None
    run = text[start:gap] + text[end - 1]
    cut = _glued_name(run)
    return start + cut if cut == len(run) - 2 else None


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
        # 这一行到"[姓名已脱敏]"为止的（第一遍遮的时候后面还有姓名）：哪个词都不是这一行的最后一个词
        continued = text.startswith("[姓名已脱敏]", end)
        # 连写的"联系人："（旧规则认的写法）后面第一个词照旧不看姓氏；新认的写法要以常见姓氏开头
        old_label = _CLASSIC_LABEL.match(label.group()) is not None
        classic = not next_line and old_label
        line_end = text.find("\n", label.end())
        rest_of_line = text[label.end() : line_end if line_end >= 0 else None]
        if not old_label and "[姓名已脱敏]" in rest_of_line:
            # 库里旧正文：新认的写法旧规则不认，这一行后面有旧规则遮过的姓名（那是真姓名），前面的
            # 词多半不是姓名（"联系人及电话：申请、[姓名已脱敏]""文件，[姓名已脱敏]"）——这一行
            # 不再认；第一遍遮过的再过一遍也走这里，不会多遮
            continue
        named = False
        words = _words(text, label.end(), end)
        for k, (a, b, word) in enumerate(words):
            if text.startswith("[姓名已脱敏]", b):
                # 紧贴着"[姓名已脱敏]"的是旧规则吞剩的半截（"联系人及电话：中小企[姓名已脱敏]"），
                # 不是姓名：库里按旧规则遮过的正文再过一遍不能再吞
                break
            # 冒号后同一行写的、这一行的最后一个词：两个字的"王镇"也算姓名（见 _name_like）
            last = not next_line and k == len(words) - 1 and not continued
            if named:
                # 第一个姓名后面只隔空格、顿号的，以常见姓氏开头的也遮（几个联系人）
                gap = text[spans[-1][1] : a]
                if not (_NAME_GAP.fullmatch(gap) and _name_like(word, last=last)):
                    break
                spans.append((a, b))
            elif _name_like(word, after_label=not next_line, any_surname=classic, last=last):
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
        # 去掉姓名和号码中间的"，电话："这一截：同一行里、号码前 30 个字以内找
        # （长行里每个号码都从行首找会很慢）
        window = max(0, end - _CUE_REACH)
        newline = text.rfind("\n", window, end)
        line_start = newline + 1 if newline >= 0 else window
        glue = _NAME_TO_PHONE.search(text, line_start, end)
        if glue and glue.group():
            end = glue.start()
        if (
            _word_ending_at(text, end) is None
            and (cut := _spaced_glued_name(text, end)) is not None
        ):
            spans.append((cut, end))
            continue
        while (found := _word_ending_at(text, end)) is not None:
            start, word = found
            if not _name_like(word) or word in _ROLE_WORDS:
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


def _plain_lines(
    node: etree._Element, skip: frozenset = frozenset(), *, keep_select: bool = False
) -> list[str]:
    """节点里的文字，一行一行。下拉框（"县区网站""省级厅委导航"这类跳转菜单）的选项不是正文：
    11/12 漯河等站正文区是空的，按文字密度兜底时会把一整串选项当成正文，所以默认跳过。
    keep_select：防护页判断看页面前 500 字时照旧连下拉框的字一起算，范围不变。"""
    return _collect_lines(node, skip, keep_select)[0]


def _lines_before(
    node: etree._Element, until: etree._Element, skip: frozenset = frozenset()
) -> list[str] | None:
    """node 里 until 前面的字（分行、跳过哪些都和 _plain_lines 一样，不带下拉框）；碰不到 until
    （它不在 node 里，或在导航、页尾、下拉框这类不收字的地方）时为 None。找正文前面的发文机关、
    文号、发布日期用：按正文节点所在的位置截，正文第一句在页面上面别处也出现过（如标题重复一遍）时
    不会截错地方。"""
    lines, reached = _collect_lines(node, skip, False, until)
    return lines if reached else None


def _collect_lines(
    node: etree._Element,
    skip: frozenset,
    keep_select: bool,
    until: etree._Element | None = None,
) -> tuple[list[str], bool]:
    """_plain_lines、_lines_before 共用：按顺序收字、分行；碰到 until 就停，第二个值说明碰到了
    没有。"""
    parts: list[str] = []
    skipped = {"script", "style", "noscript", "nav", "footer"} | (
        set() if keep_select else {"select"}
    )

    def visit(element: etree._Element) -> bool:
        if not isinstance(element.tag, str) or element in skip:
            return False
        if element is until:
            return True
        if element.tag.lower() in skipped:
            return False
        marker = f"{element.get('class', '')} {element.get('id', '')}"
        if _JUNK_CLASS.search(marker):
            return False
        if element.tag.lower() == "br":
            parts.append("\n")
        if element.text:
            parts.append(element.text)
        for child in element:
            if visit(child):
                return True
            if child.tail:
                parts.append(child.tail)
        if element.tag.lower() in _BLOCK_TAGS:
            parts.append("\n")
        return False

    reached = visit(node)
    lines = [line for raw in "".join(parts).splitlines() if (line := " ".join(raw.split()))]
    return lines, reached


def _body_node(root: etree._Element, host: str | None = None) -> etree._Element | None:
    if host in _SCRIPT_BODY_HOSTS and (node := _script_body_node(root)) is not None:
        return node
    site = _SITE_BODY_SELECTORS.get(host or "")
    if site and (node := _longest(root.xpath(site))) is not None:
        return node
    if (node := _editor_node(root)) is not None:
        return node
    for selector in _BODY_SELECTORS:
        if (node := _longest(root.xpath(selector))) is not None:
            return node
    return _density_body_node(root)


def _longest(nodes: list[etree._Element]) -> etree._Element | None:
    """够长（20 字以上）的里面最长的一个。"""
    candidates = [n for n in nodes if len(" ".join(_plain_lines(n))) >= 20]
    return max(candidates, key=lambda n: len(" ".join(_plain_lines(n)))) if candidates else None


def _editor_node(root: etree._Element) -> etree._Element | None:
    """TRS 编辑器正文区（_EDITOR），只在没有歧义时用：够长的编辑器块只有一份，或几份一模一样
    （电脑版、手机版，取第一份）；页面有 #zoom 等正文标记的，要有一份在它里面（在外面的可能是
    侧栏里的另一段），而且正文标记里编辑器块之外只有 _EDITOR_SIDE_LABELS 里的字（别的字哪怕一句
    也可能是正文的一部分，如"本通知自公布之日起施行。"）。不满足的不用，照旧走通用规则。"""
    blocks = [n for n in root.xpath(_EDITOR) if len(" ".join(_plain_lines(n))) >= 20]
    if not blocks or len({"\n".join(_plain_lines(n)) for n in blocks}) > 1:
        return None
    marked = [m for m in root.xpath(_MARKED_BODY) if len(" ".join(_plain_lines(m))) >= 20]
    if not marked:
        return blocks[0]
    for block in blocks:
        for container in marked:
            if block is container or container in block.iterancestors():
                rest = _plain_lines(container, frozenset(blocks))
                return block if set(rest) <= _EDITOR_SIDE_LABELS else None
    return None


def _script_body_node(root: etree._Element) -> etree._Element | None:
    """<div v-html="名字"> 显示的 HTML：页面自带脚本里"名字: `…`"这段模板字符串
    （见 _SCRIPT_BODY_HOSTS）。字符串里有没转义的 ${…}（要运行脚本才知道的内容）、或转义还原不了的，
    不取。"""
    names = {name.strip() for name in root.xpath("//@v-html")}
    for script in root.xpath("//script[not(@src)]"):
        source = script.text or ""
        for name in sorted(n for n in names if _VUE_NAME.fullmatch(n)):
            match = re.search(
                rf"(?<![\w$]){re.escape(name)}\s*:\s*`((?:[^`\\]|\\[\s\S])*)`", source
            )
            if not match or _TEMPLATE_INTERPOLATION.search(match.group(1)):
                continue
            fragment = _template_text(match.group(1))
            if fragment is None:
                continue
            try:
                node = html.fragment_fromstring(fragment, create_parent="div")
            except (etree.ParserError, ValueError):
                continue
            if len(" ".join(_plain_lines(node))) >= 20:
                return node
    return None


def _template_text(source: str) -> str | None:
    """按 JS 的规则还原模板字符串里的转义（_TEMPLATE_ESCAPE）；码位超出范围等还原不了的
    返回 None。"""

    def unescape(match: re.Match) -> str:
        escape = match.group(1)
        if escape[0] in "ux" and len(escape) > 1:
            return chr(int(escape[1:].strip("{}"), 16))
        return _TEMPLATE_SIMPLE.get(escape, escape)

    try:
        return _TEMPLATE_ESCAPE.sub(unescape, source)
    except (ValueError, OverflowError):
        return None


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
    for match in _ISSUER_LABEL.finditer(prefix):
        value = match.group(1).strip()
        if _AGENCY_END.search(value):
            return normalize_issuer(value)
    if title_issuer := _title_issuer(root):
        return title_issuer
    for match in _PUBLISHER_LABEL.finditer(prefix):
        value = match.group(1).strip()
        if _is_agency(value):
            return normalize_issuer(value)
    for name in ("issuer", "publishingorganization", "contentSource"):
        value = (_metadata(root, name) or "").strip()
        if len(value) <= 60 and _is_agency(value):
            return normalize_issuer(value)
    return None


def _is_agency(value: str) -> bool:
    return bool(_AGENCY_END.search(value)) and value not in _GENERIC_ISSUERS


def _title_issuer(root: etree._Element) -> str | None:
    """文件标题开头的机关（"某某市人民政府关于……""某某厅等 7 部门关于印发……"），每个机关都要是
    完整的机关名（见 _full_agency），有一个不是就不用标题。校验用的名单有一份读不到，标题一概
    不用。"""
    places, _, bodies = _agency_names()
    if not places or not bodies:
        return None
    for title in _page_titles(root):
        match = _TITLE_ISSUER.match(title)
        if not match:
            continue
        value = normalize_issuer(" ".join(match.group("org").split()))
        if all(_full_agency(part) for part in value.split("、")):
            return value + "".join((match.group("etc") or "").split())
    return None


def _full_agency(name: str) -> bool:
    """完整的机关名：国务院某个部、中央机关全称、中央机关在地方的分支，或者"地名 + 词表里的机关名称"
    （"中共"开头的是"地名 + 党委机构"）。缺地名的简称、内设机构、带会议讲话字样的新闻标题、地名后面
    夹着人名的（"北京市张三建议市政府"）都不算。"""
    if name in _GENERIC_ISSUERS or _NOT_ORG.search(name):
        return False
    bare = name.removeprefix("中华人民共和国")
    if _MINISTRY.match(bare) or name.removeprefix("中共") in _CENTRAL_AGENCIES:
        return True
    for parent, branches in _BRANCH_BODIES.items():
        if name.startswith(parent):
            rest = name.removeprefix(parent)
            body, stripped = _strip_places(rest)
            short = next((k for k in (3, 2) if rest[:k] in _PROVINCE_SHORT), 0)
            if not stripped and short:
                body, stripped = rest[short:], True
            return stripped and body in branches
    party = name.startswith("中共")
    body, stripped = _strip_places(name.removeprefix("中共"))
    return stripped and body in (_PARTY_BODIES if party else _agency_names()[2])


@functools.cache
def _agency_names() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """地级、县级区划名，"某某市"去掉"市"的简称，机关名称词表；名单文件读不到（没有文件、不是
    UTF-8）时是空的，标题里的机关一概不用，照旧看发布机构（见 _title_issuer）。"""

    def read(name: str) -> frozenset[str]:
        try:
            lines = (_DATA_DIR / name).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            logger.warning("发文机关校验用的名单读不到：%s", name)
            return frozenset()
        return frozenset(n for line in lines if (n := line.strip()) and not n.startswith("#"))

    places = read("place_names.txt")
    # 直辖市不在名单里（省级），简称单独加上（"北京经济技术开发区""上海海关"）
    cities = frozenset(n[:-1] for n in places if n.endswith("市") and len(n) >= 3)
    cities |= frozenset(_MUNICIPALITIES)
    return places, cities, read("agency_bodies.txt")


def _strip_places(name: str) -> tuple[str, bool]:
    """去掉开头的地名（省级区划名、名单里的地名，可连着几个；地级市简称后面跟开发区、海关的去掉简称）；
    返回剩下的和去没去掉。"""
    places, cities, _ = _agency_names()
    stripped = False
    while True:
        if match := _PROVINCE_HEAD.match(name):
            name, stripped = name[match.end() :], True
            continue
        head = next((k for k in range(min(len(name), 12), 1, -1) if name[:k] in places), 0)
        if not head:
            head = next(
                (
                    k
                    for k in range(min(len(name), 8), 1, -1)
                    if name[:k] in cities and name[k:].startswith(_ZONE_WORDS)
                ),
                0,
            )
        if not head:
            return name, stripped
        name, stripped = name[head:], True


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


# 不是网页上的字：脚本、样式等里的（正文提取 _plain_lines 也不收）
_NOT_PAGE_TEXT_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "canvas",
    "iframe",
    "video",
    "audio",
    "object",
    "embed",
    "datalist",
    "select",
    "font",
    "noembed",
    "noframes",
    "progress",
    "meter",
    "rp",
    # 矢量/公式的绘制及 defs、symbol 等是否被使用，不能靠 HTML 文本遍历证明。
    "svg",
    "math",
}
# 旧式 HTML 配色也能形成同色字；popover 是否弹出要靠脚本，静态文本都不作证。
_UNCERTAIN_DISPLAY_ATTRIBUTES = {
    "color",
    "bgcolor",
    "text",
    "background",
    "link",
    "vlink",
    "alink",
    "popover",
}
_LAYOUT_NUMBER = re.compile(r"([0-9]{1,4}(?:\.[0-9]{1,3})?)(px|pt|em|%)?", re.ASCII)
_BOX_SPACING = re.compile(r"(?:margin|padding)(?:-(?:top|right|bottom|left))?", re.ASCII)


def _unproven_page_element(element: etree._Element) -> bool:
    """不画出的后备文字、动态容器和可能隐藏文字的 HTML 属性，不能作自动去噪证明。"""
    return (
        not isinstance(element.tag, str)
        or element.tag.lower() in _NOT_PAGE_TEXT_TAGS
        or element.get("hidden") is not None
        or any(name in element.attrib for name in _UNCERTAIN_DISPLAY_ATTRIBUTES)
        or (element.tag.lower() == "dialog" and element.get("open") is None)
    )


def _plain_layout_style(style: str) -> bool:
    """只接受能静态读懂的有限排版声明，不把它当作浏览器的可见性判断。

    不解 CSS 转义、注释、变量、函数或 !important；未知声明整块拒绝。字号和间距设上下界，
    避免接近零的字、超大间距把旧条款藏起来。定位、裁剪、透明度、颜色等也留给人工。
    """
    for declaration in style.lower().split(";"):
        if not declaration.strip():
            continue
        name, colon, value = declaration.partition(":")
        name, value = name.strip(), value.strip()
        if not colon:
            return False
        if name == "text-align" and value in {"left", "right", "center", "justify"}:
            continue
        spacing = _BOX_SPACING.fullmatch(name)
        values = value.split()
        if not values or len(values) > (4 if name in {"margin", "padding"} else 1):
            return False
        for token in values:
            if name.startswith("margin") and spacing and token == "auto":
                continue
            number = _LAYOUT_NUMBER.fullmatch(token)
            if number is None:
                return False
            amount, unit = float(number[1]), number[2] or ""
            if spacing:
                valid = (amount == 0 and unit == "") or (
                    unit in {"px", "pt", "em"} and 0 <= amount <= (6 if unit == "em" else 100)
                )
            elif name == "font-size":
                valid = (unit == "px" and 8 <= amount <= 72) or (unit == "pt" and 6 <= amount <= 54)
            elif name == "line-height":
                valid = (unit in {"", "em"} and 1 <= amount <= 3) or (
                    unit in {"px", "pt"} and 8 <= amount <= 100
                )
            elif name in {"width", "height", "max-width", "max-height"}:
                valid = (unit == "px" and 1 <= amount <= 2000) or (
                    unit == "%" and 1 <= amount <= 100
                )
            else:
                valid = False
            if not valid:
                return False
    return True


def _known_qr_caption(element: etree._Element, node: etree._Element, host: str) -> bool:
    """四川已核实的二维码提示：只放行这句文字，不放行整块定位样式和里面的任意内容。"""
    parent = element.getparent()
    return (
        host == "kjt.sc.gov.cn"
        and element.tag == "div"
        and element.get("id") == "qr_container"
        and element.get("hidden") is None
        and (element.text or "").strip() == "扫一扫在手机打开当前页"
        and re.sub(r"\s+", "", element.get("style") or "").lower()
        == "margin:auto;position:relative;margin-bottom:5px;color:#898989;"
        and parent is not None
        and parent.get("id") == "div_div"
        and parent.getparent() is node
    )


def _hidden_source_header(element: etree._Element, node: etree._Element, host: str) -> bool:
    """只认已核实的山东页头来源字；不能把以“来源”开头的隐藏旧条款也当成网页噪声。"""
    if (
        host != "gxt.shandong.gov.cn"
        or "news-body" not in (node.get("class") or "").split()
        or element.tag != "span"
        or len(element)
        or re.sub(r"\s+", "", element.text or "") != "信息来源：山东省工业和信息化厅"
        or element.get("hidden") is not None
        or re.sub(r"\s+", "", element.get("style") or "").lower() != "display:none;"
    ):
        return False
    parent = element.getparent()
    header = parent.getparent() if parent is not None else None
    return (
        parent is not None
        and parent.tag == "p"
        and header is not None
        and "headline" in (header.get("class") or "").split()
        and header.getnext() is node
    )


def outside_body_lines(html_text: str, url: str) -> list[str] | None:
    """网页上不算正文的字，按行：导航、发布信息、按钮、页尾、弹窗，以及正文区里按 _plain_lines
    同样规则不算正文的二维码块、按钮块等。正文区按 parse_detail 同一套规则选；分行和去空白也和
    _plain_lines 一样（只在 br 前、块级元素后换行，行内的 a、span、font 不切行），所以以前收进正文的
    网页上的字，和这里的某一行完全相同。
    给 youju refetch-bodies 核对"去掉的行是不是网页上的字"用：原网页删掉的正文条款，就算还留在脚本、
    hidden 或行内样式隐藏的元素里，也不能证明只是网页噪声。唯一例外是已核实的山东工信厅页头来源字
    （限定网站、位置和全文）。
    不执行 CSS/脚本；行内样式只接受 _plain_layout_style 的有限普通排版，未知写法保守留给人工。
    旧式 HTML 配色、媒体后备文字和未展开的动态内容不作证；折叠 details 只读首个 summary。
    四川二维码只放行已核实的固定提示句，不把整个定位块当作证据。外部样式表不在此解析。
    解析不了、没有正文区的返回 None；整页没有能作证的字返回 []。"""
    if not html_text.strip():
        return None
    try:
        root = html.fromstring(html_text)
    except (etree.ParserError, ValueError):
        return None
    body = root.xpath("//body")
    host = (urlsplit(url).hostname or "").lower()
    node = _body_node(root, host)
    if not body or node is None:
        return None
    # 从 body 开始遍历前，也检查 html 等祖先；隐藏祖先不能被可见子元素“覆盖”。
    if any(
        _unproven_page_element(parent) or not _plain_layout_style(parent.get("style") or "")
        for parent in body[0].iterancestors()
    ):
        return []
    junk = _SITE_JUNK.get(host)
    skipped = frozenset(node.xpath(junk)) if junk else frozenset()
    parts: list[str] = []

    def visit(element: etree._Element, inside: bool) -> None:
        # 注释、处理指令没有标签名。后面的可见文字在 tail 上，由父元素收集。
        if not isinstance(element.tag, str):
            return
        if _unproven_page_element(element):
            return
        # 四川真实页面的浅色外框：只认这个已核实的容器和唯一声明，不一般性放行背景/前景颜色。
        known_background = (
            host == "kjt.sc.gov.cn"
            and element.tag == "div"
            and element.get("class") == "box"
            and re.sub(r"\s+", "", element.get("style") or "").lower()
            == "background-color:#e9f3fd;"
        )
        if not known_background and not _plain_layout_style(element.get("style") or ""):
            # 两个已核实例外的固定文字各自单独成行，避免和前后的网页字粘成一行。
            if not inside and _hidden_source_header(element, node, host):
                parts.append(f"\n{element.text or ''}\n")
            elif inside and _known_qr_caption(element, node, host):
                parts.append("\n扫一扫在手机打开当前页\n")
            return
        if element is node:
            inside = True
        elif inside and (
            element in skipped
            or element.tag.lower() in {"nav", "footer"}
            or _JUNK_CLASS.search(f"{element.get('class', '')} {element.get('id', '')}")
        ):
            inside = False
        tag = element.tag.lower()
        if tag == "details" and element.get("open") is None:
            # 折叠时只有首个直接 summary 子元素可见，details.text 和 summary.tail 也藏在里面。
            summary = next((child for child in element if child.tag == "summary"), None)
            if summary is not None:
                visit(summary, inside)
            return
        # 换行只看标签，不看在不在正文区：正文区里的字不收集，但这块本身要把前后的导航、页尾断开。
        # br 同时属于 _BLOCK_TAGS，多出来的空行下面会丢掉。分行必须和 _plain_lines 完全一致：
        # 只在这里多切一刀（比如给 dialog、summary 前后换行），就可能切出旧正文里没有的短行，
        # 正好等于被删的条款标题而被当成网页上的字（agy 10/24 复核举的例子）。
        if tag == "br":
            parts.append("\n")
        if not inside and element.text:
            parts.append(element.text)
        for child in element:
            visit(child, inside)
            if not inside and child.tail:
                parts.append(child.tail)
        if tag in _BLOCK_TAGS:
            parts.append("\n")

    visit(body[0], False)
    return [line for raw in "".join(parts).splitlines() if (line := " ".join(raw.split()))]


def _page_code_fingerprint(root: etree._Element, url: str) -> str:
    """页面上能让弹窗显示出来的东西的指纹，按在页面上出现的顺序原样记下：外部脚本、样式表（含预加载
    的）、框架、<base> 的完整地址（含协议和 ? 后面的参数，重复的也记），页面自带的脚本、<style>、
    on… 事件属性、框架里直接写的页面。只把页面自带脚本里 _PAGE_ID_CONTEXTS 那几句的编号当 0。"""

    def page_ids(code: str) -> str:
        for context in _PAGE_ID_CONTEXTS:
            code = context.sub(lambda m: "0".join(m.groups()), code)
        return code

    parts: list[str] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = element.tag.lower()
        rel = set((element.get("rel") or "").lower().split())
        for kind, name, wanted in (
            ("js", "src", tag == "script"),
            ("css", "href", tag == "link" and bool(_CODE_LINKS & rel)),
            ("frame", "src", tag in ("iframe", "frame", "embed")),
            ("frame", "data", tag == "object"),
            ("base", "href", tag == "base"),
        ):
            if wanted and (value := element.get(name)) is not None:
                parts.append(f"{kind} {urljoin(url, value.strip())}")
        if tag == "script" and element.get("src") is None and element.text:
            parts.append(f"script {page_ids(element.text)}")
        if tag == "style" and element.text:
            parts.append(f"style {element.text}")
        if tag == "iframe" and (srcdoc := element.get("srcdoc")) is not None:
            parts.append(f"srcdoc {srcdoc}")
        parts.extend(
            f"{name.lower()} {value}"
            for name, value in element.attrib.items()
            if name.lower().startswith("on")
        )
    return hashlib.sha256("\n\0".join(parts).encode()).hexdigest()[:16]


def _page_words(root: etree._Element, dialogs: frozenset) -> str:
    """整页的字（认得出的弹窗以外），不去噪：导航、页尾、下拉框、分享栏、noscript 里的都算，只不看
    脚本、样式的代码；属性上的字（_WORD_ATTRS）也算。放过弹窗的页面用它查还有没有别的验证、限流的字。"""
    text: list[str] = []
    attributes: list[str] = []

    def visit(element: etree._Element) -> None:
        if not isinstance(element.tag, str) or element in dialogs:
            return
        if element.tag.lower() in ("script", "style"):
            return
        attributes.extend(v for name in _WORD_ATTRS if (v := element.get(name)))
        if element.text:
            text.append(element.text)
        for child in element:
            visit(child)
            if child.tail:
                text.append(child.tail)

    visit(root)
    return "\n".join(" ".join(part.split()) for part in ["".join(text), *attributes])


def _search_throttle_dialogs(root: etree._Element, url: str) -> list[etree._Element]:
    """页面上认得出的站内搜索限流弹窗（见 _SEARCH_THROTTLE_DIALOG 上面的说明）。网站和页面代码要是
    浏览器里核实过的（_SEARCH_THROTTLE_VERIFIED）；弹窗本身四样都要对上：style 正好只有 display:none
    （不是"display:none;display:block"这类后面又改掉的）；弹窗里的字正好是"×""搜索过于频繁，请输入
    验证码""确定"三段；整个结构和 _SEARCH_THROTTLE_SHAPE 一样；没有图片、链接、说明文字这类属性。"""
    verified = _SEARCH_THROTTLE_VERIFIED.get((urlsplit(url).hostname or "").lower())
    if not verified or not root.xpath(_SEARCH_THROTTLE_DIALOG):
        return []
    if _page_code_fingerprint(root, url) not in verified:
        return []
    found = []
    for node in root.xpath(_SEARCH_THROTTLE_DIALOG):
        style = "".join((node.get("style") or "").split()).lower().rstrip(";")
        text = tuple("".join(s.split()) for s in node.itertext() if s.strip())
        shape = tuple(
            (
                e.tag,
                e.get("id") or "",
                " ".join((e.get("class") or "").split()),
                e.get("type") or "",
                e.get("value") or "",
            )
            for e in node.iter()
            if isinstance(e.tag, str)
        )
        attributes_ok = not any(
            name.startswith("on") or name in ("src", "href", "alt", "title", "placeholder")
            for e in node.iter()
            if isinstance(e.tag, str)
            for name in e.attrib
        )
        if (
            style == "display:none"
            and text == _SEARCH_THROTTLE_TEXT
            and shape == _SEARCH_THROTTLE_SHAPE
            and attributes_ok
        ):
            found.append(node)
    return found


def search_throttle_site(url: str) -> bool:
    """网址是不是放过站内搜索限流弹窗的网站（_SEARCH_THROTTLE_VERIFIED）。"""
    return (urlsplit(url).hostname or "").lower() in _SEARCH_THROTTLE_VERIFIED


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
    dialogs = frozenset(_search_throttle_dialogs(root, url))
    visible = "\n".join(_plain_lines(body[0], dialogs, keep_select=True)) if body else ""
    # 找发文机关、文号、发布日期用不带下拉框的字，和正文的取法一致
    plain = "\n".join(_plain_lines(body[0], dialogs)) if body else ""
    if _PROTECTION.search(title + visible[:500]):
        return DetailResult(failure_reason="防护页或验证码")
    # 只因为站内搜索限流弹窗才没被当成防护页的：除了弹窗，整页（不只前 500 字，导航、页尾、属性上的
    # 字都算）再没有验证、限流这类字，而且正文出自核实过的正文区、是完整的，才放
    with_dialogs = (
        "\n".join(_plain_lines(body[0], keep_select=True)) if body and dialogs else visible
    )
    needs_body = bool(dialogs) and bool(_PROTECTION.search(title + with_dialogs[:500]))
    if needs_body and _PROTECTION.search(_page_words(root, dialogs)):
        return DetailResult(failure_reason="防护页或验证码")
    host = (urlsplit(url).hostname or "").lower()
    node = _body_node(root, host)
    verified = frozenset(root.xpath(_VERIFIED_BODY)) if needs_body else frozenset()
    if needs_body and (
        node is None or not (node in verified or any(a in verified for a in node.iterancestors()))
    ):
        return DetailResult(failure_reason="防护页或验证码")
    if node is None:
        attachments = _attachments(root, url)
        return DetailResult(
            issuer=_issuer([], root, plain[:700]),
            doc_number=_doc_number([], root, plain[:700]),
            publish_date=_publish_date(root, plain[:700]),
            attachments=attachments,
            failure_reason=None if attachments else "未识别到正文容器",
            page_titles=_page_titles(root),
        )
    junk = _SITE_JUNK.get(host)
    lines = _plain_lines(node, (frozenset(node.xpath(junk)) if junk else frozenset()) | dialogs)
    raw_body = "\n".join(lines)
    # 找发文机关、文号、发布日期的范围：页面上正文节点前面的字（最后 700 字）。按节点所在的位置
    # 截，不按正文第一句在整页文字里第一次出现的地方截（标题在上面重复一遍时会截早，漏掉中间的
    # 发布信息）；正文不在页面结构里（从页面自带脚本里取的）时用整页前 700 字
    before = _lines_before(body[0], node, dialogs) if body else None
    prefix = "\n".join(before)[-700:] if before is not None else plain[:700]
    attachments = _attachments(root, url)
    # PDF-only 页面只有下载入口，HTML 正文为空；仍保留附件题录。
    if len(raw_body) < 30 or ("点击正文" in raw_body and not any(len(line) > 60 for line in lines)):
        raw_body = ""
    if needs_body and not (
        len(raw_body) >= _COMPLETE_MIN and "。" in raw_body and not _PROTECTION.search(raw_body)
    ):
        return DetailResult(failure_reason="防护页或验证码")
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
