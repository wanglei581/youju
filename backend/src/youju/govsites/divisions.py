"""全国行政区划：应接入的省、地、县名单（国家地名信息库），以及导航里常见的名称写法。

数据来自民政部国家地名信息库的公开接口（民政部网站 2026 年起不再单独公布行政区划代码，
改由地名信息库发布）。只取 31 个省级行政区，不含港澳台；新疆生产建设兵团的师市按地名信息库
挂在新疆下面（县级市）。

字段写法和 sources 表一致：
- 省份用简称（"浙江""内蒙古"）；
- 城市：地级市去掉末尾的"市"（"杭州"），自治州、地区、盟写全称；直辖市的区县填直辖市简称（"北京"）；
  省直辖的县级单位（如湖北仙桃、海南文昌）没有上级地市，城市填它自己的简称（"仙桃"）；
- 区县写全称（"西湖区"）。
"""

import csv
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DIVISIONS_API = "https://dmfw.mca.gov.cn/9095/xzqh/getList?code=&maxLevel=3"
CSV_FIELDS = ("区划代码", "名称", "类型", "层级", "省份", "城市", "区县", "上级代码")

_PROVINCE_SUFFIXES = (
    "特别行政区",
    "维吾尔自治区",
    "壮族自治区",
    "回族自治区",
    "自治区",
    "省",
    "市",
)
_SKIP_PROVINCES = ("香港", "澳门", "台湾")
# 自治州、自治县、自治旗名称里的民族名称（可带可不带"族"）：去掉后得到导航里常见的简称
_ETHNIC_NAMES = sorted(
    [
        "土家",
        "苗",
        "彝",
        "藏",
        "羌",
        "回",
        "蒙古",
        "哈萨克",
        "柯尔克孜",
        "塔吉克",
        "锡伯",
        "满",
        "朝鲜",
        "壮",
        "瑶",
        "侗",
        "布依",
        "水",
        "仡佬",
        "黎",
        "傣",
        "景颇",
        "傈僳",
        "白",
        "纳西",
        "拉祜",
        "佤",
        "哈尼",
        "普米",
        "独龙",
        "怒",
        "阿昌",
        "德昂",
        "布朗",
        "畲",
        "土",
        "撒拉",
        "东乡",
        "保安",
        "裕固",
        "达斡尔",
        "鄂温克",
        "鄂伦春",
        "仫佬",
        "毛南",
        "各",
    ],
    key=len,
    reverse=True,
)
# 约定俗成、规则推不出来的简称
_ALIASES = {
    "巴音郭楞蒙古自治州": ("巴州",),
    "博尔塔拉蒙古自治州": ("博州",),
    "克孜勒苏柯尔克孜自治州": ("克州",),
}
_LEVELS = {1: "省级", 2: "市级", 3: "区县级"}


@dataclass(frozen=True)
class Division:
    code: str  # 6 位行政区划代码
    name: str  # 地名信息库里的全称，如"杭州市""恩施土家族苗族自治州""西湖区"
    kind: str  # 省、直辖市、自治区、地级市、自治州、地区、盟、市辖区、县、县级市、旗……
    level: str  # 省级 / 市级 / 区县级
    province: str
    city: str | None
    district: str | None
    parent_code: str | None

    @property
    def label(self) -> str:
        parts = [self.province, self.city, self.district]
        out: list[str] = []
        for part in parts:
            if part and part not in out:
                out.append(part)
        return "/".join(out)


def province_short(name: str) -> str:
    for suffix in _PROVINCE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def city_short(name: str, kind: str) -> str:
    """地级市去掉末尾的"市"；自治州、地区、盟保持全称（简称容易和别的地名混）。"""
    if kind == "地级市" and name.endswith("市") and len(name) > 2:
        return name[:-1]
    return name


def _strip_ethnic(stem: str) -> str:
    """从末尾去掉民族名称（"恩施土家族苗族" → "恩施"），至少留两个字。"""
    changed = True
    while changed:
        changed = False
        for eth in _ETHNIC_NAMES:
            for tail in (eth + "族", eth):
                if stem.endswith(tail) and len(stem) - len(tail) >= 2:
                    stem = stem[: -len(tail)]
                    changed = True
                    break
            if changed:
                break
    return stem


def name_variants(name: str, kind: str = "") -> set[str]:
    """导航里可能出现的写法：全称、去掉"市/县/区/旗/盟/地区"的简称、自治州县旗的"XX州/县/旗"。

    简称至少两个字："沙市区"不会变成"沙"。
    """
    out = {name, *_ALIASES.get(name, ())}
    if kind in ("省", "自治区", "直辖市") or name.endswith(("省", "自治区")):
        out.add(province_short(name))
    m = re.fullmatch(r"(.+?)(地区|林区|特区|市|县|区|旗|盟|州)", name)
    if m and len(m.group(1)) >= 2 and "自治" not in name:
        out.add(m.group(1))
    m = re.fullmatch(r"(.+?)自治([州县旗])", name)
    if m:
        stem = _strip_ethnic(m.group(1))
        if stem.endswith("族") and len(stem) > 3:
            stem = stem[:-1]
        if len(stem) >= 2:
            out.add(stem + m.group(2))  # 恩施州
            out.add(stem)  # 恩施
    return out


def parse_tree(data: dict) -> list[Division]:
    """把地名信息库接口返回的树（getList?maxLevel=3）展开成列表，只留 31 个省级行政区。"""
    out: list[Division] = []
    for prov in data.get("children") or []:
        pname = prov["name"]
        pshort = province_short(pname)
        if pshort in _SKIP_PROVINCES:
            continue
        pcode = prov["code"][:6]
        out.append(Division(pcode, pname, prov["type"], "省级", pshort, None, None, None))
        municipality = prov["type"] == "直辖市"
        for child in prov.get("children") or []:
            ccode = child["code"][:6]
            if child["level"] == 2:
                cshort = city_short(child["name"], child["type"])
                out.append(
                    Division(
                        ccode, child["name"], child["type"], "市级", pshort, cshort, None, pcode
                    )
                )
                for county in child.get("children") or []:
                    out.append(
                        Division(
                            county["code"][:6],
                            county["name"],
                            county["type"],
                            "区县级",
                            pshort,
                            cshort,
                            county["name"],
                            ccode,
                        )
                    )
            else:
                # 直辖市的区县、省直辖的县级单位：没有地市一级
                city = pshort if municipality else _own_city(child["name"])
                out.append(
                    Division(
                        ccode,
                        child["name"],
                        child["type"],
                        "区县级",
                        pshort,
                        city,
                        child["name"],
                        pcode,
                    )
                )
    return out


def _own_city(name: str) -> str:
    """省直辖县级单位的"城市"填它自己的简称：仙桃市 → 仙桃，神农架林区 → 神农架。"""
    variants = sorted(name_variants(name), key=len)
    return variants[0] if variants else name


def write_csv(divisions: Iterable[Division], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for d in divisions:
            writer.writerow(
                [
                    d.code,
                    d.name,
                    d.kind,
                    d.level,
                    d.province,
                    d.city or "",
                    d.district or "",
                    d.parent_code or "",
                ]
            )


def read_csv(path: Path) -> list[Division]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [
            Division(
                row["区划代码"],
                row["名称"],
                row["类型"],
                row["层级"],
                row["省份"],
                row["城市"] or None,
                row["区县"] or None,
                row["上级代码"] or None,
            )
            for row in csv.DictReader(fh)
        ]


class DivisionIndex:
    """按上下级查找，按导航文字认出是哪个行政区。"""

    def __init__(self, divisions: Iterable[Division]) -> None:
        self.all = list(divisions)
        self.by_code = {d.code: d for d in self.all}
        self._children: dict[str, list[Division]] = {}
        for d in self.all:
            if d.parent_code:
                self._children.setdefault(d.parent_code, []).append(d)

    def provinces(self) -> list[Division]:
        return [d for d in self.all if d.level == "省级"]

    def children(self, code: str) -> list[Division]:
        return list(self._children.get(code, []))

    def descendants(self, code: str) -> Iterator[Division]:
        for child in self._children.get(code, []):
            yield child
            yield from self.descendants(child.code)

    def province_of(self, d: Division) -> Division:
        return self.by_code[d.code[:2] + "0000"]

    def matcher(
        self, candidates: Iterable[Division], prefixes: Iterable[str] = ()
    ) -> "NameMatcher":
        return NameMatcher(candidates, prefixes)


# 导航文字结尾常带的"人民政府""门户网站"等，比较前去掉
_TEXT_SUFFIX = re.compile(
    r"(人民政府门户网站|人民政府网站|人民政府网|人民政府|政府门户网站|政府门户网|政府网站|政府网|政府"
    r"|门户网站|门户网|门户|官方网站|官网|网站)$"
)
_BRACKETS = re.compile(r"[（(【\[].*?[）)】\]]")
_SPACES = re.compile(r"[\s　·•>»]+")


def clean_place_text(text: str, prefixes: Iterable[str] = ()) -> str:
    """去掉空白、括号注释、上级地名前缀和"人民政府"等结尾。

    "浙江省杭州市西湖区人民政府" → "西湖区"。
    """
    t = _SPACES.sub("", text or "")
    t = _BRACKETS.sub("", t)
    for _ in range(2):
        t = _TEXT_SUFFIX.sub("", t)
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if p and t.startswith(p) and len(t) > len(p) + 1:
                t = t[len(p) :]
                changed = True
    return t


class NameMatcher:
    """在给定的一组行政区里，按导航文字找是哪一个；同名或简称撞车的不认（宁缺不错）。"""

    def __init__(
        self,
        candidates: Iterable[Division],
        prefixes: Iterable[str] = (),
        exclude_short: Iterable[str] = (),
    ) -> None:
        self.prefixes = sorted({p for p in prefixes if p}, key=len, reverse=True)
        excluded = set(exclude_short)
        full: dict[str, list[Division]] = {}
        short: dict[str, list[Division]] = {}
        for d in candidates:
            full.setdefault(d.name, []).append(d)
            for v in name_variants(d.name, d.kind) - {d.name}:
                short.setdefault(v, []).append(d)
        self._full = {k: v[0] for k, v in full.items() if len(v) == 1}
        # 简称和别的行政区全称相同的（如某县简称恰是另一个市名）不用；exclude_short 里的简称
        # （和外省同名的"河北""海南"）也不用，只认全称
        self._short = {
            k: v[0] for k, v in short.items() if len(v) == 1 and k not in full and k not in excluded
        }

    def match(self, text: str) -> Division | None:
        t = clean_place_text(text, self.prefixes)
        if not t or len(t) > 16:
            return None
        return self._full.get(t) or self._short.get(t)
