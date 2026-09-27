from datetime import date
from types import SimpleNamespace

from youju.dedup import _split_by_content, match_key


def _doc(title: str) -> SimpleNamespace:
    return SimpleNamespace(title=title, publish_date=date(2026, 5, 21))


SOURCE = SimpleNamespace(dept_full_name="青海省科学技术厅")


def test_match_key_uses_title_issuer_and_date() -> None:
    a = match_key(_doc("关于公布 2026年专精特新中小企业名单的通知"), None, SOURCE)
    b = match_key(_doc("关于公布2026年专精特新中小企业名单的通知"), None, SOURCE)
    assert a == b and a is not None
    other = SimpleNamespace(dept_full_name="山东省工业和信息化厅")
    assert match_key(_doc("关于公布2026年专精特新中小企业名单的通知"), None, other) != a


def test_truncated_titles_are_not_merged() -> None:
    """列表页截断的标题，不同文件可能截成一样（如"关于印发《青海省..."），不参与归并。"""
    assert match_key(_doc("中共青海省科学技术厅党组关于印发《青海省..."), None, SOURCE) is None
    assert match_key(_doc("关于印发《青海省科技计划…"), None, SOURCE) is None


def test_same_title_but_different_number_or_body_is_not_merged() -> None:
    body_a = "各有关单位：" + "现将2026年度项目申报有关事项通知如下。" * 10
    body_b = "各有关单位：" + "现将2026年度项目验收有关事项通知如下。" * 10
    groups = _split_by_content(
        [
            (1, ("青科发〔2026〕12号", body_a[:200])),
            (2, ("青科发〔2026〕13号", None)),  # 文号不同：另成一组
            (3, (None, body_b[:200])),  # 正文与第一组不同；与第 2 条没有冲突的信息，并入第 2 组
            (4, (None, None)),  # 取不到文号和正文：跟第一组
            (5, ("青科发〔2026〕12号", None)),  # 同一文号
        ]
    )
    assert groups == [[1, 4, 5], [2, 3]]
