"""发文机关校验用的两份名单（src/youju/data/）：地名名单、机关名称词表。

- place_names.txt：地级、县级区划名，加自治州、自治县、自治旗的简称（凉山彝族自治州→凉山州），
  由 docs 里的全国行政区划清单生成；
- agency_bodies.txt：去掉地名以后的机关名称（"财政局""发展和改革委员会""人民政府办公室"），由
  docs 里部门栏目清单的部门全称生成（至少两处出现的），加上常见机关的手工补充（EXTRA_BODIES）。

正文解析时文件标题开头的机关要是"地名 + 词表里的机关名称"（见 crawler/detail.py 的 _full_agency），
"张三代表市政府""北京市财政局张三建议市政府"这类不当发文机关。

清单更新以后重新生成：在 backend/ 下 `python -m youju.govsites.placenames`。
"""

import csv
import re
from pathlib import Path

# 省级区划名（去地名时用）和机关名称结尾（和 crawler/detail.py 的 _AGENCY_END 一致）
_PROVINCES = frozenset(
    (
        "北京市", "天津市", "上海市", "重庆市", "内蒙古自治区", "广西壮族自治区", "西藏自治区",
        "宁夏回族自治区", "新疆维吾尔自治区", "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省",
        "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省",
        "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省", "台湾省",
    )
)  # fmt: skip

_BODY_END = re.compile(r"(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)$")

COVERAGE = Path(__file__).resolve().parents[4] / "docs" / "数据来源" / "全国覆盖"
DIVISIONS = COVERAGE / "行政区划.csv"
DEPTS = COVERAGE / "部门栏目"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "place_names.txt"
BODIES_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "agency_bodies.txt"
HEADER = (
    "# 地级、县级行政区划名（加自治州、自治县、自治旗的简称），发文机关校验用：标题开头的机关要以"
    "这里的地名开头。\n"
    "# 由 docs/数据来源/全国覆盖/行政区划.csv（国家地名信息库）生成，一行一个；"
    "重新生成：python -m youju.govsites.placenames\n"
)
BODIES_HEADER = (
    "# 去掉地名以后的机关名称，发文机关校验用：标题开头的机关要是地名加这里的名称。\n"
    "# 由 docs/数据来源/全国覆盖/部门栏目 的部门全称（至少两处出现）加手工补充生成，一行一个；"
    "重新生成：python -m youju.govsites.placenames\n"
)
# 部门栏目清单只盘了目标部门，常见的其他机关手工补上
EXTRA_BODIES = (
    "人民政府", "人民政府办公室", "人民政府办公厅", "政府办公室", "政府办公厅",
    "人民政府外事办公室", "外事办公室", "人民政府研究室", "人民政府督查室",
    "人民政府国有资产监督管理委员会", "国有资产监督管理委员会", "教育局", "教育厅", "公安局",
    "公安厅", "民政局", "民政厅", "司法局", "司法厅", "自然资源局", "自然资源厅",
    "自然资源和规划局", "住房和城乡建设局", "住房和城乡建设厅", "住房城乡建设局", "交通运输局",
    "交通运输厅", "水利局", "水利厅", "卫生健康委员会", "卫生健康局", "应急管理局", "应急管理厅",
    "审计局", "审计厅", "统计局", "税务局", "医疗保障局", "林业局", "林业和草原局",
    "退役军人事务局", "退役军人事务厅", "地方金融监督管理局", "行政审批局", "行政审批服务局",
    "粮食和物资储备局", "能源局", "体育局", "机关事务管理局", "城市管理局", "城市管理行政执法局",
    "综合行政执法局", "乡村振兴局", "民族宗教事务委员会", "民族宗教事务局", "新闻出版局",
    "广播电视局", "人才和就业管理中心", "政务服务管理局", "公共资源交易中心",
    "住房公积金管理中心", "社会保险事业管理局", "知识产权局", "政务公开办公室", "河长制办公室",
    "防汛抗旱指挥部办公室", "安全生产委员会办公室", "经济技术开发区管理委员会",
    "高新技术产业开发区管理委员会", "高新区管理委员会", "经开区管理委员会", "开发区管理委员会",
    "新区管理委员会", "海关",
)  # fmt: skip
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


def _strip_places(name: str, places: set[str]) -> tuple[str, bool]:
    """去掉开头的地名（省级区划名、名单里的地名，可连着几个）；返回剩下的和去没去掉。"""
    stripped = False
    while True:
        head = next(
            (
                k
                for k in range(min(len(name), 12), 1, -1)
                if name[:k] in places or name[:k] in _PROVINCES
            ),
            0,
        )
        if not head:
            return name, stripped
        name, stripped = name[head:], True


def agency_bodies(depts: Path = DEPTS, places: list[str] | None = None) -> list[str]:
    known = set(places if places is not None else place_names())
    seen: dict[str, int] = {}
    for path in sorted(depts.glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                full = (row.get("部门全称") or "").strip()
                # 去掉站名（"绿色温州-温州市生态环境局"）和括号注释
                full = re.sub(r"[（(].*?[）)]", "", re.split(r"[-—－|·]", full)[-1]).strip()
                body, stripped = _strip_places(full, known)
                if stripped and 2 <= len(body) <= 25 and _BODY_END.search(body):
                    seen[body] = seen.get(body, 0) + 1
    bodies = {body for body, n in seen.items() if n >= 2 and not body.startswith("场")}
    return sorted(bodies | set(EXTRA_BODIES))


def render(names: list[str]) -> str:
    return HEADER + "".join(f"{name}\n" for name in names)


def render_bodies(bodies: list[str]) -> str:
    return BODIES_HEADER + "".join(f"{body}\n" for body in bodies)


if __name__ == "__main__":
    names = place_names()
    OUTPUT.write_text(render(names), encoding="utf-8")
    BODIES_OUTPUT.write_text(render_bodies(agency_bodies(places=names)), encoding="utf-8")
    print(f"写入 {OUTPUT}、{BODIES_OUTPUT}")
