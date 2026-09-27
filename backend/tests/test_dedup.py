from datetime import date
from types import SimpleNamespace

from youju.dedup import match_key


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
