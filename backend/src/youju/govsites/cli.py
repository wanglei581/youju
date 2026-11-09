"""youju coverage-* 命令：生成全国覆盖清单、试跑栏目、导出接入清单、统计覆盖。"""

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[4] / "docs" / "数据来源" / "全国覆盖"
DEFAULT_CACHE = Path.home() / ".cache" / "youju-coverage"
BOARD_LOCK_WAIT = 600  # 等另一个看板命令最多这么多秒
BOARD_BUSY = "另一个覆盖看板命令（导入、重算或启用）正在跑，10 分钟内没等到，这次没做；稍后再试"


def _fetcher(timeout: float = 20.0):
    from youju.config import get_settings
    from youju.crawler.fetcher import PoliteFetcher
    from youju.govsites.inventory import CachingResolver
    from youju.govsites.links import is_gov_url

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
    from youju.govsites.inventory import Inventory, load_seeds, save_state
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
            fail_days=0 if args.retry_failed else 2,
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
            seeds=load_seeds(args.seeds),
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
            fail_days=0 if args.retry_failed else 2,
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

    from youju.db import session_factory
    from youju.govsites.board import PENDING, STATES, SourceState, cells, coverage_rate, rollup
    from youju.govsites.inventory import write_table
    from youju.govsites.verify import ColumnCheck
    from youju.models import Document, Source, SourceCheck

    _, depts = _load_states(args.state)
    depts = [d for d in depts if d.province in args.province]
    checks: dict[str, ColumnCheck] = {c.url: c for c in _load_checks(args.checks)}
    sources: dict[str, SourceState] = {}
    with session_factory()() as session:
        latest = (
            select(SourceCheck.source_id, func.max(SourceCheck.id).label("last_id"))
            .group_by(SourceCheck.source_id)
            .subquery()
        )
        last_checks = {
            sid: (status, http, items)
            for sid, status, http, items in session.execute(
                select(
                    SourceCheck.source_id,
                    SourceCheck.status,
                    SourceCheck.http_status,
                    SourceCheck.items_found,
                ).join(latest, SourceCheck.id == latest.c.last_id)
            )
        }
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
                *last_checks.get(s.id, (None, None, 0)),
            )
    result = cells(depts, sources, checks, trial=args.trial)
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
            key=lambda kv: ["国家级", "省级", "市级", "区县级"].index(kv[0]),
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

    # 同一行政区在几份结果里都有时（全国一份、单省重跑一份），以后给的为准
    portals: dict[str, object] = {}
    depts: dict[str, list] = {}
    for path in paths:
        p, d = load_state(path)
        codes = {row.code for row in p}
        for row in p:
            portals[row.code] = row
        for code in codes:
            depts[code] = []
        for row in d:
            depts.setdefault(row.code, []).append(row)
    flat = [row for rows in depts.values() for row in rows]
    return list(portals.values()), complete_slots(list(portals.values()), flat)


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
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# 集约化平台盘点数据（程序生成，youju coverage-survey）\n\n" + text + "\n",
        encoding="utf-8",
    )
    print(markdown(rows))
    print(f"写入 {out}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from youju.db import session_factory
    from youju.govsites.boarddb import (
        BOARD_STATES,
        ImportSummary,
        board_lock,
        import_inventory,
        refresh_states,
    )
    from youju.govsites.divisions import read_csv

    if args.state:
        portals, depts = _load_states(args.state)
    else:
        # 没给盘点原始结果：读清单文件（docs/数据来源/全国覆盖/门户网站、部门栏目）
        from youju.govsites.inventory import load_docs

        portals, depts = load_docs(args.out, args.province)
    if args.province:
        portals = [p for p in portals if p.province in args.province]
        depts = [d for d in depts if d.province in args.province]
    checks = {c.url: c for c in _load_checks(args.checks)}
    meta = {d.code: (d.kind, d.parent_code) for d in read_csv(args.divisions)}
    counts: Counter = Counter()
    s = ImportSummary()
    with session_factory()() as session, board_lock(session, BOARD_LOCK_WAIT) as got:
        if not got:
            print(BOARD_BUSY)
            return 1
        # 一省一导入、一省一提交（不长时间锁住格），接着按抓取结果重算这个省：
        # 看板上不留"导入了还没算"的格
        for province in sorted({p.province for p in portals}):
            part = import_inventory(
                session,
                [p for p in portals if p.province == province],
                [d for d in depts if d.province == province],
                checks,
                meta,
            )
            for field in ("divisions", "cells_created", "cells_updated", "cells_removed", "links"):
                setattr(s, field, getattr(s, field) + getattr(part, field))
            counts.update(refresh_states(session, province=province))
    print(
        f"导入完成：行政区 {s.divisions}，格新增 {s.cells_created}、更新 {s.cells_updated}、"
        f"删除 {s.cells_removed}；挂到已有来源 {s.links} 处"
    )
    print("看板状态：" + "，".join(f"{st} {counts.get(st, 0)}" for st in BOARD_STATES))
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    from youju.db import session_factory
    from youju.govsites.boarddb import BOARD_STATES, board_lock, refresh_states

    with session_factory()() as session, board_lock(session, BOARD_LOCK_WAIT) as got:
        if not got:
            print(BOARD_BUSY)
            return 1
        counts = refresh_states(session, trial=args.trial, province=args.province)
    label = "试点口径（只算数，没写看板）" if args.trial else "看板状态"
    print(label + "：" + "，".join(f"{s} {counts.get(s, 0)}" for s in BOARD_STATES))
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    from youju.db import session_factory
    from youju.govsites.boarddb import board_lock, enable_sources, refresh_states

    with session_factory()() as session, board_lock(session, BOARD_LOCK_WAIT) as got:
        if not got:
            print(BOARD_BUSY)
            return 1
        r = enable_sources(
            session, args.province, args.level, args.by, limit=args.limit, dry_run=args.dry_run
        )
        _report_enable(args, r)
        if not args.dry_run and r.enabled:
            refresh_states(session, province=args.province)  # 启用的格马上按正式口径算
    return 1 if r.blocked_reason else 0


def _report_enable(args: argparse.Namespace, r) -> None:
    """打印结果；真启用了的先写审计日志（在重算之前：重算出错也不丢审计）。"""
    if r.blocked_reason:
        print(f"没有启用：{r.blocked_reason}")
        return
    verb = "可启用" if args.dry_run else "已启用"
    print(
        f"{args.province}{args.level}（依据抽查记录 #{r.acceptance_id}）："
        f"符合条件 {r.candidates} 个，{verb} {r.enabled} 个；"
        f"最近被拒绝或读不出、不开的 {r.skipped} 个"
    )
    shown = ", ".join(str(i) for i in r.source_ids[:50])
    more = f" 等 {len(r.source_ids)} 个" if len(r.source_ids) > 50 else ""
    print(f"来源编号：{shown or '无'}{more}")
    if not args.dry_run and r.enabled:
        # 和后台操作一样留痕（操作人、依据的抽查记录、开了哪些）
        import logging

        logging.getLogger("youju.audit").info(
            json.dumps(
                {
                    "event": "admin",
                    "action": "coverage_enable",
                    "by": args.by,
                    "province": args.province,
                    "level": args.level,
                    "acceptance_id": r.acceptance_id,
                    "source_ids": r.source_ids,
                },
                ensure_ascii=False,
            )
        )


def register(sub: argparse._SubParsersAction) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, default=DEFAULT_OUT, help="清单输出目录")
    common.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="页面缓存目录")
    common.add_argument("--max-age-days", type=float, default=14, help="缓存的页面多少天内不重抓")
    common.add_argument(
        "--divisions", type=Path, default=DEFAULT_OUT / "行政区划.csv", help="行政区划清单"
    )
    common.add_argument("--retry-failed", action="store_true", help="上次没抓成功的页面重抓一次")

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
    p.add_argument(
        "--seeds",
        type=Path,
        default=DEFAULT_OUT / "导航入口补充.csv",
        help="导航入口补充（门户不通或导航由脚本生成时用的官方入口页）",
    )
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
    p.add_argument("--level", action="append", choices=("国家级", "省级", "市级", "区县级"))
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
    p.add_argument(
        "--trial", action="store_true", help="试点报告：未启用但试抓成功的也算收录，不按 3 天过期"
    )
    p.set_defaults(func=_cmd_board)

    p = sub.add_parser(
        "coverage-import", parents=[common], help="把盘点结果写进覆盖看板表（按网址挂到已有来源）"
    )
    p.add_argument(
        "--state",
        type=Path,
        action="append",
        help="盘点原始结果 JSON；不给就读清单文件（--out 目录）",
    )
    p.add_argument("--checks", type=Path, action="append", help="coverage-verify 存的试读结果 JSON")
    p.add_argument("--province", action="append", help="只导入某几个省")
    p.set_defaults(func=_cmd_import)

    p = sub.add_parser(
        "coverage-enable", help="某省某级抽查合格后，分批启用试抓成功、待启用的来源（D-122 门禁）"
    )
    p.add_argument("--province", required=True)
    p.add_argument("--level", required=True, choices=("国家级", "省级", "市级", "区县级"))
    p.add_argument("--by", required=True, help="操作人")
    p.add_argument("--limit", type=int, default=2000, help="一次最多启用多少个（每天新启用上限）")
    p.add_argument("--dry-run", action="store_true", help="只数一数，不启用")
    p.set_defaults(func=_cmd_enable)

    p = sub.add_parser("coverage-refresh", help="按来源抓取结果重算覆盖看板每格的状态")
    p.add_argument("--province", help="只算某个省")
    p.add_argument(
        "--trial",
        action="store_true",
        help="试点口径：未启用但试抓成功的也算收录、不按 3 天过期；只打印各状态格数，不写看板",
    )
    p.set_defaults(func=_cmd_refresh)

    p = sub.add_parser(
        "coverage-survey", parents=[common], help="每省集约化平台盘点（平台分布、可读比例、防护）"
    )
    p.add_argument("--state", type=Path, action="append", required=True)
    p.add_argument("--checks", type=Path, action="append")
    p.set_defaults(func=_cmd_survey)
