"""AI 用量台账（D-023）：每条命令结束时把 AI 客户端的累计用量记一笔。

运维日报据此报用量、费用和免费额度。
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from youju.ai.client import ChatClient
from youju.models import AIUsage

BEIJING = ZoneInfo("Asia/Shanghai")
# 每百万 tokens 的价格（元）：输入、输出。2026-09-28 核实的百炼官方价（D-023）；没列的模型不估费用
PRICES = {"qwen-flash": (0.15, 1.5)}
# 百炼新开通时每个模型送的免费额度（tokens，90 天内有效，北京地域）
FREE_QUOTA = 1_000_000


def record_usage(
    session: Session,
    client: ChatClient,
    task: str,
    now: datetime | None = None,
    *,
    precounted_calls: int = 0,
) -> None:
    """把客户端的累计用量加到今天（北京时间）这项任务的台账上，记完清零，重复调用不会重复记。"""
    totals = dict(client.totals)
    if not totals["calls"]:
        return
    totals["calls"] = max(0, totals["calls"] - precounted_calls)
    day = (now or datetime.now(UTC)).astimezone(BEIJING).date()
    table = AIUsage.__table__
    stmt = insert(table).values(day=day, task=task, model=client.model, **totals)
    stmt = stmt.on_duplicate_key_update(
        {key: table.c[key] + stmt.inserted[key] for key in totals},
    )
    session.execute(stmt)
    session.commit()
    for key in client.totals:
        client.totals[key] = 0


def cost_yuan(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


@dataclass
class UsageSummary:
    day: date
    calls: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 有没列价格的模型时为 None
    cost: float | None = 0.0
    # 各模型从开始记账起的累计 tokens（输入加输出）
    cumulative: dict[str, int] = field(default_factory=dict)


def usage_summary(session: Session, day: date) -> UsageSummary:
    """某一天（北京时间）的用量和费用，加上各模型的累计用量。"""
    summary = UsageSummary(day=day)
    for model, calls, errors, prompt, completion in session.execute(
        select(
            AIUsage.model,
            func.sum(AIUsage.calls),
            func.sum(AIUsage.errors),
            func.sum(AIUsage.prompt_tokens),
            func.sum(AIUsage.completion_tokens),
        )
        .where(AIUsage.day == day)
        .group_by(AIUsage.model)
    ):
        summary.calls += int(calls)
        summary.errors += int(errors)
        summary.prompt_tokens += int(prompt)
        summary.completion_tokens += int(completion)
        cost = cost_yuan(model, int(prompt), int(completion))
        summary.cost = None if cost is None or summary.cost is None else summary.cost + cost
    summary.cumulative = {
        model: int(tokens)
        for model, tokens in session.execute(
            select(
                AIUsage.model, func.sum(AIUsage.prompt_tokens + AIUsage.completion_tokens)
            ).group_by(AIUsage.model)
        )
    }
    return summary
