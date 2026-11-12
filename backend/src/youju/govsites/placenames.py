"""发文机关校验用的地名名单（src/youju/data/place_names.txt）：由全国行政区划清单生成。

名单里是地级、县级区划名，加自治州、自治县、自治旗的简称（"凉山彝族自治州"→"凉山州"）。正文解析时
文件标题开头的机关要以这些地名开头（见 crawler/detail.py 的 _starts_with_place），"张三代表市政府"
"住房和城乡建设局"这类开头不是地名的不当发文机关。

行政区划清单更新以后重新生成：在 backend/ 下 `python -m youju.govsites.placenames`。
"""

import csv
from pathlib import Path

DIVISIONS = Path(__file__).resolve().parents[4] / "docs" / "数据来源" / "全国覆盖" / "行政区划.csv"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "place_names.txt"
HEADER = (
    "# 地级、县级行政区划名（加自治州、自治县、自治旗的简称），发文机关校验用：标题开头的机关要以"
    "这里的地名开头。\n"
    "# 由 docs/数据来源/全国覆盖/行政区划.csv（国家地名信息库）生成，一行一个；"
    "重新生成：python -m youju.govsites.placenames\n"
)
_SKIP = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}
# 民族名：长的先去（"苗族侗族"这类连写的一个个去掉）
_ETHNIC = sorted(
    [
        "彝族", "苗族", "侗族", "朝鲜族", "回族", "藏族", "哈萨克族", "蒙古族", "布依族",
        "傣族", "景颇族", "傈僳族", "白族", "哈尼族", "壮族", "土家族", "柯尔克孜族", "羌族",
        "维吾尔族", "满族", "瑶族", "畲族", "黎族", "水族", "仡佬族", "毛南族", "仫佬族",
        "纳西族", "普米族", "拉祜族", "佤族", "独龙族", "怒族", "布朗族", "德昂族", "基诺族",
        "阿昌族", "京族", "撒拉族", "保安族", "东乡族", "土族", "裕固族", "锡伯族", "达斡尔族",
        "鄂温克族", "鄂伦春族", "赫哲族", "塔吉克族", "乌孜别克族", "俄罗斯族", "塔塔尔族",
        "门巴族", "珞巴族", "高山族", "各族",
    ],
    key=len,
    reverse=True,
)  # fmt: skip


def short_form(name: str) -> str | None:
    """自治州、自治县、自治旗的简称：凉山彝族自治州→凉山州、三江侗族自治县→三江县；去掉民族名
    以后不到两个字的不要。"""
    for suffix, tail in (("自治州", "州"), ("自治县", "县"), ("自治旗", "旗")):
        if not name.endswith(suffix):
            continue
        core = name.removesuffix(suffix)
        while stripped := next((e for e in _ETHNIC if core.endswith(e) and len(core) > len(e)), ""):
            core = core.removesuffix(stripped)
        return core + tail if len(core) >= 2 and core != name.removesuffix(suffix) else None
    return None


def place_names(divisions: Path = DIVISIONS) -> list[str]:
    names: set[str] = set()
    with divisions.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("名称") or "").strip()
            if row.get("层级") not in ("市级", "区县级") or name in _SKIP or len(name) < 2:
                continue
            names.add(name)
            if short := short_form(name):
                names.add(short)
    return sorted(names)


def render(names: list[str]) -> str:
    return HEADER + "".join(f"{name}\n" for name in names)


if __name__ == "__main__":
    OUTPUT.write_text(render(place_names()), encoding="utf-8")
    print(f"写入 {OUTPUT}")
