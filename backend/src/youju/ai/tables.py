"""用 AI 从脱敏后的表格样本中识别企业或机构名称列（规则按表头找不到名称列时才用）。

隐私：表头像个人信息的整列不发；没有表头、但多数值像人名（两到四个汉字、不带机构用词）的整列也不发；
其余单元格脱敏（手机号、身份证、邮箱、银行账号、电话、联系人姓名）后截到 40 字，只发前 10 行。
"""

from youju.crawler.detail import redact_sensitive
from youju.lists.rules import is_person_query, valid_org_name

from .client import ChatClient

# 表头里有这些词的列当作个人信息，不发给 AI。比解析器的清单多了同义词
# （法人代表、手机、邮箱、证件、住址等）；
# 不收"法人"单独一词，免得把"法人单位名称"这类机构名称列也去掉
PERSON_HEADERS = (
    "联系人",
    "法定代表人",
    "法人代表",
    "负责人",
    "姓名",
    "电话",
    "手机",
    "身份证",
    "证件",
    "银行账号",
    "银行卡",
    "邮箱",
    "电子邮件",
    "联系方式",
    "住址",
)

_SYSTEM = """你是政府名单表格分析助手。必须只返回 JSON 对象。
从保留列中选择“企业或机构名称”列，个人姓名列、项目名称列、地区列都不算。
表头可能缺失，拿不准时返回 null。column 是保留列的 0 起始序号，找不到时为 null；
reason 是不超过 40 字的简短理由。返回格式：{"column": 0, "reason": "..."}。"""


def _text(value: object) -> str:
    """按隐私要求脱敏并限制单元格长度。"""
    return redact_sensitive(str(value) if value is not None else "")[:40]


def _mostly_person_names(rows: list[list[str]], index: int) -> bool:
    """这一列前 10 个非空值里，一半以上像人名：当作个人信息列，不发给 AI。"""
    values = [
        str(row[index]).strip()
        for row in rows
        if index < len(row) and row[index] is not None and str(row[index]).strip()
    ][:10]
    return bool(values) and sum(is_person_query(v) for v in values) * 2 >= len(values)


def pick_name_column(
    client: ChatClient, title: str, headers: list[str], rows: list[list[str]]
) -> int | None:
    """请 AI 识别名称列，并用本地企业名称规则复核结果。"""
    if len(rows) < 2:
        return None

    width = max([len(headers), *(len(row) for row in rows)], default=0)
    normalized_headers = [str(headers[i]) if i < len(headers) else "" for i in range(width)]
    removed = {
        i
        for i, header in enumerate(normalized_headers)
        # 表头去掉空白再比（"联 系 人"这类排版常见）
        if any(term in "".join(header.split()) for term in PERSON_HEADERS)
        or _mostly_person_names(rows, i)
    }
    kept = [i for i in range(width) if i not in removed]
    if not kept:
        return None

    kept_headers = [normalized_headers[i] for i in kept]
    sample_rows: list[list[str]] = []
    for row in rows:
        values = [_text(row[i] if i < len(row) else "") for i in kept]
        if any(values):
            sample_rows.append(values)
        if len(sample_rows) >= 10:
            break
    table_lines = ["\t".join(f"[{i}] {header}" for i, header in enumerate(kept_headers))]
    table_lines.extend("\t".join(values) for values in sample_rows)
    prompt = f"标题：{_text(title)}\n表格（列序号从 0 开始）：\n" + "\n".join(table_lines)
    result = client.complete_json(_SYSTEM, prompt)
    selected = result.get("column")
    if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(kept):
        return None

    original_index = kept[selected]
    values: list[str] = []
    for row in rows[:10]:
        if original_index < len(row):
            value = str(row[original_index] if row[original_index] is not None else "").strip()
            if value:
                values.append(value)
    if not values:
        return None
    passed = sum(valid_org_name(value) for value in values)
    return original_index if passed / len(values) >= 0.6 else None
