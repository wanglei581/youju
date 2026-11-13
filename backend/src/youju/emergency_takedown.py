"""服务器上的紧急下架：电脑（公开数据的主库）不在线时，也能马上让一份文件在小程序里不对外（D-127）。

为什么另记一张表：服务器上的公开数据是本机同步过来的，直接改文件的下架时间，下一次同步就盖回去；
服务器上也不能写下架记录（document_takedown_events 是同步表，服务器上新加的行会占掉本机要推的
编号、挡住同步）。所以紧急下架只写 emergency_takedowns：只在服务器上用，不同步、不进公开快照，
随服务器的用户数据备份。

- 下架（add）：只在服务器（public_replica）上能做。和正式下架一样，归并组里每一份各记一行；
  没撤销的行指的文件所在的组整组不对外（takedown.available_condition）：列表、搜索、详情、AI、
  导出、提醒都没有它，打开详情显示"该文件已下架"。现在已经不对外的文件不重复记。
- 只管服务器（小程序）：本机不知道，本机生成的周报和临期提醒草稿、给本机测试用的公开快照里还会有它，
  电脑恢复以后要尽快带回本机。
- 撤销（lift）：下错了就撤销这份文件所在组的。行不删，撤销的人、时间、说明都记着。已经带回本机
  正式下架的，服务器上撤销不了它的正式下架，要在本机核对台恢复上架。
- 带回本机（export）：把没撤销的紧急下架写成下架清单（takedown_carry 的格式：规范网址键、
  来源编号、下架时间、原因类别，加上这一次紧急下架的编号；不带说明和操作人，说明可能写到
  投诉人），本机 takedown-carry apply --origin emergency 整组正式下架，随同步推回服务器。
- 自动撤销（settle，服务器每小时）：只认带回本机时写的、带着这一次编号的下架记录（操作人
  "服务器紧急下架#编号"，takedown_carry.emergency_operator）——本机因为别的原因下过架又恢复了、
  早先导出的旧清单晚到了，都不能把这一次的投诉一起撤掉；本机原来就下架着、本机没有这个网址的，
  带回时不写记录，紧急下架留着，要人看过再撤。本机删了这份文件（同步删除，文件编号置空）的也撤掉，
  记录和规范网址留着。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from youju.crawler.urls import url_key
from youju.dedup import canonical_ids, group_ids
from youju.models import Document, DocumentTakedownEvent, EmergencyTakedown
from youju.takedown import available_condition, is_taken_down
from youju.takedown_carry import EMERGENCY_OPERATOR, REASONS

# 原因只记核对台下架的四类；命令行可以写英文（服务器网页终端里中文不好打）
REASON_ALIASES = {
    "complaint": "侵权投诉",
    "authority": "主管部门要求",
    "error": "内容有误",
    "other": "其他",
}
OPERATOR = "服务器命令行"
SYSTEM = "系统"
SETTLE_NOTE = "已经带回本机正式下架（同步过来了），紧急下架自动撤销"
GONE_NOTE = "文件已经不在了（本机删了），紧急下架自动撤销"
SYSTEM_NOTES = (SETTLE_NOTE, GONE_NOTE)
LOCK_TRIES = 3
FIND_LIMIT = 20
NOTE_MAX = 300
BY_MAX = 80
_TZ = ZoneInfo("Asia/Shanghai")
# 不带 http(s):// 的网址（如 jnhrss.jinan.gov.cn/col/col18578/index.html）也按网址找
_BARE_URL = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(?:[/?#]|$)")


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
    batch: str
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


def _operator(value: str | None) -> str:
    operator = _text(value, BY_MAX, "操作人") or OPERATOR
    if operator == SYSTEM:
        # "系统"只给自动撤销用：列表照常显示系统撤销的说明，人填的说明默认不显示
        raise EmergencyError(f"操作人不能写“{SYSTEM}”")
    return operator


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def find(session: Session, query: str) -> list[Found]:
    """按文件编号、网址（按规范网址键比，可以不带 http://）或标题里的字找文件。
    最多 20 份，新的在前。"""
    query = (query or "").strip()
    if not query:
        raise EmergencyError("要给文件编号、网址或标题里的字")
    if query.isascii() and query.isdigit():
        condition = Document.id == int(query)
    elif "://" in query:
        condition = Document.url_key == url_key(query)
    elif _BARE_URL.match(query):
        condition = Document.url_key == url_key("http://" + query)
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


def _members(session: Session, document_id: int) -> list[int]:
    master = canonical_ids(session, [document_id]).get(document_id, document_id)
    return sorted(group_ids(session, master))


def _lock_group(session: Session, document_id: int) -> list[int]:
    """按编号顺序锁住这份文件所在的整组（和 takedown.change_state 同一顺序，不会互相死锁），
    返回锁到的编号：同组不同文件同时下架、撤销时一个一个来，后来的看得到先来的结果。
    先和同步写入排队（sync.server.queue_with_sync，一直到提交）：这期间同步改不了归并；锁上以后
    再读一遍归并，变了（没收过同步的库不排队）就放开重来，最多 3 次。"""
    from youju.sync.server import queue_with_sync

    for _ in range(LOCK_TRIES):
        queue_with_sync(session.connection())
        ids = _members(session, document_id)
        locked = list(
            session.scalars(
                select(Document.id)
                .where(Document.id.in_(ids))
                .order_by(Document.id)
                .with_for_update()
            )
        )
        if _members(session, document_id) == ids:
            return locked
        session.rollback()
    raise EmergencyError("归并关系一直在变（多半正在同步），过一会儿再试")


def add(
    session: Session, document_id: int, *, reason: str, note: str = "", operator: str = OPERATOR
) -> list[EmergencyTakedown]:
    """紧急下架一份文件：和正式下架一样，归并组里每一份各记一行。返回记下的行。"""
    reason = reason_of(reason)
    note = _text(note, NOTE_MAX, "说明")
    operator = _operator(operator)
    ids = _lock_group(session, document_id)
    if document_id not in ids:
        session.rollback()
        raise EmergencyError(f"没有编号 {document_id} 的文件")
    if is_taken_down(session, document_id):
        session.rollback()
        raise EmergencyError(
            f"文件 #{document_id} 现在已经不对外了（已经下架或紧急下架过），不用再下"
        )
    stamp = datetime.now(UTC)
    batch = uuid.uuid4().hex
    rows = [
        EmergencyTakedown(
            document_id=doc_id,
            url_key=key,
            batch=batch,
            reason=reason,
            note=note,
            created_by=operator,
            created_at=stamp,
        )
        for doc_id, key in session.execute(
            select(Document.id, Document.url_key).where(Document.id.in_(ids)).order_by(Document.id)
        )
    ]
    session.add_all(rows)
    session.commit()
    return rows


def lift(
    session: Session, document_id: int, *, note: str = "", operator: str = OPERATOR
) -> tuple[int, bool]:
    """撤销这份文件所在归并组里没撤销的紧急下架。返回撤销了几行、撤销后这份文件是否对外可见。"""
    note = _text(note, NOTE_MAX, "说明")
    operator = _operator(operator)
    # 先锁整组再找：正在提交的同组紧急下架等它提交完，一起撤
    ids = _lock_group(session, document_id)
    if document_id not in ids:
        session.rollback()
        raise EmergencyError(f"没有编号 {document_id} 的文件")
    rows = session.scalars(
        select(EmergencyTakedown)
        .where(EmergencyTakedown.document_id.in_(ids), EmergencyTakedown.lifted_at.is_(None))
        .order_by(EmergencyTakedown.id)
        .with_for_update()
    ).all()
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
            batch=row.batch,
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
    """没撤销、文件还在的紧急下架写成下架清单（JSON Lines，takedown_carry.parse 读得了），
    一行一份文件；同一个网址只出一行（取最早的紧急下架）。"""
    rows = session.execute(
        select(EmergencyTakedown, Document.url_key, Document.source_id)
        .join(Document, Document.id == EmergencyTakedown.document_id)
        .where(EmergencyTakedown.lifted_at.is_(None))
        .order_by(EmergencyTakedown.created_at, EmergencyTakedown.id)
    ).all()
    lines: dict[str, str] = {}
    # 用文件现在的规范网址（本机同步过来的，和本机一定一致）；下架时存的那份只给文件删了以后看
    for row, key, source_id in rows:
        lines.setdefault(
            key,
            json.dumps(
                {
                    "url_key": key,
                    "origin_source_id": source_id,
                    "taken_down_at": row.created_at.astimezone(UTC).isoformat(),
                    "reason": row.reason,
                    "emergency": row.batch,
                },
                ensure_ascii=False,
            ),
        )
    return list(lines.values())


def settle(session: Session) -> int:
    """带回本机已经正式下架的、文件已经不在了的紧急下架自动撤销（撤销人"系统"）。返回撤销了几行。
    只认带着这一次编号的下架记录：操作人"服务器紧急下架#编号"（takedown_carry.emergency_operator）。"""
    carried = exists().where(
        DocumentTakedownEvent.document_id == EmergencyTakedown.document_id,
        DocumentTakedownEvent.action == "takedown",
        DocumentTakedownEvent.operated_by
        == func.concat(EMERGENCY_OPERATOR, "#", EmergencyTakedown.batch),
    )
    rows = session.scalars(
        select(EmergencyTakedown)
        .where(
            EmergencyTakedown.lifted_at.is_(None),
            EmergencyTakedown.document_id.is_(None) | carried,
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


def describe_records(rows: Iterable[Record], *, notes: bool = False) -> list[str]:
    """同一次紧急下架（同一个编号、撤销情况一样）的几行合成一条。说明默认不显示（可能写到
    投诉人），notes=True 才显示；系统自动撤销的说明是固定的几句，照常显示。"""
    groups: dict[tuple, list[Record]] = {}
    for row in rows:
        key = (row.batch, row.lifted_at, row.lifted_by)
        groups.setdefault(key, []).append(row)
    lines = []
    for members in groups.values():
        first = members[0]
        what = "、".join(
            f"#{row.document_id}" if row.document_id is not None else f"已删（{row.url_key}）"
            for row in sorted(members, key=lambda r: (r.document_id is None, r.document_id or 0))
        )
        title = next((row.title for row in members if row.title), "")
        lines.append(
            f"紧急下架 #{first.batch[:8]}  文件 {what}  {first.reason}  "
            f"{_beijing(first.created_at)}  {first.created_by}  {_short(title, 40)}"
        )
        if first.note:
            lines.append(f"    说明：{first.note}" if notes else "    有说明（加 --notes 看）")
        if first.lifted_at is not None:
            lift = f"    已撤销：{_beijing(first.lifted_at)}  {first.lifted_by or ''}"
            if first.lift_note:
                # 系统自动撤销的说明是固定的几句（人不能用"系统"当操作人），照常显示
                shown = notes or (first.lifted_by == SYSTEM and first.lift_note in SYSTEM_NOTES)
                lift += f"  {first.lift_note}" if shown else "  （有说明）"
            lines.append(lift)
    return lines
