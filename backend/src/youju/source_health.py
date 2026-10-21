"""按每个启用栏目最近一次检查记录计算每日验收指标。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.models import Source, SourceCheck

BEIJING = ZoneInfo("Asia/Shanghai")
# 同一栏目连续没抓到第 3、7、14、30 天，之后每满 30 天，早上那一轮单独提醒一次
# （日报每天都写"上次成功几号"）。不只第 3 天：那天早轮没跑（抓取失败、被跳过）也不会永远错过，
# 下一个节点还会提醒；坏了很久的也隔一阵提醒一次
STREAK_ALERT_DAYS = (3, 7, 14)
STREAK_ALERT_EVERY = 30


def is_streak_milestone(days: int | None) -> bool:
    return days is not None and (
        days in STREAK_ALERT_DAYS or (days >= STREAK_ALERT_EVERY and days % STREAK_ALERT_EVERY == 0)
    )


@dataclass(frozen=True)
class SourceHealth:
    total: int
    successful: int
    failures: tuple[str, ...]
    missing: int

    @property
    def rate(self) -> float:
        return 100 * self.successful / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        # 没有启用栏目也不能算完成验收；漏检即使成功率达到 95% 也必须提醒。
        return self.total > 0 and self.successful * 100 >= self.total * 95 and self.missing == 0

    def alert_text(self) -> str:
        lines = [
            f"今天栏目检查成功率 {self.successful}/{self.total}（{self.rate:.1f}%）；"
            f"失败或漏检 {len(self.failures)} 个。"
        ]
        lines.extend(self.failures[:10])
        if len(self.failures) > 10:
            lines.append(f"另有 {len(self.failures) - 10} 个未列出")
        return "\n".join(lines)


def _latest_status_today(now: datetime):
    """每个栏目今天（北京时间零点起、到 now 为止）最近一次检查的状态；今天没检查过是 NULL。"""
    cutoff = now.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        select(SourceCheck.status)
        .where(
            SourceCheck.source_id == Source.id,
            SourceCheck.started_at >= cutoff,
            SourceCheck.started_at <= now,
        )
        .order_by(SourceCheck.started_at.desc(), SourceCheck.id.desc())
        .limit(1)
        .correlate(Source)
        .scalar_subquery()
    )


def check_sources(session: Session, *, now: datetime | None = None) -> SourceHealth:
    """只看今天（北京时间零点起）的检查。

    用 26 小时之类的滑动窗口时，昨天的成功记录会掩盖今天的漏检。
    """
    now = now or datetime.now(UTC)
    latest_status = _latest_status_today(now)
    rows = session.execute(
        select(Source.dept_full_name, Source.column_name, latest_status.label("status"))
        .where(Source.enabled.is_(True))
        .order_by(Source.id)
    ).all()
    failures = tuple(
        f"{dept} · {column} · {status or '漏检'}" for dept, column, status in rows if status != "ok"
    )
    return SourceHealth(
        total=len(rows),
        successful=sum(status == "ok" for _, _, status in rows),
        failures=failures,
        missing=sum(status is None for _, _, status in rows),
    )


@dataclass(frozen=True)
class FailingSource:
    """今天没抓成功的启用栏目（和 check_sources 的失败、漏检是同一批）。"""

    dept: str
    column: str
    # 今天最近一次检查的状态；今天还没检查过是"漏检"
    status: str
    # 上次成功检查的日期（北京时间）；从没成功过是 None
    last_success: date | None
    # 从哪天起一直没成功：上次成功的那天；从没成功过的，第一次检查那天；从没检查过是 None
    since: date | None
    # 连续没抓到第几天（北京时间日期）：上次成功是 9/25 的，9/26 是第 1 天、9/28 是第 3 天；
    # 从没成功过的，第一次检查（失败）那天就是第 1 天。今天成功过又失败的是 0
    days: int | None

    def label(self) -> str:
        if self.last_success is not None:
            return f"上次成功 {self.last_success.month}月{self.last_success.day}日"
        if self.since is not None:
            return f"从没成功过，{self.since.month}月{self.since.day}日起"
        return "从没检查过"

    def text(self, *, short: bool = False) -> str:
        """short：日报里一行放几个，截短部门和栏目名，"上次成功几号"总是完整保留。"""
        dept, column = (self.dept[:14], self.column[:10]) if short else (self.dept, self.column)
        return f"{dept} · {column} · {self.status}（{self.label()}）"


def _beijing_day(value: datetime | None) -> date | None:
    if value is None:
        return None
    # 库里一律按 UTC 存；聚合函数取出来万一不带时区，也按 UTC 理解（和 UTCDateTime 一致）
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(BEIJING).date()


def failing_sources(session: Session, *, now: datetime | None = None) -> list[FailingSource]:
    """今天没抓成功的启用栏目，带上次成功的日期，日报和"连续几天没抓到"的提醒都用它。

    上次成功取栏目上记的 last_success_at（每次检查成功时更新）；从没成功过的，按第一次检查算起，
    这样新接入就一直失败的栏目也会在第 3 天提醒。"""
    now = now or datetime.now(UTC)
    today = now.astimezone(BEIJING).date()
    first_check = (
        select(func.min(SourceCheck.started_at))
        .where(SourceCheck.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    rows = session.execute(
        select(
            Source.dept_full_name,
            Source.column_name,
            _latest_status_today(now).label("status"),
            Source.last_success_at,
            first_check.label("first_check"),
        )
        .where(Source.enabled.is_(True))
        .order_by(Source.id)
    ).all()
    failing = []
    for dept, column, status, last_success_at, first_check_at in rows:
        if status == "ok":
            continue
        last = _beijing_day(last_success_at)
        first = _beijing_day(first_check_at)
        if last is not None:
            since, days = last, (today - last).days
        elif first is not None:
            since, days = first, (today - first).days + 1
        else:
            since, days = None, None
        failing.append(
            FailingSource(
                dept=dept,
                column=column,
                status=status or "漏检",
                last_success=last,
                since=since,
                days=days,
            )
        )
    return failing
