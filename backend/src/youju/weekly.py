"""政策周报：公开题录与人工核对字段套固定模板，不调用模型或发送消息。"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from youju.crawler.detail import redact_sensitive
from youju.dedup import canonical_ids, visible_documents
from youju.fields import SUPPORT_LABELS
from youju.models import (
    DigestIssue,
    DigestProfile,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    DocumentMerge,
    ListEntry,
    Source,
)
from youju.sources.onboard import PROVINCES
from youju.takedown import available_condition

BEIJING = ZoneInfo("Asia/Shanghai")
COLUMNS = {
    "applications": "本周新发布的申报通知",
    "deadlines": "未来 30 天内截止",
    "lists": "本周公示/公布的名单",
    "policies": "本周新发布的政策文件",
}
DISCLAIMER = "第三方整理，非政府平台；信息来自政府网站公开发布的文件，以原文为准。"
CALL_TO_ACTION = "查看原文和订阅截止提醒：在微信搜索小程序「有据」，搜标题即可。"
PROVINCE_ORDER = {province: n for n, province in enumerate(PROVINCES)}
# 公众号正文中的题录字段也可能夹带网址，展示时统一去掉；带链接版保留原题录。
URL_IN_TEXT = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"'，。；、（）]+", re.IGNORECASE)
LIMIT = 30
log = logging.getLogger("youju.weekly")
# 禁用文字只约束我们自己写的文字（模板、方案名称）。政府文件的原标题照录，不改写、也不因为
# 含"智能""最"等字被排除——"智能制造""人工智能"类通知很多，排除会大量漏项。
FORBIDDEN = re.compile(r"AI|智能|最|重磅|必看", re.IGNORECASE)


def now() -> datetime:
    return datetime.now(UTC)


def week_interval(week: date | None = None, stamp: datetime | None = None) -> tuple[date, date]:
    """默认上一个完整自然周；显式日期必须是周一，不按发现时间计新发布。"""
    if week is None:
        today = (stamp or now()).astimezone(BEIJING).date()
        week = today - timedelta(days=today.weekday() + 7)
    if week.weekday() != 0:
        raise ValueError("统计周请填写周一日期")
    return week, week + timedelta(days=6)


def safe_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not any(ord(char) < 32 for char in value)
        )
    except ValueError:
        return False


def _text(value: str) -> str:
    return redact_sensitive(value).strip()


@dataclass(frozen=True)
class Item:
    id: int
    title: str
    issuer: str
    province: str
    published: date | None
    number: str
    url: str
    deadlines: tuple = ()
    support: tuple[str, ...] = ()
    entries: int = 0


@dataclass
class Generation:
    issue: DigestIssue | None
    outcome: str
    reason: str


def _items(session: Session, profile: DigestProfile, start: date, end: date, stamp: datetime):
    local = stamp.astimezone(BEIJING)
    today = local.date()
    published = func.coalesce(DocumentDetail.publish_date, Document.publish_date)
    # 先限制候选范围；历史申报只为未来截止栏目读取，历史政策和名单不扫描。
    due_ids = select(DocumentDeadline.document_id).where(
        DocumentDeadline.status == "approved",
        DocumentDeadline.approved_date.between(today, today + timedelta(days=30)),
    )
    master_due = select(DocumentMerge.canonical_id).where(
        DocumentMerge.active.is_(True), DocumentMerge.duplicate_id.in_(due_ids)
    )
    stmt = visible_documents(
        select(
            Document.id,
            Document.title,
            Document.url,
            Document.rule_kind,
            Document.kind_override,
            published.label("published"),
            Source.province,
            func.coalesce(func.nullif(DocumentDetail.issuer, ""), Source.dept_full_name).label(
                "issuer"
            ),
            DocumentDetail.doc_number,
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(
            Source.rights_level == 1,
            available_condition(),
            or_(
                published.between(start, end), Document.id.in_(due_ids), Document.id.in_(master_due)
            ),
        )
    )
    if profile.provinces:
        stmt = stmt.where(Source.province.in_(["国家", *profile.provinces]))
    rows = session.execute(stmt.order_by(published.desc(), Document.id)).all()
    rows = [
        row
        for row in rows
        if safe_url(row.url)
        and (not profile.keywords or any(word in row.title for word in profile.keywords))
    ]
    ids = [row.id for row in rows]
    if not ids:
        return {key: [] for key in COLUMNS}
    members = session.scalars(
        select(Document.id)
        .outerjoin(
            DocumentMerge,
            (DocumentMerge.duplicate_id == Document.id) & DocumentMerge.active.is_(True),
        )
        .join(Source, Source.id == Document.source_id)
        .where(
            func.coalesce(DocumentMerge.canonical_id, Document.id).in_(ids),
            Source.rights_level == 1,
            available_condition(),
        )
    ).all()
    mapping = canonical_ids(session, list(members))
    deadlines = session.scalars(
        select(DocumentDeadline)
        .where(
            DocumentDeadline.document_id.in_(members),
            DocumentDeadline.status.in_(("approved", "changed")),
        )
        .order_by(DocumentDeadline.id)
    ).all()
    changed = {
        (mapping.get(d.document_id, d.document_id), d.kind, d.approved_date, d.approved_time)
        for d in deadlines
        if d.status == "changed"
    }
    by_due: dict[int, dict] = {}
    for d in deadlines:
        master = mapping.get(d.document_id, d.document_id)
        key = (master, d.kind, d.approved_date, d.approved_time)
        if d.status == "approved" and d.approved_date and key not in changed:
            by_due.setdefault(master, {})[key] = d
    support: dict[int, set[str]] = {}
    fields = session.scalars(
        select(DocumentField)
        .join(DocumentDetail, DocumentDetail.document_id == DocumentField.document_id)
        .where(
            DocumentField.document_id.in_(members),
            DocumentField.field == "support",
            DocumentField.status == "approved",
            DocumentDetail.status == "ok",
            DocumentField.body_hash == func.sha2(DocumentDetail.body_text, 256),
        )
    ).all()
    for field in fields:
        if field.approved_value in SUPPORT_LABELS:
            support.setdefault(mapping.get(field.document_id, field.document_id), set()).add(
                field.approved_value
            )
    # 数据库只返回计数，名称和个人记录不进入模板；同名跨附件、跨归并网址只计一次。
    master = func.coalesce(DocumentMerge.canonical_id, Document.id)
    counts = dict(
        session.execute(
            select(master, func.count(func.distinct(ListEntry.normalized_name)))
            .select_from(ListEntry)
            .join(Document, Document.id == ListEntry.document_id)
            .outerjoin(
                DocumentMerge,
                (DocumentMerge.duplicate_id == Document.id) & DocumentMerge.active.is_(True),
            )
            .where(Document.id.in_(members))
            .group_by(master)
        ).all()
    )
    columns = {key: [] for key in COLUMNS}
    for row in rows:
        kind = row.kind_override or row.rule_kind
        dues = tuple(
            sorted(
                by_due.get(row.id, {}).values(),
                key=lambda d: (
                    {"企业申报截止": 0, "材料报送截止": 1, "推荐或审核截止": 2}.get(d.kind, 9),
                    d.approved_date,
                    d.approved_time.isoformat() if d.approved_time else "",
                ),
            )
        )
        item = Item(
            row.id,
            _text(row.title),
            _text(row.issuer),
            row.province,
            row.published,
            _text(row.doc_number or ""),
            row.url,
            dues,
            tuple(sorted(support.get(row.id, set()))),
            counts.get(row.id, 0),
        )
        if row.published and start <= row.published <= end:
            column = {"申报通知": "applications", "名单": "lists", "政策": "policies"}.get(kind)
            if column:
                columns[column].append(item)
        if kind == "申报通知":
            upcoming = [
                d
                for d in dues
                if today <= d.approved_date <= today + timedelta(days=30)
                and (
                    d.approved_date > today
                    or d.approved_time is None
                    or d.approved_time >= local.time().replace(tzinfo=None)
                )
            ]
            if upcoming:
                selected = min(upcoming, key=lambda d: (d.approved_date, d.id))
                columns["deadlines"].append((item, selected))
    return columns


def _one_line(value: str) -> str:
    return " ".join(_text(value).split())


def _public_text(value: str) -> str:
    return _one_line(URL_IN_TEXT.sub("", value))


def _month_day(value: date) -> str:
    return f"{value.month} 月 {value.day} 日"


def _deadline_text(item: Item, due=None, *, linked=False) -> str:
    deadlines = (due,) if due else item.deadlines
    if not deadlines:
        return "截止时间见原文"
    parts = []
    for deadline in deadlines:
        day = deadline.approved_date
        at = f" {deadline.approved_time:%H:%M}" if deadline.approved_time else ""
        kind = f"{deadline.kind} " if linked or len(deadlines) > 1 else ""
        parts.append(f"{kind}{day.isoformat() if linked else _month_day(day)}{at}（已核对）")
    return "截止：" + "；".join(parts)


def render(profile: DigestProfile, start: date, end: date, today: date, columns: dict):
    title = (
        f"政策申报周报｜{start.month} 月 {start.day} 日—"
        f"{end.month} 月 {end.day} 日（{profile.name}）"
    )
    html = ['<section style="font-size:15px;line-height:1.8;color:#222b35;">']
    lines, linked_lines = [], []
    counts = {}
    used = set()

    def paragraph(value):
        html.append(f'<p style="margin:8px 0;">{escape(_public_text(value))}</p>')
        lines.append(_public_text(value))
        linked_lines.append(_one_line(value))

    for key, label in COLUMNS.items():
        values = columns[key] if profile.columns.get(key, False) else []
        counts[key] = len(values)
        if not values:
            continue

        def order(value, column=key):
            item, due = value if column == "deadlines" else (value, None)
            return (
                PROVINCE_ORDER.get(item.province, len(PROVINCES)),
                item.province,
                due.approved_date.toordinal() if due else -(item.published or date.min).toordinal(),
                item.id,
            )

        values = sorted(values, key=order)
        group_counts = Counter(
            (value[0] if key == "deadlines" else value).province for value in values
        )
        heading = f"{label}（{len(values)} 条）"
        html.append(f'<h2 style="font-size:20px;margin:24px 0 12px;">{escape(heading)}</h2>')
        lines.extend(["", heading])
        linked_lines.extend(["", heading])
        if key == "deadlines":
            paragraph(f"截止统计基准：{today.isoformat()}")
        province = None
        for value in values[:LIMIT]:
            item, due = value if key == "deadlines" else (value, None)
            used.add(item.id)
            if item.province != province:
                province = item.province
                group = "国家部委" if province == "国家" else _public_text(province)
                group = f"{group}（{group_counts[province]} 条）"
                html.append(f'<h3 style="font-size:17px;margin:16px 0 8px;">{escape(group)}</h3>')
                lines.append(group)
                linked_lines.append(group)
            html.append(
                '<p style="margin:12px 0 0;"><strong>'
                f"{escape(_public_text(item.title))}</strong></p>"
            )
            lines.append(_public_text(item.title))
            metadata = [item.issuer]
            day = _month_day(item.published) if item.published else "发布日期见原文"
            metadata.append(f"{day}发布" if item.published and key != "lists" else day)
            if key in {"applications", "deadlines"}:
                metadata.append(_deadline_text(item, due))
                if item.support:
                    metadata.append(f"支持方式：{'、'.join(item.support)}")
            elif key == "lists":
                states = [state for state in ("公示", "公布") if state in item.title]
                if states:
                    metadata.append("/".join(states))
                if item.entries:
                    metadata.append(f"{item.entries} 条")
            elif key == "policies":
                metadata.append(f"文号：{item.number or '见原文'}")
            metadata_text = _public_text(" · ".join(metadata))
            html.append(
                '<p style="font-size:12px;color:#687482;margin:0 0 8px;">'
                f"{escape(metadata_text)}</p>"
            )
            lines.append(metadata_text)
            # 五列固定，每条一行；保留完整年月日与核定时刻，供邮件、群聊查原文。
            linked_lines.append(
                "｜".join(
                    _one_line(field).replace("｜", "|")
                    for field in (
                        item.title,
                        item.issuer,
                        item.published.isoformat() if item.published else "发布日期见原文",
                        _deadline_text(item, due, linked=True),
                        item.url,
                    )
                )
            )
        if len(values) > LIMIT:
            paragraph(f"共 {len(values)} 条，其余在「有据」小程序里查看")
    if not sum(counts.values()):
        return None
    lines.append("")
    linked_lines.append("")
    paragraph(CALL_TO_ACTION)
    paragraph(DISCLAIMER)
    html.append("</section>")
    return (
        title,
        "".join(html),
        "\n".join(lines).lstrip(),
        "\n".join(linked_lines).lstrip(),
        counts,
        sorted(used),
    )


def generate(
    session: Session, profile_id: int, week: date | None = None, stamp: datetime | None = None
) -> Generation:
    stamp = stamp or now()
    start, end = week_interval(week, stamp)
    # 所有生成和发布先锁方案再锁期刊；也串行化同一区间首次插入，避免并发覆盖已发布快照。
    profile = session.scalar(
        select(DigestProfile)
        .where(DigestProfile.id == profile_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if profile is None:
        raise ValueError("周报方案不存在")
    if FORBIDDEN.search(profile.name):
        raise ValueError("方案名称含周报禁用文字")
    issue = session.scalar(
        select(DigestIssue)
        .where(DigestIssue.profile_id == profile.id, DigestIssue.week_start == start)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if issue and issue.status == "published":
        session.commit()
        return Generation(issue, "published", "已标为已发布，未覆盖")
    today = stamp.astimezone(BEIJING).date()
    rendered = render(profile, start, end, today, _items(session, profile, start, end, stamp))
    if rendered is None:
        # 重新生成也不能留下旧草稿里的下架题录；无内容不创建空期刊。
        if issue:
            session.delete(issue)
        session.commit()
        reason = "启用栏目没有符合公开范围、地区、关键词和统计区间的内容"
        log.info("周报未生成：方案 #%s，%s 至 %s；%s", profile.id, start, end, reason)
        return Generation(None, "empty", reason)
    title, body_html, body_text, body_text_with_links, counts, document_ids = rendered
    if issue is None:
        issue = DigestIssue(profile_id=profile.id, week_start=start, week_end=end)
        session.add(issue)
    issue.title, issue.body_html, issue.body_text = title, body_html, body_text
    issue.body_text_with_links = body_text_with_links
    issue.counts, issue.document_ids, issue.generated_at = counts, document_ids, stamp
    session.commit()
    return Generation(issue, "generated", "草稿已生成")


def issue_available(session: Session, issue: DigestIssue) -> bool:
    """已生成快照也要检查实时公开开关，不能继续复制下架或转非公开的文件。"""
    ids = set(issue.document_ids)
    current = set(
        session.scalars(
            select(Document.id)
            .join(Source)
            .where(
                Document.id.in_(ids),
                Source.rights_level == 1,
                available_condition(),
            )
        )
    )
    return ids == current
