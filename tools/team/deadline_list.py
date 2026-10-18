#!/usr/bin/env python3
"""给园区专员的"本省未来 14 天截止"清单（推广与获客方案第五节），一次出两份文字：

  可转发的一份：只有截止日期已人工核对的申报通知，可以直接粘进企业群。和小程序截止页"只看已核对"
    同一个接口（/reports/deadlines 不带 excerpts）。不放网址，末尾固定一行：
    "整理自：有据（第三方工具，以政府原文为准；原文和核对状态可在小程序里查看）"。
  只给专员的一份：可转发那份的条目，加上机器从原文摘出、未经人工核对的日期（只会是 8 天以后到期的，
    附原句），再加上"7 天内可能截止、还没人工核对"的通知（不写日期）；每条带政府原文链接。
    和小程序截止页默认视图（带 excerpts）同一个接口和规则（D-061：7 天内只给人工核对过的日期）。

范围：这个省的省级部门，加上已收录的这个省的市、区；--city 只留省级和这个市（含市下面的区）；
默认不含国家部委，--national 加上。

怎么取数：和 concierge.py 一样，借开发者工具在模拟器里调线上的只读接口（截止雷达、文件详情、收录范围），
运行前开发者工具要开着"有据"项目、模拟器已经加载出首页。

用法（用后端虚拟环境里的 Python）：
  /Users/wanglei/youju-worktrees/ai/backend/.venv/bin/python tools/team/deadline_list.py <省份> [--city 市] [--national] [--out 目录]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import concierge  # 同目录的取数工具

DAYS = 14
WEEKDAYS = "一二三四五六日"
FOOTER = "整理自：有据（第三方工具，以政府原文为准；原文和核对状态可在小程序里查看）"
REVIEW_KIND = "推荐或审核截止"
GAP_MS = 400
NATIONAL = "国家部委"


PAGE_SIZE = 100  # 截止雷达每页最多 100 条


def report_path(province: str, excerpts: bool, page: int = 1) -> str:
    params = {"days": str(DAYS), "province": province, "size": str(PAGE_SIZE), "page": str(page)}
    if excerpts:
        params["excerpts"] = "1"
    return "/api/v1/reports/deadlines?" + "&".join(
        f"{k}={quote(v)}" for k, v in params.items()
    )


def in_scope(item: dict, city: str) -> bool:
    """--city：省级、国家部委的都留；市级、区县级只留这个市的。"""
    if not city or item.get("level") not in ("市级", "区县级"):
        return True
    return item.get("city") == city


def place(name: str) -> str:
    """收录范围里的市名不带"市"字（"青岛""广州"）；自治州、盟、地区原样写。"""
    return name if name.endswith(("市", "自治州", "盟", "地区")) else name + "市"


def day_text(iso: str, clock: str | None, today: date | None = None) -> str:
    """不是今年的写上年份（12 月底出的清单里有次年 1 月的截止）。"""
    day = date.fromisoformat(iso)
    year = f"{day.year}年" if today and day.year != today.year else ""
    text = f"{year}{day.month}月{day.day}日（周{WEEKDAYS[day.weekday()]}）"
    return text + (clock or "")


def by_due(item: dict) -> tuple:
    return (item.get("date") or "", item.get("time") is None, item.get("time") or "", item.get("id") or "")


def scope_text(province: str, city: str, national: bool, coverage: dict) -> str:
    cities = [
        place(c["city"])
        for c in coverage.get("cities") or []
        if c.get("province") == province and (not city or c.get("city") == city)
    ]
    districts = [
        place(d["city"]) + d["district"]
        for d in coverage.get("districts") or []
        if d.get("province") == province and (not city or d.get("city") == city)
    ]
    covered = "、".join(cities + districts)
    text = f"范围：{province}的省级部门"
    if covered:
        text += f"，加上已收录的{covered}"
    elif city:
        text += f"（{place(city)}的通知目前没有收录）"
    else:
        text += "（这个省的市、区目前都没有收录）"
    text += "发布的申报通知"
    text += "，含国家部委" if national else "，不含国家部委"
    return text + "；其他市、区（包括高新区、开发区）的通知请另看当地政府网站。"


def shareable_text(label: str, items: list[dict], scope: str, today: date) -> str:
    """可转发的一份：只有人工核对过的，不放网址。没有条目时写明这次不要发。"""
    stamp = f"{today.month}月{today.day}日"
    if not items:
        return (
            f"（这一份是空的：{stamp}整理时，{label}没有未来 {DAYS} 天截止、截止日期已人工核对的申报通知。"
            "这次不要发。）\n"
        )
    lines = [
        f"{label} · 未来 {DAYS} 天截止的申报通知",
        f"（截止日期已对照政府原文人工核对；{stamp}整理）",
        scope,  # 范围紧跟标题：标题看着像全省，其实只有省级部门和收录了的市、区
        "",
    ]
    for n, item in enumerate(items, 1):
        lines += [
            f"{n}. {day_text(item['date'], item.get('time'), today)}｜{item.get('label') or '截止'}",
            item.get("title") or "",
            item.get("issuer") or "",
            "",
        ]
    lines.append("只列截止日期已人工核对的，不是全部通知；申报条件、材料和提交方式以通知原文为准。")
    if any(item.get("label") == REVIEW_KIND for item in items):
        lines.append(
            f"标“{REVIEW_KIND}”的，是主管部门推荐或审核的截止，不是企业自己提交的截止；"
            "企业什么时候交，请看原文。"
        )
    lines.append(FOOTER)
    return "\n".join(lines) + "\n"


def private_text(
    label: str,
    verified: list[dict],
    excerpts: list[dict],
    soon: list[dict],
    soon_total: int,
    soon_more: bool,
    urls: dict[str, str],
    scope: str,
    today: date,
) -> str:
    """只给专员的一份：三段，每条带原文链接。"""

    def link(item: dict) -> str:
        url = urls.get(item.get("id") or "")
        return f"原文：{url}" if url else "原文：网址没取到，请在小程序里搜标题"

    lines = [
        f"【只给你自己看，请不要转发】{label} · 未来 {DAYS} 天截止（含机器摘录）",
        (
            f"{today.month}月{today.day}日整理。能直接转进企业群的，只有另一份（已人工核对）里的条目；"
            "这一份里“机器摘录”的日期是程序从原文里摘出来的，没有人工核对，可能有误。"
        ),
        "",
        f"一、已人工核对（{len(verified)} 条，和可转发的那份相同）",
    ]
    for n, item in enumerate(verified, 1):
        lines += [
            f"{n}. {day_text(item['date'], item.get('time'), today)}｜{item.get('label') or '截止'}",
            item.get("title") or "",
            item.get("issuer") or "",
            link(item),
            "",
        ]
    if not verified:
        lines += ["（没有）", ""]
    lines.append(
        f"二、机器摘录·未经人工核对（{len(excerpts)} 条，只会是 8 天以后到期的；"
        "日期和截止类型都是程序从原文里摘的，可能不准）"
    )
    for n, item in enumerate(excerpts, 1):
        lines += [
            f"{n}. {day_text(item['date'], item.get('time'), today)}｜{item.get('label') or '截止'}（机器摘录）",
            item.get("title") or "",
            item.get("issuer") or "",
            f"原文这句：“{item.get('quote')}”",
            link(item),
            "",
        ]
    if not excerpts:
        lines += ["（没有）", ""]
    if soon_more:
        count = f"列了 {len(soon)} 条，可能还有没列出的"
    elif soon_total > len(soon):
        count = f"{soon_total} 条，这里列了前 {len(soon)} 条"
    else:
        count = f"{len(soon)} 条"
    lines.append(f"三、7 天内可能截止、还没人工核对（{count}；不写日期，请打开原文看）")
    for n, item in enumerate(soon, 1):
        lines += [
            f"{n}. {item.get('title') or ''}",
            f"{item.get('issuer') or ''}｜{item.get('kind') or '截止未核对'}",
            link(item),
            "",
        ]
    if not soon:
        lines += ["（没有）", ""]
    lines += [
        scope,
        "二、三里的没有人工核对，请不要转；想转哪条，把标题发我们，对照原文核对好以后放进可转发的那份。",
    ]
    return "\n".join(lines) + "\n"


def failed(jobs: list[dict], results: dict[str, dict]) -> bool:
    bad = [job["id"] for job in jobs if (results.get(job["id"]) or {}).get("status") != 200]
    if bad:
        print(
            "没取成："
            + "、".join(
                f"{i}（{(results.get(i) or {}).get('error') or (results.get(i) or {}).get('status')}）"
                for i in bad
            )
        )
    return bool(bad)


def main() -> int:
    parser = argparse.ArgumentParser(description="本省未来 14 天截止清单（两份）")
    parser.add_argument("province", help="省份，如 山东、北京、内蒙古")
    parser.add_argument("--city", default="", help="只留省级和这个市的，如 青岛")
    parser.add_argument("--national", action="store_true", help="加上国家部委的")
    parser.add_argument("--out", default=".", help="输出目录，默认当前目录")
    args = parser.parse_args()
    province, city = args.province.strip(), args.city.strip().removesuffix("市")
    if province not in concierge.PROVINCES:
        print(f"省份写法不对：{province}（写简称，如 山东、内蒙古）")
        return 1

    concierge.evaluate(concierge._INSTALL_JS)
    scopes = [province, *([NATIONAL] if args.national else [])]
    jobs = [{"id": "cov", "path": "/api/v1/coverage", "gap": GAP_MS, "slim": "coverage"}]
    for n, name in enumerate(scopes):
        jobs.append({"id": f"a{n}", "path": report_path(name, False), "gap": GAP_MS, "slim": "report"})
        jobs.append({"id": f"b{n}", "path": report_path(name, True), "gap": GAP_MS, "slim": "report"})
    results = concierge.run_jobs(jobs, "取截止")
    if failed(jobs, results):
        return 1
    # 一页最多 100 条：超过的翻页取齐（--city 要在取齐以后再筛），取不齐就不出清单
    more = []
    for n, name in enumerate(scopes):
        for key, excerpts in ((f"a{n}", False), (f"b{n}", True)):
            pages = -(-(results[key].get("total") or 0) // PAGE_SIZE)
            more += [
                {"id": f"{key}p{page}", "path": report_path(name, excerpts, page), "gap": GAP_MS, "slim": "report", "of": key}
                for page in range(2, pages + 1)
            ]
    if more:
        extra = concierge.run_jobs([{k: v for k, v in job.items() if k != "of"} for job in more], "取后面几页")
        if failed(more, extra):
            return 1
        for job in more:
            results[job["of"]]["items"] = (results[job["of"]].get("items") or []) + (extra[job["id"]].get("items") or [])
    short = [
        key
        for n, _ in enumerate(scopes)
        for key in (f"a{n}", f"b{n}")
        if len(results[key].get("items") or []) < (results[key].get("total") or 0)
    ]
    if short:
        print(f"截止条目没取齐（{'、'.join(short)}），请重跑")
        return 1
    coverage = results["cov"]
    if province in {c.get("name") for c in coverage.get("notCovered") or []}:
        print(f"{province}还没有收录，出不了清单")
        return 1

    today = concierge.today_cn()
    verified, excerpts, soon, soon_total, mismatched, soon_more = [], [], [], 0, [], False
    for n, _ in enumerate(scopes):
        verified += [
            item
            for item in results[f"a{n}"].get("items") or []
            if in_scope(item, city) and item.get("date") and date.fromisoformat(item["date"]) >= today
        ]
    shared = {item["id"] for item in verified}
    for n, _ in enumerate(scopes):
        report = results[f"b{n}"]
        for item in report.get("items") or []:
            if item.get("id") in shared or not in_scope(item, city):
                continue
            if item.get("source") == "verified":
                mismatched.append(item.get("id"))
                continue
            # 和小程序 dueView 一样再拦一次：要有原句，按今天算在 8 天以后
            days = (date.fromisoformat(item["date"]) - today).days if item.get("date") else -1
            if item.get("source") == "excerpt" and item.get("quote") and concierge.EXCERPT_MIN_DAYS <= days <= DAYS:
                excerpts.append(item)
        raw = report.get("soon") or []
        # 不按文件编号去掉：同一份通知可以一类截止已核对（12 天后）、另一类 7 天内还没核对
        listed = [s for s in raw if in_scope(s, city)]
        soon += listed
        # 接口最多给 50 条。给全了就按列出的算；没给全时，不筛市的能从总数推出来，筛了市的推不出来
        if (report.get("soonTotal") or 0) > len(raw):
            if city:
                soon_more = True
                soon_total += len(listed)
            else:
                soon_total += report.get("soonTotal") or 0
        else:
            soon_total += len(listed)
    verified.sort(key=by_due)
    excerpts.sort(key=by_due)
    if mismatched:
        print(f"注意：带摘录的接口里有 {len(mismatched)} 条核对过的不在“只看已核对”里（编号 {'、'.join(mismatched)}），没有列出，请查一下")

    ids = list(dict.fromkeys(item["id"] for item in verified + excerpts + soon))
    details = concierge.run_jobs(
        [{"id": f"d{i}", "path": f"/api/v1/documents/{i}", "gap": GAP_MS, "slim": "detail"} for i in ids],
        "取原文链接",
    )
    urls = {i: (details.get(f"d{i}") or {}).get("url") or "" for i in ids}

    label = province + (f" · {place(city)}" if city else "") + ("（含国家部委）" if args.national else "")
    scope = scope_text(province, city, args.national, coverage)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stem = province + (f"_{city}" if city else "") + f"_未来{DAYS}天截止"
    stamp = today.strftime("%Y%m%d")
    first = out / f"{stem}_可转发_{stamp}.txt"
    second = out / f"{stem}_含机器摘录_只给专员_{stamp}.txt"
    first.write_text(shareable_text(label, verified, scope, today), encoding="utf-8")
    second.write_text(
        private_text(label, verified, excerpts, soon, soon_total, soon_more, urls, scope, today),
        encoding="utf-8",
    )
    print(f"已生成：{first}")
    print(f"已生成：{second}")
    print(
        f"已人工核对 {len(verified)} 条；机器摘录 {len(excerpts)} 条；7 天内还没核对 {soon_total} 条"
        + ("以上" if soon_more else "")
    )
    if not verified:
        print("可转发的那份是空的：先在管理台核对“只给专员”那份里的条目，再重新生成")
    missing = [i for i in ids if not urls[i]]
    if missing:
        print(f"有 {len(missing)} 条没取到原文链接（编号 {'、'.join(missing)}）：“只给专员”那份先别发，重跑一次")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
