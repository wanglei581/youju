"""词库的误拦检查（scripts/scan_safety_words.py）：在收录的正文和附件文字上跑一遍，只报命中。"""

import importlib.util
from pathlib import Path

from tests.test_ai_interpret import NOW
from tests.test_ai_interpret import doc as doc  # noqa: F401
from tests.test_ai_interpret import settings as settings  # noqa: F401
from youju import safety_words

SPEC = importlib.util.spec_from_file_location(
    "scan_safety_words", Path(__file__).parents[1] / "scripts" / "scan_safety_words.py"
)
scan_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan_module)


def library(*words):
    return safety_words.parse(
        {
            "version": "test-1",
            "final": False,
            "categories": {"test": {"name": "测试类别"}},
            "entries": [
                {"id": f"t{n}", "category": "test", "word": word} for n, word in enumerate(words, 1)
            ],
        }
    )


def test_scan_reports_hits_without_text(session, doc):  # noqa: F811
    result = scan_module.scan(session, library("申请书", "不会出现的短语"))
    assert result["bodies"] == 1
    assert list(result["hits"]) == ["t1"]
    assert result["hits"]["t1"]["bodies"] == [(doc.id, doc.title)]
    text = scan_module.report(result, library("申请书", "不会出现的短语"))
    assert f"通知 {doc.id}" in text and "请提交申请书" not in text  # 不打印正文


def test_scan_without_hits(session, doc):  # noqa: F811
    words = library("不会出现的短语")
    result = scan_module.scan(session, words)
    assert result["hits"] == {}
    assert "没有命中" in scan_module.report(result, words)
    assert NOW  # 夹具按 test_ai_interpret 的时间建数据


def test_starter_draft_is_valid_and_not_final():
    """起步稿能读、格式对、还没定稿（创始人定了才放进 data/safety_words.json）。"""
    path = Path(__file__).parents[2] / "docs" / "合规" / "本地高危词库_起步稿_v1.json"
    draft = safety_words.load(path)
    assert draft.final is False and len(draft.entries) >= 20
    assert all(len(entry.word) >= 4 for entry in draft.entries)  # 按短语，不按单字
    shipped = safety_words.load()
    assert shipped.entries == ()  # 随代码发布的还是空的
