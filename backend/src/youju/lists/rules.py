"""保守的名单标题和企业名称规则。"""

import re
import unicodedata
from dataclasses import dataclass

from youju.classify import doc_kind
from youju.crawler.detail import redact_sensitive

_ORG_TERMS = (
    "公司",
    "集团",
    "企业",
    "中心",
    "研究院",
    "研究所",
    "科学院",
    "事务所",
    "合作社",
    "联合社",
    "协会",
    "学会",
    "商会",
    "医院",
    "学校",
    "大学",
    "学院",
    "商行",
    "工厂",
    "厂",
    "工作室",
    "银行",
    "基金会",
    "实验室",
    "基地",
    "分院",
    "分所",
    "有限合伙",
    "普通合伙",
    "机构",
)
# “推荐申报名单”“拟申请备案企业名单公示”确为机构名单，不能整体排除“申报”“申请”。
_NEGATIVE_TITLE = (
    "取消",
    "撤销",
    "注销",
    "更名",
    "变更",
    "调整",
    "退出",
    "移出",
    "奖励",
    "补贴",
    "征集",
)
# 查名单只查认定、立项、公示、入库、备案这类名单（D-091 ③）：失信、处罚、经营异常、黑名单、
# 惩戒、警示类不解析、不进查询，标题里有这些字就不算。"无失信记录企业""无重大违法违规"这类名单
# 也不收：本身就是信用、合规状况的名单，收进来像在做企业征信（agy 10/25 合规审阅）
PUNITIVE_TITLE = (
    "失信",
    "处罚",
    "经营异常",
    "异常名录",
    "黑名单",
    "惩戒",
    "警示",
    "违规",
    "违法",
    "不良",
)
# 以个人为主的名单（人才、专家等）不进企业查询（D-091 ③）：标题里人和"名单""名录"紧挨着的
# （"高层次人才名单""某研究院专家名单""科技特派员名录"），和写"人选"的（"拟入选人才计划人选公示"）。
# 只看紧挨着的：企业名单的标题里也常有"专家评审""人才引进"这类字（"经专家评审拟认定的高新技术
# 企业名单""人才引进优秀企业名单"），不能按出现了这些字就排除
_PERSON_WORDS = (
    "人才",
    "专家",
    "个人",
    "教师",
    "医师",
    "医生",
    "律师",
    "技师",
    "工匠",
    "学者",
    "院士",
    "人员",
    "导师",
    "劳模",
    "劳动模范",
    "特派员",
    "带头人",
    "入选者",
    "获得者",
)
# 个人事项的名单（11/9 第 1 批核对补的）：退休、病残津贴、工伤认定、选调录用、任免任职、负责人、
# 劳模和先进个人、职务评审、考试成绩、专家库这类，名单里全是个人（还常有健康状况、职务），
# 按 D-091 ③、D-092 不给正文、不进查名单。以前只认"人员名单""专家名单"这种紧挨着的写法，
# "提前退休人员公示""负责人任职前公示""人大常委会任免名单"都漏了（11/4 线上快照里负责人
# 任职前公示的正文露着姓名）。
# 逐个核过本机三个库全部名单类标题（8 千多个）：不挡企业名单——"工伤保险协议机构""工伤预防项目"
# 是机构、项目，所以只认"工伤认定""工伤保险待遇"；"劳动能力鉴定档案整理服务项目"是采购，所以只认
# "丧失劳动能力""劳动能力鉴定结论/结果"；"招募……培训机构"是机构，所以只认"招募人员"（在下面的
# "人员公示"里）；"职务科技成果……试点单位名单"是单位，所以"职务"只认职称评审的写法；"登记功能"
# 不是记功，所以只认"拟记功""记功奖励"。"候选人"也会挡掉采购的"成交候选人公示"：采购结果不在
# 查名单的五类里（D-091 ③），不放出来也没关系
_PERSONAL_MATTER_LIST = (
    "退休",
    "病残",
    "伤残",
    "丧失劳动能力",
    "劳动能力鉴定结论",
    "劳动能力鉴定结果",
    "工伤认定",
    "工伤保险待遇",
    "选调",
    "录用",
    "招录",
    "聘用",
    "聘任",
    "任免",
    "任命",
    "免职",
    "任职",
    "任前",
    "拟任",
    "辞职",
    "负责人",
    "同志",
    "劳模",
    "劳动模范",
    "先进工作者",
    "先进个人",
    "红旗手",
    "道德模范",
    "技术能手",
    "拟记功",
    "记功奖励",
    "候选人",
    "选手",
    "职务资格",
    "技术职务",
    "拟任职务",
    "职务评审",
    "评审委员会",
    "考试结果",
    "考试通过",
    "考试成绩",
    "笔试",
    "面试",
    "业余无线电",
    "仲裁员",
    "运动员",
    "学员名单",
    "救助",
    "助学",
    "专家库",
    "委员名单",
    "持证上岗",
    "人才进行公示",
    "人员进行公示",
    "教育资助",
    "代表性传承人",
    "灵活就业",
    "创业担保贷款",
)
# 人和"公示""情况""审核""递补"紧挨着的也是个人类（"拟招募人员公示""提前退休人员情况公示"
# "拟资助人员递补名单"）；"个人"不算（"……资金的单位和个人的公示"里有企业）
_PERSON_NEXT_TO = ("公示", "的公示", "情况", "审核", "递补")
PERSON_TITLE = (
    *(f"{word}{suffix}" for word in _PERSON_WORDS for suffix in ("名单", "名录")),
    *(
        f"{word}{suffix}"
        for word in (*_PERSON_WORDS, "职工")
        if word != "个人"
        for suffix in _PERSON_NEXT_TO
    ),
    "人选",
    *_PERSONAL_MATTER_LIST,
)
# 获奖、表彰类名单也不进企业查询：不在 D-091 ③ 的五类（认定、立项、公示、入库、备案）里，
# 还常带获奖人、完成人的名字。标题带"奖励""补贴"的资金通知本来就不当名单（见上面 _NEGATIVE_TITLE）
HONOR_TITLE = ("获奖", "表彰")
_SELECTION = ("拟认定", "拟入库", "认定", "入库", "备案", "通过", "入选")
_ENTITY = ("企业", "机构", "单位", "中心", "研究院", "合作社", "平台", "实验室", "基地")
_BATCH = re.compile(
    r"第\s*([0-9一二三四五六七八九十百]+)\s*批|[（(]\s*第?([0-9一二三四五六七八九十百]+)\s*批\s*[）)]"
)
_CREDIT_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_CREDIT_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_LEGAL_END = re.compile(r"\((?:有限责任公司|股份有限公司|有限公司|有限合伙|普通合伙|个人独资)\)$")


@dataclass(frozen=True)
class ListClassification:
    """要不要解析这份名单、类别和批次。名单状态不在这里判断：统一按 D-018 用
    youju.classify.list_status 照录标题用词，查询时实时算，不随条目存储。"""

    is_list: bool
    category: str = "未确定"
    batch: str | None = None


def normalize_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", name).replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", value).strip()


def is_person_query(value: str) -> bool:
    value = normalize_name(value)
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", value)) and not any(
        term in value for term in _ORG_TERMS
    )


def valid_org_name(value: str, credit_code: str | None = None) -> bool:
    normalized = normalize_name(value)
    return (
        3 <= len(normalized) <= 100
        and redact_sensitive(normalized) == normalized
        and not any(c in normalized for c in "<>@；;：:，。、\n")
        and not is_person_query(normalized)
        and (
            normalized.endswith(_ORG_TERMS)
            or _LEGAL_END.search(normalized) is not None
            or credit_code is not None
        )
    )


def normalize_credit_code(value: str) -> str | None:
    code = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).upper()
    if (
        len(code) != 18
        or any(c not in _CREDIT_ALPHABET for c in code)
        or redact_sensitive(code) != code
    ):
        return None
    checksum = (
        31
        - sum(
            _CREDIT_ALPHABET.index(c) * w for c, w in zip(code[:17], _CREDIT_WEIGHTS, strict=True)
        )
        % 31
    ) % 31
    return code if _CREDIT_ALPHABET[checksum] == code[-1] else None


def _category_batch(value: str) -> tuple[str, str | None]:
    """从（归一化后的）标题看名单类别和批次。"""
    category = "其他"
    if "专精特新" in value and ("小巨人" in value or '"小巨人"' in value):
        category = '专精特新"小巨人"'
    elif "专精特新" in value and "中小企业" in value:
        category = "专精特新中小企业"
    elif "专精特新" in value:
        category = "未确定"
    elif "高新技术企业" in value:
        category = "高新技术企业"
    elif "科技型中小企业" in value:
        category = "科技型中小企业"
    elif "创新型中小企业" in value:
        category = "创新型中小企业"
    match = _BATCH.search(value)
    batch = f"第{match.group(1) or match.group(2)}批" if match else None
    return category, batch


def classify_title(title: str, kind_override: str | None = None) -> ListClassification:
    # 先与全站分类一致：不是"名单"的（含招聘、职称、采购等无关公示）不解析。
    # 人工在核对台定过类型的按人工定的（D-023）
    kind = kind_override or doc_kind(title)
    if kind != "名单":
        return ListClassification(False)
    if excluded_list_title(title):
        # 失信、处罚类和个人类名单人工定成名单也不解析（D-091 ③）
        return ListClassification(False)
    if kind_override == "名单":
        # 人工确认是名单：不再用标题关键词二次筛，直接解析
        return ListClassification(True, *_category_batch(normalize_name(title)))
    value = normalize_name(title)
    if any(term in value for term in _NEGATIVE_TITLE):
        return ListClassification(False)
    has_list_word = "名单" in value or "名录" in value
    has_release = any(term in value for term in ("公示", "公告", "公布"))
    has_selection = any(term in value for term in _SELECTION)
    has_entity = any(term in value for term in _ENTITY)
    if not has_list_word and not (
        has_selection and has_entity and (has_release or "结果" in value)
    ):
        return ListClassification(False)
    # “名单制定工作通知”只是工作部署，未提供入选名单。
    if (
        ("通知" in value and "工作" in value and not has_list_word)
        or "名单制定" in value
        or "办法" in value
    ):
        return ListClassification(False)
    return ListClassification(True, *_category_batch(value))


def excluded_list_title(title: str) -> bool:
    """不进企业查询的名单：失信、处罚等惩戒类，以个人为主的（人才、专家等），获奖、表彰类。
    和 lookup.list_title_allowed 同一口径（那边是数据库查询条件）。"""
    value = normalize_name(title or "")
    if any(term in value for term in PUNITIVE_TITLE):
        return True
    return any(term in value for term in PERSON_TITLE + HONOR_TITLE)


def candidate_key(value: str) -> str:
    """只用于单独展示近似候选；不能当作企业身份相同的证据。"""
    normalized = re.sub(r"\([^)]{1,20}\)", "", normalize_name(value))
    return re.sub(r"(?:有限责任公司|股份有限公司|有限公司|集团公司|公司)$", "", normalized)
