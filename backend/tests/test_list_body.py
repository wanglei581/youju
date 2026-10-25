"""名单类通知正文在站内可读、正文里的名单不展开（D-092，youju/lists/body.py）。"""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from youju.api import attachments as download_api
from youju.api import v1
from youju.api.app import app
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


def test_list_lines_and_notice_lines() -> None:
    def hidden(line):
        return body.is_list_line(line, NAMES, KEEP)

    # 通知里的话（有句子标点）照常显示
    assert not any(hidden(line) for line in NOTICE)
    # 表格行、表头不展开
    assert all(hidden(line) for line in TABLE)
    # 一段正好是条目名称的行（顿号连着几家）不展开
    assert hidden("山东星河科技有限公司、青岛海纳智能有限公司")
    # 序号开头、后面还有别的段的；只有一个单位名的；像人名的
    assert hidden("4 某某科技有限公司")
    assert hidden("某某科技有限公司")
    assert hidden("张三")
    # 署名、日期、"特此公告"、标题这类照常显示
    assert not hidden("山东省工业和信息化厅")
    assert not hidden("2026年10月20日")
    assert not hidden("特此公告")
    assert not hidden("关于2026年第二批专精特新中小企业名单的公示")
    assert not hidden("一、认定条件")


def test_filter_text_replaces_the_list_block_and_keeps_the_notice() -> None:
    text = "\n".join(NOTICE + TABLE + SIGNATURE)
    out = body.filter_text(text, names=NAMES, keep=KEEP, min_chars=50)
    assert out is not None
    assert "公示时间：2026年10月20日至10月26日。" in out
    assert "山东省工业和信息化厅" in out.split("\n") and "2026年10月20日" in out.split("\n")
    assert out.count(body.PLACEHOLDER) == 1
    for name in NAMES:
        assert name not in out
    # 几乎全是名单：剩下的通知文字太少，整篇按名单处理
    assert (
        body.filter_text("\n".join(TABLE + SIGNATURE[:1]), names=NAMES, keep=KEEP, min_chars=50)
        is None
    )


def test_one_cell_per_line_tables_are_hidden_as_one_block() -> None:
    # 格子里套了段落：一个格子一行，长名称被折成两行；序号、类别、半个名称都在这一块里
    cells = ["序号", "企业名称", "领域"]
    for i, name in enumerate(sorted(NAMES), 1):
        cells += [str(i), name, "高端装备制造"]
    cells += ["4", "某某（天津）网络安全科技有限责任公", "司", "新材料"]
    out = body.filter_text(
        "\n".join(NOTICE + cells + SIGNATURE), names=NAMES, keep=KEEP, min_chars=50
    )
    assert out is not None and out.count(body.PLACEHOLDER) == 1
    for gone in ("高端装备制造", "新材料", "有限责任公", "序号"):
        assert gone not in out
    assert out.split("\n")[-2:] == SIGNATURE


def test_rows_with_thousands_separators_and_unparsed_cell_tables() -> None:
    # 序号写成"4,300"：逗号后面是数字，不算句子标点
    assert body.is_list_line("4,300 某某环保科技有限公司 2026360111A8011849", set(), set())
    # 没解析出条目、名称也不是常见单位名的一格一行表格：靠逐个递增的序号认出来，表头格子一起挡
    cells = ["2025年度顶尖孵化器绩效评价结果", "序号", "顶尖孵化器名称", "属地", "评价等次"]
    for i, (name, city, grade) in enumerate(
        [("甲软件园人工智能顶尖孵化器", "甲市", "优秀"), ("乙科学中心顶尖孵化器", "乙市", "良好")],
        1,
    ):
        cells += [str(i), name, city, grade]
    out = body.filter_text("\n".join(NOTICE + cells), names=set(), keep=KEEP, min_chars=50)
    assert out is not None
    assert out.split("\n")[-2:] == ["2025年度顶尖孵化器绩效评价结果", body.PLACEHOLDER]
    for gone in ("孵化器名称", "甲软件园", "优秀", "乙市"):
        assert gone not in out


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
    out = body.filter_text("\n".join(lines), names=NAMES, keep=KEEP, min_chars=50)
    assert out is not None
    # 通知文字、署名、日期都在；紧挨在表格前面的"附件""名单"短标签并进了名单那一块
    assert out.split("\n") == [*lines[:8], body.PLACEHOLDER]


def test_names_on_contact_lines_are_masked_when_shown() -> None:
    from youju.api.v1 import body_for_reading

    text = (
        "\n".join(NOTICE[:3])
        + "\n（联系人：（联系人见原文）产业政策与法规处 柯于灿；\n联系电话：83607736）"
    )
    shown = body_for_reading(text * 3, "申报通知")
    assert shown["status"] == "ok" and "柯于灿" not in shown["text"]
    assert "产业政策与法规处 （联系人见原文）" in shown["text"]
    # 没写"联系人"的联系方式、网站页脚的编审署名也遮；普通的话不动
    from youju.api.v1 import _mask_contact_names

    assert _mask_contact_names("刘老师，（电话见原文）") == "（联系人见原文），（电话见原文）"
    assert _mask_contact_names("咨询电话：0531-12345678（王老师）").endswith("（联系人见原文）")
    assert _mask_contact_names("二审:程婷婷、韩波") == "二审:（联系人见原文）、（联系人见原文）"
    prose = "如对公示内容有异议，请于公示期内以书面或电话形式向我厅（中小企业处）反映。"
    assert _mask_contact_names(prose) == prose
    assert _mask_contact_names("【打印】 【关闭】") == "【打印】 【关闭】"


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
    assert detail["status"] == "ok" and "公示时间" in detail["text"]
    assert not any(name in detail["text"] for name in NAMES)


def test_list_dominated_and_person_list_bodies_are_not_shown(client, session) -> None:
    # 几乎全是名单：给查名单里的企业查询结果
    mostly = _notice(session, "关于公布2026年专精特新中小企业名单的通知", TABLE * 20 + SIGNATURE)
    reading = client.get(f"/api/v1/documents/{mostly.id}/reading").json()
    assert reading["status"] == "list" and reading["segments"] == []
    assert client.get(f"/api/v1/documents/{mostly.id}").json()["body"]["status"] == "list"
    # 人才、专家这类个人名单不进查名单，正文也不展示
    people = _notice(
        session, "2026年度高层次人才名单公示", NOTICE + ["张三 山东大学 教授"] + SIGNATURE
    )
    reading = client.get(f"/api/v1/documents/{people.id}/reading").json()
    assert reading["status"] == "unsupported" and reading["segments"] == []
    assert client.get(f"/api/v1/documents/{people.id}").json()["body"]["status"] == "list"
