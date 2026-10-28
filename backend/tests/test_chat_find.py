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
        # "近 N 日"和"近 N 天"一样；"这星期"这几种和"这周"一样
        *[(p, "近 7 天") for p in ("近7日", "近 7 日", "近七日", "最近7日", "最近七日")],
        *[(p, "近 7 天") for p in ("这星期", "本星期", "这个星期")],
        *[(p, "近 30 天") for p in ("近30日", "近 30 日", "近三十日", "最近30日", "最近三十日")],
        *[(p, "近 90 天") for p in ("近90日", "近九十日", "最近90日", "最近九十日")],
        *[
            (p, "近 7 天")
            for p in ("近一星期", "最近一星期", "近一个星期", "最近一个星期", "这礼拜", "这个周")
        ],
        *[(p, "近 7 天") for p in ("过去7天", "过去一周", "7天内", "一周内")],
        *[(p, "近 30 天") for p in ("过去30天", "过去一个月", "30天内", "一个月内")],
        *[(p, "近 90 天") for p in ("过去三个月", "90天内", "三个月内", "本季度")],
        *[(p, "今年") for p in ("今年", "本年", "2026年", "2026", "２０２６年")],
        ("今年最新", "近 30 天"),  # 几个时间词都有时，按近 7 天、近 30 天、近 90 天、今年的顺序取
        ("这周今年", "近 7 天"),
    ],
)
def test_时间词(phrase, value):
    conditions = _parse(f"广东{phrase}的专精特新申报通知")
    assert conditions["time"] == value
    assert (conditions["year"], conditions["period"]) == (None, None)
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


LAST_WEEK = {"from": "2026-06-08", "to": "2026-06-14"}
LAST_MONTH = {"from": "2026-05-01", "to": "2026-05-31"}


@pytest.mark.parametrize(
    "phrase,period",
    [
        *[(p, LAST_WEEK) for p in ("上周", "上个周", "上星期", "上个星期", "上一周", "上一星期")],
        *[(p, LAST_WEEK) for p in ("上一个星期", "上礼拜", "上个礼拜")],
        *[(p, LAST_MONTH) for p in ("上个月", "上月", "上一个月")],
        # 上上周、上上个月再往前一个，不认成上周、上个月
        *[(p, {"from": "2026-06-01", "to": "2026-06-07"}) for p in ("上上周", "上上个星期")],
        *[(p, {"from": "2026-06-01", "to": "2026-06-07"}) for p in ("上上一周", "上上一个星期")],
        *[(p, {"from": "2026-04-01", "to": "2026-04-30"}) for p in ("上上个月", "上上月")],
        ("上上一个月", {"from": "2026-04-01", "to": "2026-04-30"}),
        # 和别的时间词一起时先算这两种；两种都有取先写的
        ("今年上个月", LAST_MONTH),
        ("上周最新", LAST_WEEK),
        ("去年上个月", LAST_MONTH),
        ("上个月和上周", LAST_MONTH),
        ("上周和上个月", LAST_WEEK),
    ],
)
def test_上周和上个月按发布日期筛(phrase, period):
    conditions = _parse(f"广东{phrase}的专精特新申报通知")
    assert (conditions["period"], conditions["time"], conditions["year"]) == (period, "不限", None)
    assert conditions["keywords"] == ["专精特新"]


@pytest.mark.parametrize(
    "text", ["这个周期的申报通知", "上个周期的申报通知", "本周期的申报通知", "17天内的申报通知"]
)
def test_像时间词又不是的不认(text):
    """以"周"结尾的后面是"期"、以数字开头的前面紧挨着数字：不当时间。"""
    conditions = _parse(text)
    assert (conditions["time"], conditions["period"]) == ("不限", None), text
    assert _parse("十一个月内的申报通知")["time"] == "不限"


def test_上周是上一个星期一到星期日_上个月是上一个自然月():
    def period(text, today):
        return parse(text, today=today, covered=ALL_COVERED).conditions["period"]

    assert period("上周", date(2026, 6, 14)) == {"from": "2026-06-01", "to": "2026-06-07"}  # 星期日
    assert period("上周", date(2027, 1, 6)) == {"from": "2026-12-28", "to": "2027-01-03"}
    assert period("上个月", date(2027, 1, 6)) == {"from": "2026-12-01", "to": "2026-12-31"}
    assert period("上个月", date(2026, 3, 31)) == {"from": "2026-02-01", "to": "2026-02-28"}


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


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("国家高新技术企业认定", ["高新技术"]),
        ("国家级专精特新小巨人企业", ["专精特新", "小巨人"]),  # 项目名称在那里切开
        ("国家企业技术中心认定", ["技术中心"]),  # "企业"后面还是名称
        ("国家认定企业技术中心", ["技术中心"]),  # "认定"后面也是
        ("国家重点研发计划", ["重点研发计划"]),
        ("国家级“小巨人”企业", ["小巨人"]),  # 开头的引号先跳过
        ("国家《高新技术企业》认定", ["高新技术"]),
    ],
)
def test_国家后面紧跟着名称时是名称的开头_不当地区也不当关键词(text, keywords):
    conditions = _parse(text)
    assert (conditions["regions"], conditions["keywords"]) == ([], keywords)
    conditions = _parse(f"广东{text}")
    assert (conditions["regions"], conditions["keywords"]) == (["广东"], keywords)


@pytest.mark.parametrize(
    "text",
    [
        "国家",
        "国家级",
        "找国家级的",
        "国家最新的申报通知",
        "国家申报通知",
        "国家2026年的申报通知",
        "国家关于专精特新的政策",
        "国家有哪些专精特新政策",
        "国家发的申报通知",
        "国家出台的政策",
        "国家公布的名单",
        "上月国家发布的政策",
    ],
)
def test_国家单独说的是国家部委(text):
    conditions = _parse(text)
    assert conditions["regions"] == ["国家部委"], text
    # 说发文的词不留成关键词；只剩真正的词（"专精特新"）
    assert conditions["keywords"] in ([], ["专精特新"]), text


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("国家数据局的申报通知", ["数据局"]),
        ("国家知识产权局的通知", ["知识产权局"]),
        ("国家发展改革委的通知", ["发展改革委"]),
        ("国家卫生健康委员会的通知", ["卫生健康委员会"]),
        ("国家网信办的通知", ["网信办"]),
        ("国家工业和信息化部的通知", ["工业和信息化部"]),  # 名称里的"和"不断开
        ("国家税务总局公告", ["税务总局"]),
        ("国家各部委的通知", []),
        ("国家民委的通知", ["民委"]),
        # 按最短的机关名算；"发展改革委"是表里的机关名，后面还剩 3 个字，在那里切开
        ("国家发展改革委办公厅的通知", ["发展改革委", "办公厅"]),
        ("国家能源局综合司的通知", ["能源局综合司"]),
        ("国家数据局发的通知", ["数据局"]),
    ],
)
def test_国家后面紧跟着国家机关名照旧算国家部委(text, keywords):
    conditions = _parse(text)
    assert (conditions["regions"], conditions["keywords"]) == (["国家部委"], keywords)
    # 机关名后面是"和＋地名"时到那里为止，"和"不留在关键词上
    conditions = _parse("国家数据局和广东的申报通知")
    assert (conditions["regions"], conditions["keywords"]) == (["国家部委", "广东"], ["数据局"])


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("国家产业布局", ["产业布局"]),
        ("国家西部大开发", ["西部大开发"]),
        ("国家战略部署", ["战略部署"]),
        ("国家级经济技术开发区管委会", ["经济技术开发区管委会"]),  # "国家级"后面不看机关名
        ("国家核心零部件项目", ["核心零部件"]),  # 整段名称以机关字结尾才算
        ("国家电网有限公司的项目", ["电网有限"]),  # "公司"不是机关
        ("国家高新技术企业认定怎么办理", ["高新技术"]),
        ("国家专精特新怎么办", ["专精特新"]),
    ],
)
def test_以局委部署办结尾又不是机关名的不算(text, keywords):
    conditions = _parse(text)
    assert (conditions["regions"], conditions["keywords"]) == ([], keywords)


def test_地区后面的和与及或不去_可能是名称的一部分():
    assert _parse("广东或四川的专精特新")["regions"] == ["广东", "四川"]
    for text, keyword in (
        ("碳中和广东的政策", "碳中和"),  # 地区前面的：最后两个字是一个词，不去
        ("四川和邦生物", "和邦生物"),
        ("天津和平区的申报通知", "和平区"),  # 和平区没收录：照第 3 条当关键词
        ("广东和平县", "和平县"),
        ("安徽和县", "和县"),
        ("新疆和田", "和田"),
    ):
        assert _parse(text)["keywords"] == [keyword], text


def test_国家后面跟着地名或标点也是国家部委():
    for text in ("国家、广东的专精特新", "国家广东的专精特新", "国家与广东的专精特新"):
        assert _parse(text)["regions"] == ["国家部委", "广东"], text


def test_以市开头的区县和城市名连着写_认成那个区县():
    covered = Coverage(
        provinces=frozenset({"山东"}),
        cities=frozenset({("山东", "济南"), ("山东", "枣庄")}),
        districts=frozenset({("山东", "济南", "市中区"), ("山东", "枣庄", "市中区")}),
    )
    for text in ("济南市中区的申报通知", "济南市市中区的申报通知", "山东济南市中区的申报通知"):
        conditions = _parse(text, covered)
        assert (conditions["regions"], conditions["cities"]) == ([], []), text
        assert (conditions["districts"], conditions["keywords"]) == (["山东/济南/市中区"], []), text
    assert _parse("枣庄市中区的申报通知", covered)["districts"] == ["山东/枣庄/市中区"]
    assert _parse("济南市的申报通知", covered)["cities"] == ["山东/济南"]
    # 区县没收录的：按整个城市找，说这个区县没收录，"中区"不当关键词
    city_only = Coverage(frozenset({"山东"}), frozenset({("山东", "济南")}), frozenset())
    conditions = _parse("济南市中区的申报通知", city_only)
    assert (conditions["cities"], conditions["keywords"]) == (["山东/济南"], [])
    assert conditions["uncovered"] == ["山东·济南·市中区"]


CITIES_ONLY = Coverage(
    provinces=frozenset({"山东", "广东", "浙江"}),
    cities=frozenset(
        {("山东", "济南"), ("山东", "青岛"), ("广东", "深圳"), ("广东", "广州"), ("浙江", "温州")}
    ),
    districts=frozenset(),
)


@pytest.mark.parametrize(
    "text,city,district,keywords",
    [
        ("济南市中区的申报通知", "山东/济南", "山东·济南·市中区", []),
        ("济南市市中区的申报通知", "山东/济南", "山东·济南·市中区", []),
        ("青岛市南区的申报通知", "山东/青岛", "山东·青岛·市南区", []),
        ("青岛市北区专精特新", "山东/青岛", "山东·青岛·市北区", ["专精特新"]),
        ("深圳宝安区的科技项目", "广东/深圳", "广东·深圳·宝安区", ["科技"]),
        ("深圳市宝安区的科技项目", "广东/深圳", "广东·深圳·宝安区", ["科技"]),
        ("深圳 光明新区的申报通知", "广东/深圳", "广东·深圳·光明新区", []),
        ("广州的天河区申报通知", "广东/广州", "广东·广州·天河区", []),  # 中间隔着"的"也算
        # 以"山区""湾区"结尾的区县名照样算（这两个只比整个词）
        ("深圳南山区的申报通知", "广东/深圳", "广东·深圳·南山区", []),
        ("温州龙湾区的申报通知", "浙江/温州", "浙江·温州·龙湾区", []),
    ],
)
def test_收录了的城市后面没收录的区县_按整个城市找(text, city, district, keywords):
    conditions = _parse(text, CITIES_ONLY)
    assert (conditions["regions"], conditions["cities"], conditions["districts"]) == (
        [],
        [city],
        [],
    )
    assert (conditions["uncovered"], conditions["keywords"]) == ([district], keywords)


@pytest.mark.parametrize(
    "text",
    [
        "广州黄埔区和天河区的申报通知",
        "广州黄埔区、天河区的申报通知",
        "广州黄埔区，天河区的申报通知",
        "广州黄埔区 天河区的申报通知",
        "广州黄埔区 和天河区的申报通知",
        "广州的黄埔区与天河区的申报通知",
    ],
)
def test_连着写的几个没收录的区县都算(text):
    conditions = _parse(text, CITIES_ONLY)
    assert (conditions["cities"], conditions["keywords"]) == (["广东/广州"], [])
    assert conditions["uncovered"] == ["广东·广州·黄埔区", "广东·广州·天河区"]


def test_城市后面不像区县的照旧当关键词():
    for text, keywords in (
        ("深圳社区的申报通知", ["社区"]),
        ("深圳地区的申报通知", []),  # 城市后面的"地区"去掉
        ("深圳经济特区", ["经济特区"]),
        # 以园区、小区这类结尾的都不算
        ("深圳科技园区的申报通知", ["科技园区"]),
        ("深圳产业园区的申报通知", ["产业园区"]),
        ("广州的工业园区", ["工业园区"]),
        ("深圳我家小区", ["我家小区"]),
        ("深圳宝安区人工智能", ["宝安区人工智能"]),  # 超过 4 个字
        ("深圳的专项资金", ["专项"]),
    ):
        conditions = _parse(text, CITIES_ONLY)
        assert (conditions["uncovered"], conditions["keywords"]) == ([], keywords), text
    # 省、直辖市后面的不算（"天津和平区"照第 3 条当关键词）
    conditions = _parse("广东宝安区的申报通知", CITIES_ONLY)
    assert (conditions["uncovered"], conditions["keywords"]) == ([], ["宝安区"])


def test_省市后面的地区去掉_别处的照旧():
    for text, regions, cities in (
        ("广东地区的申报通知", ["广东"], []),
        ("广东省地区的申报通知", ["广东"], []),
        ("深圳地区的申报通知", [], ["广东/深圳"]),
        ("广东地区和四川地区的申报通知", ["广东", "四川"], []),
    ):
        conditions = _parse(text)
        assert (conditions["regions"], conditions["cities"]) == (regions, cities), text
        assert (conditions["keywords"], conditions["uncovered"]) == ([], []), text
    assert _parse("广东地区省级的申报通知")["levels"] == ["省级"]
    # 不跟在省、城市后面的照旧当关键词（去掉会改了意思）
    for text, keywords in (
        ("欠发达地区的政策", ["欠发达地区"]),
        ("西部地区的申报通知", ["西部地区"]),
        ("地区总部认定", ["地区总部"]),
        ("新疆和田地区", ["和田地区"]),
    ):
        assert _parse(text)["keywords"] == keywords, text


# ---- 层级 ----


@pytest.mark.parametrize(
    "text,levels,keywords",
    [
        ("省级的申报通知", ["省级"], []),
        ("市级的申报通知", ["市级"], []),
        *[(f"{word}的申报通知", ["区县级"], []) for word in ("区级", "县级", "区县级")],
        ("省市级的申报通知", ["省级", "市级"], []),
        ("市县级的申报通知", ["市级", "区县级"], []),
        ("找省级2026年的申报通知", ["省级"], []),
        ("省级、市级的专精特新", ["省级", "市级"], ["专精特新"]),
        ("省级和市级专精特新", ["省级", "市级"], ["专精特新"]),  # "市级"跟在"省级和"后面
        # 顿号、逗号也算连着说
        ("国家级、省级专精特新", ["国家部委", "省级"], ["专精特新"]),
        ("国家级、省级、市级专精特新", ["国家部委", "省级", "市级"], ["专精特新"]),
        ("省级、国家级专精特新", ["国家部委", "省级"], ["专精特新"]),
        ("国家级，省级专精特新", ["国家部委", "省级"], ["专精特新"]),
        # "以上"：省级以上 ＝ 国家部委、省级；市级以上再加市级；县级以上各层都要，不筛
        *[(f"{w}的申报通知", ["国家部委", "省级"], []) for w in ("省级以上", "省级及以上")],
        *[(f"{w}的申报通知", ["国家部委", "省级", "市级"], []) for w in ("市级以上", "市级及以上")],
        *[(f"{w}的申报通知", [], []) for w in ("县级以上", "县级及以上", "区县级以上", "区级以上")],
        ("省级以上专精特新企业", [], ["专精特新"]),  # 紧跟着名称的同样是名称的开头
        # 紧跟着名称的是名称的开头：去掉，不算层级，也不当关键词
        ("省级专精特新中小企业", [], ["专精特新", "中小"]),
        ("市级企业技术中心", [], ["技术中心"]),
        ("省级“专精特新”企业", [], ["专精特新"]),
        ("各省级专精特新", [], ["专精特新"]),
        ("副省级城市的申报通知", [], ["副省级城市"]),
    ],
)
def test_层级单独说或跟在别的层级后面才算(text, levels, keywords):
    conditions = _parse(text)
    assert (conditions["levels"], conditions["keywords"]) == (levels, keywords)
    assert conditions["regions"] == []


@pytest.mark.parametrize(
    "text,levels",
    [
        *[(f"{w}的专精特新", ["省级"]) for w in ("各省", "各省份", "各省区市")],
        ("各市专精特新", ["市级"]),  # 一直算，紧跟着名称也算
        ("各地市的专精特新", ["市级"]),
        ("各市县的专精特新", ["市级", "区县级"]),
        *[(f"{w}的专精特新", ["市级", "区县级"]) for w in ("各市区", "各县市区", "各市县区")],
        *[(f"{w}的专精特新", ["区县级"]) for w in ("各区县", "各县区", "各区", "各县")],
    ],
)
def test_各省各市各区县(text, levels):
    conditions = _parse(text)
    assert (conditions["levels"], conditions["keywords"]) == (levels, ["专精特新"])


def test_各市场各区域不是层级_全国各地不筛():
    assert _parse("各市场主体")["levels"] == []
    assert _parse("各区域的专精特新")["levels"] == []
    for text in ("全国的专精特新", "全国各地的专精特新", "各地的专精特新", "各级专精特新"):
        conditions = _parse(text)
        assert (conditions["levels"], conditions["regions"]) == ([], []), text
        assert conditions["keywords"] == ["专精特新"], text


def test_省级地区后面的级按省级看():
    conditions = _parse("广东省级的申报通知")
    assert (conditions["regions"], conditions["levels"], conditions["keywords"]) == (
        ["广东"],
        ["省级"],
        [],
    )
    assert _parse("北京市级的申报通知")["levels"] == ["省级"]  # 直辖市市里的部门是省级来源
    assert _parse("广东省级和市级的申报通知")["levels"] == ["省级", "市级"]
    # 紧跟着名称时同"省级专精特新"：只去掉，按全省找
    for text in ("广东省级专精特新中小企业名单", "广东的省级专精特新中小企业名单"):
        conditions = _parse(text)
        assert (conditions["regions"], conditions["levels"], conditions["keywords"]) == (
            ["广东"],
            [],
            ["专精特新", "中小"],
        ), text
    # 城市后面的"级"去掉：城市本来就是那一层
    conditions = _parse("深圳市级的科技项目")
    assert (conditions["cities"], conditions["levels"], conditions["keywords"]) == (
        ["广东/深圳"],
        [],
        ["科技"],
    )


@pytest.mark.parametrize(
    "text,regions,levels,keywords",
    [
        # 国家部委也放进层级；没说别的地区时从地区里拿掉
        ("国家级和省级专精特新", [], ["国家部委", "省级"], ["专精特新"]),
        ("国家及各省的申报通知", [], ["国家部委", "省级"], []),
        ("国家和各省的专精特新", [], ["国家部委", "省级"], ["专精特新"]),
        ("部委和市级的政策", [], ["国家部委", "市级"], []),
        ("省级和国家级专精特新", [], ["国家部委", "省级"], ["专精特新"]),  # 跟在"省级和"后面
        # 说了别的地区的留着：(国家部委 ∪ 广东) ∩ (国家部委 ∪ 省级)
        ("国家和广东的省级申报通知", ["国家部委", "广东"], ["国家部委", "省级"], []),
    ],
)
def test_有层级时国家部委也算层级(text, regions, levels, keywords):
    conditions = _parse(text)
    assert (conditions["regions"], conditions["levels"]) == (regions, levels)
    assert conditions["keywords"] == keywords


def test_省级地区后面的级以上():
    conditions = _parse("广东省级以上的申报通知")
    # (国家部委 ∪ 广东) ∩ (国家部委 ∪ 省级)
    assert (conditions["regions"], conditions["levels"]) == (
        ["国家部委", "广东"],
        ["国家部委", "省级"],
    )
    assert conditions["keywords"] == []
    assert _parse("广东省级及以上的申报通知")["levels"] == ["国家部委", "省级"]
    conditions = _parse("广东县级以上的申报通知")  # 各层都要：不筛层级，国家部委的也要
    assert (conditions["regions"], conditions["levels"]) == (["国家部委", "广东"], [])
    conditions = _parse("广东省级以上专精特新企业")  # 紧跟着名称：只去掉
    assert (conditions["regions"], conditions["levels"]) == (["广东"], [])
    assert conditions["keywords"] == ["专精特新"]


@pytest.mark.parametrize(
    "text,regions,cities,districts,levels",
    [
        # 城市和区县级：换成这个城市收录了的区县
        ("深圳各区的申报通知", [], [], ["广东/深圳/南山区"], ["区县级"]),
        ("深圳的区县级申报通知", [], [], ["广东/深圳/南山区"], ["区县级"]),
        # 城市和省级：换成所在的省
        ("深圳的省级政策", ["广东"], [], [], ["省级"]),
        ("国家和深圳的省级政策", ["国家部委", "广东"], [], [], ["国家部委", "省级"]),
        ("深圳的市级政策", [], ["广东/深圳"], [], ["市级"]),  # 市级照旧
        # 区县同样换：省级 → 所在的省，市级 → 所在的城市
        ("南山区的省级政策", ["广东"], [], [], ["省级"]),
        ("南山区的市级政策", [], ["广东/深圳"], [], ["市级"]),
        # 市级和区县级一起：城市和它的区县都要
        ("深圳的市级和区县级通知", [], ["广东/深圳"], ["广东/深圳/南山区"], ["市级", "区县级"]),
        # 省级又和市级、区县级一起：按全省找
        ("深圳的省级和市级政策", ["广东"], [], [], ["省级", "市级"]),
    ],
)
def test_城市区县和层级一起说(text, regions, cities, districts, levels):
    conditions = _parse(text)
    assert (conditions["regions"], conditions["cities"]) == (regions, cities)
    assert (conditions["districts"], conditions["levels"]) == (districts, levels)
    assert conditions["uncovered"] == []


def test_城市一个区县都没收录时说没收录():
    conditions = _parse("广州的区县级申报通知", CITIES_ONLY)
    assert (conditions["cities"], conditions["districts"]) == ([], [])
    assert conditions["uncovered"] == ["广东·广州（区县级）"]
    assert conditions_text({"conditions": conditions, "notes": ["uncovered"]}) == ""  # 不去搜
    conditions = _parse("广州和深圳各区的申报通知", CITIES_ONLY)
    assert conditions["uncovered"] == ["广东·广州（区县级）", "广东·深圳（区县级）"]
    # 区县所在的城市没收录、又说了市级
    covered = Coverage(frozenset({"广东"}), frozenset(), frozenset({("广东", "深圳", "南山区")}))
    conditions = _parse("南山区的市级政策", covered)
    assert (conditions["cities"], conditions["uncovered"]) == ([], ["广东·深圳（市级）"])


def test_有层级时国家部委没收录也照样搜别的层级():
    covered = Coverage(frozenset({"广东"}), frozenset(), frozenset())
    conditions = _parse("国家和省级的申报通知", covered)
    assert (conditions["regions"], conditions["levels"]) == ([], ["省级"])
    assert conditions["uncovered"] == ["国家部委"]
    assert conditions_text({"conditions": conditions, "notes": ["uncovered"]}) == (
        "地区 不限 · 层级 省级 · 类型 申报通知 · 发布 不限时间"
    )
    # 写到的地区都没收录：照旧不去搜，条件那句为空
    conditions = _parse("西藏的省级申报通知", covered)
    assert (conditions["levels"], conditions["uncovered"]) == (["省级"], ["西藏"])
    assert conditions_text({"conditions": conditions, "notes": ["uncovered"]}) == ""


def test_跟在地区后面的国家也算国家部委():
    conditions = _parse("广东和国家高新技术企业")
    assert (conditions["regions"], conditions["keywords"]) == (["广东", "国家部委"], ["高新技术"])
    # 前面的不是地区、层级的照旧是名称的开头
    conditions = _parse("专精特新和国家高新技术企业")
    assert (conditions["regions"], conditions["keywords"]) == ([], ["专精特新", "高新技术"])


# ---- 和、机关名、项目名称、空白 ----


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("人工智能和广东的政策", ["人工智能"]),
        ("人工智能与广东的政策", ["人工智能"]),
        ("人工智能 和 广东的政策", ["人工智能"]),
        ("人工智能和各省的政策", ["人工智能"]),  # 层级前面的也去掉
        ("专精特新以及广东", ["专精特新"]),  # "以及"两个字一起去掉
        ("高新技术和专精特新", ["高新技术", "专精特新"]),  # 名称前面的也去掉
        ("专精特新和高新技术企业", ["专精特新", "高新技术"]),  # 名称后面的不粘到下一个词上
        # 最后两个字是一个词的不去
        *[(f"{word}广东的政策", [word]) for word in ("碳中和", "参与", "普及", "涉及")],
        ("碳中和专精特新", ["碳中和", "专精特新"]),
    ],
)
def test_地区层级名称前面的和去掉_最后两个字是一个词的不去(text, keywords):
    assert _parse(text)["keywords"] == keywords


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("发改委专精特新", ["发改委", "专精特新"]),
        ("专精特新发改委", ["专精特新", "发改委"]),
        ("人工智能发改委专精特新", ["人工智能", "发改委", "专精特新"]),
        ("发改委办公室的通知", ["发改委", "办公室"]),
        ("科技厅人工智能", ["科技厅", "人工智能"]),
        ("工信部中小企业局的通知", ["工信部", "中小企业局"]),  # 机关名里的停用词不拆
        # 切完只剩一个字的并回去；"有、和"这类单字不并
        ("省发改委的通知", ["省发改委"]),
        ("市人社局的补贴", ["市人社局", "补贴"]),
        ("科技局长", ["科技局长"]),
        ("有发改委的通知吗", ["发改委"]),
    ],
)
def test_机关名在那里切开(text, keywords):
    assert _parse(text)["keywords"] == keywords


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("国家发改委专精特新通知", ["发改委", "专精特新"]),
        ("国家工信部小巨人", ["工信部", "小巨人"]),
        ("国家统计局数据", ["统计局", "数据"]),
        ("国家“发改委”的通知", ["发改委"]),
    ],
)
def test_国家后面以机关名开头的算国家部委(text, keywords):
    conditions = _parse(text)
    assert (conditions["regions"], conditions["keywords"]) == (["国家部委"], keywords)


def test_机关简称的别的写法写在条件里_不给搜索页链接():
    conditions = chat_find.empty_conditions() | {"keywords": ["发改委"]}
    stored = {"conditions": conditions, "notes": []}
    assert conditions_text(stored).endswith("标题里有 发改委（或 发展改革委、发展和改革委员会）")
    assert "q" not in search_params(conditions)
    for keyword, others in (
        ("发展改革委", "发改委、发展和改革委员会"),  # 写全称的也一样
        ("省发改委", "省发展改革委、省发展和改革委员会"),  # 前面多一个字的同样换
        ("工信部", "工业和信息化部"),
        ("人社局", "人力资源社会保障局、人力资源和社会保障局"),
        ("市场监督管理局", "市监局、市场监管局"),
        ("国税总局", "税务总局"),
    ):
        conditions["keywords"] = [keyword]
        assert conditions_text(stored).endswith(f"标题里有 {keyword}（或 {others}）"), keyword
        assert "q" not in search_params(conditions)
    conditions["keywords"] = ["科技厅", "专精特新"]  # 没有别的写法的照旧
    assert conditions_text(stored).endswith("标题里同时有 科技厅、专精特新")
    conditions["keywords"] = ["科技厅"]
    assert search_params(conditions)["q"] == "科技厅"


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("专精特新小巨人", ["专精特新", "小巨人"]),
        ("小巨人专精特新", ["小巨人", "专精特新"]),
        ("制造业单项冠军企业", ["制造业", "单项冠军"]),
        ("隐形冠军瞪羚企业", ["隐形冠军", "瞪羚"]),
        ("独角兽揭榜挂帅", ["独角兽", "揭榜挂帅"]),
        ("产业链链主企业", ["产业链", "链主"]),
        ("雏鹰计划", ["雏鹰", "计划"]),
    ],
)
def test_项目名称在那里切开(text, keywords):
    assert _parse(text)["keywords"] == keywords


def test_打了空白的地方切开关键词():
    assert _parse("人工智能 补贴")["keywords"] == ["人工智能", "补贴"]
    assert _parse("人工智能补贴")["keywords"] == ["人工智能补贴"]
    assert _parse("人工智能　补贴")["keywords"] == ["人工智能", "补贴"]  # 全角空格
    # 地区、时间照样按去掉空白的字认
    conditions = _parse("广 东 近 30 天 的 人工 智能")
    assert (conditions["regions"], conditions["time"]) == (["广东"], "近 30 天")
    assert conditions["keywords"] == ["人工", "智能"]
    # "本年度"换成"2026年度"以后，后面打了空白的地方跟着挪；"本年度"中间的不算
    assert _parse("本年度人工 智能")["keywords"] == ["2026年度", "人工", "智能"]
    assert _parse("本年度 本年度人工 智能")["keywords"] == ["2026年度", "人工", "智能"]
    assert _parse("本 年度人工智能")["keywords"] == ["2026年度", "人工智能"]


def test_写了几个意思不一样的时间():
    def times(text):
        return parse(text, today=TODAY, covered=ALL_COVERED).times

    for text in (
        "这周和上周",
        "今年最新",
        "上个月和上周",
        "去年和前年",
        "去年上个月",
        "2025年今年",
    ):
        assert times(f"广东{text}的申报通知"), text
    # 意思一样的算一个
    for text in ("最近一周这周", "2026年今年", "去年2025年", "上周上个星期", "上周"):
        assert not times(f"广东{text}的申报通知"), text


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
        "levels": [],
        "uncovered": [],
        "kind": "申报通知",
        "time": "近 30 天",
        "year": None,
        "period": None,
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


def test_上周和上个月按发布日期找_不给搜索页链接(session, world):
    stored = _find(session, "四川上周的申报通知")
    assert stored["conditions"]["period"] == {"from": "2026-06-08", "to": "2026-06-14"}
    assert stored["ids"] == _ids(world, "sc_apply")  # 6 月 2 日的那份不在上周
    assert (stored["link"], stored["notes"]) == (False, [])
    assert (
        conditions_text(stored)
        == "地区 四川 · 类型 申报通知 · 发布于 2026 年 6 月 8 日至 6 月 14 日"
    )
    assert _find(session, "上个月的专精特新名单")["ids"] == _ids(world, "gd_list")
    assert "vague" not in _find(session, "上个月")["notes"]  # 只写了时间也去搜


def test_上周和上个月两头都算(session):
    hb = _source(session, "湖北")
    days = {
        day: _doc(session, hb, f"湖北省关于组织申报测试项目的通知{n}", day).id
        for n, day in enumerate(
            [date(2026, 4, 30), date(2026, 5, 1), date(2026, 5, 31), date(2026, 6, 1)]
            + [date(2026, 6, 7), date(2026, 6, 8), date(2026, 6, 14), date(2026, 6, 15)]
        )
    }
    session.commit()
    week = _find(session, "湖北上周的申报通知")["ids"]
    assert week == [days[date(2026, 6, 14)], days[date(2026, 6, 8)]]
    month = _find(session, "湖北上个月的申报通知")["ids"]
    assert month == [days[date(2026, 5, 31)], days[date(2026, 5, 1)]]


def test_读的时候_这次以前存的回答没有period也不出错(session, world):
    stored = _find(session, "广东近30天的申报通知")
    del stored["conditions"]["period"]
    del stored["conditions"]["levels"]
    body = render(session, stored, today=TODAY)
    assert (body["conditions"]["period"], body["conditions"]["levels"]) == (None, [])
    assert body["searchLink"] == {"kind": "申报通知", "time": "近 30 天", "regions": "广东"}
    assert body["conditionsText"] == "地区 广东 · 类型 申报通知 · 发布 近 30 天"
    assert conditions_text(stored) == body["conditionsText"]


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
    # 几个关键词要都有：条件那句写"同时有"
    assert conditions_text(stored).endswith("标题里同时有 专精特新、中小")
    stored = _find(session, "发改委或工信部的通知")
    assert conditions_text(stored).endswith(
        "标题里同时有 发改委（或 发展改革委、发展和改革委员会）、工信部（或 工业和信息化部）"
    )


# ---- 层级、机关简称、项目名称、空白、没收录的区县、几个时间（查库） ----


def test_层级和搜索页结果一样(session, world, client):
    stored = _find(session, "国家级和省级专精特新")
    conditions = stored["conditions"]
    assert (conditions["regions"], conditions["levels"]) == ([], ["国家部委", "省级"])
    assert conditions["keywords"] == ["专精特新"]
    # 南山区的是区县级；培训会那份是资讯与指南，折叠
    assert stored["ids"] == _ids(
        world, "gd_apply", "js_next", "gd_list", "gj_train", "gd_policy", "gd_list_old"
    )
    assert (stored["link"], stored["notes"]) == (True, [])
    _same_as_search_page(client, stored)
    assert conditions_text(stored) == (
        "地区 不限 · 层级 国家部委、省级 · 类型 不限（不含资讯与指南） · 发布 不限时间 · "
        "标题里有 专精特新"
    )
    assert render(session, stored, today=TODAY)["searchLink"] == {
        "kind": "all",
        "time": "不限",
        "q": "专精特新",
        "levels": "国家部委,省级",
    }


@pytest.mark.parametrize(
    "text,names",
    [
        ("广东省级的申报通知", ("gd_apply", "gd_cash")),  # 深圳、南山区的不要
        # (国家部委 ∪ 广东) ∩ (国家部委 ∪ 省级)：国家部委的留着，南山区的不要
        (
            "国家和广东省级的专精特新",
            ("gd_apply", "gd_list", "gj_train", "gd_policy", "gd_list_old"),
        ),
        ("各市的科技项目", ("jl_city", "sz_apply")),
        ("各区县的专精特新", ("ns_apply",)),
    ],
)
def test_层级按搜索页的层级筛(session, world, client, text, names):
    stored = _find(session, text)
    assert stored["conditions"]["levels"]
    assert stored["ids"] == _ids(world, *names)
    assert stored["link"] is True
    _same_as_search_page(client, stored)


@pytest.mark.parametrize(
    "text,names,link",
    [
        ("深圳各区的申报通知", ("ns_apply",), True),
        ("南山区的市级项目", ("sz_apply",), True),
        ("深圳的省级政策", ("gd_policy",), True),
        (
            "广东省级以上的专精特新",
            ("gd_apply", "gd_list", "gj_train", "gd_policy", "gd_list_old"),
            True,
        ),
        # 城市和它的区县一起：搜索页打开时区县替换城市，表达不了，不给链接
        ("深圳的市级和区县级申报通知", ("sz_apply", "ns_apply"), False),
    ],
)
def test_城市区县和层级一起说_按搜索页的层级筛(session, world, client, text, names, link):
    stored = _find(session, text)
    assert stored["conditions"]["levels"]
    assert stored["ids"] == _ids(world, *names)
    assert stored["link"] is link
    if link:
        _same_as_search_page(client, stored)


def test_城市一个区县都没收录时不去搜(session, world):
    _source(session, "广东", "市级", "广州")
    session.commit()
    stored = _find(session, "广州的区县级申报通知")
    assert (stored["total"], stored["ids"], stored["notes"]) == (0, [], ["uncovered"])
    assert stored["conditions"]["uncovered"] == ["广东·广州（区县级）"]
    assert conditions_text(stored) == ""
    assert NOTES["uncovered"].format(uncovered="广东·广州（区县级）") == (
        "收录里没有广东·广州（区县级）的来源，查不到那里的通知。"
    )


def test_广东地区和广东找到的一样(session, world, client):
    stored = _find(session, "广东地区的申报通知")
    assert stored["conditions"] == _find(session, "广东的申报通知")["conditions"]
    assert stored["ids"] == _ids(world, "gd_apply", "sz_apply", "ns_apply", "gd_cash")
    assert (stored["link"], stored["notes"]) == (True, [])
    _same_as_search_page(client, stored)


def test_读的时候_以前给了链接现在表达不了的不给(session, world):
    """以前存的回答给了链接，关键词后来成了机关简称（有别的写法，搜索页一个 q 表达不了）：不给。"""
    stored = {
        "conditions": chat_find.empty_conditions() | {"keywords": ["发改委"]},
        "total": 1,
        "ids": [],
        "link": True,
        "notes": [],
    }
    assert render(session, stored, today=TODAY)["searchLink"] is None
    stored["conditions"]["keywords"] = ["专精特新"]
    assert render(session, stored, today=TODAY)["searchLink"] == {
        "kind": "all",
        "time": "不限",
        "q": "专精特新",
    }


def test_机关简称按各种写法找_不给搜索页链接(session, world):
    gd = session.get(Source, session.get(Document, world["gd_apply"]).source_id)
    sz = session.get(Source, session.get(Document, world["sz_apply"]).source_id)
    full = _doc(
        session, gd, "广东省发展改革委关于组织申报2026年数字经济项目的通知", date(2026, 6, 9)
    )
    longest = _doc(
        session, sz, "深圳市发展和改革委员会关于组织申报2026年产业项目的通知", date(2026, 6, 8)
    )
    short = _doc(session, gd, "广东省发改委关于组织申报2026年服务业项目的通知", date(2026, 6, 7))
    session.commit()
    stored = _find(session, "广东发改委的申报通知")
    assert stored["conditions"]["keywords"] == ["发改委"]
    assert stored["ids"] == [full.id, longest.id, short.id]
    assert (stored["link"], stored["notes"]) == (False, [])
    assert conditions_text(stored) == (
        "地区 广东 · 类型 申报通知 · 发布 不限时间 · "
        "标题里有 发改委（或 发展改革委、发展和改革委员会）"
    )
    assert render(session, stored, today=TODAY)["searchLink"] is None
    # 几个词时每个都要命中，机关名命中一种写法就行
    stored = _find(session, "发展改革委数字经济")
    assert stored["conditions"]["keywords"] == ["发展改革委", "数字经济"]
    assert stored["ids"] == [full.id]
    assert _find(session, "国家发改委的通知")["ids"] == []  # 国家部委没有


def test_专精特新小巨人分开找_标题加了引号也找得到(session, world):
    stored = _find(session, "专精特新小巨人")
    assert stored["conditions"]["keywords"] == ["专精特新", "小巨人"]
    assert stored["ids"] == _ids(world, "gd_list", "gj_train")
    assert stored["link"] is False


def test_打了空白的词分开找(session, world):
    stored = _find(session, "四川 工业 管理")
    assert stored["conditions"]["keywords"] == ["工业", "管理"]
    assert stored["ids"] == _ids(world, "sc_near")
    assert _find(session, "四川工业管理")["notes"] == ["empty"]


def test_没找到又有长的词时说可以用空格隔开(session, world):
    stored = _find(session, "广东低空经济无人机补贴")
    assert stored["conditions"]["keywords"] == ["低空经济无人机补贴"]
    assert (stored["total"], stored["notes"]) == (0, ["empty", "spaces"])
    # 4 个字以内的、机关名不加；像标题的照旧只有 title_none
    assert _find(session, "广东低空经济")["notes"] == ["empty"]
    assert _find(session, "广东人力资源和社会保障厅")["notes"] == ["empty"]
    assert NOTES["spaces"].startswith("词连着写时")


def test_没收录的区县按整个城市找_说没收录(session, world, client):
    stored = _find(session, "深圳宝安区的科技计划项目")
    conditions = stored["conditions"]
    assert (conditions["cities"], conditions["keywords"]) == (["广东/深圳"], ["科技计划"])
    assert conditions["uncovered"] == ["广东·深圳·宝安区"]
    assert stored["ids"] == _ids(world, "sz_apply")
    assert (stored["link"], stored["notes"]) == (True, ["uncovered"])
    _same_as_search_page(client, stored)
    assert conditions_text(stored) == (
        "地区 广东·深圳 · 类型 申报通知 · 发布 不限时间 · 标题里有 科技计划"
    )
    assert NOTES["uncovered"].format(uncovered="、".join(conditions["uncovered"])) == (
        "收录里没有广东·深圳·宝安区的来源，查不到那里的通知。"
    )


def test_写了几个时间只按一个找_说一声(session, world):
    stored = _find(session, "四川这周和上周的申报通知")
    assert stored["conditions"]["period"] == LAST_WEEK
    assert (stored["ids"], stored["notes"]) == (_ids(world, "sc_apply"), ["times"])
    stored = _find(session, "广东今年最新的申报通知")
    assert stored["conditions"]["time"] == "近 30 天"
    assert stored["notes"] == ["times"]
    assert "只按条件里“发布”那一项找的" in NOTES["times"]
    # 意思一样的不算
    assert _find(session, "广东最近一周这周的申报通知")["notes"] == []
    assert _find(session, "广东2026年今年的申报通知")["notes"] == []


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
    stored["conditions"] = chat_find.empty_conditions() | {
        "period": {"from": "2026-12-28", "to": "2027-01-03"}
    }
    assert conditions_text(stored) == (
        "地区 不限 · 类型 不限（不含资讯与指南） · 发布于 2026 年 12 月 28 日至 2027 年 1 月 3 日"
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
        "times",
        "first_three",
        "more",
        "narrow",
        "empty",
        "spaces",
        "title_none",
        "content",
        "photo",
        "vague",
    }
    for code in NOTES:
        assert f"- {code}：" in chat_find.__doc__, code
    assert NOTES["uncovered"].format(uncovered="西藏") == "收录里没有西藏的来源，查不到那里的通知。"
    # 也用在上周、上个月、近 N 天上：写"时间"不写"年份"
    assert "或去掉地区、时间再找" in NOTES["empty"] and "年份" not in NOTES["empty"]


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
    assert _parse("企业家的申报通知")["keywords"] == ["企业家"]
    assert _parse("白名单")["keywords"] == ["白名单"]
    assert _parse("黑名单")["keywords"] == ["黑名单"]
    assert _parse("企业白名单")["keywords"] == ["白名单"]
    assert _parse("工业企业白名单")["keywords"] == ["工业", "白名单"]
    assert _parse("有企业补贴吗")["keywords"] == ["补贴"]  # "有、为、给"这类单字不并
    assert _parse("小微企业有什么补贴")["keywords"] == ["小微", "补贴"]
    assert _parse("企业能申请吗")["keywords"] == []
    assert _parse("专精特新名单有吗")["keywords"] == ["专精特新"]


@pytest.mark.parametrize(
    "text,keywords",
    [
        ("怎么申报专精特新", ["专精特新"]),
        ("企业可以申请哪些补贴", ["补贴"]),
        ("需要哪些材料", []),
        ("高企申报流程是什么", ["高企"]),
        ("专精特新怎么办理", ["专精特新"]),
    ],
)
def test_问法的词也去掉(text, keywords):
    assert _parse(text)["keywords"] == keywords


def test_去掉的词后面的和不粘到下一个词上():
    assert _parse("高新技术企业和专精特新企业的申报通知")["keywords"] == ["高新技术", "专精特新"]
    assert _parse("高新技术企业和科技型中小企业")["keywords"] == ["高新技术", "科技型中小"]
    assert _parse("工业和信息化部的通知")["keywords"] == ["工业和信息化部"]
    # 以"和"开头的县级地名不去"和"
    assert _parse("找和平区的申报通知")["keywords"] == ["和平区"]
    assert _parse("关于和田地区的通知")["keywords"] == ["和田地区"]
    # 只和"材料、企业、公司"并：问句里的停用词不并成关键词
    for text, keywords in (
        ("广东有什么政策", []),
        ("高新技术企业认定的条件是什么", ["高新技术"]),
        ("专精特新申报要什么材料", ["专精特新"]),
        ("什么样的条件", []),
    ):
        assert _parse(text)["keywords"] == keywords, text


@pytest.mark.parametrize("word", ["发布", "出台", "印发", "下发", "公布", "颁布"])
def test_说发文的词也去掉(word):
    conditions = _parse(f"广东上周{word}的专精特新申报通知")
    assert (conditions["regions"], conditions["keywords"]) == (["广东"], ["专精特新"])


def test_机关名后面的发的去掉_开发的研发的不去():
    assert _parse("工信部发的通知")["keywords"] == ["工信部"]
    conditions = _parse("广东省科技厅发的申报通知")
    assert (conditions["regions"], conditions["keywords"]) == (["广东"], ["科技厅"])
    assert _parse("开发的软件")["keywords"] == ["开发", "软件"]
    assert _parse("研发的项目")["keywords"] == ["研发"]


def test_问内容的提示只在那几个词单独出现时才加():
    parse_text = lambda text: chat_find.parse(text, today=TODAY, covered=ALL_COVERED)  # noqa: E731
    assert parse_text("新材料").content is False
    assert parse_text("专精特新什么时候截止").content is True
    assert parse_text("要交哪些材料").content is True
