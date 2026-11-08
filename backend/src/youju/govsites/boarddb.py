"""覆盖看板落库：把盘点结果写进 coverage_* 表、按来源抓取结果重算每格状态、按级汇总、记按省抽查。

- 导入（coverage-import）：行政区和格按键（区划代码；区划 + 部门 + 序号）更新，不动人工确认的内容；
  格里的栏目按网址挂到已有来源（网址相同就是同一个来源）；
- 重算（coverage-refresh）：规则和离线统计同一份（board.cell_for），只读 sources、source_checks、
  documents；人工确认"网站无此栏目"的照人工；
- 汇总：每级、每省各状态格数和覆盖率（人工确认过的"网站无此栏目"和"不能抓"不算缺口）。
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from youju.govsites.board import STATES, SourceState, cell_for
from youju.govsites.inventory import STANDARD_DEPTS, ColumnHit, DeptRow, PortalRow
from youju.models import (
    CoverageAcceptance,
    CoverageCell,
    CoverageCellSource,
    CoverageDivision,
    Document,
    Source,
    SourceCheck,
)

BOARD_STATES = STATES + ("待启用",)
LEVELS = ("国家级", "省级", "市级", "区县级")
_KINDS = {"国家级": "国务院", "省级": "省", "市级": "地级市", "区县级": "县"}


@dataclass
class ImportSummary:
    divisions: int = 0
    cells_created: int = 0
    cells_updated: int = 0
    cells_removed: int = 0
    links: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def _clip(value: str | None, size: int) -> str | None:
    return value[:size] if value else value


def import_inventory(
    session: Session,
    portals: Iterable[PortalRow],
    depts: Iterable[DeptRow],
    checks: dict[str, object] | None = None,
    divisions_meta: dict[str, tuple[str, str | None]] | None = None,
) -> ImportSummary:
    """盘点结果写进看板表。divisions_meta：区划代码 → (类型, 上级代码)，来自行政区划清单。"""
    summary = ImportSummary()
    checks = checks or {}
    divisions_meta = divisions_meta or {}
    stamp = _now()
    portals = list(portals)
    existing_div = {d.code: d for d in session.scalars(select(CoverageDivision))}
    for p in portals:
        kind, parent = divisions_meta.get(p.code, (_KINDS.get(p.level, ""), None))
        div = existing_div.get(p.code)
        if div is None:
            div = CoverageDivision(code=p.code)
            session.add(div)
        div.name, div.kind, div.level, div.province = p.name, kind, p.level, p.province
        div.city, div.district, div.parent_code = p.city or None, p.district or None, parent
        div.portal_status = p.status
        div.portal_url = _clip(p.portal_url, 512) or None
        div.portal_reason = _clip(p.reason, 300) or None
        div.portal_nav_source = _clip(p.nav_source, 512) or None
        div.site_id_code = _clip(p.site_id_code, 20) or None
        div.platform = _clip(p.platform, 64) or None
        div.inventoried_at = stamp
        summary.divisions += 1
    session.flush()

    # 同一行政区同一部门的第几个（开发区可有几个）
    seen: Counter[tuple[str, str]] = Counter()
    wanted: dict[tuple[str, str, int], DeptRow] = {}
    for d in depts:
        key = (d.code, d.dept)
        wanted[(d.code, d.dept, seen[key])] = d
        seen[key] += 1
    codes = {p.code for p in portals}
    cells = {
        (c.division_code, c.dept, c.seq): c
        for c in session.scalars(select(CoverageCell).where(CoverageCell.division_code.in_(codes)))
    }
    url_to_source = _sources_by_url(
        session, {col.url for d in wanted.values() for col in d.columns}
    )
    for key, d in wanted.items():
        cell = cells.get(key)
        if cell is None:
            cell = CoverageCell(division_code=key[0], dept=key[1], seq=key[2])
            cell.state, cell.confirmed_no_column = "需人工", False
            session.add(cell)
            summary.cells_created += 1
        else:
            summary.cells_updated += 1
        cell.required = d.dept in STANDARD_DEPTS
        cell.dept_full_name = _clip(d.dept_full_name, 128) or None
        cell.site_url = _clip(d.site_url, 512) or None
        cell.site_type = d.site_type or None
        cell.nav_source = _clip(d.nav_source, 512) or None
        cell.inventory_status = d.status
        cell.inventory_reason = _clip(d.reason, 300) or None
        cell.columns = [
            {
                "kind": c.kind,
                "name": c.name,
                "url": c.url,
                "source": c.source,
                # 试读结论（coverage-verify）：没导入成来源的栏目，看板据此说明原因
                "result": getattr(checks.get(c.url), "result", "") or "",
                "reason": getattr(checks.get(c.url), "reason", "") or "",
            }
            for c in d.columns
        ]
    session.flush()
    # 盘点里已经没有的格：没人工动过的删掉，人工确认过的留着
    for key, cell in cells.items():
        if key in wanted:
            continue
        if cell.confirmed_no_column or cell.alt_source or cell.note:
            cell.inventory_status, cell.inventory_reason = (
                "导航未找到",
                "这次盘点没找到（保留人工记录）",
            )
        else:
            session.delete(cell)
            summary.cells_removed += 1
    session.flush()
    # 格 ↔ 来源：按网址重挂
    cell_ids = [
        c.id
        for c in session.scalars(select(CoverageCell).where(CoverageCell.division_code.in_(codes)))
    ]
    if cell_ids:
        session.execute(delete(CoverageCellSource).where(CoverageCellSource.cell_id.in_(cell_ids)))
    for cell in session.scalars(select(CoverageCell).where(CoverageCell.division_code.in_(codes))):
        linked: set[int] = set()
        for col in cell.columns or []:
            source_id = url_to_source.get(col.get("url", ""))
            if source_id and source_id not in linked:
                linked.add(source_id)
                session.add(
                    CoverageCellSource(cell_id=cell.id, source_id=source_id, kind=col["kind"])
                )
                summary.links += 1
    session.commit()
    return summary


def _sources_by_url(session: Session, urls: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    chunk = list(urls)
    for i in range(0, len(chunk), 500):
        part = chunk[i : i + 500]
        for sid, url in session.execute(select(Source.id, Source.url).where(Source.url.in_(part))):
            out[url] = sid
    return out


def _source_states(session: Session, source_ids: set[int]) -> dict[int, SourceState]:
    """来源的启用、健康、最后成功、最后一次检查的结果和条目数、名下文件数。"""
    if not source_ids:
        return {}
    ids = list(source_ids)
    latest = (
        select(SourceCheck.source_id, func.max(SourceCheck.id).label("last_id"))
        .where(SourceCheck.source_id.in_(ids))
        .group_by(SourceCheck.source_id)
        .subquery()
    )
    last = {
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
    docs = dict(
        session.execute(
            select(Document.source_id, func.count())
            .where(Document.source_id.in_(ids))
            .group_by(Document.source_id)
        ).all()
    )
    out: dict[int, SourceState] = {}
    for s in session.scalars(select(Source).where(Source.id.in_(ids))):
        status, http, items = last.get(s.id, (None, None, 0))
        out[s.id] = SourceState(
            s.url,
            s.enabled,
            s.fetch_mode,
            s.health,
            s.last_success_at,
            s.last_error,
            s.verification_status,
            docs.get(s.id, 0),
            status,
            http,
            items or 0,
        )
    return out


@dataclass
class _Check:
    result: str
    reason: str


def refresh_states(
    session: Session, now: datetime | None = None, trial: bool = False, province: str | None = None
) -> Counter:
    """按来源抓取结果重算每格状态；状态变了记 state_changed_at。返回各状态格数。"""
    now = now or _now()
    stmt = select(CoverageCell, CoverageDivision).join(
        CoverageDivision, CoverageDivision.code == CoverageCell.division_code
    )
    if province:
        stmt = stmt.where(CoverageDivision.province == province)
    rows = session.execute(stmt).all()
    links: dict[int, list[int]] = defaultdict(list)
    cell_ids = [cell.id for cell, _ in rows]
    for i in range(0, len(cell_ids), 1000):
        for cid, sid in session.execute(
            select(CoverageCellSource.cell_id, CoverageCellSource.source_id).where(
                CoverageCellSource.cell_id.in_(cell_ids[i : i + 1000])
            )
        ):
            links[cid].append(sid)
    states = _source_states(session, {sid for sids in links.values() for sid in sids})
    counts: Counter = Counter()
    for cell, div in rows:
        columns = cell.columns or []
        row = DeptRow(
            div.code,
            div.level,
            div.province,
            div.city or "",
            div.district or "",
            cell.dept,
            dept_full_name=cell.dept_full_name or "",
            site_url=cell.site_url or "",
            site_type=cell.site_type or "",
            nav_source=cell.nav_source or "",
            status=cell.inventory_status,
            reason=cell.inventory_reason or "",
            columns=[
                ColumnHit(c["kind"], c["name"], c["url"], c.get("source", "")) for c in columns
            ],
        )
        sources = {states[sid].url: states[sid] for sid in links.get(cell.id, []) if sid in states}
        checks = {
            c["url"]: _Check(c.get("result", ""), c.get("reason", ""))
            for c in columns
            if c.get("result")
        }
        result = cell_for(row, sources, checks, now, trial)
        state, reason = result.state, result.reason
        if cell.confirmed_no_column and state in ("网站无此栏目", "需人工"):
            state, reason = "网站无此栏目", f"人工确认（{cell.confirmed_by or ''}）"
        if state == "不能抓" and cell.alt_source:
            reason = f"{reason}；替代来源：{cell.alt_source}"
        if state != cell.state:
            cell.state_changed_at = now
        cell.state, cell.reason = state, _clip(reason, 300)
        cell.last_success_at = result.last_success_at
        counts[state] += 1
    session.commit()
    return counts


def summary(session: Session, level: str | None = None) -> dict:
    """每级、每省各状态格数和覆盖率；带上按省抽查的最新结论。"""
    stmt = (
        select(
            CoverageDivision.level,
            CoverageDivision.province,
            CoverageCell.state,
            CoverageCell.confirmed_no_column,
            func.count(),
        )
        .join(CoverageDivision, CoverageDivision.code == CoverageCell.division_code)
        .group_by(
            CoverageDivision.level,
            CoverageDivision.province,
            CoverageCell.state,
            CoverageCell.confirmed_no_column,
        )
    )
    if level:
        stmt = stmt.where(CoverageDivision.level == level)
    by_prov: dict[tuple[str, str], Counter] = defaultdict(Counter)
    confirmed: Counter[tuple[str, str]] = Counter()
    for lv, prov, state, conf, n in session.execute(stmt):
        by_prov[(lv, prov)][state] += n
        if conf and state == "网站无此栏目":
            confirmed[(lv, prov)] += n
    accepted = _latest_acceptances(session)
    provinces = []
    totals: dict[str, Counter] = defaultdict(Counter)
    total_conf: Counter[str] = Counter()
    for (lv, prov), cnt in sorted(
        by_prov.items(), key=lambda kv: (LEVELS.index(kv[0][0]), kv[0][1])
    ):
        totals[lv].update(cnt)
        total_conf[lv] += confirmed[(lv, prov)]
        acc = accepted.get((prov, lv)) or accepted.get((prov, "全部"))
        provinces.append(
            {
                "level": lv,
                "province": prov,
                "counts": {s: cnt.get(s, 0) for s in BOARD_STATES},
                "total": sum(cnt.values()),
                "rate": rate(cnt, confirmed[(lv, prov)]),
                "acceptance": acc,
            }
        )
    levels = {
        lv: {
            "counts": {s: cnt.get(s, 0) for s in BOARD_STATES},
            "total": sum(cnt.values()),
            "rate": rate(cnt, total_conf[lv]),
        }
        for lv, cnt in totals.items()
    }
    return {"levels": levels, "provinces": provinces}


def rate(counter: Counter, confirmed_no_column: int) -> float | None:
    """覆盖率 = 已收录 ÷（应接入 − 人工确认过的网站无此栏目 − 不能抓）。"""
    total = sum(counter.values()) - confirmed_no_column - counter.get("不能抓", 0)
    return round(counter.get("已收录", 0) / total, 4) if total > 0 else None


def _latest_acceptances(session: Session) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for a in session.scalars(select(CoverageAcceptance).order_by(CoverageAcceptance.sampled_at)):
        out[(a.province, a.level)] = {
            "passed": a.passed,
            "sampled_by": a.sampled_by,
            "sampled_at": a.sampled_at.isoformat() if a.sampled_at else None,
            "sample_size": a.sample_size,
            "note": a.note,
        }
    return out
