"""AI 用量台账（D-023）：每条命令结束时记一笔，按天、任务、模型累加；运维日报据此报用量和费用。"""

import json
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from youju.ai.client import AIError, ChatClient
from youju.ai.usage import cost_yuan, record_usage, usage_summary
from youju.models import AIUsage


def make_client(replies, model="qwen-flash"):
    """replies：依次返回的 (状态码, 输入 tokens, 输出 tokens)。"""
    queue = list(replies)

    def handler(request):
        status, prompt, completion = queue.pop(0)
        body = {"usage": {"prompt_tokens": prompt, "completion_tokens": completion}}
        if status == 200:
            body["choices"] = [{"message": {"content": json.dumps({"ok": True})}}]
        return httpx.Response(status, json=body)

    return ChatClient(
        "https://example.test",
        "key",
        model,
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_client_totals_count_calls_errors_and_tokens() -> None:
    client = make_client([(200, 10, 2), (500, 3, 0)])
    client.complete_json("JSON", "JSON")
    with pytest.raises(AIError):
        client.complete_json("JSON", "JSON")
    assert client.totals == {"calls": 2, "errors": 1, "prompt_tokens": 13, "completion_tokens": 2}


def test_record_usage_adds_up_by_beijing_day_task_and_model(session) -> None:
    client = make_client([(200, 100, 20)] * 3)
    client.complete_json("JSON", "JSON")
    # 北京时间 9 月 29 日 0 点 30 分（UTC 还是 28 日）
    late = datetime(2026, 9, 28, 16, 30, tzinfo=UTC)
    record_usage(session, client, "audit", now=late)
    assert client.totals["calls"] == 0  # 记完清零，重复调用不会重复记
    record_usage(session, client, "audit", now=late)
    client.complete_json("JSON", "JSON")
    client.complete_json("JSON", "JSON")
    record_usage(session, client, "audit", now=late)
    row = session.scalar(select(AIUsage))
    assert (row.day, row.task, row.model) == (date(2026, 9, 29), "audit", "qwen-flash")
    assert (row.calls, row.errors, row.prompt_tokens, row.completion_tokens) == (3, 0, 300, 60)
    assert session.scalar(select(AIUsage).where(AIUsage.task != "audit")) is None


def test_usage_summary_costs_and_cumulative_totals(session) -> None:
    session.add_all(
        [
            AIUsage(
                day=date(2026, 9, 28),
                task="check",
                model="deepseek-flash",
                calls=10,
                errors=1,
                prompt_tokens=20000,
                completion_tokens=3000,
            ),
            AIUsage(
                day=date(2026, 9, 28),
                task="audit",
                model="deepseek-flash",
                calls=2,
                errors=0,
                prompt_tokens=1000,
                completion_tokens=200,
            ),
            AIUsage(
                day=date(2026, 9, 20),
                task="check",
                model="deepseek-flash",
                calls=50,
                errors=0,
                prompt_tokens=100000,
                completion_tokens=5000,
            ),
        ]
    )
    session.commit()
    summary = usage_summary(session, date(2026, 9, 28))
    assert (summary.calls, summary.errors) == (12, 1)
    assert (summary.prompt_tokens, summary.completion_tokens) == (21000, 3200)
    assert summary.cost == pytest.approx((21000 * 2 + 3200 * 8) / 1_000_000)
    assert summary.cumulative == {"deepseek-flash": 129200}

    session.add(
        AIUsage(
            day=date(2026, 9, 28),
            task="check",
            model="qwen-plus",
            calls=1,
            errors=0,
            prompt_tokens=10,
            completion_tokens=1,
        )
    )
    session.commit()
    assert usage_summary(session, date(2026, 9, 28)).cost is None  # 没列价格的模型不瞎估
    # 以前百炼的 qwen-flash 不再列价（不用了，价也没重新核）
    assert cost_yuan("qwen-flash", 1_000_000, 0) is None
    assert cost_yuan("deepseek-flash", 1_000_000, 1_000_000) == pytest.approx(2 + 8)
    assert cost_yuan("deepseek-v4-pro", 1_000_000, 1_000_000) == pytest.approx(9.0 + 27.0)
    # 旧模型名还能调用，按 Flash 价计费
    assert cost_yuan("deepseek-v4-flash", 1_000_000, 1_000_000) == pytest.approx(2 + 8)
    assert cost_yuan("deepseek-v4-flash-vision-exp", 1_000_000, 0) == pytest.approx(2)


def test_usage_summary_reports_the_earliest_price_check_date(session) -> None:
    """日报写"价格核于…"：当天用到的、列了价格的模型各自的核价日期，去重后按先后排。"""
    day = date(2026, 10, 21)

    def add(model: str) -> None:
        session.add(
            AIUsage(
                day=day,
                task="check",
                model=model,
                calls=1,
                errors=0,
                prompt_tokens=1000,
                completion_tokens=100,
            )
        )
        session.commit()

    assert usage_summary(session, day).priced_on == ()  # 当天没用量
    add("deepseek-flash")
    add("deepseek-v4-pro")
    assert usage_summary(session, day).priced_on == (date(2026, 10, 22),)
    add("qwen-flash")  # 没列价的模型：当天费用不估，核价日期也不算它
    summary = usage_summary(session, day)
    assert summary.cost is None and summary.priced_on == (date(2026, 10, 22),)
