import pytest
from sqlalchemy import select

from youju.classify import dept_group, doc_kind, kind_expr, list_status, region_label
from youju.models import Document, Source

CASES = [
    ("山东省2026年第4批拟入库科技型中小企业名单公示", "名单", "公示"),
    ("关于公布黑龙江省第二批省级工业设计研究院培育对象名单的通知", "名单", "正式公布"),
    ("关于2026年度黑龙江省自然科学基金拟资助项目的公示", "名单", "公示"),
    ("关于2026年第4批入库科技型中小企业名单的公告", "名单", "正式公布"),
    # 标题没写明公示或公布：不标状态（不从"拟"等字推断）
    ("关于拟认定2026年度省级企业技术中心名单的通知", "名单", None),
    (
        "关于组织开展全区2026年度享受增值税加计抵减政策的先进制造业企业名单制定工作有关事项的通知",
        "申报通知",
        None,
    ),
    ("省科技厅关于组织申报国家重点研发计划重点专项2026年度项目的通知", "申报通知", None),
    ("福建省科学技术厅关于继续征集第四期省自然科学基金联合资助单位的通知", "申报通知", None),
    (
        "江苏省科学技术厅 江苏省财政厅关于印发《江苏省前沿技术研发计划项目管理办法（试行）》的通知",
        "政策",
        None,
    ),
    ("专利文献馆2026年8-9月公益讲座计划：“《专利优先审查管理办法》修改解读” 专题", "其他", None),
    ("2026年1—8月份全国固定资产投资基本情况", "其他", None),
    ("关于北京市2026年第四批高新技术企业更名的通知", "名单", None),
]


@pytest.mark.parametrize(("title", "kind", "status"), CASES)
def test_doc_kind_and_status(title, kind, status):
    assert doc_kind(title) == kind
    assert list_status(title) == status


def test_dept_group_and_region_label():
    assert dept_group("科技") == "科技部门"
    assert dept_group("工信") == "工信部门"
    assert dept_group("经信") == "工信部门"
    assert dept_group("发改委") == "其他部门"
    assert region_label("国家", "国家级") == "国家"
    assert region_label("辽宁", "省级") == "辽宁 · 省级"


def test_sql_expression_matches_python(session):
    """数据库里的分类（筛选、分页用）必须与 Python 规则完全一致。"""
    src = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知",
        url="https://kjt.shandong.gov.cn/x/",
        enabled=True,
        parser_config={},
    )
    session.add(src)
    session.flush()
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for i, (title, _, _) in enumerate(CASES):
        session.add(
            Document(
                source_id=src.id,
                url=f"https://kjt.shandong.gov.cn/{i}.html",
                url_key=f"kjt.shandong.gov.cn/{i}.html",
                title=title,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    session.flush()
    rows = session.execute(select(Document.title, kind_expr(Document.title))).all()
    assert len(rows) == len(CASES)
    for title, sql_kind in rows:
        assert sql_kind == doc_kind(title), title
