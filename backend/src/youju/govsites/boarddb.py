"""覆盖看板落库：把盘点结果写进 coverage_* 表、按来源抓取结果重算每格状态、按级汇总、记按省抽查。

- 导入（coverage-import）：行政区按区划代码、格按区划 + 部门更新，不动人工确认的内容；开发区一个
  行政区可有几个，按网址、再按全称配上已有的格（顺序变了人工记录也不会挂错）；
- 重算（coverage-refresh）：规则和离线统计同一份（board.cell_for），只读 sources、source_checks、
  documents；格里的栏目每次按网址重新挂到来源（后导入的来源也能挂上）；一省一提交；
  人工确认"网站无此栏目"只对没认出栏目的格生效；
- 汇总：每级、每省各状态格数和覆盖率（人工确认过的"网站无此栏目"和"不能抓"不算缺口）。
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from youju.crawler.adapters import LIST_API_OPEN_KEY, VIA_API_WORD, list_api_adapter
from youju.crawler.urls import url_key
from youju.govsites.board import STATES, SourceState, _outcome, cell_for
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
# 人工能确认"网站无此栏目"的程序结论；打不开（抓取失败）、不能抓的看不到网站内容，不能确认
CONFIRMABLE = ("网站无此栏目", "需人工")
_CHUNK = 1000  # IN 列表分批：全国约 4 万格、几万个来源，免得一条语句带几万个参数


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


def _chunks(items: Sequence, size: int = _CHUNK) -> Iterator[Sequence]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def can_confirm_no_column(columns: list | None, state: str) -> bool:
    """能不能人工确认"网站无此栏目"：格里没认出栏目，且程序判的是网站无此栏目或需人工。

    认出了栏目（哪怕还读不了）说明网站上有，确认了会把一个缺口从分母里扣掉；打不开、不能抓的
    看不到网站内容，同样不能确认。"""
    return not columns and state in CONFIRMABLE


def _url_key(url: str | None) -> str:
    """比网址用：不分 http/https、大小写和末尾斜杠。"""
    u = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix) :]
            break
    return u.rstrip("/")


def _take(free: list[CoverageCell], match: Callable[[CoverageCell], bool]) -> CoverageCell | None:
    for i, cell in enumerate(free):
        if match(cell):
            return free.pop(i)
    return None


def _pair_cells(
    dept: str, rows: list[DeptRow], olds: list[CoverageCell]
) -> tuple[list[tuple[DeptRow, CoverageCell | None]], list[CoverageCell]]:
    """这次盘点的行配上已有的格，返回 (配对, 没配上的旧格)；配对里格为 None 的要新建。

    标准部门一个行政区一格，直接配；开发区一个行政区可有几个，按网址、再按全称配，配不上的
    另起一格：免得盘点顺序一变，人工确认和备注挂到别的开发区上。"""
    if dept != "开发区" and len(rows) <= 1 and len(olds) <= 1:
        if not rows:
            return [], list(olds)
        return [(rows[0], olds[0] if olds else None)], []
    free = list(olds)
    pairs: list[tuple[DeptRow, CoverageCell | None]] = []
    pending: list[DeptRow] = []
    for d in rows:
        key = _url_key(d.site_url)
        cell = _take(free, lambda c, key=key: bool(key) and _url_key(c.site_url) == key)
        if cell is None:
            pending.append(d)
        else:
            pairs.append((d, cell))
    for d in pending:
        name = d.dept_full_name
        pairs.append((d, _take(free, lambda c, name=name: bool(name) and c.dept_full_name == name)))
    return pairs, free


def _column_check(url: str, checks: dict[str, object], prev: dict[str, dict]) -> dict[str, str]:
    """栏目的试读结论（coverage-verify）：这次给了用这次的，没给就留上次导入的。"""
    if url in checks:
        c = checks[url]
        return {
            "result": getattr(c, "result", "") or "",
            "reason": getattr(c, "reason", "") or "",
        }
    old = prev.get(url) or {}
    return {"result": old.get("result", "") or "", "reason": old.get("reason", "") or ""}


def _fill_cell(cell: CoverageCell, d: DeptRow, checks: dict[str, object]) -> None:
    prev = {c.get("url", ""): c for c in (cell.columns or [])}
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
            # 没导入成来源的栏目，看板据试读结论说明原因
            **_column_check(c.url, checks, prev),
        }
        for c in d.columns
    ]


def import_inventory(
    session: Session,
    portals: Iterable[PortalRow],
    depts: Iterable[DeptRow],
    checks: dict[str, object] | None = None,
    divisions_meta: dict[str, tuple[str, str | None]] | None = None,
) -> ImportSummary:
    """盘点结果写进看板表。divisions_meta：区划代码 → (类型, 上级代码)，来自行政区划清单。

    只动这次给了门户的行政区；状态要再跑一次 refresh_states（命令行 coverage-import 会接着跑）。"""
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
            existing_div[p.code] = div
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

    codes = sorted({p.code for p in portals})
    code_set = set(codes)
    wanted: dict[tuple[str, str], list[DeptRow]] = defaultdict(list)
    for d in depts:
        if d.code in code_set:  # 只动这次给了门户的行政区
            wanted[(d.code, d.dept)].append(d)
    existing: dict[tuple[str, str], list[CoverageCell]] = defaultdict(list)
    for part in _chunks(codes):
        for c in session.scalars(
            select(CoverageCell)
            .where(CoverageCell.division_code.in_(part))
            .execution_options(populate_existing=True)
        ):
            existing[(c.division_code, c.dept)].append(c)

    touched: list[CoverageCell] = []
    for key in sorted(wanted.keys() | existing.keys()):
        olds = sorted(existing.get(key, []), key=lambda c: c.seq)
        pairs, stale = _pair_cells(key[1], wanted.get(key, []), olds)
        next_seq = max((c.seq for c in olds), default=-1) + 1
        for d, cell in pairs:
            if cell is None:
                cell = CoverageCell(division_code=key[0], dept=key[1], seq=next_seq)
                next_seq += 1
                cell.state, cell.confirmed_no_column = "需人工", False
                session.add(cell)
                summary.cells_created += 1
            else:
                summary.cells_updated += 1
            _fill_cell(cell, d, checks)
            touched.append(cell)
        # 盘点里已经没有的格：没人工动过的删掉，人工确认过的留着
        for cell in stale:
            if not (
                cell.confirmed_no_column or cell.alt_source or cell.note
            ) and _delete_if_untouched(session, cell):
                summary.cells_removed += 1
                continue
            cell.inventory_status = "导航未找到"
            cell.inventory_reason = "这次盘点没找到（保留人工记录）"
            touched.append(cell)
    session.flush()
    summary.links = _write_links(session, _desired_links(session, touched))
    session.commit()
    return summary


def _delete_if_untouched(session: Session, cell: CoverageCell) -> bool:
    """删掉没人工动过的格：删除语句里再核一次人工字段（读完以后有人刚在后台写了备注就不删）。"""
    deleted = session.execute(
        delete(CoverageCell)
        .where(
            CoverageCell.id == cell.id,
            CoverageCell.confirmed_no_column.is_(False),
            CoverageCell.alt_source.is_(None),
            CoverageCell.note.is_(None),
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if deleted:
        session.expunge(cell)
        return True
    try:
        session.refresh(cell)  # 刚被人工动过：留着，下面照"保留人工记录"处理
    except InvalidRequestError:
        session.expunge(cell)  # 别处已经删了（refresh 找不到这一行）
        return True
    return False


@contextmanager
def board_lock(session: Session, wait: int) -> Iterator[bool]:
    """覆盖看板的导入、重算、启用互斥（MySQL 命名锁，按库名区分）：拿到给 True，wait 秒内没拿到
    给 False。用单独的连接拿，进程退出、连接断开时自动放开；和后台保存之间靠行锁错开。"""
    with session.get_bind().connect() as conn:
        name = f"youju_coverage_board:{conn.scalar(text('SELECT DATABASE()'))}"[:64]
        got = conn.scalar(text("SELECT GET_LOCK(:name, :wait)"), {"name": name, "wait": wait}) == 1
        conn.commit()  # 锁跟着连接、不跟事务
        try:
            yield got
        finally:
            if got:
                conn.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": name})


def _sources_by_url(session: Session, urls: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in _chunks(sorted(urls)):
        for sid, url in session.execute(select(Source.id, Source.url).where(Source.url.in_(part))):
            out[url] = sid
    return out


def _desired_links(session: Session, cells: Sequence[CoverageCell]) -> dict[int, dict[int, str]]:
    """格 → {来源 id: 栏目类型}：格里的栏目按网址找来源（网址相同就是同一个来源）。"""
    urls = {col.get("url", "") for c in cells for col in (c.columns or [])} - {""}
    url_to_source = _sources_by_url(session, urls)
    out: dict[int, dict[int, str]] = {}
    for cell in cells:
        links: dict[int, str] = {}
        for col in cell.columns or []:
            sid = url_to_source.get(col.get("url", ""))
            if sid and sid not in links:
                links[sid] = (col.get("kind") or "")[:8]
        out[cell.id] = links
    return out


def _write_links(session: Session, desired: dict[int, dict[int, str]]) -> int:
    """按 desired 改 coverage_cell_sources：只重写有变化的格。返回挂上的总处数。"""
    ids = sorted(desired)
    existing: dict[int, dict[int, str]] = defaultdict(dict)
    for part in _chunks(ids):
        for cid, sid, kind in session.execute(
            select(
                CoverageCellSource.cell_id, CoverageCellSource.source_id, CoverageCellSource.kind
            ).where(CoverageCellSource.cell_id.in_(part))
        ):
            existing[cid][sid] = kind
    changed = [cid for cid in ids if existing.get(cid, {}) != desired[cid]]
    for part in _chunks(changed):
        session.execute(delete(CoverageCellSource).where(CoverageCellSource.cell_id.in_(part)))
    for cid in changed:
        for sid, kind in desired[cid].items():
            session.add(CoverageCellSource(cell_id=cid, source_id=sid, kind=kind))
    return sum(len(v) for v in desired.values())


def _source_states(session: Session, source_ids: set[int]) -> dict[int, SourceState]:
    """来源的启用、健康、最后成功、最后一次检查的结果和条目数、名下文件数。"""
    out: dict[int, SourceState] = {}
    for part in _chunks(sorted(source_ids)):
        # 最后一次检查：按 (source_id, started_at) 索引取每个来源最大的 started_at
        latest = (
            select(SourceCheck.source_id, func.max(SourceCheck.started_at).label("last_at"))
            .where(SourceCheck.source_id.in_(part))
            .group_by(SourceCheck.source_id)
            .subquery()
        )
        last: dict[int, tuple] = {}
        for sid, status, http, items in session.execute(
            select(
                SourceCheck.source_id,
                SourceCheck.status,
                SourceCheck.http_status,
                SourceCheck.items_found,
            )
            .join(
                latest,
                (SourceCheck.source_id == latest.c.source_id)
                & (SourceCheck.started_at == latest.c.last_at),
            )
            .order_by(SourceCheck.id)
        ):
            last[sid] = (status, http, items)  # 同一时刻有两条的取后写的
        docs = dict(
            session.execute(
                select(Document.source_id, func.count())
                .where(Document.source_id.in_(part))
                .group_by(Document.source_id)
            ).all()
        )
        for row in session.execute(
            select(
                Source.id,
                Source.url,
                Source.enabled,
                Source.fetch_mode,
                Source.health,
                Source.last_success_at,
                Source.last_error,
                Source.verification_status,
            ).where(Source.id.in_(part))
        ):
            status, http, items = last.get(row.id, (None, None, 0))
            out[row.id] = SourceState(
                row.url,
                row.enabled,
                row.fetch_mode,
                row.health,
                row.last_success_at,
                row.last_error,
                row.verification_status,
                docs.get(row.id, 0),
                status,
                http,
                items or 0,
            )
    return out


@dataclass
class _Check:
    result: str
    reason: str


def _compute(
    cell: CoverageCell,
    div: CoverageDivision,
    links: dict[int, str],
    states: dict[int, SourceState],
    now: datetime,
    trial: bool,
) -> tuple[str, str, datetime | None]:
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
        columns=[ColumnHit(c["kind"], c["name"], c["url"], c.get("source", "")) for c in columns],
    )
    sources = {states[sid].url: states[sid] for sid in links if sid in states}
    checks = {
        c["url"]: _Check(c.get("result", ""), c.get("reason", ""))
        for c in columns
        if c.get("result")
    }
    result = cell_for(row, sources, checks, now, trial)
    state, reason = result.state, result.reason
    if cell.confirmed_no_column and can_confirm_no_column(columns, state):
        when = f"，{cell.confirmed_at:%Y-%m-%d}" if cell.confirmed_at else ""
        state, reason = "网站无此栏目", f"人工确认网站无此栏目（{cell.confirmed_by or ''}{when}）"
    if state == "不能抓" and cell.alt_source:
        reason = f"{reason}；替代来源：{cell.alt_source}"
    return state, reason, result.last_success_at


def refresh_cells(
    session: Session,
    rows: Sequence[tuple[CoverageCell, CoverageDivision]],
    now: datetime | None = None,
    trial: bool = False,
) -> Counter:
    """重算给定的格（不提交）：先按网址重挂来源，再按来源抓取结果算状态。

    trial=True 是试点口径（没启用、试抓成功的也算已收录）：只算数、什么都不写，免得正式看板
    混进用户还看不到的来源。"""
    now = now or _now()
    desired = _desired_links(session, [cell for cell, _ in rows])
    if not trial:
        _write_links(session, desired)
    states = _source_states(session, {sid for links in desired.values() for sid in links})
    counts: Counter = Counter()
    for cell, div in rows:
        state, reason, last_success = _compute(
            cell, div, desired.get(cell.id, {}), states, now, trial
        )
        counts[state] += 1
        if trial:
            continue
        if state != cell.state:
            cell.state_changed_at = now
        cell.state, cell.reason = state, _clip(reason, 300)
        cell.last_success_at = last_success
        cell.refreshed_at = now
    return counts


def refresh_states(
    session: Session, now: datetime | None = None, trial: bool = False, province: str | None = None
) -> Counter:
    """按来源抓取结果重算每格状态，一省一提交（不长时间锁住整张表）。返回各状态格数。"""
    now = now or _now()
    if province:
        provinces = [province]
    else:
        provinces = list(
            session.scalars(
                select(CoverageDivision.province).distinct().order_by(CoverageDivision.province)
            )
        )
    counts: Counter = Counter()
    for prov in provinces:
        stmt = (
            select(CoverageCell, CoverageDivision)
            .join(CoverageDivision, CoverageDivision.code == CoverageCell.division_code)
            .where(CoverageDivision.province == prov)
            .execution_options(populate_existing=True)
        )
        if not trial:
            # 锁住这个省的格到提交：后台同时保存人工确认的，等这边算完再写（它自己会当场重算）
            stmt = stmt.with_for_update(of=CoverageCell)
        rows = session.execute(stmt).all()
        counts.update(refresh_cells(session, [(c, d) for c, d in rows], now, trial))
        if not trial:
            session.commit()
    return counts


def summary(session: Session, level: str | None = None) -> dict:
    """每级、每省各状态格数、覆盖率和状态最后重算时间；带上按省抽查的最新结论。"""
    stmt = (
        select(
            CoverageDivision.level,
            CoverageDivision.province,
            CoverageCell.state,
            CoverageCell.confirmed_no_column,
            func.count(),
            func.count(CoverageCell.refreshed_at),
            func.min(CoverageCell.refreshed_at),
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
    # 状态按什么时候的抓取结果算：取最早重算的那格；有格从没重算过记 None
    refreshed: dict[tuple[str, str], list] = {}
    for lv, prov, state, conf, n, n_refreshed, oldest in session.execute(stmt):
        by_prov[(lv, prov)][state] += n
        if conf and state == "网站无此栏目":
            confirmed[(lv, prov)] += n
        refreshed.setdefault((lv, prov), []).append(oldest if n_refreshed == n else None)
    accepted = _latest_acceptances(session)
    provinces = []
    totals: dict[str, Counter] = defaultdict(Counter)
    total_conf: Counter[str] = Counter()
    level_refreshed: dict[str, list] = defaultdict(list)
    for (lv, prov), cnt in sorted(
        by_prov.items(), key=lambda kv: (LEVELS.index(kv[0][0]), kv[0][1])
    ):
        totals[lv].update(cnt)
        total_conf[lv] += confirmed[(lv, prov)]
        at = _oldest(refreshed[(lv, prov)])
        level_refreshed[lv].append(at)
        provinces.append(
            {
                "level": lv,
                "province": prov,
                "counts": {s: cnt.get(s, 0) for s in BOARD_STATES},
                "total": sum(cnt.values()),
                "rate": rate(cnt, confirmed[(lv, prov)]),
                "refreshed_at": _iso(at),
                "acceptance": latest_acceptance(accepted, prov, lv),
            }
        )
    levels = {
        lv: {
            "counts": {s: cnt.get(s, 0) for s in BOARD_STATES},
            "total": sum(cnt.values()),
            "rate": rate(cnt, total_conf[lv]),
            "refreshed_at": _iso(_oldest(level_refreshed[lv])),
        }
        for lv, cnt in totals.items()
    }
    return {"levels": levels, "provinces": provinces}


def _oldest(times: list[datetime | None]) -> datetime | None:
    """一组重算时间里最早的；有一个是 None（有格没重算过）就是 None。"""
    if not times or any(t is None for t in times):
        return None
    return min(times)  # type: ignore[type-var]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def rate(counter: Counter, confirmed_no_column: int) -> float | None:
    """覆盖率 = 已收录 ÷（应接入 − 人工确认过的网站无此栏目 − 不能抓）。"""
    total = sum(counter.values()) - confirmed_no_column - counter.get("不能抓", 0)
    return round(counter.get("已收录", 0) / total, 4) if total > 0 else None


def latest_acceptance(
    accepted: dict[tuple[str, str], dict], province: str, level: str
) -> dict | None:
    """某省某级最新的抽查结论：本级的和"全部"的哪个新用哪个（一样新用本级的）。"""
    options = [a for a in (accepted.get((province, level)), accepted.get((province, "全部"))) if a]
    # 一样新时 max 取前面的，也就是本级的
    return max(options, key=lambda a: _when(a["sampled_at"])) if options else None


def _when(value: str | None) -> datetime:
    """抽查时间按时间比（ISO 写法有没有微秒都不影响）；没写时区的按 UTC。"""
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    at = datetime.fromisoformat(value)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _latest_acceptances(session: Session) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for a in session.scalars(
        select(CoverageAcceptance).order_by(CoverageAcceptance.sampled_at, CoverageAcceptance.id)
    ):
        out[(a.province, a.level)] = {
            "id": a.id,
            "level": a.level,
            "passed": a.passed,
            "sampled_by": a.sampled_by,
            "sampled_at": a.sampled_at.isoformat() if a.sampled_at else None,
            "sample_size": a.sample_size,
            "note": a.note,
        }
    return out


@dataclass
class EnableResult:
    candidates: int = 0
    enabled: int = 0
    blocked_reason: str | None = None  # 没抽查或没过（D-122）时不启用，写明原因
    acceptance_id: int | None = None  # 依据的抽查记录
    source_ids: list[int] = field(default_factory=list)  # 这次启用（dry-run 时是可启用）的来源
    skipped: int = 0  # 挂在这一级、试抓成功过，但最近被网站拒绝或读不出的（不开）
    missing: list[str] = field(default_factory=list)  # only_urls 里有、这次没开的（写明原因）


def enable_sources(
    session: Session,
    province: str,
    level: str,
    by: str,
    limit: int = 2000,
    dry_run: bool = False,
    now: datetime | None = None,
    verified: bool = False,
    only_urls: list[str] | None = None,
) -> EnableResult:
    """把某省某级"试抓成功、待启用"的来源分批打开（D-122 门禁）。

    只在这个省这一级最新的抽查合格时才启用；没抽查或没过的一个都不开（新来源先不放给用户）。
    只开：挂在这一级格上的、没启用的、静态抓取、没被网站拒绝、试抓成功过的来源；一次最多 limit 个
    （每天新启用上限，第二阶段计划第三节）。

    verified=True：也开这台库里还没抓过、但试读可读（"试跑可读"）的来源——本机试抓、抽查合格后
    在线上第一次打开时用：线上不先试抓没启用的来源（文件一抓进来就对用户可见）。

    only_urls：只开这些栏目（批次文件里这个省这一级的，按规范网址对），其余一个不开；批次里有、
    这次没开的写进 missing（没导入、已经在收、被拒绝或读不出），方便和批次文件核对。"""
    result = EnableResult()
    acc = latest_acceptance(_latest_acceptances(session), province, level)
    if not acc or not acc["passed"]:
        result.blocked_reason = (
            f"{province}{level}还没抽查合格（D-122：没抽查或抽查没过的省，新接的来源先不放给用户）"
        )
        return result
    result.acceptance_id = acc["id"]
    rows = session.execute(
        select(Source)
        .join(CoverageCellSource, CoverageCellSource.source_id == Source.id)
        .join(CoverageCell, CoverageCell.id == CoverageCellSource.cell_id)
        .join(CoverageDivision, CoverageDivision.code == CoverageCell.division_code)
        .where(
            CoverageDivision.province == province,
            CoverageDivision.level == level,
            Source.enabled.is_(False),
            Source.fetch_mode == "static",
            Source.health != "blocked",
            *([] if verified else [Source.last_success_at.is_not(None)]),
        )
        .distinct()
        .order_by(Source.id)
    ).scalars()
    found = list(rows)
    wanted: dict[str, str] | None = None
    if only_urls is not None:
        wanted = {url_key(u): u for u in only_urls}
        found = [s for s in found if url_key(s.url) in wanted]
    # 和看板同一套结论：最近一次被 robots、401/403/412/521 拒绝的，或读不出条目的，不开（D-013）
    states = _source_states(session, {s.id for s in found})
    now = now or _now()
    candidates = []
    for source in found:
        state = states.get(source.id)
        if state and _outcome(source.url, {source.url: state}, {}, now)[0] == "pending":
            candidates.append(source)
        else:
            result.skipped += 1
    result.candidates = len(candidates)
    if wanted is not None:
        got = {url_key(s.url) for s in candidates[:limit]}
        rest = {key: url for key, url in wanted.items() if key not in got}
        # 按规范网址找库里的同一个列表（只差 http/https 等写法的也算），和上线脚本数"已经打开"的
        # 口径一致：另一种写法已经启用的算"已经在收"
        same: dict[str, list[Source]] = {}
        if rest:
            for src in session.scalars(select(Source)):
                key = url_key(src.url)
                if key in rest:
                    same.setdefault(key, []).append(src)
        for key, url in rest.items():
            srcs = same.get(key, [])
            if not srcs:
                why = "库里没有（没导入）"
            elif any(s.enabled for s in srcs):
                why = "已经在收" + ("" if any(s.url == url for s in srcs) else "：库里是另一种写法")
            else:
                why = "最近被网站拒绝、读不出、不是静态抓取，或不在这一级的格上"
            result.missing.append(f"{url}（{why}）")
    for source in candidates[:limit]:
        if not dry_run:
            source.enabled = True
            note = f"全国覆盖：{province}{level}抽查合格（抽查记录 #{acc['id']}）后由 {by} 启用"
            if VIA_API_WORD in (source.verification_status or ""):
                # 试读经列表接口读出、这次复检抽查合格后打开：写"经接口读"标记，正式抓取才走接口
                # （D-128；没有标记的来源一律不走接口）
                source.parser_config = {
                    **(source.parser_config or {}),
                    LIST_API_OPEN_KEY: list_api_adapter(source.url),
                }
                note += "，经接口读（D-128）"
            source.note = f"{source.note}；{note}" if source.note else note
        result.enabled += 1
        result.source_ids.append(source.id)
    if not dry_run:
        session.commit()
    return result
