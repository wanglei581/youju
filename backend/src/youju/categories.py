"""项目类别（D-051 第二阶段）：按标题把申报通知和名单归到 8 个类别，供小程序按类别筛选。

- 只按标题关键词归类，仅供检索参考，不是官方分类，也不据此判断企业能不能申报。
- 一份文件只归一类：按 CATEGORY_RULES 的顺序，先命中的算；都不命中算"其他申报"。
  顺序从具体到宽泛，例如"科学技术奖"先归表彰奖励，"新型研发机构"先归平台载体与园区。
- 只对申报通知和名单显示、筛选（CATEGORY_KINDS）；政策、资讯与指南、其他不归类。
- 与文件类型一样，入库时算好存在 rule_category（筛选只比较这一列），规则改了由 youju reclassify
  重算；还没算的（刚升级）筛选时当场按同一套规则判断，结果一样。
"""

from sqlalchemy import ColumnElement, and_, case, not_, or_

OTHER_CATEGORY = "其他申报"
# 只有这两类文件有项目类别
CATEGORY_KINDS = ("申报通知", "名单")
# （类别, 关键词）：按顺序匹配
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "资质与梯度培育",
        (
            "高新技术企业",
            "高企",
            "科技型中小企业",
            "科小",
            "专精特新",
            "小巨人",
            "单项冠军",
            "创新型中小企业",
            "优质中小企业",
            "梯度培育",
            "瞪羚",
            "独角兽",
            "雏鹰",
            "隐形冠军",
            "技术先进型服务企业",
            "科技领军企业",
            "创新型企业",
            "创新型评价",
            "科技型企业",
            "加计抵减",
        ),
    ),
    (
        "首台套与新产品",
        (
            "首台",
            "首套",
            "首批次",
            "首版次",
            "新产品",
            "新技术新产品",
            "创新产品",
            "首购",
        ),
    ),
    (
        "表彰奖励",
        (
            "科学技术奖",
            "科技奖",
            "技术奖",
            "质量奖",
            "专利奖",
            "设计奖",
            "表彰",
            "荣誉",
            "称号",
            "授奖",
            "获奖",
            "提名",
        ),
    ),
    (
        "平台载体与园区",
        (
            "实验室",
            "技术创新中心",
            "工程技术研究中心",
            "工程研究中心",
            "企业技术中心",
            "制造业创新中心",
            "创新中心",
            "研发机构",
            "研发中心",
            "研究院",
            "工业设计中心",
            "设计中心",
            "中试",
            "概念验证",
            "孵化器",
            "众创空间",
            "加速器",
            "科技园",
            "园区",
            "产业园",
            "高新区",
            "开发区",
            "基地",
            "平台",
            "创新联合体",
            "产业集群",
            "服务机构",
            "技术转移机构",
            "促进中心",
            "技术中心",
            "载体",
            "科技基础条件",
        ),
    ),
    (
        "科技计划与研发",
        (
            "科技计划",
            "研发计划",
            "重点研发",
            "重大专项",
            "重点专项",
            "科技专项",
            "科技项目",
            "科学基金",
            "联合基金",
            "揭榜",
            "攻关",
            "软科学",
            "课题",
            "产学研",
            "前沿技术",
            "基础研究",
            "关键核心技术",
            "成果转化",
            "技术创新项目",
            "研发",
            "科研",
            "研究开发",
            "科学研究",
            "技术创新计划",
            "技术需求",
            "引导地方科技发展",
        ),
    ),
    (
        "人才",
        (
            "人才",
            "院士",
            "学者",
            "英才",
            "工程师",
            "技术经理人",
            "技术经纪人",
            "科技特派员",
            "创新创业团队",
            "团队",
            "青年",
            "引智",
            "外国专家",
            "工匠",
            "职称",
            "技能",
            "专业技术资格",
            "科技副总",
        ),
    ),
    (
        "试点示范",
        (
            "试点",
            "示范",
            "场景",
            "标杆",
            "典型",
            "灯塔",
            "智能工厂",
            "数字化转型",
            "绿色工厂",
            "绿色制造",
            "零碳",
            "智能制造",
            "先行区",
            "领跑者",
            "案例",
        ),
    ),
)
CATEGORIES = tuple(name for name, _ in CATEGORY_RULES) + (OTHER_CATEGORY,)


def doc_category(title: str) -> str:
    """按标题归类（不看文件类型）；只对申报通知和名单显示，见 shown_category。"""
    for name, words in CATEGORY_RULES:
        if any(w in title for w in words):
            return name
    return OTHER_CATEGORY


def shown_category(kind: str, category: str | None, title: str) -> str | None:
    """给用户看的项目类别：申报通知和名单才有；category 是入库时算好的，没算的当场按标题判断。"""
    if kind not in CATEGORY_KINDS:
        return None
    return category or doc_category(title)


def _matches(title: ColumnElement[str], words: tuple[str, ...]) -> ColumnElement[bool]:
    return or_(*(title.contains(w, autoescape=True) for w in words))


def category_expr(title: ColumnElement[str]) -> ColumnElement[str]:
    """与 doc_category 完全相同的判断，写成 SQL（按顺序，先命中的算）。"""
    return case(
        *((_matches(title, words), name) for name, words in CATEGORY_RULES),
        else_=OTHER_CATEGORY,
    )


def category_filter(title: ColumnElement[str], categories: list[str]) -> ColumnElement[bool]:
    """按标题规则筛选类别（rule_category 还没算时用）。"""
    parts: dict[str, ColumnElement[bool]] = {}
    earlier: list[ColumnElement[bool]] = []
    for name, words in CATEGORY_RULES:
        hit = _matches(title, words)
        parts[name] = and_(hit, *(not_(e) for e in earlier))
        earlier.append(hit)
    parts[OTHER_CATEGORY] = and_(*(not_(e) for e in earlier))
    return or_(*(parts[c] for c in categories if c in parts))
