"""运维日报（D-023）：每天早上把数据底座的运行情况和待办发到群里，
AI 从待核对的通知里挑几份建议先核对。

- 数字都是查数据库算的；待办条数直接用核对台的计数函数，和核对台看到的一致。
- AI 只做"挑"：给它待核对通知的编号、发文单位、标题和候选截止日期（政府公开信息，
  不含任何用户数据），它返回编号和理由；编号必须在给它的清单里，理由只给内部看
  （R1 不给用户看 AI 写的文字）。
- 群机器人一条消息有字节上限：放不下时先少列 AI 建议，数字部分不截。
"""

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from youju.ai.client import AIError, ChatClient
from youju.ai.usage import BEIJING, UsageSummary, usage_summary
from youju.alerts import MAX_ALERT_BYTES
from youju.classify import effective_kind
from youju.dedup import canonical_ids, groups_for, visible_documents
from youju.models import (
    AccessDaily,
    AdvisorTask,
    AISafetyHit,
    AttachmentCopy,
    Document,
    DocumentDeadline,
    DocumentDetail,
    Follow,
    ListEntry,
    Source,
    Subscription,
    User,
)
from youju.repost import grouped_documents
from youju.source_health import check_sources, failing_sources

log = logging.getLogger("youju.digest")

# AI 挑选时最多给它看多少份待核对通知、挑几份
_PICK_FROM = 30
_PICKS = 3

_SYSTEM = """你是政策申报助手的值班编辑。必须只返回 JSON 对象，字段 picks 为数组。
下面是等待人工核对截止日期的政府通知清单（编号、发文单位、标题、候选截止日期）。
挑出最值得先核对的至多 3 份：候选截止越近、面向企业的申报或资金项目、覆盖面越广的越优先。
每项字段：id（清单里的编号）、reason（不超过 20 字）。"""


# ---- 能读情况（D-097 ④）----
# 近 30 天发布、能公开的申报通知和政策（合并转载只算主条目）在小程序里能不能读全，用详情页、
# 阅读页同一个判断（v1.reading_body、attachment_text.readings），分母都含失败、处理中和不支持的。
# 达到下面的门槛再提审：正文能读率 ≥90%（争取 95%）、附件没有等了超过一轮的、整条能读率 ≥75%。
READ_KINDS = ("申报通知", "政策")
BODY_GATE = 90
BODY_GOAL = 95
WHOLE_GATE = 75
# 每天 6:30、20:30 两轮抓取（deploy/crontab），每轮当轮取新附件的文字；两轮最长隔 14 小时，
# 等得比这久就是漏了一轮
ROUND_HOURS = 14
SITES_SHOWN = 10
# 读不了的原因（页面上的说法见 api/readers.py、attachment_text.NOTES）→ 日报里的短说法
BODY_PROBLEMS = {
    "empty": "没取到正文",
    "chrome": "正文像网页导航",
    "link_gone": "原网页已打不开",
    # 下面几种申报通知、政策一般不会有（名单类、个人事项才有），写全免得落到"没取到正文"
    "short_list": "名单挡完剩字太少",
    "list": "正文主要是名单",
    "personal": "个人事项不给",
}
ATTACHMENT_PROBLEMS = {
    "pending": "附件等取文字",
    "scan": "附件是扫描件",
    "partial": "附件有扫描页",
    "format": "附件格式不支持",
    "too_large": "附件太大",
    "broken": "附件损坏或加密",
    "empty": "附件没有文字",
    "gone": "附件在原网站已删",
    "webpage": "附件下载不了",
    "unavailable": "附件下载不了",
    "host": "附件不在政府网站",
    "link_gone": "原网页已打不开",
    "old": "附件超出范围",
    "kind": "附件不读",
    "list": "名单附件",
    "personal": "附件含个人信息",
    "source": "附件来源不公开",
}


@dataclass
class Readability:
    notices: int = 0
    body_ok: int = 0
    # 正文读不了的原因 → 份数（BODY_PROBLEMS 的说法）
    no_body: Counter = field(default_factory=Counter)
    attachments: int = 0
    attachments_ok: int = 0
    # 附件读不了的原因 → 个数（ATTACHMENT_PROBLEMS 的说法）
    attachment_problems: Counter = field(default_factory=Counter)
    pending: int = 0
    # 等取文字的附件里最老的一个等了多少秒（从通知第一次收录算起）、是哪一份通知（编号、标题开头）
    oldest_pending_seconds: int | None = None
    oldest_pending_doc: tuple[int, str] | None = None
    # 等取文字、但通知没有收录时间（等了多久算不出来）的附件个数：有这种的，附件等待门槛不算达到
    pending_unknown: int = 0
    whole_ok: int = 0
    # 正文、附件都能读，但附件里有扫描页（不算整条能读，单列）
    scanned_only: int = 0
    # 读不全的通知最多的网站：（来源部门全称，份数，[(原因, 份数)…]，原因只列最多的两个）
    sites: list = field(default_factory=list)

    @property
    def oldest_pending_hours(self) -> int | None:
        """写给人看的小时数，和按秒判断门槛一致：没超过一轮的按整小时往下写（13 小时 1 秒写 13），
        超过的往上进位（14 小时多 1 秒写 15，不会写成压线的 14）。"""
        seconds = self.oldest_pending_seconds
        if seconds is None:
            return None
        if seconds <= ROUND_HOURS * 3600:
            return seconds // 3600
        return math.ceil(seconds / 3600)


@dataclass
class Digest:
    day: date
    sources_total: int = 0
    sources_ok: int = 0
    source_failures: tuple[str, ...] = ()
    new_documents: int = 0
    new_by_kind: dict[str, int] = field(default_factory=dict)
    details_ok: int = 0
    details_failed: int = 0
    new_candidates: int = 0
    approved: int = 0
    rejected: int = 0
    pending: int = 0
    pending_7: int = 0
    pending_30: int = 0
    # 最近一次核对截止（通过、驳回、改期）的日期，北京时间；从没核对过是 None
    last_reviewed: date | None = None
    nearest_due: date | None = None
    kinds_pending: int = 0
    audit_flagged: int = 0
    feedback_pending: int = 0
    new_users: int = 0
    active_users: int = 0
    total_users: int = 0
    new_follows: int = 0
    reminders_sent: int = 0
    reminders_failed: int = 0
    usage: UsageSummary | None = None
    # 下面三项是新加的统计，各自单独查：哪一项出错只把那一项写成"取不到"（False），
    # 不让整条日报发不出去
    # 接口（M3）：昨天（北京时间）各服务的小程序接口汇总：（服务，请求数，5xx，3 秒及以上）
    access: tuple[tuple[str, int, int, int], ...] = ()
    access_ok: bool = True
    # 数据质量（D6）：近 30 天发布、能公开的申报通知和名单
    notices_30: int = 0
    notices_with_candidates: int = 0
    lists_30: int = 0
    lists_parsed: int = 0
    quality_ok: bool = True
    # 用量（M5）：数据库大小取不到时是 None
    db_bytes: int | None = None
    attachment_copies: int = 0
    attachment_bytes: int = 0
    result_files: int = 0
    usage_ok: bool = True
    # 内容安全（门槛第 4 道）：近 24 小时拦下的 AI 结果（按层），检测接口出错失败的 AI 任务。
    # 只有次数，不含任何文字和用户编号
    safety_local: int = 0
    safety_wechat: int = 0
    safety_check_down: int = 0
    safety_ok: bool = True
    # 能读情况（D-097 ④）：出错时是 None、readability_ok 为 False
    readability: Readability | None = None
    readability_ok: bool = True


@dataclass
class Pick:
    document_id: int
    title: str
    dept: str
    due: date
    reason: str


def _count(session: Session, stmt) -> int:
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def _distinct(session: Session, rows: list[DocumentDeadline]) -> list[DocumentDeadline]:
    """按核对台的归并规则去重：同一通知（归并组）里类型、日期、时间、证据句都相同的候选只算一条。"""
    masters = canonical_ids(session, [d.document_id for d in rows])
    seen: dict[tuple, DocumentDeadline] = {}
    for d in sorted(rows, key=lambda row: row.id):
        key = (
            masters.get(d.document_id, d.document_id),
            d.kind,
            d.candidate_date,
            d.candidate_time,
            d.evidence,
        )
        seen.setdefault(key, d)
    return list(seen.values())


def build_digest(session: Session, now: datetime | None = None) -> Digest:
    """过去 24 小时的运行情况、当前待办和昨天（北京时间）的 AI 用量。"""
    from youju.api import app as admin

    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    today = now.astimezone(BEIJING).date()
    digest = Digest(day=today)

    health = check_sources(session, now=now)
    digest.sources_total, digest.sources_ok = health.total, health.successful
    # 每个异常栏目带上"上次成功几号"：日报天天列同样几个异常时，分得清是新坏的还是坏了很久的。
    # 坏得最久的排前面（日报只列前 3 个）；从没检查过的最反常，排最前
    failing = sorted(
        failing_sources(session, now=now),
        key=lambda item: (item.days is not None, -(item.days or 0)),
    )
    digest.source_failures = tuple(item.text(short=True) for item in failing)

    # 新文件：同一通知的多个网址只算一份
    titles = session.execute(
        grouped_documents(
            visible_documents(
                select(Document, Source)
                .join(Source, Source.id == Document.source_id)
                .where(Document.is_baseline.is_(False), Document.first_seen_at >= since)
            )
        ).with_only_columns(Document.title, Document.kind_override)
    ).all()
    digest.new_documents = len(titles)
    digest.new_by_kind = dict(
        Counter(effective_kind(title, override) for title, override in titles).most_common()
    )
    for status, count in session.execute(
        select(DocumentDetail.status == "ok", func.count())
        .where(DocumentDetail.fetched_at >= since)
        .group_by(DocumentDetail.status == "ok")
    ):
        if status:
            digest.details_ok = count
        else:
            digest.details_failed = count
    # 候选和核对都按核对台的归并规则去重：同一通知其他网址上的相同候选不重复算
    fresh = session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.created_at >= since)
    ).all()
    digest.new_candidates = len(_distinct(session, list(fresh)))
    reviewed = session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.reviewed_at >= since)
    ).all()
    by_status = Counter(d.status for d in _distinct(session, list(reviewed)))
    digest.approved, digest.rejected = by_status["approved"], by_status["rejected"]

    # 待办：待核对用核对台列表的同一规则（_pending_shown_ids），日期都按这份日报的"今天"算
    pending = session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.status == "pending")
    ).all()
    shown_ids = admin._pending_shown_ids(session, list(pending))
    shown = [d for d in pending if d.id in shown_ids]
    upcoming = [d.candidate_date for d in shown if d.candidate_date >= today]
    digest.pending = len(shown)
    digest.pending_30 = sum(due <= today + timedelta(days=30) for due in upcoming)
    digest.nearest_due = min(upcoming, default=None)
    # 每天第一件事（D-061）：和管理台"截止日期 · 7 天内"同一个计数规则，按这份日报的"今天"算
    digest.pending_7 = admin.pending_due_count(session, "7", today)
    # 最近一次核对：通过、驳回记在 reviewed_at，标"已变更"（延期、调整、撤销）记在 changed_at
    stamps = [
        session.scalar(
            select(func.max(DocumentDeadline.reviewed_at)).where(
                DocumentDeadline.status != "pending"
            )
        ),
        session.scalar(
            select(func.max(DocumentDeadline.changed_at)).where(
                DocumentDeadline.status == "changed"
            )
        ),
    ]
    stamps = [s if s.tzinfo is not None else s.replace(tzinfo=UTC) for s in stamps if s]
    if stamps:
        digest.last_reviewed = max(stamps).astimezone(BEIJING).date()
    digest.kinds_pending = admin.kinds_pending_count(session, None)["pending"]
    digest.audit_flagged = admin.audit_flagged_count(session, None)["flagged"]
    digest.feedback_pending = admin.feedback_pending_count(session, None)["pending"]

    # 用户：只报总数，不列任何人
    digest.new_users = _count(session, select(User.id).where(User.created_at >= since))
    digest.active_users = _count(session, select(User.id).where(User.last_visited_at >= since))
    digest.total_users = _count(session, select(User.id))
    digest.new_follows = _count(session, select(Follow.id).where(Follow.created_at >= since))
    attempted = select(Subscription.status).where(Subscription.attempted_at >= since)
    sent = Counter(session.scalars(attempted))
    digest.reminders_sent, digest.reminders_failed = sent["sent"], sent["failed"]

    digest.usage = usage_summary(session, today - timedelta(days=1))
    _quality_and_usage(session, digest, today)
    try:
        with session.begin_nested():
            _safety(session, digest, since)
    except Exception as exc:  # noqa: BLE001 - 统计出错不能让整条日报发不出去；只记类型
        log.warning("日报的内容安全统计出错：%s", type(exc).__name__)
        digest.safety_ok = False
    try:
        with session.begin_nested():
            digest.readability = readability(session, now)
    except Exception as exc:  # noqa: BLE001 - 统计出错不能让整条日报发不出去；只记类型
        log.warning("日报的能读情况统计出错：%s", type(exc).__name__)
        digest.readability_ok = False
    return digest


def _quality_and_usage(session: Session, digest: Digest, today: date) -> None:
    """接口（昨天）、数据质量（近 30 天发布）、用量：都是查库算的总数，不含任何用户信息。
    每一项放在一个保存点里查：出错只回滚这一项、标成取不到，前面算好的数和后面几项照常。"""
    for step, flag in ((_access, "access_ok"), (_quality, "quality_ok"), (_usage, "usage_ok")):
        try:
            with session.begin_nested():
                step(session, digest, today)
        except Exception as exc:  # noqa: BLE001 - 统计出错不能让整条日报发不出去；只记类型
            log.warning("日报的%s统计出错：%s", flag, type(exc).__name__)
            setattr(digest, flag, False)


def _safety(session: Session, digest: Digest, since: datetime) -> None:
    from youju.ai import result_safety

    layers = dict(
        session.execute(
            select(AISafetyHit.layer, func.count(AISafetyHit.id))
            .where(AISafetyHit.created_at >= since)
            .group_by(AISafetyHit.layer)
        ).all()
    )
    digest.safety_local, digest.safety_wechat = layers.get("local", 0), layers.get("wechat", 0)
    digest.safety_check_down = (
        session.scalar(
            select(func.count(AdvisorTask.id)).where(
                AdvisorTask.error == result_safety.CHECK_DOWN_MESSAGE,
                AdvisorTask.finished_at >= since,
            )
        )
        or 0
    )


def _safety_line(digest: Digest) -> str | None:
    """有拦下的、有检测出错的，或者统计出错时才写一行；都是 0 就不写。"""
    if not digest.safety_ok:
        return "内容安全（24 小时）：取不到（统计出错，看容器日志）"
    blocked = digest.safety_local + digest.safety_wechat
    if not blocked and not digest.safety_check_down:
        return None
    parts = []
    if blocked:
        parts.append(
            f"拦下 {blocked} 次（本地词库 {digest.safety_local}、微信 {digest.safety_wechat}），"
            "按处置记录里的通知编号看一眼是不是误拦了正常原文"
        )
    if digest.safety_check_down:
        parts.append(
            f"检测接口出错、AI 任务没做成 {digest.safety_check_down} 个（admin 可能调不通微信接口）"
        )
    return "内容安全（24 小时）：" + "；".join(parts)


def _access(session: Session, digest: Digest, today: date) -> None:
    digest.access = tuple(
        (service, requests, errors, slow)
        for service, requests, errors, slow in session.execute(
            select(AccessDaily.service, AccessDaily.requests, AccessDaily.errors, AccessDaily.slow)
            .where(AccessDaily.day == today - timedelta(days=1))
            .order_by(AccessDaily.service)
        )
    )


def _quality(session: Session, digest: Digest, today: date) -> None:
    from youju.api import v1

    since = today - timedelta(days=29)

    def recent(kind: str) -> list[int]:
        # 和小程序同一口径：能公开的、不是归并掉的副本、按类型规则（人工改过的按人工）
        return list(
            session.scalars(
                visible_documents(
                    select(Document.id)
                    .join(Source, Source.id == Document.source_id)
                    .where(
                        v1.public_condition(),
                        v1._kind_is([kind]),
                        Document.publish_date >= since,
                        Document.publish_date <= today,
                    )
                )
            )
        )

    def owners(ids: list[int]) -> dict[int, int]:
        # 归并组里各网址 → 主文件：正文、截止候选、名单条目可能在同组的其他网址上，小程序也会用
        return {
            member: master for master, group in groups_for(session, ids).items() for member in group
        }

    notices = recent("申报通知")
    owner = owners(notices)
    digest.notices_30 = len(notices)
    # 正文能不能读：见能读情况（readability，申报通知和政策，和详情页、阅读页同一个判断）
    candidates = session.scalars(
        select(DocumentDeadline.document_id)
        .where(DocumentDeadline.document_id.in_(list(owner)))
        .distinct()
    )
    digest.notices_with_candidates = len({owner[doc_id] for doc_id in candidates})

    lists = recent("名单")
    list_owner = owners(lists)
    digest.lists_30 = len(lists)
    # 解析出企业名单：和名单看板同一口径（v1._list_board_payload），同组任一网址有企业条目就算
    parsed = session.scalars(
        select(ListEntry.document_id)
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ListEntry.document_id.in_(list(list_owner)),
            ListEntry.normalized_name != "",
            v1.public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
        )
        .distinct()
    )
    digest.lists_parsed = len({list_owner[doc_id] for doc_id in parsed})


def _usage(session: Session, digest: Digest, today: date) -> None:
    size = session.scalar(
        text(
            "SELECT SUM(data_length + index_length) FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )
    )
    digest.db_bytes = int(size) if size is not None else None
    # 多个原站链接内容相同时共用同一个存储对象（对象名是内容的 SHA-256）：按对象去重
    objects = (
        select(AttachmentCopy.file_id, func.max(AttachmentCopy.size).label("size"))
        .where(AttachmentCopy.status == "ready", AttachmentCopy.file_id.is_not(None))
        .group_by(AttachmentCopy.file_id)
        .subquery()
    )
    count, total = session.execute(
        select(func.count(), func.coalesce(func.sum(objects.c.size), 0)).select_from(objects)
    ).one()
    digest.attachment_copies, digest.attachment_bytes = int(count), int(total)
    digest.result_files = (
        session.scalar(
            select(func.count())
            .select_from(AdvisorTask)
            .where(AdvisorTask.result_file_id.is_not(None))
        )
        or 0
    )


def _seconds(now: datetime, then: datetime | None) -> int | None:
    """从 then 到 now 多少秒；库里没有时区的时间按 UTC（和日报别处一样）。"""
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0, int((now - then).total_seconds()))


def readability(session: Session, now: datetime) -> Readability:
    """近 30 天申报通知、政策的能读情况（D-097 ④），口径见上。"""
    from youju import attachment_text
    from youju.api import v1

    today = now.astimezone(BEIJING).date()
    since = today - timedelta(days=29)
    rows = session.execute(
        visible_documents(
            select(Document, Source)
            .join(Source, Source.id == Document.source_id)
            .where(
                v1.public_condition(),
                v1._kind_is(list(READ_KINDS)),
                Document.publish_date >= since,
                Document.publish_date <= today,
            )
        ).order_by(Document.id)
    ).all()
    result = Readability()
    sites: Counter = Counter()
    reasons: dict[str, Counter] = {}
    for doc, src in rows:
        kind = effective_kind(doc.title, doc.kind_override)
        items = attachment_text.readings(session, doc.id, now)
        # 和详情页、阅读页同一个判断（原网页已失效的也在里面：problem 是 link_gone）
        body = v1.reading_body(
            session, doc, kind, attachments=bool(items), redact=attachment_text.mask
        )
        body_ok, problem = body.status == "ok", body.problem
        result.notices += 1
        # 这份通知读不全的原因（网站排行按份数算，一份里几个附件同一原因只算一次）
        found: set[str] = set()
        if body_ok:
            result.body_ok += 1
        else:
            label = BODY_PROBLEMS.get(problem or body.status, "没取到正文")
            result.no_body[label] += 1
            found.add(label)
        scanned = False
        for item in items:
            result.attachments += 1
            if item.readable:
                result.attachments_ok += 1
                if item.scanned_pages:
                    scanned = True
                    found.add(ATTACHMENT_PROBLEMS["partial"])
                continue
            label = ATTACHMENT_PROBLEMS.get(item.state, "附件读不了")
            result.attachment_problems[label] += 1
            found.add(label)
            if item.state == "pending":
                result.pending += 1
                waited = _seconds(now, doc.first_seen_at)
                if waited is None:
                    result.pending_unknown += 1
                elif (
                    result.oldest_pending_seconds is None or waited > result.oldest_pending_seconds
                ):
                    result.oldest_pending_seconds = waited
                    result.oldest_pending_doc = (doc.id, doc.title[:20])
        # 整条能读：正文能读、附件都能读（没有附件的也算：没有要读的附件）
        complete = body_ok and all(item.readable for item in items)
        if complete and not scanned:
            result.whole_ok += 1
            continue
        if complete:
            result.scanned_only += 1
        site = src.dept_full_name or "（来源没写部门）"
        sites[site] += 1
        reasons.setdefault(site, Counter()).update(found)
    result.sites = [
        (site, count, reasons[site].most_common(2))
        for site, count in sorted(sites.items(), key=lambda kv: (-kv[1], kv[0]))[:SITES_SHOWN]
    ]
    return result


def _gate(value: int, whole: int, gate: int) -> str:
    """ "门槛 90%，达到"：按向下取整的百分比比（有一份读不了就不会写成达到 100%）。"""
    if not whole:
        return f"门槛 {gate}%"
    reached = math.floor(100 * value / whole) >= gate
    return f"门槛 {gate}%，{'达到' if reached else '没达到'}"


def _counts(counter: Counter) -> str:
    return "、".join(f"{label} {n}" for label, n in counter.most_common())


def gates(rb: Readability) -> tuple[bool, bool, bool]:
    """三项提审门槛各达到没有：正文能读率、整条能读率（按向下取整的百分比比，没有通知时算没达到），
    附件没有等了超过一轮的（按秒比；有一个等了多久算不出来的就按没达到算；没有等取文字的算达到）。"""
    body = bool(rb.notices) and math.floor(100 * rb.body_ok / rb.notices) >= BODY_GATE
    whole = bool(rb.notices) and math.floor(100 * rb.whole_ok / rb.notices) >= WHOLE_GATE
    wait = not rb.pending or (
        not rb.pending_unknown
        and rb.oldest_pending_seconds is not None
        and rb.oldest_pending_seconds <= ROUND_HOURS * 3600
    )
    return body, whole, wait


def _verdict(rb: Readability) -> str:
    return "提审门槛达到了" if all(gates(rb)) else "提审门槛还没达到"


def _waiting(rb: Readability) -> str:
    if not rb.pending:
        return "没有等取文字的附件"
    hours = rb.oldest_pending_hours
    if hours is None:
        return f"等取文字的附件 {rb.pending_unknown} 个等了多久不知道"
    if rb.pending_unknown:
        return (
            f"有收录时间的里等取文字最久的附件 {hours} 小时，"
            f"另有 {rb.pending_unknown} 个等了多久不知道"
        )
    return f"等取文字最久的附件 {hours} 小时"


def readability_summary(digest: Digest) -> str:
    """主日报里的一行：三项提审门槛（D-097 ④）各是多少、达到没有；细的另发一条。"""
    rb = digest.readability
    if not digest.readability_ok or rb is None:
        return "能读（近 30 天申报通知、政策）：取不到（统计出错，看容器日志）"
    return (
        f"能读（近 30 天申报通知、政策 {rb.notices} 份）：{_verdict(rb)}——"
        f"正文 {_pct(rb.body_ok, rb.notices)}（要 {BODY_GATE}%）、"
        f"整条 {_pct(rb.whole_ok, rb.notices)}（要 {WHOLE_GATE}%）、"
        f"{_waiting(rb)}（要不超过 {ROUND_HOURS} 小时）；详见下一条"
    )


def readability_text(digest: Digest) -> str | None:
    """能读情况（另发一条）：三项提审门槛达到没有、读不了的原因、读不全最多的网站。放不下时从
    最后一个网站开始去掉。统计出错时不发（主日报里已写取不到）。"""
    rb = digest.readability
    if not digest.readability_ok or rb is None:
        return None
    head = (
        f"运维日报·能读情况 {_md(digest.day)}（近 30 天申报通知、政策 {rb.notices} 份）："
        f"{_verdict(rb)}（正文、整条、附件等待三项都达到才算，D-097）"
    )
    body = (
        f"正文能读 {rb.body_ok} 份（{_pct(rb.body_ok, rb.notices)}，"
        f"{_gate(rb.body_ok, rb.notices, BODY_GATE)}，争取 {BODY_GOAL}%）"
    )
    if rb.no_body:
        body += f"；读不了的：{_counts(rb.no_body)}"
    hours = rb.oldest_pending_hours
    if not rb.pending:
        wait = "没有等取文字的"
    else:
        wait = f"等取文字的 {rb.pending} 个"
        if hours is not None and rb.oldest_pending_doc:
            doc_id, title = rb.oldest_pending_doc
            which = "有收录时间的里最久的" if rb.pending_unknown else "最久的"
            wait += f"，{which}等了 {hours} 小时（通知 {doc_id}《{title}》）"
        if rb.pending_unknown:
            wait += f"，{rb.pending_unknown} 个附件所在的通知没有收录时间、等了多久不知道"
    reached = "达到" if gates(rb)[2] else "没达到"
    pending = f"{wait}（门槛不超过一轮 {ROUND_HOURS} 小时，{reached}）"
    attachments = (
        f"附件 {rb.attachments} 个，能读 {rb.attachments_ok} 个"
        f"（{_pct(rb.attachments_ok, rb.attachments)}）；{pending}"
    )
    others = Counter(
        {k: v for k, v in rb.attachment_problems.items() if k != ATTACHMENT_PROBLEMS["pending"]}
    )
    if others:
        attachments += f"；其他读不了的：{_counts(others)}"
    whole = (
        f"整条能读 {rb.whole_ok} 份（{_pct(rb.whole_ok, rb.notices)}，"
        f"{_gate(rb.whole_ok, rb.notices, WHOLE_GATE)}）：正文和附件都能读、附件没有扫描页；"
        f"正文和附件都能读、但附件有扫描页的 {rb.scanned_only} 份单列、不算"
    )
    sites = [
        f"{site} {count}（{'、'.join(f'{label} {n}' for label, n in top)}）"
        for site, count, top in rb.sites
    ]
    budget = MAX_ALERT_BYTES - len("【有据】".encode())
    while True:
        lines = [head, body, attachments, whole]
        if sites:
            lines.append(
                "读不全最多的网站（份数；括号里是原因和份数，一份可能有几个原因）："
                + "；".join(sites)
            )
        text = "\n".join(lines)
        if len(text.encode()) <= budget or not sites:
            return text
        sites.pop()


def pick_for_review(
    session: Session, client: ChatClient, now: datetime | None = None
) -> list[Pick]:
    """请 AI 从候选截止还没过的待核对通知里挑几份建议先核对；出错时返回空，不影响日报。"""
    today = (now or datetime.now(UTC)).astimezone(BEIJING).date()
    rows = session.execute(
        visible_documents(
            select(
                Document.id,
                Document.title,
                Source.dept_full_name,
                func.min(DocumentDeadline.candidate_date).label("due"),
            )
            .join(DocumentDeadline, DocumentDeadline.document_id == Document.id)
            .join(Source, Source.id == Document.source_id)
            .where(DocumentDeadline.status == "pending", DocumentDeadline.candidate_date >= today)
        )
        .group_by(Document.id, Document.title, Source.dept_full_name)
        .order_by("due", Document.id)
        .limit(_PICK_FROM)
    ).all()
    if not rows:
        return []
    offered = {doc_id: (title, dept, due) for doc_id, title, dept, due in rows}
    listing = "\n".join(
        f"编号 {doc_id}｜{dept}｜{title[:60]}｜候选截止 {due}" for doc_id, title, dept, due in rows
    )
    try:
        result = client.complete_json(_SYSTEM, f"待核对通知：\n{listing}", max_tokens=300)
    except AIError:
        return []
    picks: list[Pick] = []
    items = result.get("picks")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            doc_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if doc_id not in offered or any(p.document_id == doc_id for p in picks):
            continue
        title, dept, due = offered[doc_id]
        reason = str(item.get("reason") or "").strip()[:20]
        picks.append(Pick(doc_id, title, dept, due, reason))
        if len(picks) >= _PICKS:
            break
    return picks


def _cost_cap(cost: float) -> str:
    """费用上限往上取到分：0.004 元写成 0.01（四舍五入会写成 0.00）。

    先去掉浮点误差，1.8 不会因为存成 1.8000000001 多算一分。
    """
    return f"{math.ceil(round(cost * 100, 6)) / 100:.2f}"


def _tokens(value: int) -> str:
    if value < 10000:
        return str(value)
    return f"{value // 10000} 万" if value % 10000 == 0 else f"{value / 10000:.1f} 万"


def _md(value: date) -> str:
    return f"{value.month}月{value.day}日"


def _mb(value: int) -> str:
    return "不到 1 MB" if value < 1024 * 1024 else f"约 {value / 1024 / 1024:.0f} MB"


def _pct(part: int, whole: int) -> str:
    # 向下取整：有一次失败就不会写成 100%
    return f"{math.floor(100 * part / whole)}%" if whole else "—"


def _quality_line(digest: Digest) -> str:
    if not digest.quality_ok:
        return "数据（近 30 天发布）：取不到（统计出错，看容器日志）"
    return (
        f"数据（近 30 天发布）：申报通知 {digest.notices_30} 份，"
        f"有截止候选 {_pct(digest.notices_with_candidates, digest.notices_30)}；"
        f"名单 {digest.lists_30} 份，解析出企业名单 {_pct(digest.lists_parsed, digest.lists_30)}"
    )


def _usage_line(digest: Digest) -> str:
    if not digest.usage_ok:
        return "用量：取不到（统计出错，看容器日志）"
    db = f"数据库{_mb(digest.db_bytes)}" if digest.db_bytes is not None else "数据库大小取不到"
    return (
        f"用量：{db}；附件副本 {digest.attachment_copies} 个，{_mb(digest.attachment_bytes)}；"
        f"批量工具结果文件 {digest.result_files} 个"
    )


def _access_line(digest: Digest) -> str:
    yesterday = _md(digest.day - timedelta(days=1))
    if not digest.access_ok:
        return f"接口（{yesterday}）：取不到（统计出错，看容器日志）"
    if not digest.access:
        return f"接口（{yesterday}）：没有汇总（访问日志归档没跑成，或当天没有小程序请求）"
    parts = []
    for service, requests, errors, slow in digest.access:
        fast = requests - errors - slow
        rate = math.floor(1000 * fast / requests) / 10 if requests else 100.0
        parts.append(
            f"{service} {requests} 次，5xx {errors} 次、其他请求 3 秒及以上 {slow} 次，"
            f"3 秒内成功 {rate:.1f}%"
        )
    return f"接口（{yesterday}，小程序接口）：" + "；".join(parts)


def digest_text(digest: Digest, picks: list[Pick]) -> str:
    """群提醒正文：数字部分完整保留，超出字节上限时少列 AI 建议。"""
    failures = ""
    if digest.source_failures:
        shown = "；".join(digest.source_failures[:3])
        more = "等" if len(digest.source_failures) > 3 else ""
        failures = f"；异常 {len(digest.source_failures)} 个：{shown}{more}"
    rate = 100 * digest.sources_ok / digest.sources_total if digest.sources_total else 0.0
    kinds = "、".join(f"{kind} {count}" for kind, count in digest.new_by_kind.items())
    nearest = f"，最近 {_md(digest.nearest_due)}" if digest.nearest_due else ""
    if digest.last_reviewed is None:
        reviewed = "还没核对过一条"
    else:
        days = (digest.day - digest.last_reviewed).days
        reviewed = f"上次核对 {_md(digest.last_reviewed)}" + (
            f"（{days} 天前）" if days > 0 else ""
        )
    todo = f"今天先核：7 天内到期的截止待核对 {digest.pending_7} 条（管理台“截止日期 · 7 天内”）"
    lines = [
        f"运维日报 {_md(digest.day)}",
        f"{todo}；{reviewed}",
        f"抓取：启用栏目 {digest.sources_total} 个，今天检查成功 {digest.sources_ok} 个"
        f"（{rate:.1f}%）{failures}",
        f"新文件：24 小时新发现 {digest.new_documents} 份"
        + (f"（{kinds}）" if kinds else "")
        + f"；补详情成功 {digest.details_ok}、失败 {digest.details_failed}；"
        f"新截止候选 {digest.new_candidates} 条",
        f"核对：24 小时通过 {digest.approved}、驳回 {digest.rejected}；"
        f"待核对 {digest.pending} 条（30 天内截止 {digest.pending_30} 条{nearest}）；"
        f"类型待定 {digest.kinds_pending}；质检存疑 {digest.audit_flagged}；"
        f"纠错待处理 {digest.feedback_pending}",
        f"用户：新增 {digest.new_users}、24 小时活跃 {digest.active_users}、"
        f"累计 {digest.total_users}；新关注 {digest.new_follows}；"
        f"提醒发出 {digest.reminders_sent}、失败 {digest.reminders_failed}",
        _access_line(digest),
        _quality_line(digest),
        readability_summary(digest),
        _usage_line(digest),
    ]
    safety = _safety_line(digest)
    if safety:
        lines.append(safety)
    usage = digest.usage
    if usage is not None and (usage.calls or usage.cumulative):
        if usage.cost is None:
            cost = "，费用未知（有未列价格的模型）"
        elif not usage.priced_on:
            cost = ""  # 当天没有用量，只报累计
        else:
            # 几个模型核价日期不同时并列写出（不用"—"，免得被看成一段时间）
            checked = "、".join(_md(day) for day in usage.priced_on)
            cost = (
                f"，当日模型调用费最多 {_cost_cap(usage.cost)} 元"
                f"（按官网最高价估，价格核于 {checked}，实际可能更低）"
            )
        quota = "；".join(
            f"{model} 累计 {_tokens(tokens)} tokens"
            for model, tokens in list(usage.cumulative.items())[:3]
        )
        lines.append(
            f"AI（{_md(usage.day)}）：调用 {usage.calls} 次、出错 {usage.errors} 次，"
            f"输入 {_tokens(usage.prompt_tokens)}、输出 {_tokens(usage.completion_tokens)} tokens"
            f"{cost}" + (f"；{quota}" if quota else "")
        )
    marks = "①②③④⑤"
    pick_lines = [
        f"{marks[i]}《{p.title[:22]}》（{p.dept[:12]}）候选 {_md(p.due)}截止"
        + (f"：{p.reason}" if p.reason else "")
        for i, p in enumerate(picks)
    ]
    # 群提醒会加"【有据】"前缀；放不下时从最后一条 AI 建议开始去掉，数字部分本身超长时截断
    budget = MAX_ALERT_BYTES - len("【有据】".encode())
    while True:
        body = lines + (["AI 建议先核对（仅供参考）："] + pick_lines if pick_lines else [])
        text = "\n".join(body)
        if len(text.encode()) <= budget:
            return text
        if not pick_lines:
            cut = budget - len("…".encode())
            return text.encode()[:cut].decode("utf-8", "ignore") + "…"
        pick_lines.pop()
