"""youju coverage-* 命令：生成全国覆盖清单、试跑栏目、导出接入清单、统计覆盖。"""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[4] / "docs" / "数据来源" / "全国覆盖"
DEFAULT_CACHE = Path.home() / ".cache" / "youju-coverage"


def _fetcher(timeout: float = 20.0):
    from youju.config import get_settings
    from youju.govsites.inventory import CachingResolver
    from youju.govsites.links import is_gov_url
    from youju.crawler.fetcher import PoliteFetcher

    settings = get_settings()
    return PoliteFetcher(
        settings.user_agent,
        timeout,
        settings.per_host_interval,
        resolver=CachingResolver(),
        url_allowed=is_gov_url,
    )


def _cmd_divisions(args: argparse.Namespace) -> int:
    from youju.govsites.divisions import DIVISIONS_API, parse_tree, write_csv
    from youju.govsites.pagecache import PageCache

    with _fetcher(60.0) as fetcher:
        cache = PageCache(args.cache, fetcher, ok_days=args.max_age_days)
        result = cache.get(DIVISIONS_API)
    if result.status != "ok":
        print(f"国家地名信息库接口打不开：{result.status} {result.error}")
        return 1
    divisions = parse_tree(json.loads(result.text)["data"])
    out = args.out / "行政区划.csv"
    write_csv(divisions, out)
    levels = {lv: sum(d.level == lv for d in divisions) for lv in ("省级", "市级", "区县级")}
    print(f"写入 {out}：" + "，".join(f"{k} {v}" for k, v in levels.items()))
    return 0


def _cmd_inventory(args: argparse.Namespace) -> int:
    from youju.govsites.divisions import DivisionIndex, read_csv
    from youju.govsites.inventory import Inventory, save_state
    from youju.govsites.pagecache import PageCache
    from youju.govsites.report import write_outputs

    index = DivisionIndex(read_csv(args.divisions))
    started = time.time()
    log_path = args.cache / "inventory.log"
    log_fh = open(log_path, "a", encoding="utf-8") if args.log else None  # noqa: SIM115

    def progress(msg: str) -> None:
        if log_fh:
            log_fh.write(msg + "\n")
            log_fh.flush()
        if args.verbose:
            print(msg, flush=True)

    with _fetcher() as fetcher:
        cache = PageCache(
            args.cache,
            None if args.offline else fetcher,
            ok_days=args.max_age_days,
            offline=args.offline,
        )
        inv = Inventory(
            index,
            cache,
            provinces=args.province,
            max_level=args.max_level,
            workers=args.workers,
            per_province=args.per_province,
            progress=progress,
        )
        inv.run()
    state = args.cache / (
        "state-" + ("_".join(args.province) if args.province else "全国") + ".json"
    )
    save_state(inv, state)
    summary = write_outputs(index, list(inv.portals.values()), inv.depts, args.out, args.province)
    print(summary)
    print(
        f"用时 {time.time() - started:.0f} 秒；页面缓存命中 {cache.hits}、"
        f"联网抓取 {cache.fetches}；原始结果 {state}"
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    import hashlib
    from datetime import date

    from youju.govsites.inventory import write_table
    from youju.govsites.pagecache import PageCache
    from youju.govsites.report import COLUMN_FIELDS, column_rows, verify_summary
    from youju.govsites.verify import check_columns, columns_of

    _, depts = _load_states(args.state)
    if args.province:
        depts = [d for d in depts if d.province in args.province]
    if args.level:
        depts = [d for d in depts if d.level in args.level]
    cols = columns_of(depts)
    if args.sample_per_level:
        # 抽样（平台盘点用）：每省每级取网址散列最小的 N 个，重跑抽到的一样
        picked = []
        groups: dict[tuple[str, str], list] = {}
        for c in cols:
            groups.setdefault((c.province, c.level), []).append(c)
        for group in groups.values():
            group.sort(key=lambda c: hashlib.sha1(c.url.encode()).hexdigest())
            picked += group[: args.sample_per_level]
        cols = picked
    print(f"试读 {len(cols)} 个栏目……", flush=True)
    with _fetcher() as fetcher:
        cache = PageCache(
            args.cache,
            None if args.offline else fetcher,
            ok_days=args.max_age_days,
            offline=args.offline,
        )
        checks = check_columns(cache, cols, workers=args.workers)
    today = date.today().isoformat()
    by_prov: dict[str, list] = {}
    for c in checks:
        by_prov.setdefault(c.province, []).append(c)
    sub = "栏目抽样试读" if args.sample_per_level else "栏目清单"
    for prov, group in by_prov.items():
        write_table(args.out / sub / f"{prov}.csv", COLUMN_FIELDS, column_rows(group, today))
    tag = ("抽样-" if args.sample_per_level else "") + (
        "_".join(args.province) if args.province else "全国"
    )
    dump = [asdict(c) | {"latest": c.latest.isoformat() if c.latest else None} for c in checks]
    (args.cache / f"checks-{tag}.json").write_text(
        json.dumps(dump, ensure_ascii=False), encoding="utf-8"
    )
    print(verify_summary(checks))
    return 0


def _cmd_board(args: argparse.Namespace) -> int:
    from collections import Counter

    from sqlalchemy import func, select

    from youju.govsites.board import PENDING, STATES, SourceState, cells, coverage_rate, rollup
    from youju.govsites.inventory import write_table
    from youju.govsites.verify import ColumnCheck
    from youju.db import session_factory
    from youju.models import Document, Source

    _, depts = _load_states(args.state)
    depts = [d for d in depts if d.province in args.province]
    checks: dict[str, ColumnCheck] = {c.url: c for c in _load_checks(args.checks)}
    sources: dict[str, SourceState] = {}
    with session_factory()() as session:
        counts = dict(
            session.execute(
                select(Document.source_id, func.count()).group_by(Document.source_id)
            ).all()
        )
        for s in session.scalars(select(Source).where(Source.province.in_(args.province))):
            sources[s.url] = SourceState(
                s.url,
                s.enabled,
                s.fetch_mode,
                s.health,
                s.last_success_at,
                s.last_error,
                s.verification_status,
                counts.get(s.id, 0),
            )
    result = cells(depts, sources, checks)
    header = (
        "层级",
        "省份",
        "城市",
        "区县",
        "区划代码",
        "部门",
        "部门全称",
        "状态",
        "原因",
        "最后成功时间",
        "部门网址",
        "导航来源页",
        "栏目网址",
    )
    rows = [
        [
            c.level,
            c.province,
            c.city,
            c.district,
            c.code,
            c.dept,
            c.dept_full_name,
            c.state,
            c.reason,
            c.last_success_at.strftime("%Y-%m-%d %H:%M") if c.last_success_at else "",
            c.site_url,
            c.nav_source,
            " ".join(c.column_urls),
        ]
        for c in result
    ]
    for prov in args.province:
        write_table(
            args.out / "试点覆盖" / f"{prov}.csv", header, [r for r in rows if r[1] == prov]
        )
    states = STATES + (PENDING,)
    print("层级 | 应接入 | " + " | ".join(states) + " | 覆盖率")
    for prov in args.province:
        for level, cnt in sorted(
            rollup(c for c in result if c.province == prov).items(),
            key=lambda kv: ["省级", "市级", "区县级"].index(kv[0]),
        ):
            rate = coverage_rate(cnt)
            shown = f"{rate:.0%}" if rate is not None else "-"
            print(
                f"{prov}{level} | {sum(cnt.values())} | "
                + " | ".join(str(cnt[s]) for s in states)
                + f" | {shown}"
            )
    total = Counter(c.state for c in result)
    print("合计 | " + str(sum(total.values())) + " | " + " | ".join(str(total[s]) for s in states))
    return 0


def _load_states(paths) -> tuple[list, list]:
    """读一个或几个盘点结果；门户没找到的行政区补上标准部门格（见 complete_slots）。"""
    from youju.govsites.inventory import complete_slots, load_state

    portals, depts = [], []
    for path in paths:
        p, d = load_state(path)
        portals += p
        depts += d
    return portals, complete_slots(portals, depts)


def _load_checks(paths) -> list:
    from youju.govsites.verify import ColumnCheck

    out = []
    for path in paths or []:
        for c in json.loads(path.read_text(encoding="utf-8")):
            c.pop("latest", None)
            out.append(ColumnCheck(**c))
    return out


def _cmd_survey(args: argparse.Namespace) -> int:
    from youju.govsites.divisions import DivisionIndex, read_csv
    from youju.govsites.survey import detail, markdown, survey_all

    portals, depts = _load_states(args.state)
    checks = _load_checks(args.checks)
    index = DivisionIndex(read_csv(args.divisions))
    provinces = ["国家"] + [p.province for p in index.provinces()]
    rows = survey_all(provinces, portals, depts, checks)
    text = "\n\n".join([markdown(rows)] + [detail(r) for r in rows])
    out = args.out / "集约化平台盘点_数据.md"
    out.write_text(
        "# 集约化平台盘点数据（程序生成，youju coverage-survey）\n\n" + text + "\n",
        encoding="utf-8",
    )
    print(markdown(rows))
    print(f"写入 {out}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, default=DEFAULT_OUT, help="清单输出目录")
    common.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="页面缓存目录")
    common.add_argument("--max-age-days", type=float, default=14, help="缓存的页面多少天内不重抓")
    common.add_argument(
        "--divisions", type=Path, default=DEFAULT_OUT / "行政区划.csv", help="行政区划清单"
    )

    p = sub.add_parser(
        "coverage-divisions", parents=[common], help="从国家地名信息库取全国行政区划"
    )
    p.set_defaults(func=_cmd_divisions)

    p = sub.add_parser(
        "coverage-inventory", parents=[common], help="逐级走官方导航，生成全国门户、部门、栏目清单"
    )
    p.add_argument("--province", action="append", help="只走某个省（简称，可重复）")
    p.add_argument("--max-level", choices=("省级", "市级", "区县级"), default="区县级")
    p.add_argument("--workers", type=int, default=96)
    p.add_argument("--per-province", type=int, default=6, help="同一省同时处理几个行政区")
    p.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    p.add_argument("--log", action="store_true", help="进度写到缓存目录的 inventory.log")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=_cmd_inventory)

    p = sub.add_parser(
        "coverage-verify", parents=[common], help="试读盘点找到的栏目，生成可导入的栏目清单"
    )
    p.add_argument(
        "--state",
        type=Path,
        action="append",
        required=True,
        help="coverage-inventory 存的原始结果 JSON，可重复",
    )
    p.add_argument("--province", action="append")
    p.add_argument("--level", action="append", choices=("省级", "市级", "区县级"))
    p.add_argument("--sample-per-level", type=int, help="每省每级只抽 N 个（平台盘点用）")
    p.add_argument("--workers", type=int, default=48)
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser(
        "coverage-board",
        parents=[common],
        help="按覆盖看板的五种状态统计（读本机数据库里的来源和抓取结果）",
    )
    p.add_argument("--state", type=Path, action="append", required=True)
    p.add_argument("--checks", type=Path, action="append", help="coverage-verify 存的试读结果 JSON")
    p.add_argument("--province", action="append", required=True)
    p.set_defaults(func=_cmd_board)

    p = sub.add_parser(
        "coverage-survey", parents=[common], help="每省集约化平台盘点（平台分布、可读比例、防护）"
    )
    p.add_argument("--state", type=Path, action="append", required=True)
    p.add_argument("--checks", type=Path, action="append")
    p.set_defaults(func=_cmd_survey)
