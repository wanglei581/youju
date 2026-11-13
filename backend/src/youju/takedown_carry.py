"""首发前搬家（D-127）：把云托管上已下架的文件带到本机。

本机是公开数据的主库，它的数据来自不含下架文件的公开导出（public_snapshot 不导出下架的文件和
下架记录），所以不知道哪些通知下架过；本机 11/8 起自己在抓，下架过、但原网站列表上还挂着的通知，
可能已经当成新文件收进来、推到了服务器。

- 清单：云托管导出脚本（backend/deploy/selfhost/shared/cloudrun-export.py）生成，JSON Lines，
  每行一个网址：当前处于下架状态的归并组里每一份文件的规范网址键、原来的来源编号、下架时间、
  原因类别。不带备注、操作人、标题（可能写到投诉人或个人信息）。
- youju takedown-carry apply --file（本机）：写进 takedown_blocks，以后再抓到同一个规范网址
  不再收录（crawler/service.py 的 save_items）；本机已经收进来的同网址文件整组下架
  （takedown.change_state），下架状态和记录随同步推到服务器（记录里的备注同步时清空）。
  可以重复执行：已经下架的不重复记。
- youju takedown-carry check --file（本机、服务器都能跑，只读）：清单里的网址在这个库里有没有
  对用户可见的文件，有就退出码 1。

服务器上的紧急下架（youju/emergency_takedown.py）也用这个格式带回本机，每行多一项 emergency
（那一次紧急下架的编号，不含说明）：takedown-carry apply --origin emergency 只把本机已有的同网址
文件整组正式下架，不写拦截表——那些文件本机都有，以后本机恢复上架还要照常更新；下架记录的操作人
记"服务器紧急下架#编号"，服务器按编号认是哪一次带回的；本机没有的网址数会列出来。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from youju.models import URL_KEY_MAX, Document, TakedownBlock
from youju.takedown import available_condition, change_state, is_taken_down

# 和核对台下架的原因类型一致（api/app.py 的 TakedownRequest）；清单里别的值按"其他"记
REASONS = ("侵权投诉", "主管部门要求", "内容有误", "其他")
NOTE = "首发前搬家（D-127）：云托管上已下架，按规范网址带过来"
OPERATOR = "搬家（D-127）"
EMERGENCY_NOTE = "服务器上紧急下架过（电脑不在线时），按规范网址带回本机"
# 紧急下架的编号：32 位小写十六进制（emergency_takedown 里 uuid4().hex）
_EMERGENCY = re.compile(r"[0-9a-f]{32}")
EMERGENCY_OPERATOR = "服务器紧急下架"
_CHUNK = 500


class CarryError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    url_key: str
    origin_source_id: int | None
    taken_down_at: datetime
    reason: str
    emergency: str | None = None  # 服务器紧急下架的编号（只有紧急下架带回的清单有）


@dataclass
class ApplyReport:
    listed: int = 0
    blocks_added: int = 0
    found: int = 0
    taken_down: int = 0
    already_hidden: int = 0


@dataclass
class CheckReport:
    listed: int = 0
    blocked: int = 0
    present: int = 0
    visible: list[str] | None = None


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise CarryError("下架时间要是 ISO 格式的字符串")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse(lines: Iterable[str]) -> list[Item]:
    """读清单。格式不对就整份拒绝（不做一半）；同一个网址出现多次只留一条。"""
    items: dict[str, Item] = {}
    for number, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CarryError(f"第 {number} 行不是 JSON") from exc
        if not isinstance(row, dict):
            raise CarryError(f"第 {number} 行不是一条记录")
        extra = set(row) - {"url_key", "origin_source_id", "taken_down_at", "reason", "emergency"}
        if extra:
            # 清单只该有这几项：多出来的（备注、操作人、标题）可能有个人信息，不收
            raise CarryError(f"第 {number} 行有清单以外的字段：{', '.join(sorted(extra))}")
        emergency = row.get("emergency")
        if emergency is not None and (
            not isinstance(emergency, str) or not _EMERGENCY.fullmatch(emergency)
        ):
            raise CarryError(f"第 {number} 行的紧急下架编号不对")
        key = row.get("url_key")
        if not isinstance(key, str) or not key.strip() or len(key) > URL_KEY_MAX:
            raise CarryError(f"第 {number} 行的规范网址不对")
        source = row.get("origin_source_id")
        if source is not None and (type(source) is not int or source <= 0):
            raise CarryError(f"第 {number} 行的来源编号不对")
        try:
            stamp = _parse_time(row.get("taken_down_at"))
        except ValueError as exc:
            raise CarryError(f"第 {number} 行的下架时间不对") from exc
        reason = row.get("reason")
        reason = reason if reason in REASONS else "其他"
        items.setdefault(key, Item(key, source, stamp, reason, emergency))
    return list(items.values())


def load(path: Path) -> list[Item]:
    with path.open(encoding="utf-8") as fh:
        return parse(fh)


def _chunks(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def emergency_operator(item: Item) -> str:
    """紧急下架带回的下架记录的操作人：带上那一次紧急下架的编号（同步到服务器，settle 按它认）。"""
    return f"{EMERGENCY_OPERATOR}#{item.emergency}"


def apply(
    session: Session,
    items: list[Item],
    *,
    block: bool = True,
    note: str = NOTE,
    operator: str | Callable[[Item], str] = OPERATOR,
) -> ApplyReport:
    """写拦截表（block=False 时不写）；本机已有的同网址文件整组下架。operator 可以按条给。"""
    report = ApplyReport(listed=len(items))
    if not items:
        return report
    keys = [item.url_key for item in items]
    if block:
        _block(session, items, report)
    by_key = {item.url_key: item for item in items}
    operator_of = operator if callable(operator) else (lambda _item: operator)
    found: list[tuple[int, str]] = []
    for part in _chunks(keys):
        found += [
            (doc_id, key)
            for doc_id, key in session.execute(
                select(Document.id, Document.url_key)
                .where(Document.url_key.in_(part))
                .order_by(Document.id)
            )
        ]
    report.found = len(found)
    for doc_id, key in found:
        if is_taken_down(session, doc_id):
            report.already_hidden += 1
            continue
        # 整组下架（归并组里别的副本一起），各副本各记一条下架记录
        item = by_key[key]
        change_state(
            session,
            doc_id,
            action="takedown",
            note=note,
            operator=operator_of(item),
            reason=item.reason,
        )
        report.taken_down += 1
    return report


def _block(session: Session, items: list[Item], report: ApplyReport) -> None:
    keys = [item.url_key for item in items]
    existing: set[str] = set()
    for part in _chunks(keys):
        existing |= set(
            session.scalars(select(TakedownBlock.url_key).where(TakedownBlock.url_key.in_(part)))
        )
    fresh = [item for item in items if item.url_key not in existing]
    for start in range(0, len(fresh), _CHUNK):
        part = fresh[start : start + _CHUNK]
        stmt = mysql_insert(TakedownBlock).values(
            [
                {
                    "url_key": item.url_key,
                    "origin_source_id": item.origin_source_id,
                    "taken_down_at": item.taken_down_at,
                    "reason": item.reason,
                }
                for item in part
            ]
        )
        # 并发重跑时另一边刚写进去的同一个网址：保留先写的
        session.execute(stmt.on_duplicate_key_update(url_key=stmt.inserted.url_key))
    session.commit()
    report.blocks_added = len(fresh)


def check(session: Session, items: list[Item]) -> CheckReport:
    """只读：清单里的网址在这个库里还有没有对用户可见的文件。"""
    report = CheckReport(listed=len(items), visible=[])
    keys = [item.url_key for item in items]
    for part in _chunks(keys):
        report.blocked += len(
            session.scalars(select(TakedownBlock.id).where(TakedownBlock.url_key.in_(part))).all()
        )
        report.present += len(
            session.scalars(select(Document.id).where(Document.url_key.in_(part))).all()
        )
        report.visible += list(
            session.scalars(
                select(Document.url_key).where(Document.url_key.in_(part), available_condition())
            )
        )
    return report
