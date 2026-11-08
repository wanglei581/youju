"""原网页失效的通知 7 天后清理（D-123，创始人 11/8；没定的几件 D-124 按推荐定了）。

什么算失效沿用原网页检查（youju/link_check.py）：连续两次、相隔 20 小时以上打不开，记
documents.link_gone_at。记下那天（北京时间）起 7 天是清理期：

- 这 7 天里正文、附件照常给看（详情页、阅读页：v1.reading_body、attachment_text.judge），页面写明
  "原网页已无法访问，本平台将于某月某日停止展示；内容变动以官方最新发布为准"（notice；D-124 ④照
  agy 合规审阅的改法：不猜"可能已被撤回或更正"，给用户看的地方不说"清理"），可以保存
  （我的保存，youju/saved.py）、导出。
- 期间每天再查一次（link_check 的清理期队列）：能打开了，link_check 清空 link_gone_at，清理就取消；
  同一份通知在原栏目换了新地址的（keep_reasons 的 moved：同一栏目、同一标题、原网页没失效的另一份）
  等几种情况也不清理（KEEP_REASONS）。名单类不清理，一直标明"原网页某月某日起已撤下"（D-123 ④）；
  这 7 天里正文照常给，7 天过后不给正文、只留查名单（D-124 ③）。
- 失效那天 + 7 天起（cleanup_on）不再给正文和附件（照 D-097），本机原网页检查跑完以后删掉这份通知
  （cleanup_expired：要 30 小时内确认过还打不开、原栏目 48 小时内抓成功过，同一通知的几个网址整组
  一起删）；2.0 里删除经同步通道带到服务器（PR #453），用户保存的副本不受影响
  （saved_documents 不建外键）。
- 因投诉下架的不算官方失效：照原来的做法下架（行留着、隐藏），不清理；用户保存的副本一起删
  （saved.purge_taken_down）。
- 清理以后，引用这份通知的 AI 解读、条款对照结果和问一问的回答不再给看，说清楚原网页已无法访问、
  本平台已停止展示，保存过的去"我的 → 我的保存"看；"我的记录"里这一条照常列着、标"原通知已停止
  展示"，可以删除（D-124 ②，cleaned_ids）。
- 发布超过 90 天的申报通知、政策也每月查一次原网页，失效了一样进清理（D-124 ①，link_check）。

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
    # 照常给、能保存导出，7 天过后不给正文、只留查名单（D-124 ③）
    "list": "名单类，不按 7 天清理",
    "history": "有下架、恢复记录（记录要留着，删不了这份通知）",
    "changed": "别的通知的截止变更指向它",
    "listed": "原栏目还列着这份通知",
    "merged": "同一份通知的其他网址还能打开",
    "moved": "同一份通知在原栏目换了新地址",
    # 只在删的时候看（keep_reasons 的 cleanup=True，和 cleanup_expired 的整组判断）：页面照常按
    # 自己的 7 天显示
    "waiting": "同一份通知的其他网址还在 7 天里或这次删不了，等整组都能删时一起删",
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


# 给用户看的几句（D-124 ④：照 agy 合规审阅的改法，"清理"一律说"停止展示"，免得被理解成政府废止）。
# 通知已经删掉以后不知道是哪天，不写日期
NOT_FOUND = (
    "没有找到这份文件，可能是原网页已无法访问、本平台已停止展示；保存过的在“我的 → 我的保存”里看"
)
# 清理以后打开引用它的 AI 解读、条款对照结果（D-124 ②）
CLEANED_RESULT = (
    "这份通知原网页已无法访问，本平台已停止展示，这条结果不再提供；"
    "此前保存过这份通知的，可以在“我的 → 我的保存”里看"
)
# 问一问里问这份通知的回答（D-124 ②）
CLEANED_ANSWER = (
    "这份通知原网页已无法访问，本平台已停止展示，这条回答不再显示；"
    "此前保存过这份通知的，可以在“我的 → 我的保存”里看"
)
# 批量工具站内结果里引用它的那一条（整份结果照常给，只挡这一条）
CLEANED_ITEM = "这一条的通知原网页已无法访问，本平台已停止展示，这一条不再提供"


def notice(gone_at: datetime) -> str:
    """卡片、详情页、阅读页写的提示（D-123 ②，说法照 D-124 ④）：哪天停止展示是清理那天。"""
    return (
        f"原网页已无法访问，本平台将于{md(cleanup_on(gone_at))}停止展示；内容变动以官方最新发布为准"
    )


def list_notice(gone_at: datetime) -> str:
    """名单类的标记（D-123 ④：卡片和详情页标明原网页某月某日起已撤下）。"""
    return f"原网页{md(gone_on(gone_at))}起已撤下"


def is_list(doc: Document) -> bool:
    from youju.classify import effective_kind

    return effective_kind(doc.title, doc.kind_override) == "名单"


def _squash(title: str | None) -> str:
    # 和归并（dedup.match_key）一样只去掉空白
    return re.sub(r"\s+", "", title or "")


def merge_groups(session: Session, ids: list[int]) -> dict[int, set[int]]:
    """每份所在归并组（同一份通知的不同网址）的全部编号，含它自己；不在组里的就是它自己。"""
    if not ids:
        return {}
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
    if members:
        for master, duplicate in session.execute(
            select(DocumentMerge.canonical_id, DocumentMerge.duplicate_id).where(
                DocumentMerge.active.is_(True), DocumentMerge.canonical_id.in_(members)
            )
        ):
            members[master].add(duplicate)
    return {doc_id: members[master] for doc_id, master in masters.items()}


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
    groups = merge_groups(session, ids)
    others = {m for group in groups.values() for m in group} - set(ids)
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
    for doc_id, group in groups.items():
        if group & live:
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
    for doc_id, group in groups.items():
        if group & waiting:
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
    return f"{notice(doc.link_gone_at)}。"


def payload(doc: Document, window: bool, readable: bool = True) -> dict:
    """接口里的 gone：原网页打不开的日期、清理日期（名单类没有）、页面上的两句话（notice 是完整的
    第一句，卡片只写这句；cleanupNotice 是详情页的第二句）；window 是还在 7 天里、正文附件照常给；
    canExport 是还在 7 天里、又有能读的正文或附件（readable，详情页才知道），给"导出 Word"；
    canSave 另要能保存（saving_open：新版隐私指引已经上线），给"保存"。见 payloads。"""
    can_export = window and readable
    can_save = can_export and saving_open()
    if is_list(doc):
        return {
            "since": gone_on(doc.link_gone_at).isoformat(),
            "cleanup": None,
            "notice": list_notice(doc.link_gone_at),
            # 7 天过后正文不给（D-124 ③）：名单照常能查
            "cleanupNotice": "" if window else "正文不再提供，名单请在“查名单”里按企业名称查询",
            "list": True,
            "window": window,
            "canSave": can_save,
            "canExport": can_export,
        }
    # 第二句按实际给的按钮写；没有能读的正文和附件（没东西可存）时不写（哪天停止展示第一句已经写了）
    second = "可以先保存或导出" if can_save else ("可以先导出" if can_export else "")
    return {
        "since": gone_on(doc.link_gone_at).isoformat(),
        "cleanup": cleanup_on(doc.link_gone_at).isoformat(),
        "notice": notice(doc.link_gone_at),
        "cleanupNotice": second,
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


def cleaned_ids(
    session: Session, ids: Iterable[int | str | None], settings: Settings | None = None
) -> set[int]:
    """这些通知编号里已经不在库里的（D-124 ②）。通知只有到期清理会删（cleanup_expired，其余都是
    下架、行留着；documents.source_id 对栏目是 RESTRICT），开关开着时就当是清理掉的；开关关着
    不判断（不会清理），照原来的说法。以后加别的删通知行的路径（比如 2.0 同步带来别的删除），要
    守住这一条，不然下架的会被说成"已停止展示"。"""
    wanted: set[int] = set()
    for value in ids:
        text = "" if value is None else str(value)
        if text.isdigit() and int(text) > 0:
            wanted.add(int(text))
    if not wanted or not enabled(settings):
        return set()
    return wanted - set(session.scalars(select(Document.id).where(Document.id.in_(wanted))))


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
    listing: list[dict] | None = None,
) -> dict[str, int]:
    """本机：删掉过了清理期的原网页失效通知（D-123）。原网页检查跑完以后调用（cli check-links）。

    返回 due 到期的份数（名单类不算）、deleted 删了几份（dry_run 时是"会删"几份）、stale 最近
    30 小时没有确认还打不开、source_stale 原栏目 48 小时没抓成功（这两种推迟到下次）、failed 删的
    时候出错的份数（记日志，接着删下一份），以及 KEEP_REASONS 各种不清理的份数。删通知用数据库的
    外键规则：正文、截止、附件列表、名单等公开数据级联删除；用户的截止提醒授权（subscriptions）、
    截止线索跟着删，纠错断开；用户保存的副本（saved_documents）不建外键，不受影响。2.0 里本机删掉
    的行经同步通道带到服务器（PR #453）。

    dry_run 只数不删，开关关着也能跑：打开开关那天会一次清掉积下来的"失效超过 7 天"的通知，
    要先试跑报数给创始人（D-124 ⑤，没定）。listing：试跑时把会删的、推迟的、等同组其他网址的
    （waiting）逐份放进去（编号、失效日期、这次怎么处理、标题），份数和 deleted + stale +
    source_stale + waiting 对得上；别的不清理的（名单、有下架记录等）不列。"""
    counts = {
        "due": 0,
        "deleted": 0,
        "stale": 0,
        "source_stale": 0,
        "failed": 0,
        **{reason: 0 for reason in KEEP_REASONS},
    }
    if not enabled(settings) and not dry_run:
        return counts
    now = _utc(now or datetime.now(UTC))
    due = _due(session, now)
    session.commit()
    counts["due"] = len(due)
    kept = keep_reasons(session, due, now, cleanup=True)
    for reason in kept.values():
        counts[reason] += 1
    candidates = [doc.id for doc in due if doc.id not in kept]
    possible = set(candidates)
    postponed = _postponed(session, [doc for doc in due if doc.id in possible], now)
    # 整组一起删：组里只要有一份这次删不了（不清理、推迟、还没到期），整组都先不删，免得主条目删了、
    # 归并关系跟着没了，转载的那份单独冒出来（独立审阅第 2 轮）。组里有到期了也不清理、或者还没
    # 到期的（不在 candidates 里）：这次和以后都要等，推迟的也算等同组，免得试跑报的"推迟、早晚要删"
    # 偏大（独立审阅 D-124 第 2 轮）
    groups = merge_groups(session, candidates)
    blocked = {doc_id for doc_id in candidates if not groups[doc_id] <= possible}
    for doc_id in blocked:
        postponed.pop(doc_id, None)
    for reason in postponed.values():
        counts[reason] += 1
    ready = [doc_id for doc_id in candidates if doc_id not in postponed and doc_id not in blocked]
    allowed = set(ready)
    held = {doc_id for doc_id in ready if not groups[doc_id] <= allowed}
    counts["waiting"] += len(blocked) + len(held)
    ready = [doc_id for doc_id in ready if doc_id not in held]
    session.commit()
    if dry_run:
        counts["deleted"] = len(ready)
        if listing is not None:
            will = set(ready)
            for doc in due:
                # 不清理的（名单、有下架记录、原栏目还列着等）不列；等同组其他网址的（waiting：组里
                # 还有在 7 天里的，或者这次整组删不了）列上，和计数对得上（独立审阅 D-124 第 1 轮）
                if doc.id in kept and kept[doc.id] != "waiting":
                    continue
                state = (
                    "会删"
                    if doc.id in will
                    else {
                        "stale": "推迟：最近 30 小时没确认还打不开",
                        "source_stale": "推迟：原栏目 48 小时没抓成功",
                    }.get(postponed.get(doc.id, ""), "等同组其他网址")
                )
                listing.append(
                    {
                        "id": doc.id,
                        "since": gone_on(doc.link_gone_at).isoformat(),
                        "state": state,
                        "title": doc.title,
                    }
                )
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
            log.warning(
                "原网页失效到期清理：#%s 没删成（%s）",
                doc_id,
                exc.__class__.__name__,
                exc_info=True,
            )
    log.info("原网页失效到期清理：%s", counts)
    return counts
