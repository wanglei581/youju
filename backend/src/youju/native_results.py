"""数据工具生成时同步保存站内结果；和 Excel 使用同一个 report，不重跑查询。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from youju.compare import (
    COMPARE_SUMMARY_HEADERS,
    CompareInputError,
    compare_summary_rows,
    removed_input_counts,
)

MAX_BYTES = 8 * 1024 * 1024
LABELS = {
    "input": "客户输入",
    "normalized": "比对用的名称",
    "status": "结果",
    "note": "说明",
    "publish_date": "发布日期",
    "issuer": "发文机关",
    "province": "省份",
    "city": "城市",
    "district": "区县",
    "level": "层级",
    "name": "名单中名称",
    "credit_code": "统一社会信用代码",
    "batch": "批次",
    "sequence": "名单序号",
    "region": "名单中企业地区",
    "doc_number": "文号",
    "url": "来源地址",
    "list_type": "名单类型",
    "year": "年度",
    "customer_name": "客户名称",
    "deadline_type": "截止类型",
    "deadline_date": "截止日期",
    "deadline_time": "截止时间",
    "deadline_status": "截止核对情况",
    "deadline_quote": "截止原文句子",
    "remaining_days": "生成时剩余天数",
    "support": "支持方式",
}


def _fields(data: dict, labels: dict | None = None) -> list[dict]:
    labels = labels or LABELS
    return [
        {"label": label, "value": str(data[key])}
        for key, label in labels.items()
        if data.get(key) is not None and data[key] != ""
    ]


def _summary_items(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> list[dict]:
    return [
        {
            "documentId": None,
            "title": "汇总",
            "fields": [
                {"label": label, "value": str(value) if value is not None and value != "" else "—"}
                for label, value in zip(headers, row, strict=True)
            ],
        }
        for row in rows
    ]


def build(kind: str, report, *, generated_at: datetime | None = None) -> dict:
    """仅保留结果里本来就有的字段；不复制上传文件或整份原始输入。"""
    data = report.as_dict()
    items = []
    notice = "第三方整理，非政府平台。未查到仅指收录范围内未找到，不代表未入选。"
    if kind == "compare":
        title = "企业名单比对结果"
        generated = data.get("data_version")
        notice += "发布阶段按标题用词标注；名单序号不是附件页码。"
        removed = removed_input_counts(report)
        if removed:
            notice += "已去掉：" + "、".join(
                f"{reason}{count}行" for reason, count in removed.items()
            )
        items.extend(_summary_items(COMPARE_SUMMARY_HEADERS, compare_summary_rows(report)))
        for row in data["rows"]:
            for match in row["matches"] or [None]:
                fields = _fields(
                    row, {k: LABELS[k] for k in ("input", "normalized", "status", "note")}
                )
                if match:
                    labels = dict(LABELS, status="发布阶段")
                    fields += _fields(match, labels)
                items.append(
                    {
                        "documentId": str(match["document_id"]) if match else None,
                        "title": match["title"] if match else row["status"],
                        "fields": fields,
                    }
                )
    elif kind == "track":
        from youju.track import TRACK_SUMMARY_HEADERS, _scope_note, track_summary_rows

        title = "客户相关通知表"
        generated = data.get("generated_at")
        notice += "仅查近90天申报通知，已确认超过截止时间的通知不列；机器摘录未经人工核对。"
        notice += "7天内只计人工核对的日期，14/30天另含机器摘录；截止未核对的也可能快截止。"
        items.extend(_summary_items(TRACK_SUMMARY_HEADERS, track_summary_rows(report)))
        for client in report.clients:
            for match in client.items or [None]:
                fields = [
                    {"label": "客户名称", "value": client.customer_name},
                    {
                        "label": "客户地区",
                        "value": " / ".join(
                            filter(
                                None,
                                (
                                    client.profile.province,
                                    client.profile.city,
                                    client.profile.district,
                                ),
                            )
                        ),
                    },
                    {"label": "查询说明", "value": _scope_note(client.profile)},
                ]
                if match:
                    fields += _fields(
                        match.as_dict(), {k: v for k, v in LABELS.items() if k != "customer_name"}
                    )
                items.append(
                    {
                        "documentId": str(match.document_id) if match else None,
                        "title": match.title if match else "本次收录范围内未找到相关通知",
                        "fields": fields,
                    }
                )
    else:
        raise CompareInputError("不支持的站内结果类型")
    for n, item in enumerate(items):
        item["n"] = n
        if len(json.dumps(item, ensure_ascii=False).encode()) > 200 * 1024:
            raise CompareInputError("单项结果内容过长，请减少条件后重新生成")
    content = {
        "schema": 1,
        "kind": kind,
        "title": title,
        "notice": notice,
        "generatedAt": (generated_at or datetime.now(UTC)).isoformat(),
        "dataVersion": generated,
        "items": items,
    }
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    if len(encoded) > MAX_BYTES:
        raise CompareInputError("结果内容过多，请减少客户数量后重新生成")
    return {**content, "revision": hashlib.sha256(encoded).hexdigest()}
