"""找通知（youju/chat_find.py）：规则取条件、交给现有搜索、存的和读的（问一问开发设计第六节第 1 条、
第五节 /chat/examples、第十五节 PR-2 验收里找通知的几条）。"""

import itertools
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from youju import chat_find
from youju.api import v1
from youju.api.app import app
from youju.chat_find import (
    NOTES,
    PROVINCE_ALIASES,
    TYPE_WORDS,
    Coverage,
    conditions_text,
    examples,
    looks_like_title,
    normalize,
    parse,
    render,
    run,
    search_params,
)
from youju.db import get_session
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    RepostGroup,
    RepostGroupMember,
    Source,
)

# 北京时间 2026-06-15（周一）上午 10 点。不用真实时钟：写死的日期碰上真实时钟会在某一天变红
NOW = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TODAY = NOW.date()
STAMP = datetime(2026, 6, 1, tzinfo=UTC)
_serial = itertools.count(1)

# 不查库的单测用：31 个省级地区和国家部委都收录了
ALL_COVERED = Coverage(
    provinces=frozenset([*v1._PROVINCE_ORDER, "国家部委"]),
    cities=frozenset({("广东", "深圳"), ("吉林", "吉林"), ("江苏", "南京"), ("福建", "福州")}),
    districts=frozenset(
        {
            ("广东", "深圳", "南山区"),
            ("天津", "天津", "河北区"),
            ("江苏", "南京", "鼓楼区"),
            ("福建", "福州", "鼓楼区"),
            ("江苏", "苏州", "昆山市"),
        }
    ),
)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setattr(v1, "now", lambda: NOW)
    chat_find._EXAMPLES.clear()
    yield
    chat_find._EXAMPLES.clear()


def _parse(text, covered=ALL_COVERED):
    return parse(text, today=TODAY, covered=covered).conditions


def _source(session, province, level="省级", city=None, district=None, *, covered=True):
    n = next(_serial)
    src = Source(
        level=level,
        province=province,
        city=city,
        district=district,
        dept="工信",
        dept_full_name=f"合成{district or city or province}部门",
        column_name="通知",
        url=f"https://s{n}.example.gov.cn/list/",
        enabled=True,
        parser_config={},
        rights_level=1,
        last_success_at=STAMP if covered else None,
    )
    session.add(src)
    session.flush()
    return src


def _doc(session, src, title, day, number=None):
    n = next(_serial)
    doc = Document(
        source_id=src.id,
        url=f"https://example.gov.cn/doc/{n}.html",
        url_key=f"example.gov.cn/doc/{n}.html",
        title=title,
        publish_date=day,
        first_seen_at=STAMP,
        last_seen_at=STAMP,
    )
    session.add(doc)
    session.flush()
    if number:
        session.add(
            DocumentDetail(
                document_id=doc.id, body_text="", doc_number=number, fetched_at=STAMP, status="ok"
            )
        )
    return doc


@pytest.fixture
def world(session):
    """几个省、一个市、一个区县的合成通知；西藏的栏目启用了但还没成功检查过（没收录）。"""
    gj = _source(session, "国家", "国家级")
    gd = _source(session, "广东")
    sz = _source(session, "广东", "市级", "深圳")
    ns = _source(session, "广东", "区县级", "深圳", "南山区")
    sc = _source(session, "四川")
    js = _source(session, "江苏")
    jl = _source(session, "吉林")
    jlc = _source(session, "吉林", "市级", "吉林")
    xz = _source(session, "西藏", covered=False)
    docs = {
        # 入库的文号用全角数字
        "gd_apply": _doc(
            session,
            gd,
            "广东省工业和信息化厅关于组织开展2026年专精特新中小企业申报工作的通知",
            date(2026, 6, 10),
            "粤工信融资函〔２０２６〕５６号",
        ),
        "gd_policy": _doc(
            session,
            gd,
            "关于印发《广东省专精特新中小企业培育管理办法（试行）》的通知",
            date(2026, 3, 2),
        ),
        "gd_list": _doc(
            session,
            gd,
            "关于公布2026年第１批专精特新“小巨人”企业名单（第二批）的公告",
            date(2026, 5, 20),
        ),
        "gd_list_old": _doc(
            session, gd, "广东省2025年度专精特新中小企业拟认定名单公示", date(2025, 12, 20)
        ),
        "gd_cash": _doc(session, gd, "关于2025年政策兑现申报的通知", date(2025, 11, 1)),
        "gd_guide": _doc(session, gd, "关于举办专精特新企业培训会的通知", date(2026, 6, 11)),
        "sz_apply": _doc(
            session, sz, "深圳市科技创新局关于组织申报2026年科技计划项目的通知", date(2026, 6, 1)
        ),
        "ns_apply": _doc(
            session, ns, "南山区关于申报2026年度专精特新企业扶持资金的通知", date(2026, 5, 1)
        ),
        "sc_apply": _doc(
            session,
            sc,
            "四川省经济和信息化厅关于组织申报2026年省级工业发展资金项目的通知",
            date(2026, 6, 12),
            "川经信办函〔2026〕356号",
        ),
        "sc_near": _doc(
            session,
            sc,
            "四川省经济和信息化厅关于做好工业发展资金项目管理的通知",
            date(2026, 6, 2),
            "川经信办函〔2026〕357号",
        ),
        "gj_train": _doc(
            session,
            gj,
            "工业和信息化部办公厅关于开展2026年度专精特新“小巨人”企业培育工作的通知",
            date(2026, 4, 10),
        ),
        "js_next": _doc(
            session,
            js,
            "江苏省工业和信息化厅关于组织申报2027年度专精特新中小企业的通知",
            date(2026, 6, 5),
        ),
        "jl_city": _doc(session, jlc, "吉林市关于组织申报2026年科技项目的通知", date(2026, 6, 3)),
        "jl_prov": _doc(
            session, jl, "吉林省科技厅关于组织申报2026年科技项目的通知", date(2026, 6, 4)
        ),
        "xz_apply": _doc(
            session, xz, "西藏自治区科技厅关于组织申报2026年科技项目的通知", date(2026, 6, 1)
        ),
    }
    session.commit()
    return {name: doc.id for name, doc in docs.items()}


@pytest.fixture
def client(session_factory):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ids(world, *names):
    return [world[name] for name in names]


def _find(session, text):
    return run(session, text, today=TODAY)


def _same_as_search_page(client, stored):
    """ "在搜索页看全部"带的参数，按搜索页的请求（group=1、excerpts=1、默认 fold=1）查出来一模一样。"""
    params = search_params(stored["conditions"])
    res = client.get(
        "/api/v1/search", params={**params, "group": 1, "excerpts": 1, "fold": 1, "limit": 10}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == stored["total"]
    assert [int(item["id"]) for item in body["items"]] == stored["ids"]


# ---- 第 6 条 时间 ----


@pytest.mark.parametrize(
    "phrase,value",
    [
        *[
            (p, "近 7 天")
            for p in ("近 7 天", "近七天", "最近 7 天", "最近七天", "这周", "本周", "这几天")
        ],
        *[(p, "近 7 天") for p in ("近一周", "近1周", "最近一周", "最近这几天", "近７天")],
        *[
            (p, "近 30 天")
            for p in ("近 30 天", "近30天", "近三十天", "最近 30 天", "最近三十天", "近一个月")
        ],
        *[
            (p, "近 30 天")
            for p in ("最近一个月", "近1个月", "最近", "最新", "近期", "本月", "这个月")
        ],
        *[
            (p, "近 90 天")
            for p in ("近 90 天", "近九十天", "最近 90 天", "近三个月", "最近三个月")
        ],
        *[(p, "近 90 天") for p in ("近3个月", "这个季度")],
        *[(p, "今年") for p in ("今年", "本年", "2026年", "2026", "２０２６年")],
        ("今年最新", "近 30 天"),  # 几个时间词都有时，按近 7 天、近 30 天、近 90 天、今年的顺序取
        ("这周今年", "近 7 天"),
    ],
)
def test_时间词(phrase, value):
    conditions = _parse(f"广东{phrase}的专精特新申报通知")
    assert conditions["time"] == value
    assert conditions["year"] is None
    assert conditions["keywords"] == ["专精特新"]


@pytest.mark.parametrize(
    "phrase,year",
    [("去年", 2025), ("前年", 2024), ("2025年", 2025), ("2025", 2025), ("2010年", 2010)],
)
def test_往年按发布日期筛那一年(phrase, year):
    conditions = _parse(f"广东{phrase}的专精特新名单")
    assert (conditions["time"], conditions["year"]) == ("不限", year)
    assert conditions["keywords"] == ["专精特新"]


def test_年度不当时间而当标题词():
    assert _parse("2026年度专精特新申报通知") == _parse("本年度专精特新申报通知")
    conditions = _parse("2025年度专精特新申报通知")
    assert (conditions["time"], conditions["year"]) == ("不限", None)
    assert conditions["keywords"] == ["2025年度", "专精特新"]
    assert _parse("本年度专精特新")["keywords"] == ["2026年度", "专精特新"]


def test_晚于今年和早于2000年的年份不当时间():
    for text, keyword in (("2027年专精特新", "2027年"), ("1999年专精特新", "1999年")):
        conditions = _parse(text)
        assert (conditions["time"], conditions["year"]) == ("不限", None)
        assert conditions["keywords"] == [keyword, "专精特新"]


# ---- 第 5 条 类型、第 7 条 关键词 ----


@pytest.mark.parametrize(
    "text,kind",
    [
        ("专精特新名单", "名单"),
        ("专精特新公示", "名单"),
        ("专精特新名录", "名单"),
        ("拟认定专精特新", "名单"),
        ("入选专精特新", "名单"),
        ("专精特新申报", "申报通知"),
        ("专精特新申请", "申报通知"),
        ("政策兑现", "申报通知"),  # 不能先认成政策
        ("政策兑现申报", "申报通知"),
        ("专精特新征集", "申报通知"),
        ("专精特新遴选", "申报通知"),
        ("推荐专精特新", "申报通知"),
        ("认定专精特新", "申报通知"),
        ("专精特新政策", "政策"),
        ("专精特新办法", "政策"),
        ("专精特新意见", "政策"),
        ("专精特新措施", "政策"),
        ("专精特新细则", "政策"),
        ("专精特新规定", "政策"),
        ("专精特新通知", "申报通知"),
        ("专精特新指南", "申报通知"),
        ("专精特新项目", "申报通知"),
        ("专精特新资金", "申报通知"),
        ("专精特新", "all"),
    ],
)
def test_类型按顺序先命中的算(text, kind):
    assert _parse(text)["kind"] == kind
    assert kind in v1.TABS


def test_类型词全部去掉():
    assert _parse("广东专精特新申报通知")["keywords"] == ["专精特新"]
    for word in TYPE_WORDS:
        assert _parse(f"专精特新{word}")["keywords"] == ["专精特新"], word


def test_停用词和标点去掉后按它们切开():
    conditions = _parse("帮我找一下关于专精特新的相关通知")
    assert conditions["keywords"] == ["专精特新"]
    assert _parse("请问有没有高新技术企业认定的材料要求")["keywords"] == ["高新技术"]
    assert _parse("人工智能，低空经济")["keywords"] == ["人工智能", "低空经济"]
    # 一个字的不要
    assert _parse("找查搜")["keywords"] == []


def test_问内容的词也去掉_照样能找到():
    for text in ("专精特新能不能申请", "专精特新补贴多少", "专精特新多少钱"):
        parsed = parse(text, today=TODAY, covered=ALL_COVERED)
        assert parsed.conditions["keywords"] == ["专精特新"], text
        assert parsed.content


def test_关键词最多3个每个最多20字():
    parsed = parse("人工智能、低空经济、生物医药、集成电路", today=TODAY, covered=ALL_COVERED)
    assert parsed.conditions["keywords"] == ["人工智能", "低空经济", "生物医药"]
    assert parsed.dropped
    long = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    assert _parse(long)["keywords"] == [long[:20]]


# ---- 第 2–4 条 地区 ----


def test_31个省级地区带不带省市自治区和全称都认():
    names = set(PROVINCE_ALIASES.values()) - {"国家部委"}
    assert names == set(v1._PROVINCE_ORDER) and len(names) == 31
    full = {
        "北京市", "天津市", "河北省", "山西省", "内蒙古自治区", "辽宁省", "吉林省", "黑龙江省",
        "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省", "湖北省",
        "湖南省", "广东省", "广西壮族自治区", "海南省", "重庆市", "四川省", "贵州省", "云南省",
        "西藏自治区", "陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
        "广西自治区", "宁夏自治区", "新疆自治区",
    }  # fmt: skip
    for alias, name in PROVINCE_ALIASES.items():
        if name != "国家部委":
            assert _parse(f"找{alias}今年的专精特新")["regions"] == [name], alias
    assert full <= set(PROVINCE_ALIASES)
    for name in v1._PROVINCE_ORDER:
        assert _parse(f"{name}的通知")["regions"] == [name]


@pytest.mark.parametrize("word", ["国家", "国家级", "部委", "部里", "国家部委"])
def test_国家部委的说法(word):
    assert _parse(f"{word}的专精特新政策")["regions"] == ["国家部委"]


def test_几个地区取并集():
    assert _parse("广东和四川的专精特新")["regions"] == ["广东", "四川"]
    assert _parse("国家和广东的专精特新")["regions"] == ["国家部委", "广东"]


def test_地市区县只认收录了的_带不带市都认():
    for text in (
        "深圳的科技项目",
        "深圳市的科技项目",
        "广东深圳的科技项目",
        "广东省深圳市科技项目",
    ):
        conditions = _parse(text)
        # 具体城市替换同省的全省条件（同搜索页）
        assert (conditions["regions"], conditions["cities"]) == ([], ["广东/深圳"]), text
    conditions = _parse("广东深圳南山区的科技项目")
    assert (conditions["regions"], conditions["cities"]) == ([], [])
    assert conditions["districts"] == ["广东/深圳/南山区"]
    # 没收录的城市不当地区，当关键词
    conditions = _parse("东莞的专精特新")
    assert (conditions["regions"], conditions["cities"]) == ([], [])
    assert conditions["keywords"] == ["东莞", "专精特新"]


def test_和省同名的市要写市字():
    assert _parse("吉林市的科技项目")["cities"] == ["吉林/吉林"]
    conditions = _parse("吉林的科技项目")
    assert (conditions["regions"], conditions["cities"]) == (["吉林"], [])


def test_区县名比省名长的先认():
    conditions = _parse("天津河北区的通知")
    assert conditions["districts"] == ["天津/天津/河北区"]
    assert conditions["regions"] == []


def test_同名区县按写到的省市缩小():
    assert _parse("南京鼓楼区的通知")["districts"] == ["江苏/南京/鼓楼区"]
    assert _parse("福建鼓楼区的通知")["districts"] == ["福建/福州/鼓楼区"]
    assert set(_parse("鼓楼区的通知")["districts"]) == {"江苏/南京/鼓楼区", "福建/福州/鼓楼区"}


def test_区县里的县级市带不带市都认_区不能省():
    for text in ("昆山的通知", "昆山市的通知"):
        assert _parse(text)["districts"] == ["江苏/苏州/昆山市"], text
    conditions = _parse("南山的通知")
    assert (conditions["districts"], conditions["keywords"]) == ([], ["南山"])


def test_没收录的省级地区单独记():
    covered = Coverage(frozenset({"广东"}), frozenset(), frozenset())
    conditions = _parse("广东和西藏的专精特新", covered)
    assert (conditions["regions"], conditions["uncovered"]) == (["广东"], ["西藏"])
    conditions = _parse("国家的专精特新", covered)
    assert (conditions["regions"], conditions["uncovered"]) == ([], ["国家部委"])


# ---- 第 1 条 文号（不查库的部分） ----


def test_认文号():
    for text in (
        "川经信办函〔2026〕356号",
        "帮我找一下川经信办函〔2026〕356号的通知",
        "文号是川经信办函[2026]356号",
        "川经信办函（2026）356号",
        "川经信办函【２０２６】３５６号",
        "川经信办函 2026 356 号",
    ):
        parsed = parse(text, today=TODAY, covered=ALL_COVERED)
        assert parsed.number == ("川经信办函", 2026, 356), text
        assert parsed.conditions["docNumber"].startswith("川经信办函"), text
    assert parse("国家税务总局公告2026年第5号", today=TODAY, covered=ALL_COVERED).number == (
        "国家税务总局公告",
        2026,
        5,
    )
    # 没有机关简称的不算文号
    assert parse("找〔2026〕356号", today=TODAY, covered=ALL_COVERED).number is None


# ---- 第 0 条 整个标题（不查库的部分） ----


def test_像整个标题的四条():
    title = "关于印发广东省专精特新中小企业培育管理办法的通知"
    assert looks_like_title(normalize(title))
    # ① 末尾的标点和最后一对括号里的字不算
    assert looks_like_title(normalize("广东省专精特新中小企业培育管理办法（试行）"))
    assert looks_like_title(normalize("关于公布2026年专精特新中小企业名单（第二批）。"))
    assert not looks_like_title(normalize("关于印发广东省专精特新中小企业培育管理的工作"))
    # ② 只留汉字、字母、数字后要超过 15 个字
    assert not looks_like_title(normalize("2027年度专精特新申报通知"))
    # ③ 没有问号和问句的字
    assert not looks_like_title(normalize("关于专精特新什么时候截止"))
    assert not looks_like_title(normalize(title + "？"))
    assert not looks_like_title(normalize("是否印发广东省专精特新中小企业培育管理办法的通知"))
    # ④ 不以"找、查、搜……"开头
    assert not looks_like_title(normalize("找" + title))
    assert not looks_like_title(normalize("帮我" + title))


# ---- 查库：规则 0–7 走一遍 ----


def test_近30天(session, world, client):
    stored = _find(session, "找广东近 30 天的申报通知")
    assert stored["conditions"] == {
        "regions": ["广东"],
        "cities": [],
        "districts": [],
        "uncovered": [],
        "kind": "申报通知",
        "time": "近 30 天",
        "year": None,
        "keywords": [],
        "docNumber": None,
        "title": False,
    }
    # 广东省级和广东的市、区县来源都算；南山区那份 5/1 发的超过 30 天
    assert stored["ids"] == _ids(world, "gd_apply", "sz_apply")
    assert (stored["total"], stored["link"], stored["notes"]) == (2, True, [])
    _same_as_search_page(client, stored)


@pytest.mark.parametrize(
    "text",
    [
        "关于印发广东省专精特新中小企业培育管理办法试行的通知",
        "关于印发《广东省专精特新中小企业培育管理办法（试行）》的通知",
        "关于印发“广东省专精特新中小企业培育管理办法”（试行）的通知。",
    ],
)
def test_整个标题带不带书名号括号都比得中(session, world, text):
    stored = _find(session, text)
    assert stored["ids"] == _ids(world, "gd_policy")
    assert stored["conditions"]["title"] is True
    assert (stored["link"], stored["notes"]) == (False, ["title"])
    assert stored["conditions"] | {"title": False} == chat_find.empty_conditions()


@pytest.mark.parametrize(
    "text",
    [
        "关于公布2026年第1批专精特新小巨人企业名单(第二批)的公告",
        "关于公布２０２６年第１批专精特新“小巨人”企业名单（第二批）的公告",
        "关于公布 2026年第1批 专精特新《小巨人》企业名单 第二批 的公告",
    ],
)
def test_整个标题全角半角都比得中(session, world, text):
    assert _find(session, text)["ids"] == _ids(world, "gd_list")


def test_问句不当标题(session, world):
    stored = _find(session, "关于专精特新什么时候截止")
    assert stored["conditions"]["title"] is False
    assert stored["conditions"]["keywords"] == ["专精特新"]
    # 不限类型：资讯与指南折叠（培训会那份不列）
    assert stored["ids"] == _ids(
        world, "gd_apply", "js_next", "gd_list", "ns_apply", "gj_train", "gd_policy", "gd_list_old"
    )
    assert stored["notes"] == ["content"]


def test_像标题但比不中的按规则1到7找到(session, world, client):
    stored = _find(session, "广东省2026年专精特新申报通知")
    conditions = stored["conditions"]
    assert conditions["title"] is False
    assert (conditions["regions"], conditions["kind"], conditions["time"]) == (
        ["广东"],
        "申报通知",
        "今年",
    )
    assert conditions["keywords"] == ["专精特新"]
    assert stored["ids"] == _ids(world, "gd_apply", "ns_apply")
    _same_as_search_page(client, stored)


def test_像标题但什么都没找到请输入标题里的几个词(session, world):
    stored = _find(session, "关于印发广东省低空经济产业发展行动方案的通知")
    assert (stored["total"], stored["ids"], stored["notes"]) == (0, [], ["title_none"])


def test_标题比中的照搜索页排除下架_合并转发(session, world):
    title = "关于开展2026年度中小企业数字化转型试点申报的通知"
    gj = session.get(Source, session.get(Document, world["gj_train"]).source_id)
    gd = session.get(Source, session.get(Document, world["gd_apply"]).source_id)
    sc = session.get(Source, session.get(Document, world["sc_apply"]).source_id)
    upstream = _doc(session, gj, title, date(2026, 5, 10))
    repost = _doc(session, gd, title, date(2026, 5, 12))
    hidden = _doc(session, sc, title, date(2026, 5, 13))
    hidden.taken_down_at = STAMP
    group = RepostGroup(strength="strong", topic="数字化转型试点")
    session.add(group)
    session.flush()
    for doc in (upstream, repost):
        session.add(
            RepostGroupMember(
                document_id=doc.id, group_id=group.id, confirmed_by="创始人", confirmed_at=STAMP
            )
        )
    session.commit()
    stored = _find(session, title)
    assert (stored["total"], stored["ids"]) == (1, [upstream.id])
    item = render(session, stored, today=TODAY)["items"][0]
    assert item["group"]["items"][0]["id"] == str(repost.id)


def test_比中的都下架了就按规则找(session, world):
    doc = session.get(Document, world["gd_policy"])
    doc.taken_down_at = STAMP
    session.commit()
    stored = _find(session, "关于印发广东省专精特新中小企业培育管理办法试行的通知")
    assert stored["conditions"]["title"] is False
    assert world["gd_policy"] not in stored["ids"]


def test_政策兑现是申报通知(session, world, client):
    stored = _find(session, "广东的政策兑现")
    assert stored["conditions"]["kind"] == "申报通知"
    assert stored["conditions"]["keywords"] == []
    assert stored["ids"] == _ids(world, "gd_apply", "sz_apply", "ns_apply", "gd_cash")
    _same_as_search_page(client, stored)


def test_年度当标题词去找(session, world):
    stored = _find(session, "2027年度专精特新申报通知")
    assert stored["conditions"]["keywords"] == ["2027年度", "专精特新"]
    assert stored["ids"] == _ids(world, "js_next")
    stored = _find(session, "本年度专精特新申报")
    assert stored["conditions"]["keywords"] == ["2026年度", "专精特新"]
    assert stored["ids"] == _ids(world, "ns_apply")


def test_往年按发布日期找_不给搜索页链接(session, world):
    stored = _find(session, "广东去年的专精特新名单")
    assert stored["conditions"]["year"] == 2025
    assert stored["ids"] == _ids(world, "gd_list_old")
    assert (stored["link"], stored["notes"]) == (False, ["year"])
    assert (
        conditions_text(stored)
        == "地区 广东 · 类型 名单与公示 · 发布于 2025 年 · 标题里有 专精特新"
    )
    assert _find(session, "2025年广东的申报通知")["ids"] == _ids(world, "gd_cash")


def test_文号只按文号找(session, world):
    for text in (
        "帮我找一下川经信办函〔2026〕356号的通知",
        "川经信办函[2026]356号",
        "川经信办函(2026)356号",
        "川经信办函２０２６３５６号",
        "今年的川经信办函〔2026〕356号",  # 只按文号：时间不看
    ):
        stored = _find(session, text)
        assert stored["ids"] == _ids(world, "sc_apply"), text
        assert (stored["link"], stored["notes"]) == (False, []), text
    stored = _find(session, "帮我找一下川经信办函〔2026〕356号的通知")
    assert stored["conditions"]["docNumber"] == "川经信办函〔2026〕356号"
    assert conditions_text(stored) == "文号 川经信办函〔2026〕356号"
    # 入库的是全角数字；序号按数比
    assert _find(session, "粤工信融资函〔2026〕56号")["ids"] == _ids(world, "gd_apply")
    assert _find(session, "粤工信融资函〔2026〕056号")["ids"] == _ids(world, "gd_apply")


@pytest.mark.parametrize(
    "text",
    [
        "川经信办函〔2026〕358号",
        "川经信办函〔2025〕356号",
        "川经信函〔2026〕356号",
        "经信办函〔2026〕356号",
    ],
)
def test_文号没有一模一样的不拿相近的顶替(session, world, text):
    stored = _find(session, text)
    assert (stored["total"], stored["ids"], stored["notes"]) == (0, [], ["doc_number_none"])
    assert stored["conditions"]["docNumber"]


def test_地市区县(session, world, client):
    stored = _find(session, "深圳的科技计划项目")
    assert stored["conditions"]["cities"] == ["广东/深圳"]
    assert stored["ids"] == _ids(world, "sz_apply")
    _same_as_search_page(client, stored)
    stored = _find(session, "广东深圳南山区的专精特新")
    assert stored["conditions"]["districts"] == ["广东/深圳/南山区"]
    assert stored["ids"] == _ids(world, "ns_apply")
    _same_as_search_page(client, stored)
    assert _find(session, "吉林市的科技项目")["ids"] == _ids(world, "jl_city")
    assert _find(session, "吉林的科技项目")["ids"] == _ids(world, "jl_prov", "jl_city")


def test_没收录的地区不去搜(session, world):
    # 西藏有栏目、也有通知，但还没成功检查过：不算收录
    for text, name in (("找西藏的科技项目申报", "西藏"), ("找青海的申报通知", "青海")):
        stored = _find(session, text)
        assert (stored["total"], stored["ids"], stored["link"]) == (0, [], False)
        assert stored["notes"] == ["uncovered"]
        assert stored["conditions"]["uncovered"] == [name]
        assert conditions_text(stored) == ""


def test_还有收录了的地区就只搜收录了的(session, world, client):
    stored = _find(session, "四川和西藏的申报通知")
    assert (stored["conditions"]["regions"], stored["conditions"]["uncovered"]) == (
        ["四川"],
        ["西藏"],
    )
    assert stored["ids"] == _ids(world, "sc_apply")
    assert (stored["link"], stored["notes"]) == (True, ["uncovered"])
    _same_as_search_page(client, stored)


def test_问内容照样列通知_末尾加提示(session, world):
    stored = _find(session, "广东今年专精特新什么时候截止")
    assert stored["ids"] == _ids(world, "gd_apply", "gd_list", "ns_apply", "gd_policy")
    assert stored["notes"] == ["content"]
    # 没找到时不加
    assert _find(session, "广东今年低空经济什么时候截止")["notes"] == ["empty"]


@pytest.mark.parametrize("word", ["拍照", "照片", "图片", "截图", "扫一扫", "扫描"])
def test_说到拍照不去搜(session, word):
    stored = _find(session, f"我手上有一张纸质通知，能{word}问吗")
    assert stored == {
        "type": "find",
        "conditions": chat_find.empty_conditions(),
        "total": 0,
        "ids": [],
        "link": False,
        "notes": ["photo"],
    }
    assert NOTES["photo"] == "请输入通知标题或文号，帮你找原文。"


def test_像信用代码的不出错(session, world):
    stored = _find(session, "91440300MA5D9XXX0X")
    assert (stored["total"], stored["notes"]) == (0, ["empty"])


def test_搜不到就说搜不到_不去掉条件重搜(session, world):
    stored = _find(session, "广东今年的低空经济政策")
    assert (stored["total"], stored["notes"]) == (0, ["empty"])
    conditions = stored["conditions"]
    assert (conditions["kind"], conditions["keywords"]) == ("政策", ["低空经济"])


# ---- 映射表：每种类型、时间交给 search_statement 的和搜索页链接一样 ----

KIND_WORDS = {"申报通知": "申报通知", "名单": "名单", "政策": "政策", "all": "专精特新"}
TIME_WORDS = {
    "近 7 天": "近7天",
    "近 30 天": "近30天",
    "近 90 天": "近90天",
    "今年": "今年",
    "不限": "",
}


@pytest.mark.parametrize("kind", list(KIND_WORDS))
@pytest.mark.parametrize("time", list(TIME_WORDS))
def test_每种类型和时间和搜索页结果一样(session, world, client, kind, time):
    stored = _find(session, f"广东{TIME_WORDS[time]}的{KIND_WORDS[kind]}")
    assert (stored["conditions"]["kind"], stored["conditions"]["time"]) == (kind, time)
    assert set(TIME_WORDS) == set(v1.TIMES) and set(KIND_WORDS) == v1.TABS
    _same_as_search_page(client, stored)
    if stored["total"]:
        assert stored["link"]
        assert render(session, stored, today=TODAY)["searchLink"] == search_params(
            stored["conditions"]
        )


@pytest.mark.parametrize(
    "text",
    ["国家和广东今年的专精特新", "部委的专精特新", "深圳近90天的科技计划项目", "南山区今年的资金"],
)
def test_地区和国家部委和搜索页结果一样(session, world, client, text):
    stored = _find(session, text)
    assert stored["total"]
    _same_as_search_page(client, stored)


def test_搜索页表达不了的不给链接(session, world):
    stored = _find(session, "广东专精特新、中小企业申报")
    assert stored["conditions"]["keywords"] == ["专精特新", "中小"]
    assert stored["link"] is False
    assert "q" not in search_params(stored["conditions"])
    assert stored["ids"] == _ids(world, "gd_apply")


# ---- 超过 10 份 ----


@pytest.fixture
def many(session):
    js = _source(session, "江苏")
    docs = [
        _doc(
            session,
            js,
            f"江苏省工业和信息化厅关于组织申报2026年人工智能项目的通知（第{i}批）",
            date(2026, 6, 14) - timedelta(days=i),
        )
        for i in range(12)
    ]
    session.commit()
    return [doc.id for doc in docs]


def test_超过10份列最新10份(session, many):
    stored = _find(session, "江苏人工智能申报")
    assert (stored["total"], stored["ids"]) == (12, many[:10])
    assert (stored["link"], stored["notes"]) == (True, ["more"])
    stored = _find(session, "江苏人工智能、组织申报")
    assert (stored["total"], stored["link"]) == (12, False)
    assert stored["notes"] == ["more", "narrow"]


def test_词太多只按前3个(session, many):
    stored = _find(session, "江苏人工智能、组织、省工业和信息化厅、第3批")
    assert stored["conditions"]["keywords"] == ["人工智能", "组织", "省工业和信息化厅"]
    assert "first_three" in stored["notes"]


# ---- 存的和读的 ----


def test_存的只有关键词和文号是原话里的字(session, world):
    text = "帮我找一下广东今年的专精特新申报通知"
    stored = _find(session, text)
    raw = json.dumps(stored, ensure_ascii=False)
    assert "专精特新" in raw
    for piece in ("帮我", "找一下", "申报通知的"):
        assert piece not in raw
    # 整个标题比中的：只记 true，不存那句话，也不存标题
    stored = _find(session, "关于印发广东省专精特新中小企业培育管理办法试行的通知")
    raw = json.dumps(stored, ensure_ascii=False)
    assert stored["conditions"]["title"] is True
    for piece in ("培育", "管理办法", "印发"):
        assert piece not in raw
    assert set(stored) == {"type", "conditions", "total", "ids", "link", "notes"}


def test_读的时候按编号重新取(session, world):
    stored = _find(session, "找广东近 30 天的申报通知")
    # 存了以后：一份下架了，另一份截止刚核对过
    session.get(Document, world["sz_apply"]).taken_down_at = STAMP
    session.add(
        DocumentDeadline(
            document_id=world["gd_apply"],
            fingerprint="chat-find",
            kind="企业申报截止",
            candidate_date=date(2026, 6, 30),
            approved_date=date(2026, 6, 30),
            evidence="企业申报截止时间为6月30日",
            confidence="高",
            status="approved",
        )
    )
    session.commit()
    answer = render(session, stored, today=TODAY)
    assert [item["id"] for item in answer["items"]] == [str(world["gd_apply"])]
    item = answer["items"][0]
    assert item["deadline"]["date"] == "2026-06-30"
    assert {"ai", "due", "deadline_state", "link_gone", "title"} <= set(item)
    assert answer["hidden"] == 1
    assert answer["total"] == 2  # 当时的总数
    assert answer["conditionsText"] == "地区 广东 · 类型 申报通知 · 发布 近 30 天"
    assert answer["searchLink"] == {"kind": "申报通知", "time": "近 30 天", "regions": "广东"}
    assert answer["notes"] == []
    assert set(answer) == {
        "type",
        "conditions",
        "conditionsText",
        "total",
        "items",
        "searchLink",
        "notes",
        "hidden",
    }


def test_读的时候_没有链接的不给searchLink(session, world):
    answer = render(session, _find(session, "川经信办函〔2026〕356号"), today=TODAY)
    assert answer["searchLink"] is None
    assert [item["id"] for item in answer["items"]] == [str(world["sc_apply"])]
    answer = render(session, _find(session, "找西藏的科技项目申报"), today=TODAY)
    assert (answer["items"], answer["conditionsText"], answer["hidden"]) == ([], "", 0)


def test_读的时候没有回答也不出错(session):
    answer = render(session, {}, today=TODAY)
    assert (answer["items"], answer["total"], answer["hidden"]) == ([], 0, 0)
    assert (answer["searchLink"], answer["notes"]) == (None, [])
    assert answer["conditions"] == chat_find.empty_conditions()


def test_条件那句的写法():
    stored = {
        "conditions": chat_find.empty_conditions()
        | {"regions": ["广东"], "kind": "申报通知", "time": "今年", "keywords": ["专精特新"]},
        "notes": [],
    }
    assert conditions_text(stored) == "地区 广东 · 类型 申报通知 · 发布 今年 · 标题里有 专精特新"
    stored["conditions"] = chat_find.empty_conditions() | {
        "regions": ["国家部委"],
        "cities": ["山东/青岛"],
        "districts": ["北京/北京/朝阳区"],
    }
    assert conditions_text(stored) == (
        "地区 国家部委、山东·青岛、北京·朝阳区 · 类型 不限（不含资讯与指南） · 发布 不限时间"
    )


# ---- /chat/examples ----


@pytest.fixture
def busy(session):
    gd, sc, js = (_source(session, p) for p in ("广东", "四川", "江苏"))
    gj = _source(session, "国家", "国家级")
    xz = _source(session, "西藏", covered=False)
    for src, count in ((gd, 3), (sc, 2), (js, 1), (gj, 5), (xz, 6)):
        for i in range(count):
            _doc(session, src, f"关于组织申报2026年第{i + 1}批科技项目的通知", date(2026, 6, 1 + i))
    # 30 天以前的不算
    for i in range(4):
        _doc(session, js, f"关于组织申报2026年第{i + 1}批人才项目的通知", date(2026, 4, 1 + i))
    session.commit()
    return js


def test_示例是近30天申报通知最多的两个已收录省份(session, busy):
    sentences = examples(session, now=NOW)
    # 国家部委不是省份；西藏没收录；江苏 30 天以前的不算
    assert sentences == ["找广东近 30 天的申报通知", "找四川近 30 天的申报通知"]
    for sentence in sentences:
        stored = _find(session, sentence)
        assert stored["total"] and stored["conditions"]["regions"] == [sentence[1:3]]


def test_示例按北京时间每小时算一次(session, busy):
    assert examples(session, now=NOW)[0] == "找广东近 30 天的申报通知"
    for i in range(4):
        _doc(session, busy, f"关于组织申报2026年第{i + 1}批数据项目的通知", date(2026, 6, 10 + i))
    session.commit()
    # 同一个小时里不重算
    later = NOW + timedelta(minutes=50)
    assert examples(session, now=later.astimezone(UTC))[0] == "找广东近 30 天的申报通知"
    assert examples(session, now=NOW + timedelta(hours=1)) == [
        "找江苏近 30 天的申报通知",
        "找广东近 30 天的申报通知",
    ]


def test_没有申报通知就不给示例(session):
    _source(session, "广东")
    session.commit()
    assert examples(session, now=NOW) == []


def test_同样多的按行政区划顺序(session):
    sc, gd = _source(session, "四川"), _source(session, "广东")
    for src in (sc, gd):
        _doc(session, src, "关于组织申报2026年科技项目的通知", date(2026, 6, 1))
    session.commit()
    assert examples(session, now=NOW) == ["找广东近 30 天的申报通知", "找四川近 30 天的申报通知"]


# ---- 提示代码 ----


def test_提示代码都有对应的话():
    assert set(NOTES) == {
        "title",
        "doc_number_none",
        "uncovered",
        "year",
        "first_three",
        "more",
        "narrow",
        "empty",
        "title_none",
        "content",
        "photo",
        "vague",
    }
    for code in NOTES:
        assert f"- {code}：" in chat_find.__doc__, code
    assert NOTES["uncovered"].format(uncovered="西藏") == "收录里没有西藏的来源，查不到那里的通知。"


@pytest.mark.parametrize("text", ["《》", "😀", "帮我找一下", "有没有什么", "新"])
def test_一个条件都没有不去搜(session, world, text):
    """只有停用词、标点、表情或一个字：不去搜（不然就把全部通知都列出来了），请用户换个说法。"""
    stored = _find(session, text)
    assert stored["total"] == 0 and stored["ids"] == [] and stored["notes"] == ["vague"]
    assert stored["link"] is False
    assert "没认出要找什么" in NOTES["vague"]


def test_停用词粘在词里不拆开():
    """切完只剩一个字、又紧挨着两个字以上的停用词的，并成一个词（"新材料"不拆成"新"和"材料"）。"""
    assert _parse("新材料")["keywords"] == ["新材料"]
    conditions = _parse("找新材料方面的政策")
    assert conditions["keywords"] == ["新材料"] and conditions["kind"] == "政策"
    assert _parse("大企业的申报通知")["keywords"] == ["大企业"]
    # 两个字以上的照旧拆：停用词"企业"去掉，"高新技术"留下
    assert _parse("请问有没有高新技术企业认定的材料要求")["keywords"] == ["高新技术"]


def test_问内容的提示只在那几个词单独出现时才加():
    parse_text = lambda text: chat_find.parse(text, today=TODAY, covered=ALL_COVERED)  # noqa: E731
    assert parse_text("新材料").content is False
    assert parse_text("专精特新什么时候截止").content is True
    assert parse_text("要交哪些材料").content is True
