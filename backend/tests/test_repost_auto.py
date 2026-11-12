from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import combinations

import pytest

from youju.models import RepostGroup, RepostGroupMember
from youju.repost import (
    AUTO_REVIEWER,
    Candidate,
    Cited,
    auto_reason,
    auto_upstream,
    cited_upstream,
    pair_strength,
    signals,
    withdraw_group,
)


@pytest.fixture
def example():
    group = RepostGroup(
        strength="strong", created_by="rule", no_auto=False, needs_review=False, revision=1
    )
    rows = [RepostGroupMember(document_id=i, active=True, joined_by="rule") for i in (1, 2, 3)]
    cands = {
        i: Candidate(
            id=i,
            title="关于开展创新产业发展项目申报工作的通知",
            kind="申报通知",
            province=str(i),
            dept="科技",
            publish_date=date(2026, 1, i),
            has_body=True,
            cited=(Cited("工信厅办函〔2026〕1号", "项目申报通知"),),
        )
        for i in (1, 2, 3)
    }
    return group, rows, cands


def test_eligible_and_pairs(example):
    group, rows, cands = example
    assert auto_reason(group, rows, cands, set()) == ""
    assert auto_upstream(rows, cands, set()) == "工信厅办函〔2026〕1号"
    for a, b in combinations(cands.values(), 2):
        assert pair_strength(a, signals(a), b, signals(b)) == "strong"


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "关于开展创新产业发展另一方向项目申报工作的通知"},
        {"cited": (Cited("工信厅联节函〔2026〕1号", "通知"),)},
        {"cited": ()},  # 管理办法不会被 cited_upstream 收录
        {"kind": "名单"},
        {"kind": "政策"},
        {"kind": "其他"},
        {"province": "1"},
        {"title": "关于开展省级创新产业发展项目申报工作的通知"},
        {"has_body": False},
        {"title": "关于2025年创新产业发展项目申报工作的通知"},
        {"publish_date": date(2026, 5, 1)},
    ],
)
def test_rejects_counterexamples(example, changes):
    group, rows, cands = example
    cands[2] = replace(cands[2], **changes)
    assert auto_reason(group, rows, cands, set())


def test_chain_and_duplicates(example):
    group, rows, cands = example
    other = Cited("工信厅办函〔2026〕2号", "通知")
    cands[2] = replace(cands[2], cited=(*cands[2].cited, other))
    cands[3] = replace(cands[3], cited=(other,))
    assert auto_reason(group, rows, cands, set())
    assert not auto_reason(group, rows, cands, {3})
    assert auto_reason(group, rows, cands, {2, 3})
    del cands[1]
    assert auto_reason(group, rows, cands, {3})


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("group", "no_auto", True),
        ("group", "needs_review", True),
        ("group", "created_by", "human"),
        ("group", "strength", "weak"),
        ("row", "active", False),
        ("row", "joined_by", "human"),
        ("row", "confirmed_at", datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_human_traces(example, target, field, value):
    group, rows, cands = example
    setattr(group if target == "group" else rows[0], field, value)
    assert auto_reason(group, rows, cands, set())


def test_auto_confirmation_and_withdraw(example):
    group, rows, cands = example
    stamp = datetime.now(UTC)
    for row in rows:
        row.confirmed_by, row.confirmed_at, row.confirmed_fingerprint = AUTO_REVIEWER, stamp, "x"
    assert not auto_reason(group, rows, cands, set())
    assert withdraw_group(group, rows, stamp + timedelta(seconds=1)) == [1, 2, 3]
    assert group.no_auto and not group.needs_review and group.revision == 2
    assert all(
        r.joined_by == "human"
        and r.confirmed_at is None
        and r.confirmed_by is None
        and r.confirmed_fingerprint is None
        for r in rows
    )


def test_private_table():
    from youju.public_snapshot import EXCLUDED_TABLES, PUBLIC_TABLES
    from youju.sync.tables import SYNC_TABLES

    assert "repost_auto_checks" in EXCLUDED_TABLES
    assert "repost_auto_checks" not in PUBLIC_TABLES | SYNC_TABLES


def test_migration_schema_operations(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic/versions/0055_repost_auto_checks.py"
    spec = importlib.util.spec_from_file_location("repost_auto_checks_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    calls = []
    for name in ("create_table", "create_index", "drop_index", "drop_table"):
        monkeypatch.setattr(
            migration.op, name, lambda *args, _name=name, **kw: calls.append((_name, args))
        )
    migration.upgrade()
    migration.downgrade()
    assert migration.revision == "0055" and migration.down_revision == "0054"
    assert [c[0] for c in calls] == ["create_table", "create_index", "drop_index", "drop_table"]


def test_auto_merge_default_off(monkeypatch):
    from youju.config import Settings

    monkeypatch.delenv("YOUJU_REPOST_AUTO_MERGE", raising=False)
    assert Settings(_env_file=None).repost_auto_merge is False
    monkeypatch.setenv("YOUJU_REPOST_AUTO_MERGE", "true")
    assert Settings(_env_file=None).repost_auto_merge is True


def test_short_core_titles_are_not_merged(example):
    group, rows, cands = example
    for i in cands:
        cands[i] = replace(cands[i], title="关于开展申报工作的通知")
    assert auto_reason(group, rows, cands, set()) == "核心标题太短"


def _rule_battery() -> list:
    """判定条件的一组固定样例的结论，加上判定用到的几样规则在固定样例上的结果：条件、core_title、
    cited_upstream、own_number、分类规则（effective_kind）一变，这些结果就可能变（见
    test_rule_version_is_bumped_when_the_rules_change）。"""
    from youju.classify import effective_kind
    from youju.repost import core_title, own_number

    group = RepostGroup(
        strength="strong", created_by="rule", no_auto=False, needs_review=False, revision=1
    )

    def cand(i, **changes):
        base = Candidate(
            id=i,
            title="关于开展创新产业发展项目申报工作的通知",
            kind="申报通知",
            province=str(i),
            dept="科技",
            publish_date=date(2026, 1, i),
            has_body=True,
            cited=(Cited("工信厅办函〔2026〕1号", "项目申报通知"),),
        )
        return replace(base, **changes)

    variants = [
        {},
        {"title": "关于开展创新产业发展另一方向项目申报工作的通知"},
        {"cited": (Cited("工信厅联节函〔2026〕1号", "通知"),)},
        {"cited": ()},
        {"kind": "名单"},
        {"title": "关于开展省级创新产业发展项目申报工作的通知"},
        {"has_body": False},
        {"title": "关于2025年创新产业发展项目申报工作的通知"},
        {"publish_date": date(2026, 5, 1)},
        {"title": "转发关于开展创新产业发展项目申报工作的通知"},
        {"title": "关于组织开展创新产业发展项目申报推荐工作的通知"},
    ]
    out = []
    for changes in variants:
        rows = [RepostGroupMember(document_id=i, active=True, joined_by="rule") for i in (1, 2, 3)]
        cands = {1: cand(1), 2: cand(2, **changes), 3: cand(3)}
        out.append(auto_reason(group, rows, cands, set()))
    # 部里的原件不引用自己，按自己的发文字号对上（协调会话 11/13 口径）
    number = "工信厅办函〔2026〕1号"
    original = {"cited": (), "own_number": number}
    ministry = "工业和信息化部办公厅关于开展创新产业发展项目申报工作的通知"
    for changes in (
        {1: original},
        {1: {"cited": (), "own_number": "工信厅办函〔2026〕2号"}},
        {1: {"cited": (), "own_number": None}},
        {1: original, 2: original},
        {1: {**original, "title": ministry}, 2: original},
        {1: original, 2: original, 3: original},
        {1: {**original, "kind": "其他"}},
        {1: {**original, "has_body": False}},
    ):
        rows = [RepostGroupMember(document_id=i, active=True, joined_by="rule") for i in (1, 2, 3)]
        cands = {i: cand(i, **changes.get(i, {})) for i in (1, 2, 3)}
        out.append(auto_reason(group, rows, cands, set()))
    # 判定用到的几样规则在固定样例上的结果
    bodies = (
        "现将《关于组织开展2026年度视听系统典型案例征集工作的通知》（工信厅联电子函〔2026〕376号）"
        "转发给你们",
        "根据《创新产业发展项目管理办法》（工信部规〔2026〕1号）",
        "按照《关于发布创新产业发展项目申报指南的通知》（国科发资〔2026〕2号）",
        "转发《关于开展创新产业发展项目申报工作的通知》（陕工信发〔2026〕3号）",
        "《关于开展创新产业发展项目申报工作的通知》（工信厅函[2024]5号）",
        "《关于开展创新产业发展项目申报工作的通知》（工信厅函【2026】05号）",
    )
    out.extend([[c.number, c.title] for c in cited_upstream(body, 2026)] for body in bodies)
    titles = (
        "关于开展创新产业发展项目申报工作的通知",
        "关于转发工业和信息化部办公厅关于组织开展2026年度视听系统典型案例征集工作的通知的通知",
        "关于公布2026年度创新产业发展项目入选名单的通知",
        "关于印发《创新产业发展项目管理办法》的通知",
        "关于2026年第三季度创新产业发展情况的通报",
        "关于开展省级创新产业发展项目申报工作的通知",
    )
    out.extend(effective_kind(t, None) for t in titles)
    out.extend(core_title(t) for t in titles)
    out.extend(
        own_number(n)
        for n in ("工信厅办函[2026]01号", "（工信厅办函〔2026〕1号）", "工信厅办函〔2026〕1号、2号")
    )
    return out


# 判定条件的版本和这组样例结论的指纹。测试失败说明判定的结果变了（条件、核心标题、上游文号、
# 自己的发文字号、分类规则改了）：放宽了要把 repost.AUTO_RULE_VERSION 换成新日期（旧的抽查不再
# 算数，要重新抽 100 组），再把这里两项一起更新；修紧不换版本号，只更新指纹（下一轮重建会把不再
# 满足的组退回待确认，每周 Codex 抽 10 组照常盯着，协调会话 11/13 定的规矩）
RULE_FINGERPRINT = ("2026-11-13", "20a9852ff391ad8c")


def test_rule_version_is_bumped_when_the_rules_change():
    import hashlib
    import json

    from youju.repost import AUTO_RULE_VERSION

    digest = hashlib.sha256(json.dumps(_rule_battery(), ensure_ascii=False).encode()).hexdigest()
    assert (AUTO_RULE_VERSION, digest[:16]) == RULE_FINGERPRINT


def test_ministry_original_counts_by_its_own_number(example):
    # 部里的原件不引用自己：按它自己的发文字号（详情里页面元数据或正文头上的），和转发件引用的完全
    # 一致才算对上（协调会话 11/13 口径）；别的条件一样不放
    group, rows, cands = example
    number = "工信厅办函〔2026〕1号"
    cands[1] = replace(cands[1], cited=(), own_number=number)
    assert auto_reason(group, rows, cands, set()) == ""
    assert auto_upstream(rows, cands, set()) == number
    for a, b in combinations(cands.values(), 2):
        assert pair_strength(a, signals(a), b, signals(b)) == "strong"
    other = replace(cands[1], own_number="工信厅办函〔2026〕2号")
    assert auto_reason(group, rows, {**cands, 1: other}, set()) == "没有共同上游文号"
    none = replace(cands[1], own_number=None)
    assert auto_reason(group, rows, {**cands, 1: none}, set()) == "缺少上游文号"
    # 两份都一点上游证据没有：这两份之间不是强证据，不合；其中一份标题带部委就行
    bare = {**cands, 2: replace(cands[2], cited=(), own_number=number)}
    assert auto_reason(group, rows, bare, set()) == "两份以上没有上游证据"
    titled = replace(cands[1], title="工业和信息化部办公厅关于开展创新产业发展项目申报工作的通知")
    assert auto_reason(group, rows, {**bare, 1: titled}, set()) == ""
    # 没有一份转发件引用它：对不上
    alone = {i: replace(c, cited=(), own_number=number) for i, c in cands.items()}
    assert auto_reason(group, rows, alone, set()) == "没有共同上游文号"
    for changes in (
        {"kind": "其他"},
        {"has_body": False},
        {"title": "关于开展创新产业发展另一方向项目申报工作的通知"},
        {"province": "2"},
    ):
        assert auto_reason(group, rows, {**cands, 1: replace(cands[1], **changes)}, set())


def test_only_the_given_versions_are_judged(example):
    # 只判给的几行（已经自动确认的和新来的一起判）；人工痕迹照样看全部组员行
    group, rows, cands = example
    cands[3] = replace(cands[3], has_body=False)
    assert auto_reason(group, rows, cands, set()) == "缺少正文"
    assert auto_reason(group, rows, cands, set(), versions=rows[:2]) == ""
    assert auto_upstream(rows, cands, set(), versions=rows[:2]) == "工信厅办函〔2026〕1号"
    rows[2].joined_by = "human"
    assert auto_reason(group, rows, cands, set(), versions=rows[:2]) == "组员有人工处理"
