"""原网页失效的通知 7 天后清理（D-123，创始人 11/8）。

什么算失效沿用原网页检查（youju/link_check.py）：连续两次、相隔 20 小时以上打不开，记
documents.link_gone_at。记下那天（北京时间）起 7 天是清理期：

- 这 7 天里正文、附件照常给看（详情页、阅读页：v1.reading_body、attachment_text.judge），页面写明
  "原网页某月某日起打不开，内容可能已被发布机关撤回或更正，以官方为准"和哪天清理，可以保存
  （我的保存，youju/saved.py）、导出。
- 期间每天再查一次（link_check 的清理期队列）：能打开了，link_check 清空 link_gone_at，清理就取消；
  同一份通知在原栏目换了新地址的（moved_condition：同一栏目、同一标题、原网页没失效的另一份）也不清理。
- 失效那天 + 7 天起（cleanup_on）不再给正文和附件（照 D-097），本机原网页检查跑完以后删掉这份通知
  （cleanup_expired）；2.0 里删除经同步通道带到服务器（PR #453），用户保存的副本不受影响
  （saved_documents 不建外键）。
- 因投诉下架的不算官方失效：照原来的做法下架（行留着、隐藏），不清理；用户保存的副本一起删
  （saved.purge_taken_down）。

开关 YOUJU_GONE_CLEANUP_ENABLED，默认关：关着照 D-097，失效的正文、附件不给，不清理，
不能保存、导出。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from youju.config import Settings, get_settings
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentMerge,
    DocumentTakedownEvent,
    Source,
)
from youju.takedown import available_condition

log = logging.getLogger("youju.gone")
BEIJING = ZoneInfo("Asia/Shanghai")
DAYS = 7  # 失效那天算第 1 天，第 8 天清理（D-123：7 天后清理）
# 清理前最近一次确认原网页还打不开（documents.link_gone_seen_at，判断不了的不算）要在这么久以内：
# 清理期里每天查、排最前，清理那天早上一般刚确认过；没轮到、或网站临时出故障判断不了，就推迟
# （D-123 ①：网站临时出故障不能误删）
FRESH = timedelta(hours=30)
# 原栏目这么久没抓成功（sources.last_success_at）：不知道它还列不列着这份通知，推迟
# （独立审阅第 1 轮）
SOURCE_FRESH = timedelta(hours=48)
# 失效后这么久以内的每天查（清理期 7 天，留几天给检查没轮到、清理推迟的）：link_check 的清理期队列
RECHECK_SPAN = timedelta(days=DAYS + 3)
# 原栏目列表这么久以内还列着、又是失效以后看到的：还挂在原栏目里，先不清理（删了下一轮抓取会把它
# 当新文件重新收进来，关注的人还会收到"新文件"）
LISTED_WITHIN = timedelta(hours=48)

# 原网页失效、到期也不清理的几种情况（页面照 D-097 显示"原文链接已失效"，不写清理日期、不能保存）
KEEP_REASONS = {
    # D-123 ④：名单类（查名单用到的，失效的多是公示期满后撤下的）不按 7 天清理，照常留着、
    # 查名单照常能查到，卡片和详情页标明"原网页某月某日起已撤下"；这 7 天里正文和别的通知一样
    # 照常给、能保存导出（7 天过后给不给没定，照现在不给，D-123 ⑥）
    "list": "名单类，不按 7 天清理",
    "history": "有下架、恢复记录（记录要留着，删不了这份通知）",
    "changed": "别的通知的截止变更指向它",
    "listed": "原栏目还列着这份通知",
    "merged": "同一份通知的其他网址还能打开",
    "moved": "同一份通知在原栏目换了新地址",
    # 只在删的时候看（keep_reasons 的 cleanup=True）：页面照常按自己的 7 天显示
    "waiting": "同一份通知的其他网址还在 7 天里，等最后一个到期一起删",
}


def enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).gone_cleanup_enabled)


def saving_open(settings: Settings | None = None) -> bool:
    """能保存（我的保存）：开关开着，而且服务器现在的协议版本已经写进"您保存的通知"
    （agreements.SAVED_AGREEMENT_VERSION）。新版指引还没上线时不给保存按钮，免得点了才被拒、
    又请用户重新同意一个服务器还不认的版本（独立审阅第 1 轮）。"""
    from youju.agreements import SAVED_AGREEMENT_VERSION, agreement_at_least

    settings = settings or get_settings()
    return enabled(settings) and agreement_at_least(
        settings.agreement_version, SAVED_AGREEMENT_VERSION
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def beijing_date(value: datetime) -> date:
    return _utc(value).astimezone(BEIJING).date()


def gone_on(gone_at: datetime | None) -> date | None:
    """原网页确认打不开的那天（北京时间）。"""
    return beijing_date(gone_at) if gone_at is not None else None


def cleanup_on(gone_at: datetime | None) -> date | None:
    """清理那天：失效那天 + 7 天。这天起正文、附件不再给，本机当天原网页检查跑完以后删掉。"""
    day = gone_on(gone_at)
    return day + timedelta(days=DAYS) if day is not None else None


def cutoff(now: datetime) -> datetime:
    """link_gone_at 不早于这个时刻（UTC）的还在清理期里：失效那天不早于"今天往前 6 天"。"""
    first = beijing_date(now) - timedelta(days=DAYS - 1)
    return datetime.combine(first, time(0), tzinfo=BEIJING).astimezone(UTC)


def viewable(gone_at: datetime | None, now: datetime, settings: Settings | None = None) -> bool:
    """原网页失效、还在清理期里（开关开着）：正文、附件照常给，可以保存、导出。"""
    return gone_at is not None and enabled(settings) and _utc(gone_at) >= cutoff(now)


def blocked(gone_at: datetime | None, now: datetime, settings: Settings | None = None) -> bool:
    """原网页失效、不给正文和附件：开关关着（D-097），或者过了清理期还没删掉。"""
    return gone_at is not None and not viewable(gone_at, now, settings)


def readable_condition(column, now: datetime, settings: Settings | None = None):
    """SQL 条件：这份的正文能用（原网页没失效，或者开关开着、还在清理期里）。"""
    if not enabled(settings):
        return column.is_(None)
    return column.is_(None) | (column >= cutoff(now))


def md(day: date) -> str:
    return f"{day.month}月{day.day}日"


def notice(gone_at: datetime) -> str:
    """卡片、详情页、阅读页写的提示（D-123 ②）。"""
    return f"原网页{md(gone_on(gone_at))}起打不开，内容可能已被发布机关撤回或更正，以官方为准"


def list_notice(gone_at: datetime) -> str:
    """名单类的标记（D-123 ④：卡片和详情页标明原网页某月某日起已撤下）。"""
    return f"原网页{md(gone_on(gone_at))}起已撤下"


def is_list(doc: Document) -> bool:
    from youju.classify import effective_kind

    return effective_kind(doc.title, doc.kind_override) == "名单"


def _squash(title: str | None) -> str:
    # 和归并（dedup.match_key）一样只去掉空白
    return re.sub(r"\s+", "", title or "")


def keep_reasons(
    session: Session, docs: Iterable[Document], now: datetime, *, cleanup: bool = False
) -> dict[int, str]:
    """这些原网页失效的通知里，到期也不清理的：{编号: KEEP_REASONS 的键}。一种原因就够，按
    KEEP_REASONS 的顺序给第一种。没失效的不看。cleanup：删的时候另看 waiting（组里别的网址还在
    7 天里）；显示的时候不看它，组里几个网址都在 7 天里时照常显示（agy 第 2 轮）。"""
    docs = [doc for doc in docs if doc.link_gone_at is not None]
    if not docs:
        return {}
    ids = [doc.id for doc in docs]
    found: dict[int, str] = {}

    def mark(doc_id: int, reason: str) -> None:
        found.setdefault(doc_id, reason)

    for doc in docs:
        if is_list(doc):
            mark(doc.id, "list")

    # 下架、恢复记录对 documents 是 RESTRICT：有记录的删不了，也不该删（审计记录要留）
    for doc_id in session.scalars(
        select(DocumentTakedownEvent.document_id)
        .where(DocumentTakedownEvent.document_id.in_(ids))
        .distinct()
    ):
        mark(doc_id, "history")
    # 别的通知"截止已变更、见新通知"指向它（RESTRICT）：新通知删了，旧通知的说明就断了
    for doc_id in session.scalars(
        select(DocumentDeadline.changed_to_document_id)
        .where(DocumentDeadline.changed_to_document_id.in_(ids))
        .distinct()
    ):
        mark(doc_id, "changed")
    for doc in docs:
        seen = _utc(doc.last_seen_at) if doc.last_seen_at else None
        if seen and seen > _utc(doc.link_gone_at) and seen >= now - LISTED_WITHIN:
            mark(doc.id, "listed")
    # 归并组（同一份通知的不同网址）里还有原网页没失效、没下架的
    canonical = {
        row.duplicate_id: row.canonical_id
        for row in session.execute(
            select(DocumentMerge.duplicate_id, DocumentMerge.canonical_id).where(
                DocumentMerge.active.is_(True), DocumentMerge.duplicate_id.in_(ids)
            )
        )
    }
    masters = {doc_id: canonical.get(doc_id, doc_id) for doc_id in ids}
    members: dict[int, set[int]] = {master: {master} for master in masters.values()}
    for master, duplicate in session.execute(
        select(DocumentMerge.canonical_id, DocumentMerge.duplicate_id).where(
            DocumentMerge.active.is_(True), DocumentMerge.canonical_id.in_(members)
        )
    ):
        members[master].add(duplicate)
    others = {m for group in members.values() for m in group} - set(ids)
    # 删的时候，组里还在 7 天里的也要等：各网址先后打不开时等最后一个也到期了整组一起删，免得主条目
    # 先删、归并关系跟着没了，转载的那份又单独冒出来（agy 第 1 轮）
    live: set[int] = set()
    waiting: set[int] = set()
    if others:
        for doc_id, gone_at in session.execute(
            select(Document.id, Document.link_gone_at).where(
                Document.id.in_(others), available_condition()
            )
        ):
            if gone_at is None:
                live.add(doc_id)
            elif cleanup and _utc(gone_at) >= cutoff(now):
                waiting.add(doc_id)
    for doc_id, master in masters.items():
        if members[master] & live:
            mark(doc_id, "merged")
    # 原栏目换了新地址：同一栏目、同一标题、原网页没失效、没下架的另一份，发布日期一样，或者是
    # 原网页开始打不开以后才出现的（同名的往年通知不算）
    by_source: dict[int, list[Document]] = {}
    for doc in docs:
        by_source.setdefault(doc.source_id, []).append(doc)
    for source_id, group in by_source.items():
        dates = {doc.publish_date for doc in group if doc.publish_date}
        since = min(_utc(doc.link_suspect_at or doc.link_gone_at) for doc in group)
        condition = Document.first_seen_at >= since
        if dates:
            condition = condition | Document.publish_date.in_(dates)
        rows = session.execute(
            select(Document.title, Document.publish_date, Document.first_seen_at).where(
                Document.source_id == source_id,
                Document.id.not_in(ids),
                Document.link_gone_at.is_(None),
                available_condition(),
                condition,
            )
        ).all()
        for doc in group:
            title = _squash(doc.title)
            began = _utc(doc.link_suspect_at or doc.link_gone_at)
            if title and any(
                _squash(row.title) == title
                and (
                    (doc.publish_date is not None and row.publish_date == doc.publish_date)
                    or _utc(row.first_seen_at) >= began
                )
                for row in rows
            ):
                mark(doc.id, "moved")
    for doc_id, master in masters.items():
        if members[master] & waiting:
            mark(doc_id, "waiting")
    return found


def window_ids(
    session: Session, docs: Iterable[Document], now: datetime, settings: Settings | None = None
) -> set[int]:
    """这些通知里还在 7 天清理期里、正文附件照常给（能保存、导出）的：到期会清理的，加上名单类
    （不清理，这 7 天里一样给）。别的不清理的（KEEP_REASONS 其余几种）照 D-097 不给。"""
    pending = [doc for doc in docs if viewable(doc.link_gone_at, now, settings)]
    if not pending:
        return set()
    kept = keep_reasons(session, pending, now)
    return {doc.id for doc in pending if kept.get(doc.id) in (None, "list")}


def in_window(
    session: Session, doc: Document, now: datetime, settings: Settings | None = None
) -> bool:
    return doc.id in window_ids(session, [doc], now, settings)


def reading_note(doc: Document) -> str:
    """阅读页正文、附件上面、导出的 Word 里写的一句（没有保存、导出按钮，不说"可以先保存"）。"""
    if is_list(doc):
        return f"{list_notice(doc.link_gone_at)}。"
    return f"{notice(doc.link_gone_at)}。这份通知将于{md(cleanup_on(doc.link_gone_at))}清理。"


def payload(doc: Document, window: bool, readable: bool = True) -> dict:
    """接口里的 gone：原网页打不开的日期、清理日期（名单类没有）、页面上的两句话；window 是还在
    7 天里、正文附件照常给；canExport 是还在 7 天里、又有能读的正文或附件（readable，详情页才
    知道），给"导出 Word"；canSave 另要能保存（saving_open：新版隐私指引已经上线），给"保存"。
    见 payloads。"""
    can_export = window and readable
    can_save = can_export and saving_open()
    if is_list(doc):
        return {
            "since": gone_on(doc.link_gone_at).isoformat(),
            "cleanup": None,
            "notice": list_notice(doc.link_gone_at),
            # 7 天过后正文不给（D-123 ⑥ 没定，照现在）：名单照常能查
            "cleanupNotice": "" if window else "正文不再提供，名单请在“查名单”里按企业名称查询",
            "list": True,
            "window": window,
            "canSave": can_save,
            "canExport": can_export,
        }
    clean = cleanup_on(doc.link_gone_at)
    # 没有能读的正文和附件（没东西可存）时不说"可以先保存或导出"
    tail = "，可以先保存或导出" if can_save else ("，可以先导出" if can_export else "")
    return {
        "since": gone_on(doc.link_gone_at).isoformat(),
        "cleanup": clean.isoformat(),
        "notice": notice(doc.link_gone_at),
        "cleanupNotice": f"这份通知将于{md(clean)}清理{tail}",
        "list": False,
        "window": True,
        "canSave": can_save,
        "canExport": can_export,
    }


def payloads(
    session: Session,
    docs: Iterable[Document],
    now: datetime,
    settings: Settings | None = None,
    readable: dict[int, bool] | None = None,
) -> dict[int, dict]:
    """要带 gone 的（开关开着）：还在 7 天里、会清理的；名单类原网页失效的一直带（标明已撤下）。
    别的（原网页能打开、开关关着、过了清理日期、不清理的）不带，小程序照原来的 link_gone 显示。
    readable：详情页算好的"有没有能读的正文或附件"，没给的按有算（列表卡片不显示按钮）。"""
    if not enabled(settings):
        return {}
    docs = [doc for doc in docs if doc.link_gone_at is not None]
    if not docs:
        return {}
    window = window_ids(session, docs, now, settings)
    readable = readable or {}
    return {
        doc.id: payload(doc, doc.id in window, readable.get(doc.id, True))
        for doc in docs
        if doc.id in window or is_list(doc)
    }


# ---- 本机：到期清理 ----


def _due(session: Session, now: datetime, ids: list[int] | None = None, *, lock: bool = False):
    stmt = (
        select(Document)
        .where(
            Document.link_gone_at.is_not(None),
            Document.link_gone_at < cutoff(now),
            # 名单类不清理（D-123 ④）：直接不捞出来，免得留着的名单越积越多、每天重算一遍
            # （rule_kind 是入库时按标题算好的类型，人工定过的看 kind_override，同 effective_kind）
            # 两列都空（升级过渡期规则列还没算）时按不是名单算，免得 NULL 比较把它整个漏掉
            # （agy 第 3 轮）
            func.coalesce(Document.kind_override, Document.rule_kind, "") != "名单",
            # 下架的（整组）照原来的做法留着、隐藏，不清理
            available_condition(),
        )
        .order_by(Document.link_gone_at, Document.id)
    )
    if ids is not None:
        stmt = stmt.where(Document.id.in_(ids))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(stmt))


def _postponed(session: Session, docs: list[Document], now: datetime) -> dict[int, str]:
    """这次先不删、下次再看的：stale 最近 30 小时没有确认还打不开（没轮到检查，或网站出故障判断
    不了）；source_stale 原栏目 48 小时没抓成功（不知道还列不列着）。"""
    out: dict[int, str] = {}
    sources = {doc.source_id for doc in docs}
    fresh_sources = (
        set(
            session.scalars(
                select(Source.id).where(
                    Source.id.in_(sources), Source.last_success_at >= now - SOURCE_FRESH
                )
            )
        )
        if sources
        else set()
    )
    for doc in docs:
        seen = doc.link_gone_seen_at
        if seen is None or _utc(seen) < now - FRESH:
            out[doc.id] = "stale"
        elif doc.source_id not in fresh_sources:
            out[doc.id] = "source_stale"
    return out


def cleanup_expired(
    session: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> dict[str, int]:
    """本机：删掉过了清理期的原网页失效通知（D-123）。原网页检查跑完以后调用（cli check-links）。

    返回 due 到期的份数（名单类不算）、deleted 删了几份（dry_run 时是"会删"几份）、stale 最近
    30 小时没有确认还打不开、source_stale 原栏目 48 小时没抓成功（这两种推迟到下次）、failed 删的
    时候出错的份数（记日志，接着删下一份），以及 KEEP_REASONS 各种不清理的份数。删通知用数据库的
    外键规则：正文、截止、附件列表、名单等公开数据级联删除；用户的截止提醒授权（subscriptions）、
    截止线索跟着删，纠错断开；用户保存的副本（saved_documents）不建外键，不受影响。2.0 里本机删掉
    的行经同步通道带到服务器（PR #453）。"""
    counts = {
        "due": 0,
        "deleted": 0,
        "stale": 0,
        "source_stale": 0,
        "failed": 0,
        **{reason: 0 for reason in KEEP_REASONS},
    }
    if not enabled(settings):
        return counts
    now = _utc(now or datetime.now(UTC))
    due = _due(session, now)
    session.commit()
    counts["due"] = len(due)
    kept = keep_reasons(session, due, now, cleanup=True)
    for reason in kept.values():
        counts[reason] += 1
    postponed = _postponed(session, [doc for doc in due if doc.id not in kept], now)
    for reason in postponed.values():
        counts[reason] += 1
    ready = [doc.id for doc in due if doc.id not in kept and doc.id not in postponed]
    session.commit()
    if dry_run:
        counts["deleted"] = len(ready)
        return counts
    for doc_id in ready:
        # 一份一个事务：锁住这一行再核对一遍（这期间恢复了、下架了、换了地址的不删）。
        # 一份出错（锁等待超时等）只记日志、回滚，接着删下一份，不让一份卡住整批
        try:
            docs = _due(session, now, [doc_id], lock=True)
            if (
                not docs
                or keep_reasons(session, docs, now, cleanup=True)
                or _postponed(session, docs, now)
            ):
                session.rollback()
                continue
            session.execute(delete(Document).where(Document.id == doc_id))
            session.commit()
            counts["deleted"] += 1
        except Exception as exc:  # noqa: BLE001 - 一份出意外不拖累整批，下次再删
            session.rollback()
            counts["failed"] += 1
            log.warning("原网页失效到期清理：#%s 没删成（%s）", doc_id, exc.__class__.__name__)
    log.info("原网页失效到期清理：%s", counts)
    return counts
