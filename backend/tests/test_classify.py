import pytest
from sqlalchemy import select

from youju.classify import (
    dept_group,
    doc_kind,
    kind_expr,
    list_status,
    region_label,
    status_filter,
)
from youju.models import Document, Source

CASES = [
    ("山东省2026年第4批拟入库科技型中小企业名单公示", "名单", "公示"),
    ("关于公布黑龙江省第二批省级工业设计研究院培育对象名单的通知", "名单", "公布"),
    ("关于2026年度黑龙江省自然科学基金拟资助项目的公示", "名单", "公示"),
    ("关于2026年第4批入库科技型中小企业名单的公告", "名单", None),
    ("2026年度京津冀自然科学基金合作专项拟资助项目公告", "名单", None),
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
    # 标题不写"名单"的名单
    ("关于上海市2026年第五批入库科技型中小企业的公告", "名单", None),
    ("国家发展改革委体制改革综合司关于研究课题入选单位的公告", "名单", None),
    (
        "自治区工业和信息化厅、财政厅关于公布2026年度自治区“专精特新”中小企业认定和复核结果"
        "（第二批）的通知",
        "名单",
        "公布",
    ),
    # 组织认定和复核工作的仍是申报通知
    ("关于组织开展2026年度专精特新中小企业认定和复核工作的通知", "申报通知", None),
    ("2026年第三批省科技攻关计划项目结项验收结果公示", "名单", "公示"),
    # 讲的是产能但列的是企业，仍算名单
    ("关于2026年度按照330天和270天计算产能的水泥熟料生产线和所属企业名单的公示", "名单", "公示"),
    # 与企业申报无关的公示：不算名单、不标状态
    ("关于山西省工业和信息化厅所属事业单位2026年度公开招聘拟聘用人员的公示", "其他", None),
    (
        "科学技术部国际科技合作中心关于公布2026年度公开招聘应届毕业生补充面试人员名单的通知",
        "其他",
        None,
    ),
    ("关于2025年度工程系列科技管理专业高级职称评审结果的公示", "其他", None),
    ("山东钢铁集团永锋临港有限公司新建钢铁项目产能置换方案公示", "其他", None),
    ("2026年度电力行业“双随机、一公开”执法检查结果公示（第三批）", "其他", None),
    ("贵州省工业和信息化厅关于《厅机关办公打印机设备定点维修服商》比选结果公示", "其他", None),
    ("河南省第三轮中央生态环境保护督察第15项整改任务销号公示", "其他", None),
    (
        "贵州省工业和信息化厅关于公布2026年贵州省第三期业余无线电台操作技术能力（A类）"
        "考试合格人员名单的通知",
        "其他",
        None,
    ),
    ("市科技局关于天津市科技专家库2026年第四批拟入库专家的公示", "其他", None),
    ("高新技术领域“赛马制”项目评审专家公示", "其他", None),
    ("关于对申报2026年专业技术资格人员进行公示的公告", "其他", None),
    ("重庆市经济和信息化委员会行政处罚公示", "其他", None),
    (
        "贵州省工业和信息化厅关于对符合2026年第三期业余无线电台操作技术能力（A类） 考试资格审核结果的公示",
        "其他",
        None,
    ),
    # 列机构的考试名单仍是名单
    ("关于公布2026年度社会化考试机构名单的通知", "名单", "公布"),
    ("关于企业证照办理优化措施公开及办理意见建议的公示", "政策", None),
    # 名单正文摘录里带"意见建议"的，写明名单仍是名单
    ("关于2026年度拟认定企业名单的公示 公示期间如有意见建议请反映", "名单", "公示"),
    # 征求意见：不算名单，与其他征求意见稿一样按政策
    (
        "广东省科学技术厅关于核定享受“十五五”期间支持科技创新进口税收政策的研发机构名单"
        "有关实施办法（征求意见稿）公开征求意见的公告",
        "政策",
        None,
    ),
    ("关于《科技创新项目管理办法》公开征求社会意见的公示", "政策", None),
    # 写明名单的征求意见仍是名单
    ("关于2026年度拟资助项目名单征求意见的公示", "名单", "公示"),
    # 没有名单字样的职称申报通知不受影响
    ("关于开展2026年度湖北省电子信息工程专业职称评审申报工作的通知", "申报通知", None),
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
    # 名单状态筛选：数据库里筛出来的与 Python 判断一致，正反例都在 CASES 里
    for status in ("公示", "公布"):
        where = status_filter(Document.title, [status])
        matched = set(session.scalars(select(Document.title).where(where)))
        expected = {title for title, _, _ in CASES if list_status(title) == status}
        assert expected and matched == expected, status
