"""按标题给文件分类：申报通知 / 名单 / 政策 / 其他，以及名单的状态（公示 / 公布，只照录标题用词）。

同一套关键词既用于 Python（测试、单条判断），也生成 SQL 条件（列表筛选、分页在数据库里完成）。
规则按顺序匹配：先排除与企业申报无关的公示（招聘、采购等），再名单，再申报通知，再政策；
都不命中算"其他"。拿不准的归"其他"，不硬判。
"""

from sqlalchemy import ColumnElement, and_, case, not_, or_

# 名单：标题里出现这些词。"公示"单独出现时也多是名单公示
LIST_WORDS = (
    "名单",
    "公示",
    "拟认定",
    "拟入库",
    "入库企业",
    "认定结果",
    "拟立项",
    "拟支持",
    "拟资助",
    "资助决定",
    "企业更名",
)
# 与企业申报无关的公示和名单（招聘、职称评审、采购比选、产能置换、执法检查、督察整改等），
# 标题有名单字样也不算名单，归"其他"。职称、招聘名单列的是个人，也不该放进查名单
LIST_OFF_TOPIC = (
    "招聘",
    "拟聘",
    "职称",
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
    "督察",
    "销号",
)
# 征求意见：政策草案（写了"征求意见稿"），或只凭"公示"二字才像名单的（如"关于《XX管理办法》
# 公开征求社会意见的公示"），不算名单，按其他规则判断，与其他征求意见稿一样有政策字样的归政策。
# 写明名单、拟资助等的（如"拟资助项目名单征求意见的公示"）仍是名单
CONSULT_WORDS = ("征求",)
DRAFT_WORDS = ("征求意见稿",)
STRONG_LIST_WORDS = tuple(w for w in LIST_WORDS if w != "公示")
# 带名单字样、但其实是在组织一项工作（如"组织开展……名单制定工作的通知"），
# 且不是公示或公布结果的，算申报通知
LIST_APPLY_HINTS = ("组织开展", "有关事项的通知", "工作的通知", "申报")
LIST_RESULT_HINTS = ("公示", "公布", "拟", "结果", "决定")
# 申报通知：组织申报、征集、推荐、遴选
APPLY_WORDS = ("申报", "征集", "推荐", "遴选", "评选", "报名", "组织开展", "申请工作")
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
)
POLICY_EXCLUDE = ("讲座", "培训", "会议", "活动", "招聘", "招标", "研修班")

KINDS = ("申报通知", "名单", "政策", "其他")


def _has(title: str, words: tuple[str, ...]) -> bool:
    return any(w in title for w in words)


def _is_list(title: str) -> bool:
    if not _has(title, LIST_WORDS):
        return False
    return not (_has(title, LIST_APPLY_HINTS) and not _has(title, LIST_RESULT_HINTS))


def _off_topic(title: str) -> bool:
    return _has(title, LIST_WORDS) and _has(title, LIST_OFF_TOPIC)


def _consult(title: str) -> bool:
    if _has(title, DRAFT_WORDS):
        return True
    return _has(title, CONSULT_WORDS) and not _has(title, STRONG_LIST_WORDS)


def doc_kind(title: str) -> str:
    if _off_topic(title):
        return "其他"
    consult = _consult(title)
    if not consult and _is_list(title):
        return "名单"
    if _has(title, APPLY_WORDS) or (not consult and _has(title, LIST_WORDS)):
        return "申报通知"
    if _has(title, POLICY_WORDS) and not _has(title, POLICY_EXCLUDE):
        return "政策"
    return "其他"


# 名单状态是关键字段（团队章程合规红线：未经核验不得发布），只照录标题原文用词（决策记录 D-018）：
# 标题写了"公示"标"公示"；写了"公布"且没有"拟"字标"公布"；只写"公告"的不标
# （"公告"也用于拟资助、拟认定等尚在公示阶段的名单，如"……拟资助项目公告"）
STATUS_PUBLIC = ("公示",)
STATUS_FINAL = ("公布",)
STATUS_NOT_FINAL = ("拟",)


def list_status(title: str) -> str | None:
    """名单状态，只照录标题原文：公示 / 公布；标题没写明或不是名单时返回 None。"""
    if doc_kind(title) != "名单":
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
    off_topic = and_(has_list, _any_in(title, LIST_OFF_TOPIC))
    consult = or_(
        _any_in(title, DRAFT_WORDS),
        and_(_any_in(title, CONSULT_WORDS), not_(_any_in(title, STRONG_LIST_WORDS))),
    )
    is_list = and_(
        not_(off_topic),
        not_(consult),
        has_list,
        not_(and_(_any_in(title, LIST_APPLY_HINTS), not_(_any_in(title, LIST_RESULT_HINTS)))),
    )
    is_apply = and_(
        not_(off_topic),
        not_(is_list),
        or_(_any_in(title, APPLY_WORDS), and_(has_list, not_(consult))),
    )
    is_policy = and_(
        not_(off_topic),
        not_(is_list),
        not_(is_apply),
        _any_in(title, POLICY_WORDS),
        not_(_any_in(title, POLICY_EXCLUDE)),
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


def status_filter(title: ColumnElement[str], statuses: list[str]) -> ColumnElement[bool]:
    """名单状态筛选：只作用于名单类文件。"""
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
