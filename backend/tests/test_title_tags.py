"""支持方式、产业方向（按标题归类，只用于筛选；D-051 第三阶段）。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from youju import fields
from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentDetail, DocumentField, Source
from youju.title_tags import (
    INDUSTRIES,
    INDUSTRY_RULES,
    SUPPORT_KINDS,
    SUPPORT_MODES,
    SUPPORT_RULES,
)

from .test_classify_labeled import LABELED

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
HEADERS = {"X-Admin-Token": "secret"}
# 小程序界面上不能出现的字样（产业方向的标签名不用这些词）
FORBIDDEN = ("人工智能", "智能", "AI", "大模型")

# 标题取自 youju_perf 的真实文件
FUND_APPLY = "关于组织申报2027年度自治区科技金融补助的通知"
LOAN_APPLY = "辽宁省工业和信息化厅关于组织开展2026年三季度辽宁省制造业固定资产贷款贴息及担保补贴项目申报工作的通知"
QUALIFY_LIST = "关于公布2026年黑龙江省第二批专精特新中小企业认定及复核通过名单的通知"
PICKED_APPLY = (
    "关于开展2026年辽宁省“人工智能+制造”典型应用案例、高质量行业数据集和行业模型遴选工作的通知"
)
POLICY = "关于印发《江苏省物联网产业发展三年行动计划》的通知"
GUIDE = "陕西省科技成果转化“三项改革”系列路演丨（33）生物医药专场"
PLAIN = "关于组织开展2026年江苏省科技副总选派工作的通知"
RESEARCH = "关于开展2026年度安徽省自然科学基金项目申报工作的通知"
FILING = "天津市2026年第五批拟入库科技型中小企业名单公示"


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


def add(session, number, title, *, day=date(2026, 9, 25)):
    source = session.scalar(select(Source).where(Source.province == "辽宁"))
    if source is None:
        source = Source(
            level="省级",
            province="辽宁",
            dept="科技",
            dept_full_name="辽宁省科学技术厅",
            column_name="通知公告",
            url="https://kjt.ln.gov.cn/tzgg/",
            enabled=True,
            parser_config={},
        )
        session.add(source)
        session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url=f"https://kjt.ln.gov.cn/tzgg/{number}.html",
        url_key=f"kjt.ln.gov.cn/tzgg/{number}.html",
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
        "fund": FUND_APPLY,
        "loan": LOAN_APPLY,
        "qualify": QUALIFY_LIST,
        "picked": PICKED_APPLY,
        "policy": POLICY,
        "guide": GUIDE,
        "plain": PLAIN,
        "research": RESEARCH,
        "filing": FILING,
    }
    return {key: add(session, i, title).id for i, (key, title) in enumerate(titles.items())}


def search(client, **params):
    response = client.get("/api/v1/search", params={"time": "近 30 天", "fold": 0, **params})
    assert response.status_code == 200, response.text
    return response.json()


def ids(body):
    return {int(item["id"]) for item in body["items"]}


@pytest.mark.parametrize(
    ("title", "supports", "industries"),
    [
        (FUND_APPLY, ("资金支持",), ()),
        (LOAN_APPLY, ("资金支持", "贷款贴息"), ("先进制造",)),
        (QUALIFY_LIST, ("资格认定",), ()),
        (PICKED_APPLY, ("征集入选",), ("数字经济", "先进制造")),
        (POLICY, (), ("数字经济",)),
        (GUIDE, (), ("生物医药与健康",)),
        (PLAIN, (), ()),
        (RESEARCH, ("项目立项",), ()),
        (FILING, ("入库备案",), ()),
        ("关于2026年度重点产业技术协同攻关专项（重点产业类）拟立项项目的公示", ("项目立项",), ()),
        (
            "成渝地区“一带一路”国际技术转移中心关于发布海外节点建设任务“揭榜挂帅”榜单的通知",
            ("项目立项",),
            (),
        ),
        # 已有项目的结题验收不算立项；"科技型企业孵化器"不算入库备案
        ("2026年第三批省科技攻关计划项目结项验收结果公示", (), ()),
        (
            "广东省科学技术厅关于2026年工业和信息化部科技型企业孵化器推荐申报名单的公示",
            ("资格认定", "征集入选"),
            (),
        ),
        (
            "关于2026年度省级绿色工厂、绿色工业园区名单的通知",
            ("资格认定",),
            ("先进制造", "新能源与绿色低碳"),
        ),
        ("关于拟认定化工园区名单的公示", ("资格认定",), ("新材料与化工",)),
        ("关于征集2026年天津市公共卫生项目的通知", ("征集入选",), ("生物医药与健康",)),
        (
            "关于开展2026年农机装备产业建圈强链培育引导资金申报工作的通知",
            ("资金支持",),
            ("先进制造", "现代农业"),
        ),
        (
            "关于组织开展2026年度视听系统典型案例征集工作的通知",
            ("征集入选",),
            ("电子信息与集成电路",),
        ),
        ("关于征集2026年度科技服务业企业效能提升项目的通知", ("征集入选",), ("文旅与现代服务业",)),
        # 撤销、取消资格的不算支持方式；职称是个人事项
        ("关于贵州省取消3户企业高新技术企业资格的公告", (), ()),
        (
            "关于开展2026年度湖北省电子信息工程专业职称评审申报工作的通知",
            (),
            ("电子信息与集成电路",),
        ),
        # 发文机关名称里的"工业和信息化""能源局"不算产业方向；"绿色通道"不算绿色低碳
        ("河南省工业和信息化厅关于开通企业服务绿色通道的通知", (), ()),
    ],
)
def test_tags_by_title(title, supports, industries):
    assert SUPPORT_RULES.tags(title) == supports
    assert INDUSTRY_RULES.tags(title) == industries
    assert SUPPORT_RULES.stored(title) == ",".join(supports)


def test_support_labels_use_reviewed_field_words():
    """筛选的支持方式和详情页核对字段用同一套词，只多一个"征集入选"。"""
    assert SUPPORT_MODES == ("资金支持", "贷款贴息", "资格认定", "入库备案", "项目立项", "征集入选")
    assert set(SUPPORT_MODES) - set(fields.SUPPORT_LABELS) == {"征集入选"}


def test_labels_have_no_forbidden_words():
    for label in SUPPORT_MODES + INDUSTRIES:
        assert not any(word in label for word in FORBIDDEN), label
    assert len(INDUSTRIES) == 8 and len(SUPPORT_MODES) == 6
    assert SUPPORT_KINDS == ("申报通知", "名单")


def test_tag_sql_matches_python(session):
    """数据库里按标题判断（还没重算时的筛选用）与 Python 规则完全一致。"""
    titles = [x["title"] for x in LABELED] + [FUND_APPLY, LOAN_APPLY, QUALIFY_LIST, PICKED_APPLY]
    titles += [POLICY, GUIDE, PLAIN, RESEARCH, FILING]
    titles += [
        "河南省工业和信息化厅关于开通企业服务绿色通道的通知",
        "2026年第三批省科技攻关计划项目结项验收结果公示",
    ]
    for i, title in enumerate(titles):
        add(session, 100 + i, title)
    for rules in (SUPPORT_RULES, INDUSTRY_RULES):
        for label in rules.labels:
            matched = set(
                session.scalars(
                    select(Document.title).where(rules.sql_filter(Document.title, [label]))
                )
            )
            assert matched == {t for t in titles if label in rules.tags(t)}, label
        both = list(rules.labels[:2])
        matched = set(
            session.scalars(select(Document.title).where(rules.sql_filter(Document.title, both)))
        )
        assert matched == {t for t in titles if set(both) & set(rules.tags(t))}


def test_columns_filled_on_insert(docs, session):
    stored = session.get(Document, docs["loan"])
    assert stored.rule_supports == "资金支持,贷款贴息"
    assert stored.rule_industries == "先进制造"
    # 没有标签存空串，和还没算（NULL）区分开
    plain = session.get(Document, docs["plain"])
    assert (plain.rule_supports, plain.rule_industries) == ("", "")


def test_support_filter_multi_select_only_apply_and_lists(client, docs):
    assert ids(search(client, supports="资金支持")) == {docs["fund"], docs["loan"]}
    # 同一组里多选取并集
    union = search(client, supports="贷款贴息,资格认定")
    assert ids(union) == {docs["loan"], docs["qualify"]}
    assert ids(search(client, supports="征集入选")) == {docs["picked"]}
    # 政策、资讯与指南没有支持方式：标题里有"资金"的政策也不列
    assert search(client, supports="资金支持", kind="政策")["total"] == 0
    # 和其他筛选取交集
    assert ids(search(client, supports="资金支持,资格认定", kind="名单")) == {docs["qualify"]}
    # 科研项目申报、立项和入库备案也有标签
    assert ids(search(client, supports="项目立项")) == {docs["research"]}
    assert ids(search(client, supports="入库备案,项目立项")) == {docs["research"], docs["filing"]}
    bad = client.get("/api/v1/search", params={"supports": "资金奖补"})
    assert bad.status_code == 422


def test_industry_filter_multi_select_all_kinds(client, docs):
    assert ids(search(client, industries="数字经济")) == {docs["picked"], docs["policy"]}
    union = search(client, industries="先进制造,生物医药与健康")
    assert ids(union) == {docs["loan"], docs["picked"], docs["guide"]}
    # 资讯与指南默认折叠：折叠后不列，只给篇数
    folded = search(client, industries="生物医药与健康", fold=1)
    assert folded["total"] == 0 and folded["folded"] == 1
    # 两组同时选：交集
    assert ids(search(client, industries="先进制造", supports="征集入选")) == {docs["picked"]}
    assert client.get("/api/v1/search", params={"industries": "人工智能"}).status_code == 422


def test_filters_work_before_reclassify(client, docs, session):
    cases = [{"supports": "资金支持,征集入选"}, {"industries": "数字经济,先进制造"}]
    expected = [ids(search(client, **params)) for params in cases]
    session.execute(update(Document).values(rule_supports=None, rule_industries=None))
    session.commit()
    assert [ids(search(client, **params)) for params in cases] == expected


def test_tags_are_not_returned_or_shown(client, docs):
    """按标题算的标签只用于筛选：列表和详情都不返回。"""
    for item in search(client, supports="资金支持")["items"]:
        assert not {"supports", "industries", "rule_supports", "rule_industries"} & set(item)
        assert "support" not in item and "fields" not in item
    detail = client.get(f"/api/v1/documents/{docs['loan']}").json()
    assert not {"supports", "industries", "rule_supports", "rule_industries"} & set(detail)
    # 没有人工核对通过的支持方式：详情不给 fields（不会把"资金支持""贷款贴息"当成事实展示）
    assert "fields" not in detail


def test_shown_support_comes_only_from_reviewed_fields(client, docs, session):
    body = "支持方式：对符合条件的企业给予贷款贴息。" * 3
    doc_id = docs["loan"]
    session.add(
        DocumentDetail(
            document_id=doc_id, body_text=body, fetched_at=datetime.now(UTC), status="ok"
        )
    )

    def field(value, status):
        session.add(
            DocumentField(
                document_id=doc_id,
                field="support",
                value=value,
                evidence="对符合条件的企业给予贷款贴息。",
                evidence_offset=5,
                body_hash=fields.body_fingerprint(body),
                status=status,
                source="rule",
                approved_value=value if status == "approved" else None,
                reviewed_by="创始人" if status == "approved" else None,
                reviewed_at=datetime.now(UTC) if status == "approved" else None,
            )
        )
        session.commit()

    # 待核对的不展示
    field("资金支持", "pending")
    assert "fields" not in client.get(f"/api/v1/documents/{doc_id}").json()
    # 核对通过的才展示：标题算出的是"资金支持""贷款贴息"，展示的只有核对通过的"贷款贴息"
    field("贷款贴息", "approved")
    shown = client.get(f"/api/v1/documents/{doc_id}").json()["fields"]
    assert shown["support"] == ["贷款贴息"]
    assert set(shown["support"]) <= set(fields.SUPPORT_LABELS)


def test_follow_conditions_accept_supports_and_industries(client, docs, session):
    from youju.api.me import _conditions_stmt

    stmt = _conditions_stmt(session, {"time": "不限", "supports": "资格认定"})
    assert {doc.id for doc, _ in session.execute(stmt)} == {docs["qualify"]}
    stmt = _conditions_stmt(session, {"time": "不限", "industries": "数字经济"})
    assert {doc.id for doc, _ in session.execute(stmt)} == {docs["picked"], docs["policy"]}


def test_reclassify_fills_tag_columns(session, session_factory, monkeypatch, capsys):
    from youju import cli

    doc = add(session, 1, LOAN_APPLY)
    session.execute(update(Document).values(rule_supports=None, rule_industries=None))
    session.commit()
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    assert cli.main(["reclassify"]) == 0
    assert "共 1 份，更新 1 份" in capsys.readouterr().out
    session.expire_all()
    stored = session.get(Document, doc.id)
    assert (stored.rule_supports, stored.rule_industries) == ("资金支持,贷款贴息", "先进制造")
    # 再跑一次没有变化
    assert cli.main(["reclassify"]) == 0
    assert "更新 0 份" in capsys.readouterr().out
