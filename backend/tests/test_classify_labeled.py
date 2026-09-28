"""按标题分类的评测集：2026-09-28 验收 A5 抽样的真实标题（开发库随机抽样，五批去重后 489 条），
每条由两名成员独立判断（第一至四批 Claude 和 agy，第五批 Claude 和 Codex）。

- expected：两人判断一致的写一个类别；不一致的两种都算对；职称申报通知按 #43 的规则算申报通知。
- 第一至四批都参与过调规则，第五批没有。所以这里的正确率不代表对新数据的正确率，它的用处是防止
  以后改规则时退步。对新数据的正确率要另抽样本衡量：第四、五批（调规则之前）都是 89%，见
  docs/迭代/验收A5抽样记录_2026-09-28.md。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from youju.classify import doc_kind, kind_expr
from youju.models import Document, Source

LABELED = json.loads(
    (Path(__file__).parent / "fixtures" / "classify_labeled.json").read_text(encoding="utf-8")
)
# 改规则时正确率不能低于这个下限；确实提高了再同步调高
MIN_ACCURACY = 0.95


def test_accuracy_on_labeled_titles() -> None:
    wrong = [
        (x["title"], doc_kind(x["title"]), x["expected"])
        for x in LABELED
        if doc_kind(x["title"]) not in x["expected"]
    ]
    accuracy = 1 - len(wrong) / len(LABELED)
    assert accuracy >= MIN_ACCURACY, f"正确率 {accuracy:.1%}，判错的：{wrong}"


def test_sql_matches_python_on_labeled_titles(session) -> None:
    """列表筛选、分页用的数据库分类，在全部评测标题上都要与 Python 规则一致。"""
    src = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知",
        url="https://kjt.shandong.gov.cn/x/",
        enabled=True,
        parser_config={},
    )
    session.add(src)
    session.flush()
    now = datetime.now(UTC)
    for i, item in enumerate(LABELED):
        session.add(
            Document(
                source_id=src.id,
                url=f"https://kjt.shandong.gov.cn/labeled/{i}.html",
                url_key=f"kjt.shandong.gov.cn/labeled/{i}.html",
                title=item["title"],
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    session.flush()
    rows = session.execute(select(Document.title, kind_expr(Document.title))).all()
    assert len(rows) == len(LABELED)
    mismatched = [
        (title, sql_kind, doc_kind(title))
        for title, sql_kind in rows
        if sql_kind != doc_kind(title)
    ]
    assert mismatched == []
