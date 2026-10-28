"""找通知（问一问第一期）：一句话按规则取条件，交给现有搜索列出通知，不用模型。

依据：问一问开发设计（#328）第六节第 1 条（取条件的规则 0–7 和后面几条）、第五节
/chat/examples、第十二节"回答：找通知"那张表。

- run：取条件、搜，得出存进 ai_chat_turns.answer_json 的回答：条件、总数、列出的通知编号（按当时
  的顺序，最多 10 份）、给不给"在搜索页看全部"、提示代码；不存标题和截止。条件里从原话取出的字
  只有 keywords、docNumber 和 uncovered 里没收录的区县；title 只记比没比中。
- render：打开对话时按存的编号重新取行（_items，截止、核对状态、能不能解读都是最新的），下架的
  不显示、份数记在 hidden；conditionsText、searchLink 现拼、不存（存的时候给了链接、按现在的规则
  表达不了的不给）。
- examples：/chat/examples 的示例，按北京时间每小时算一次。
- parse：第 1–7 条取条件，不查库（收录范围由调用方给）。

排序、行的格式、截止的写法都和搜索页一样（search_statement、_items(grouped, excerpts)）；
条件到搜索参数只有 search_params 一张表，自己搜和"在搜索页看全部"带的参数是同一份。

设计没写死、这里定下的几处：
- 一句里有几个时间词时，按上周、上个月这类（取先写的）、近 7 天、近 30 天、近 90 天、今年、
  往年的顺序取第一个（往年取先写的）；意思不一样的有两个以上（"这周和上周"，"这周"和"近 7 天"
  算一个）时加 times 那句；
- "上周"这类（上星期、上个星期、上一周、上礼拜……，_PERIOD_OF）按发布日期筛上一个星期一到
  星期日，"上个月"这类（上月、上一个月）筛上一个自然月（北京时间）；"上上周""上上个月"再往前
  一个。搜索页没有这几种，同往年不给"在搜索页看全部"；
- "近 N 日""过去 N 天""N 天内"这类和"近 N 天"一样，"这星期""这礼拜""近一个星期"这类和"这周"
  一样算近 7 天，"本季度"同"这个季度"（完整的见 _TIME_WORDS）；
- 第 2 条的细化（设计的表写"国家""国家级"认成国家部委）：后面紧跟着别的名称时（国家高新技术
  企业、国家级专精特新、国家认定企业技术中心）是名称的开头，不当地区，也不当关键词（省里发的这类
  通知，标题多半不写"国家"）；"国家"后面的整段名称以局、委、部、署、办、厅、司结尾的是国家机关
  （国家数据局、国家发展改革委办公厅、国家能源局综合司；"布局、部署、举办、怎么办"这类不算，见
  _NOT_AGENCY），照旧算国家部委，名称当关键词（"国家级"后面不看机关名）；后面的一段以 _AGENCIES
  里的机关名开头的也是（国家发改委专精特新通知）。单独说的也是国家部委：后面是句子结尾、
  不是汉字的（标点、数字、字母；开头的引号、书名号先跳过）、地名、层级、时间词、类型词（"认定"除外：
  国家认定企业技术中心）、问内容的词、说发文的词（发布、出台、发的……）、"和、与、及、或"，或者
  "企业""公司"以外的停用词（国家的、国家最新、国家发的申报通知）。前面是"和、与、及、或"、顿号、
  逗号，再前面是算了的地区或层级的也是（省级和国家级专精特新、广东和国家高新技术企业）；
- 层级（搜索页"层级"那组，和地区取交集）：省级 → 省级，市级 → 市级，区级、县级、区县级 → 区县级，
  省市级、市县级两层都算；省级以上、省级及以上 → 国家部委和省级，市级以上、市级及以上再加市级，
  区级、县级、区县级以上（及以上）各层都要，等于不筛（_LEVEL_WORDS）。单独说的（同上面"国家"
  单独说的那几种，后面也可以是别的层级）或者前面是"和、与、及、或"、顿号、逗号，再前面是算了的
  地区或层级的才算（省级的申报通知、国家级和省级专精特新、国家级、省级、市级专精特新）；紧跟着
  名称时是名称的开头（省级专精特新中小企业、省级以上专精特新企业：省里、市里都转发这类通知），
  去掉、不算，也不当关键词；"副省级"里的不算。各省、各省份、各省区市 → 省级，各市、各地市 → 市级，
  各市县、各市区、各县市区、各市县区 → 市级和区县级，各区县、各县区、各区、各县 → 区县级
  （_SCOPES，一直算；"各市场""各区域"不算）；全国各地、全国、各地、各级不筛，同停用词去掉。
  省级地区名后面紧跟着"级""级以上""级及以上"的（广东省级、北京市级、广东省级以上）按"省级"
  "省级以上"同样看；城市、区县后面的"级"去掉（本来就是那一层），"级以上"同"市级以上""区县级以上"。
  城市、区县和层级一起说时（搜索页的城市只管市级来源、区县只管区县级来源，直接取交集一份都没有）：
  省级 → 所在的省（深圳的省级政策 ＝ 广东 ∩ 省级），市级 → 城市（区县换成所在的城市），区县级 →
  区县（城市换成它收录了的区县：深圳各区；一个都没收录的写进 uncovered："广东·广州（区县级）"，
  区县所在的城市没收录的写"广东·深圳（市级）"）；省级又和市级、区县级一起说的按全省找（深圳的省级
  和市级政策 ＝ 广东 ∩ 省级、市级）。层级里有国家部委时（说了国家部委，或者"省级以上"这类），说了
  别的地区就把国家部委也放进地区，没说就从地区里拿掉（国家和省级 ＝ 层级 国家部委、省级；国家和
  广东的省级、广东省级以上 ＝ 地区 国家部委、广东 ∩ 层级 国家部委、省级，同搜索页 requestScope 的
  拼法），这样国家部委的不会因为取交集被筛掉；
- 去掉的词、名称（见下面机关名、项目名称两条）后面紧跟着的"和、与、及、或"不粘到下一个词上
  （"高新技术企业和专精特新企业"不留"和专精特新"）；地区后面的不去（"四川和邦生物""天津和平区"
  里的"和"是名称的一部分）。词的最后一个字是"和、与、及、或"、后面紧跟着地区、层级或名称的，去掉
  这个字（人工智能和广东、国家数据局和广东），最后两个字是一个词的不去（_JOIN_WORDS：中和、普及、
  参与……：碳中和广东），"以及"两个字一起去掉；
- 机关简称（_SPELLINGS：发改委、工信部、人社局……）：关键词是表里的一种写法时，标题里有同一组的
  任一种写法都算（发改委 ＝ 发改委、发展改革委、发展和改革委员会，写全称的也一样），前面或后面多一个
  字的同样换（省发改委 ＝ 省发展改革委……）；搜索页一个关键词表达不了，不给"在搜索页看全部"。
  连着的字里有 _AGENCIES 里的机关名（表里的各种写法，加科技厅、财政局这类）时在那里切开
  （发改委专精特新 ＝ 发改委、专精特新），切完只剩一个字的并回去（省发改委；"有、和"这类单字不并），
  机关名里的停用词不拆（中小企业局）；
- 项目名称（_PROGRAMS：专精特新、小巨人、单项冠军……）：连着的字里有它们时在那里切开（专精特新小巨人
  ＝ 专精特新、小巨人；标题常写成 专精特新“小巨人”）；
- 用户打的空白：地区、时间、整个标题、文号照样按去掉空白的字认，关键词在打了空白的地方切开
  （"人工智能 补贴" ＝ 人工智能、补贴；位置按 NFKC 以后的字算，"本年度"换了以后跟着挪）；
- 问法的词（怎么、如何、需要、可以、流程、办理、单字"是"……，_ASKS）也当停用词去掉（"怎么申报
  专精特新"不留"怎么"）；
- 机关名后面的"发的"（前一个字是部、厅、局、委、办、署、府、院）也去掉（"工信部发的通知"）；
- 以数字开头的时间词前面紧挨着数字时不认（"17天内"不是"7天内"）；以"周"结尾的时间词后面是
  "期"时不认（"这个周期""上个周期"）；
- "发布、出台、印发、下发、公布、颁布"这几个说发文的词也当停用词去掉（"上周发布的申报通知"）；
- 以"市"开头的区县和城市名连着写、"市"只写一个的（"济南市中区"＝济南市市中区），认成那个
  区县，不拆成"济南市"和关键词"中区"（同第 3 条，只认收录了的区县）；
- 没收录的区县：认出的城市后面紧跟着的一段（中间可以隔一个"的"；停用词、类型词那些切开以后）以
  "区""县"结尾、2–4 个字的，当作这个城市里没收录的区县（城市名以"市"结尾、后面是"中区""南区"
  "北区"的，区县是"市"加这两个字：济南市中区、青岛市南区）：不当关键词，按整个城市找，写进
  uncovered（"山东·济南·市中区"）、加 uncovered 那句。用"和、与、及、或"、顿号、逗号、空白连着的
  几个都算（广州黄埔区和天河区）。"各"开头的、以 _NOT_DISTRICT_ENDS 里的词结尾的（科技园区、
  我家小区、经济特区……）和整个是 _NOT_DISTRICT 里的词的（地区、山区……：南山区、龙湾区这类
  区县名也以它们结尾，只比整个词）不算；
- 认出的省级地区、城市后面紧跟着的"地区"去掉（广东地区的申报通知 ＝ 广东，不留关键词"地区"）；
  别处的照旧当关键词（欠发达地区、西部地区、地区总部：去掉会改了意思）；
- "××××年度"单独算一个关键词（"当标题词"），不和前后的字连成一个；晚于今年、早于 2000 年的
  "××××年"不当时间，也这样单独算一个；
- "问题像在问内容"那几个词（能不能、补贴多少等）也当停用词去掉，不然会留成关键词、一份都找不到；
- "最近一周"和"近一周"同样算近 7 天（否则会先认成"最近"，剩下"一周"当关键词）；
- 文号也认"公告2026年第5号"这种写法（年份后面跟"年"），序号按数比（056 和 56 一样）；
- 有结果才给"在搜索页看全部"（link）；城市和它的区县一起时不给（搜索页打开时区县替换所属城市，
  结果不一样：深圳的市级和区县级）；
- 几个关键词时每个都要命中，条件那句写"标题里同时有 A、B"。

提示代码（notes）和小程序写的话（第十二节；原文在 NOTES 里。{docNumber}、{year} 照 conditions，
{uncovered} 用"、"连起来，{next_year} 是 year + 1；［…］是按钮）：

- title：按标题找到的。
- doc_number_none：收录里没有文号是"{docNumber}"的通知。文号可能抄错了，也可能我们没收录；
  可以输入标题里的几个词再找。
- uncovered：收录里没有{uncovered}的来源，查不到那里的通知。［收录范围 ›］（一个收录了的地区都
  没有时不搜，只有这一句；还有收录了的，就只搜收录了的；没收录的区县按整个城市找；城市一个区县
  都没收录又说了区县级的写"广东·广州（区县级）"。有层级时国家部委算层级，没收录也照样搜别的层级）
- year：按发布日期找的 {year} 年发布的通知；"{year} 年度"的项目可能在 {next_year} 年才发通知，
  找不到可以改成"今年"再找。
- times：写了几个时间，只按条件里"发布"那一项找的；要按别的时间找，只写那一个再找。
- first_three：只按前 3 个词找的。
- more：这里列最新 10 份。（link 为真时接［在搜索页看全部 ›］，为假时同时有 narrow）
- narrow：可以加上地区、年份或更具体的词缩小范围。
- empty：按这些条件没找到。可以少写几个词，或去掉地区、时间再找；也可能这个地方还没收录，
  看"说明"里的收录范围。
- spaces：词连着写时，标题里要一字不差才找得到；可以用空格把几个词隔开再找，比如"低空经济 补贴"。
  （没找到、又有一个关键词有 5 个以上汉字时，接在 empty 后面；机关名不算）
- title_none：没有标题完全一样的通知，按条件也没找到。可以只输入标题里的几个词再找。
- content：找通知只帮你找到通知。想知道某份通知里的截止、材料、条件怎么写的，点那一行的
  "问这份通知"，AI 从原文里摘出相关的句子。
- photo：请输入通知标题或文号，帮你找原文。
- vague：没认出要找什么（一个条件都没有）：不去搜，请用户写上标题里的词或地区、类型、时间。
"""

from __future__ import annotations

import itertools
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
from youju.regions import (
    city_key,
    covered_city_scopes,
    covered_district_scopes,
    resolve_city,
    resolve_district,
)

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
    "times": "写了几个时间，只按条件里“发布”那一项找的；要按别的时间找，只写那一个再找。",
    "first_three": "只按前 3 个词找的。",
    "more": "这里列最新 10 份。",
    "narrow": "可以加上地区、年份或更具体的词缩小范围。",
    "empty": (
        "按这些条件没找到。可以少写几个词，或去掉地区、时间再找；也可能这个地方还没收录，"
        "看“说明”里的收录范围。"
    ),
    "spaces": (
        "词连着写时，标题里要一字不差才找得到；可以用空格把几个词隔开再找，比如“低空经济 补贴”。"
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
# 国家机关名的结尾（"国家"后面的整段名称以这些结尾）
_AGENCY_ENDS = ("局", "委", "部", "署", "办", "厅", "司", "委员会", "办公室")
# 以这几个字结尾、又不是机关名的词
_NOT_AGENCY = (
    "布局", "格局", "大局", "全局", "结局", "开局", "部署", "签署", "总部", "内部", "外部", "全部",
    "局部", "干部", "东部", "西部", "南部", "北部", "中部", "本部", "俱乐部", "怎么办", "举办",
    "主办", "承办", "协办", "创办", "开办", "合办", "代办", "大厅", "展厅", "公司",
)  # fmt: skip

# 层级：取值和搜索页的 levels 一样（v1.LEVELS），按这个顺序写
LEVEL_ORDER = (NATIONAL, "省级", "市级", "区县级")
# 单独说、或用"和、与、及、或"、顿号跟在地区、层级后面才算；紧跟着名称时是名称的开头（见 _areas）。
# "县级以上"这类各层都要，等于不筛
_LEVEL_WORDS = {
    "省级": ("省级",),
    "市级": ("市级",),
    "区级": ("区县级",),
    "县级": ("区县级",),
    "区县级": ("区县级",),
    "省市级": ("省级", "市级"),
    "市县级": ("市级", "区县级"),
    **dict.fromkeys(("省级以上", "省级及以上"), (NATIONAL, "省级")),
    **dict.fromkeys(("市级以上", "市级及以上"), (NATIONAL, "省级", "市级")),
    **dict.fromkeys(
        ("区级以上", "区级及以上", "县级以上", "县级及以上", "区县级以上", "区县级及以上"),
        LEVEL_ORDER,
    ),
}
# 说的就是那一层，一直算
_SCOPES = {
    **dict.fromkeys(("各省", "各省份", "各省区市"), ("省级",)),
    **dict.fromkeys(("各市", "各地市"), ("市级",)),
    **dict.fromkeys(("各市县", "各市区", "各县市区", "各市县区"), ("市级", "区县级")),
    **dict.fromkeys(("各区县", "各县区", "各区", "各县"), ("区县级",)),
}
_NOT_SCOPE = {"各市": "场", "各区": "域"}  # 后面是这个字的不算（各市场主体、各区域）
_ANYWHERE = ("全国各地", "全国", "各地", "各级")  # 不筛，同停用词去掉
_LISTED = (*_JOINS, "、", ",")  # 连着说几个地区、层级（"，"NFKC 以后是","）

# 机关简称和标题里的写法：同一组任一种写法都算（见 _alternates）
_SPELLINGS = (
    ("发改委", "发展改革委", "发展和改革委员会"),
    ("发改局", "发展改革局", "发展和改革局"),
    ("工信部", "工业和信息化部"),
    ("工信厅", "工业和信息化厅"),
    ("工信局", "工业和信息化局"),
    ("经信委", "经济和信息化委员会"),
    ("经信厅", "经济和信息化厅"),
    ("经信局", "经济和信息化局"),
    ("人社部", "人力资源社会保障部", "人力资源和社会保障部"),
    ("人社厅", "人力资源社会保障厅", "人力资源和社会保障厅"),
    ("人社局", "人力资源社会保障局", "人力资源和社会保障局"),
    ("市监局", "市场监管局", "市场监督管理局"),
    ("市场监管总局", "市场监督管理总局"),
    ("卫健委", "卫生健康委", "卫生健康委员会"),
    ("住建部", "住房城乡建设部", "住房和城乡建设部"),
    ("住建厅", "住房城乡建设厅", "住房和城乡建设厅"),
    ("住建局", "住房城乡建设局", "住房和城乡建设局"),
    ("文旅部", "文化和旅游部"),
    ("文旅厅", "文化和旅游厅"),
    ("文旅局", "文化和旅游局"),
    ("交通部", "交通运输部"),
    ("交通厅", "交通运输厅"),
    ("交通局", "交通运输局"),
    ("国税总局", "税务总局"),
)
_SPELLING_OF = {word: group for group in _SPELLINGS for word in group}
# 机关名：在这些名称那里切开（"发改委专精特新"），"国家"后面以它们开头的算国家部委
_AGENCIES = (
    *_SPELLING_OF,
    "科技部", "科技厅", "科技局", "财政部", "财政厅", "财政局", "商务部", "商务厅", "商务局",
    "国资委", "网信办", "知识产权局", "统计局", "中小企业局",
)  # fmt: skip
# 项目名称：标题常给它们加引号（专精特新“小巨人”），连着写的在这里切开
_PROGRAMS = (
    "专精特新",
    "小巨人",
    "单项冠军",
    "隐形冠军",
    "瞪羚",
    "独角兽",
    "揭榜挂帅",
    "链主",
    "雏鹰",
)
_NAMES = frozenset((*_AGENCIES, *_PROGRAMS))
# 词最后的"和、与、及、或"后面紧跟着地区、层级或名称时去掉，最后两个字是这些词的不去（碳中和广东）
_JOIN_WORDS = (
    "中和", "共和", "温和", "缓和", "调和", "平和", "饱和", "祥和", "柔和", "涉及", "惠及", "普及",
    "波及", "危及", "顾及", "参与", "给与",
)  # fmt: skip
# 紧跟在城市后面、以"区""县"结尾又不是区县的（见 _districts）：以这些结尾的（科技园区、我家小区）
_NOT_DISTRICT_ENDS = (
    "园区", "社区", "小区", "景区", "片区", "特区", "大湾区", "示范区", "试验区", "先行区",
    "自贸区", "保税区",
)  # fmt: skip
# 整个词是这些的（南山区、龙湾区、江城区这类区县名也以"山区""湾区""城区"结尾，只比整个词）
_NOT_DISTRICT = ("地区", "城区", "市区", "郊区", "辖区", "山区", "湾区", "区县", "县区")


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
        ("上上周", "上上个周", "上上一周", "上上星期", "上上个星期", "上上一星期", "上上一个星期")
        + ("上上礼拜", "上上个礼拜"),
        ("week", 2),
    ),
    **dict.fromkeys(("上个月", "上月", "上一个月"), ("month", 1)),
    **dict.fromkeys(("上上个月", "上上月", "上上一个月"), ("month", 2)),
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
# 问法的词：也当停用词去掉（"怎么申报专精特新""企业可以申请哪些补贴"）
_ASKS = (
    "怎么", "怎样", "如何", "哪里", "在哪里", "在哪", "需要", "可以", "开始", "结束", "流程",
    "办理", "是",
)  # fmt: skip
# 切完剩一个字时只和这几个停用词并成一个词（"新材料""大企业"），见 _pieces
_GLUE = ("材料", "企业", "公司", "名单")
_NO_GLUE = "有为给对让帮在是要能会可把被向从和与及或"  # 这些单字不和后面的词并（"有企业补贴"）
# 以"和"开头的县级地名：去掉的词后面紧跟着它们时，"和"不去（"找和平区的申报通知"）
_HE_PLACES = (
    "和平",
    "和县",
    "和田",
    "和龙",
    "和顺",
    "和政",
    "和静",
    "和硕",
    "和林格尔",
    "和布克赛尔",
)
# 文号前面不算机关简称的字（"帮我找一下文号是……"）。地名不在里面："国家税务总局公告"的"国家"、
# 机关简称开头的省名都是简称的一部分，去掉了就会拿别的机关的文号顶替
_NUMBER_LEAD = ("文号", "发文字号", "字号", "编号", "是", "为")


def _alternation(words) -> re.Pattern:
    """几个词任一个；长的在前，"最近7天"不会先认成"最近"。"""
    return re.compile("|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True)))


_NUMERALS = "0123456789零一二两三四五六七八九十百"


def _time_word(word: str) -> str:
    """一个时间词的正则：以数字开头的前面不能紧挨着数字（"17天内"不认成"7天内"），以"周"结尾的
    后面不能是"期"（"这个周期""上个周期"不是时间）。"""
    pattern = re.escape(word)
    if word[0] in _NUMERALS:
        pattern = f"(?<![{_NUMERALS}]){pattern}"
    if word.endswith("周"):
        pattern += "(?!期)"
    return pattern


_TIME = re.compile(
    "|".join(_time_word(w) for w in sorted({*_TIME_OF, *_PAST, *_PERIOD_OF}, key=len, reverse=True))
)
# 切开的地方：要去掉的词（类型词、停用词……；机关名后面的"发的"也去掉："工信部发的通知"，"开发的"
# "研发的"不算），和留着的名称（_NAMES），见 _pieces
_CUTS = re.compile(
    _alternation([*TYPE_WORDS, *STOPWORDS, *_CONTENT, *_ISSUE, *_ASKS, *_ANYWHERE, *_NAMES]).pattern
    + "|(?<=[部厅局委办署府院])发的"
)
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
            *_ASKS,
        },
        key=len,
        reverse=True,
    )
)


# "国家""国家级"、层级单独说时后面跟的词，地名、层级另外看。"企业""公司""认定"不算（国家企业
# 技术中心、国家认定企业技术中心）
_ALONE_AFTER = tuple(
    {
        *STOPWORDS,
        *TYPE_WORDS,
        *_CONTENT,
        *_ISSUE,
        *_ASKS,
        *_ANYWHERE,
        "发的",
        *_TIME_OF,
        *_PAST,
        *_PERIOD_OF,
        *_JOINS,
    }
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
    times: bool = False  # 写了几个意思不一样的时间，只按一个找


@dataclass(frozen=True)
class _Found:
    """_areas 认出来的：条件里的地区和层级，和后面切关键词要用的位置。"""

    conditions: dict[str, Any]
    starts: frozenset[int]  # 取走的地区、层级、名称开头从哪里开始（前面的"和"不粘到关键词上）
    # 城市名后面那个字的位置 →（这个城市, 城市名以"市"结尾）
    after_city: dict[int, tuple[list[tuple[str, str]], bool]]


def normalize(text: str | None) -> str:
    """先 NFKC（全角转半角），再去掉所有空白。"""
    return "".join(unicodedata.normalize("NFKC", text or "").split())


def _spaced(text: str | None, today: date) -> tuple[str, frozenset[int]]:
    """normalize 以后、"本年度"换成"今年的年份＋年度"的字，和用户打了空白的地方（空白后面那个字在
    这串字里的位置；"本年度"中间的不算，后面的跟着挪）。"""
    parts = unicodedata.normalize("NFKC", text or "").split()
    joined = "".join(parts)
    new = f"{today.year}年度"
    starts = [m.start() for m in re.finditer("本年度", joined)]
    spaces = frozenset(
        at + sum(len(new) - 3 for start in starts if start + 3 <= at)
        for at in itertools.accumulate(len(part) for part in parts[:-1])
        if not any(start < at < start + 3 for start in starts)
    )
    return joined.replace("本年度", new), spaces


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
        "levels": [],  # 层级（同搜索页 levels），按 LEVEL_ORDER 的顺序
        "uncovered": [],  # 认出来但没收录的省级地区，和收录了的城市里没收录的区县（"省·市·区县"）
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


def _join_tail(word: str) -> str:
    """词最后的"和、与、及、或"去掉（调用方看过后面紧跟着地区、层级或名称），最后两个字是一个词的
    不去（_JOIN_WORDS：碳中和），"以及"两个字一起去掉。"""
    if word.endswith("以及"):
        return word[:-2]
    if word.endswith(_JOINS) and not word.endswith(_JOIN_WORDS):
        return word[:-1]
    return word


def _pieces(run: str) -> list[tuple[int, str, bool]]:
    """把一串连着的字按类型词、停用词、问内容的词切开：（位置, 字, 是不是要去掉的词）。
    切完剩一个字、又紧挨着"材料、企业、公司"（_GLUE）的，和它并成一个词，不去掉（"新材料"不拆成
    "新"和停用词"材料"；"大企业"不拆成"大"和"企业"）。别的停用词不并（"有什么政策"不留
    "有什么"），"有、为、给"这类单字也不并（_NO_GLUE："有企业补贴"不留"有企业"，"企业有什么补贴"不留
    "企业有"）。机关名、项目名称（_NAMES）留着、在那里切开（"发改委专精特新"）；机关名前后只剩一个字的
    并回去（"省发改委"，_NO_GLUE 里的不并）。去掉的词、名称后面紧跟着的"和、与、及、或"不粘到下一个
    词上（"企业和专精特新"），名称前面的去掉（"高新技术和专精特新"，见 _join_tail）。"""
    raw: list[tuple[int, str, str]] = []  # （位置, 字, drop 去掉的词 / name 名称 / word 别的字）
    pos = 0
    for m in _CUTS.finditer(run):
        if m.start() > pos:
            raw.append((pos, run[pos : m.start()], "word"))
        raw.append((m.start(), m.group(), "name" if m.group() in _NAMES else "drop"))
        pos = m.end()
    if pos < len(run):
        raw.append((pos, run[pos:], "word"))
    out: list[tuple[int, str, str]] = []
    i = 0
    while i < len(raw):
        at, text, kind = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        if kind == "word" and len(text) == 1 and text not in _NO_GLUE:
            if nxt and nxt[2] == "drop" and nxt[1] in _GLUE:
                out.append((at, text + nxt[1], "word"))
                i += 2
                continue
            if out and out[-1][2] == "drop" and out[-1][1] in _GLUE:
                prev = out.pop()
                out.append((prev[0], prev[1] + text, "word"))
                i += 1
                continue
            if nxt and nxt[1] in _AGENCIES:  # "省发改委"
                out.append((at, text + nxt[1], "name"))
                i += 2
                continue
            if out and out[-1][1] in _AGENCIES:  # "科技局长"
                prev = out.pop()
                out.append((prev[0], prev[1] + text, "name"))
                i += 1
                continue
        if (
            kind == "word"
            and out
            and out[-1][2] != "word"
            and text[0] in _JOINS
            and len(text) >= 3
            and not text.startswith(_HE_PLACES)
        ):
            at, text = at + 1, text[1:]  # "企业和专精特新"：去掉的词后面的"和"不粘到下一个词上
        if kind == "word" and nxt and nxt[2] == "name":
            text = _join_tail(text)  # "高新技术和专精特新"
        if text:
            out.append((at, text, kind))
        i += 1
    return [(at, text, kind == "drop") for at, text, kind in out]


def _alone(rest: str, places: tuple[str, ...]) -> bool:
    """单独说的（rest 是后面的字；开头的引号、书名号先跳过）：后面是句子结尾、不是汉字的、地名或
    层级（places），或 _ALONE_AFTER 里的词。"""
    rest = rest.lstrip(_OPENERS)
    return (
        not rest
        or not _HAN_CHAR.match(rest[0])
        or rest.startswith(_ALONE_AFTER)
        or rest.startswith(places)
    )


def _national(rest: str, places: tuple[str, ...], *, agency: bool) -> bool:
    """看"国家""国家级"是不是说国家部委（rest 是它后面的字；开头的引号、书名号先跳过）：
    - 单独说的（_alone）；
    - 后面紧跟着国家机关名（agency，只看"国家"）：以 _AGENCIES 里的机关名开头（国家发改委
      专精特新），或者连着的汉字（到 _NAME_END 里的词、地名或"和＋地名"为止）整段以 _AGENCY_ENDS
      结尾、又不以 _NOT_AGENCY 里的词结尾（国家数据局、国家能源局综合司）。
    别的是名称的开头（国家高新技术企业、国家级专精特新）。"""
    if _alone(rest, places):
        return True
    if not agency:
        return False
    rest = rest.lstrip(_OPENERS)
    if rest.startswith(_AGENCIES):
        return True
    end = 0
    while end < len(rest) and _HAN_CHAR.match(rest[end]):
        if rest.startswith(_NAME_END, end) or rest.startswith(places, end):
            break
        if rest[end] in _JOINS and rest.startswith(places, end + 1):  # "国家数据局和广东"
            break
        end += 1
    name = rest[:end]
    return len(name) >= 2 and name.endswith(_AGENCY_ENDS) and not name.endswith(_NOT_AGENCY)


def _area_words(table: dict[str, list[tuple[str, ...]]]) -> re.Pattern:
    """地名、层级、"各省"这类任一个，长的在前。"各省"这类后面是"级"的不算（各省级＝各＋省级），
    "副省级"里的"省级"不算。"""

    def one(word: str) -> str:
        pattern = re.escape(word)
        if word in _SCOPES:
            pattern += f"(?![级{_NOT_SCOPE.get(word, '')}])"
        elif word.startswith("省级"):
            pattern = "(?<!副)" + pattern
        return pattern

    words = sorted({*table, *_LEVEL_WORDS, *_SCOPES}, key=len, reverse=True)
    return re.compile("|".join(one(word) for word in words))


def _areas(text: str, marks: list[str], covered: Coverage) -> _Found:
    """第 2–4 条和层级。几个地区取并集；同名的市、区县按句子里写到的省、市缩小，缩小不了就都算。
    具体城市、区县替换同省的全省条件，具体区县替换所属城市（同搜索页 withoutCityProvinces、
    withoutDistrictCities，这样"在搜索页看全部"打开的条件和这里一样）。层级怎么算见模块说明。"""
    table = _area_table(covered)
    provinces: list[str] = []
    city_hits: list[list[tuple[str, ...]]] = []
    # 城市名后面那个字的位置 →（city_hits 里第几个, 城市名以"市"结尾）
    city_ends: dict[int, tuple[int, bool]] = {}
    district_hits: list[list[tuple[str, ...]]] = []
    levels: set[str] = set()
    counted: set[int] = set()  # 算了的地区、层级在哪里结束（看"和"连着说的）
    starts: set[int] = set()
    places = (*table, *_LEVEL_WORDS, *_SCOPES)
    for m in _area_words(table).finditer(text):
        start, end, word = m.start(), m.end(), m.group()
        _take(marks, start, end)
        starts.add(start)
        joined = start >= 2 and text[start - 1] in _LISTED and start - 1 in counted
        if word in _SCOPES or word in _LEVEL_WORDS:
            if word in _SCOPES or joined or _alone(text[end:], places):
                levels.update(_SCOPES.get(word) or _LEVEL_WORDS[word])
                counted.add(end)
            continue  # 紧跟着名称的（"省级专精特新"）：名称的开头，不算，也不当关键词
        if word in _NAME_STARTS and not (
            joined or _national(text[end:], places, agency=word == "国家")
        ):
            continue  # 名称的开头（"国家高新技术企业"）：不当地区，也不当关键词
        meanings = table[word]
        kinds = {meaning[0] for meaning in meanings}
        # "广东省级""广东省级以上"按"省级""省级以上"看；城市、区县后面的"级"本来就是那一层，
        # "以上"同"市级以上""区县级以上"
        tail = next((t for t in ("级及以上", "级以上", "级") if text.startswith(t, end)), "")
        if tail:
            _take(marks, end, end + len(tail))
            if "province" in kinds:
                level = "" if meanings[0][1] == NATIONAL else "省" + tail
            elif tail == "级":
                level = ""
            else:
                level = ("市" if "city" in kinds else "区县") + tail
            if level and _alone(text[end + len(tail) :], places):
                levels.update(_LEVEL_WORDS[level])
            end += len(tail)
        elif (
            text.startswith("地区", end)
            and kinds & {"province", "city"}
            and meanings[0][1] != NATIONAL
        ):
            _take(marks, end, end + 2)  # "广东地区""深圳地区"就是广东、深圳
            end += 2
        counted.add(end)
        if "province" in kinds:
            provinces.append(meanings[0][1])
        elif "city" in kinds:
            city_ends[end] = (len(city_hits), word.endswith("市"))
            if text.startswith("的", end):  # "广州的天河区"同"广州天河区"
                city_ends[end + 1] = city_ends[end]
            city_hits.append([meaning[1:] for meaning in meanings if meaning[0] == "city"])
        else:
            district_hits.append([meaning[1:] for meaning in meanings])
    named = set(provinces)
    city_hits = [[c for c in hit if c[0] in named] or hit for hit in city_hits]
    cities: list[tuple[str, ...]] = [c for hit in city_hits for c in hit]
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
    regions = [p for p in named_order if p in covered.provinces and p not in parents]
    uncovered = [p for p in named_order if p not in covered.provinces]
    districts = list(dict.fromkeys(districts))
    picked = [level for level in LEVEL_ORDER if level in levels]
    if picked and NATIONAL in named_order and NATIONAL not in picked:
        picked.insert(0, NATIONAL)  # 有层级又说了国家部委：国家部委也是一层
    local = [level for level in picked if level != NATIONAL]
    if local and (cities or districts):
        regions, cities, districts, missing = _by_level(regions, cities, districts, local, covered)
        uncovered += missing
    if NATIONAL in picked:
        if NATIONAL not in covered.provinces:
            picked.remove(NATIONAL)
            if NATIONAL not in uncovered:
                uncovered.append(NATIONAL)
        elif named - {NATIONAL} or city_hits or district_hits:
            # (国家部委 ∪ 地区) ∩ (国家部委 ∪ 层级)，不然国家部委的取交集就筛掉了
            regions = regions if NATIONAL in regions else [NATIONAL, *regions]
        else:
            regions = [p for p in regions if p != NATIONAL]  # 国家和省级 ＝ 层级 国家部委、省级
    if set(picked) >= set(LEVEL_ORDER):
        picked = []  # "县级以上"：各层都要，不筛
    return _Found(
        conditions={
            "regions": regions,
            "cities": [f"{p}/{c}" for p, c in cities],
            "districts": [f"{p}/{c}/{d}" for p, c, d in districts],
            "levels": picked,
            "uncovered": uncovered,
        },
        starts=frozenset(starts),
        after_city={end: (city_hits[i], shi) for end, (i, shi) in city_ends.items()},
    )


def _city_districts(province: str, city: str, covered: Coverage) -> list[tuple[str, str, str]]:
    """这个城市里收录了的区县（省, 市, 区县），写法同 _area_table。"""
    out = []
    for p, c, d in sorted(covered.districts):
        resolved = resolve_district(p, c, d, covered.districts)
        if p == province and resolved and city_key(resolved[0]) == city_key(city) and _plain(d):
            out.append((p, *resolved))
    return list(dict.fromkeys(out))


def _by_level(
    regions: list[str],
    cities: list[tuple[str, ...]],
    districts: list[tuple[str, ...]],
    local: list[str],
    covered: Coverage,
) -> tuple[list[str], list[tuple[str, ...]], list[tuple[str, ...]], list[str]]:
    """城市、区县和省级、市级、区县级一起说时换成搜索页能取交集的条件（搜索页的城市只管市级来源、
    区县只管区县级来源，直接取交集一份都没有）：省级 → 所在的省；市级 → 城市（区县换成所在的
    城市）；区县级 → 区县（城市换成它收录了的区县）。省级又和市级、区县级一起说的按全省找（城市、
    区县换成所在的省）。换不出来的（城市没收录区县、区县所在的城市没收录）写进返回的最后一项，
    如"广东·广州（区县级）"。"""
    whole = "省级" in local and len(local) > 1
    regions, out_cities, out_districts, missing = list(regions), [], [], []
    for p, c in cities:
        if "省级" in local:
            regions.append(p)
        if whole:
            continue
        if "市级" in local:
            out_cities.append((p, c))
        if "区县级" in local:
            found = _city_districts(p, c, covered)
            out_districts += found
            missing += [] if found else [f"{_area_label(f'{p}/{c}')}（区县级）"]
    for p, c, d in districts:
        if "省级" in local:
            regions.append(p)
        if whole:
            continue
        if "市级" in local:
            city = resolve_city(p, c, covered.cities)
            out_cities += [(p, city)] if city else []
            missing += [] if city else [f"{_area_label(f'{p}/{c}')}（市级）"]
        if "区县级" in local:
            out_districts.append((p, c, d))
    return (
        list(dict.fromkeys(regions)),
        list(dict.fromkeys(out_cities)),
        list(dict.fromkeys(out_districts)),
        list(dict.fromkeys(missing)),
    )


_DISTRICT_HEAD = re.compile(f"[{_HAN}]{{1,3}}?[区县]")  # 最短的、以"区""县"结尾的开头


def _districts(piece: str, after: tuple[list[tuple[str, str]], bool] | None) -> list[str]:
    """紧跟在认出的城市后面、像区县名的一段（这个城市里没收录的区县）：以"区""县"结尾、2–4 个字，
    不是"各"开头，不以 _NOT_DISTRICT_ENDS 里的词结尾、也不是 _NOT_DISTRICT 里的词；用"和、与、
    及、或"连着的几个都算（黄埔区和天河区）。城市名以"市"结尾、第一个是"中区、南区、北区"的，
    前面补上"市"（济南市中区、青岛市南区）。after 是前面的城市（_Found.after_city）。不像的
    返回空的。"""
    names: list[str] = []
    rest = piece
    while after and rest:
        m = _DISTRICT_HEAD.match(rest)
        name = m.group() if m else ""
        tail = rest[len(name) :]
        if (
            not name
            or not 2 <= len(name) <= 4
            or name.startswith("各")
            or name.endswith(_NOT_DISTRICT_ENDS)
            or name in _NOT_DISTRICT
            or (tail and tail[0] not in _JOINS)
        ):
            return []
        if not names and after[1] and name in ("中区", "南区", "北区"):
            name = "市" + name
        names.append(name)
        rest = tail[1:]
    return names


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
    """第 1–7 条：从一句话里取条件。没认出来的字当关键词，不另外猜。text 是用户打的原话（用户打的
    空白要用来切关键词）。"""
    text, spaces = _spaced(text, today)
    conditions = empty_conditions()
    content = _has(text, _CONTENT)
    numbers = doc_numbers(text)
    if numbers:  # 只按文号找
        agency, year, seq, shown = numbers[0]
        conditions["docNumber"] = shown
        return Parsed(conditions, (agency, year, seq), content, False)

    marks = list(text)
    found = _areas(text, marks, covered)
    conditions |= found.conditions

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
    # 意思不一样的时间（"这周"和"近 7 天"算一个）
    times = len({*(p for _, p in periods), *(y for _, y in past), *windows}) > 1
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
    uncovered: list[str] = []
    # 没收录的区县后面用顿号、逗号、空白隔开的下一段，也按同一个城市看（黄埔区、天河区）
    listed: dict[int, list[tuple[str, str]]] = {}
    for run in _WORD.finditer("".join(marks)):
        cuts = [run.start(), *sorted(p for p in spaces if run.start() < p < run.end()), run.end()]
        for begin, end in itertools.pairwise(cuts):  # 用户打了空白的地方切开
            for at, piece, drop in _pieces(run.string[begin:end]):
                if drop:
                    content = content or piece in _CONTENT
                    continue
                at += begin
                if at + len(piece) in found.starts:  # "人工智能和广东"
                    piece = _join_tail(piece)
                after = found.after_city.get(at)
                if after is None and at in listed:
                    after = (listed[at], False)
                    if piece[:1] in _JOINS:  # "黄埔区 和天河区"
                        at, piece = at + 1, piece[1:]
                names = _districts(piece, after)
                if after and names:  # 收录了的城市里没收录的区县：按整个城市找
                    uncovered += [
                        _area_label(f"{p}/{c}/{name}") for name in names for p, c in after[0]
                    ]
                    stop = at + len(piece)
                    if stop in spaces or text.startswith(("、", ","), stop):
                        listed[stop] = listed[stop + 1] = after[0]
                    continue
                tokens.append((at, piece))
    conditions["uncovered"] = list(dict.fromkeys(conditions["uncovered"] + uncovered))
    words = list(dict.fromkeys(t[:KEYWORD_CHARS] for _, t in sorted(tokens) if len(t) >= 2))
    conditions["keywords"] = words[:KEYWORDS_MAX]
    return Parsed(conditions, None, content, len(words) > KEYWORDS_MAX, times)


# ---- 搜 ----


def _alternates(keyword: str) -> list[str]:
    """关键词的别的写法（_SPELLINGS）：整个词是表里的一种写法，或前面、后面多一个字（"省发改委"
    ＝ 省发展改革委、省发展和改革委员会）。没有的是空的。"""
    for head, body, tail in (
        ("", keyword, ""),
        (keyword[:1], keyword[1:], ""),
        ("", keyword[:-1], keyword[-1:]),
    ):
        group = _SPELLING_OF.get(body)
        if group:
            return [head + word + tail for word in group if word != body]
    return []


def search_params(conditions: dict[str, Any]) -> dict[str, str]:
    """条件 → 搜索参数（search_statement 的参数和"在搜索页看全部"的链接共用这一张表）。
    kind、time 一直带上：搜索页默认是近 30 天。只有一个关键词、又没有别的写法时才有 q（多个的、
    有别的写法的另外取交集）。"""
    params = {"kind": conditions["kind"], "time": conditions["time"]}
    keywords = conditions["keywords"]
    if len(keywords) == 1 and not _alternates(keywords[0]):
        params["q"] = keywords[0]
    for key in ("regions", "cities", "districts", "levels"):
        if conditions.get(key):
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
        levels=params.get("levels"),
        cities=params.get("cities"),
        districts=params.get("districts"),
    )
    if "q" not in params:  # 每个关键词都要命中标题或文号；有别的写法的命中一种就行
        for keyword in conditions["keywords"]:
            spellings = [keyword, *_alternates(keyword)]
            stmt = stmt.where(
                or_(*(Document.id.in_(_keyword_ids(session, word)) for word in spellings))
            )
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
    1 个、不超过 50 字、没有别的写法（层级搜索页有，照样给），也没有城市和它的区县一起（搜索页
    打开时区县替换所属城市，见 withoutDistrictCities："深圳的市级和区县级"）。"""
    keywords = conditions["keywords"]
    cities = {(p, city_key(c)) for p, c in (key.split("/") for key in conditions["cities"])}
    nested = any(
        (p, city_key(c)) in cities
        for p, c, _ in (key.split("/") for key in conditions["districts"])
    )
    return (
        not conditions["title"]
        and not conditions["docNumber"]
        and conditions["year"] is None
        and conditions["period"] is None
        and conditions["time"] in TIMES
        and len(keywords) <= 1
        and all(len(k) <= SEARCH_Q_CHARS and not _alternates(k) for k in keywords)
        and not nested
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
    parsed = parse(text, today=today, covered=coverage(session))  # 原话：用户打的空白要用
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
    if _blocked(conditions):
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
    if parsed.times:
        notes.append("times")
    if parsed.dropped:
        notes.append("first_three")
    if not total:
        notes.append("title_none" if title_like else "empty")
        if not title_like and any(_glued(k) for k in conditions["keywords"]):
            notes.append("spaces")
    return _answer(conditions, total, shown, notes + tail(total, link), link)


def _glued(keyword: str) -> bool:
    """像几个词连着写的：5 个以上汉字，又不是机关名（有别的写法的、_NAMES 里的）。"""
    return (
        len(_HAN_CHAR.findall(keyword)) >= 5 and keyword not in _NAMES and not _alternates(keyword)
    )


def _nothing(conditions: dict[str, Any]) -> bool:
    """没有关键词、地区、层级、往年、上周或上个月，时间不限，类型不限：等于什么条件都没有。"""
    return (
        not conditions["keywords"]
        and not _has_area(conditions)
        and not conditions["uncovered"]
        and not conditions.get("levels")
        and conditions["year"] is None
        and conditions["period"] is None
        and conditions["time"] == "不限"
        and conditions["kind"] == "all"
    )


def _has_area(conditions: dict[str, Any]) -> bool:
    return bool(conditions["regions"] or conditions["cities"] or conditions["districts"])


def _blocked(conditions: dict[str, Any]) -> bool:
    """写到的地区都没收录：不去搜。有层级时国家部委算层级，没收录也照样搜别的层级。"""
    levels = conditions.get("levels")
    named = [u for u in conditions["uncovered"] if not (levels and u == NATIONAL)]
    return bool(named) and not _has_area(conditions)


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


def _shown(keyword: str) -> str:
    """关键词的写法：有别的写法的写成"发改委（或 发展改革委、发展和改革委员会）"。"""
    others = _alternates(keyword)
    return f"{keyword}（或 {'、'.join(others)}）" if others else keyword


def conditions_text(stored: dict[str, Any]) -> str:
    """条件那句（小程序写在"按这些条件找的："后面），如"地区 广东 · 层级 省级 · 类型 申报通知 ·
    发布 今年 · 标题里有 专精特新"（没有层级的不写那一项；几个关键词要都有，写"标题里同时有
    A、B"）。没去搜的（拍照、地区都没收录）和整个标题比中的为空串。"""
    conditions = stored["conditions"]
    if conditions["title"] or "photo" in stored["notes"] or _blocked(conditions):
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
    parts = [f"地区 {'、'.join(areas) if areas else '不限'}"]
    if conditions.get("levels"):
        parts.append(f"层级 {'、'.join(conditions['levels'])}")
    parts += [f"类型 {KIND_LABELS[conditions['kind']]}", published]
    keywords = conditions["keywords"]
    if keywords:
        lead = "标题里同时有" if len(keywords) > 1 else "标题里有"
        parts.append(f"{lead} {'、'.join(_shown(k) for k in keywords)}")
    return " · ".join(parts)


def render(session: Session, stored: dict[str, Any], *, today: date) -> dict[str, Any]:
    """按存的编号重新取行给小程序：截止、核对状态、能不能解读都按现在的；下架、隐藏的不显示，
    份数记在 hidden（"有 N 份已下架，不再显示"）。total 是当时的总数。today 和 run 对应，
    行里的截止照搜索页按 v1.now() 算。缺了的项按空的算（调用方没有回答时传 {}）。存的时候给了
    链接、现在的规则表达不了的（比如关键词后来成了机关简称）不给。"""
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
        "searchLink": (
            search_params(conditions) if stored.get("link") and _expressible(conditions) else None
        ),
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
