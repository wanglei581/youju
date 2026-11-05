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
from datetime import datetime

from youju.govsites.inventory import DeptRow

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


def cell_for(row: DeptRow, sources: dict[str, SourceState], checks: dict[str, object]) -> Cell:
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
        cell.state, cell.reason = "需人工", "官方导航里没找到这个部门的网站或频道"
        return cell
    if row.status == "不能抓":
        cell.state, cell.reason = "不能抓", f"部门网站：{row.reason}"
        return cell
    if row.status == "打不开":
        cell.state, cell.reason = "抓取失败", f"部门网站打不开：{row.reason[:80]}"
        return cell
    if not row.columns:
        cell.state = "网站无此栏目"
        cell.reason = "部门网站首页和信息公开页都没有通知公告、政策文件、公示公告栏目（待人工确认）"
        return cell
    found = [sources[u] for u in cell.column_urls if u in sources]
    ok = [s for s in found if s.last_success_at is not None and s.documents > 0]
    if ok:
        cell.state = "已收录"
        cell.last_success_at = max(s.last_success_at for s in ok)  # type: ignore[type-var]
        cell.reason = f"{len(ok)}/{len(cell.column_urls)} 个栏目已收录"
        return cell
    if found and all(_refused(s.verification_status) or s.fetch_mode == "blocked" for s in found):
        cell.state, cell.reason = "不能抓", found[0].verification_status or "网站拒绝抓取"
        return cell
    tried = [s for s in found if s.last_error]
    if tried:
        cell.state, cell.reason = "抓取失败", (tried[0].last_error or "")[:100]
        return cell
    # 还没导入或还没抓：看试读结论
    results = [checks[u] for u in cell.column_urls if u in checks]
    readable = [c for c in results if getattr(c, "result", "") == "可读"]
    if readable or found:
        cell.state, cell.reason = (
            PENDING,
            "栏目试读可读，已导入未启用" if found else "栏目试读可读，未导入",
        )
        return cell
    blocked = [c for c in results if getattr(c, "result", "") == "不能抓"]
    if blocked and len(blocked) == len(results):
        cell.state, cell.reason = "不能抓", getattr(blocked[0], "reason", "")
        return cell
    down = [c for c in results if getattr(c, "result", "") == "打不开"]
    if down and len(down) == len(results):
        cell.state, cell.reason = "抓取失败", f"栏目打不开：{getattr(down[0], 'reason', '')[:80]}"
        return cell
    if results:
        cell.state = "需人工"
        cell.reason = "；".join(
            sorted({f"{getattr(c, 'result', '')}" for c in results if getattr(c, "result", "")})
        )
        return cell
    cell.state, cell.reason = "需人工", "栏目还没试读"
    return cell


def cells(
    depts: Iterable[DeptRow], sources: dict[str, SourceState], checks: dict[str, object]
) -> list[Cell]:
    return [cell_for(d, sources, checks) for d in depts]


def rollup(items: Iterable[Cell]) -> dict[str, Counter]:
    """按层级汇总各状态的格数。"""
    out: dict[str, Counter] = {}
    for c in items:
        out.setdefault(c.level, Counter())[c.state] += 1
    return out


def coverage_rate(counter: Counter) -> float | None:
    """覆盖率 = 已收录 ÷（应接入 − 网站无此栏目 − 不能抓）。

    没有栏目、明确不让抓的不算缺口，另列。"""
    total = sum(counter.values()) - counter["网站无此栏目"] - counter["不能抓"]
    return counter["已收录"] / total if total > 0 else None
