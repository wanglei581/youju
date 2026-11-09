"""把盘点结果写成按省分开的 CSV，并汇总每省、每级的找到情况。

只写某几级（levels）时，文件里其他级的行原样留下：几条线分级并行时，谁跑哪一级就只换哪一级的行。
"""

import csv
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable
from pathlib import Path

from youju.govsites.divisions import DivisionIndex
from youju.govsites.inventory import (
    _LEVEL_ORDER,
    DEPT_FIELDS,
    OPTIONAL_DEPTS,
    PORTAL_FIELDS,
    STANDARD_DEPTS,
    DeptRow,
    PortalRow,
    complete_slots,
    dept_rows,
    portal_rows,
    write_table,
)

LEVELS = ("国家级", "省级", "市级", "区县级")


def write_outputs(
    index: DivisionIndex,
    portals: list[PortalRow],
    depts: list[DeptRow],
    out_dir: Path,
    provinces: Iterable[str] | None = None,
    levels: Collection[str] | None = None,
) -> str:
    """写门户网站/<省>.csv、部门栏目/<省>.csv。levels 给了就只换这几级的行，其他级照旧。"""
    depts = complete_slots(portals, depts)
    by_prov_p: dict[str, list[PortalRow]] = defaultdict(list)
    by_prov_d: dict[str, list[DeptRow]] = defaultdict(list)
    for p in portals:
        by_prov_p[p.province].append(p)
    for d in depts:
        by_prov_d[d.province].append(d)
    for prov, rows in by_prov_p.items():
        p_path = out_dir / "门户网站" / f"{prov}.csv"
        d_path = out_dir / "部门栏目" / f"{prov}.csv"
        p_rows = portal_rows(rows)
        d_rows = dept_rows(by_prov_d.get(prov, []))
        if levels:
            p_rows = merge_level_rows(p_path, PORTAL_FIELDS, p_rows, levels, _portal_key)
            d_rows = merge_level_rows(d_path, DEPT_FIELDS, d_rows, levels, _dept_key)
        write_table(p_path, PORTAL_FIELDS, p_rows)
        write_table(d_path, DEPT_FIELDS, d_rows)
    return summarize(index, portals, depts, provinces)


_DEPT_ORDER = {d: i for i, d in enumerate(STANDARD_DEPTS + OPTIONAL_DEPTS)}


def _portal_key(row: list[str]) -> tuple:
    # 和 portal_rows 同一个顺序：省、层级、区划代码（第 0 列层级，第 4 列区划代码）
    return (row[4][:2], _LEVEL_ORDER.get(row[0], 9), row[4])


def _dept_key(row: list[str]) -> tuple:
    return (row[4][:2], _LEVEL_ORDER.get(row[0], 9), row[4], _DEPT_ORDER.get(row[5], 99))


def merge_level_rows(
    path: Path,
    header: Iterable[str],
    new_rows: list[list[str]],
    levels: Collection[str],
    key: Callable[[list[str]], tuple],
) -> list[list[str]]:
    """文件里不属于 levels 的行原样留下（第 0 列是层级），属于的换成 new_rows 里这几级的行。

    表头和现在的不一样（改过列）就不合并，报错让全量重跑，免得旧行错列。"""
    kept: list[list[str]] = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            old_header = next(reader, None)
            if old_header is not None and list(old_header) != list(header):
                raise ValueError(f"{path} 的表头和现在的不一样，不能只写某几级：请全量重跑这个省")
            kept = [row for row in reader if row and row[0] not in levels]
    rows = kept + [row for row in new_rows if row[0] in levels]
    return sorted(rows, key=key)


def summarize(
    index: DivisionIndex,
    portals: list[PortalRow],
    depts: list[DeptRow],
    provinces: Iterable[str] | None = None,
) -> str:
    only = set(provinces or [])
    lines = ["省份 | 层级 | 行政区 | 门户找到 | 门户打不开/不能抓 | 标准部门应有 | 找到 | 有栏目"]
    total = Counter()
    for prov in [p.province for p in index.provinces() if not only or p.province in only]:
        for level in LEVELS:
            divs = [d for d in index.all if d.province == prov and d.level == level]
            if not divs:
                continue
            ps = [p for p in portals if p.province == prov and p.level == level]
            ds = [
                d
                for d in depts
                if d.province == prov and d.level == level and d.dept in STANDARD_DEPTS
            ]
            found_p = sum(p.status == "已找到" for p in ps)
            bad_p = sum(p.status in ("打不开", "不能抓") for p in ps)
            slots = len(divs) * len(STANDARD_DEPTS)
            # 同一行政区同一部门可能有多行（开发区），按 (区划, 部门) 去重
            found = {(d.code, d.dept) for d in ds if d.status == "已找到"}
            with_cols = {(d.code, d.dept) for d in ds if d.status == "已找到" and d.columns}
            lines.append(
                f"{prov} | {level} | {len(divs)} | {found_p} | {bad_p} | {slots} | "
                f"{len(found)} | {len(with_cols)}"
            )
            total.update(
                {
                    "divs": len(divs),
                    "found_p": found_p,
                    "bad_p": bad_p,
                    "slots": slots,
                    "found": len(found),
                    "cols": len(with_cols),
                }
            )
    lines.append(
        f"合计 | | {total['divs']} | {total['found_p']} | {total['bad_p']} | {total['slots']} | "
        f"{total['found']} | {total['cols']}"
    )
    return "\n".join(lines)


# —— 栏目清单（和全国来源清单_P0_v1.csv 同样的前 14 列，可直接 youju import-sources）——

COLUMN_FIELDS = (
    "层级",
    "省份",
    "城市",
    "区县",
    "部门",
    "部门全称",
    "栏目名称",
    "栏目URL",
    "HTTP状态",
    "页面日期数",
    "最新日期样例",
    "验证状态",
    "验证日期",
    "备注",
    "栏目类型",
    "网站URL",
    "导航来源页",
    "栏目来源页",
    "平台",
    "区划代码",
)


def import_status(check) -> str:
    """试读结论 → 验证状态（开头的词决定 import-sources 的抓取方式；全国覆盖新来源一律不启用）。

    sources.verification_status 最长 64 字：超长的截短（开头的词保留，抓取方式不变）。"""
    return _import_status(check)[:64]


def _import_status(check) -> str:
    from youju.govsites import verify as v

    if check.result == v.READABLE:
        return "试跑可读（全国覆盖 v1，未启用）"
    if check.result == v.NEEDS_SCRIPT:
        return "需浏览器渲染或接口（全国覆盖 v1）"
    if check.result == v.NO_ITEMS:
        return f"已访问（{check.reason}，需人工确认是否列表页）"
    if check.result == v.BLOCKED:
        if "robots" in check.reason:
            return "robots 禁止抓取（尊重，不绕过）"
        return f"网站防护拦截（{check.reason}，不绕过）"
    return f"本机无法访问（{check.reason[:60]}）"


def column_rows(checks, checked_on: str) -> list[list[str]]:
    order = {"国家级": -1, "省级": 0, "市级": 1, "区县级": 2}
    rows = []
    for c in sorted(checks, key=lambda c: (c.code[:2], order[c.level], c.code, c.dept, c.kind)):
        if "#xxgk-" in c.url:
            continue  # 信息公开分类树上的分类不是能打开的栏目，不进导入清单（看板里记需人工）
        dept_full = (c.dept_full_name or c.dept)[:120]
        note = "；".join(c.samples[:2])
        rows.append(
            [
                c.level,
                c.province,
                c.city if c.level != "省级" else "",
                c.district if c.level == "区县级" else "",
                c.dept,
                dept_full,
                c.name[:60],
                c.url,
                str(c.http_status or ""),
                str(c.dated),
                c.latest.isoformat() if c.latest else "",
                import_status(c),
                checked_on,
                note[:200],
                c.kind,
                c.site_url,
                c.nav_source,
                c.column_source,
                c.platform,
                c.code,
            ]
        )
    return rows


def column_key(row: list[str]) -> tuple:
    """栏目清单的顺序（和 column_rows 一样）：省、层级、区划代码、部门、栏目类型。"""
    return (row[19][:2], _LEVEL_ORDER.get(row[0], 9), row[19], row[4], row[14])


def verify_summary(checks) -> str:
    from youju.govsites import verify as v

    lines = ["省份 | 层级 | 栏目 | 可读 | 需渲染/接口 | 未识别 | 不能抓 | 打不开"]
    groups: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for c in checks:
        groups[(c.province, c.level)][c.result] += 1
    for (prov, level), cnt in sorted(
        groups.items(), key=lambda kv: (kv[0][0], LEVELS.index(kv[0][1]))
    ):
        lines.append(
            f"{prov} | {level} | {sum(cnt.values())} | {cnt[v.READABLE]} | {cnt[v.NEEDS_SCRIPT]} | "
            f"{cnt[v.NO_ITEMS]} | {cnt[v.BLOCKED]} | {cnt[v.DOWN]}"
        )
    return "\n".join(lines)
