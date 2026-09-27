"""保守的名单标题和企业名称规则。"""

import re
import unicodedata
from dataclasses import dataclass

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
    "申报",
    "申请",
    "征集",
)
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
    is_list: bool
    category: str = "未确定"
    status: str = "未确定"
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


def classify_title(title: str) -> ListClassification:
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
    status = (
        "公示"
        if "公示" in value or "拟" in value
        else "正式公布"
        if any(term in value for term in ("公告", "公布", "认定", "入库", "备案", "通过"))
        else "未确定"
    )
    match = _BATCH.search(value)
    batch = f"第{match.group(1) or match.group(2)}批" if match else None
    return ListClassification(True, category, status, batch)


def candidate_key(value: str) -> str:
    """只用于单独展示近似候选；不能当作企业身份相同的证据。"""
    normalized = re.sub(r"\([^)]{1,20}\)", "", normalize_name(value))
    return re.sub(r"(?:有限责任公司|股份有限公司|有限公司|集团公司|公司)$", "", normalized)
