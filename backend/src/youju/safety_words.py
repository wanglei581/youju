"""本地高危词库：内容安全的第一层（D-093 门槛第 4 条；带 AI 提审补齐清单第 4 道）。

在本机比，不发给任何人。两处用：
- AI 解读、条款对照的结果存进去之前（ai/result_safety.py），命中就不显示、记处置记录；
- 问一问的提问和回答（问一问开发设计第九节第 3、6 条，PR-2、PR-3）。流式接收时每收到一段，
  就把已经收到的全部文字交给 check，跨段的词也查得到。

**现在的词库只是草稿占位**（data/safety_words.json：version 是 draft-0，final 为 false，
没有词条）。词库从哪来、分哪些类别、谁来维护，要经合规审定、创始人定；定稿（final 为 true）以前
问一问不开放（看 Library.final）。这里只做机制：类别先按《网络信息内容生态治理规定》第六条
（违法信息 11 类）、第七条（不良信息 9 类）列出，词条以后补，每条 {"id", "category", "word"}。
改词库就升版本，处置记录里记版本（Hit.version）。

比较前，词条和要查的文字做同样的处理：NFKC（全角转半角）、casefold（转小写）、去掉空白和零宽字符。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PATH = Path(__file__).parent / "data" / "safety_words.json"
# 空白和零宽字符（零宽空格、零宽非连接符、零宽连接符、字连接符、BOM）：夹在词中间也照样认得出
_GAPS = re.compile(
    r"[\s\N{ZERO WIDTH SPACE}-\N{ZERO WIDTH JOINER}\N{WORD JOINER}\N{ZERO WIDTH NO-BREAK SPACE}]+"
)


def normalize(text: str) -> str:
    """比较用的写法：NFKC、casefold，去掉空白和零宽字符。"""
    return _GAPS.sub("", unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Hit:
    """命中的词条。违规记录的原因写这三样，不写词本身。"""

    category: str  # 类别代码，如 illegal_rumor
    entry_id: str  # 词条编号
    version: str  # 词库版本


@dataclass(frozen=True)
class Entry:
    id: str
    category: str
    word: str  # 处理过（normalize）的词


@dataclass(frozen=True)
class Library:
    version: str
    categories: dict[str, dict[str, Any]]  # 类别代码 → {"group", "clause", "name"}
    entries: tuple[Entry, ...]
    final: bool = False  # 经合规审定的定稿；为假时问一问不开放。check 不看它，照样查
    # 按词的第一个字分组，同一组里长的在前：查一段文字时每个位置只比这个字开头的几条
    _by_first: dict[str, tuple[Entry, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        groups: dict[str, list[Entry]] = {}
        for entry in sorted(self.entries, key=lambda e: -len(e.word)):
            groups.setdefault(entry.word[0], []).append(entry)
        object.__setattr__(self, "_by_first", {k: tuple(v) for k, v in groups.items()})


def parse(data: Any) -> Library:
    """检查词库的格式、处理好词条。格式不对抛 ValueError（报错里只写词条编号，不写词）。"""
    if not isinstance(data, dict):
        raise ValueError("词库要是一个 JSON 对象")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("词库没有版本号 version")
    final = data.get("final", False)
    if not isinstance(final, bool):
        raise ValueError("词库的 final 只能是 true 或 false")
    categories = data.get("categories")
    if (
        not isinstance(categories, dict)
        or not categories
        or not all(
            isinstance(info, dict) and isinstance(info.get("name"), str) and info["name"]
            for info in categories.values()
        )
    ):
        raise ValueError("词库的类别 categories 不对：每个类别要有名称 name")
    items = data.get("entries")
    if not isinstance(items, list):
        raise ValueError("词库的词条 entries 要是列表")
    entries: list[Entry] = []
    seen: set[str] = set()
    for n, item in enumerate(items, start=1):
        entry_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(entry_id, str) or not entry_id.strip() or entry_id in seen:
            raise ValueError(f"第 {n} 个词条的编号 id 没有写或者重复了")
        category = item.get("category")
        if not isinstance(category, str) or category not in categories:
            raise ValueError(f"词条 {entry_id} 的类别不在 categories 里")
        word = item.get("word")
        word = normalize(word) if isinstance(word, str) else ""
        if not word:
            raise ValueError(f"词条 {entry_id} 的词是空的")
        seen.add(entry_id)
        entries.append(Entry(entry_id, category, word))
    if final and not entries:
        raise ValueError("定稿的词库（final 为 true）不能没有词条")
    return Library(version, categories, tuple(entries), final)


def load(path: str | Path | None = None) -> Library:
    """读词库文件，默认是随代码发布的 data/safety_words.json。"""
    with open(PATH if path is None else path, encoding="utf-8") as f:
        return parse(json.load(f))


# 启动时载入内存。文件坏了就起不来（测试会先发现），不会带着坏词库放行
LIBRARY = load()


def check(text: str, library: Library | None = None) -> Hit | None:
    """text 里有没有词库里的词：有就返回最靠前的那一个（同一位置取最长的），没有返回 None。

    library 不给就用启动时载入的 LIBRARY。流式接收回答时，每收到一段就把已经收到的全部文字传进来
    （不是只传新的那一段）。词库定没定稿都照样查。"""
    library = LIBRARY if library is None else library
    plain = normalize(text)
    for at, char in enumerate(plain):
        for entry in library._by_first.get(char, ()):
            if plain.startswith(entry.word, at):
                return Hit(entry.category, entry.id, library.version)
    return None
