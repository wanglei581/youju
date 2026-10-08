"""资讯与指南（默认折叠）、"其他"里真申报的建议与批量确认、项目类别（D-051 第二阶段）。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from youju.api import v1
from youju.api.app import app
from youju.categories import (
    CATEGORIES,
    OTHER_CATEGORY,
    category_expr,
    category_filter,
    doc_category,
)
from youju.classify import doc_kind
from youju.db import get_session
from youju.models import Document, Source

from .test_classify_labeled import LABELED

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
HEADERS = {"X-Admin-Token": "secret"}

APPLY = "关于组织开展2026年度高新技术企业认定工作的通知"
LIST = "关于公布2026年第一批专精特新中小企业名单的通知"
POLICY = "关于印发《高新技术企业认定管理办法》的通知"
GUIDE_STAT = "2026年8月份居民消费价格同比上涨0.8%"
GUIDE_HIRE = "山东省科学技术厅所属事业单位2026年公开招聘拟聘用人员公示"
SUGGESTED = "关于开展2025年度山东省科学技术奖提名工作的通知"
OTHER = "关于重要工业品低价无序竞争成本核算有关事项的通知"


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    monkeypatch.setattr(v1, "now", lambda: NOW)
    yield TestClient(app)
    app.dependency_overrides.clear()


def add(session, number, title, *, day=date(2026, 9, 25), province="山东"):
    source = session.scalar(select(Source).where(Source.province == province))
    if source is None:
        source = Source(
            level="省级",
            province=province,
            dept="科技",
            dept_full_name=f"{province}省科学技术厅",
            column_name="通知公告",
            url=f"https://kjt.{number}.gov.cn/tzgg/",
            enabled=True,
            parser_config={},
        )
        session.add(source)
        session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url=f"https://kjt.example.gov.cn/tzgg/{number}.html",
        url_key=f"kjt.example.gov.cn/tzgg/{number}.html",
        title=title,
        publish_date=day,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.commit()
    return doc


@pytest.fixture
def docs(session):
    titles = {
        "apply": APPLY,
        "list": LIST,
        "policy": POLICY,
        "stat": GUIDE_STAT,
        "hire": GUIDE_HIRE,
        "suggested": SUGGESTED,
        "other": OTHER,
    }
    return {key: add(session, i, title).id for i, (key, title) in enumerate(titles.items())}


def search(client, **params):
    response = client.get("/api/v1/search", params={"time": "近 30 天", **params})
    assert response.status_code == 200, response.text
    return response.json()


def ids(body):
    return {int(item["id"]) for item in body["items"]}


def test_rules_put_news_into_guide_and_keep_suggestions_in_other():
    assert doc_kind(GUIDE_STAT) == doc_kind(GUIDE_HIRE) == "资讯与指南"
    assert doc_kind(SUGGESTED) == doc_kind(OTHER) == "其他"
    assert (doc_kind(APPLY), doc_kind(LIST), doc_kind(POLICY)) == ("申报通知", "名单", "政策")


def test_search_folds_guides_by_default_and_says_how_many(client, docs):
    folded = search(client, fold=1)
    guides = {docs["stat"], docs["hire"]}
    assert not guides & ids(folded)
    assert folded["folded"] == folded["guides"] == 2
    assert folded["total"] == 5 and folded["total_all"] == 5
    assert all(item["kind"] != "资讯与指南" for item in folded["items"])
    # 点开查看：fold=0 照常列出，并给出篇数
    shown = search(client, fold=0)
    assert guides <= ids(shown)
    assert (shown["total"], shown["folded"], shown["guides"]) == (7, 0, 2)
    # 不传 fold（旧版小程序）：和以前一样全部列出，不统计
    legacy = search(client)
    assert legacy["total"] == 7 and "folded" not in legacy and "guides" not in legacy


def test_folded_count_follows_the_other_filters(client, docs, session):
    add(session, 20, "2026年1—8月份全国固定资产投资基本情况", province="国家")
    assert search(client, fold=1)["folded"] == 3
    assert search(client, fold=1, regions="山东")["folded"] == 2
    assert search(client, fold=1, q="招聘")["folded"] == 1
    assert search(client, fold=1, q="招聘")["total"] == 0
    # 超出发布时间的不算折叠，也不算在不限时间的份数里
    add(session, 21, "2025年8月份居民消费价格同比上涨0.5%", day=date(2025, 8, 9))
    body = search(client, fold=1)
    assert body["folded"] == 3 and body["total_all"] == 5


def test_only_the_all_tab_without_types_folds(client, docs):
    # 页签或类型已经把资讯与指南排除了：没有可折叠的
    assert search(client, fold=1, kind="申报通知")["folded"] == 0
    assert search(client, fold=1, types="申报通知,其他")["folded"] == 0
    # 明确选了"资讯与指南"：照常列出，不折叠
    picked = search(client, fold=1, types="资讯与指南")
    assert ids(picked) == {docs["stat"], docs["hire"]}
    assert picked["folded"] == 0
    # "其他"不再包含资讯与指南
    assert ids(search(client, types="其他")) == {docs["suggested"], docs["other"]}


def test_unconfirmed_suggestion_does_not_change_public_kind(client, docs):
    suggested = docs["suggested"]
    pending = client.get("/api/admin/kind-suggestions", headers=HEADERS).json()
    assert [(i["id"], i["shown_kind"], i["suggested"], i["reason"]) for i in pending] == [
        (suggested, "其他", "申报通知", "提名工作")
    ]
    assert client.get("/api/admin/kind-suggestions/count", headers=HEADERS).json() == {"pending": 1}
    # 没确认：对外仍是"其他"，不在申报通知里，也不折叠
    assert suggested not in ids(search(client, kind="申报通知"))
    item = next(i for i in search(client, fold=1)["items"] if int(i["id"]) == suggested)
    assert item["kind"] == "其他" and item["category"] is None
    assert client.get(f"/api/v1/documents/{suggested}").json()["kind"] == "其他"


def test_batch_confirm_changes_kind_and_can_be_undone(client, docs, session):
    suggested = docs["suggested"]
    body = {"ids": [suggested], "kind": "申报通知", "decided_by": "创始人"}
    res = client.post("/api/admin/kinds/batch", headers=HEADERS, json=body)
    assert res.status_code == 200 and res.json() == {"updated": 1, "kind": "申报通知"}
    assert suggested in ids(search(client, kind="申报通知"))
    detail = client.get(f"/api/v1/documents/{suggested}").json()
    assert detail["kind"] == "申报通知" and detail["category"] == "表彰奖励"
    assert client.get("/api/admin/kind-suggestions", headers=HEADERS).json() == []
    decided = client.get(
        "/api/admin/kind-suggestions", headers=HEADERS, params={"status": "decided"}
    ).json()
    assert [(i["id"], i["decided"], i["decided_by"]) for i in decided] == [
        (suggested, "申报通知", "创始人")
    ]
    # 撤回：回到按标题规则（其他），重新出现在待确认里
    client.post(f"/api/admin/documents/{suggested}/kind/undo", headers=HEADERS)
    assert suggested not in ids(search(client, kind="申报通知"))
    assert client.get("/api/admin/kind-suggestions/count", headers=HEADERS).json() == {"pending": 1}
    # "保持其他"也是人定过的：不再出现在待确认里
    keep = {"ids": [suggested], "kind": "其他", "decided_by": "创始人"}
    assert client.post("/api/admin/kinds/batch", headers=HEADERS, json=keep).status_code == 200
    assert client.get("/api/admin/kind-suggestions", headers=HEADERS).json() == []


LIST_LIKE = "关于2025年江苏省产学研合作项目立项的通知"


def test_list_suggestion_listed_and_filtered_by_kind(client, docs, session):
    listed = add(session, 30, LIST_LIKE).id
    pending = client.get("/api/admin/kind-suggestions", headers=HEADERS).json()
    # 同一天发布的按编号从新到旧
    assert [(i["id"], i["suggested"], i["reason"]) for i in pending] == [
        (listed, "名单", "立项"),
        (docs["suggested"], "申报通知", "提名工作"),
    ]
    count = "/api/admin/kind-suggestions/count"
    assert client.get(count, headers=HEADERS).json() == {"pending": 2}
    assert client.get(count, headers=HEADERS, params={"suggested": "名单"}).json() == {"pending": 1}
    only = client.get(
        "/api/admin/kind-suggestions", headers=HEADERS, params={"suggested": "名单"}
    ).json()
    assert [(i["id"], i["shown_kind"], i["suggested"]) for i in only] == [(listed, "其他", "名单")]
    apply_only = client.get(
        "/api/admin/kind-suggestions", headers=HEADERS, params={"suggested": "申报通知"}
    ).json()
    assert [i["id"] for i in apply_only] == [docs["suggested"]]
    bad = client.get("/api/admin/kind-suggestions", headers=HEADERS, params={"suggested": "政策"})
    assert bad.status_code == 422


def test_unconfirmed_list_suggestion_does_not_change_public_kind(client, docs, session):
    listed = add(session, 30, LIST_LIKE).id
    # 没确认：对外仍是"其他"——不在名单页签、最近名单、名单看板里，详情也是"其他"，也不折叠
    assert listed not in ids(search(client, kind="名单"))
    assert listed in ids(search(client, types="其他"))
    item = next(i for i in search(client, fold=1)["items"] if int(i["id"]) == listed)
    assert (item["kind"], item["status"], item["category"]) == ("其他", None, None)
    recent = client.get("/api/v1/lists/recent").json()
    assert listed not in {int(i["id"]) for i in recent["items"]}
    board = client.get("/api/v1/reports/lists").json()
    assert listed not in {int(i["id"]) for i in board["recent"]["items"]}
    detail = client.get(f"/api/v1/documents/{listed}").json()
    assert detail["kind"] == "其他" and detail["body"]["status"] == "original"


def test_confirm_list_suggestion_then_undo(client, docs, session):
    listed = add(session, 30, LIST_LIKE).id
    body = {"ids": [listed], "kind": "名单", "decided_by": "创始人"}
    assert client.post("/api/admin/kinds/batch", headers=HEADERS, json=body).status_code == 200
    assert listed in ids(search(client, kind="名单"))
    detail = client.get(f"/api/v1/documents/{listed}").json()
    assert detail["kind"] == "名单" and detail["category"] == "科技计划与研发"
    assert client.get(
        "/api/admin/kind-suggestions/count", headers=HEADERS, params={"suggested": "名单"}
    ).json() == {"pending": 0}
    decided = client.get(
        "/api/admin/kind-suggestions",
        headers=HEADERS,
        params={"status": "decided", "suggested": "名单"},
    ).json()
    assert [(i["id"], i["decided"], i["suggested"]) for i in decided] == [(listed, "名单", "名单")]
    client.post(f"/api/admin/documents/{listed}/kind/undo", headers=HEADERS)
    assert listed not in ids(search(client, kind="名单"))
    assert client.get(
        "/api/admin/kind-suggestions/count", headers=HEADERS, params={"suggested": "名单"}
    ).json() == {"pending": 1}


def test_batch_validation(client, docs, session):
    url = "/api/admin/kinds/batch"
    ok = {"ids": [docs["suggested"]], "kind": "申报通知", "decided_by": "创始人"}
    assert client.post(url, json=ok).status_code == 401
    assert client.post(url, headers=HEADERS, json={**ok, "decided_by": ""}).status_code == 422
    assert client.post(url, headers=HEADERS, json={**ok, "ids": []}).status_code == 422
    assert client.post(url, headers=HEADERS, json={**ok, "kind": "新闻"}).status_code == 422
    # 有一份不存在就整批不改
    missing = client.post(url, headers=HEADERS, json={**ok, "ids": [docs["suggested"], 999999]})
    assert missing.status_code == 404
    session.expire_all()
    assert session.get(Document, docs["suggested"]).kind_override is None
    # 人也可以把文件定为资讯与指南
    guide = {**ok, "ids": [docs["other"]], "kind": "资讯与指南"}
    assert client.post(url, headers=HEADERS, json=guide).status_code == 200
    assert docs["other"] in ids(search(client, types="资讯与指南"))


def test_categories_on_items_detail_and_filter(client, docs):
    items = {int(i["id"]): i for i in search(client, fold=0)["items"]}
    assert items[docs["apply"]]["category"] == "资质与梯度培育"
    assert items[docs["list"]]["category"] == "资质与梯度培育"
    # 政策、资讯与指南、其他不归类
    for key in ("policy", "stat", "other"):
        assert items[docs[key]]["category"] is None
    assert client.get(f"/api/v1/documents/{docs['policy']}").json()["category"] is None
    picked = search(client, categories="资质与梯度培育")
    # 政策标题里也有"高新技术企业"，但类别只在申报通知和名单里找
    assert ids(picked) == {docs["apply"], docs["list"]}
    assert search(client, categories="表彰奖励")["total"] == 0
    both = search(client, categories="资质与梯度培育,人才", kind="名单")
    assert ids(both) == {docs["list"]}
    assert client.get("/api/v1/search", params={"categories": "高企"}).status_code == 422


def test_category_filter_works_before_reclassify(client, docs, session):
    expected = ids(search(client, categories="资质与梯度培育"))
    session.execute(update(Document).values(rule_category=None))
    session.commit()
    assert ids(search(client, categories="资质与梯度培育")) == expected
    items = {int(i["id"]): i for i in search(client)["items"]}
    assert items[docs["apply"]]["category"] == "资质与梯度培育"


def test_follow_conditions_accept_categories(client, docs, session):
    from youju.api.me import _conditions_stmt

    stmt = _conditions_stmt(session, {"time": "不限", "categories": "资质与梯度培育"})
    assert {doc.id for doc, _ in session.execute(stmt)} == {docs["apply"], docs["list"]}


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("关于开展2026年度高新技术企业认定工作的通知", "资质与梯度培育"),
        ("关于组织推荐第八批制造业单项冠军企业的通知", "资质与梯度培育"),
        ("关于组织申报2026年首台（套）重大技术装备推广应用项目的通知", "首台套与新产品"),
        ("关于2026年度山东省科学技术奖提名工作的通知", "表彰奖励"),
        ("关于组织申报2026年度省重点实验室的通知", "平台载体与园区"),
        ("关于组织申报新型研发机构的通知", "平台载体与园区"),
        ("关于组织申报国家重点研发计划重点专项2026年度项目的通知", "科技计划与研发"),
        ("关于发布辽宁省双向“揭榜挂帅”技术需求清单的通知", "科技计划与研发"),
        ("关于开展2026年度省级技术经理人认定工作的通知", "人才"),
        ("关于征集2026年人工智能典型应用场景的通知", "试点示范"),
        ("关于组织开展2026年三季度贷款贴息项目申报工作的通知", OTHER_CATEGORY),
    ],
)
def test_doc_category(title, category):
    assert doc_category(title) == category


def test_category_sql_matches_python(session):
    """数据库里的项目类别（还没重算时的筛选用）与 Python 规则完全一致。"""
    titles = [x["title"] for x in LABELED] + [APPLY, LIST, POLICY, SUGGESTED]
    for i, title in enumerate(titles):
        add(session, 100 + i, title)
    rows = session.execute(select(Document.title, category_expr(Document.title))).all()
    assert len(rows) == len(titles)
    assert [t for t, sql in rows if sql != doc_category(t)] == []
    # 每个类别筛出来的也一致
    for name in CATEGORIES:
        matched = set(
            session.scalars(select(Document.title).where(category_filter(Document.title, [name])))
        )
        assert matched == {t for t in titles if doc_category(t) == name}, name


def test_reclassify_fills_rule_category(session, session_factory, monkeypatch, capsys):
    from youju import cli

    doc = add(session, 1, APPLY)
    session.execute(update(Document).values(rule_category=None, rule_kind="其他"))
    session.commit()
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    assert cli.main(["reclassify"]) == 0
    assert "共 1 份，更新 1 份" in capsys.readouterr().out
    session.expire_all()
    stored = session.get(Document, doc.id)
    assert (stored.rule_kind, stored.rule_category) == ("申报通知", "资质与梯度培育")
