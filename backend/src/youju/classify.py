"""按标题给文件分类：申报通知 / 名单 / 政策 / 其他，以及名单的状态（公示 / 公布，只照录标题用词）。

同一套关键词既用于 Python（测试、单条判断），也生成 SQL 条件（列表筛选、分页在数据库里完成）。
规则按顺序匹配：先排除与企业申报无关的公示（招聘、采购等），再名单，再申报通知，再政策；
都不命中算"其他"。拿不准的归"其他"，不硬判。
"""

import re

from sqlalchemy import ColumnElement, and_, case, not_, or_

# 名单：标题里出现这些词。"公示"单独出现时也多是名单公示
LIST_WORDS = (
    "名单",
    "公示",
    "拟认定",
    "拟入库",
    "入库企业",
    "入库科技型",  # "第二批入库科技型中小企业的公告"这类标题不写"名单"
    "入选",
    "认定结果",
    "复核结果",
    "拟立项",
    "立项公告",
    "立项公示",
    "立项的公示",
    "立项的公告",
    "拟支持",
    "拟资助",
    "资助决定",
    "企业更名",
    # 评审、审查、评估等工作的结果：列的是入选的项目或单位（2026-09-28 抽样核对时补）
    "评审结果",
    "审查结果",
    "评估结果",
    "评价结果",
    "核查结果",
    "验收结果",
    "遴选结果",
    "评选结果",
    # 揭榜、比赛的结果
    "中榜",
    "获奖",
    "晋级",
    "决赛结果",
)
# 与企业申报无关的公示和名单（招聘、职称评审、考试合格人员、评审专家、采购比选、产能置换、执法检查、
# 行政处罚、督察整改等），标题有名单字样也不算名单，归"其他"。职称、招聘、考试、专家名单列的是个人，
# 也不该放进查名单（查名单只查企业和机构）
LIST_OFF_TOPIC = (
    "招聘",
    "拟聘",
    "职称",
    "专业技术资格",
    "任职资格",
    "考试合格",
    "考试资格",
    "考试成绩",
    "专家公示",
    "专家的公示",
    "采购结果",
    "比选",
    "中标",
    "询价",
    "产能置换",
    "产能退出",
    "产能转移",
    "扩区",
    "双随机",
    "执法检查",
    "行政处罚",
    "督察",
    "销号",
    "重大事项变更",
    "收费",
    "监督检查",
    "人选",
    "项目信息",
)
# 招聘是个人事项：不管标题怎么写，都不是企业申报，也不列进名单。
# 职称申报通知仍算申报通知（科技服务机构常代办，#43 定的规则），职称名单则按上面的规则归"其他"
PERSONAL_WORDS = ("招聘",)
# 网站专题栏目的归档入口（标题以"（已归档"结尾），不是文件（列表解析已不收，这里兜底）
JUNK_SUFFIXES = ("（已归档", "(已归档", "（已归档）", "(已归档)")
# 征求意见：政策草案（写了"征求意见稿"），或只凭"公示"二字才像名单的，不算名单，
# 按其他规则判断，与其他征求意见稿一样有政策字样的归政策。例如"关于《XX管理办法》
# 公开征求社会意见的公示""关于企业证照办理优化措施公开及办理意见建议的公示"。
# 写明名单、拟资助等的（如"拟资助项目名单征求意见的公示"）仍是名单
CONSULT_WORDS = ("征求", "意见建议")
DRAFT_WORDS = ("征求意见稿",)
STRONG_LIST_WORDS = tuple(w for w in LIST_WORDS if w != "公示")
# 带名单字样、但其实是在组织一项工作（如"组织开展……名单制定工作的通知"），
# 且不是公示或公布结果的，算申报通知
LIST_APPLY_HINTS = ("组织开展", "有关事项的通知", "工作的通知", "申报")
LIST_RESULT_HINTS = ("公示", "公布", "拟", "结果", "决定")
# 标题写"公布"、列的是项目或单位、又不是具名文件（没有书名号）或申报通知的，也是名单，
# 如"关于公布……先行先试联合体及项目的通知"
PUBLISH_WORDS = ("公布",)
PUBLISH_OBJECTS = (
    "项目",
    "企业",
    "单位",
    "机构",
    "联合体",
    "基地",
    "平台",
    "中心",
    "示范",
    "车间",
    "工厂",
    "园区",
)
# 公布的是标准、目录、清单、指南等文件时不算名单
PUBLISH_EXCLUDE = ("标准", "目录", "清单", "指南")
# 申报通知：组织申报、征集、推荐、遴选、申领、揭榜、参赛，以及发放各类服务券
APPLY_WORDS = (
    "申报",
    "征集",
    "推荐",
    "遴选",
    "评选",
    "报名",
    "申请工作",
    "申请的通知",
    "申领",
    "揭榜",
    "服务券",
    "创新券",
    "科技券",
    "算力券",
    "数据券",
)
# 这些词单独出现时也可能是统计、检查、培训、采购、新闻等，不是让企业去申报；
# 印发具名文件（如"印发《……评价工作细则》"）的是政策
WEAK_APPLY_WORDS = (
    "组织开展",
    "评价工作",
    "大赛",
    "认定和复核",
    "认定工作",
    "复核工作",
    "评审工作",
    "报送",
    "结题验收",
    "验收工作",
)
# "报送……情况"是报送进展、统计情况，不是申报
APPLY_EXCLUDE = ("统计", "调查", "检查", "培训", "会议", "讲座", "成交", "采购", "收官", "情况")
# 政策文件；讲座、培训、会议等活动通知里即使提到"办法"也不算政策
POLICY_WORDS = (
    "办法",
    "规定",
    "意见",
    "方案",
    "细则",
    "条例",
    "规划",
    "措施",
    "若干政策",
    "实施意见",
    "规范性文件",
    "废止",
    "政策的公告",
    "政策的通知",
)
# 印发、征求意见的具名文件（书名号）：多是办法、方案、指南、清单等，标题被截断时也认得出
POLICY_MARKS = ("印发《", "征求《", "关于《", "发布《", "公布《")
# 撤销某个项目的方案、单个项目的方案不是政策
POLICY_EXCLUDE = (
    "讲座",
    "培训",
    "会议",
    "活动",
    "招聘",
    "招标",
    "研修班",
    "撤销",
    "项目方案",
    "产能置换",
)

KINDS = ("申报通知", "名单", "政策", "其他")


def _has(title: str, words: tuple[str, ...]) -> bool:
    return any(w in title for w in words)


def _publish_list(title: str) -> bool:
    return (
        _has(title, PUBLISH_WORDS)
        and _has(title, PUBLISH_OBJECTS)
        and "《" not in title
        and not _has(title, APPLY_WORDS + PUBLISH_EXCLUDE + POLICY_WORDS)
    )


def _is_list(title: str) -> bool:
    if _publish_list(title):
        return True
    if not _has(title, LIST_WORDS):
        return False
    return not (_has(title, LIST_APPLY_HINTS) and not _has(title, LIST_RESULT_HINTS))


def _off_topic(title: str) -> bool:
    return (_has(title, LIST_WORDS) or _publish_list(title)) and _has(title, LIST_OFF_TOPIC)


def _named_policy(title: str) -> bool:
    """具名文件（书名号）里有办法、规定、意见、方案等字样：是政策，即使标题里有"申报""创新券"等字，
    如"关于印发《创新券管理办法》的通知"。书名号里没有这些字的（如"印发《……项目申报指南》"）不算。
    与 SQL 的 LIKE '%《%办法%》%' 同义：前面有《、后面有》即可。"""
    return _has(title, POLICY_MARKS) and any(
        re.search("《.*" + re.escape(w) + ".*》", title, re.S) for w in POLICY_WORDS
    )


def _early_policy(title: str) -> bool:
    """先于申报通知判断的政策：征求意见稿、"征集《……》……意见"、具名政策文件。"""
    return not _has(title, POLICY_EXCLUDE) and (
        _has(title, DRAFT_WORDS) or ("征集《" in title and "意见" in title) or _named_policy(title)
    )


def _is_apply(title: str, consult: bool) -> bool:
    return (
        _has(title, APPLY_WORDS)
        or (_has(title, WEAK_APPLY_WORDS) and not _has(title, APPLY_EXCLUDE + POLICY_MARKS))
        or (not consult and _has(title, LIST_WORDS))
    )


def _consult(title: str) -> bool:
    if _has(title, DRAFT_WORDS):
        return True
    return _has(title, CONSULT_WORDS) and not _has(title, STRONG_LIST_WORDS)


def doc_kind(title: str) -> str:
    if _off_topic(title) or _has(title, PERSONAL_WORDS) or title.endswith(JUNK_SUFFIXES):
        return "其他"
    consult = _consult(title)
    if not consult and _is_list(title):
        return "名单"
    if _early_policy(title):
        return "政策"
    if _is_apply(title, consult):
        return "申报通知"
    if _has(title, POLICY_WORDS + POLICY_MARKS) and not _has(title, POLICY_EXCLUDE):
        return "政策"
    return "其他"


# 名单状态是关键字段（团队章程合规红线：未经核验不得发布），只照录标题原文用词（决策记录 D-018）：
# 标题写了"公示"标"公示"；写了"公布"且没有"拟"字标"公布"；只写"公告"的不标
# （"公告"也用于拟资助、拟认定等尚在公示阶段的名单，如"……拟资助项目公告"）
STATUS_PUBLIC = ("公示",)
STATUS_FINAL = ("公布",)
STATUS_NOT_FINAL = ("拟",)


def effective_kind(title: str, override: str | None) -> str:
    """给用户看的类型：人工在核对台定过的按人工定的，没定过的按标题规则判断。"""
    return override or doc_kind(title)


def list_status(title: str, kind: str | None = None) -> str | None:
    """名单状态，只照录标题原文：公示 / 公布；标题没写明或不是名单时返回 None。
    kind 是这份文件给用户看的类型（人工定过类型时传进来），不传就按标题规则判断。"""
    if (kind or doc_kind(title)) != "名单":
        return None
    if _has(title, STATUS_PUBLIC):
        return "公示"
    if _has(title, STATUS_FINAL) and not _has(title, STATUS_NOT_FINAL):
        return "公布"
    return None


def _any_in(column: ColumnElement[str], words: tuple[str, ...]) -> ColumnElement[bool]:
    return or_(*(column.contains(w, autoescape=True) for w in words))


def _sql_parts(title: ColumnElement[str]) -> dict[str, ColumnElement[bool]]:
    """与 doc_kind 完全相同的判断，写成 SQL 条件。"""
    has_list = _any_in(title, LIST_WORDS)
    publish_list = and_(
        _any_in(title, PUBLISH_WORDS),
        _any_in(title, PUBLISH_OBJECTS),
        not_(title.contains("《", autoescape=True)),
        not_(_any_in(title, APPLY_WORDS + PUBLISH_EXCLUDE + POLICY_WORDS)),
    )
    off_topic = or_(
        and_(or_(has_list, publish_list), _any_in(title, LIST_OFF_TOPIC)),
        _any_in(title, PERSONAL_WORDS),
        or_(*(title.like("%" + suffix) for suffix in JUNK_SUFFIXES)),
    )
    consult = or_(
        _any_in(title, DRAFT_WORDS),
        and_(_any_in(title, CONSULT_WORDS), not_(_any_in(title, STRONG_LIST_WORDS))),
    )
    is_list = and_(
        not_(off_topic),
        not_(consult),
        or_(
            publish_list,
            and_(
                has_list,
                not_(
                    and_(_any_in(title, LIST_APPLY_HINTS), not_(_any_in(title, LIST_RESULT_HINTS)))
                ),
            ),
        ),
    )
    named_policy = and_(
        _any_in(title, POLICY_MARKS),
        or_(*(title.like(f"%《%{w}%》%") for w in POLICY_WORDS)),
    )
    is_draft_policy = and_(
        not_(off_topic),
        not_(is_list),
        not_(_any_in(title, POLICY_EXCLUDE)),
        or_(
            _any_in(title, DRAFT_WORDS),
            and_(
                title.contains("征集《", autoescape=True), title.contains("意见", autoescape=True)
            ),
            named_policy,
        ),
    )
    is_apply = and_(
        not_(off_topic),
        not_(is_list),
        not_(is_draft_policy),
        or_(
            _any_in(title, APPLY_WORDS),
            and_(
                _any_in(title, WEAK_APPLY_WORDS),
                not_(_any_in(title, APPLY_EXCLUDE + POLICY_MARKS)),
            ),
            and_(has_list, not_(consult)),
        ),
    )
    is_policy = or_(
        is_draft_policy,
        and_(
            not_(off_topic),
            not_(is_list),
            not_(is_apply),
            _any_in(title, POLICY_WORDS + POLICY_MARKS),
            not_(_any_in(title, POLICY_EXCLUDE)),
        ),
    )
    is_other = and_(not_(is_list), not_(is_apply), not_(is_policy))
    return {"名单": is_list, "申报通知": is_apply, "政策": is_policy, "其他": is_other}


def kind_expr(title: ColumnElement[str]) -> ColumnElement[str]:
    parts = _sql_parts(title)
    return case(
        (parts["名单"], "名单"),
        (parts["申报通知"], "申报通知"),
        (parts["政策"], "政策"),
        else_="其他",
    )


def kind_filter(title: ColumnElement[str], kinds: list[str]) -> ColumnElement[bool]:
    parts = _sql_parts(title)
    return or_(*(parts[k] for k in kinds if k in parts))


def status_filter(
    title: ColumnElement[str],
    statuses: list[str],
    is_list: ColumnElement[bool] | None = None,
) -> ColumnElement[bool]:
    """名单状态筛选：只作用于名单类文件（is_list 不传就按标题规则判断是不是名单）。"""
    if is_list is None:
        is_list = _sql_parts(title)["名单"]
    public = _any_in(title, STATUS_PUBLIC)
    final = and_(not_(public), _any_in(title, STATUS_FINAL), not_(_any_in(title, STATUS_NOT_FINAL)))
    parts = {"公示": and_(is_list, public), "公布": and_(is_list, final)}
    return or_(*(parts[s] for s in statuses if s in parts))


def dept_group(dept: str) -> str:
    """发文部门分组：科技部门 / 工信部门 / 其他部门（来源清单的部门简称）。"""
    if "科技" in dept:
        return "科技部门"
    if "工信" in dept or "经信" in dept or "工业和信息化" in dept:
        return "工信部门"
    return "其他部门"


def region_label(province: str, level: str) -> str:
    """列表上显示的地区：国家级来源显示"国家"，其余"省份 · 级别"。"""
    if province == "国家":
        return "国家"
    return f"{province} · {level}"
