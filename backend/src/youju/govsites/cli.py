"""youju coverage-* 命令：生成全国覆盖清单、试跑栏目、导出接入清单、统计覆盖。"""

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[4] / "docs" / "数据来源" / "全国覆盖"
DEFAULT_CACHE = Path.home() / ".cache" / "youju-coverage"
LEVEL_ORDER = ("国家级", "省级", "市级", "区县级")
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
    levels = set(args.levels or [])
    max_level = args.max_level
    if levels:
        # 只写这几级：走到最深的那一级就停（国家级、省级都只用走到省级）
        deepest = max(levels, key=LEVEL_ORDER.index)
        max_level = "省级" if deepest in ("国家级", "省级") else deepest
        if args.max_level != "区县级":
            print(f"给了 --levels，走到哪一级按它定（{max_level}），--max-level 不用")
    elif args.max_level != "区县级":
        # 只给 --max-level：只换走到的这几级，更下级的行照旧（几条线并行时不冲掉别人的）
        levels = set(LEVEL_ORDER[: LEVEL_ORDER.index(args.max_level) + 1])
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
            max_level=max_level,
            workers=args.workers,
            per_province=args.per_province,
            progress=progress,
            seeds=load_seeds(args.seeds),
            dept_levels=levels or None,
        )
        inv.run()
    # 只写某几级时原始结果另存一份（文件名带层级），不覆盖全量的那份
    tag = "_".join(args.province) if args.province else "全国"
    if levels:
        tag += "-" + "_".join(lv for lv in LEVEL_ORDER if lv in levels)
    state = args.cache / f"state-{tag}.json"
    save_state(inv, state, levels or None)
    portals, depts = list(inv.portals.values()), inv.depts
    if levels:
        # 只写这几级：上级的门户行没找部门，不进清单、不补"导航未找到"的格
        portals = [p for p in portals if p.level in levels]
        depts = [d for d in depts if d.level in levels]
    summary = write_outputs(index, portals, depts, args.out, args.province, levels or None)
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
    from youju.govsites.report import (
        COLUMN_FIELDS,
        column_key,
        column_rows,
        dedupe_column_rows,
        merge_level_rows,
        verify_summary,
    )
    from youju.govsites.verify import check_columns, columns_for

    _, depts = _load_states(args.state)
    if args.province:
        depts = [d for d in depts if d.province in args.province]
    # 高一级先占网址（省厅栏目也挂在区县页上时算省级），再按 --level 过滤
    cols = columns_for(depts, set(args.level) if args.level else None)
    scope = [d for d in depts if d.level in args.level] if args.level else depts
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
    if args.level:
        # 只试读某几级：范围里的每个省都写（这一级一个栏目也没有也要把旧行换掉），其他级的行照旧
        for d in scope:
            by_prov.setdefault(d.province, [])
    sub = "栏目抽样试读" if args.sample_per_level else "栏目清单"
    for prov, group in by_prov.items():
        path = args.out / sub / f"{prov}.csv"
        rows = column_rows(group, today)
        if args.level:
            rows = merge_level_rows(path, COLUMN_FIELDS, rows, set(args.level), column_key)
            rows = dedupe_column_rows(rows)  # 别级旧行里同一网址的，留最高一级那行
        write_table(path, COLUMN_FIELDS, rows)
    tag = ("抽样-" if args.sample_per_level else "") + (
        "_".join(args.province) if args.province else "全国"
    )
    if args.level:
        tag += "-" + "_".join(lv for lv in LEVEL_ORDER if lv in args.level)
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

    if args.verified and not args.batch:
        print("--verified 要和 --batch 一起用：只开批次文件里、证据表复核通过的栏目")
        return 1
    with session_factory()() as session, board_lock(session, BOARD_LOCK_WAIT) as got:
        if not got:
            print(BOARD_BUSY)
            return 1
        r = enable_sources(
            session,
            args.province,
            args.level,
            args.by,
            limit=args.limit,
            dry_run=args.dry_run,
            verified=args.verified,
            only_urls=_batch_urls(args.batch, args.province, args.level) if args.batch else None,
        )
        _report_enable(args, r)
        if not args.dry_run and r.enabled:
            refresh_states(session, province=args.province)  # 启用的格马上按正式口径算
    return 1 if r.blocked_reason else 0


def _batch_urls(path: Path, province: str, level: str) -> list[str]:
    """批次文件里这个省这一级的栏目，再按证据表的最终结论过滤一遍：批次生成以后复核又改成
    "不通过"的（比如创始人复看时写的），这里就不开。证据表在批次文件旁边的 抽查/<层级>/<省>.csv。"""
    import csv

    from youju.govsites.sample import final_verdict

    with path.open(encoding="utf-8-sig", newline="") as fh:
        urls = [
            r["栏目URL"] for r in csv.DictReader(fh) if r["省份"] == province and r["层级"] == level
        ]
    evidence = path.parent.parent / "抽查" / level / f"{province}.csv"
    if not evidence.exists():
        print(f"没有证据表 {evidence}，一个都不开")
        return []
    with evidence.open(encoding="utf-8-sig", newline="") as fh:
        ok = {r["栏目URL"] for r in csv.DictReader(fh) if final_verdict(r) == "通过"}
    dropped = [u for u in urls if u not in ok]
    if dropped:
        print(
            f"批次里有、但证据表最终结论不是通过的 {len(dropped)} 个，不开：" + "；".join(dropped)
        )
    return [u for u in urls if u in ok]


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
    if r.missing:
        print(f"批次里有、这次没开的 {len(r.missing)} 个：" + "；".join(r.missing))
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
                    "verified": args.verified,
                    "batch": str(args.batch) if args.batch else None,
                    "source_ids": r.source_ids,
                },
                ensure_ascii=False,
            )
        )


def _cmd_sample(args: argparse.Namespace) -> int:
    import csv

    from youju.db import session_factory
    from youju.govsites.inventory import write_table
    from youju.govsites.sample import (
        SAMPLE_FIELDS,
        counties_by_city,
        sample_rows,
        summary_lines,
        to_csv_row,
    )

    # 地市部门核对要认下辖区县（"南京市玄武区……"不算南京市的部门）：没有行政区划清单就不抽
    counties = {}
    if args.level == "市级":
        if not args.divisions.exists():
            print(
                f"找不到行政区划清单 {args.divisions}：地市部门核对要用它认下辖区县。"
                "先用 coverage-divisions 取一份，或用 --divisions 指定"
            )
            return 1
        counties = counties_by_city(args.divisions)
    with session_factory()() as session:
        rows = sample_rows(session, args.level, args.province, args.size, counties=counties)
    if not rows:
        print("库里没有这一级的来源（先导入、试抓）")
        return 1
    base = args.out / "抽查" / args.level
    by_prov: dict[str, list] = {}
    for r in rows:
        by_prov.setdefault(r.source.province, []).append(r)
    review = SAMPLE_FIELDS.index("复核人")
    for prov, group in by_prov.items():
        path = base / f"{prov}.csv"
        # 重跑时保留已经填了的人工复核（按栏目网址对上）
        kept: dict[str, list[str]] = {}
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as fh:
                for old in csv.DictReader(fh):
                    kept[old["栏目URL"]] = [old["复核人"], old["复核结论"], old["复核备注"]]
        out = []
        for r in group:
            line = to_csv_row(r)
            line[review:] = kept.get(r.source.url, line[review:])
            out.append(line)
        write_table(path, SAMPLE_FIELDS, out)
    text = "\n".join(summary_lines(rows))
    print(text)
    print(f"证据表写在 {base}/<省>.csv；最后三列留给人工复核")
    return 0


def _sha1(path: Path) -> str:
    import hashlib

    return hashlib.sha1(path.read_bytes()).hexdigest()[:12] if path.exists() else "无"


def _cmd_accept(args: argparse.Namespace) -> int:
    """按证据表记每省抽查结论（D-122、D-126）：上线时由操作人在线上运行，写抽查记录。"""
    from datetime import UTC, datetime

    from youju.db import session_factory
    from youju.govsites.sample import PASS_RATE, evidence_results, unknown_verdicts
    from youju.models import CoverageAcceptance

    folder = args.out / "抽查" / args.level
    for line in unknown_verdicts(folder):
        print(f"复核结论认不出（这个省先不合格）：{line}")
    results = [
        r for r in evidence_results(folder) if not args.province or r.province in args.province
    ]
    if not results:
        print(f"{folder} 里没有证据表")
        return 1
    print(
        f"合格标准：每个栏目都有结论，读得准的（通过、不收）占 {PASS_RATE:.0%} 以上"
        "（待重试、不能抓不算进分母；只有通过的进本批）"
    )
    for r in results:
        verdict = "合格" if r.accepted else "没过"
        print(
            f"{r.province}：{verdict}（{r.total} 个栏目，通过 {r.passed}，不通过 {r.failed}，"
            f"待重试 {r.retry}，不能抓 {r.blocked}，不收 {r.excluded}，没复核 {r.pending}，"
            f"读得准的 {r.rate:.0%}）"
        )
    if args.dry_run:
        print("试算，没写库")
        return 0
    level = "国家级" if args.level == "国家级" else args.level
    with session_factory()() as session:
        for r in results:
            session.add(
                CoverageAcceptance(
                    province=r.province,
                    level=level,
                    sampled_by=args.by,
                    sampled_at=datetime.now(UTC),
                    sample_size=r.total,
                    passed=r.accepted,
                    note=(
                        f"全检 {r.total} 个栏目：通过 {r.passed}，不通过 {r.failed}，"
                        f"待重试 {r.retry}，不能抓 {r.blocked}，不收 {r.excluded}（都不进本批）；"
                        f"证据 docs/数据来源/全国覆盖/抽查/{args.level}/{r.province}.csv"
                        f"（sha1 {_sha1(folder / f'{r.province}.csv')}）"
                    )[:300],
                )
            )
        session.commit()
    import logging

    logging.getLogger("youju.audit").info(
        json.dumps(
            {
                "event": "admin",
                "action": "coverage_accept",
                "by": args.by,
                "level": args.level,
                "results": {r.province: r.accepted for r in results},
            },
            ensure_ascii=False,
        )
    )
    print(f"已记 {len(results)} 条抽查结论")
    return 0


# 以前导入过线上的来源清单（docs/数据来源/ 下）。候选清单（如 地市来源候选_R1.5_v1.csv）
# 还没接入，不算
LIVE_LISTS = (
    "全国来源清单_P0_v1.csv",
    "地市来源接入_2026-10-15.csv",
    "区县来源接入_2026-10-15.csv",
)


@dataclass
class BatchPlan:
    rows: list
    already: list[str]  # 线上已在收（启用）的同一个列表
    held: list[str]  # 线上清单里有、但没启用的（要渲染、访问控制等），不进本批
    missing: list[str]  # 证据表里通过、栏目清单里对不上的
    unknown: list[str]  # 复核结论认不出的
    skipped_files: list[str] = field(default_factory=list)  # 表头和现在不一样、没读的栏目清单


def build_batch(out: Path, levels: list[str]) -> BatchPlan:
    """一批的导入行：只收抽查合格省份里、最终结论是"通过"的试读可读栏目，网址不重复。

    按（层级、省、栏目网址）对证据表和栏目清单；线上来源清单里已有的同一个列表（规范网址相同，
    只差 http/https 等）不进本批，按那份清单的验证状态分成"已在收"和"有但没启用"。"""
    import csv

    from youju.crawler.urls import url_key
    from youju.govsites.report import COLUMN_FIELDS
    from youju.govsites.sample import evidence_results, final_verdict, unknown_verdicts
    from youju.sources.importer import mode_for

    passed: set[tuple[str, str, str]] = set()  # (层级, 省份, 栏目网址)
    accepted: set[tuple[str, str]] = set()  # (层级, 省份)
    unknown: list[str] = []
    for level in levels:
        folder = out / "抽查" / level
        unknown += unknown_verdicts(folder)
        accepted |= {(level, r.province) for r in evidence_results(folder) if r.accepted}
        for path in sorted(folder.glob("*.csv")):
            with path.open(encoding="utf-8-sig", newline="") as fh:
                passed |= {
                    (level, r["省份"], r["栏目URL"])
                    for r in csv.DictReader(fh)
                    if final_verdict(r) == "通过"
                }
    live: dict[str, tuple[str, bool]] = {}  # 规范网址 → (说明, 线上是否启用)
    for name in LIVE_LISTS:
        path = out.parent / name
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                url = (r.get("栏目URL") or "").strip()
                if url:
                    _, on = mode_for((r.get("验证状态") or "").strip())
                    live[url_key(url)] = (f"{name}：{url}", on)
    plan = BatchPlan([], [], [], [], unknown)
    seen: set[str] = set()
    matched: set[tuple[str, str, str]] = set()
    for path in sorted((out / "栏目清单").glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            if next(reader, None) != list(COLUMN_FIELDS):
                plan.skipped_files.append(str(path))
                continue
            for row in reader:
                level, prov, url, status = row[0], row[1], row[7], row[11]
                key = (level, prov, url)
                if level not in levels or (level, prov) not in accepted or key not in passed:
                    continue
                matched.add(key)
                if not status.startswith("试跑可读") or url in seen:
                    continue
                if url_key(url) in live:
                    where, on = live[url_key(url)]
                    (plan.already if on else plan.held).append(f"{prov} {url}（{where}）")
                    continue
                seen.add(url)
                plan.rows.append(row)
    plan.missing = sorted(k[2] for k in passed if (k[0], k[1]) in accepted and k not in matched)
    return plan


def _cmd_batch(args: argparse.Namespace) -> int:
    """写一批的导入文件（批次/<名字>.csv）。"""
    from collections import Counter

    from youju.govsites.inventory import write_table
    from youju.govsites.report import COLUMN_FIELDS

    plan = build_batch(args.out, args.level)
    for line in plan.unknown:
        print(f"复核结论认不出（这个省先不合格）：{line}")
    for path in plan.skipped_files:
        print(f"{path} 的表头和现在的不一样，没读（要全量重跑这个省）")
    out = args.out / "批次" / f"{args.name}.csv"
    write_table(out, COLUMN_FIELDS, plan.rows)
    by_prov = Counter(r[1] for r in plan.rows)
    print(f"写入 {out}：{len(plan.rows)} 个栏目，{len(by_prov)} 个省（含国家级）")
    print("，".join(f"{p} {n}" for p, n in by_prov.most_common()))
    if plan.already:
        print(f"线上已在收、不进本批的 {len(plan.already)} 个：" + "；".join(plan.already))
    if plan.held:
        print(
            f"线上清单里有、但没启用（要渲染、访问控制等）的 {len(plan.held)} 个，不进本批："
            + "；".join(plan.held)
        )
    if plan.missing:
        print(
            f"证据表里通过、栏目清单里对不上的 {len(plan.missing)} 个：" + "；".join(plan.missing)
        )
    return 0


def _cmd_golive(args: argparse.Namespace) -> int:
    """一批来源上线（见 govsites/golive.py）：结论行纯 ASCII，写进 <log-dir>/<tag>-<时间>.txt。"""
    from youju.db import session_factory
    from youju.govsites.boarddb import board_lock
    from youju.govsites.golive import Report, run

    if not str(args.log_dir).isascii():
        print("--log-dir 要用纯 ASCII 的路径（结论行纯 ASCII）")
        return 2
    args.log_dir.mkdir(parents=True, exist_ok=True)
    report = Report(args.log_dir, args.tag)
    report.line(f"log={report.path}")  # 第一行就给出文件名：最后一行留给结论
    with session_factory()() as session, board_lock(session, BOARD_LOCK_WAIT) as got:
        if not got:
            report.line("stop=1 step=lock reason=another_board_command_running")
            return 1
        ok = run(
            session,
            args.out,
            args.batch,
            args.level,
            args.by,
            args.apply,
            report,
            operator=args.operator,
        )
    return 0 if ok else 1


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
    p.add_argument(
        "--levels",
        action="append",
        choices=LEVEL_ORDER,
        help="只写这几级的行（可重复），清单文件里其他级的行照旧：几条线分级并行时用",
    )
    p.add_argument("--workers", type=int, default=96)
    p.add_argument("--per-province", type=int, default=6, help="同一省同时处理几个行政区")
    p.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    p.add_argument(
        "--seeds",
        type=Path,
        default=DEFAULT_OUT / "导航入口补充",
        help="导航入口补充（门户不通或导航由脚本生成时用的官方入口页）：目录或单个 CSV",
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
    p.add_argument(
        "--verified",
        action="store_true",
        help="也开这台库里还没抓过、但试读可读的来源（本机试抓、抽查合格后在线上第一次打开时用）",
    )
    p.add_argument(
        "--batch",
        type=Path,
        help="只开这份批次文件里这个省这一级的栏目（批次/<名字>.csv），报出批次里有、这次没开的",
    )
    p.set_defaults(func=_cmd_enable)

    p = sub.add_parser(
        "coverage-sample", help="每省抽查的证据表：抽到的栏目、抓取结果、最新 3 条和自动核对"
    )
    p.add_argument("--level", required=True, choices=LEVEL_ORDER)
    p.add_argument("--province", action="append", help="只抽某几个省")
    p.add_argument("--size", type=int, help="每省抽几个（不给就全查）")
    p.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="清单目录（证据写进 抽查/<层级>/）"
    )
    p.add_argument(
        "--divisions", type=Path, default=DEFAULT_OUT / "行政区划.csv", help="行政区划清单"
    )
    p.set_defaults(func=_cmd_sample)

    p = sub.add_parser(
        "coverage-accept", help="按证据表记每省抽查结论（上线时在线上运行，写抽查记录）"
    )
    p.add_argument("--level", required=True, choices=LEVEL_ORDER)
    p.add_argument("--by", required=True, help="操作人")
    p.add_argument("--province", action="append", help="只记某几个省")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="清单目录（读 抽查/<层级>/）")
    p.add_argument("--dry-run", action="store_true", help="只算结论，不写库")
    p.set_defaults(func=_cmd_accept)

    p = sub.add_parser(
        "coverage-batch", help="生成一批的导入文件：只收抽查合格省份里复核通过的栏目"
    )
    p.add_argument("--level", action="append", required=True, choices=LEVEL_ORDER)
    p.add_argument("--name", required=True, help="文件名（写到 批次/<名字>.csv）")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="清单目录")
    p.set_defaults(func=_cmd_batch)

    p = sub.add_parser(
        "coverage-golive",
        help="一批来源上线：导入→看板→记抽查结论→分省打开，一步一核对，结论行纯 ASCII",
    )
    p.add_argument("--batch", required=True, help="批次文件名（批次/<名字>.csv）")
    p.add_argument("--level", action="append", required=True, choices=LEVEL_ORDER)
    p.add_argument("--by", required=True, help="抽查人（记进抽查记录）")
    p.add_argument("--operator", help="打开来源的操作人（记进来源备注和审计），默认同 --by")
    p.add_argument("--tag", default="batch1", help="日志文件名和最后一行的前缀（只用 ASCII）")
    p.add_argument("--log-dir", type=Path, default=Path("/tmp"), help="结论和细节写到哪个目录")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="清单目录")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="只试算：批次、导入、抽查结论、每省应打开数"
    )
    mode.add_argument(
        "--apply", action="store_true", help="按顺序做，每一步先试算、核对，对不上就停"
    )
    p.set_defaults(func=_cmd_golive)

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
