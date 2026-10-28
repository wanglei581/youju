"""只读：把本地高危词库放到收录的通知正文和附件文字上跑一遍，看会不会误拦政府原文。

在 backend/ 下运行（库连接看 YOUJU_DATABASE_URL；本机用 tools/team/fresh-test-db.sh
建的公开内容库）：
    uv run python scripts/scan_safety_words.py [--words 词库文件]

为什么要跑：AI 解读、对照显示给用户的全是政府原文的原句，结果存进去之前要过这个词库
（门槛第 4 道）。词库误拦了政府原文，正常的解读就显示不出来。词条定稿前要跑到命中 0 份。

只打印每个词条命中了几份、前几份的通知编号和标题（附件只给版本编号）；不打印正文，不写数据库。
有命中时退出码是 1。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju import safety_words
from youju.config import get_settings
from youju.db import create_mysql_engine
from youju.models import AttachmentTextSegment, Document, DocumentDetail

SAMPLES = 5


def scan(session: Session, library: safety_words.Library) -> dict:
    """扫一遍，返回扫了几份正文（bodies）、几段附件文字（segments），和每个词条命中的
    通知（编号、标题）和附件文字版本（hits）。"""
    hits: dict[str, dict[str, list]] = defaultdict(lambda: {"bodies": [], "segments": []})
    entries = library.entries
    bodies = segments = 0
    rows = session.execute(
        select(Document.id, Document.title, DocumentDetail.body_text)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .execution_options(yield_per=500)
    )
    for doc_id, title, body in rows:
        bodies += 1
        plain = safety_words.normalize(body or "")
        for entry in entries:
            if entry.word in plain:
                hits[entry.id]["bodies"].append((doc_id, title))
    parts = session.execute(
        select(AttachmentTextSegment.version_id, AttachmentTextSegment.text).execution_options(
            yield_per=2000
        )
    )
    for version_id, text in parts:
        segments += 1
        plain = safety_words.normalize(text or "")
        for entry in entries:
            if entry.word in plain:
                hits[entry.id]["segments"].append(version_id)
    return {"bodies": bodies, "segments": segments, "hits": dict(hits)}


def report(result: dict, library: safety_words.Library) -> str:
    lines = [
        f"词库 {library.version}（{len(library.entries)} 条）；扫了 {result['bodies']} 份正文、"
        f"{result['segments']} 段附件文字。",
    ]
    if not result["hits"]:
        lines.append("没有命中：不会误拦已收录的政府原文。")
        return "\n".join(lines)
    category = {entry.id: entry.category for entry in library.entries}
    for entry_id, found in sorted(result["hits"].items()):
        docs, versions = found["bodies"], sorted(set(found["segments"]))
        lines.append(
            f"- 词条 {entry_id}（{category.get(entry_id, '?')}）：正文 {len(docs)} 份、"
            f"附件 {len(versions)} 份"
        )
        for doc_id, title in docs[:SAMPLES]:
            lines.append(f"    通知 {doc_id}《{(title or '')[:40]}》")
        if versions:
            lines.append(f"    附件文字版本 {versions[:SAMPLES]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--words", type=Path, help="词库文件（默认随代码发布的那一份）")
    args = parser.parse_args(argv)
    library = safety_words.load(args.words)
    engine = create_mysql_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            result = scan(session, library)
    finally:
        engine.dispose()
    print(report(result, library))
    return 1 if result["hits"] else 0


if __name__ == "__main__":
    sys.exit(main())
