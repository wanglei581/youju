"""覆盖看板：每个"行政区 × 目标部门"一格，算出它现在的状态、原因和最后成功时间，按层级汇总。

五种状态（创始人 11/5 定）：
- 已收录：至少一个栏目抓取成功、读出了条目（看最后成功时间）；
- 抓取失败：栏目或部门网站暂时打不开、连续失败（原因写最后一次错误）；
- 网站无此栏目：部门网站能打开，但首页和信息公开页都没有通知公告、政策文件、公示公告栏目
  （程序判断，待人工确认；这类部门的文件一般由门户或上级统一发布）；
- 需人工：官方导航里没找到这个部门，或栏目读不出条目（要浏览器渲染、接口或人工写规则）；
- 不能抓：要登录、有验证码、防护拦截或 robots.txt 禁止，一律不绕过
  （另找替代来源，如省级政策文件库）。

接入过程中还有一个过渡状态"待启用"：栏目试读可读、已导入但还没抓过（全国覆盖的新来源默认不启用）。
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from youju.crawler.adapters import LIST_API_REFUSED
from youju.govsites.inventory import SHARED_ONLY, DeptRow

STATES = ("已收录", "抓取失败", "网站无此栏目", "需人工", "不能抓")
PENDING = "待启用"
LEVELS = ("国家级", "省级", "市级", "区县级")


@dataclass
class SourceState:
    """sources 表里和看板有关的几列（看板不直接依赖 ORM，便于离线统计）。"""

    url: str
    enabled: bool
    fetch_mode: str
    health: str
    last_success_at: datetime | None
    last_error: str | None
    verification_status: str | None
    documents: int = 0
    last_status: str | None = None  # 最后一次检查的结果（source_checks.status）
    last_http: int | None = None
    # 最后一次检查识别出的条目数：同一份文件挂在先抓到它的来源下，合并部门共用栏目时
    # 后一个来源名下没有文件，但它的检查照样读出了条目
    last_items: int = 0


@dataclass
class Cell:
    code: str
    level: str
    province: str
    city: str
    district: str
    dept: str
    dept_full_name: str
    site_url: str
    nav_source: str
    state: str = "需人工"
    reason: str = ""
    last_success_at: datetime | None = None
    column_urls: list[str] = field(default_factory=list)


def _refused(status: str | None) -> bool:
    return bool(status) and any(w in status for w in ("拒绝", "防护", "拦截", "robots 禁止"))


# "已收录"的时效：最后成功超过这么多天（每天检查 1–4 次）就算抓取失败
STALE_DAYS = 3


def _outcome(
    url: str,
    sources: dict[str, SourceState],
    checks: dict[str, object],
    now: datetime,
    trial: bool = False,
):
    """一个栏目现在的结论：ok / stale / blocked / failed / pending / unread，以及说明。

    trial：试点报告用——未启用但试抓成功的也算收录，不按 3 天过期（试点抓一轮就停）。"""
    s = sources.get(url)
    if s is not None:
        # 先看最近一次是不是被拒（成功过、后来被 robots 或防护拒的，记不能抓而不是过期）
        if s.last_status == "robots_disallowed":
            return "blocked", "robots.txt 禁止"
        if s.last_status == "http_error" and s.last_http in (401, 403, 412, 521):
            return "blocked", f"HTTP {s.last_http} 拒绝访问"
        if s.last_status == LIST_API_REFUSED:
            # 列表接口被读取护栏（D-128）拦下：原因写在最近一次的错误里
            return "blocked", s.last_error or "列表接口按读取护栏不读"
        if _refused(s.verification_status) or s.fetch_mode == "blocked" or s.health == "blocked":
            return "blocked", s.verification_status or s.last_error or "网站拒绝抓取"
        last = s.last_success_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is not None and (s.documents > 0 or s.last_items > 0):
            if trial:
                return "ok", ""
            if not s.enabled:
                # 新来源默认不启用：试抓成功但没在收，算待启用，不算已收录、也不算过期
                return "pending", "试抓成功，待启用"
            age = now - last
            if age > timedelta(days=STALE_DAYS):
                return "stale", f"连续 {age.days} 天没有成功（{(s.last_error or '')[:60]}）"
            return "ok", ""
        if s.last_status == "parse_empty":
            return "unread", "页面能打开，但读不出条目（要浏览器渲染、接口或写规则）"
        if s.last_status and s.last_status != "ok":
            return "failed", (s.last_error or s.last_status)[:100]
        if s.last_status == "ok":
            return "unread", "抓到了页面，但没有条目"
        # 导入了还没抓：按导入时的验证状态
        vs = s.verification_status or ""
        if vs.startswith("试跑可读"):
            return "pending", "栏目试读可读，已导入未启用"
        if vs.startswith("本机无法访问"):
            return "failed", vs
        return "unread", vs or "已导入，还没抓过"
    c = checks.get(url)
    result = getattr(c, "result", "") if c is not None else ""
    reason = getattr(c, "reason", "") if c is not None else ""
    if result == "不能抓":
        return "blocked", reason
    if result == "打不开":
        return "failed", f"栏目打不开：{reason[:80]}"
    if result == "可读":
        return "pending", "栏目试读可读，未导入"
    if result:
        return "unread", result
    return "unread", "栏目还没试读"


def cell_for(
    row: DeptRow,
    sources: dict[str, SourceState],
    checks: dict[str, object],
    now: datetime | None = None,
    trial: bool = False,
) -> Cell:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cell = Cell(
        row.code,
        row.level,
        row.province,
        row.city,
        row.district,
        row.dept,
        row.dept_full_name,
        row.site_url,
        row.nav_source,
        column_urls=[c.url for c in row.columns],
    )
    if row.status == "导航未找到":
        cell.state = "需人工"
        cell.reason = row.reason or "官方导航里没找到这个部门的网站或频道"
        return cell
    if row.status == "不能抓":
        cell.state, cell.reason = "不能抓", row.reason or "网站拒绝抓取"
        return cell
    if row.status == "打不开":
        cell.state, cell.reason = "抓取失败", f"打不开：{row.reason[:80]}"
        return cell
    if not row.columns and row.reason == SHARED_ONLY:
        cell.state, cell.reason = "需人工", SHARED_ONLY
        return cell
    if not row.columns:
        cell.state = "网站无此栏目"
        cell.reason = "部门网站首页和信息公开页都没有通知公告、政策文件、公示公告栏目（待人工确认）"
        return cell
    outcomes = [(_outcome(u, sources, checks, now, trial), u) for u in cell.column_urls]
    kinds = Counter(o[0] for o, _ in outcomes)
    blocked_note = f"；另有 {kinds['blocked']} 个栏目网站拒绝抓取" if kinds["blocked"] else ""
    if kinds["ok"]:
        cell.state = "已收录"
        cell.last_success_at = max(
            sources[u].last_success_at  # type: ignore[type-var]
            for (k, _), u in outcomes
            if k == "ok"
        )
        cell.reason = f"{kinds['ok']}/{len(outcomes)} 个栏目已收录" + blocked_note
        return cell
    failed = [why for (k, why), _ in outcomes if k in ("failed", "stale")]
    if failed:
        cell.state, cell.reason = "抓取失败", failed[0] + blocked_note
        stale = [sources[u].last_success_at for (k, _), u in outcomes if k == "stale"]
        cell.last_success_at = max(stale) if stale else None  # type: ignore[type-var]
        return cell
    if kinds["blocked"] and kinds["blocked"] == len(outcomes):
        cell.state = "不能抓"
        cell.reason = next(why for (k, why), _ in outcomes if k == "blocked")
        return cell
    if kinds["pending"]:
        cell.state, cell.reason = PENDING, next(w for (k, w), _ in outcomes if k == "pending")
        cell.reason += blocked_note
        return cell
    cell.state = "需人工"
    cell.reason = "；".join(sorted({w for (k, w), _ in outcomes if k == "unread"})) + blocked_note
    return cell


def cells(
    depts: Iterable[DeptRow],
    sources: dict[str, SourceState],
    checks: dict[str, object],
    now: datetime | None = None,
    trial: bool = False,
) -> list[Cell]:
    return [cell_for(d, sources, checks, now, trial) for d in depts]


def rollup(items: Iterable[Cell]) -> dict[str, Counter]:
    """按层级汇总各状态的格数。"""
    out: dict[str, Counter] = {}
    for c in items:
        out.setdefault(c.level, Counter())[c.state] += 1
    return out


def coverage_rate(counter: Counter, confirmed_no_column: int = 0) -> float | None:
    """覆盖率 = 已收录 ÷（应接入 − 人工确认过的网站无此栏目 − 不能抓）。

    程序判的"网站无此栏目"人工确认前不从分母里扣（免得没认出栏目就把覆盖率抬高）；
    不能抓有防护页、robots 等证据，扣掉但单独列出、要有替代来源说明。
    """
    total = sum(counter.values()) - confirmed_no_column - counter["不能抓"]
    return counter["已收录"] / total if total > 0 else None
