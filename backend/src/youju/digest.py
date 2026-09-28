"""运维日报（D-023）：每天早上把数据底座的运行情况和待办发到群里，
AI 从待核对的通知里挑几份建议先核对。

- 数字都是查数据库算的；待办条数直接用核对台的计数函数，和核对台看到的一致。
- AI 只做"挑"：给它待核对通知的编号、发文单位、标题和候选截止日期（政府公开信息，
  不含任何用户数据），它返回编号和理由；编号必须在给它的清单里，理由只给内部看
  （R1 不给用户看 AI 写的文字）。
- 群机器人一条消息有字节上限：放不下时先少列 AI 建议，数字部分不截。
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.ai.client import AIError, ChatClient
from youju.ai.usage import BEIJING, FREE_QUOTA, UsageSummary, usage_summary
from youju.alerts import MAX_ALERT_BYTES
from youju.classify import effective_kind
from youju.dedup import visible_documents
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    Follow,
    Source,
    Subscription,
    User,
)
from youju.source_health import check_sources

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
    pending_30: int = 0
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


@dataclass
class Pick:
    document_id: int
    title: str
    due: date
    reason: str


def _count(session: Session, stmt) -> int:
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def build_digest(session: Session, now: datetime | None = None) -> Digest:
    """过去 24 小时的运行情况、当前待办和昨天（北京时间）的 AI 用量。"""
    from youju.api import app as admin

    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    today = now.astimezone(BEIJING).date()
    digest = Digest(day=today)

    health = check_sources(session, now=now)
    digest.sources_total, digest.sources_ok = health.total, health.successful
    digest.source_failures = health.failures

    # 新文件：同一通知的多个网址只算一份
    titles = session.execute(
        visible_documents(
            select(Document.title, Document.kind_override).where(
                Document.is_baseline.is_(False), Document.first_seen_at >= since
            )
        )
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
    digest.new_candidates = _count(
        session, select(DocumentDeadline.id).where(DocumentDeadline.created_at >= since)
    )
    reviewed = visible_documents(
        select(DocumentDeadline.status)
        .join(Document, Document.id == DocumentDeadline.document_id)
        .where(DocumentDeadline.reviewed_at >= since)
    )
    by_status = Counter(session.scalars(reviewed))
    digest.approved, digest.rejected = by_status["approved"], by_status["rejected"]

    # 待办：和核对台同一口径
    digest.pending = admin.pending_count(session, None, "all")["pending"]
    digest.pending_30 = admin.pending_count(session, None, "30")["pending"]
    digest.nearest_due = session.scalar(
        select(func.min(DocumentDeadline.candidate_date)).where(
            DocumentDeadline.status == "pending", DocumentDeadline.candidate_date >= today
        )
    )
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
    return digest


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
    offered = {doc_id: (title, due) for doc_id, title, _, due in rows}
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
        title, due = offered[doc_id]
        reason = str(item.get("reason") or "").strip()[:20]
        picks.append(Pick(doc_id, title, due, reason))
        if len(picks) >= _PICKS:
            break
    return picks


def _tokens(value: int) -> str:
    if value < 10000:
        return str(value)
    return f"{value // 10000} 万" if value % 10000 == 0 else f"{value / 10000:.1f} 万"


def _md(value: date) -> str:
    return f"{value.month}月{value.day}日"


def digest_text(digest: Digest, picks: list[Pick]) -> str:
    """群提醒正文：数字部分完整保留，超出字节上限时少列 AI 建议。"""
    failures = ""
    if digest.source_failures:
        shown = "；".join(item[:30] for item in digest.source_failures[:2])
        more = "等" if len(digest.source_failures) > 2 else ""
        failures = f"；异常 {len(digest.source_failures)} 个：{shown}{more}"
    rate = 100 * digest.sources_ok / digest.sources_total if digest.sources_total else 0.0
    kinds = "、".join(f"{kind} {count}" for kind, count in digest.new_by_kind.items())
    nearest = f"，最近 {_md(digest.nearest_due)}" if digest.nearest_due else ""
    lines = [
        f"运维日报 {_md(digest.day)}",
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
    ]
    usage = digest.usage
    if usage is not None and (usage.calls or usage.cumulative):
        cost = (
            "，费用未知（有未列价格的模型）" if usage.cost is None else f"，约 {usage.cost:.2f} 元"
        )
        quota = "；".join(
            f"{model} 累计 {_tokens(tokens)} tokens（免费额度 {_tokens(FREE_QUOTA)}）"
            for model, tokens in usage.cumulative.items()
        )
        lines.append(
            f"AI（{_md(usage.day)}）：调用 {usage.calls} 次、出错 {usage.errors} 次，"
            f"输入 {_tokens(usage.prompt_tokens)}、输出 {_tokens(usage.completion_tokens)} tokens"
            f"{cost}" + (f"；{quota}" if quota else "")
        )
    marks = "①②③④⑤"
    pick_lines = [
        f"{marks[i]}《{p.title[:22]}》候选 {_md(p.due)}截止" + (f"：{p.reason}" if p.reason else "")
        for i, p in enumerate(picks)
    ]
    # 群提醒会加"【有据】"前缀；放不下时从最后一条 AI 建议开始去掉
    budget = MAX_ALERT_BYTES - len("【有据】".encode())
    while True:
        body = lines + (["AI 建议先核对（仅供参考）："] + pick_lines if pick_lines else [])
        text = "\n".join(body)
        if len(text.encode()) <= budget or not pick_lines:
            return text
        pick_lines.pop()
