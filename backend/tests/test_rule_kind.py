"""文件类型存成一列（rule_kind，验收 C1）：入库时算好，规则改了由 youju reclassify 重算；
筛选结果和逐行按规则判断完全一样。"""

from datetime import UTC, date, datetime

from sqlalchemy import select, update

from youju.api import v1
from youju.api.v1 import _kind_is
from youju.classify import doc_kind, kind_filter
from youju.crawler.listing import ListItem
from youju.crawler.service import save_items
from youju.models import Document, Source

TITLES = [
    "关于组织申报2026年度科技计划项目的通知",
    "关于公布2026年第一批高新技术企业名单的通知",
    "关于印发《科技企业管理办法》的通知",
    "关于2026年度职称评审结果的公示",
    "2026年第三季度工作简报",
    "关于开展2026年度高新技术企业认定工作的通知",
]


def add_source(session):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知公告",
        url="https://kjt.shandong.gov.cn/tzgg/",
        parser_config={},
    )
    session.add(source)
    session.commit()
    return source


def test_orm_and_bulk_inserts_store_the_rule_kind(session) -> None:
    source = add_source(session)
    now = datetime.now(UTC)
    one = Document(
        source_id=source.id,
        url="https://kjt.shandong.gov.cn/tzgg/a.html",
        url_key="kjt.shandong.gov.cn/tzgg/a.html",
        title=TITLES[1],
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(one)
    session.commit()
    assert one.rule_kind == doc_kind(TITLES[1]) == "名单"
    items = [
        ListItem(title, f"https://kjt.shandong.gov.cn/tzgg/{i}.html", date(2026, 9, 20))
        for i, title in enumerate(TITLES)
    ]
    save_items(session, source, items, baseline=False, seen_at=now)
    session.commit()
    stored = dict(session.execute(select(Document.title, Document.rule_kind)).all())
    assert all(stored[t] == doc_kind(t) for t in TITLES)


def test_filter_matches_the_rules_row_by_row_even_before_reclassify(session) -> None:
    source = add_source(session)
    now = datetime.now(UTC)
    items = [
        ListItem(title, f"https://kjt.shandong.gov.cn/tzgg/{i}.html", date(2026, 9, 20))
        for i, title in enumerate(TITLES)
    ]
    save_items(session, source, items, baseline=False, seen_at=now)
    session.commit()
    kinds = ["申报通知", "名单", "政策", "其他"]

    def by_column(kind):
        return set(session.scalars(select(Document.title).where(_kind_is([kind]))))

    def by_rules(kind):
        return set(
            session.scalars(select(Document.title).where(kind_filter(Document.title, [kind])))
        )

    expected = {k: by_rules(k) for k in kinds}
    assert {k: by_column(k) for k in kinds} == expected
    # 刚升级、还没重算（列为空）时当场按规则判断，结果一样
    session.execute(update(Document).values(rule_kind=None))
    session.commit()
    assert {k: by_column(k) for k in kinds} == expected


def test_reclassify_fixes_stale_and_missing_kinds(session, session_factory, monkeypatch, capsys):
    from youju import cli

    source = add_source(session)
    now = datetime.now(UTC)
    items = [
        ListItem(title, f"https://kjt.shandong.gov.cn/tzgg/{i}.html", date(2026, 9, 20))
        for i, title in enumerate(TITLES)
    ]
    save_items(session, source, items, baseline=False, seen_at=now)
    session.commit()
    session.execute(update(Document).where(Document.title == TITLES[0]).values(rule_kind="其他"))
    session.execute(update(Document).where(Document.title == TITLES[1]).values(rule_kind=None))
    session.commit()
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    assert cli.main(["reclassify"]) == 0
    assert f"共 {len(TITLES)} 份，更新 2 份" in capsys.readouterr().out
    session.expire_all()
    stored = dict(session.execute(select(Document.title, Document.rule_kind)).all())
    assert all(stored[t] == doc_kind(t) for t in TITLES)


def test_reclassify_cleans_existing_titles_idempotently(
    session, session_factory, monkeypatch, capsys
):
    from youju import cli

    source = add_source(session)
    title = "》 自治区科技厅关于2026年第四批拟入库科技型中小企业名单的公示"
    save_items(
        session,
        source,
        [ListItem(title, "https://kjt.nx.gov.cn/old.html", date(2026, 10, 8))],
        False,
        datetime.now(UTC),
    )
    session.commit()
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    assert cli.main(["reclassify"]) == 0
    assert "更新 1 份" in capsys.readouterr().out
    session.expire_all()
    doc = session.query(Document).one()
    assert doc.title == title[2:] and doc.rule_kind == "名单"
    assert cli.main(["reclassify"]) == 0
    assert "更新 0 份" in capsys.readouterr().out


def _sql_size(session, condition) -> int:
    stmt = select(Document.id).where(condition)
    return len(str(stmt.compile(session.get_bind(), compile_kwargs={"literal_binds": True})))


def test_rule_fallback_left_out_once_all_columns_are_filled(session, monkeypatch):
    """规则列都算完时，筛选的 SQL 不带标题规则兜底（它有几十万字符，线上一次搜索因此要 3–7 秒），
    结果和带兜底时完全一样；有 NULL 的行时照旧带上。"""
    source = add_source(session)
    items = [
        ListItem(title, f"https://kjt.shandong.gov.cn/tzgg/{i}.html", date(2026, 9, 20))
        for i, title in enumerate(TITLES)
    ]
    save_items(session, source, items, baseline=False, seen_at=datetime.now(UTC))
    session.commit()

    def titles(condition):
        return set(session.scalars(select(Document.title).where(condition)))

    conditions = {
        "不是资讯与指南": lambda: v1._kind_is(v1._NOT_GUIDE),
        "申报通知": lambda: v1._kind_is(["申报通知"]),
        "项目类别": lambda: v1._category_is(["资质与梯度培育", "科技计划与研发"]),
        "支持方式": lambda: v1._supports_are(list(v1.SUPPORT_LABELS)[:2]),
        "产业方向": lambda: v1._industries_are(v1.INDUSTRIES[:2]),
    }
    assert v1._rules_complete is False
    with_fallback = {name: titles(make()) for name, make in conditions.items()}
    long_sql = _sql_size(session, v1._kind_is(v1._NOT_GUIDE))

    assert v1.note_rules_complete(session) is True
    assert {name: titles(make()) for name, make in conditions.items()} == with_fallback
    assert with_fallback["申报通知"] and with_fallback["项目类别"]
    assert all(_sql_size(session, make()) < 2000 for make in conditions.values())
    assert long_sql > 100_000

    # 有一行还没算：兜底回来，这一行照样按标题规则筛出来
    session.execute(update(Document).where(Document.title == TITLES[0]).values(rule_kind=None))
    session.commit()
    assert v1.note_rules_complete(session) is False
    assert TITLES[0] in titles(v1._kind_is(["申报通知"]))
    assert _sql_size(session, v1._kind_is(["申报通知"])) > 10_000

    # 确认算完后 60 秒内不重复查库；过了再查
    session.execute(update(Document).values(rule_kind="其他"))
    session.commit()
    monkeypatch.setattr(v1, "_RULES_RECHECK", 60.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(v1._time, "monotonic", lambda: clock["now"])
    assert v1.note_rules_complete(session) is True
    session.execute(update(Document).where(Document.title == TITLES[0]).values(rule_kind=None))
    session.commit()
    clock["now"] = 1059.0
    assert v1.note_rules_complete(session) is True
    clock["now"] = 1061.0
    assert v1.note_rules_complete(session) is False
