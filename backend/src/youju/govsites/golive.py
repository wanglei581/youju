"""一批来源上线（D-122、D-126）：导入 → 看板 → 记抽查结论 → 分省打开，一步一核对。

在线上 admin 容器的终端里前台运行（sh deploy/batch1.sh）。控制台贴回来的中文会乱码（10/16 的教训），
所以每一步只打一行纯 ASCII 的结论（如 `step=enable prov=TJ lvl=PROV n=15 expect=15 ok=1`），
最后一行 `batch1_done=1 enabled=<本批个数>`；同时写进 /tmp/<tag>-<时间>.txt（第一行给出文件名）。
要看哪些网址对不上，另写进 /tmp/<tag>-<时间>-detail.txt（中文），结论行里给出文件名；审计行、
意料外的错的经过也写在那里。

- --check：只做能试算的：批次文件和证据表一致、导入试算、抽查结论试算、每省应打开数。
- --apply：按顺序做，每一步先试算、核对，对不上就停（`stop=1 step=…`），不往下走。重跑时已经
  做完的步骤会认出来（导入过的跳过、记过同一份证据的抽查结论不重记、已经打开的不重开）。
- 后台"覆盖看板"里人工记的"没过"（回滚时补记的、上线前先压住的）不盖掉：这个省在批次里就停下，
  不在批次里就不重记它的抽查结论。

每省应打开数从批次文件和证据表现算（批次里这个省这一级、证据表最终结论是"通过"的），不写死。
"""

import csv
import json
import logging
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.crawler.urls import url_key
from youju.govsites.sample import evidence_results, final_verdict, unknown_verdicts
from youju.models import CoverageAcceptance, Source

# 省份简称（GB/T 2260 的两字母写法）：结论行只用 ASCII
ABBR = {
    "国家": "CN",
    "北京": "BJ",
    "天津": "TJ",
    "河北": "HE",
    "山西": "SX",
    "内蒙古": "NM",
    "辽宁": "LN",
    "吉林": "JL",
    "黑龙江": "HL",
    "上海": "SH",
    "江苏": "JS",
    "浙江": "ZJ",
    "安徽": "AH",
    "福建": "FJ",
    "江西": "JX",
    "山东": "SD",
    "河南": "HA",
    "湖北": "HB",
    "湖南": "HN",
    "广东": "GD",
    "广西": "GX",
    "海南": "HI",
    "重庆": "CQ",
    "四川": "SC",
    "贵州": "GZ",
    "云南": "YN",
    "西藏": "XZ",
    "陕西": "SN",
    "甘肃": "GS",
    "青海": "QH",
    "宁夏": "NX",
    "新疆": "XJ",
}
LVL = {"国家级": "NAT", "省级": "PROV", "市级": "CITY", "区县级": "COUNTY"}
ORDER = list(ABBR)
# 按证据表记的抽查结论（coverage-accept 和这里第五步写的）备注里都有这一段；没有的是后台人工记的
EVIDENCE_MARK = "证据 docs/数据来源/全国覆盖/抽查/"


class Stop(Exception):
    """某一步对不上：停下，不往下走。"""


class Report:
    """结论行（ASCII）打到屏幕并写进文件；细节（中文）只写进另一个文件。"""

    def __init__(self, log_dir: Path, tag: str, echo: Callable[[str], None] = print) -> None:
        assert tag.isascii(), tag
        self.tag = tag
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"{tag}-{stamp}.txt"
        self.detail_path = log_dir / f"{tag}-{stamp}-detail.txt"
        self.echo = echo
        self.lines: list[str] = []
        self.step = "start"  # 正在做哪一步：出了意料外的错，停下时报这一步

    def line(self, text: str) -> None:
        assert text.isascii(), text
        self.lines.append(text)
        self.echo(text)
        with self.path.open("a", encoding="ascii") as fh:
            fh.write(text + "\n")

    def detail(self, text: str) -> None:
        with self.detail_path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def _bool(ok: bool) -> int:
    return 1 if ok else 0


def expectations(out: Path, batch: Path, levels: list[str]) -> dict[tuple[str, str], list[str]]:
    """每省每级应打开的栏目：批次里这一省这一级、证据表最终结论是"通过"的（按省份顺序）。"""
    with batch.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["层级"] in levels]
    passed: dict[tuple[str, str], set[str]] = {}
    for level in levels:
        for path in sorted((out / "抽查" / level).glob("*.csv")):
            with path.open(encoding="utf-8-sig", newline="") as fh:
                passed[(level, path.stem)] = {
                    r["栏目URL"] for r in csv.DictReader(fh) if final_verdict(r) == "通过"
                }
    want: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        key = (r["层级"], r["省份"])
        if r["栏目URL"] in passed.get(key, set()):
            want.setdefault(key, []).append(r["栏目URL"])
    return dict(
        sorted(want.items(), key=lambda kv: (levels.index(kv[0][0]), ORDER.index(kv[0][1])))
    )


def _audit(report: Report, action: str, **fields) -> None:
    """审计行照常写日志（stderr）；在容器终端里跑时 stderr 不进云托管日志，所以也写进细节文件。"""
    line = json.dumps({"event": "admin", "action": action, **fields}, ensure_ascii=False)
    logging.getLogger("youju.audit").info(line)
    report.detail(f"审计：{line}")


def _rollback(session: Session, report: Report) -> None:
    """停下前回滚；连接已经断了（查询中途被杀）时回滚自己也会出错，记下来、照样打 stop 行。"""
    try:
        session.rollback()
    except Exception:
        report.detail("回滚出错（连接可能已经断了）：\n" + traceback.format_exc())


def _manual_holds(latest: dict, results: list, level: str) -> dict[str, dict]:
    """最新一条抽查结论是后台人工记的"没过"的省（备注里没有证据表那一段）。"""
    from youju.govsites.boarddb import latest_acceptance

    holds = {}
    for r in results:
        acc = latest_acceptance(latest, r.province, level)
        if acc and not acc["passed"] and EVIDENCE_MARK not in (acc["note"] or ""):
            holds[r.province] = acc
    return holds


def run(
    session: Session,
    out: Path,
    batch_name: str,
    levels: list[str],
    by: str,
    apply: bool,
    report: Report,
    now: datetime | None = None,
    operator: str | None = None,
) -> bool:
    """跑一遍；返回是否走完（--check 走完试算，--apply 全部打开）。对不上时打 stop=1 并返回 False。

    by：抽查人（记进抽查记录）；operator：打开来源的操作人（记进来源备注和审计），默认同 by。"""
    operator = operator or by
    from youju.govsites.boarddb import enable_sources, import_inventory, refresh_states
    from youju.govsites.cli import build_batch
    from youju.sources.importer import import_sources

    batch = out / "批次" / f"{batch_name}.csv"
    try:
        # 一、批次文件和证据表一致（复核改了却忘了重生成批次，这里就停）
        report.step = "batch"
        if not batch.exists():
            report.line("stop=1 step=batch reason=batch_file_missing")
            return False
        with batch.open(encoding="utf-8-sig", newline="") as fh:
            committed = list(csv.reader(fh))[1:]
        plan = build_batch(out, levels)
        unknown = [u for lv in levels for u in unknown_verdicts(out / "抽查" / lv)]
        for u in unknown:
            report.detail(f"复核结论认不出：{u}")
        same = plan.rows == committed
        if not same:
            now_rows = {tuple(r) for r in plan.rows}
            old_rows = {tuple(r) for r in committed}
            for r in sorted(old_rows - now_rows):
                report.detail(f"批次文件里有、按证据表重算没有（或内容变了）：{r[0]} {r[1]} {r[7]}")
            for r in sorted(now_rows - old_rows):
                report.detail(f"按证据表重算有、批次文件里没有（或内容变了）：{r[0]} {r[1]} {r[7]}")
        want = expectations(out, batch, levels)
        total = sum(len(v) for v in want.values())
        ok = same and not unknown and total == len(committed)
        report.line(
            f"step=batch rows={len(committed)} expect={total} provinces={len(want)} "
            f"same_as_evidence={_bool(same)} unknown_verdicts={len(unknown)} ok={_bool(ok)}"
        )
        if not ok:
            raise Stop("batch")

        # 二、导入（只加新的；已经在库里的同一个列表不碰）
        report.step = "import"
        known = {url_key(u) for (u,) in session.execute(select(Source.url))}
        expect_new = sum(1 for r in committed if url_key(r[7]) not in known)
        trial = import_sources(session, batch, only_new=True, dry_run=True)
        for x in trial.existing + trial.near:
            report.detail(f"库里已有：{x}")
        ok = trial.created == expect_new
        report.line(
            f"step=import_check new={trial.created} expect={expect_new} "
            f"existing={len(trial.existing) + len(trial.near)} ok={_bool(ok)}"
        )
        if not ok:
            raise Stop("import_check")
        if apply:
            done = import_sources(session, batch, only_new=True)
            ok = done.created == expect_new
            report.line(f"step=import new={done.created} expect={expect_new} ok={_bool(ok)}")
            if not ok:
                raise Stop("import")

        # 三、抽查结论（按证据表算；合格的要盖住批次里所有省；后台人工记了"没过"的省不开）
        report.step = "accept_check"
        from youju.govsites.boarddb import _latest_acceptances

        results = {lv: evidence_results(out / "抽查" / lv) for lv in levels}
        latest = _latest_acceptances(session)
        holds = {lv: _manual_holds(latest, results[lv], lv) for lv in levels}
        for lv in levels:
            accepted = {r.province for r in results[lv] if r.accepted}
            need = {p for (lvl, p) in want if lvl == lv}
            blocking = need & holds[lv].keys()
            ok = need <= accepted and not blocking
            held = len(results[lv]) - len(accepted)
            for p, acc in sorted(holds[lv].items()):
                report.detail(
                    f"{lv} {p} 后台记了没过（抽查记录 #{acc['id']}，{acc['sampled_by']}，"
                    f"{acc['sampled_at']}）：{acc['note'] or ''}"
                    + (
                        "——在本批里，停下：要放先在看板记合格或重新抽查"
                        if p in need
                        else "——不在本批，不盖掉"
                    )
                )
            report.line(
                f"step=accept_check lvl={LVL[lv]} accepted={len(accepted)} held={held} "
                f"batch_provinces={len(need)} manual_holds={len(holds[lv])} ok={_bool(ok)}"
            )
            if not ok:
                for p in sorted(need - accepted):
                    report.detail(f"批次里有、抽查没合格：{lv} {p}")
                raise Stop("accept_check")
        if not apply:
            for (lv, prov), urls in want.items():
                report.line(
                    f"step=enable_expect prov={ABBR[prov]} lvl={LVL[lv]} expect={len(urls)}"
                )
            report.line(f"check_done=1 expect_total={total}")
            return True

        # 四、看板：全国清单写进看板表并重算（只动清单里有门户的行政区）
        report.step = "coverage_import"
        from youju.govsites.divisions import read_csv
        from youju.govsites.inventory import load_docs

        portals, depts = load_docs(out, None)
        divisions = out / "行政区划.csv"
        meta = (
            {d.code: (d.kind, d.parent_code) for d in read_csv(divisions)}
            if divisions.exists()
            else {}
        )
        cells = 0
        for prov in sorted({p.province for p in portals}):
            part = import_inventory(
                session,
                [p for p in portals if p.province == prov],
                [d for d in depts if d.province == prov],
                {},
                meta,
            )
            cells += part.cells_created + part.cells_updated
            refresh_states(session, now=now, province=prov)
        ok = cells > 0
        report.line(f"step=coverage_import divisions={len(portals)} cells={cells} ok={_bool(ok)}")
        if not ok:
            raise Stop("coverage_import")

        # 五、记抽查结论：同一份证据（备注里的 sha1 相同）、同一结论已经记过的不重记；
        # 后台人工记的"没过"不盖掉。写之前按最新的记录再查一遍：第四步看板导入要一两分钟，这期间
        # 有人在看板记了"没过"，在本批里的省就停（不然这里补记"合格"会盖过它、第六步照开）
        report.step = "accept"
        from youju.govsites.boarddb import latest_acceptance
        from youju.govsites.cli import _sha1

        for lv in levels:
            written = skipped = kept = 0
            latest = _latest_acceptances(session)
            fresh = _manual_holds(latest, results[lv], lv)
            need = {p for (lvl, p) in want if lvl == lv}
            late = sorted((need & fresh.keys()) - holds[lv].keys())
            for p in late:
                acc = fresh[p]
                report.detail(
                    f"{lv} {p} 看板导入期间后台记了没过（抽查记录 #{acc['id']}，"
                    f"{acc['sampled_by']}）：{acc['note'] or ''}——在本批里，停下"
                )
            if late:
                raise Stop("accept")
            for r in results[lv]:
                if r.province in fresh:
                    kept += 1
                    continue
                sha = _sha1(out / "抽查" / lv / f"{r.province}.csv")
                acc = latest_acceptance(latest, r.province, lv)
                if (
                    acc
                    and acc["level"] == lv
                    and acc["passed"] == r.accepted
                    and sha in (acc["note"] or "")
                ):
                    skipped += 1
                    continue
                session.add(
                    CoverageAcceptance(
                        province=r.province,
                        level=lv,
                        sampled_by=by,
                        sampled_at=now or datetime.now(UTC),
                        sample_size=r.total,
                        passed=r.accepted,
                        note=(
                            f"全检 {r.total} 个栏目：通过 {r.passed}，不通过 {r.failed}，"
                            f"待重试 {r.retry}，不能抓 {r.blocked}，"
                            f"不收 {r.excluded}（都不进本批）；"
                            f"证据 docs/数据来源/全国覆盖/抽查/{lv}/{r.province}.csv（sha1 {sha}）"
                        )[:300],
                    )
                )
                written += 1
            session.commit()
            _audit(
                report,
                "coverage_accept",
                by=by,
                level=lv,
                results={r.province: r.accepted for r in results[lv] if r.province not in fresh},
            )
            report.line(
                f"step=accept lvl={LVL[lv]} written={written} already={skipped} "
                f"kept_manual={kept} ok=1"
            )

        # 六、分省打开：先试算，个数对上再开；已经打开的算进 done_before
        report.step = "enable"
        for (lv, prov), urls in want.items():
            keys = {url_key(u) for u in urls}
            done_before = sum(
                1
                for (u, on) in session.execute(select(Source.url, Source.enabled))
                if on and url_key(u) in keys
            )
            expect = len(urls) - done_before
            trial = enable_sources(
                session, prov, lv, operator, dry_run=True, verified=True, only_urls=urls, now=now
            )
            missing = [m for m in trial.missing if "已经在收" not in m]
            for m in missing:
                report.detail(f"{lv} {prov} 批次里有、开不了：{m}")
            ok = trial.blocked_reason is None and trial.enabled == expect and not missing
            report.line(
                f"step=enable_check prov={ABBR[prov]} lvl={LVL[lv]} n={trial.enabled} "
                f"expect={expect} done_before={done_before} missing={len(missing)} ok={_bool(ok)}"
            )
            if not ok:
                raise Stop("enable_check")
            if expect:
                real = enable_sources(
                    session, prov, lv, operator, verified=True, only_urls=urls, now=now
                )
                ok = real.enabled == expect
                _audit(
                    report,
                    "coverage_enable",
                    by=operator,
                    province=prov,
                    level=lv,
                    acceptance_id=real.acceptance_id,
                    verified=True,
                    batch=str(batch),
                    source_ids=real.source_ids,
                )
                report.line(
                    f"step=enable prov={ABBR[prov]} lvl={LVL[lv]} n={real.enabled} "
                    f"expect={expect} ok={_bool(ok)}"
                )
                if not ok:
                    raise Stop("enable")
                refresh_states(session, now=now, province=prov)
        enabled_now = sum(
            1
            for (u, on) in session.execute(select(Source.url, Source.enabled))
            if on and url_key(u) in {url_key(x) for urls in want.values() for x in urls}
        )
        ok = enabled_now == total
        report.line(f"{report.tag}_done={_bool(ok)} enabled={enabled_now} expect={total}")
        return ok
    except Stop as exc:
        _rollback(session, report)
        report.line(f"stop=1 step={exc} detail={report.detail_path.name}")
        return False
    except Exception as exc:  # 意料外的错：也打一行 stop，出错经过写进细节文件，不只留在终端
        report.detail(traceback.format_exc())
        _rollback(session, report)
        report.line(
            f"stop=1 step={report.step} error={type(exc).__name__} detail={report.detail_path.name}"
        )
        return False
