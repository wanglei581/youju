"""评测脚本只用假客户端；不访问模型。"""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import event, func, select

from youju.ai.client import AIError
from youju.config import Settings
from youju.models import AdvisorTask, AdvisorUsage, AIUsage, Document, DocumentDetail, Source

SPEC = importlib.util.spec_from_file_location(
    "eval_interpret", Path(__file__).parents[1] / "scripts" / "eval_interpret.py"
)
eval_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eval_script)
NOW = datetime(2026, 10, 3, tzinfo=UTC)
QUOTE = "请提交申请材料。"


@pytest.fixture(autouse=True)
def block_real_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("测试不许访问真实模型")

    monkeypatch.setattr(eval_script, "ChatClient", forbidden)


@pytest.fixture
def documents(session):
    ids = []
    for index in range(2):
        source = Source(
            level="省级",
            province="山东",
            dept="科技",
            dept_full_name=f"科技厅{index}",
            column_name="通知",
            url=f"https://official.test/{index}",
            parser_config={},
            rights_level=1,
        )
        session.add(source)
        session.flush()
        for size in (100, 2000, 8000):
            row = Document(
                source_id=source.id,
                title="申报通知",
                kind_override="申报通知",
                url=f"https://official.test/{index}/{size}",
                url_key=f"official.test/{index}/{size}",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add(row)
            session.flush()
            session.add(
                DocumentDetail(
                    document_id=row.id, body_text=QUOTE + "乙" * size, fetched_at=NOW, status="ok"
                )
            )
            ids.append(row.id)
    session.commit()
    return ids


class FakeChat:
    def __init__(self, *args, skill="interpret", **kwargs):
        self.skill = skill
        self.calls = 0
        self.closed = False
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def complete_json(self, system, user, max_tokens):
        self.calls += 1
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        assert system == eval_script.get_skill(self.skill).system_prompt
        assert json.loads(user)["paragraphs"]
        kind = "材料" if self.skill == "interpret" else "条件"
        entry = {"type": kind, "refs": [{"pid": "P001", "quote": QUOTE}]}
        if self.skill == "clause":
            entry["keys"] = ["rd", "rd", "region", "unknown"]
        if self.calls == 2:
            raise AIError("故意放入敏感的异常文本，不允许输出")
        return {"items": [entry, entry, {"type": kind, "refs": [{"quote": "太短"}]}]}

    def close(self):
        self.closed = True


def test_selection_deterministic_source_rotation_and_lengths(session, documents):
    a = eval_script.select_documents(session, "fake", 6, None)
    session.expire_all()
    b = eval_script.select_documents(session, "fake", 6, list(reversed(documents)))
    assert [r["doc_id"] for r in a] == [r["doc_id"] for r in b]
    assert [r["source_id"] for r in a] == [1, 2, 1, 2, 1, 2]
    assert [
        eval_script.length_band(
            len(eval_script.interpret.paragraph_body(r["snapshot"]["paragraphs"]))
        )
        for r in a
    ] == [0, 1, 2, 0, 1, 2]
    assert len(eval_script.select_documents(session, "fake", 2, None)) == 2
    assert len(eval_script.select_documents(session, "fake", 30, [documents[-1]])) == 1
    assert eval_script.select_documents(session, "fake", 30, [99999]) == []
    # 没正文、非一级来源、名单、下架和超长正文不会选入。
    session.get(Document, documents[0]).taken_down_at = NOW
    session.get(Document, documents[1]).kind_override = "名单"
    session.get(Document, documents[2]).detail.body_text = "甲" * 24001
    session.get(Document, documents[3]).detail.body_text = ""
    session.get(Document, documents[4]).source.rights_level = 2
    session.commit()
    assert eval_script.select_documents(session, "fake", 30, None) == []


def test_fake_client_metrics_and_outputs(session, documents, tmp_path, monkeypatch):
    selected = eval_script.select_documents(session, "fake", 3, None)
    ticks = iter((0, 2, 2, 6, 6, 12))
    monkeypatch.setattr(eval_script.time, "monotonic", lambda: next(ticks))
    fake = FakeChat()
    records = eval_script.evaluate(selected, fake)
    assert fake.calls == 3
    assert records[1]["status"] == "error" and records[1]["error_category"] == "AIError"
    summary = eval_script.summarize(records)
    assert summary["document_count"] == 3 and summary["success_count"] == 2
    assert summary["returned_count"] == 6 and summary["kept_count"] == 2
    assert summary["retention_rate"] == pytest.approx(1 / 3)
    assert summary["dropped_reasons"]["duplicate"] == 2
    assert summary["dropped_reasons"]["too_short"] == 2
    assert summary["type_coverage"] == dict(对象=0, 支持=0, 条件=0, 时间=0, 方式=0, 材料=2)
    assert summary["average_elapsed_seconds"] == 4
    assert summary["usage"] == {"prompt_tokens": 30, "completion_tokens": 15}
    eval_script.write_outputs(tmp_path, records)
    assert json.loads((tmp_path / "summary.json").read_text()) == summary
    lines = (tmp_path / "results.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["items"]
    assert "故意放入敏感" not in "\n".join(lines)
    assert "| 成功篇数 | 2 |" in (tmp_path / "summary.md").read_text()
    assert all(
        session.scalar(select(func.count(cls.id))) == 0
        for cls in (AdvisorTask, AdvisorUsage, AIUsage)
    )


def test_no_key_exits_before_database_or_client(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        eval_script, "get_settings", lambda: Settings(_env_file=None, ai_api_key=None)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("无密钥时不应连接数据库")

    monkeypatch.setattr(eval_script, "create_mysql_engine", forbidden)
    assert eval_script.main(["--out", str(tmp_path)]) != 0
    assert capsys.readouterr().out == "没有配置 AI 密钥，只能用 --dry-run\n"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("skill", ["interpret", "clause"])
def test_main_read_only_and_no_queue(
    db_engine, session, documents, tmp_path, monkeypatch, dry_run, skill
):
    settings = Settings(
        _env_file=None,
        database_url=db_engine.url.render_as_string(hide_password=False),
        ai_api_key=None if dry_run else SecretStr("fake"),
        ai_model="fake",
    )
    monkeypatch.setattr(eval_script, "get_settings", lambda: settings)
    monkeypatch.setattr(eval_script, "create_mysql_engine", lambda url: db_engine)
    fake = FakeChat(skill=skill)
    options = []

    def factory(*args, **kwargs):
        options.append(kwargs)
        return fake

    if not dry_run:
        monkeypatch.setattr(eval_script, "ChatClient", factory)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", record)
    try:
        args = [
            "--skill",
            skill,
            "--limit",
            "3",
            "--out",
            str(tmp_path),
            "--doc-ids",
            ",".join(map(str, reversed(documents))),
        ]
        assert eval_script.main(args + (["--dry-run"] if dry_run else [])) == 0
    finally:
        event.remove(db_engine, "before_cursor_execute", record)
    assert statements[0] == "START TRANSACTION READ ONLY"
    assert all(s.lstrip().upper().startswith(("SELECT", "START TRANSACTION")) for s in statements)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert len(summary["doc_ids"]) == 3
    assert fake.calls == (0 if dry_run else 3)
    assert options == ([] if dry_run else [{"timeout": 180.0, "max_retries": 0}])
    assert summary["attempted_count"] == (0 if dry_run else 3)
    if skill == "clause":
        assert summary["key_counts"] == {
            key: (2 if not dry_run and key in ("rd", "region") else 0)
            for key in eval_script.clause.KEYS
        }
        assert summary["empty_keys_count"] == 0
    assert fake.closed is (not dry_run)
    assert all(
        session.scalar(select(func.count(cls.id))) == 0
        for cls in (AdvisorTask, AdvisorUsage, AIUsage)
    )


def test_invalid_model_shape_and_empty_summary(session, documents):
    class BadShape(FakeChat):
        def complete_json(self, *args, **kwargs):
            return {"items": "错误结构"}

    records = eval_script.evaluate(
        eval_script.select_documents(session, "fake", 1, None), BadShape()
    )
    assert records[0]["error_category"] == "ValueError"
    assert eval_script.summarize(records)["success_count"] == 0
    empty = eval_script.summarize([])
    assert empty["retention_rate"] == 0 and empty["average_elapsed_seconds"] == 0


def test_clause_key_and_empty_counts_in_outputs(session, documents, tmp_path):
    class ClauseChat(FakeChat):
        def complete_json(self, system, user, max_tokens):
            payload = super().complete_json(system, user, max_tokens)
            payload["items"].append(
                {"type": "对象", "refs": [{"pid": "P001", "quote": QUOTE}], "keys": []}
            )
            return payload

    records = eval_script.evaluate(
        eval_script.select_documents(session, "fake", 3, None, "clause"),
        ClauseChat(skill="clause"),
        "clause",
    )
    summary = eval_script.summarize(records, "clause")
    assert summary["key_counts"] == {
        key: 2 if key in ("rd", "region") else 0 for key in eval_script.clause.KEYS
    }
    assert summary["empty_keys_count"] == 2
    assert summary["type_coverage"] == {"对象": 2, "条件": 2}
    assert records[1]["status"] == "error" and records[1]["key_counts"]["rd"] == 0
    eval_script.write_outputs(tmp_path, records, "clause")
    assert json.loads((tmp_path / "summary.json").read_text()) == summary
    assert "items" not in json.loads((tmp_path / "results.jsonl").read_text().splitlines()[0])
    assert "| keys 为空 | 2 |" in (tmp_path / "summary.md").read_text()


def test_body_length_includes_trailing_newlines(session, documents):
    body = "\n\n" + QUOTE + "\n\n"
    session.get(Document, documents[0]).detail.body_text = body
    session.commit()
    selected = eval_script.select_documents(session, "fake", 1, [documents[0]])
    assert eval_script.evaluate(selected, None)[0]["body_length"] == len(body)


def test_configuration_errors_do_not_print_sensitive_values(monkeypatch, capsys):
    def invalid_settings():
        raise ValueError("配置异常中可能有敏感值，禁止输出")

    monkeypatch.setattr(eval_script, "get_settings", invalid_settings)
    assert eval_script.main(["--dry-run"]) == 1
    assert capsys.readouterr().out == "评测失败：ValueError\n"
