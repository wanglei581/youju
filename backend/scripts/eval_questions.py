"""AI 解读 30 题评测（第 6 道门槛）：固定的题目和金标准，看模型的解读能不能答到。

只读：不建任务、不扣额度、不写用量表，数据库按只读事务打开。在 backend/ 下运行：
  uv run python scripts/eval_questions.py --check                  只核对题目集和库，不调模型
  uv run python scripts/eval_questions.py --fake-model --out 目录   假模型走一遍（自检，不联网）
  uv run python scripts/eval_questions.py --out 目录                调真实模型
真实模型的线路只认环境变量 YOUJU_AI_ADVISOR_*（不读 backend/.env）。创始人用
tools/team/run-eval-hy3.sh 跑：脚本问密钥、设线路，只交给这一个进程。

做法：题目集里每篇通知只调一次模型（几道题共用一篇时不重复调）。提示词、段落、max_tokens 和线上
解读一样（eval_interpret.evaluate_one），interpret.validate 逐字核对以后，给每道题打分。

答到的判定（overlaps）：两边先用逐字核对的同一套规范化（interpret.normalize，全角转半角），
再去掉全部空白，然后
  1. 保留条目的原句包含金句，算答到；
  2. 金句包含保留条目的原句，而且这句至少有金句 60% 长，也算答到；
  3. 其他情况（只是共用一个日期或词组）不算。
理由：validate 总把引用扩成整句，金句也是用同一套断句（content.locate）取的整句，真答到时两边
一样长，或者保留的更长（模型一次引了几句）。第 2 条只给断句不同留余地（以后断句规则改了、PDF
折行接句不同）。60% 挡得住“三、申报时间”这种小标题和“附件：”这种半截。只共用日期不算，免得
“企业填报截止”和“区县推荐截止”碰巧同一天时混成一题。
类别对上才算通过：原句答到了、类别不对的另计“类别不对”。need=all 的题（材料清单这类），
每一句金句都要答到。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from youju.ai import advisor_endpoint, content, interpret
from youju.ai.client import ChatClient
from youju.config import Settings
from youju.db import create_mysql_engine
from youju.models import Document


def _sibling(name: str):
    """同目录的评测脚本（scripts 不是包，按文件路径载入）。"""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_interpret = _sibling("eval_interpret")

GOLD = Path(__file__).resolve().parents[1] / "eval" / "interpret_30.json"
TYPES = interpret.INTERPRET.types
BANDS = ("短文", "中篇", "长文")  # eval_interpret.length_band：<=1500、<=6000、>6000 字
MIN_SHARE = 0.6
# 云开发网关：不走流式、1 分钟内没生成完的请求不返回结果（D-098）
GATEWAY_SECONDS = 60
# hy3 按云开发资源点计：每百万 tokens 输入 1000 点、输出 4000 点，1000 点约合 1 元
# （10/28 统筹给的口径，以云开发控制台为准）
INPUT_POINTS = 1000
OUTPUT_POINTS = 4000
POINTS_PER_YUAN = 1000
# 候选门槛，创始人拍板前只是建议
BAR_PASS_RATE = 0.9
BAR_JSON_INVALID = 1
MAX_CONCURRENCY = 3
BEIJING = timezone(timedelta(hours=8))
STATUS_LABELS = {
    "pass": "答对",
    "wrong_type": "类别不对",
    "partial": "没答全",
    "miss": "没答到",
    "error": "调用失败",
}


class GoldError(ValueError):
    """题目集和库对不上：先修题目集（或重建库），不调模型。文字只含题号、通知号这些自己写的内容。"""


def load_settings() -> Settings:
    # 只认环境变量：runner 传进来的线路就是要评测的线路，backend/.env 里别的配置不掺进来
    return Settings(_env_file=None)


def load_gold(path: Path) -> dict:
    """读题目集并检查结构：题号不重复、类别是解读的六类、每题至少一句金句，
    通知都列在 documents 里。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    documents = data.get("documents")
    questions = data.get("questions")
    if not isinstance(documents, list) or not isinstance(questions, list) or not questions:
        raise GoldError("题目集要有 documents 和 questions 两个列表")
    doc_ids = [d.get("doc_id") for d in documents]
    if len(set(doc_ids)) != len(doc_ids) or not all(d.get("url_key") for d in documents):
        raise GoldError("documents 里通知号重复或缺网址键")
    ids = [q.get("id") for q in questions]
    if len(set(ids)) != len(ids):
        raise GoldError("题号重复")
    for q in questions:
        if q.get("type") not in TYPES:
            raise GoldError(f"{q.get('id')} 的类别不是解读的六类之一")
        # also：两可的题另外也算对的类别（agy 10/28 复核：比如"打印几份、逐级寄送"算材料也算方式）
        if not all(kind in TYPES for kind in q.get("also", [])):
            raise GoldError(f"{q.get('id')} 的 also 里有不是六类之一的类别")
        if q.get("need", "any") not in ("any", "all"):
            raise GoldError(f"{q['id']} 的 need 只能是 any 或 all")
        if q.get("doc_id") not in doc_ids:
            raise GoldError(f"{q['id']} 的通知没列在 documents 里")
        gold = q.get("gold")
        if not isinstance(gold, list) or not gold:
            raise GoldError(f"{q['id']} 没有金句")
        for g in gold:
            if not isinstance(g, dict) or not isinstance(g.get("sid"), str):
                raise GoldError(f"{q['id']} 的金句缺段号")
            if not isinstance(g.get("quote"), str) or len(normalized(g["quote"])) < 6:
                raise GoldError(f"{q['id']} 的金句太短")
            # 逐字核对会丢掉含脱敏内容的句子，这样的句子永远答不到
            if "已脱敏]" in g["quote"]:
                raise GoldError(f"{q['id']} 的金句含脱敏内容")
    return data


def normalized(value: str) -> str:
    """逐字核对的规范化（全角转半角、空白并成一个空格），再去掉空白：PDF 折行不影响比较。"""
    return interpret.normalize(value)[0].replace(" ", "")


def overlaps(kept: str, gold: str) -> bool:
    """保留条目的原句答没答到这句金句（规则和理由见模块说明）。"""
    kept, gold = normalized(kept), normalized(gold)
    if not kept or not gold:
        return False
    if gold in kept:
        return True
    return kept in gold and len(kept) >= MIN_SHARE * len(gold)


def find_gold(paragraphs: list[dict], sid: str, quote: str) -> str | None:
    """金句开头所在的段号；不在正文里给 None。

    金句必须原样出现在 prepare 段落拼回的正文里（interpret.paragraph_body，跨段的 PDF 折行句
    中间是换行）。sid 那一段开头的就用 sid；库刷新以后重新分段、原句还在，给它现在的段号。"""
    body = interpret.paragraph_body(paragraphs)
    found = []
    at = body.find(quote)
    while at >= 0:
        found.extend(p["pid"] for p in paragraphs if p["start"] <= at < p["end"])
        at = body.find(quote, at + 1)
    if not found:
        return None
    return sid if sid in found else found[0]


def resolve(session: Session, gold: dict, model: str) -> tuple[list[dict], list[str]]:
    """按题目集找通知、取快照（和线上一样先 content.check 再 interpret.prepare）、核对金句。

    通知号对不上网址键（库重建过）时按网址键找。找不到、不能解读、金句不在正文里都抛 GoldError，
    不调模型。返回（每篇通知一个候选，提醒）。"""
    problems: list[str] = []
    notes: list[str] = []
    candidates: list[dict] = []
    for doc in gold["documents"]:
        key = doc["doc_id"]
        row = session.get(Document, key)
        if row is None or row.url_key != doc["url_key"]:
            row = session.scalar(select(Document).where(Document.url_key == doc["url_key"]))
            if row is None:
                problems.append(f"通知 {key} 在库里找不到")
                continue
            notes.append(f"通知 {key} 按网址键对上了库里的 {row.id}")
        try:
            content.check(session, row.id)
            snapshot, _, _ = interpret.prepare(session, row.id, model)
        except interpret.InterpretRefusal as exc:
            problems.append(f"通知 {key} 现在不能解读（{exc.code or '其他原因'}）")
            continue
        if doc.get("body_hash") and doc["body_hash"] != snapshot["body_hash"]:
            # 正文变了不一定作废：金句都还在就照样评，只提醒一声，分数和出题时不完全可比
            notes.append(f"通知 {key} 的正文和出题时不同")
        for q in gold["questions"]:
            if q["doc_id"] != key:
                continue
            for g in q["gold"]:
                sid = find_gold(snapshot["paragraphs"], g["sid"], g["quote"])
                if sid is None:
                    problems.append(f"{q['id']} 的金句不在通知 {key} 的正文里")
                elif sid != g["sid"]:
                    notes.append(f"{q['id']} 的金句段号 {g['sid']} 现在是 {sid}")
                    g["sid"] = sid
        candidates.append(
            {"key": key, "doc_id": row.id, "source_id": row.source_id, "snapshot": snapshot}
        )
    if problems:
        raise GoldError("；".join(problems))
    return candidates, notes


def say(line: str) -> None:
    print(line, flush=True)


def run_calls(
    candidates: list[dict],
    factory: Callable[[], object],
    concurrency: int,
    progress: Callable[[str], None] = say,
) -> list[dict]:
    """每篇通知调一次模型，最多 concurrency 篇同时跑；每篇用自己的客户端（用量各记各的）。
    结果按题目集里通知的顺序排。"""

    def one(candidate: dict) -> dict:
        client = factory()
        try:
            return eval_interpret.evaluate_one(candidate, client)
        finally:
            client.close()

    records: list[dict | None] = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(one, c): i for i, c in enumerate(candidates)}
        for done, future in enumerate(as_completed(futures), 1):
            index = futures[future]
            record = future.result()
            records[index] = record
            state = "成功" if record["status"] == "success" else f"失败（{record['error_code']}）"
            progress(
                f"进度 {done}/{len(candidates)}：通知 {candidates[index]['key']} "
                f"用时 {record['elapsed_seconds']:.1f} 秒，{state}"
            )
    return records


def score(question: dict, record: dict) -> dict:
    """给一道题打分（overlaps 看原句，再看类别）。"""
    items = record.get("items") or []
    reached = [
        sorted(
            {
                item["type"]
                for item in items
                if any(overlaps(ref["quote"], g["quote"]) for ref in item["refs"])
            }
        )
        for g in question["gold"]
    ]
    accepted = {question["type"], *question.get("also", [])}
    typed = [bool(accepted & set(kinds)) for kinds in reached]
    need_all = question.get("need", "any") == "all"
    passed = all(typed) if need_all else any(typed)
    any_type = [bool(kinds) for kinds in reached]
    if passed:
        status = "pass"
    elif record["status"] != "success":
        status = "error"
    elif all(any_type) if need_all else any(any_type):
        status = "wrong_type"
    elif any(any_type):
        status = "partial"
    else:
        status = "miss"
    return {
        "id": question["id"],
        "type": question["type"],
        "need": question.get("need", "any"),
        "pass": passed,
        "status": status,
        "covered": sum(typed),
        "gold_count": len(question["gold"]),
        "reached_types": sorted({kind for kinds in reached for kind in kinds}),
    }


def percentile(values: list[float], share: float) -> float:
    """最近秩法：从小到大排，取第 ceil(share·n) 个。
    n=17 时 p50 是第 9 个，p95 是第 17 个（最大的那个）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(1, math.ceil(share * len(ordered))) - 1]


def points(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * INPUT_POINTS + completion_tokens * OUTPUT_POINTS) / 1_000_000


def summarize(records: list[dict], scores: list[dict]) -> dict:
    attempted = [r for r in records if r["status"] != "dry_run"]
    elapsed = [r["elapsed_seconds"] for r in attempted]
    prompt = sum(r["usage"]["prompt_tokens"] for r in records)
    completion = sum(r["usage"]["completion_tokens"] for r in records)
    total_points = points(prompt, completion)
    by_type = {}
    for kind in TYPES:
        rows = [s for s in scores if s["type"] == kind]
        passed = sum(s["pass"] for s in rows)
        by_type[kind] = {
            "count": len(rows),
            "pass": passed,
            "rate": passed / len(rows) if rows else 0.0,
            "wrong_type": sum(s["status"] == "wrong_type" for s in rows),
            "failed": [s["id"] for s in rows if not s["pass"]],
        }
    passed = sum(s["pass"] for s in scores)
    over = [r for r in attempted if r["elapsed_seconds"] > GATEWAY_SECONDS]
    summary = {
        "question_count": len(scores),
        "pass_count": passed,
        "pass_rate": passed / len(scores) if scores else 0.0,
        "by_type": by_type,
        "status_counts": dict(Counter(s["status"] for s in scores)),
        "failed": [{"id": s["id"], "status": s["status"]} for s in scores if not s["pass"]],
        "notice_count": len(records),
        "attempted_count": len(attempted),
        "success_count": sum(r["status"] == "success" for r in records),
        "error_count": sum(r["status"] == "error" for r in records),
        "error_codes": dict(Counter(r["error_code"] for r in records if r["error_code"])),
        "json_invalid_count": sum(r["json_valid"] is False for r in records),
        "model_refusal_count": sum(bool(r["model_refusal"]) for r in records),
        "empty_after_check_count": sum(bool(r.get("refusal")) for r in records),
        "returned_count": sum(r["returned_count"] for r in records),
        "kept_count": sum(r["kept_count"] for r in records),
        "dropped_reasons": {
            reason: sum(r["dropped_reasons"][reason] for r in records)
            for reason in interpret.DROPPED_REASONS
        },
        "latency": {
            "p50": percentile(elapsed, 0.5),
            "p95": percentile(elapsed, 0.95),
            "max": max(elapsed, default=0.0),
            "mean": sum(elapsed) / len(elapsed) if elapsed else 0.0,
            "over_gateway": len(over),
            "over_gateway_doc_ids": [r["doc_id"] for r in over],
        },
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reported": bool(prompt or completion),
        },
        "cost": {
            "points": total_points,
            "yuan": total_points / POINTS_PER_YUAN,
            "points_per_notice": total_points / len(attempted) if attempted else 0.0,
            "yuan_per_notice": (
                total_points / len(attempted) / POINTS_PER_YUAN if attempted else 0.0
            ),
        },
    }
    summary["bars"] = bars(summary)
    checked = [b["ok"] for b in summary["bars"] if b["ok"] is not None]
    if not attempted:
        summary["verdict"] = "未调用模型"
    else:
        summary["verdict"] = "PASS" if all(checked) else "FAIL"
    return summary


def bars(summary: dict) -> list[dict]:
    """候选门槛（创始人定）：总通过率、时间类全对、JSON 无效次数、60 秒网关，费用只报告。"""
    time_row = summary["by_type"]["时间"]
    latency = summary["latency"]
    exceptions = "、".join(time_row["failed"])
    return [
        {
            "key": "pass_rate",
            "label": f"总通过率 ≥ {BAR_PASS_RATE:.0%}",
            "actual": (
                f"{summary['pass_count']}/{summary['question_count']}（{summary['pass_rate']:.0%}）"
            ),
            "ok": bool(summary["question_count"]) and summary["pass_rate"] >= BAR_PASS_RATE,
        },
        {
            "key": "time_all",
            "label": "时间类全对（截止日期最要紧）",
            "actual": f"{time_row['pass']}/{time_row['count']}"
            + (f"，例外：{exceptions}" if exceptions else ""),
            "ok": bool(time_row["count"]) and time_row["pass"] == time_row["count"],
        },
        {
            "key": "json_invalid",
            "label": f"JSON 无效 ≤ {BAR_JSON_INVALID} 次（按调用次数算）",
            "actual": f"{summary['json_invalid_count']}/{summary['attempted_count']}",
            "ok": summary["json_invalid_count"] <= BAR_JSON_INVALID,
        },
        {
            "key": "gateway",
            "label": f"没有超过 {GATEWAY_SECONDS} 秒的请求",
            "actual": f"{latency['over_gateway']} 次（最长 {latency['max']:.1f} 秒）",
            "ok": latency["over_gateway"] == 0,
            "note": "不达标：上线前必须打开流式（YOUJU_AI_ADVISOR_STREAM=true）",
        },
        {
            "key": "cost",
            "label": "平均每篇费用（只报告，不设门槛）",
            "actual": (
                f"{summary['cost']['points_per_notice']:.1f} 点"
                f"（约 {summary['cost']['yuan_per_notice']:.4f} 元）"
                if summary["usage"]["reported"]
                else "接口没返回用量"
            ),
            "ok": None,
        },
    ]


class FakeModel:
    """自检用的假模型，不联网：按金句给引用（只给开头一截，让逐字核对去扩成整句），另加两条该丢的
    引用（原文里没有的、太短的）；故意漏掉最后一题、把倒数第二题归错类别，报告里“没答到”
    “类别不对”两种情形也走一遍。用量按字数粗估。"""

    def __init__(self, plan: dict[str, list[dict]]):
        self.plan = plan
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def complete_json(self, system: str, user: str, max_tokens: int) -> dict:
        paragraphs = json.loads(user)["paragraphs"]
        payload = {"items": self.plan[interpret.digest(paragraphs)], "missing": [], "refusal": None}
        self.last_usage = {
            "prompt_tokens": len(system + user) // 2,
            "completion_tokens": len(json.dumps(payload, ensure_ascii=False)) // 2,
        }
        return payload

    def close(self) -> None:
        pass


def fake_plan(gold: dict, candidates: list[dict]) -> dict[str, list[dict]]:
    questions = gold["questions"]
    skipped = questions[-1]["id"]
    mistyped = questions[-2]["id"] if len(questions) > 1 else None
    plan = {}
    for candidate in candidates:
        items = []
        for q in questions:
            if q["doc_id"] != candidate["key"] or q["id"] == skipped:
                continue
            kind = q["type"]
            if q["id"] == mistyped:
                kind = next(t for t in TYPES if t != q["type"])
            for g in q["gold"]:
                head = g["quote"].split("\n", 1)[0][:30]
                items.append(
                    {"type": kind, "text": head, "refs": [{"pid": g["sid"], "quote": head}]}
                )
        items.append(
            {
                "type": "材料",
                "text": "这一句原文里没有，自检用",
                "refs": [{"pid": "P001", "quote": "这一句原文里没有，自检用"}],
            }
        )
        items.append({"type": "时间", "text": "附件", "refs": [{"pid": "P001", "quote": "附件"}]})
        plan[interpret.digest(candidate["snapshot"]["paragraphs"])] = items
    return plan


def band(length: int) -> str:
    return BANDS[eval_interpret.length_band(length)]


def short(value: str, limit: int = 60) -> str:
    flat = "".join(value.split("\n"))
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def gateway_label(base_url: str) -> str:
    """报告里只写是哪类线路，不写环境 ID。"""
    host = urlsplit(base_url).hostname or ""
    return "云开发网关" if host.endswith(".tcloudbasegateway.com") else "其他地址"


def write_outputs(
    out: Path, gold: dict, records: list[dict], scores: list[dict], summary: dict, meta: dict
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    docs = {d["doc_id"]: d for d in gold["documents"]}
    questions = {q["id"]: q for q in gold["questions"]}
    by_doc: dict[int, list[dict]] = {}
    for s in scores:
        by_doc.setdefault(questions[s["id"]]["doc_id"], []).append(s)
    with (out / "records.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            doc = docs[record["gold_doc_id"]]
            line = {
                **record,
                "url_key": doc["url_key"],
                "title": doc["title"],
                "province": doc.get("province"),
                "band": band(record["body_length"]),
                "questions": by_doc.get(record["gold_doc_id"], []),
            }
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")
    (out / "summary.json").write_text(
        json.dumps({"meta": meta, **summary}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "report.md").write_text(report(gold, records, scores, summary, meta), encoding="utf-8")


def report(gold: dict, records: list[dict], scores: list[dict], summary: dict, meta: dict) -> str:
    docs = {d["doc_id"]: d for d in gold["documents"]}
    questions = {q["id"]: q for q in gold["questions"]}
    record_by_doc = {r["gold_doc_id"]: r for r in records}
    verdict = {"PASS": "通过", "FAIL": "不通过"}.get(summary["verdict"], summary["verdict"])
    lines = ["# AI 解读 30 题评测", ""]
    if meta["mode"] == "fake":
        lines += [
            "> **假模型自检，不是 hy3 的成绩**：只说明题目集、逐字核对、打分和报告都走得通。",
            "",
        ]
    stream = "开" if meta["stream"] else "关"
    setup = (
        f"{meta['route']}；流式{stream}；并发 {meta['concurrency']}；"
        f"max_tokens {meta['max_tokens']}；提示词 {meta['prompt_version']}"
    )
    data = f"{gold.get('snapshot', '')}；题目集 {meta['gold_file']}（第 {gold.get('version')} 版）"
    lines += [
        f"- 候选门槛：**{summary['verdict']}（{verdict}）**（门槛是建议，由创始人定）",
        f"- 题目：{gold.get('questions_by', '')}",
        f"- 金标准：{gold.get('gold_by', '')}",
        f"- 模型：{meta['model']}（{setup}）",
        f"- 数据：{data}",
        f"- 运行：{meta['started_at']} 到 {meta['finished_at']}（北京时间）",
        f"- 规模：{summary['question_count']} 题，{summary['notice_count']} 篇通知，"
        f"每篇调用 1 次（共 {summary['attempted_count']} 次）",
        "",
        "## 候选门槛",
        "",
        "| 门槛 | 实际 | 结果 |",
        "|---|---|---|",
    ]
    for bar in summary["bars"]:
        result = "只报告" if bar["ok"] is None else "通过" if bar["ok"] else "不通过"
        if bar["ok"] is False and bar.get("note"):
            result += f"：{bar['note']}"
        lines.append(f"| {bar['label']} | {bar['actual']} | {result} |")
    lines += ["", "## 按类别", "", "| 类别 | 题数 | 答对 | 通过率 | 类别不对 | 没答对的题 |"]
    lines.append("|---|---:|---:|---:|---:|---|")
    for kind, row in summary["by_type"].items():
        lines.append(
            f"| {kind} | {row['count']} | {row['pass']} | {row['rate']:.0%} | "
            f"{row['wrong_type']} | {'、'.join(row['failed']) or '—'} |"
        )
    lines += [
        "",
        "## 每道题",
        "",
        "| 题号 | 类别 | 提问人 | 问题 | 通知 | 长度 | 结果 | 答到时归的类别 |",
        "|---|---|---|---|---:|---|---|---|",
    ]
    for s in scores:
        q = questions[s["id"]]
        result = STATUS_LABELS[s["status"]]
        if s["need"] == "all":
            result += f"（{s['covered']}/{s['gold_count']} 句）"
        length = band(record_by_doc[q["doc_id"]]["body_length"])
        lines.append(
            f"| {s['id']} | {s['type']} | {q.get('role', '')} | {q['question']} | {q['doc_id']} "
            f"| {length} | {result} | {'、'.join(s['reached_types']) or '—'} |"
        )
    lines += [
        "",
        "## 每篇通知",
        "",
        "| 通知 | 地区 | 长度（字） | 耗时（秒） | 输入/输出 tokens | JSON | 返回/保留 "
        "| 丢弃（原因） | 拒答 | 出错 |",
        "|---:|---|---|---:|---|---|---|---|---|---|",
    ]
    for r in records:
        doc = docs[r["gold_doc_id"]]
        dropped = {k: v for k, v in r["dropped_reasons"].items() if v}
        reasons = "、".join(f"{eval_interpret.DROPPED_LABELS[k]} {v}" for k, v in dropped.items())
        json_label = {True: "有效", False: "无效", None: "—"}[r["json_valid"]]
        refusal = "模型拒答" if r["model_refusal"] else "核对后为空" if r.get("refusal") else "—"
        lines.append(
            f"| {r['gold_doc_id']} | {doc.get('province', '')} | {band(r['body_length'])}"
            f"（{r['body_length']}） | {r['elapsed_seconds']:.1f} | "
            f"{r['usage']['prompt_tokens']}/{r['usage']['completion_tokens']} | {json_label} | "
            f"{r['returned_count']}/{r['kept_count']} | {sum(dropped.values())}"
            f"{f'（{reasons}）' if reasons else ''} | {refusal} | {r['error_code'] or '—'} |"
        )
    latency, cost, usage = summary["latency"], summary["cost"], summary["usage"]
    over_ids = "、".join(map(str, latency["over_gateway_doc_ids"]))
    timing = (
        f"p50 {latency['p50']:.1f} 秒，p95 {latency['p95']:.1f} 秒，"
        f"最长 {latency['max']:.1f} 秒，平均 {latency['mean']:.1f} 秒；"
        f"超过 {GATEWAY_SECONDS} 秒 {latency['over_gateway']} 次"
    ) + (f"（通知 {over_ids}）" if over_ids else "")
    tokens = f"输入 {usage['prompt_tokens']} tokens，输出 {usage['completion_tokens']} tokens"
    if summary["attempted_count"] and not usage["reported"]:
        tokens += "（接口没返回用量）"
    money = (
        f"共 {cost['points']:.1f} 点（约 {cost['yuan']:.3f} 元），"
        f"平均每篇 {cost['points_per_notice']:.1f} 点（约 {cost['yuan_per_notice']:.4f} 元）"
    )
    codes = "、".join(f"{k} {v}" for k, v in summary["error_codes"].items())
    errors = (
        f"{summary['error_count']} 篇{f'（{codes}）' if codes else ''}；"
        f"JSON 无效 {summary['json_invalid_count']} 次；"
        f"模型拒答 {summary['model_refusal_count']} 次；"
        f"核对后一条不剩 {summary['empty_after_check_count']} 次"
    )
    dropped_all = "、".join(
        f"{eval_interpret.DROPPED_LABELS[k]} {v}" for k, v in summary["dropped_reasons"].items()
    )
    lines += [
        "",
        "## 耗时和费用",
        "",
        f"- 耗时：{timing}",
        f"- 用量：{tokens}",
        f"- 费用估算：{money}",
        f"- 出错：{errors}",
        f"- 逐字核对：模型返回 {summary['returned_count']} 条，"
        f"保留 {summary['kept_count']} 条；丢弃 {dropped_all}",
    ]
    failed = [s for s in scores if not s["pass"]]
    if failed:
        lines += ["", "## 没答对的题（金句和模型给的同类原句，便于复核）", ""]
        for s in failed:
            q = questions[s["id"]]
            lines.append(f"### {s['id']} {q['question']}（{STATUS_LABELS[s['status']]}）")
            lines.append("")
            lines += [f"- 金句 {g['sid']}：{short(g['quote'], 80)}" for g in q["gold"]]
            same = [i for i in record_by_doc[q["doc_id"]]["items"] if i["type"] == q["type"]]
            if same:
                lines += [f"- 模型的“{q['type']}”：{short(i['text'], 80)}" for i in same[:5]]
            else:
                lines.append(f"- 模型没给“{q['type']}”类的原句")
            lines.append("")
    lines += [
        "",
        "## 怎么算的",
        "",
        "- 答到：两边全角转半角、去掉空白后，模型保留的原句包含金句；"
        f"或金句包含模型的原句、且这句至少有金句 {MIN_SHARE:.0%} 长。只共用日期或词组不算。",
        "- 答对：答到，而且模型归的类别和题目要求的一样；"
        "材料清单这类（need=all）每句金句都要答到。",
        "- 每篇通知只调一次模型，提示词、段落、max_tokens 和线上解读相同；"
        "逐字核对用 interpret.validate。",
        "- 耗时 p50、p95 用最近秩法（从小到大第 ceil(p·n) 个），含调用失败的那几次。",
        f"- 费用：输入每百万 tokens {INPUT_POINTS} 点、输出 {OUTPUT_POINTS} 点，"
        f"{POINTS_PER_YUAN} 点约合 1 元（以云开发控制台为准）。",
    ]
    return "\n".join(lines) + "\n"


def concurrency_arg(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(f"并发只能是 1 到 {MAX_CONCURRENCY}")
    return number


def beijing_now() -> str:
    return datetime.now(UTC).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true", help="只核对题目集和库，不调模型")
    parser.add_argument("--fake-model", action="store_true", help="假模型自检：不联网、不要密钥")
    parser.add_argument("--concurrency", type=concurrency_arg, default=2)
    args = parser.parse_args(argv)
    if not args.check and args.out is None:
        parser.error("要给 --out 输出目录")
    engine = None
    try:
        gold = load_gold(args.gold)
        settings = load_settings()
        base_url, key, model, extra = advisor_endpoint(settings)
        if args.fake_model:
            model = "fake"
        elif not args.check and not key:
            print(
                "没有配置对外 AI 的线路和密钥（YOUJU_AI_ADVISOR_*），只能用 --check 或 --fake-model"
            )
            return 1
        engine = create_mysql_engine(settings.database_url)
        with engine.connect() as connection:
            # 数据库层强制只读，防止今后 prepare 的变动意外写表
            connection.execute(text("START TRANSACTION READ ONLY"))
            with Session(bind=connection, autoflush=False) as session:
                candidates, notes = resolve(session, gold, model)
            connection.rollback()
        for note in notes:
            print(f"提醒：{note}")
        if args.check:
            print(
                f"题目集核对通过：{len(gold['questions'])} 题、{len(candidates)} 篇通知，"
                "金句都在正文里"
            )
            return 0
        if args.fake_model:
            plan = fake_plan(gold, candidates)

            def factory():
                return FakeModel(plan)
        else:

            def factory():
                return ChatClient(
                    base_url,
                    key,
                    model,
                    timeout=settings.ai_advisor_timeout_seconds,
                    max_retries=0,
                    extra_body=extra,
                    stream=settings.ai_advisor_stream,
                )

        meta = {
            "mode": "fake" if args.fake_model else "model",
            "model": model,
            "route": "假模型，不联网" if args.fake_model else gateway_label(base_url),
            "stream": bool(settings.ai_advisor_stream) and not args.fake_model,
            "concurrency": args.concurrency,
            "max_tokens": 12000,
            "prompt_version": interpret.PROMPT_VERSION,
            "gold_file": args.gold.name,
            "started_at": beijing_now(),
        }
        started = time.monotonic()
        records = run_calls(candidates, factory, args.concurrency)
        for candidate, record in zip(candidates, records, strict=True):
            record["gold_doc_id"] = candidate["key"]
        by_doc = {r["gold_doc_id"]: r for r in records}
        scores = [score(q, by_doc[q["doc_id"]]) for q in gold["questions"]]
        summary = summarize(records, scores)
        meta["finished_at"] = beijing_now()
        meta["wall_seconds"] = round(time.monotonic() - started, 1)
        write_outputs(args.out, gold, records, scores, summary, meta)
        for bar in summary["bars"]:
            mark = "—" if bar["ok"] is None else "PASS" if bar["ok"] else "FAIL"
            print(f"[{mark}] {bar['label']}：{bar['actual']}")
        print(f"候选门槛：{summary['verdict']}（总用时 {meta['wall_seconds']} 秒）")
        print(f"报告：{args.out / 'report.md'}")
        return 0
    except GoldError as exc:
        print(f"题目集和库对不上，没有调用模型：{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - 不打印可能含连接配置或响应的异常原文
        print(f"评测失败：{type(exc).__name__}")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
