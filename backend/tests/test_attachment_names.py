"""附件里的联系人姓名，只在显示时遮（11/2 F13）。

认姓名和正文同一套（常见姓氏开头的 2–3 个字、带"老师""处长"这类称呼的），但只看联系人、电话这类
标签后面同一句里的、紧挨在号码前面的、列名是"联系人""姓名"的格子和只写着"联系人"那一格的下一格：
附件里的表头、长表格行、长句子整行套正文的规则会误遮"万元""申请日""安装"这类词（本机库全量对比）。"""

import pytest

from tests.test_readers import client, path, seed  # noqa: F401 - 夹具和造数据的函数
from youju.api import v1
from youju.models import AttachmentTextSegment

NAME = "（这里隐去了联系人）"
SIGN = "（这里隐去了署名）"
PHONE = "（这里隐去了电话）"


@pytest.mark.parametrize(
    "text, expected",
    [
        # 标签后面同一句里的姓名（含逗号隔开的几位、带称呼的、单位后面的）
        ("联系人：吾慧星，电话：[电话已脱敏]", f"联系人：{NAME}，电话：{PHONE}"),
        (
            "联系人：四川省技术转移中心，樊老师，李老师",
            f"联系人：四川省技术转移中心，{NAME}，{NAME}",
        ),
        (
            "联系人：省科技厅高新处张三；联系电话：[电话已脱敏]",
            f"联系人：省科技厅高新处{NAME}；联系电话：{PHONE}",
        ),
        # 紧挨在号码前面的
        ("沈彬 [电话已脱敏]", f"{NAME} {PHONE}"),
        (
            "新一代电子信息制造业方向：任老师，[电话已脱敏]；",
            f"新一代电子信息制造业方向：{NAME}，{PHONE}；",
        ),
        ("刘 文 [电话已脱敏]", f"{NAME} {PHONE}"),
        # "联系人："写在行尾，下一行有号码
        ("联系人：\n刘菊芳 [电话已脱敏]", f"联系人：\n{NAME} {PHONE}"),
        # 网站编审署名
        ("一审：张三 二审：李四", f"一审：{SIGN} 二审：{SIGN}"),
    ],
)
def test_names_in_attachment_text(text, expected):
    assert v1.display_attachment_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # 本机库全量对比里整行套正文规则会误遮的
        "熟知相关部门负责人、安全员、班组长等联系方式。",
        "（3）请求给予非自愿许可的布图设计的名称、登记号、申请日、颁证日，以及权利人的姓名或者名称；",
        "联系方式：\n科技金融服务处（南京市龙蟠路 175 号 504 房间）",
        "构建汽缸垫多物理场合模型，模拟高温、高压、强腐蚀等工况下的力学性能",
        "须完成上述五个工序自动化成套设备的开发、制造、安装、调试及产线集成联调",
        "请各单位确定一名联系人，负责申报工作的组织协调。",
        # 单位名、职务
        "联系人：省科技厅高新处",
        "联系人及职务：处长",
    ],
)
def test_attachment_text_without_names_is_left_alone(text):
    assert v1.display_attachment_text(text) == v1.display_text(text)


def test_attachment_table_rows():
    row = v1.display_attachment_row
    # 列名是"联系人""姓名"的格子
    assert row([("地区", "石家庄市"), ("联系人", "张诚"), ("联系电话", "[电话已脱敏]")]) == [
        "石家庄市",
        NAME,
        PHONE,
    ]
    assert row(
        [("序号", "示例"), ("姓名", "张三"), ("工作单位", "***研究院"), ("职称", "研究员")]
    ) == [
        "示例",
        NAME,
        "***研究院",
        "研究员",
    ]
    # 只写着"联系人"那一格的下一格
    assert row([("", "联系人："), ("", "王五"), ("", "电话："), ("", "[电话已脱敏]")]) == [
        "联系人：",
        NAME,
        "电话：",
        PHONE,
    ]
    # 下一格只有号码：整格像姓名才遮，单位名不遮
    assert row([("", "信推处"), ("", "徐丽萍"), ("", "[电话已脱敏]")]) == ["信推处", NAME, PHONE]
    # 格子里自己写着标签
    assert row([("", "联系方式：卢俊业[电话已脱敏]")]) == [f"联系方式：{NAME}{PHONE}"]


@pytest.mark.parametrize(
    "cells",
    [
        # 表头：不遮（"总经费（万元）"在"负责人"后面）
        [("", "课题名称"), ("", "承担单位"), ("", "负责人"), ("", "总经费\n（万元）")],
        [("", "经费投入计划（万元）"), ("", "是否同意公开需求信息"), ("", "联系人\n（必填）")],
        [("", "联系人"), ("", "职务"), ("", "手机")],
        # 列名是"负责人"，内容不是姓名
        [("团队负责人", "团队成员")],
        [("申报联系人", "营业收入（万元）")],
        # 列名是"姓名"这类、内容是一大段话：只按标签判断
        [("负责人", "本人同意并授权大赛主办方对参赛内容中所包含（但不限于）所有文本、图片资料等")],
    ],
)
def test_table_rows_without_names_are_left_alone(cells):
    assert v1.display_attachment_row(cells) == [v1.display_text(v) for _, v in cells]


def test_attachment_reading_page_masks_names(client, session):  # noqa: F811
    doc, att, _, version = seed(session)
    rows = {
        row.n: row
        for row in session.query(AttachmentTextSegment).filter_by(version_id=version.id)
        if row.n in (1, 2)
    }
    rows[1].text = "联系人：李四，电话：[电话已脱敏]"
    rows[2].kind = "row"
    rows[2].text = "联系人 | 张诚 | [电话已脱敏]"
    rows[2].cells = [
        {"c": 0, "t": "石家庄市", "h": "地区"},
        {"c": 1, "t": "张诚", "h": "联系人"},
        {"c": 2, "t": "[电话已脱敏]", "h": "联系电话"},
    ]
    session.commit()
    data = client.get(path(doc, att)).json()
    by_n = {s["n"]: s for s in data["segments"]}
    assert by_n[1]["text"] == f"联系人：{NAME}，电话：{PHONE}"
    assert [c["t"] for c in by_n[2]["cells"]] == ["石家庄市", NAME, PHONE]
    assert [c["h"] for c in by_n[2]["cells"]] == ["地区", "联系人", "联系电话"]
    assert "李四" not in str(data) and "张诚" not in str(data)
    # 库里存的文字没动
    session.expire_all()
    stored = session.query(AttachmentTextSegment).filter_by(version_id=version.id, n=2).one()
    assert stored.cells[1]["t"] == "张诚"
