"""全国覆盖公开文件里的样例标题不写个人姓名（栏目清单"备注"、抽查证据"最新1–3"）。"""

import pytest

from youju.govsites.privacy import MASK, public_title


@pytest.mark.parametrize(
    "title",
    [
        # 个人事项（D-097 ② 的词）
        "关于某某劳动能力初次鉴定结论书的 公告送达",
        "乐清市人民政府关于马欣、南林扶同志任免职务的通知",
        "嘉兴市南湖区人民政府关于杨阳等职务任免的通知",
        "省政府关于同意追认陆贤智同志为烈士的批复",
        "关于聘任张婧为技师（文秘资料员）的通知",
        "泰顺县人民政府关于给予蔡齐志等4名见义勇为人员记个人三等功的决定",
        # 不在个人事项词里、但写着人名
        "白马镇宅基地建房规划许可内容公布（杨继法、张东鑫等15户）",
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
    ],
)
def test_other_titles_are_kept(title):
    assert public_title(title) == title


def test_column_rows_mask_sample_titles():
    from types import SimpleNamespace

    from youju.govsites.report import column_rows

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
        samples=[
            "关于张某某劳动能力鉴定结论书的公告送达",
            "关于开展2026年度技能人才评价工作的通知",
        ],
        kind="通知公告",
        site_url="https://rst.shanxi.gov.cn/",
        nav_source="",
        column_source="",
        platform="",
    )
    import youju.govsites.report as report

    real = report.import_status
    report.import_status = lambda c: "试跑可读（全国覆盖 v1，未启用）"
    try:
        [row] = column_rows([check], "2026-11-11")
    finally:
        report.import_status = real
    assert row[13] == f"{MASK}；关于开展2026年度技能人才评价工作的通知"
