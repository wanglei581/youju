"""按每个启用栏目最近一次检查记录计算每日验收指标。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import Source, SourceCheck


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


def check_sources(session: Session, *, now: datetime | None = None) -> SourceHealth:
    """只看今天（北京时间零点起）的检查。

    用 26 小时之类的滑动窗口时，昨天的成功记录会掩盖今天的漏检。
    """
    now = now or datetime.now(UTC)
    cutoff = now.astimezone(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    latest_status = (
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
