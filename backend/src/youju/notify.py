"""一次性订阅提醒；定时任务可重复运行，不重试已尝试发送的授权。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from youju.api import v1
from youju.api.me import notification_statement
from youju.config import Settings, get_settings
from youju.models import (
    Document,
    Follow,
    FollowRepostNotification,
    RepostGroup,
    Source,
    Subscription,
    User,
)
from youju.repost import confirmed_members

log = logging.getLogger("youju.notify")


def send_message(payload: dict) -> dict:
    response = httpx.post(
        "http://api.weixin.qq.com/cgi-bin/message/subscribe/send", json=payload, timeout=10
    )
    response.raise_for_status()
    return response.json()


def _payload(row: Subscription, user: User, template: dict, values: dict, page: str) -> dict:
    fields = template.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("模板字段配置无效")
    data = {}
    for field, source in fields.items():
        if source not in values:
            raise ValueError(f"未知模板字段来源：{source}")
        value = str(values[source])
        if field.startswith("thing"):
            value = value[:20]
        data[field] = {"value": value}
    return {"touser": user.openid, "template_id": row.template_id, "page": page, "data": data}


def _deadline(
    session: Session, row: Subscription, stamp: datetime
) -> tuple[dict, str] | str | None:
    document = session.get(Document, row.document_id)
    if document is None:
        return "expired"
    from youju.takedown import is_taken_down

    if is_taken_down(session, document.id):
        return None
    # 发送时再查一遍能不能公开（来源级别可能在订阅之后才改）：不能公开的当作失效，不发
    public = session.scalar(
        select(Document.id)
        .join(Source, Document.source_id == Source.id)
        .where(Document.id == document.id, v1.public_condition())
    )
    if public is None:
        return "expired"
    approved = v1._approved(session, [document.id]).get(document.id, [])
    future = [d for d in approved if v1.deadline_open(d, stamp)]
    if not future:
        return "expired"
    due = min(future, key=lambda d: (d.approved_date, d.approved_time or datetime.min.time()))
    if due.approved_date > stamp.date() + timedelta(days=3):
        return None
    return (
        {"title": document.title, "date": due.approved_date.isoformat(), "kind": due.kind},
        f"pages/detail/index?id={document.id}",
    )


def _new_docs(session: Session, row: Subscription) -> tuple[dict, str] | str | None:
    follow = session.get(Follow, row.follow_id)
    if follow is None:
        return "expired"
    stmt = notification_statement(session, follow)
    count = v1._count(session, stmt)
    if not count:
        return None
    members = confirmed_members()
    group_ids = session.scalars(
        select(members.c.group_id)
        .distinct()
        .where(members.c.document_id.in_(stmt.with_only_columns(Document.id)))
    ).all()
    doc, _ = session.execute(v1._ordered(stmt).limit(1)).one()
    info = v1.repost_versions(session, [doc.id]).get(doc.id)
    versions = len(info["others"]) if info else 0
    group_summary = f"另有 {versions} 个转发版本" if versions else ""
    prefix, suffix = "你关注的'", f"'有 {count} 份新文件"
    summary = prefix + follow.name[: max(0, 20 - len(prefix) - len(suffix))] + suffix
    return (
        {
            "name": (
                follow.name[: max(0, 19 - len(group_summary))] + " " + group_summary
                if group_summary
                else follow.name
            ),
            "count": count,
            "summary": (f"{count} 份新文件，{group_summary}" if group_summary else summary),
            "title": (
                doc.title[: max(0, 19 - len(group_summary))] + " " + group_summary
                if group_summary
                else doc.title
            ),
            "versions": versions,
            "group_summary": group_summary,
            "_group_ids": group_ids,
        },
        f"pages/detail/index?id={doc.id}" if info else "pages/follow/index",
    )


def run_notify(
    session: Session,
    *,
    settings: Settings | None = None,
    sender: Callable[[dict], dict] = send_message,
    stamp: datetime | None = None,
) -> dict[str, int]:
    """正式模式锁住未用授权并消费；演练模式只记录候选，不修改数据库。"""
    settings = settings or get_settings()
    stamp = (stamp or v1.now()).astimezone(v1._TZ)
    result = {"sent": 0, "failed": 0, "refused": 0, "expired": 0, "skipped": 0, "dry_run": 0}
    ids = session.scalars(
        select(Subscription.id).where(Subscription.status == "unused").order_by(Subscription.id)
    ).all()
    for row_id in ids:
        stmt = select(Subscription).where(
            Subscription.id == row_id, Subscription.status == "unused"
        )
        if settings.send_wx_messages:
            stmt = stmt.with_for_update(skip_locked=True)
        row = session.scalar(stmt)
        if row is None:
            continue
        target = (
            _deadline(session, row, stamp) if row.kind == "deadline" else _new_docs(session, row)
        )
        if target == "expired":
            if settings.send_wx_messages:
                row.status, row.attempted_at = "expired", stamp
                session.commit()
            result["expired"] += 1
            continue
        template = (
            settings.template_deadline if row.kind == "deadline" else settings.template_newdocs
        )
        if target is None or not template or not template.get("id") or not template.get("fields"):
            result["skipped"] += 1
            continue
        if row.template_id != template["id"]:
            result["skipped"] += 1
            continue
        values, page = target
        user = session.get(User, row.user_id)
        # 协议更新后还没重新同意的用户先不发（授权保留，重新同意后照常发）
        if user.agreement_version != settings.agreement_version:
            result["skipped"] += 1
            continue
        try:
            payload = _payload(row, user, template, values, page)
        except ValueError:
            log.exception("订阅模板配置无效，跳过授权 #%s", row.id)
            result["skipped"] += 1
            continue
        if not settings.send_wx_messages:
            log.info("演练：授权 #%s，类型 %s，页面 %s", row.id, row.kind, page)
            result["dry_run"] += 1
            continue
        # 先消费授权再发 HTTP；进程在请求途中退出也不会重复发送。
        row.status, row.errcode, row.attempted_at = "failed", -1, stamp
        session.commit()
        try:
            response: dict[str, Any] = sender(payload)
            errcode = int(response["errcode"])
        except Exception:  # noqa: BLE001 - 任何发送器异常都要消费授权并记录失败
            log.exception("订阅消息发送失败：授权 #%s", row.id)
            errcode = -1
        # 用户可在请求飞行期间注销；已被级联删除的记录不能再写回。
        if session.scalar(select(Subscription.id).where(Subscription.id == row_id)) is None:
            session.rollback()
            continue
        row.errcode = errcode
        row.status = "sent" if errcode == 0 else "refused" if errcode == 43101 else "failed"
        if row.kind == "newDocs" and row.status == "sent":
            follow = session.get(Follow, row.follow_id)
            if follow is not None:
                follow.last_notified_at = stamp
                # 发送期间组可能被移除；锁住仍存在的组再记录，和管理台一样按组编号加锁。
                group_ids = (
                    session.scalars(
                        select(RepostGroup.id)
                        .where(RepostGroup.id.in_(values["_group_ids"]))
                        .order_by(RepostGroup.id)
                        .with_for_update()
                    ).all()
                    if values["_group_ids"]
                    else []
                )
                if group_ids:
                    session.execute(
                        insert(FollowRepostNotification)
                        .values(
                            [
                                {"follow_id": follow.id, "group_id": group_id, "notified_at": stamp}
                                for group_id in group_ids
                            ]
                        )
                        .on_duplicate_key_update(notified_at=stamp)
                    )
        session.commit()
        result[row.status] += 1
    return result


def run_cleanup(
    session: Session, *, settings: Settings | None = None, stamp: datetime | None = None
) -> dict[str, int]:
    """按保存期限删除个人信息：埋点、纠错、订阅授权（已用完的、长期没用上的），期限见配置和隐私保护指引。"""
    from sqlalchemy import delete

    from youju.models import Event, Feedback, JobRun

    settings = settings or get_settings()
    stamp = stamp or v1.now()
    result = {
        "job_runs": session.execute(
            delete(JobRun).where(JobRun.started_at < stamp - timedelta(days=90))
        ).rowcount,
        "events": session.execute(
            delete(Event).where(
                Event.created_at < stamp - timedelta(days=settings.retain_events_days)
            )
        ).rowcount,
        "feedback": session.execute(
            delete(Feedback).where(
                Feedback.created_at < stamp - timedelta(days=settings.retain_feedback_days)
            )
        ).rowcount,
        "subscriptions": session.execute(
            delete(Subscription).where(
                Subscription.status != "unused",
                Subscription.attempted_at
                < stamp - timedelta(days=settings.retain_used_subscriptions_days),
            )
        ).rowcount,
        # 一直没用上的授权（如关注长期没有新文件）也不无限期保存
        "unused_subscriptions": session.execute(
            delete(Subscription).where(
                Subscription.status == "unused",
                Subscription.created_at
                < stamp - timedelta(days=settings.retain_unused_subscriptions_days),
            )
        ).rowcount,
    }
    # 工具任务的名单和导出文件有单独的保存期限；保存期限不受功能开关影响。
    from youju.advisor_tasks import cleanup_advisor
    from youju.payments import cleanup as cleanup_payments

    result.update(cleanup_advisor(session, settings=settings, stamp=stamp))
    result.update(cleanup_payments(session, settings, stamp))
    session.commit()
    return result
