"""本地高危词库（第九节第 3、6 条）。测试只用无害的测试词，不放真的违规词。"""

import json

import pytest

from youju import safety_words
from youju.safety_words import Hit, check, load, parse


def library(*entries, final=True, version="test-1"):
    return parse(
        {
            "version": version,
            "final": final,
            "categories": {
                "illegal_other": {"group": "违法信息", "name": "其他"},
                "harmful_vulgar": {"group": "不良信息", "name": "低俗"},
            },
            "entries": [
                {"id": entry_id, "category": category, "word": word}
                for entry_id, category, word in entries
            ],
        }
    )


WORDS = library(
    ("t1", "illegal_other", "测试违禁词甲"),
    ("t2", "harmful_vulgar", "TestBadWord"),
    ("t3", "illegal_other", "测试违禁词甲乙"),
    ("t4", "harmful_vulgar", "测试词丙"),
)


def test_shipped_library_is_a_draft_placeholder():
    """随代码发布的词库还没定稿：没有词条、final 为假（问一问不开放），类别按《网络信息内容生态
    治理规定》第六条 11 类、第七条 9 类列好。"""
    shipped = load()
    assert shipped == safety_words.LIBRARY
    assert shipped.version == "draft-0"
    assert shipped.final is False
    assert shipped.entries == ()
    groups = [info["group"] for info in shipped.categories.values()]
    assert groups.count("违法信息") == 11 and groups.count("不良信息") == 9
    assert len(groups) == 20
    assert check("专精特新申报什么时候截止") is None


def test_hit_reports_category_entry_and_version():
    assert check("请问测试违禁词甲是什么", WORDS) == Hit("illegal_other", "t1", "test-1")
    assert check("没有命中的正常问题", WORDS) is None


@pytest.mark.parametrize(
    "text",
    [
        "测试 违禁\n词甲",  # 中间夹空白、换行
        "测试\N{IDEOGRAPHIC SPACE}违禁词甲",  # 全角空格
        "测试\N{ZERO WIDTH SPACE}违禁词甲",
        "测试\N{ZERO WIDTH NO-BREAK SPACE}违禁词\N{WORD JOINER}甲",
        # 其他显示时看不见的字符（Unicode 的"默认可忽略"）：软连字符、方向控制符、变体选择符、
        # 标签字符、韩文填充符
        "测试\N{SOFT HYPHEN}违禁词甲",
        "测试违禁\N{VARIATION SELECTOR-16}词\N{RIGHT-TO-LEFT OVERRIDE}甲",
        "测试\N{LEFT-TO-RIGHT MARK}违禁词\N{TAG LATIN SMALL LETTER A}甲",
        "测\N{HANGUL FILLER}试违禁\N{COMBINING GRAPHEME JOINER}词甲",
    ],
)
def test_spaces_inside_word_still_hit(text):
    assert check(text, WORDS).entry_id == "t1"


@pytest.mark.parametrize(
    "text", ["testbadword", "TESTBADWORD", "ＴｅｓｔＢａｄＷｏｒｄ", "Test Bad Word"]
)
def test_width_and_case_ignored(text):
    assert check(text, WORDS) == Hit("harmful_vulgar", "t2", "test-1")


def test_earliest_then_longest():
    assert check("先说测试词丙，再说测试违禁词甲", WORDS).entry_id == "t4"
    # 同一个位置开头的两个词取长的
    assert check("测试违禁词甲乙", WORDS).entry_id == "t3"


def test_text_joined_from_chunks_checked_as_a_whole():
    """check 查传进来的全部文字：几段拼起来才成的词也查得到（收完整段用它兜底；流式接收时每段
    只查窗口，见 test_chat_ask 里 _Guard 的测试）。"""
    chunks = ["根据通知第三条，测试违", "禁词", "甲在附件里"]
    received = ""
    hits = []
    for chunk in chunks:
        received += chunk
        hits.append(check(received, WORDS))
    assert hits[:2] == [None, None]
    assert hits[2] == Hit("illegal_other", "t1", "test-1")


def test_longest_entry_length():
    """最长词条的长度（处理过以后的字数）：载入时算好，流式接收每段都要用；没有词条为 0。"""
    assert WORDS.longest == len("testbadword") == 11
    assert library(final=False).longest == 0
    assert library(("t1", "illegal_other", "甲 \N{SOFT HYPHEN}乙")).longest == 2  # 处理过以后的字数


def test_invisible_char_does_not_block_combining():
    """看不见的字符在规范化之前就去掉：组合用字形连接符、变体选择符夹在字母和组合符号中间，
    去掉以后照样合成（Codex 第 3 轮）。"""
    accented = library(("t1", "illegal_other", "café"))
    assert safety_words.normalize("e\N{COMBINING GRAPHEME JOINER}\u0301") == "é"
    assert check("cafe\N{COMBINING GRAPHEME JOINER}\u0301", accented).entry_id == "t1"
    assert check("cafe\N{VARIATION SELECTOR-1}\u0301", accented).entry_id == "t1"
    assert safety_words.strip_invisible("甲\u200b乙 丙") == "甲乙 丙"  # 空白留到规范化以后再去


def test_check_does_not_depend_on_final():
    draft = library(("t1", "illegal_other", "测试违禁词甲"), final=False)
    assert draft.final is False
    assert check("测试违禁词甲", draft) == Hit("illegal_other", "t1", "test-1")


def test_final_defaults_to_false():
    data = {"version": "x", "categories": {"c": {"name": "类"}}, "entries": []}
    assert parse(data).final is False


def test_default_library_is_used(monkeypatch):
    monkeypatch.setattr(safety_words, "LIBRARY", WORDS)
    assert check("测试违禁词甲") == Hit("illegal_other", "t1", "test-1")


def test_load_from_file(tmp_path):
    path = tmp_path / "words.json"
    data = {
        "version": "test-2",
        "final": True,
        "categories": {"illegal_other": {"name": "其他"}},
        "entries": [{"id": "a", "category": "illegal_other", "word": "测试违禁词乙"}],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    words = load(path)
    assert words.final is True
    assert check("……测试违禁词乙……", words) == Hit("illegal_other", "a", "test-2")


BASE = {
    "version": "test-1",
    "categories": {"illegal_other": {"name": "其他"}},
    "entries": [{"id": "a", "category": "illegal_other", "word": "测试违禁词甲"}],
}


@pytest.mark.parametrize(
    "change",
    [
        {"version": ""},
        {"version": None},
        {"final": "false"},
        {"categories": {}},
        {"categories": {"illegal_other": {"group": "违法信息"}}},  # 没有名称
        {"entries": {}},
        {"entries": [{"id": "a", "category": "unknown", "word": "测试违禁词甲"}]},
        {"entries": [{"id": "a", "category": "illegal_other", "word": " \N{ZERO WIDTH SPACE} "}]},
        {"entries": [{"category": "illegal_other", "word": "测试违禁词甲"}]},
        {
            "entries": [
                {"id": "a", "category": "illegal_other", "word": "测试违禁词甲"},
                {"id": "a", "category": "illegal_other", "word": "测试违禁词乙"},
            ]
        },
        {"final": True, "entries": []},  # 定稿的不能没有词条
        {"entries": ["测试违禁词甲"]},
    ],
)
def test_bad_library_refused(change):
    with pytest.raises(ValueError) as error:
        parse(BASE | change)
    assert "测试违禁词" not in str(error.value)  # 报错里不写词
