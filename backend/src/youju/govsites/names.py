"""按名称认部门和栏目：哪些链接是目标部门的网站（或门户里的部门频道），哪些是目标栏目。

目标部门（创始人 11/5 定的范围，各级都一样）：政府办公厅（政府文件）、发改、工信（经信）、科技、
商务、
财政、人社、市场监管（含知识产权）、农业农村、文旅、生态环境、数据局，以及国家级、省级开发区和
高新区管委会。县级常把几个部门合在一起（"科学技术和工业信息化局"），一个部门可以同时算几类。
"""

import re

# (部门简称, 匹配部门名称的正则)。简称和 sources.dept 现有写法一致（发改委、工信、科技……）
DEPT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("政府办", re.compile(r"人民政府办公[厅室]|政府办公[厅室]|^政府办$|^[省市县区]政府办$")),
    ("发改委", re.compile(r"发展和改革|发展改革|发改")),
    (
        "工信",
        re.compile(
            r"工业和信息化|工业信息化|经济和信息化|经济信息化|工业和科技|工信|经信|工业和商务|"
            r"科技和工业|科学技术和工业|工业科技|科工信|工科|经济科技信息化|经济商务和信息化|经济商务信息化|"
            r"经济发展和信息化"
        ),
    ),
    ("科技", re.compile(r"科学技术|科技(?!馆|园|城|大学|学院|中心|报|馆)|科工信|工科")),
    ("商务", re.compile(r"商务|投资促进|招商|口岸")),
    ("财政", re.compile(r"财政")),
    ("人社", re.compile(r"人力资源和社会保障|人力资源社会保障|人力社保|人社")),
    ("市场监管", re.compile(r"市场监督管理|市场监管|市监局|知识产权")),
    ("农业农村", re.compile(r"农业农村|农业和农村|农牧|农业")),
    (
        "文旅",
        re.compile(
            r"文化和旅游|文化广电和旅游|文化和广电旅游|文化广电旅游|文化旅游|文旅|文化体育和旅游|文体旅|"
            r"文化广电体育和旅游|文化广电体育旅游|文广旅|文广新旅|文化广电新闻出版和旅游"
        ),
    ),
    ("生态环境", re.compile(r"生态环境")),
    (
        "数据局",
        re.compile(
            r"数据局|大数据|数据管理|数据资源|数据和政务|政务服务和数据|政务服务数据|数据发展"
        ),
    ),
    (
        "开发区",
        re.compile(
            r"经济技术开发区|高新技术产业开发区|高新技术产业园区|高新区|经开区|开发区|产业园区管理委员会|"
            r"综合保税区|自由贸易试验区|自贸区|保税区|新区管理委员会|新区管委会"
        ),
    ),
)
TARGET_DEPTS = tuple(label for label, _ in DEPT_RULES)

# 机关名称结尾（"……厅""……局""……委员会"）：用来排除新闻标题、服务事项等不是机关名的链接
_ORG_END = re.compile(
    r"(厅|局|委|委员会|办公厅|办公室|办|管委会|管理委员会|中心|署|分局|区|园区|集团|部|总局|总署)$"
)
# 机构名称里不会出现的词：出现了就多半是新闻、事项、专题
_NOT_ORG = re.compile(
    r"召开|举行|开展|关于|通知|公告|公示|会议|活动|调研|印发|发布|解读|专题|专栏|服务指南|办事服务|"
    r"在线服务|办理|指南|大厅|"
    r"查询|申请|报名|系统|平台|热线|信箱|投诉|举报|在线|视频|图片|新闻|动态|要闻|访谈|直播|"
    r"领导|局长(?!.{0,8}分?局$)|主任|书记|讲话|考察|走访|年报|报告|统计|规划|计划|方案|工作动态|工作部署|政策|文件|资讯"
)
_ORG_MAX_LEN = 30


def _bare_org(text: str) -> str:
    """去掉括号注释；显示时被截断的（"……局（长兴县人民政府区域合..."）连同没合上的括号一起去掉。"""
    t = re.sub(r"[（(].*?[）)]", "", text or "")
    return re.sub(r"[（(][^）)]*$|(?:\.{2,}|…+)$", "", t)


def looks_like_org(text: str) -> bool:
    t = _bare_org(text)
    if not t or len(t) > _ORG_MAX_LEN or len(t) < 3:
        return False
    if _NOT_ORG.search(t):
        return False
    return bool(_ORG_END.search(t))


def dept_categories(text: str) -> list[str]:
    """机关名称属于哪几个目标部门；不是机关名或不是目标部门返回空列表。

    "市场监督管理局（知识产权局）"只算市场监管一类；"科学技术和工业信息化局"算科技、工信两类。
    """
    if not looks_like_org(text):
        return []
    t = _bare_org(text)
    out: list[str] = []
    for label, pattern in DEPT_RULES:
        if pattern.search(t):
            out.append(label)
    # "农业农村"的宽松写法"农业"不能吃掉"农业科学院""农业机械化服务中心"这类事业单位
    # （"农业农村局和水利局""农业农村和水利局""农业农村水利局"这类合并部门照算，
    # "农业机械管理局"不算）
    if (
        "农业农村" in out
        and not re.search(r"(农业农村|农业和农村|农牧|农业)(厅|局|委|委员会|部)", t)
        and not re.search(
            r"(农业农村|农业和农村|农牧|农业)"
            r"(?:和?(?:水利|水务|林业|畜牧|乡村振兴|扶贫|渔业|海洋|粮食)){1,2}(厅|局|委|委员会|部)$",
            t,
        )
    ):
        out.remove("农业农村")
    # 开发区：只要管委会（不是开发区里的某个局）
    if "开发区" in out:
        if not re.search(r"(管理委员会|管委会|工作委员会|管理局|管理办公室)$", t):
            out.remove("开发区")
        else:
            out = ["开发区"]  # 开发区管委会下的"经济发展局"等不另算
    # 政府办公厅和其他部门互斥：办公厅名称里不会带别的部门
    if "政府办" in out:
        out = ["政府办"]
    return out


def alias_categories(text: str) -> list[str]:
    """括号里加挂的牌子带来的目标部门（"经信局（商务局）"的商务）。正名已经算的不重复。

    牌子是这个机关的职能，但"科学技术局（知识产权局）"不该抢走真正的市场监管局：盘点只在
    这一类没有正名候选时才用它（备用候选）。"""
    main = dept_categories(text)
    if not main or main in (["开发区"], ["政府办"]):
        return []
    out: list[str] = []
    for part in _bracket_orgs(text):
        for label in dept_categories(part):
            if label not in main and label not in out and label not in ("政府办", "开发区"):
                out.append(label)
    return out


def _bracket_orgs(text: str) -> list[str]:
    """括号里像机关名的部分（"……局（商务局）"里的"商务局"；"、"分开的几个都算）。"""
    out: list[str] = []
    for inner in re.findall(r"[（(]([^（()）]+)[）)]", text or ""):
        for part in re.split(r"[、，,；;]", inner):
            part = part.strip()
            if re.search(r"(局|委|委员会|办|办公室)$", part):
                out.append(part)
    return out


# 栏目类型 → 写法（先比完全相同，再比包含）。和全国来源清单里已验证的栏目名一致
COLUMN_RULES: tuple[tuple[str, tuple[str, ...], re.Pattern[str]], ...] = (
    (
        "通知公告",
        (
            "通知公告",
            "通知通告",
            "公告通知",
            "通知",
            "工作通知",
            "通知告示",
            "最新通知",
            "通知公示",
            "公告栏",
            "文件通知",
            "通告公告",
            "公告",
            "重要公告",
            "公告信息",
        ),
        re.compile(
            r"^(?:本[局委厅部]|部门|工作)?通知(?:公告|通告|公示)?$|^公告(?:通知)?$|通知公告"
        ),
    ),
    (
        "公示公告",
        ("公示公告", "公告公示", "公示", "公示信息", "公示公开", "公示通告"),
        re.compile(r"^公示(?:公告|信息|公开)?$|公告公示|公示公告"),
    ),
    (
        "政策文件",
        (
            "政策文件",
            "规范性文件",
            "行政规范性文件",
            "政策法规",
            "文件发布",
            "政府文件",
            "部门文件",
            "本级文件",
            "政策发布",
            "政策",
            "文件",
            "法规文件",
            "政策文件库",
            "最新文件",
            "政府规章",
            "政策法规文件",
        ),
        re.compile(
            r"^(?:[省市县区]政府|[省市县区]?政府办(?:公[厅室])?|本[局委厅]|部门|[省市县区]级)?"
            r"(?:政策|规范性|行政规范性)?文件(?:发布|库)?$|^政策(?:法规|发布|文件)?$|规范性文件|政策文件"
        ),
    ),
)
COLUMN_KINDS = tuple(kind for kind, _, _ in COLUMN_RULES)
# 不要的栏目：解读、征求意见、废止、信息公开年报等
_NOT_COLUMN = re.compile(r"征求|意见|废止|失效|清理|年报|指南|问答|图解|动漫|视频|备案|目录|依申请")
# "政策解读"不是政策文件；"政策文件及解读"（浙江等地信息公开分类）是
_INTERPRETATION = re.compile(r"解读")
_WITH_INTERPRETATION = re.compile(
    r"^(?:政策文件|规范性文件|政策法规|文件)(?:及|和|与)(?:政策)?解读$"
)
_COLUMN_MAX_LEN = 14


def column_kind(text: str) -> tuple[str, int] | None:
    """链接文字是哪类目标栏目，以及匹配强度（0 完全相同，1 包含）；不是返回 None。"""
    t = re.sub(r"\s+", "", text or "")
    t = re.sub(r"[>»›]+$|^[>»›·•]+", "", t)
    if not t or len(t) > _COLUMN_MAX_LEN or _NOT_COLUMN.search(t):
        return None
    if _INTERPRETATION.search(t):
        return ("政策文件", 1) if _WITH_INTERPRETATION.match(t) else None
    for kind, names, _pattern in COLUMN_RULES:
        if t in names:
            return kind, 0
    for kind, _, pattern in COLUMN_RULES:
        if pattern.search(t):
            return kind, 1
    return None
