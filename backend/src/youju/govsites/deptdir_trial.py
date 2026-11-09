"""部门目录规则试读：挑几个县，按现在的盘点规则（含部门目录、归属判断）走一遍，再用正式抓取程序的
列表识别把找到的栏目读一遍，和 11/8 全国清单比，写出逐格明细。

  uv run python -m youju.govsites.deptdir_trial --county 330522 --county 140426 --out 某目录

每个县连同它的省、地市一起走（上级的部门网站要先认出来，县里链到上级部门的才能排掉）；门户网址和
导航来源页取清单文件 门户网站/<省>.csv（11/8 官方导航的结果），不重新走全省导航。只读公开页面，
页面缓存、robots、间隔 3 秒、透明标识都和 coverage-inventory 一样。
"""

import argparse
import csv
from collections.abc import Callable
from datetime import date
from pathlib import Path

from youju.govsites.cli import DEFAULT_CACHE, DEFAULT_OUT, _fetcher
from youju.govsites.deptdir import directory_score
from youju.govsites.divisions import DivisionIndex, read_csv
from youju.govsites.inventory import STANDARD_DEPTS, Found, Inventory, load_docs
from youju.govsites.pagecache import PageCache
from youju.govsites.verify import check_columns, columns_of

FIELDS = (
    "区划代码",
    "省份",
    "城市",
    "区县",
    "部门",
    "状态",
    "部门全称",
    "部门网址",
    "导航来源页",
    "来自部门目录",
    "原因",
    "栏目类型",
    "栏目名称",
    "栏目网址",
    "试读结果",
    "带日期条目",
    "最新日期",
    "试读说明",
    "11/8 清单状态",
    "11/8 部门网址",
    "11/8 栏目数",
)


def run(
    index: DivisionIndex,
    cache: PageCache,
    codes: list[str],
    out_dir: Path,
    on_county: Callable[[list[list[str]], list[str]], None] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """按县试读；每走完一个县调用一次 on_county(这个县的明细行, 这个县的说明)。"""
    provinces = sorted({index.by_code[c].province for c in codes})
    portals, old_depts = load_docs(out_dir, provinces)
    portal_by_code = {p.code: p for p in portals}
    old = {(d.code, d.dept): d for d in old_depts}
    rows: list[list[str]] = []
    notes: list[str] = []
    done: dict[str, Inventory] = {}
    for code in codes:
        first_row, first_note = len(rows), len(notes)
        county = index.by_code[code]
        chain = []
        c = county.parent_code
        while c and c in index.by_code:
            chain.insert(0, index.by_code[c])
            c = index.by_code[c].parent_code
        chain.append(county)
        # 同一个省、市的几个县共用一次上级的结果
        key = chain[-2].code if len(chain) > 1 else code
        inv = done.get(key)
        if inv is None:
            inv = Inventory(index, cache, provinces=[county.province], workers=8, per_province=1)
            done[key] = inv
        for d in chain:
            if d.code in inv.portals:
                continue
            p = portal_by_code.get(d.code)
            found = (
                Found(p.portal_url, p.nav_text, p.nav_source)
                if p is not None and p.status == "已找到" and p.portal_url
                else None
            )
            inv._process(d, found)
        dirs = inv.directories.get(code, [])
        for u in dirs:
            orgs, targets = directory_score(inv.page(u), inv.portals[code].portal_url)
            notes.append(
                f"{county.label}：部门目录页 {u}（机关名链接 {orgs} 个，其中目标部门 {targets} 个）"
            )
        if not dirs:
            notes.append(f"{county.label}：没认出部门目录页（门户 {inv.portals[code].portal_url}）")
        mine = [d for d in inv.depts if d.code == code]
        checks = {c.url: c for c in check_columns(cache, columns_of(mine), workers=8)}
        for d in sorted(mine, key=lambda r: (STANDARD_DEPTS + ("数据局", "开发区")).index(r.dept)):
            o = old.get((d.code, d.dept))
            base = [
                d.code,
                d.province,
                d.city,
                d.district,
                d.dept,
                d.status,
                d.dept_full_name,
                d.site_url,
                d.nav_source,
                "是" if d.nav_source in dirs else "",
                d.reason,
            ]
            tail = [
                o.status if o else "",
                o.site_url if o else "",
                str(len(o.columns)) if o else "",
            ]
            if not d.columns:
                rows.append(base + ["", "", "", "", "", "", ""] + tail)
            for col in d.columns:
                ck = checks.get(col.url)
                if ck is not None and "#xxgk-gkmlpt-" in col.url:
                    # 试读程序把信息公开分类一律说成大汉的，数字广东的写清楚
                    # （verify.py 归全国覆盖主线，不改它）
                    ck.reason = (
                        "数字广东信息公开平台的分类，列表由接口按分类加载（待写广东集约化适配器）"
                    )
                rows.append(
                    base
                    + [
                        col.kind,
                        col.name,
                        col.url,
                        ck.result if ck else "",
                        str(ck.dated) if ck else "",
                        str(ck.latest or "") if ck else "",
                        ck.reason if ck else "",
                    ]
                    + tail
                )
        if on_county is not None:
            on_county(rows[first_row:], notes[first_note:])
    return rows, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--county", action="append", required=True, help="区县区划代码（可重复）")
    ap.add_argument("--out", type=Path, required=True, help="明细 CSV 写到哪里")
    ap.add_argument("--docs", type=Path, default=DEFAULT_OUT, help="清单目录（读门户网址）")
    ap.add_argument("--divisions", type=Path, default=DEFAULT_OUT / "行政区划.csv")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args(argv)
    index = DivisionIndex(read_csv(args.divisions))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerow(FIELDS)

    def write(rows: list[list[str]], notes: list[str]) -> None:
        # 每个县走完就写：中途断了，走完的县照样留下
        with args.out.open("a", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
        for n in notes:
            print(n, flush=True)

    with _fetcher() as fetcher:
        cache = PageCache(args.cache, fetcher)
        rows, _ = run(index, cache, args.county, args.docs, on_county=write)
    print(f"{date.today()} 写入 {args.out}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
