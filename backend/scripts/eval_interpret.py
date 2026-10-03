"""只读评测政策解读：不创建任务、不扣额度、不写用量表。

在 backend/ 下运行：uv run python scripts/eval_interpret.py --dry-run --out /tmp/interpret-eval
长度分层：短文 <=1500 字，中篇 <=6000 字，长文 >6000 字；来源按 ID 轮流取。
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict, deque
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from youju.ai import interpret
from youju.ai.client import ChatClient
from youju.config import get_settings
from youju.db import create_mysql_engine
from youju.models import Document, Source
from youju.takedown import available_condition


def length_band(length: int) -> int:
    return 0 if length <= 1500 else 1 if length <= 6000 else 2


def select_documents(session: Session, model: str, limit: int, doc_ids: list[int] | None) -> list:
    """先按来源及长度分桶，再确定性轮取；仅 prepare 通过的文件进入候选。"""
    stmt = (
        select(Document.id, Document.source_id)
        .join(Source, Source.id == Document.source_id)
        .where(available_condition(), Source.rights_level == 1)
        .order_by(Document.source_id, Document.id)
    )
    if doc_ids is not None:
        stmt = stmt.where(Document.id.in_(doc_ids))
    buckets = defaultdict(lambda: defaultdict(deque))
    for doc_id, source_id in session.execute(stmt):
        try:
            snapshot, _, _ = interpret.prepare(session, doc_id, model)
        except interpret.InterpretRefusal:
            continue
        body_length = snapshot["body_length"]
        buckets[source_id][length_band(body_length)].append(
            {"doc_id": doc_id, "source_id": source_id, "snapshot": snapshot}
        )
    selected = []
    sources = deque(sorted(buckets))
    while sources and len(selected) < limit:
        source_id = sources.popleft()
        bands = buckets[source_id]
        # 来源优先轮取，长度目标按短、中、长循环；缺一档时选择下一档。
        target = len(selected) % 3
        for band in ((target + offset) % 3 for offset in range(3)):
            if bands[band]:
                selected.append(bands[band].popleft())
                break
        if any(bands.values()):
            sources.append(source_id)
    return selected


def evaluate(selected: list, client: ChatClient | None) -> list[dict]:
    records = []
    for candidate in selected:
        snapshot = candidate["snapshot"]
        paragraphs = snapshot["paragraphs"]
        record = {
            "doc_id": candidate["doc_id"],
            "source_id": candidate["source_id"],
            "body_length": snapshot["body_length"],
            "paragraph_count": len(paragraphs),
            "attachments": snapshot["attachments"],
            "status": "dry_run" if client is None else "error",
            "returned_count": 0,
            "kept_count": 0,
            "dropped_reasons": dict.fromkeys(interpret.DROPPED_REASONS, 0),
            "type_counts": dict.fromkeys(interpret.TYPES, 0),
            "missing": list(interpret.TYPES),
            "items": [],
            "elapsed_seconds": 0.0,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "error_category": None,
        }
        if client is not None:
            started = time.monotonic()
            try:
                payload = client.complete_json(
                    interpret.SYSTEM_PROMPT,
                    json.dumps({"paragraphs": paragraphs}, ensure_ascii=False),
                    max_tokens=12000,
                )
                if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                    record["returned_count"] = len(payload["items"])
                result = interpret.validate(payload, paragraphs)
                record.update(
                    status="success",
                    kept_count=len(result["items"]),
                    items=result["items"],
                    dropped_reasons=result["dropped_reasons"],
                    missing=result["missing"],
                    refusal=result["refusal"],
                )
                for item in result["items"]:
                    record["type_counts"][item["type"]] += 1
            except Exception as exc:  # noqa: BLE001 - 单篇失败继续；只记录类别，不泄露响应或配置
                record["error_category"] = type(exc).__name__
            finally:
                record["elapsed_seconds"] = time.monotonic() - started
                record["usage"] = {
                    key: client.last_usage.get(key, 0)
                    for key in ("prompt_tokens", "completion_tokens")
                }
        records.append(record)
    return records


def summarize(records: list[dict]) -> dict:
    attempted = [r for r in records if r["status"] != "dry_run"]
    returned = sum(r["returned_count"] for r in records)
    kept = sum(r["kept_count"] for r in records)
    return {
        "doc_ids": [r["doc_id"] for r in records],
        "document_count": len(records),
        "attempted_count": len(attempted),
        "success_count": sum(r["status"] == "success" for r in records),
        "error_count": sum(r["status"] == "error" for r in records),
        "returned_count": returned,
        "kept_count": kept,
        "retention_rate": kept / returned if returned else 0.0,
        "dropped_reasons": {
            reason: sum(r["dropped_reasons"][reason] for r in records)
            for reason in interpret.DROPPED_REASONS
        },
        "type_coverage": {
            kind: sum(r["type_counts"][kind] > 0 for r in records) for kind in interpret.TYPES
        },
        "average_elapsed_seconds": (
            sum(r["elapsed_seconds"] for r in attempted) / len(attempted) if attempted else 0.0
        ),
        "usage": {
            key: sum(r["usage"][key] for r in records)
            for key in ("prompt_tokens", "completion_tokens")
        },
    }


def write_outputs(out: Path, records: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(records)
    with (out / "results.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 政策解读评测",
        "",
        "选中文件 ID：" + ", ".join(map(str, summary["doc_ids"])),
        "",
        "保留率 = 保留条数 / 模型返回条数；平均耗时含调用失败，不含 dry-run。",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
    ]
    for label, key in (
        ("篇数", "document_count"),
        ("调用篇数", "attempted_count"),
        ("成功篇数", "success_count"),
        ("失败篇数", "error_count"),
        ("模型返回总条数", "returned_count"),
        ("保留总条数", "kept_count"),
    ):
        lines.append(f"| {label} | {summary[key]} |")
    lines.extend(
        [
            f"| 保留率 | {summary['retention_rate']:.2%} |",
            f"| 平均耗时（秒） | {summary['average_elapsed_seconds']:.3f} |",
            f"| 输入 tokens | {summary['usage']['prompt_tokens']} |",
            f"| 输出 tokens | {summary['usage']['completion_tokens']} |",
            "",
            "| 丢弃原因 | 条数 |",
            "|---|---:|",
        ]
    )
    labels = dict(
        not_found="未找到",
        ambiguous="多处命中",
        too_short="引用过短",
        redacted="涉及脱敏",
        bad_shape="条目格式错误",
        duplicate="重复条目",
    )
    lines.extend(f"| {labels[k]}（{k}） | {v} |" for k, v in summary["dropped_reasons"].items())
    lines.extend(["", "| 类别 | 覆盖篇数 |", "|---|---:|"])
    lines.extend(f"| {k} | {v} |" for k, v in summary["type_coverage"].items())
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("limit 必须大于 0")
    return number


def parse_doc_ids(value: str) -> list[int]:
    try:
        ids = sorted({int(part.strip()) for part in value.split(",")})
    except ValueError as exc:
        raise argparse.ArgumentTypeError("doc-ids 必须是逗号分隔的正整数") from exc
    if not ids or min(ids) < 1:
        raise argparse.ArgumentTypeError("doc-ids 必须是逗号分隔的正整数")
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=positive_int, default=30)
    parser.add_argument("--out", type=Path, default=Path("interpret-eval"))
    parser.add_argument("--doc-ids", type=parse_doc_ids)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    client = None
    engine = None
    try:
        settings = get_settings()
        if not args.dry_run and not settings.ai_api_key:
            print("没有配置 AI 密钥，只能用 --dry-run")
            return 1
        engine = create_mysql_engine(settings.database_url)
        with engine.connect() as connection:
            # 数据库层强制只读，防止今后 prepare 的变动意外写表。
            connection.execute(text("START TRANSACTION READ ONLY"))
            with Session(bind=connection, autoflush=False) as session:
                selected = select_documents(session, settings.ai_model, args.limit, args.doc_ids)
            connection.rollback()
        if not args.dry_run:
            client = ChatClient(
                settings.ai_base_url,
                settings.ai_api_key.get_secret_value(),
                settings.ai_model,
                max_retries=0,
            )
        records = evaluate(selected, client)
        write_outputs(args.out, records)
        print(f"已选 {len(records)} 篇，结果写入输出目录")
        return 0
    except Exception as exc:  # noqa: BLE001 - 不打印可能含连接配置的异常原文
        print(f"评测失败：{type(exc).__name__}")
        return 1
    finally:
        if client is not None:
            client.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
