"""全国覆盖公开文件里不写个人姓名：栏目清单"备注"不写样例标题，抽查证据"最新1–3"挡掉个人事项。

用例里的人名都是编的（张三、李四……），句式照公开文件里见过的写法。
"""

import pytest

from youju.govsites.privacy import MASK, public_title


@pytest.mark.parametrize(
    "title",
    [
        # 个人事项（D-097 ② 的词，加劳动能力、送达、宅基地、见义勇为、遗失作废、辞去）
        "关于张三劳动能力初次鉴定结论书的 公告送达",
        "某市人民政府关于李四、王五同志任免职务的通知",
        "某区人民政府关于赵六等职务任免的通知",
        "省政府关于同意追认孙七同志为烈士的批复",
        "某县人民政府关于给予周八等4名见义勇为人员记个人三等功的决定",
        "某镇宅基地建房规划许可内容公布（张三、李四等15户）",
        "不动产权证作废公告（王五、赵六）",
        "某区某路1号101室钱七不动产权证书遗失声明",
        "某省人民代表大会常务委员会关于接受孙七辞去某省人民代表大会常务委员会委员职务的决定",
        # 不在词表里、只靠人名写法认出来的
        "关于聘任张三为技师（文秘资料员）的通知",
        "关于张三、李四、王五、赵六的安置决定",
        "关于对张三的征地补偿安置决定",
        "关于胡一、张二、赵三、赵四《责令公开申请书》的答复书",
        "徐一、徐二、徐三批前公示",
        "吴四批前公示",
        "鲍一、吴二批前公示",
        "·徐一、徐二批前公示",
        "关于张三代表《关于优化政府招商引资工作的建议》的答复",
        "关于调整党政领导接访日安排的公告 - 李四",
    ],
)
def test_personal_titles_are_masked(title):
    assert public_title(title) == MASK


@pytest.mark.parametrize(
    "title",
    [
        "关于2026年浙江省服务业领军企业名单的公示",
        "关于开展2026年度省级专精特新中小企业认定工作的通知",
        "市场监管局等5部门关于支持人工智能创新发展的通知",
        "2026年浙江省政府一般债券（三期）信息披露文件",
        # 顿号连着的品类、企业类型不是人名
        "关于调整车用汽、柴油价格的通知",
        "关于开展成长型、龙头型、创新型企业培育工作的通知",
        "秦淮河、石臼湖、固城湖水环境综合治理方案",
        "某地块项目规划批前公示",
        "某项目建设工程许可批前公示",
        # 征地补偿标准、办法是普通政策
        "关于印发某市征地补偿标准的通知",
        "关于调整征收补偿办法的通知",
        "关于人大代表建议办理情况的报告",
    ],
)
def test_other_titles_are_kept(title):
    assert public_title(title) == title


def test_column_rows_write_no_sample_titles():
    """栏目清单的"备注"不写样例标题：导入来源时备注还会写进来源备注。"""
    from types import SimpleNamespace

    import youju.govsites.report as report

    check = SimpleNamespace(
        level="省级",
        province="山西",
        city="",
        district="",
        code="140000",
        dept="人社",
        dept_full_name="山西省人力资源和社会保障厅",
        name="通知公告",
        url="https://rst.shanxi.gov.cn/tzgg/",
        http_status=200,
        dated=20,
        latest=None,
        samples=["关于开展2026年度技能人才评价工作的通知", "关于张三劳动能力鉴定结论书的公告送达"],
        kind="通知公告",
        site_url="https://rst.shanxi.gov.cn/",
        nav_source="",
        column_source="",
        platform="",
    )
    real = report.import_status
    report.import_status = lambda c: "试跑可读（全国覆盖 v1，未启用）"
    try:
        [row] = report.column_rows([check], "2026-11-11")
    finally:
        report.import_status = real
    assert row[report.COLUMN_FIELDS.index("备注")] == ""
