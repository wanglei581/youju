"""每省的政府网站集约化平台盘点：同一省的部门、市、县网站是不是建在同一个平台上，
列表能不能直接读，有没有防护。数据来自盘点结果（页面特征）和抽样试读结果。"""

from collections import Counter, defaultdict
from collections.abc import Iterable

from youju.govsites.inventory import DeptRow, PortalRow

LEVELS = ("国家级", "省级", "市级", "区县级")


def _vendor(platform: str) -> str:
    return platform.split("|", 1)[0] if platform and platform != "未识别" else "未识别"


def province_survey(
    prov: str,
    portals: list[PortalRow],
    depts: list[DeptRow],
    checks: list,
) -> dict:
    """一个省的盘点数字：门户和部门网站的平台分布、最大平台占比、可读比例、防护和打不开。"""
    sites: dict[str, str] = {}  # 网站 → 平台标签（门户 + 部门网站，频道不重复算）
    reps: dict[str, list[str]] = defaultdict(list)
    for p in portals:
        if p.status == "已找到" and p.portal_url:
            sites[p.portal_url] = p.platform
            if len(reps[p.platform]) < 3:
                reps[p.platform].append(f"{p.level}·{p.name} {p.portal_url}")
    for d in depts:
        if d.status == "已找到" and d.site_type == "独立网站" and d.site_url:
            sites.setdefault(d.site_url, d.platform)
            if len(reps[d.platform]) < 3:
                reps[d.platform].append(f"{d.level}·{d.dept_full_name} {d.site_url}")
    platforms = Counter(sites.values())
    vendors = Counter(_vendor(p) for p in sites.values())
    top_vendor, top_n = vendors.most_common(1)[0] if vendors else ("", 0)
    # 按网站数：门户没找到时补的部门格（complete_slots，原因以"门户"开头）不重复算
    real = [d for d in depts if d.site_url and not d.reason.startswith("门户")]
    blocked = Counter(f"门户·{p.reason}" for p in portals if p.status == "不能抓")
    blocked.update(f"部门·{d.reason}" for d in real if d.status == "不能抓")
    down = sum(p.status == "打不开" for p in portals) + sum(d.status == "打不开" for d in real)
    results = Counter(getattr(c, "result", "") for c in checks)
    by_platform: dict[str, Counter] = defaultdict(Counter)
    for c in checks:
        by_platform[_vendor(getattr(c, "platform", ""))][getattr(c, "result", "")] += 1
    return {
        "province": prov,
        "sites": len(sites),
        "platforms": platforms,
        "vendors": vendors,
        "top_vendor": top_vendor,
        "top_share": top_n / len(sites) if sites else 0.0,
        "reps": reps,
        "blocked": blocked,
        "down": down,
        "checks": results,
        "by_platform": by_platform,
    }


def survey_all(
    provinces: Iterable[str], portals: list[PortalRow], depts: list[DeptRow], checks: list
) -> list[dict]:
    p_by: dict[str, list[PortalRow]] = defaultdict(list)
    d_by: dict[str, list[DeptRow]] = defaultdict(list)
    c_by: dict[str, list] = defaultdict(list)
    for p in portals:
        p_by[p.province].append(p)
    for d in depts:
        d_by[d.province].append(d)
    for c in checks:
        c_by[c.province].append(c)
    return [province_survey(prov, p_by[prov], d_by[prov], c_by[prov]) for prov in provinces]


def markdown(rows: list[dict]) -> str:
    lines = [
        "| 省份 | 网站数 | 主要平台（占比） | 平台种类 | "
        "抽样试读：可读/需渲染/未识别/不能抓/打不开 | 防护、拦截 | 打不开 |",
        "|---|---:|---|---:|---|---|---:|",
    ]
    for r in rows:
        c = r["checks"]
        blocked = "；".join(f"{k}×{v}" for k, v in r["blocked"].most_common(3)) or "无"
        lines.append(
            f"| {r['province']} | {r['sites']} | "
            f"{r['top_vendor'] or '—'}（{r['top_share']:.0%}） | "
            f"{len(r['vendors'])} | {c['可读']}/{c['需浏览器渲染或接口']}/{c['未识别出条目']}/"
            f"{c['不能抓']}/{c['打不开']} | {blocked} | {r['down']} |"
        )
    return "\n".join(lines)


def detail(r: dict) -> str:
    """一个省的平台明细：每个平台的网站数、代表网址、抽样试读结果。"""
    out = [f"### {r['province']}", ""]
    for platform, n in r["platforms"].most_common(6):
        reps = "；".join(r["reps"].get(platform, [])[:2])
        out.append(f"- {platform}：{n} 个网站。代表：{reps}")
    for vendor, cnt in sorted(r["by_platform"].items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(cnt.values())
        out.append(
            f"- 抽样试读 · {vendor}：{total} 个栏目，可读 {cnt['可读']}，需渲染或接口 "
            f"{cnt['需浏览器渲染或接口']}，未识别 {cnt['未识别出条目']}，"
            f"不能抓 {cnt['不能抓']}，打不开 {cnt['打不开']}"
        )
    return "\n".join(out)
