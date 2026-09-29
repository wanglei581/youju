"""模型辅助核对支持方式和申报对象；所有证据必须来自正文原句。"""

from __future__ import annotations

import re

from youju.fields import SUPPORT_LABELS, FieldCandidate

from .client import ChatClient

_SYSTEM = """你是政府申报通知字段核对助手。只返回 JSON 对象，字段 fields 为数组。
每项字段 field 为 support 或 audience，value 为固定支持方式标签或申报对象原文摘录，
evidence 必须是正文逐字连续原句。不要改写，不确定就不输出。support 只能使用：资金支持、
贷款贴息、资格认定、入库备案、项目立项、其他。"""


def _prepare(text: str) -> str:
    if len(text) <= 7000:
        return text
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    picked = [
        p
        for p in parts
        if re.search(r"支持|贴息|认定|入库|备案|立项|申报对象|申报主体|申报单位", p)
    ]
    return "".join(picked)[:7000]


def extract_fields_with_ai(client: ChatClient, title: str, text: str) -> list[FieldCandidate]:
    result = client.complete_json(_SYSTEM, f"标题：{title}\n正文：{_prepare(text)}")
    items = result.get("fields", [])
    if not isinstance(items, list):
        return []
    out: list[FieldCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        value = str(item.get("value", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if field not in ("support", "audience") or not value or not evidence:
            continue
        if field == "support" and value not in SUPPORT_LABELS:
            continue
        if field == "audience" and len(value) > 200:
            value = value[:200]
        if len(evidence) > 300 or evidence not in text:
            continue
        if field == "audience" and len(value) > 200:
            continue
        offset = text.find(evidence)
        if offset < 0:
            continue
        out.append(FieldCandidate(field, value, evidence, offset))
    return out
