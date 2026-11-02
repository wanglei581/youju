"""30 题评测：只用假模型或 httpx 的假接口，不访问真实模型，也不碰密钥。"""

import importlib.util
import json
import os
import shutil
import subprocess
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, func, select

from youju.ai import interpret
from youju.ai.client import ChatClient
from youju.config import Settings
from youju.models import AdvisorTask, AdvisorUsage, AIUsage, Document, DocumentDetail, Source

BACKEND = Path(__file__).resolve().parents[1]
RUNNER = BACKEND.parent / "tools" / "team" / "run-eval-hy3.sh"
SPEC = importlib.util.spec_from_file_location(
    "eval_questions", BACKEND / "scripts" / "eval_questions.py"
)
eq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eq)
NOW = datetime(2026, 10, 28, tzinfo=UTC)
KEY = "sk-test-never-shown-0000"
BODY_A = (
    "一、申报时间\n企业网上申报截止时间为2026年11月30日17:00。\n"
    "区县科技局于2026年12月5日前完成审核推荐。\n二、申报材料\n1.项目申报书原件；\n2.企业营业执照复印件。"
)
BODY_B = "申报单位为本省注册的企业。\n每个项目最高支持50万元。\n通过省政务服务网在线申报。"


@pytest.fixture(autouse=True)
def block_real_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("测试不许访问真实模型")

    monkeypatch.setattr(eq, "ChatClient", forbidden)


@pytest.fixture
def notices(session):
    ids = []
    for index, body in enumerate((BODY_A, BODY_B)):
        source = Source(
            level="省级",
            province="河北",
            dept="科技",
            dept_full_name=f"科技厅{index}",
            column_name="通知公告",
            url=f"https://official.test/list/{index}",
            parser_config={},
            rights_level=1,
        )
        session.add(source)
        session.flush()
        row = Document(
            source_id=source.id,
            title=f"关于组织申报第{index}批项目的通知",
            kind_override="申报通知",
            url=f"https://official.test/notice/{index}",
            url_key=f"official.test/notice/{index}",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        session.add(row)
        session.flush()
        session.add(DocumentDetail(document_id=row.id, body_text=body, fetched_at=NOW, status="ok"))
        ids.append(row.id)
    session.commit()
    return ids


def make_gold(ids):
    a, b = ids
    return {
        "version": 1,
        "questions_by": "测试",
        "gold_by": "测试",
        "snapshot": "测试库",
        "documents": [
            {"doc_id": a, "url_key": "official.test/notice/0", "title": "甲", "province": "河北"},
            {"doc_id": b, "url_key": "official.test/notice/1", "title": "乙", "province": "河北"},
        ],
        "questions": [
            {
                "id": "T1",
                "type": "时间",
                "question": "企业网上申报最晚哪天？",
                "doc_id": a,
                "gold": [{"sid": "P002", "quote": "企业网上申报截止时间为2026年11月30日17:00。"}],
            },
            {
                "id": "T2",
                "type": "时间",
                "question": "区县推荐截止哪天？",
                "doc_id": a,
                "gold": [{"sid": "P003", "quote": "区县科技局于2026年12月5日前完成审核推荐。"}],
            },
            {
                "id": "M1",
                "type": "材料",
                "question": "要交哪些材料？",
                "doc_id": a,
                "need": "all",
                "gold": [
                    {"sid": "P005", "quote": "1.项目申报书原件；"},
                    {"sid": "P006", "quote": "2.企业营业执照复印件。"},
                ],
            },
            {
                "id": "S1",
                "type": "支持",
                "question": "最多给多少钱？",
                "doc_id": b,
                "gold": [{"sid": "P002", "quote": "每个项目最高支持50万元。"}],
            },
            {
                "id": "W1",
                "type": "方式",
                "question": "在哪里申报？",
                "doc_id": b,
                "gold": [{"sid": "P003", "quote": "通过省政务服务网在线申报。"}],
            },
        ],
    }


def write_gold(tmp_path, gold):
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("kept", "gold", "expected"),
    [
        # 一样、保留的更长（模型一次引了几句）、PDF 折行、全角数字：都算答到
        ("申报截止时间为2026年11月30日。", "申报截止时间为2026年11月30日。", True),
        (
            "三、申报时间\n申报截止时间为2026年11月30日。逾期不予受理。",
            "申报截止时间为2026年11月30日。",
            True,
        ),
        ("申报截止时间为\n2026年11月30日。", "申报截止时间为2026年11月30日。", True),
        ("申报截止时间为２０２６年１１月３０日。", "申报截止时间为2026年11月30日。", True),
        # 金句包含保留的原句：至少 60% 才算（正好 60% 算）
        ("甲乙丙丁戊己", "甲乙丙丁戊己庚辛壬癸", True),
        ("甲乙丙丁戊", "甲乙丙丁戊己庚辛壬癸", False),
        # 小标题、只共用日期、空的都不算
        ("三、申报时间", "三、申报时间：企业网上申报截止时间为2026年11月30日。", False),
        (
            "区县科技局于2026年11月30日前完成审核推荐。",
            "企业于2026年11月30日前完成网上申报。",
            False,
        ),
        ("", "申报截止时间为2026年11月30日。", False),
    ],
)
def test_overlap_rule(kept, gold, expected):
    assert eq.overlaps(kept, gold) is expected


def test_find_gold_handles_wrapped_lines_and_moved_paragraphs():
    paragraphs = interpret.split_paragraphs("通知如下：\n申报截止时间为2026年\n11月30日。\n其他")
    quote = "申报截止时间为2026年\n11月30日。"
    assert eq.find_gold(paragraphs, "P002", quote) == "P002"
    # 段号变了、原句还在：给现在的段号
    assert eq.find_gold(paragraphs, "P009", quote) == "P002"
    # 去掉换行就不是原样了
    assert eq.find_gold(paragraphs, "P002", "申报截止时间为2026年11月30日。") is None


def test_load_gold_checks_structure(tmp_path):
    gold = make_gold([1, 2])
    assert eq.load_gold(write_gold(tmp_path, gold))["questions"][0]["id"] == "T1"
    broken = [
        lambda g: g["questions"].append(dict(g["questions"][0])),
        lambda g: g["questions"][0].update(type="其他"),
        lambda g: g["questions"][0].update(need="most"),
        lambda g: g["questions"][0].update(doc_id=99),
        lambda g: g["questions"][0].update(gold=[]),
        lambda g: g["questions"][0]["gold"][0].update(quote="太短了"),
        lambda g: g["questions"][0]["gold"][0].update(quote="联系电话：[电话已脱敏]，请咨询"),
        lambda g: g["documents"][0].pop("url_key"),
    ]
    for change in broken:
        gold = make_gold([1, 2])
        change(gold)
        with pytest.raises(eq.GoldError):
            eq.load_gold(write_gold(tmp_path, gold))


def test_gold_must_be_verbatim_in_prepared_paragraphs(session, notices, tmp_path):
    gold = eq.load_gold(write_gold(tmp_path, make_gold(notices)))
    candidates, notes = eq.resolve(session, gold, "fake")
    assert [c["doc_id"] for c in candidates] == notices and notes == []
    # 改一个字就不是原句：不调模型，报出题号
    gold = make_gold(notices)
    gold["questions"][1]["gold"][0]["quote"] = "区县科技局于2026年12月6日前完成审核推荐。"
    with pytest.raises(eq.GoldError, match="T2"):
        eq.resolve(session, gold, "fake")
    # 段号记错了、原句还在：改用现在的段号并提醒
    gold = make_gold(notices)
    gold["questions"][0]["gold"][0]["sid"] = "P001"
    _, notes = eq.resolve(session, gold, "fake")
    assert gold["questions"][0]["gold"][0]["sid"] == "P002" and "T1" in notes[0]
    # 正文和出题时不同（指纹变了）但金句都在：照样评，提醒一声
    gold = make_gold(notices)
    gold["documents"][1]["body_hash"] = "出题时的指纹"
    _, notes = eq.resolve(session, gold, "fake")
    assert notes == [f"通知 {notices[1]} 的正文和出题时不同"]


def test_resolve_rematches_by_url_key_and_refuses_ineligible(session, notices):
    gold = make_gold(notices)
    gold["documents"][0]["doc_id"] = 90001
    for q in gold["questions"]:
        if q["doc_id"] == notices[0]:
            q["doc_id"] = 90001
    candidates, notes = eq.resolve(session, gold, "fake")
    assert candidates[0]["key"] == 90001 and candidates[0]["doc_id"] == notices[0]
    assert "按网址键" in notes[0]
    gold["documents"][0]["url_key"] = "official.test/gone"
    with pytest.raises(eq.GoldError, match="找不到"):
        eq.resolve(session, gold, "fake")
    session.get(Document, notices[1]).kind_override = "名单"
    session.commit()
    with pytest.raises(eq.GoldError, match="不能解读"):
        eq.resolve(session, make_gold(notices), "fake")


class CountingModel:
    """记下调用次数的假模型：每篇给一条能答到 T1 的引用。"""

    calls = Counter()
    lock = threading.Lock()

    def __init__(self):
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def complete_json(self, system, user, max_tokens):
        paragraphs = json.loads(user)["paragraphs"]
        with self.lock:
            self.calls[paragraphs[0]["text"]] += 1
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 20}
        quote = paragraphs[1]["text"][:12]
        return {"items": [{"type": "时间", "refs": [{"pid": "P002", "quote": quote}]}]}

    def close(self):
        pass


def test_one_call_per_notice_even_when_questions_share_it(session, notices, tmp_path):
    gold = eq.load_gold(write_gold(tmp_path, make_gold(notices)))
    candidates, _ = eq.resolve(session, gold, "fake")
    CountingModel.calls.clear()
    lines = []
    records = eq.run_calls(candidates, CountingModel, 2, progress=lines.append)
    # 5 道题、2 篇通知：只调 2 次，结果按题目集的顺序
    assert sum(CountingModel.calls.values()) == 2 and set(CountingModel.calls.values()) == {1}
    assert [r["doc_id"] for r in records] == notices and len(lines) == 2
    for candidate, record in zip(candidates, records, strict=True):
        record["gold_doc_id"] = candidate["key"]
    by_doc = {r["gold_doc_id"]: r for r in records}
    scores = {q["id"]: eq.score(q, by_doc[q["doc_id"]]) for q in gold["questions"]}
    assert scores["T1"]["status"] == "pass" and scores["T2"]["status"] == "miss"


def _items(*pairs):
    return [
        {"type": kind, "text": quote, "refs": [{"pid": "P001", "quote": quote}]}
        for kind, quote in pairs
    ]


def test_score_statuses():
    question = {
        "id": "M",
        "type": "材料",
        "need": "all",
        "gold": [{"quote": "1.项目申报书原件；"}, {"quote": "2.企业营业执照复印件。"}],
    }
    ok = {"status": "success"}
    both = _items(("材料", "1.项目申报书原件；"), ("材料", "2.企业营业执照复印件。"))
    assert eq.score(question, {**ok, "items": both})["status"] == "pass"
    half = eq.score(question, {**ok, "items": both[:1]})
    assert half["status"] == "partial" and half["covered"] == 1 and half["gold_count"] == 2
    wrong = eq.score(
        question,
        {**ok, "items": _items(("方式", "1.项目申报书原件；"), ("方式", "2.企业营业执照复印件。"))},
    )
    assert wrong["status"] == "wrong_type" and wrong["reached_types"] == ["方式"]
    assert eq.score(question, {**ok, "items": []})["status"] == "miss"
    assert eq.score(question, {"status": "error", "items": []})["status"] == "error"
    # need=any：答到一句就算；只用对了类别的条目
    anyq = {**question, "need": "any"}
    mixed = _items(("方式", "1.项目申报书原件；"), ("材料", "2.企业营业执照复印件。"))
    assert eq.score(anyq, {**ok, "items": mixed})["pass"] is True


def test_percentile_nearest_rank():
    assert eq.percentile([], 0.5) == 0.0
    assert eq.percentile([5.0, 1.0, 3.0], 0.5) == 3.0
    assert eq.percentile([5.0, 1.0, 3.0], 0.95) == 5.0
    values = [float(v) for v in range(1, 18)]
    assert eq.percentile(values, 0.5) == 9.0 and eq.percentile(values, 0.95) == 17.0


def _record(doc_id, elapsed, prompt, completion, **extra):
    record = {
        "doc_id": doc_id,
        "gold_doc_id": doc_id,
        "status": "success",
        "elapsed_seconds": elapsed,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        "json_valid": True,
        "error_code": None,
        "model_refusal": False,
        "refusal": None,
        "returned_count": 3,
        "kept_count": 2,
        "dropped_reasons": dict.fromkeys(interpret.DROPPED_REASONS, 0),
        "items": [],
        "body_length": 100,
    }
    record.update(extra)
    return record


def _scores(failed=(), types=None):
    types = (
        types
        or ["时间"] * 7 + ["条件"] * 6 + ["材料"] * 6 + ["对象"] * 4 + ["支持"] * 4 + ["方式"] * 3
    )
    return [
        {
            "id": f"Q{i:02d}",
            "type": kind,
            "pass": f"Q{i:02d}" not in failed,
            "status": "miss" if f"Q{i:02d}" in failed else "pass",
        }
        for i, kind in enumerate(types, 1)
    ]


def test_summary_math():
    records = [
        _record(1, 10.0, 600_000, 200_000),
        _record(
            2,
            30.0,
            300_000,
            200_000,
            dropped_reasons={**dict.fromkeys(interpret.DROPPED_REASONS, 0), "not_found": 2},
        ),
        _record(
            3,
            61.0,
            100_000,
            100_000,
            status="error",
            json_valid=False,
            error_code="invalid_json",
            kept_count=0,
            returned_count=0,
        ),
    ]
    scores = _scores(failed={"Q08", "Q20"})
    summary = eq.summarize(records, scores)
    assert summary["pass_count"] == 28 and summary["pass_rate"] == pytest.approx(28 / 30)
    assert summary["by_type"]["条件"] == {
        "count": 6,
        "pass": 5,
        "rate": pytest.approx(5 / 6),
        "wrong_type": 0,
        "failed": ["Q08"],
    }
    assert summary["by_type"]["时间"]["rate"] == 1.0
    latency = summary["latency"]
    assert (latency["p50"], latency["p95"], latency["max"]) == (30.0, 61.0, 61.0)
    assert latency["mean"] == pytest.approx(101 / 3)
    assert latency["over_gateway"] == 1 and latency["over_gateway_doc_ids"] == [3]
    assert summary["usage"] == {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 500_000,
        "reported": True,
    }
    # 输入 100 万 tokens × 1000 点 + 输出 50 万 × 4000 点 = 3000 点，约 3 元；3 篇平均 1000 点
    assert summary["cost"]["points"] == pytest.approx(3000)
    assert summary["cost"]["yuan"] == pytest.approx(3.0)
    assert summary["cost"]["points_per_notice"] == pytest.approx(1000)
    assert summary["json_invalid_count"] == 1 and summary["error_codes"] == {"invalid_json": 1}
    assert summary["dropped_reasons"]["not_found"] == 2
    assert summary["kept_count"] == 4 and summary["returned_count"] == 6


def test_pass_bar():
    fast = [_record(i, 20.0, 1000, 500) for i in range(17)]
    bar = {b["key"]: b for b in eq.summarize(fast, _scores(failed={"Q08", "Q20", "Q30"}))["bars"]}
    # 27/30 = 90% 正好达标
    assert bar["pass_rate"]["ok"] is True and bar["time_all"]["ok"] is True
    assert bar["json_invalid"]["ok"] is True and bar["gateway"]["ok"] is True
    assert bar["cost"]["ok"] is None
    assert eq.summarize(fast, _scores(failed={"Q08", "Q20", "Q30"}))["verdict"] == "PASS"
    failing = eq.summarize(fast, _scores(failed={"Q08", "Q20", "Q29", "Q30"}))
    assert {b["key"]: b["ok"] for b in failing["bars"]}["pass_rate"] is False
    assert failing["verdict"] == "FAIL"
    # 时间类错一道就不达标，并列出例外
    time_miss = {b["key"]: b for b in eq.summarize(fast, _scores(failed={"Q03"}))["bars"]}[
        "time_all"
    ]
    assert time_miss["ok"] is False and "Q03" in time_miss["actual"]
    # JSON 无效 2 次、有一次超过 60 秒：各自不达标
    broken = fast[:15] + [
        _record(90, 20.0, 0, 0, json_valid=False, status="error") for _ in range(2)
    ]
    assert {b["key"]: b["ok"] for b in eq.summarize(broken, _scores())["bars"]}[
        "json_invalid"
    ] is False
    slow = fast[:16] + [_record(91, 60.5, 1000, 500)]
    gateway = {b["key"]: b for b in eq.summarize(slow, _scores())["bars"]}["gateway"]
    assert gateway["ok"] is False and "STREAM" in gateway["note"]
    # 不是云开发线路（DeepSeek 官方接口）：60 秒那条只报告，不算门槛；费用写明是参考值
    other = eq.summarize(slow, _scores(), cloudbase=False)
    other_bars = {b["key"]: b for b in other["bars"]}
    assert other_bars["gateway"]["ok"] is None and "只报告" in other_bars["gateway"]["label"]
    assert "参考值" in other_bars["cost"]["label"] and other["verdict"] == "PASS"
    assert eq.gateway_label("https://api.deepseek.com") == "DeepSeek 官方接口"
    # DeepSeek 的模型在后台价目表里：费用按官网高峰时段价算成元，不再按云开发价目估
    priced = eq.summarize(slow, _scores(), cloudbase=False, model="deepseek-v4-pro")
    prompt = sum(r["usage"]["prompt_tokens"] for r in slow)
    completion = sum(r["usage"]["completion_tokens"] for r in slow)
    assert priced["cost"]["basis"] == "price_table"
    assert priced["cost"]["yuan"] == pytest.approx((prompt * 9.0 + completion * 27.0) / 1_000_000)
    priced_cost = {b["key"]: b for b in priced["bars"]}["cost"]
    assert "DeepSeek 官网" in priced_cost["label"] and priced_cost["actual"].startswith("约 ")
    assert "deepseek-v4-pro 输入每百万 tokens 9.0 元" in eq.cost_note(priced)
    assert "参考值" in eq.cost_note(other) and "云开发控制台" in eq.cost_note(
        eq.summarize(slow, _scores())
    )
    dry = eq.summarize([_record(1, 0.0, 0, 0, status="dry_run")], _scores())
    assert dry["verdict"] == "未调用模型"


def test_main_fake_model_end_to_end_read_only(
    db_engine, session, notices, tmp_path, monkeypatch, capsys
):
    settings = Settings(
        _env_file=None, database_url=db_engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(eq, "load_settings", lambda: settings)
    monkeypatch.setattr(eq, "create_mysql_engine", lambda url: db_engine)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", record)
    try:
        gold = write_gold(tmp_path, make_gold(notices))
        assert eq.main(["--gold", str(gold), "--check"]) == 0
        assert "题目集核对通过：5 题、2 篇通知" in capsys.readouterr().out
        out = tmp_path / "out"
        assert eq.main(["--gold", str(gold), "--out", str(out), "--fake-model"]) == 0
    finally:
        event.remove(db_engine, "before_cursor_execute", record)
    assert statements[0] == "START TRANSACTION READ ONLY"
    assert all(s.lstrip().upper().startswith(("SELECT", "START TRANSACTION")) for s in statements)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "假模型自检，不是 hy3 的成绩" in report and "| T1 | 时间 |" in report
    lines = [json.loads(line) for line in (out / "records.jsonl").read_text().splitlines()]
    assert [line["doc_id"] for line in lines] == notices
    assert lines[0]["questions"][0]["id"] == "T1"
    summary = json.loads((out / "summary.json").read_text())
    # 假模型故意漏掉最后一题、把倒数第二题归错类别
    statuses = {s["id"]: s["status"] for s in summary["failed"]}
    assert statuses == {"S1": "wrong_type", "W1": "miss"}
    assert summary["pass_count"] == 3 and summary["meta"]["mode"] == "fake"
    assert all(
        session.scalar(select(func.count(cls.id))) == 0
        for cls in (AdvisorTask, AdvisorUsage, AIUsage)
    )


def _sse(payload, usage):
    content = json.dumps(payload, ensure_ascii=False)
    chunks = [
        {"choices": [{"delta": {"content": content[:20]}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": content[20:]}, "finish_reason": "stop"}]},
        {"choices": [], "usage": usage},
    ]
    return (
        "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
        + "data: [DONE]\n\n"
    )


@pytest.mark.parametrize("stream", [False, True])
def test_main_with_real_client_on_fake_endpoint(
    db_engine, session, notices, tmp_path, monkeypatch, stream
):
    """真的 ChatClient 走一遍（假接口不联网）：线路、密钥、流式、附加参数都按配置传，报告里没有密钥。"""
    base = "https://env-test.api.tcloudbasegateway.com/v1/ai/cloudbase"
    settings = Settings(
        _env_file=None,
        database_url=db_engine.url.render_as_string(hide_password=False),
        ai_advisor_base_url=base,
        ai_advisor_api_key=SecretStr(KEY),
        ai_advisor_model="hy3",
        ai_advisor_request_extra={"thinking": {"type": "disabled"}},
        ai_advisor_stream=stream,
    )
    monkeypatch.setattr(eq, "load_settings", lambda: settings)
    monkeypatch.setattr(eq, "create_mysql_engine", lambda url: db_engine)
    gold = make_gold(notices)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(
            (
                request.url.path,
                request.headers["authorization"],
                body["model"],
                body.get("thinking"),
                body.get("stream", False),
            )
        )
        paragraphs = json.loads(body["messages"][1]["content"])["paragraphs"]
        texts = {p["pid"]: p["text"] for p in paragraphs}
        items = [
            {"type": q["type"], "refs": [{"pid": g["sid"], "quote": g["quote"]}]}
            for q in gold["questions"]
            for g in q["gold"]
            if texts.get(g["sid"]) == g["quote"]
        ]
        payload = {"items": items, "missing": [], "refusal": None}
        usage = {"prompt_tokens": 1000, "completion_tokens": 300}
        if body.get("stream"):
            return httpx.Response(
                200, text=_sse(payload, usage), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(payload, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )

    options = []

    def factory(*args, **kwargs):
        options.append(kwargs)
        return ChatClient(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(eq, "ChatClient", factory)
    out = tmp_path / "out"
    assert (
        eq.main(
            ["--gold", str(write_gold(tmp_path, gold)), "--out", str(out), "--concurrency", "1"]
        )
        == 0
    )
    assert (
        seen
        == [
            (
                "/v1/ai/cloudbase/chat/completions",
                f"Bearer {KEY}",
                "hy3",
                {"type": "disabled"},
                stream,
            )
        ]
        * 2
    )
    assert (
        options
        == [
            {
                "timeout": 180.0,
                "max_retries": 0,
                "extra_body": {"thinking": {"type": "disabled"}},
                "stream": stream,
            }
        ]
        * 2
    )
    summary = json.loads((out / "summary.json").read_text())
    assert summary["pass_count"] == 5 and summary["verdict"] == "PASS"
    assert summary["usage"] == {"prompt_tokens": 2000, "completion_tokens": 600, "reported": True}
    assert summary["meta"]["route"] == "云开发网关" and summary["meta"]["stream"] is stream
    for name in ("report.md", "records.jsonl", "summary.json"):
        text = (out / name).read_text(encoding="utf-8")
        assert KEY not in text and "env-test" not in text


def _raise(exc):
    raise exc


def _candidate():
    body = "企业网上申报截止时间为2026年11月30日17:00。"
    return {
        "doc_id": 1,
        "source_id": 1,
        "snapshot": {
            "paragraphs": interpret.split_paragraphs(body),
            "body_length": len(body),
            "attachments": 0,
        },
    }


@pytest.mark.parametrize(
    ("respond", "code", "json_valid"),
    [
        (
            lambda r: httpx.Response(
                401, json={"error": f"bad key {KEY}", "auth": r.headers["authorization"]}
            ),
            "http_401",
            None,
        ),
        (lambda r: httpx.Response(504, text=f"gateway timeout {KEY}"), "http_504", None),
        (lambda r: _raise(httpx.ConnectError(f"refused {KEY}", request=r)), "network", None),
        (lambda r: _raise(httpx.ReadTimeout(f"slow {KEY}", request=r)), "timeout", None),
        (
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": f"不是 JSON {KEY}"}, "finish_reason": "stop"}
                    ]
                },
            ),
            "invalid_json",
            False,
        ),
        (
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]}
            ),
            "truncated",
            False,
        ),
        (
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"items": "错"}'}, "finish_reason": "stop"}
                    ]
                },
            ),
            "bad_structure",
            False,
        ),
    ],
)
def test_call_errors_never_carry_key_or_response(respond, code, json_valid):
    client = ChatClient(
        "https://env-test.api.tcloudbasegateway.com/v1/ai/cloudbase",
        KEY,
        "hy3",
        transport=httpx.MockTransport(respond),
        max_retries=0,
        sleep=lambda s: None,
    )
    with client:
        record = eq.eval_interpret.evaluate_one(_candidate(), client)
    assert record["status"] == "error" and record["error_code"] == code
    assert record["json_valid"] is json_valid
    assert KEY not in json.dumps(record, ensure_ascii=False)


def test_main_without_key_stops_before_database(monkeypatch, capsys, tmp_path):
    settings = Settings(
        _env_file=None, ai_api_key=None, ai_advisor_base_url="", ai_advisor_api_key=None
    )
    monkeypatch.setattr(eq, "load_settings", lambda: settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("没有密钥时不应连接数据库")

    monkeypatch.setattr(eq, "create_mysql_engine", forbidden)
    gold = write_gold(tmp_path, make_gold([1, 2]))
    assert eq.main(["--gold", str(gold), "--out", str(tmp_path / "out")]) == 1
    assert "只能用 --check 或 --fake-model" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_gateway_label_hides_environment_id():
    assert (
        eq.gateway_label("https://env-abc.api.tcloudbasegateway.com/v1/ai/cloudbase")
        == "云开发网关"
    )
    assert eq.gateway_label("https://api.example.com/v1") == "其他地址"


def test_question_set_file():
    gold = eq.load_gold(eq.GOLD)
    questions = gold["questions"]
    assert len(questions) == 30
    assert Counter(q["type"] for q in questions) == {
        "时间": 7,
        "条件": 6,
        "材料": 6,
        "对象": 4,
        "支持": 4,
        "方式": 3,
    }
    assert "Grok 额度用完，由 Claude 起草" in gold["questions_by"]
    assert "agy 复核" in gold["questions_by"]
    # 三档长度都有，长文要有接近 24000 字上限的
    lengths = [d["body_length"] for d in gold["documents"]]
    assert {eq.band(n) for n in lengths} == set(eq.BANDS) and max(lengths) > 20000
    assert max(lengths) <= interpret.MAX_BODY
    assert all(d["url_key"] and d["title"] for d in gold["documents"])
    assert {q["role"] for q in questions} == {"经办人", "顾问"}


def _run(args, stdin, **env):
    return subprocess.run(
        ["bash", str(RUNNER), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **env},
    )


def test_runner_refuses_empty_key(tmp_path):
    out = tmp_path / "out"
    result = _run(
        ["https://env-test.api.tcloudbasegateway.com/v1/ai/cloudbase", "--out", str(out)], ""
    )
    assert result.returncode == 2 and "没有输入密钥，不运行" in result.stderr
    assert not out.exists()
    blank = _run(
        ["https://env-test.api.tcloudbasegateway.com/v1/ai/cloudbase", "--out", str(out)], "   \n"
    )
    assert blank.returncode == 2 and not out.exists()


def test_runner_refuses_missing_or_plain_http_address(tmp_path):
    env = {"YOUJU_EVAL_HY3_BASE_URL": ""}
    assert _run(["--out", str(tmp_path / "a")], "", **env).returncode == 2
    result = _run(["http://env-test.api.tcloudbasegateway.com/v1/ai/cloudbase"], "", **env)
    assert result.returncode == 2 and "https://" in result.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="要用 uv 启动评测进程")
def test_runner_never_prints_key(tmp_path):
    # 库连不上：核对那一步就停下，不会走到调模型；整个输出里不能有密钥
    dead = "mysql+pymysql://root@127.0.0.1:1/youju_eval30?charset=utf8mb4"
    result = _run(
        [
            "https://env-test.api.tcloudbasegateway.com/v1/ai/cloudbase",
            "--out",
            str(tmp_path / "out"),
        ],
        f"{KEY}\n",
        YOUJU_EVAL_DATABASE_URL=dead,
    )
    assert result.returncode == 1 and "评测失败：OperationalError" in result.stdout
    assert KEY not in result.stdout + result.stderr
    assert not (tmp_path / "out").exists()


def test_also_types_count_as_right():
    """两可的题：主类别以外，also 里的类别也算对（agy 10/28 复核）。"""
    question = {
        "id": "Q",
        "type": "材料",
        "also": ["方式"],
        "need": "any",
        "gold": [{"sid": "P1", "quote": "打印一式8份逐级签章后寄送"}],
    }
    record = {
        "status": "success",
        "items": [{"type": "方式", "refs": [{"pid": "P1", "quote": "打印一式8份逐级签章后寄送"}]}],
    }
    assert eq.score(question, record)["pass"] is True
    assert eq.score({**question, "also": []}, record)["status"] == "wrong_type"
