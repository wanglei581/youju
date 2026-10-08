"""申报条件抽取评测（D-052 第④项）：用真实正文、模拟的模型，跑通“抽取 → 校验 → 核对 → 对外”。

不调用真实模型，不读 .env。在 backend/ 下运行：

    uv run python scripts/eval_conditions.py \
        --source-url 'mysql+pymysql://root@127.0.0.1:33306/youju_perf?charset=utf8mb4' \
        --target-url 'mysql+pymysql://root@127.0.0.1:33306/youju_test_codex?charset=utf8mb4'

- 源库只读：会话设为只读事务，只做 SELECT。
- 第一步（不写库）：对源库里的申报通知正文，模拟模型给出条件（混入编数字、编原文、下判断的条目），
  看服务端校验留下多少、丢掉多少、丢的原因对不对，留下的引用是否都能在正文里逐字找到。
- 第二步（给了 --target-url 才做）：把这些文件复制到一个测试库（库名必须以 youju_test 开头，
  会先清空重建），跑每轮抽取、核对台接口和详情接口，确认只有整份确认过的条件对外。
"""

from __future__ import annotations

import argparse
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import bindparam, make_url, text

from youju.ai.conditions import validate
from youju.classify import doc_kind
from youju.crawler.detail import redact_sensitive
from youju.db import create_mysql_engine

# 模拟模型：从正文里挑像条件的句子
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_CUE = re.compile(r"须|应当|应具备|应具有|不得|具有|具备|注册|成立|不低于|不少于|以上|信用|失信")
_SKIP = re.compile(r"截止|截至|联系|电话|邮箱|地址|材料|附件|网址|系统|报送|资金|补助|奖励")
_NUMBERING = re.compile(
    r"^\s*(?:[（(][一二三四五六七八九十\d]+[）)]|[一二三四五六七八九十\d]+[、.．])\s*"
)
_CATEGORY_CUES = (
    ("知识产权", re.compile(r"专利|著作权|商标|知识产权")),
    (
        "财务指标",
        re.compile(r"营业收入|销售收入|利润|研发投入|研发费用|纳税|资产|负债|万元|亿元|%|％"),
    ),
    ("人员", re.compile(r"人员|职工|员工|负责人|团队|人才")),
    ("主体资格", re.compile(r"注册|成立|法人|企业|单位|信用|失信|资质|登记")),
)


def _category(sentence: str) -> str:
    for name, cue in _CATEGORY_CUES:
        if cue.search(sentence):
            return name
    return "其他"


def _summary(sentence: str) -> str:
    """模拟模型的一句话复述：去掉编号，超长时在分句处截断（不截断数字）。"""
    core = _NUMBERING.sub("", sentence).strip().rstrip("。；;")
    if len(core) <= 60:
        return core
    cut = max(core.rfind(mark, 0, 60) for mark in "，,、：:")
    return core[:cut] if cut > 10 else re.sub(r"\d+$", "", core[:60])


def _loose(quote: str) -> str:
    """模型给的引用常和正文差一点空白、全角半角：模拟这种差别（校验应当宽容）。"""
    quote = re.sub(r"\s+", "", quote)
    return quote.replace("％", "%").replace("（", "(").replace("）", ")")


class SimulatedModel:
    """模拟后台模型的输出：每份最多 12 条真条件，外加按规律混进去的坏条目。"""

    model = "simulated"

    def __init__(self) -> None:
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.totals = {"calls": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.planted: Counter = Counter()

    def complete_json(self, system: str, user: str, max_tokens: int = 800) -> dict:
        body = user.split("正文：\n", 1)[-1]
        self.totals["calls"] += 1
        items = []
        for match in _SENTENCE.finditer(body):
            sentence = match.group().strip()
            if len(items) >= 12:
                break
            if (
                not 10 <= len(sentence) <= 200
                or not _CUE.search(sentence)
                or _SKIP.search(sentence)
            ):
                continue
            items.append(
                {
                    "category": _category(sentence),
                    "text": _summary(sentence),
                    "quote": _loose(sentence),
                }
            )
        for index, item in enumerate(items):
            if index % 5 == 4 and re.search(r"\d", item["text"]):
                item["text"] = re.sub(
                    r"\d", lambda m: str((int(m.group()) + 1) % 10), item["text"], count=1
                )
                self.planted["number_mismatch"] += 1
            elif index % 7 == 6:
                item["quote"] = item["quote"][:4] + "（另有规定）" + item["quote"][4:]
                self.planted["not_found"] += 1
        if items:
            items.append({**items[0], "text": "符合以上条件的企业可以申报"})
            self.planted["judgement"] += 1
        return {"conditions": items}


def read_documents(source_url: str, limit: int) -> list[dict]:
    """源库只读：只取一级公开来源、正文抓到了、标题像申报通知的文件。"""
    engine = create_mysql_engine(source_url)
    rows: list[dict] = []
    with engine.connect() as conn:
        conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
        result = conn.execute(
            text(
                "SELECT d.id, d.title, d.url, d.url_key, d.publish_date, d.first_seen_at,"
                " d.last_seen_at, t.body_text, s.id AS source_id, s.level, s.province, s.dept,"
                " s.dept_full_name, s.column_name, s.url AS source_url"
                " FROM documents d JOIN document_details t ON t.document_id = d.id"
                " JOIN sources s ON s.id = d.source_id"
                " WHERE t.status = 'ok' AND t.body_text <> '' AND s.rights_level = 1"
                " ORDER BY d.id DESC"
            )
        )
        for row in result.mappings():
            if doc_kind(row["title"]) != "申报通知":
                continue
            rows.append(dict(row))
            if len(rows) >= limit:
                break
        ids = [row["id"] for row in rows]
        deadlines = (
            conn.execute(
                text(
                    "SELECT document_id, fingerprint, kind, candidate_date, candidate_time,"
                    " evidence, confidence, status, approved_date FROM document_deadlines"
                    " WHERE status IN ('pending', 'approved') AND document_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            )
            .mappings()
            .all()
            if ids
            else []
        )
    engine.dispose()
    by_doc: dict[int, list[dict]] = {}
    for deadline in deadlines:
        by_doc.setdefault(deadline["document_id"], []).append(dict(deadline))
    for row in rows:
        row["deadlines"] = by_doc.get(row["id"], [])
    return rows


def check_validation(rows: list[dict]) -> dict:
    """第一步：只跑模拟模型和校验，不写任何库。"""
    model = SimulatedModel()
    reasons: Counter = Counter()
    kept = per_doc_zero = verbatim_errors = 0
    lengths = []
    started = time.perf_counter()
    for row in rows:
        body = redact_sensitive(row["body_text"])
        payload = model.complete_json("", f"标题：{row['title']}\n正文：\n{body}")
        result = validate(payload, body)
        kept += len(result.items)
        per_doc_zero += not result.items
        reasons.update({k: v for k, v in result.reasons.items() if v})
        for item in result.items:
            lengths.append(len(item.quote))
            if body[item.offset : item.offset + len(item.quote)] != item.quote:
                verbatim_errors += 1
    lengths.sort()
    return {
        "documents": len(rows),
        "kept": kept,
        "documents_without_items": per_doc_zero,
        "dropped": dict(reasons),
        "planted": dict(model.planted),
        "verbatim_errors": verbatim_errors,
        "quote_length_median": lengths[len(lengths) // 2] if lengths else 0,
        "quote_length_max": lengths[-1] if lengths else 0,
        "seconds": round(time.perf_counter() - started, 2),
    }


def run_flow(rows: list[dict], target_url: str, limit: int) -> dict:
    """第二步：复制到测试库，跑每轮抽取、核对台和详情接口。"""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from youju import conditions as service
    from youju.api import app as app_module
    from youju.db import get_session
    from youju.migrations import upgrade
    from youju.models import Document, DocumentDeadline, DocumentDetail, Source

    url = make_url(target_url)
    if not (url.database or "").startswith("youju_test"):
        raise SystemExit("目标库名必须以 youju_test 开头（会清空重建）")
    server = create_mysql_engine(url.set(database=""))
    with server.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{url.database}`"))
        conn.execute(
            text(f"CREATE DATABASE `{url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
        )
    server.dispose()
    upgrade(target_url)
    engine = create_mysql_engine(target_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bodies: dict[int, str] = {}
    with factory() as session:
        sources: dict[int, Source] = {}
        for row in rows:
            source = sources.get(row["source_id"])
            if source is None:
                source = Source(
                    level=row["level"],
                    province=row["province"],
                    dept=row["dept"],
                    dept_full_name=row["dept_full_name"],
                    column_name=row["column_name"],
                    url=row["source_url"],
                    rights_level=1,
                )
                session.add(source)
                session.flush()
                sources[row["source_id"]] = source
            doc = Document(
                source_id=source.id,
                url=row["url"],
                url_key=row["url_key"],
                title=row["title"],
                publish_date=row["publish_date"],
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
            )
            session.add(doc)
            session.flush()
            bodies[doc.id] = redact_sensitive(row["body_text"])
            session.add(
                DocumentDetail(
                    document_id=doc.id,
                    body_text=row["body_text"],
                    fetched_at=row["last_seen_at"],
                    status="ok",
                )
            )
            for d in row["deadlines"]:
                session.add(
                    DocumentDeadline(
                        document_id=doc.id, **{k: v for k, v in d.items() if k != "document_id"}
                    )
                )
        session.commit()
        model = SimulatedModel()
        report = service.run_extraction(session, model, limit=limit, now=datetime.now(UTC))
        again = service.run_extraction(session, model, limit=limit, now=datetime.now(UTC))

    def override():
        with factory() as session:
            yield session

    app_module.app.dependency_overrides[get_session] = override
    original = app_module.get_settings
    app_module.get_settings = lambda: SimpleNamespace(admin_token="eval-token", role="all")
    headers = {"X-Admin-Token": "eval-token"}
    flow: dict = {}
    try:
        with TestClient(app_module.app) as client:
            flow["pending_before"] = client.get(
                "/api/admin/conditions/count", headers=headers
            ).json()["pending"]
            queue = client.get("/api/admin/conditions?limit=100", headers=headers).json()
            flow["queue_tiers"] = dict(Counter(card["tier_label"] for card in queue))
            flow["highlight_ok"] = all(
                item["before"] + item["quote"] + item["after"] in bodies[card["document_id"]]
                for card in queue
                for item in card["items"]
            )
            first, second = queue[0], queue[1] if len(queue) > 1 else None
            body = {"reviewed_by": "评测"}
            for index, item in enumerate(first["items"]):
                action = "delete" if index == len(first["items"]) - 1 and index else "approve"
                client.post(
                    f"/api/admin/conditions/items/{item['id']}/{action}", headers=headers, json=body
                )
            if first["items"]:
                edited = first["items"][0]
                client.post(
                    f"/api/admin/conditions/items/{edited['id']}/edit",
                    headers=headers,
                    json={**body, "text": edited["text"] + "（改写）"},
                )
            flow["confirm"] = client.post(
                f"/api/admin/conditions/{first['list_id']}/confirm", headers=headers, json=body
            ).json()
            shown = (
                client.get(f"/api/v1/documents/{first['document_id']}")
                .json()
                .get("apply_conditions")
            )
            flow["public_items"] = len(shown["items"]) if shown else 0
            flow["public_sample"] = shown["items"][0] if shown else None
            edited_public = [
                i for i in (shown or {}).get("items", []) if i["text"].endswith("（改写）")
            ]
            # 改写过的那条：对外显示改后的文字，原文引用不变
            flow["edited_quote_kept"] = len(edited_public) == 1 and (
                edited_public[0]["quote"] == first["items"][0]["quote"]
            )
            if second:
                flow["unconfirmed_public"] = (
                    "apply_conditions"
                    in client.get(f"/api/v1/documents/{second['document_id']}").json()
                )
            flow["pending_after"] = client.get(
                "/api/admin/conditions/count", headers=headers
            ).json()["pending"]
            client.post(
                f"/api/admin/conditions/{first['list_id']}/reopen", headers=headers, json=body
            )
            flow["public_after_reopen"] = (
                "apply_conditions" in client.get(f"/api/v1/documents/{first['document_id']}").json()
            )
    finally:
        app_module.app.dependency_overrides.clear()
        app_module.get_settings = original
        engine.dispose()
    return {
        "copied": len(rows),
        "extracted": report.documents,
        "due_soon_first": report.due_soon,
        "lists": report.lists,
        "items": report.items,
        "empty": report.empty,
        "dropped": dict(report.dropped_reasons),
        "second_round_documents": again.documents,
        **flow,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-url", required=True, help="只读的源库（如 youju_perf）")
    parser.add_argument(
        "--target-url", help="跑完整流程用的测试库（库名以 youju_test 开头，会清空）"
    )
    parser.add_argument("--limit", type=int, default=200, help="取多少份申报通知")
    args = parser.parse_args()
    if args.target_url:
        # 程序里用默认连接的地方（如每次请求写访问日志）也只连测试库，不连开发库 youju
        os.environ["YOUJU_DATABASE_URL"] = args.target_url
        from youju.config import get_settings
        from youju.db import get_engine

        get_settings.cache_clear()
        get_engine.cache_clear()
    rows = read_documents(args.source_url, args.limit)
    print("第一步 校验：", check_validation(rows))
    if args.target_url:
        print("第二步 流程：", run_flow(rows, args.target_url, args.limit))


if __name__ == "__main__":
    main()
