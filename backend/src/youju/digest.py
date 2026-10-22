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
    notices_with_body: int = 0
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
    from youju.takedown import available_condition

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
    # 有正文：和详情页取正文同一规则（v1._reading_body）——只取一级来源、抓取成功的；这份文件
    # 自己有就用自己的，没有再用同组最长的；够 200 字才算
    lengths: dict[int, dict[int, int]] = {master: {} for master in notices}
    for doc_id, length in session.execute(
        select(DocumentDetail.document_id, func.char_length(DocumentDetail.body_text))
        .join(Document, Document.id == DocumentDetail.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.document_id.in_(list(owner)),
            DocumentDetail.status == "ok",
            Source.rights_level == 1,
            available_condition(),
        )
    ):
        if length:
            lengths[owner[doc_id]][doc_id] = length
    # 选出详情页会用的那一份（自己的，或同组最长的），再用详情页同一个函数清理后判断够不够字数：
    # 详情页先脱敏、去掉网页固定文字和多余空行，不到 200 字就不显示正文
    chosen: dict[int, int] = {}
    for master, by_doc in lengths.items():
        if master in by_doc:
            chosen[master] = master
        elif by_doc:
            chosen[master] = max(by_doc, key=lambda doc_id: (by_doc[doc_id], -doc_id))
    shown = 0
    picked = list(chosen.values())
    for start in range(0, len(picked), 100):
        for (body,) in session.execute(
            select(DocumentDetail.body_text).where(
                DocumentDetail.document_id.in_(picked[start : start + 100])
            )
        ):
            shown += v1.body_for_reading(body, "申报通知")["status"] == "ok"
    digest.notices_with_body = shown
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
        f"有正文 {_pct(digest.notices_with_body, digest.notices_30)}、"
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
        _usage_line(digest),
    ]
    usage = digest.usage
    if usage is not None and (usage.calls or usage.cumulative):
        cost = (
            "，费用未知（有未列价格的模型）"
            if usage.cost is None
            else f"，当日模型调用费最多 {_cost_cap(usage.cost)} 元（按官网最高价估，实际通常更低）"
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
