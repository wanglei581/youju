"""发文机关校验用的地名名单和全国行政区划清单一致（区划清单更新了要重新生成）。"""

from youju.govsites.placenames import OUTPUT, place_names, render, short_form


def test_place_names_file_matches_divisions() -> None:
    assert OUTPUT.read_text(encoding="utf-8") == render(place_names())


def test_short_forms_of_autonomous_areas() -> None:
    assert short_form("凉山彝族自治州") == "凉山州"
    assert short_form("黔东南苗族侗族自治州") == "黔东南州"
    assert short_form("三江侗族自治县") == "三江县"
    assert short_form("东乡族自治县") is None  # 去掉民族名就没有地名了
    assert short_form("攀枝花市") is None
