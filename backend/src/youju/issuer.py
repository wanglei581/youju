"""发文机关展示名整理；保留数据库里的原始落款。"""

import re

_SPACED_CHARS = re.compile(
    r"(?<![\u4e00-\u9fff])(?:[\u4e00-\u9fff]\s+){2,}[\u4e00-\u9fff](?=\s|$|[、，。])"
)
_BRANCH_AGENCIES = (
    ("国家税务总局", "税务局"),
    ("国家金融监督管理总局", "监管局"),
    ("中国人民银行", "分行"),
    ("国家外汇管理局", "分局"),
    ("海关总署", "海关"),
)


# 机关名常见的结尾字：空格在这些字后面，才是两个联合发文机关之间的分隔；
# 其他位置的空格（如名称中间的排版空格）不当分隔，原样保留
_AGENCY_TAIL = "厅局部委署关行室会心府院组司处站所办"


def normalize_issuer(value: str) -> str:
    """合并落款字间空格（"安 徽 省 财 政 厅"），并用顿号分隔联合发文机关。"""
    value = value.strip()
    value = _SPACED_CHARS.sub(lambda match: re.sub(r"\s+", "", match.group()), value)
    for parent, branch in _BRANCH_AGENCIES:
        value = re.sub(
            rf"({parent})\s+(?=[\u4e00-\u9fff]+{branch}(?:\s|、|$))",
            r"\1",
            value,
        )
    return re.sub(rf"(?<=[{_AGENCY_TAIL}])\s+(?=[\u4e00-\u9fff])", "、", value)
