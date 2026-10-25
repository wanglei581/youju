"""名单类通知正文在站内可读、正文里的名单不展开（D-092，youju/lists/body.py）。"""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from youju.api import attachments as download_api
from youju.api import v1
from youju.api.app import app
from youju.api.v1 import _mask_contact_names, body_for_reading
from youju.db import get_session
from youju.lists import body
from youju.models import Document, DocumentDetail, ListEntry, ListParse, Source

NOW = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
NAMES = {"山东星河科技有限公司", "青岛海纳智能有限公司", "烟台远航机械有限公司"}
KEEP = {"山东省工业和信息化厅"}
NOTICE = [
    "各市工业和信息化局：",
    "根据《山东省专精特新中小企业认定管理办法》，经企业申报、市级推荐、专家评审，"
    "拟认定以下企业为2026年第二批专精特新中小企业，现予以公示。",
    "公示时间：2026年10月20日至10月26日。",
    "公示期间如有异议，请以书面形式向省工业和信息化厅反映。联系人：[姓名已脱敏]，电话：[电话已脱敏]。",
]
TABLE = [
    "序号 企业名称 所在市",
    "1 山东星河科技有限公司 济南市",
    "2 青岛海纳智能有限公司 青岛市",
    "3 烟台远航机械有限公司 烟台市",
]
SIGNATURE = ["山东省工业和信息化厅", "2026年10月20日"]


def run(lines, names=NAMES):
    return body.filter_text("\n".join(lines), names=names, keep=KEEP, min_chars=50)


def test_list_lines_and_notice_lines() -> None:
    def hidden(line):
        return body.is_list_line(line, NAMES, KEEP)

    # 通知里的话（有句子标点）照常显示
    assert not any(hidden(line) for line in NOTICE)
    # 表格行、表头不展开；带标点、不长的有条目名称的行也算（"统一代码：……"）
    assert all(hidden(line) for line in TABLE)
    assert hidden("1 山东星河科技有限公司 统一代码：913701020001000284；")
    # 一段正好是条目名称的行（顿号连着几家）不展开
    assert hidden("山东星河科技有限公司、青岛海纳智能有限公司")
    # 序号开头、后面还有别的段的；只有一个单位名的；像人名的；几个人名并列的；人员表头
    assert hidden("4 某某科技有限公司")
    assert hidden("某某科技有限公司")
    assert hidden("张三")
    assert hidden("张三、李四、王五")
    assert hidden("序号 姓名 工作单位 职务")
    # 表格行带日期、时间也算；字间带空格的表头也算
    assert hidden("86 柔化科技团队 2024年5月 创业组 芜湖市")
    assert hidden("1 某某创客团队 9：00 路演")
    assert hidden("序 号 姓 名 工 作 单 位 职 务")
    # 有条目名称、但带着异议这类话的是通知里的句子
    assert not hidden("1 山东星河科技有限公司 须于10月26日前提异议。")
    # 署名、日期、日期区间、"共 64 家""特此公告"、标题这类照常显示
    for line in (
        "2026 年 7 月 15 日",
        "山东省工业和信息化厅",
        "2026年10月20日",
        "2024年10月1日 至 2024年10月7日",
        "共 64 家",
        "公示期 五个 工作日",
        "特此公告",
        "关于2026年第二批专精特新中小企业名单的公示",
        "一、认定条件",
    ):
        assert not hidden(line), line


def test_filter_text_replaces_the_list_block_and_keeps_the_notice() -> None:
    out = run(NOTICE + TABLE + SIGNATURE)
    assert out.text is not None and out.hidden is True
    assert "公示时间：2026年10月20日至10月26日。" in out.text
    assert out.text.split("\n")[-2:] == SIGNATURE
    assert out.text.count(body.PLACEHOLDER) == 1
    assert not any(name in out.text for name in NAMES)
    # 正文里没有名单（名单在附件里）：原样给，不说"名单不展开"
    plain = run(NOTICE + SIGNATURE)
    assert plain.text == "\n".join(NOTICE + SIGNATURE) and plain.hidden is False
    # 几乎全是名单：剩下的通知文字太少，整篇按名单处理
    assert run(TABLE + SIGNATURE[:1]) == body.Filtered(None, True, "list")
    # 没认出名单、正文本来就很短（多半没抓全，或只抓到网页导航）：按普通正文没抓全处理
    assert run(["首页 政务公开 科技资讯", "现将名单予以公示（见附件）。"]) == body.Filtered(
        None, False, "short"
    )


def test_text_after_the_list_is_not_swallowed() -> None:
    # 名单后面的公示期、日期区间、"共 64 家"、没写标点的通知句照常显示，不会被并进名单那一块
    after = [
        "如有异议请向市科委反映",
        "公示期为2024年10月1日至10月7日。",
        "2024年10月1日 至 2024年10月7日",
        "共 64 家",
        "特此公示",
    ]
    out = run(NOTICE + TABLE + after + SIGNATURE)
    assert out.text is not None
    lines = out.text.split("\n")
    assert lines[lines.index(body.PLACEHOLDER) + 1 :] == [*after, *SIGNATURE]


def test_one_cell_per_line_tables_are_hidden_as_one_block() -> None:
    # 格子里套了段落：一个格子一行，长名称被折成两行；序号、类别、半个名称都在这一块里
    cells = ["序号", "企业名称", "领域"]
    for i, name in enumerate(sorted(NAMES), 1):
        cells += [str(i), name, "高端装备制造"]
    cells += ["4", "某某（天津）网络安全科技有限责任公", "司", "新材料"]
    out = run(NOTICE + cells + SIGNATURE)
    assert out.text is not None and out.text.count(body.PLACEHOLDER) == 1
    for gone in ("高端装备制造", "新材料", "有限责任公", "序号"):
        assert gone not in out.text
    assert out.text.split("\n")[-2:] == SIGNATURE


def test_wide_unparsed_cell_tables_and_thousands_separators() -> None:
    # 序号写成"4,300"：逗号后面是数字，不算句子标点
    assert body.is_list_line("4,300 某某环保科技有限公司 2026360111A8011849", set(), set())
    # 没解析出条目、名称也不是常见单位名、一行有 7 列的一格一行表格：靠逐个递增的序号认出来
    cells = ["2025年度顶尖孵化器绩效评价结果"]
    for i in range(1, 4):
        cells += [str(i), f"甲{i}人工智能顶尖孵化器", "甲市", "优秀", "运营良好", "2024", "备注"]
    out = run(NOTICE + cells, names=set())
    assert out.text is not None
    assert out.text.split("\n")[-2:] == ["2025年度顶尖孵化器绩效评价结果", body.PLACEHOLDER]
    for gone in ("孵化器", "优秀", "甲市"):
        assert gone not in out.text.split("\n")[-1]


def test_a_stray_line_far_from_the_list_does_not_hide_the_notice() -> None:
    # 通知中间偶然一行只有一个单位名、称呼没带冒号：不算名单那一块的起点
    lines = [
        "各有关企业",
        NOTICE[1],
        "某某科技有限公司",
        NOTICE[2],
        NOTICE[3],
        "特此公告",
        "山东省工业和信息化厅",
        "2026年10月20日",
        "附件",
        "名单",
        *TABLE,
    ]
    out = run(lines)
    assert out.text is not None
    # 通知文字、署名、日期都在；紧挨在表格前面的"附件""名单"短标签并进了名单那一块
    assert out.text.split("\n") == [*lines[:8], body.PLACEHOLDER]


def test_numbered_lists_and_spaced_headers() -> None:
    # 逐行编号的名单（地区、单位，没解析出条目）：连着 5 行以上、编号逐个递增的整块不展开
    regions = [
        "北京市朝阳区",
        "天津市武清区",
        "山西省晋城市",
        "内蒙古自治区鄂尔多斯市",
        "辽宁省沈阳市",
    ]
    items = [f"{i}、{name}" for i, name in enumerate(regions, 1)]
    out = run(
        [*NOTICE, "附件：", "第五批全国社会信用体系建设示范区名单", *items, "6、黑龙江省鸡西市"],
        names=set(),
    )
    assert out.text is not None and out.hidden is True
    assert out.text.split("\n")[-3:] == [
        "附件：",
        "第五批全国社会信用体系建设示范区名单",
        body.PLACEHOLDER,
    ]
    # 通知里的小标题（"1、认定条件"）中间隔着段落，不算名单；不到 5 行的编号也不算
    sections = []
    for i, title in enumerate(("认定条件", "材料要求", "受理时间", "工作要求", "其他事项"), 1):
        sections += [f"{i}、{title}", NOTICE[1]]
    assert run(sections, names=set()).hidden is False
    assert run(NOTICE + items[:4], names=set()).hidden is False
    # 小标题之间只隔着空行也不算；名称里带"材料"的单位照样算条目
    outline = [
        f"{i}、{t}"
        for i, t in enumerate(
            ("申报条件", "材料要求", "工作程序", "办理流程", "其他事项", "时间安排"), 1
        )
    ]
    assert run([*NOTICE, *"\n\n".join(outline).split("\n")], names=set()).hidden is False
    firms = [f"{i}、山东第{i}新材料科技有限公司" for i in range(1, 6)]
    assert run([*NOTICE, *firms], names=set()).hidden is True
    # PDF 转成的文字字字之间带空格的表头（"序 号 集 群 名 称"）并进名单那一块
    spaced = run([*NOTICE, "附件", "序 号 集 群 名 称", *TABLE[1:], *SIGNATURE])
    assert spaced.text is not None
    assert spaced.text.split("\n") == [*NOTICE, body.PLACEHOLDER, *SIGNATURE]


def test_aligned_rows_without_parsed_names_are_hidden() -> None:
    # 没解析出条目名称、名称也不像单位名的表格（格子之间只剩空格）：连着 4 行段数一样的算名单
    # （没有序号；表格前面的短标题"获奖名单"并进这一块）
    rows = [f"第{i}创客团队 创业组 芜湖市" for i in range(1, 5)]
    out = run([*NOTICE, "获奖名单", *rows, *SIGNATURE], names=set())
    assert out.text is not None and out.hidden is True
    assert out.text.split("\n") == [*NOTICE, body.PLACEHOLDER, *SIGNATURE]
    assert run([*NOTICE, *rows[:3], *SIGNATURE], names=set()).hidden is False
    # 通知里的话不会连着几行段数一样：不到 4 行、段数不一样的照常显示
    loose = ["一 创业组 芜湖市", "二 成长组 铜陵市 宣城市", "三 团队组"]
    assert run([*NOTICE, *loose], names=set()).hidden is False


def test_list_not_found_but_names_remain_is_not_shown() -> None:
    # 名单写成一句话（带标点、很长），没认出来：留下的文字里还有好几家名单里的单位，宁可不给
    long_line = (
        "经评审，拟认定"
        + "、".join(sorted(NAMES))
        + "等企业为专精特新中小企业，"
        + "特别说明" * 20
        + "。"
    )
    out = run(NOTICE + [long_line] + SIGNATURE)
    assert out.text is None and out.reason == "mixed"


def test_names_on_contact_and_editor_lines_are_masked_when_shown() -> None:
    text = (
        "\n".join(NOTICE[:3])
        + "\n（联系人：（联系人见原文）产业政策与法规处 柯于灿；\n联系电话：83607736）"
    )
    shown = body_for_reading(text * 3, "申报通知")
    assert shown["status"] == "ok" and "柯于灿" not in shown["text"]
    assert "产业政策与法规处 （联系人见原文）" in shown["text"]
    cases = {
        "刘老师，（电话见原文）": "（联系人见原文），（电话见原文）",
        "联系人：高新处张三，电话：（电话见原文）": "联系人：高新处（联系人见原文），电话：（电话见原文）",
        "联系人：张三和李四": "联系人：（联系人见原文）和（联系人见原文）",
        "联系人：张三/李四": "联系人：（联系人见原文）/（联系人见原文）",
        "联 系 人：王五": "联 系 人：（联系人见原文）",
        # 网站编审署名：换成"（署名已隐去）"，不冒充联系人
        "二审:程婷婷、韩波": "二审:（署名已隐去）、（署名已隐去）",
        "来源：某网 责编：张三": "来源：某网 责编：（署名已隐去）",
    }
    for line, expected in cases.items():
        assert _mask_contact_names(line) == expected, line
    assert _mask_contact_names("咨询电话：0531-12345678（王老师）").endswith("（联系人见原文）")
    # 写着电话号码的行像姓名的都遮（电话前后、括号里）；"联系电话：张三"；"高新区张三"；字间带空格的
    for line, expected in {
        "吾慧星、张竞博，电话：（电话见原文）": "（联系人见原文）、（联系人见原文），电话：（电话见原文）",
        "联系电话：88888888（李四）": "联系电话：88888888（联系人见原文）",
        "咨询电话：（电话见原文）（张伟）": "咨询电话：（电话见原文）（联系人见原文）",
        "电话：张三（0531-12345678）": "电话：（联系人见原文）（0531-12345678）",
        "联系电话：张三": "联系电话：（联系人见原文）",
        "联系人：高新区张三": "联系人：高新区（联系人见原文）",
        "孟 双、王 璐 联系电话：（电话见原文）": "（联系人见原文）、（联系人见原文） 联系电话：（电话见原文）",
        "联系人：张 三": "联系人：（联系人见原文）",
    }.items():
        assert _mask_contact_names(line) == expected, line
    # 标签写在上一行末尾：下一行的姓名也遮（隔着空行也遮）；下面接着一行一个"姓名 电话"的，一直遮到
    # 不是为止；下一行是表格模板的栏目（"推荐单位（盖章）："）的不遮
    assert _mask_contact_names("联系人：\n张三") == "联系人：\n（联系人见原文）"
    assert _mask_contact_names("联系人：\n\n张三") == "联系人：\n\n（联系人见原文）"
    form = "联系人及电话：\n推荐单位（盖章）：\n填报日期："
    assert _mask_contact_names(form) == form
    rows = _mask_contact_names("联系人：\n张三 88881111\n李四 （电话见原文）\n王五同志负责解释。")
    assert rows == (
        "联系人：\n（联系人见原文） 88881111\n（联系人见原文） （电话见原文）\n王五同志负责解释。"
    )
    # 普通的话、时间、单位名不动
    for line in (
        "如对公示内容有异议，请于公示期内以书面或电话形式向我厅（中小企业处）反映。",
        "【打印】 【关闭】",
        "服务时间：周一 至 周五 （电话见原文）",
        "联系电话：88888888（周一至周五）",
        "电话：0531-12345678（办公室）",
        "联系电话：88888888（高新处）",
        "项目编号 20260001 资金 12000000元",
        "序号 企业名称 成立 原值 联系电话",
        "联系人：防汛指挥部成立",
        "推荐单位：（盖章） 联系人/联系电话：",
        "联系电话：高新区",
        "联系人：科技和信息化局",
    ):
        assert _mask_contact_names(line) == line, line


@pytest.mark.parametrize(
    "title, readable",
    [
        ("关于2026年第二批专精特新中小企业名单的公示", True),
        ("2026年度高层次人才名单公示", False),
        ("2026年严重失信企业名单", False),
        ("经专家评审拟认定的高新技术企业名单公示", True),
    ],
)
def test_which_list_notices_can_be_read(title, readable) -> None:
    assert body.readable(title) is readable


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(v1, "now", lambda: NOW)
    download_api._times.clear()
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    download_api._times.clear()


def _notice(session, title, lines, names=NAMES):
    source = Source(
        level="省级",
        province="山东",
        dept="工信",
        dept_full_name="山东省工业和信息化厅",
        column_name="通知公告",
        url=f"https://gxt.shandong.gov.cn/{abs(hash(title)) % 10**6}/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        url=f"https://gxt.shandong.gov.cn/{abs(hash(title)) % 10**8}.html",
        url_key=f"gxt.shandong.gov.cn/{abs(hash(title)) % 10**8}.html",
        title=title,
        kind_override="名单",
        publish_date=date(2026, 10, 20),
        first_seen_at=NOW,
        last_seen_at=NOW,
        is_baseline=False,
        status="fetched",
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text="\n".join(lines),
            issuer="山东省工业和信息化厅",
            fetched_at=NOW,
            status="ok",
        )
    )
    session.add(
        ListParse(
            document_id=doc.id,
            source_url=doc.url,
            source_kind="detail",
            status="ok",
            entry_count=len(names),
            attempted_at=NOW,
        )
    )
    for name in names:
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=doc.url,
                name=name,
                normalized_name=name,
                category="专精特新",
            )
        )
    session.commit()
    return doc


def test_reading_and_detail_show_the_notice_but_not_the_list(client, session) -> None:
    doc = _notice(session, "关于2026年第二批专精特新中小企业名单的公示", NOTICE + TABLE + SIGNATURE)
    reading = client.get(f"/api/v1/documents/{doc.id}/reading").json()
    assert reading["status"] == "ok"
    text = "\n".join(s["text"] for s in reading["segments"])
    assert "公示时间：2026年10月20日至10月26日。" in text and body.PLACEHOLDER in text
    assert not any(name in text for name in NAMES)
    assert "名单内容站内不展开" in reading["notice"] and reading["listHidden"] is True
    detail = client.get(f"/api/v1/documents/{doc.id}").json()["body"]
    assert (
        detail["status"] == "ok" and "公示时间" in detail["text"] and detail["listHidden"] is True
    )
    assert not any(name in detail["text"] for name in NAMES)
    # 名单在附件里、正文里没有名单：照常读，不说"名单不展开"、不带 listHidden
    plain = _notice(session, "关于2026年第三批专精特新中小企业名单的公示", NOTICE + SIGNATURE)
    reading = client.get(f"/api/v1/documents/{plain.id}/reading").json()
    assert reading["status"] == "ok" and "listHidden" not in reading
    assert "名单内容站内不展开" not in reading["notice"]
    assert "listHidden" not in client.get(f"/api/v1/documents/{plain.id}").json()["body"]


def test_list_dominated_and_person_list_bodies_are_not_shown(client, session) -> None:
    # 几乎全是名单：给查名单里的企业查询结果
    mostly = _notice(session, "关于公布2026年专精特新中小企业名单的通知", TABLE * 20 + SIGNATURE)
    reading = client.get(f"/api/v1/documents/{mostly.id}/reading").json()
    assert reading["status"] == "list" and reading["segments"] == []
    assert client.get(f"/api/v1/documents/{mostly.id}").json()["body"]["status"] == "list"
    assert "正文主要是名单" in reading["notice"]
    # 名单没能和通知文字分开（留下的文字里还有好几家名单里的单位）：说清楚是没分开
    mixed_line = "经评审，拟认定" + "、".join(sorted(NAMES)) + "等企业，" + "特别说明" * 20 + "。"
    mixed = _notice(session, "关于2026年专精特新中小企业名单的公告", NOTICE + [mixed_line])
    reading = client.get(f"/api/v1/documents/{mixed.id}/reading").json()
    assert reading["status"] == "list" and "没能和通知文字分开" in reading["notice"]
    assert client.get(f"/api/v1/documents/{mixed.id}").json()["body"]["status"] == "list"
    # 正文本来就很短（只抓到网页导航）：和别的类一样按没抓全说，不说"主要是名单"
    short = _notice(session, "关于2026年度创新型中小企业名单的公示", ["首页 政务公开 搜索"])
    reading = client.get(f"/api/v1/documents/{short.id}/reading").json()
    assert reading["status"] == "empty" and "名单" not in reading["notice"]
    assert client.get(f"/api/v1/documents/{short.id}").json()["body"] == {"status": "none"}
    # 人才、专家这类个人名单不进查名单：两处都按"不展示"说，不引导去查名单
    people = _notice(
        session, "2026年度高层次人才名单公示", NOTICE + ["张三 山东大学 教授"] + SIGNATURE
    )
    reading = client.get(f"/api/v1/documents/{people.id}/reading").json()
    assert reading["status"] == "unsupported" and reading["segments"] == []
    assert client.get(f"/api/v1/documents/{people.id}").json()["body"] == {"status": "original"}
