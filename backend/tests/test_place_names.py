"""发文机关校验用的地名名单和全国行政区划清单一致（区划清单更新了要重新生成）。"""

from youju.govsites.placenames import (
    BODIES_OUTPUT,
    OUTPUT,
    agency_bodies,
    place_names,
    render,
    render_bodies,
    short_form,
)


def test_name_lists_match_docs() -> None:
    """地名名单和区划清单一致，机关名称词表和部门栏目清单、手工补充一致（清单更新了要重新生成）。"""
    names = place_names()
    assert OUTPUT.read_text(encoding="utf-8") == render(names)
    bodies = agency_bodies(places=names)
    assert BODIES_OUTPUT.read_text(encoding="utf-8") == render_bodies(bodies)
    assert {"人民政府", "财政局", "发展和改革委员会", "经济技术开发区管理委员会"} <= set(bodies)


def test_short_forms_of_autonomous_areas() -> None:
    assert short_form("凉山彝族自治州") == "凉山州"
    assert short_form("黔东南苗族侗族自治州") == "黔东南州"
    assert short_form("三江侗族自治县") == "三江县"
    assert short_form("东乡族自治县") is None  # 去掉民族名就没有地名了
    assert short_form("攀枝花市") is None
