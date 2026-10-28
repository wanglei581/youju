# ruff: noqa: F811
"""开卖那一版的后端必做项（付费服务协议"开卖那一版要一起做的"第 8、9、13 项，10/28）：
没出结果的付费任务不算用过、苹果问询的"已核实应当退款"和"无法核验"、停止服务公示、
赠送流水 3650 天清理、对账告警。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from tests.test_payments import (  # noqa: F401
    HEADERS,
    NOW,
    create,
    delivered,
    inquiry,
    order,
    push,
    run,
    self_refund,
    setup_pay,
    submit,
)
from youju import advisor_tasks, cli, payments
from youju.api import v1
from youju.models import (
    AdvisorTask,
    PassReward,
    PayEvent,
    PayOrder,
    PayReconciliation,
    WorkbenchPass,
)


def ios_order(setup_pay, session):
    setup_pay[2].order_type = 7
    return delivered(setup_pay, session)


def finish(session, task_id, status, at):
    task = session.get(AdvisorTask, task_id)
    task.status, task.finished_at = status, at
    advisor_tasks.record_pass_completion(session, task)
    session.commit()


# ---- 没出结果的付费任务不算用过（第 13 项） ------------------------------------------------


def test_paid_usage_is_recorded_when_a_result_comes_out_not_at_submit(setup_pay, session):
    row = delivered(setup_pay, session)
    first = submit(setup_pay, count=51, key="a").json()["id"]
    second = submit(setup_pay, count=51, key="b").json()["id"]
    session.expire_all()
    ticket = payments.order_pass(session, row)
    assert ticket.last_task_id == second and ticket.last_used_at is None
    finish(session, second, "failed", NOW + timedelta(minutes=1))
    session.expire_all()
    assert payments.order_pass(session, row).last_used_at is None
    finish(session, first, "done", NOW + timedelta(minutes=5))
    # 更早出结果的晚到记录不把时间改早
    session.expire_all()
    task = session.get(AdvisorTask, first)
    task.finished_at = NOW + timedelta(minutes=2)
    advisor_tasks.record_pass_completion(session, task)
    session.commit()
    session.expire_all()
    assert payments.order_pass(session, row).last_used_at == NOW + timedelta(minutes=5)


def test_self_refund_waits_for_a_running_paid_task_then_opens_when_it_fails(setup_pay, session):
    row = delivered(setup_pay, session)
    task_id = submit(setup_pay, count=51).json()["id"]
    session.expire_all()
    assert payments.refund_reason(session, row, NOW) == payments.PAID_TASK_RUNNING
    session.get(AdvisorTask, task_id).status = "running"
    session.commit()
    assert payments.refund_reason(session, row, NOW) == payments.PAID_TASK_RUNNING
    finish(session, task_id, "failed", NOW + timedelta(minutes=1))
    session.expire_all()
    assert payments.refund_reason(session, row, NOW) is None
    # 出了结果的就是用过了，原来那句不变
    other = submit(setup_pay, count=51, key="again").json()["id"]
    finish(session, other, "done", NOW + timedelta(minutes=2))
    session.expire_all()
    assert "已用过付费的量" in payments.refund_reason(session, row, NOW)


def test_restricted_api_account_can_self_refund_after_a_failed_paid_task(
    setup_pay, restricted_api_factory, monkeypatch
):
    """自助退款在接口服务上先锁这段使用期的付费任务行（SELECT … FOR UPDATE）：按部署文件授权的
    账号不缺权限；系统出错的付费任务不挡退款。"""
    from youju import access_log
    from youju.api import app as app_module
    from youju.db import get_session

    with restricted_api_factory() as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app_module.app.dependency_overrides[get_session] = override
        with limited() as session:
            row = delivered(setup_pay, session)
            task_id = submit(setup_pay, count=51).json()["id"]
            assert self_refund(setup_pay, row).status_code == 422
            session.expire_all()
            finish(session, task_id, "failed", NOW + timedelta(minutes=1))
            response = self_refund(setup_pay, row)
            assert response.status_code == 200 and response.json()["status"] == "退款中"


# ---- 苹果问询：已核实应当退款、无法核验（第 8 项②③） ----------------------------------------


def test_marked_ios_order_is_suggested_for_refund_even_if_used_and_late(
    setup_pay, session, monkeypatch
):
    row = ios_order(setup_pay, session)
    task_id = submit(setup_pay, count=51).json()["id"]
    finish(session, task_id, "done", NOW + timedelta(minutes=1))
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(hours=30))
    assert inquiry(setup_pay, row)["result_info"] == "已使用"
    result, _ = payments.mark_refund_due(session, row.id, "系统故障三天不能用", NOW)
    assert result == "marked"
    answer = inquiry(setup_pay, row)
    assert answer["result_code"] == 0 and answer["result_info"] == "应当退款"
    # 凭据不写应当退款的原因（答复给到境外的 Apple，只给必要的）
    assert answer["evidence"] == "付款后已超过 24 小时；用过付费的量；已核实应当退款"
    assert "故障" not in answer["evidence"]
    session.expire_all()
    marks = session.scalars(select(PayEvent).where(PayEvent.kind == "refund_due_marked")).all()
    assert len(marks) == 1 and marks[0].summary == {"pay_order_id": row.id}
    assert payments.mark_refund_due(session, row.id, None, NOW)[0] == "cleared"
    assert inquiry(setup_pay, row)["result_info"] == "已使用"
    session.expire_all()
    assert session.get(PayOrder, row.id).refund_due_note is None


def test_mark_refund_due_only_for_paid_ios_orders(setup_pay, session):
    android = delivered(setup_pay, session)
    assert payments.mark_refund_due(session, android.id, "系统故障三天不能用", NOW)[0] == "not_ios"
    assert payments.mark_refund_due(session, 999999, "系统故障三天不能用", NOW)[0] == "missing"
    row = ios_order(setup_pay, session)
    assert payments.mark_refund_due(session, row.id, None, NOW)[0] == "unchanged"
    assert (
        payments.mark_refund_due(session, row.id, "系统故障三天不能用", NOW, dry_run=True)[0]
        == "marked"
    )
    session.expire_all()
    assert session.get(PayOrder, row.id).refund_due_at is None
    assert (
        session.scalar(select(func.count(PayEvent.id)).where(PayEvent.kind.like("refund_due%")))
        == 0
    )


def test_unknown_order_and_errors_answer_unverifiable(setup_pay, session, monkeypatch):
    answer = push(
        setup_pay, {"Event": "xpay_subscribe_ios_refund_query_notify", "pay_order_id": "nope"}
    ).json()
    assert (answer["result_code"], answer["result_info"], answer["evidence"]) == (
        0,
        "无法核验",
        "暂时无法核验",
    )
    row = ios_order(setup_pay, session)

    def broken(*args, **kwargs):
        raise RuntimeError("数据库连接 mysql://secret")

    monkeypatch.setattr(payments, "ios_decision", broken)
    answer = inquiry(setup_pay, row)
    assert answer == {**answer, **payments.IOS_UNVERIFIABLE}
    assert "secret" not in str(answer)


def test_shutdown_notice_stops_new_sales_and_suggests_every_ios_refund(
    setup_pay, session, monkeypatch
):
    row = ios_order(setup_pay, session)
    task_id = submit(setup_pay, count=51).json()["id"]
    finish(session, task_id, "done", NOW + timedelta(minutes=1))
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(days=3))
    assert inquiry(setup_pay, row)["result_code"] == 1
    setup_pay[1].pay_shutdown_notice = True
    answer = inquiry(setup_pay, row)
    assert answer["result_code"] == 0 and answer["evidence"].endswith("已核实应当退款")
    # 公示后不再卖新的使用期；已有订单照常能看、能处理
    assert setup_pay[0].get("/api/v1/pay/products", headers=HEADERS).status_code == 404
    assert create(setup_pay).status_code == 404
    assert setup_pay[0].get("/api/v1/pay/orders", headers=HEADERS).status_code == 200


def test_order_carries_the_terms_version_it_agreed_to(setup_pay, session):
    row = delivered(setup_pay, session)
    items = setup_pay[0].get("/api/v1/pay/orders", headers=HEADERS).json()["items"]
    assert items[0]["orderId"] == row.id and items[0]["termsVersion"] == "pay-test"


# ---- 命令行：youju pay-refund-due -----------------------------------------------------------


@pytest.fixture
def refund_due_cli(setup_pay, session_factory, monkeypatch):
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    return lambda *args: cli.main(["pay-refund-due", *args])


def test_refund_due_command_marks_and_prints_an_ascii_line(
    setup_pay, session, refund_due_cli, capsys
):
    row = ios_order(setup_pay, session)
    assert (
        refund_due_cli(
            "--order-id", str(row.id), "--reason", "未成年人未经监护人同意购买", "--dry-run"
        )
        == 0
    )
    assert capsys.readouterr().out.strip().splitlines()[-1] == (
        f"refund_due order={row.id} result=marked dry_run=1"
    )
    session.expire_all()
    assert session.get(PayOrder, row.id).refund_due_at is None
    assert refund_due_cli("--order-id", str(row.id), "--reason", "未成年人未经监护人同意购买") == 0
    session.expire_all()
    marked = session.get(PayOrder, row.id)
    assert (
        marked.refund_due_at is not None and marked.refund_due_note == "未成年人未经监护人同意购买"
    )
    assert refund_due_cli("--order-id", str(row.id), "--clear") == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].endswith("result=cleared dry_run=0")
    setup_pay[2].order_type = 0  # 后面这笔按默认渠道付款（不是苹果）
    android = delivered(setup_pay, session)
    assert refund_due_cli("--order-id", str(android.id), "--clear") == 1
    with pytest.raises(SystemExit):
        refund_due_cli("--order-id", str(row.id))  # 原因和 --clear 必须给一个
    with pytest.raises(SystemExit):
        refund_due_cli("--order-id", str(row.id), "--reason", "太短")


# ---- 赠送流水满 3650 天清理（第 9 项） ------------------------------------------------------


def test_cleanup_deletes_gifts_after_3650_days_with_their_segments(setup_pay, session):
    row = delivered(setup_pay, session)
    old_at, new_at = NOW - timedelta(days=3651), NOW - timedelta(days=10)
    for ref_id, at in ((1, old_at), (2, new_at)):
        reward = PassReward(
            user_id=row.user_id,
            kind="correction",
            requested_days=1,
            granted_days=1,
            ref_type="feedback",
            ref_id=ref_id,
            granted_at=at,
        )
        session.add(reward)
        session.flush()
        session.add(
            WorkbenchPass(
                user_id=row.user_id,
                reward_id=reward.id,
                starts_at=at,
                ends_at=at + timedelta(days=1),
            )
        )
    session.commit()
    result = payments.cleanup(session, setup_pay[1], NOW)
    session.commit()
    assert result["pass_rewards"] == 1
    assert session.scalars(select(PassReward.ref_id)).all() == [2]
    assert (
        session.scalar(
            select(func.count(WorkbenchPass.id)).where(WorkbenchPass.reward_id.is_not(None))
        )
        == 1
    )
    # 订单自己的使用期不动
    assert payments.order_pass(session, row) is not None


# ---- 对账告警（Codex 10/28 建议） -----------------------------------------------------------


def test_reconcile_alerts_once_when_bill_and_local_differ(setup_pay, session):
    delivered(setup_pay, session)
    day = advisor_tasks.usage_day(NOW)
    alerts = []
    row = payments.reconcile(session, day, {}, NOW, alert=alerts.append)
    assert row.status == "different" and len(alerts) == 1
    assert "1 笔对不上" in alerts[0] and "out_trade_no" not in alerts[0]
    payments.reconcile(session, day, {}, NOW + timedelta(hours=1), alert=alerts.append)
    assert len(alerts) == 1


def test_missing_bill_columns_alert_once_on_days_with_sales(setup_pay, session):
    delivered(setup_pay, session)
    _, settings, fake = setup_pay
    settings.xpay_bill_order_column = ""
    alerts = []
    day = advisor_tasks.usage_day(NOW)
    for _ in range(2):
        payments.download_reconcile(session, day, fake.client, NOW, alert=alerts.append)
    assert len(alerts) == 1 and "没配账单字段" in alerts[0]
    assert session.scalar(select(PayReconciliation.status)) == "failed"
    payments.download_reconcile(session, date(2026, 1, 1), fake.client, NOW, alert=alerts.append)
    assert len(alerts) == 1  # 那天没有本地订单：不告警
