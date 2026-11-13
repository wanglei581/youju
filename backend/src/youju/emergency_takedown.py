"""服务器上的紧急下架：电脑（公开数据的主库）不在线时，也能马上让一份文件不对外（D-127）。

为什么另记一张表：服务器上的公开数据是本机同步过来的，直接改文件的下架时间，下一次同步就盖回去；
服务器上也不能写下架记录（document_takedown_events 是同步表，服务器上新加的行会占掉本机要推的
编号、挡住同步）。所以紧急下架只写 emergency_takedowns：只在服务器上用，不同步、不进公开快照，
随服务器的用户数据备份。

- 下架（add）：只在服务器（public_replica）上能做。没撤销的行指的文件所在的归并组整组不对外，
  和正式下架同一口径（takedown.available_condition）：列表、搜索、详情、AI、导出、提醒都没有它，
  打开详情显示"该文件已下架"。现在已经不对外的文件不重复记。
- 撤销（lift）：下错了就撤销。行不删，撤销的人、时间、说明都记着。已经带回本机正式下架的，
  服务器上撤销不了它的正式下架，要在本机核对台恢复上架。
- 带回本机（export）：把没撤销的紧急下架写成下架清单（takedown_carry 的格式：规范网址键、
  来源编号、下架时间、原因类别；不带说明和操作人，说明可能写到投诉人），本机开机后
  takedown-carry apply --origin emergency 整组正式下架，随同步推回服务器。不带回也一直有效。
- 自动撤销（settle，服务器每小时）：文件已经正式下架了、或者本机在紧急下架之后对它做过正式下架
  （同步过来的下架记录），紧急下架就撤掉（撤销人"系统"）：以后这份文件上不上架照本机的。
  本机删了这份文件（同步删除，文件编号置空）的也撤掉，记录和下架时的规范网址留着。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from youju.crawler.urls import url_key
from youju.dedup import canonical_ids, group_ids
from youju.models import Document, DocumentTakedownEvent, EmergencyTakedown
from youju.takedown import available_condition, is_taken_down
from youju.takedown_carry import REASONS

# 原因只记核对台下架的四类；命令行可以写英文（服务器网页终端里中文不好打）
REASON_ALIASES = {
    "complaint": "侵权投诉",
    "authority": "主管部门要求",
    "error": "内容有误",
    "other": "其他",
}
OPERATOR = "服务器命令行"
SYSTEM = "系统"
SETTLE_NOTE = "本机已经正式下架（同步过来了），紧急下架自动撤销"
GONE_NOTE = "文件已经不在了（本机删了），紧急下架自动撤销"
FIND_LIMIT = 20
_TZ = ZoneInfo("Asia/Shanghai")
NOTE_MAX = 300
BY_MAX = 80


class EmergencyError(ValueError):
    pass


@dataclass(frozen=True)
class Found:
    id: int
    source_id: int
    publish_date: str
    title: str
    url: str
    visible: bool


@dataclass(frozen=True)
class Record:
    id: int
    document_id: int | None
    url_key: str
    reason: str
    note: str
    created_by: str
    created_at: datetime
    lifted_at: datetime | None
    lifted_by: str | None
    lift_note: str | None
    title: str | None


def reason_of(value: str) -> str:
    value = (value or "").strip()
    reason = REASON_ALIASES.get(value.lower(), value)
    if reason not in REASONS:
        raise EmergencyError(
            "原因要是 complaint（侵权投诉）、authority（主管部门要求）、error（内容有误）、"
            "other（其他）之一"
        )
    return reason


def _text(value: str | None, limit: int, what: str) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        raise EmergencyError(f"{what}最多 {limit} 个字")
    return value


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def find(session: Session, query: str) -> list[Found]:
    """按文件编号、网址（按规范网址键比）或标题里的字找文件，最多 20 份，新的在前。"""
    query = (query or "").strip()
    if not query:
        raise EmergencyError("要给文件编号、网址或标题里的字")
    if query.isascii() and query.isdigit():
        condition = Document.id == int(query)
    elif "://" in query:
        condition = Document.url_key == url_key(query)
    else:
        condition = Document.title.like(_like(query), escape="\\")
    visible = available_condition().label("visible")
    rows = session.execute(
        select(
            Document.id,
            Document.source_id,
            Document.publish_date,
            Document.title,
            Document.url,
            visible,
        )
        .where(condition)
        .order_by(Document.id.desc())
        .limit(FIND_LIMIT)
    ).all()
    return [
        Found(
            id=row.id,
            source_id=row.source_id,
            publish_date=row.publish_date.isoformat() if row.publish_date else "",
            title=row.title,
            url=row.url,
            visible=bool(row.visible),
        )
        for row in rows
    ]


def _group(session: Session, document_id: int) -> list[int]:
    master = canonical_ids(session, [document_id]).get(document_id, document_id)
    return group_ids(session, master)


def add(
    session: Session, document_id: int, *, reason: str, note: str = "", operator: str = OPERATOR
) -> tuple[EmergencyTakedown, list[int]]:
    """紧急下架一份文件（整组不对外）。返回这一行和组里的文件编号。"""
    reason = reason_of(reason)
    note = _text(note, NOTE_MAX, "说明")
    operator = _text(operator, BY_MAX, "操作人") or OPERATOR
    # 锁住这份文件：同一份同时下两次只记一条
    doc = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if doc is None:
        session.rollback()
        raise EmergencyError(f"没有编号 {document_id} 的文件")
    if is_taken_down(session, document_id):
        session.rollback()
        raise EmergencyError(
            f"文件 #{document_id} 现在已经不对外了（已经下架或紧急下架过），不用再下"
        )
    row = EmergencyTakedown(
        document_id=document_id,
        url_key=doc.url_key,
        reason=reason,
        note=note,
        created_by=operator,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return row, _group(session, document_id)


def _active_in_group(session: Session, document_id: int) -> list[EmergencyTakedown]:
    ids = _group(session, document_id)
    return list(
        session.scalars(
            select(EmergencyTakedown)
            .where(EmergencyTakedown.document_id.in_(ids), EmergencyTakedown.lifted_at.is_(None))
            .order_by(EmergencyTakedown.id)
            .with_for_update()
        )
    )


def lift(
    session: Session, document_id: int, *, note: str = "", operator: str = OPERATOR
) -> tuple[int, bool]:
    """撤销这份文件所在归并组里没撤销的紧急下架。返回撤销了几条、撤销后这份文件是否对外可见。"""
    note = _text(note, NOTE_MAX, "说明")
    operator = _text(operator, BY_MAX, "操作人") or OPERATOR
    if session.get(Document, document_id) is None:
        raise EmergencyError(f"没有编号 {document_id} 的文件")
    rows = _active_in_group(session, document_id)
    if not rows:
        session.rollback()
        raise EmergencyError(f"文件 #{document_id} 所在的组没有紧急下架着的")
    stamp = datetime.now(UTC)
    for row in rows:
        row.lifted_at, row.lifted_by, row.lift_note = stamp, operator, note
    session.commit()
    return len(rows), not is_taken_down(session, document_id)


def records(session: Session, *, include_lifted: bool = False) -> list[Record]:
    """紧急下架的记录（默认只列没撤销的），新的在前。"""
    query = (
        select(EmergencyTakedown, Document.title)
        .outerjoin(Document, Document.id == EmergencyTakedown.document_id)
        .order_by(EmergencyTakedown.id.desc())
    )
    if not include_lifted:
        query = query.where(EmergencyTakedown.lifted_at.is_(None))
    return [
        Record(
            id=row.id,
            document_id=row.document_id,
            url_key=row.url_key,
            reason=row.reason,
            note=row.note,
            created_by=row.created_by,
            created_at=row.created_at,
            lifted_at=row.lifted_at,
            lifted_by=row.lifted_by,
            lift_note=row.lift_note,
            title=title,
        )
        for row, title in session.execute(query)
    ]


def export_lines(session: Session) -> list[str]:
    """没撤销的紧急下架写成下架清单（JSON Lines，takedown_carry.parse 读得了）：每条带上它所在
    归并组里每一份文件的规范网址；同一个网址只出一行（取最早的紧急下架）。"""
    rows = session.scalars(
        select(EmergencyTakedown)
        .where(EmergencyTakedown.lifted_at.is_(None), EmergencyTakedown.document_id.is_not(None))
        .order_by(EmergencyTakedown.created_at, EmergencyTakedown.id)
    ).all()
    lines: dict[str, str] = {}
    for row in rows:
        ids = _group(session, row.document_id)
        stamp = row.created_at.astimezone(UTC).isoformat()
        for key, source_id in session.execute(
            select(Document.url_key, Document.source_id)
            .where(Document.id.in_(ids))
            .order_by(Document.id)
        ):
            if key in lines:
                continue
            lines[key] = json.dumps(
                {
                    "url_key": key,
                    "origin_source_id": source_id,
                    "taken_down_at": stamp,
                    "reason": row.reason,
                },
                ensure_ascii=False,
            )
    return list(lines.values())


def _formally_taken_over(row_document_id, row_created_at):
    """文件现在正式下架着，或者本机在紧急下架之后对它做过正式下架（同步过来的下架记录）。"""
    later = exists().where(
        DocumentTakedownEvent.document_id == row_document_id,
        DocumentTakedownEvent.action == "takedown",
        DocumentTakedownEvent.operated_at >= row_created_at,
    )
    taken = exists().where(Document.id == row_document_id, Document.taken_down_at.is_not(None))
    return taken | later


def settle(session: Session) -> int:
    """本机已经接手的、文件已经不在了的紧急下架自动撤销（撤销人"系统"）。返回撤销了几条。"""
    rows = session.scalars(
        select(EmergencyTakedown)
        .where(
            EmergencyTakedown.lifted_at.is_(None),
            EmergencyTakedown.document_id.is_(None)
            | _formally_taken_over(EmergencyTakedown.document_id, EmergencyTakedown.created_at),
        )
        .order_by(EmergencyTakedown.id)
        .with_for_update()
    ).all()
    stamp = datetime.now(UTC)
    for row in rows:
        note = GONE_NOTE if row.document_id is None else SETTLE_NOTE
        row.lifted_at, row.lifted_by, row.lift_note = stamp, SYSTEM, note
    session.commit()
    return len(rows)


def _short(title: str, limit: int = 60) -> str:
    return title if len(title) <= limit else title[:limit] + "…"


def _beijing(stamp: datetime) -> str:
    return stamp.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")


def describe_found(found: Iterable[Found]) -> list[str]:
    lines = []
    for item in found:
        state = "对外可见" if item.visible else "不对外"
        lines.append(
            f"文件 #{item.id}  {state}  {item.publish_date or '无日期'}  来源 {item.source_id}  "
            f"{_short(item.title)}"
        )
        lines.append(f"    {item.url}")
    return lines


def describe_records(rows: Iterable[Record]) -> list[str]:
    lines = []
    for row in rows:
        what = (
            f"文件 #{row.document_id}"
            if row.document_id is not None
            else f"文件已删（网址 {row.url_key}）"
        )
        lines.append(
            f"{what}  {row.reason}  {_beijing(row.created_at)}  {row.created_by}  "
            f"{_short(row.title or '', 40)}"
        )
        if row.note:
            lines.append(f"    说明：{row.note}")
        if row.lifted_at is not None:
            lift = f"    已撤销：{_beijing(row.lifted_at)}  {row.lifted_by or ''}"
            lines.append(lift + (f"  {row.lift_note}" if row.lift_note else ""))
    return lines
