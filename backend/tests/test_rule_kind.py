"""文件类型存成一列（rule_kind，验收 C1）：入库时算好，规则改了由 youju reclassify 重算；
筛选结果和逐行按规则判断完全一样。"""

from datetime import UTC, date, datetime

from sqlalchemy import select, update

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
