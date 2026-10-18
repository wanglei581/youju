"""政策周报、临期提醒和名单速报：公开题录与人工核对字段套固定模板，不调用模型或发送消息。

公众号正文的排法按《公众号与小程序搭配方案》（PR #287）第四节第 5 条：首行是声明和统计周；
周报按省份分块（国家部委一块排最前，各省按本期条目多少排），块内依次是 30 天内已核对截止、
新申报通知、名单、政策文件，每块最多 5 条，其余在小程序"本期清单"里看；全国性的政策文件单列
一段；文末写入口、数据截至时间、纠错方式和"不代办申报、不承诺结果"。正文不放网址（公众号
正文不能链到政府网站），每条靠小程序页面承接。
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from youju.api import v1
from youju.classify import (
    LIST_OFF_TOPIC,
    PERSONAL_CONTEST,
    PERSONAL_QUALIFICATIONS,
    PERSONAL_WORDS,
    effective_kind,
    list_status,
)
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
# 稿件类型：周报按上一个完整自然周统计；临期提醒归入生成当天所在的周，每周四生成；
# 名单速报一份名单一篇，大名单公布时在管理台手动生成。
KINDS = {"weekly": "周报", "reminder": "临期提醒", "list_flash": "名单速报"}
REMINDER_DAYS = 7
REMINDER_WEEKDAY = 4
REMINDER_EMPTY = f"本周未来 {REMINDER_DAYS} 天没有已核对截止的申报项目"
# 声明放最前面，转出去也带着；文末再写一句不代办、不承诺（方案第四节第 5 条、agy 合规意见）。
HEAD = "第三方整理，非政府平台，以原文为准。"
FOOT = "第三方整理的公开信息索引，不代办申报、不承诺结果。"
CORRECTION = "纠错：在小程序文件详情页点「纠错」，或直接给本号发消息。"
GUIDE = "使用说明：见本号《使用说明》，讲收录范围和截止日期怎么核对。"
# 写截止日期的稿件文末加一句（agy 合规意见）：初审、推荐往往比文件写的截止早，
# 免得读者按文件日期误了时间。
LOCAL_EARLIER = (
    "地市、区县组织初审或推荐的，报送时间可能早于文件写的截止，以属地主管部门的要求为准。"
)
NATIONAL = "国家"
PROVINCE_ORDER = {province: n for n, province in enumerate(PROVINCES)}
# 公众号正文中的题录字段也可能夹带网址，展示时统一去掉；带链接版保留原题录。
# 不带协议头的裸域名（如 kjt.example.gov.cn/tzgg）也去掉：只认常见后缀，免得误伤"2.0 版"这类写法。
URL_IN_TEXT = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"'，。；、（）]+"
    r"|(?<![a-z0-9.@-])(?:[a-z0-9-]+\.)+(?:gov\.cn|com\.cn|org\.cn|net\.cn|edu\.cn|cn|com|net|org)"
    r"(?![a-z0-9-])(?:[/?#][^\s<>\"'，。；、（）]*)?",
    re.IGNORECASE,
)
PER_BLOCK = 5  # 周报每省（含国家部委）最多列几条
POLICY_LIMIT = 10  # 全国性政策文件最多列几条
REMINDER_LIMIT = 10  # 临期提醒最多列几项（方案第四节第 4 条）
# 公众号文章标题上限（官方"新建草稿"接口：标题总长度不超过 32 个字，10/18 核对）。周报标题里的
# 统计按这个长度能放几项放几项；名单速报照录原标题，超了由管理台提示人工删减，不自动截断。
TITLE_LIMIT = 32
# 一份文件只列在一个栏目：同时是本周新通知又在 30 天内截止的，列在截止里。
SECTION_ORDER = ("deadlines", "applications", "lists", "policies")
# 小程序页面路径（管理台显示，创始人在公众号编辑器里插入小程序链接时粘贴，不带开头的斜杠）。
DIGEST_PAGE = "pages/digest/index"
log = logging.getLogger("youju.weekly")
# 禁用文字只约束我们自己写的文字（模板、方案名称）。政府文件的原标题照录，不改写、也不因为
# 含"智能""最"等字被排除——"智能制造""人工智能"类通知很多，排除会大量漏项。
FORBIDDEN = re.compile(r"AI|智能|最|重磅|必看", re.IGNORECASE)

_SECTION = '<section style="font-size:15px;line-height:1.8;color:#222b35;">'
_PLAIN = "margin:8px 0;"
_NOTE = "font-size:13px;color:#687482;margin:8px 0;"
_TITLE = "margin:12px 0 0;"
_META = "font-size:12px;color:#687482;margin:0 0 8px;"
_H3 = "font-size:17px;margin:20px 0 8px;"


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


def reminder_interval(week: date | None = None, stamp: datetime | None = None) -> tuple[date, date]:
    """临期提醒归入生成当天（北京时间）所在的自然周；截止按生成当天计算，只能生成本周。"""
    today = (stamp or now()).astimezone(BEIJING).date()
    current = today - timedelta(days=today.weekday())
    if week is not None and week != current:
        if week.weekday() != 0:
            raise ValueError("统计周请填写周一日期")
        raise ValueError("临期提醒按生成当天计算截止，只能生成本周：请留空或填写本周周一")
    return current, current + timedelta(days=6)


def _monday(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def interval(kind: str, week: date | None = None, stamp: datetime | None = None):
    if kind not in KINDS:
        raise ValueError("稿件类型不支持")
    if kind == "list_flash":
        # 名单速报归入名单发布的那一周（generate 里按名单重新算）；这里只是生成当天的周。
        return _monday((stamp or now()).astimezone(BEIJING).date())
    return (reminder_interval if kind == "reminder" else week_interval)(week, stamp)


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
    versions: int = 0


@dataclass
class Generation:
    issue: DigestIssue | None
    outcome: str
    reason: str


@dataclass
class Rendered:
    title: str
    body_html: str
    body_text: str
    body_text_with_links: str
    counts: dict
    document_ids: list[int]
    items: list[dict] = field(default_factory=list)


def _issuer_column():
    return func.coalesce(func.nullif(DocumentDetail.issuer, ""), Source.dept_full_name).label(
        "issuer"
    )


def _public_members(session: Session, ids: list[int]) -> list[int]:
    """这些主文件所在归并组里能公开的成员（同样只认一级来源、没有下架）。"""
    return list(
        session.scalars(
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
    )


def _entry_counts(session: Session, members: list[int]) -> dict[int, int]:
    # 数据库只返回计数，名称和个人记录不进入模板；同名跨附件、跨归并网址只计一次。
    if not members:
        return {}
    master = func.coalesce(DocumentMerge.canonical_id, Document.id)
    return dict(
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
            _issuer_column(),
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
        stmt = stmt.where(Source.province.in_([NATIONAL, *profile.provinces]))
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
    members = _public_members(session, ids)
    # "已变更"看整个归并组（含非一级来源的副本，和小程序 v1._approved 一样）：副本上标了
    # 已变更的截止，主文件上同一截止也不能再写；写进公众号的日期仍只取一级来源上核对通过的。
    group = list(
        session.scalars(
            select(Document.id)
            .outerjoin(
                DocumentMerge,
                (DocumentMerge.duplicate_id == Document.id) & DocumentMerge.active.is_(True),
            )
            .where(func.coalesce(DocumentMerge.canonical_id, Document.id).in_(ids))
        )
    )
    public = set(members)
    mapping = canonical_ids(session, group)
    deadlines = session.scalars(
        select(DocumentDeadline)
        .where(
            DocumentDeadline.document_id.in_(group),
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
        if (
            d.status == "approved"
            and d.approved_date
            and key not in changed
            and d.document_id in public
        ):
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
    for row_field in fields:
        if row_field.approved_value in SUPPORT_LABELS:
            support.setdefault(
                mapping.get(row_field.document_id, row_field.document_id), set()
            ).add(row_field.approved_value)
    counts = _entry_counts(session, members)
    versions = v1.repost_versions(session, ids)
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
            len(versions[row.id]["others"]) if row.id in versions else 0,
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
                # 排序和分块按最近的那个截止；正文写窗口内全部已核对的截止（企业申报截止在前），
                # 免得只写最近的"推荐或审核截止"被当成企业截止。
                selected = min(upcoming, key=lambda d: (d.approved_date, d.id))
                columns["deadlines"].append((item, selected, tuple(upcoming)))
    # 发布栏目先按方案和统计周筛选，再用列表的主条目规则折叠；截止栏目保留各版本。
    for key in ("applications", "lists", "policies"):
        values = columns[key]
        kept = (
            set(
                session.scalars(
                    v1._grouped(
                        v1._base().where(Document.id.in_([item.id for item in values]))
                    ).with_only_columns(Document.id)
                )
            )
            if values
            else set()
        )
        columns[key] = [item for item in values if item.id in kept]
    return columns


def _one_line(value: str) -> str:
    return " ".join(_text(value).split())


def _public_text(value: str) -> str:
    return _one_line(URL_IN_TEXT.sub("", value))


def _month_day(value: date) -> str:
    return f"{value.month} 月 {value.day} 日"


def _full_date(value: date) -> str:
    return f"{value.year} 年 {value.month} 月 {value.day} 日"


def _span(start: date, end: date) -> str:
    """标题里的日期范围：同月写"10月13日—19日"，跨月写"9月28日—10月4日"（标题里不加空格）；
    跨年两头都写年份，免得在搜索结果、转发卡片里和往年同一周混淆。"""
    if start.year != end.year:
        return f"{start.year}年{start.month}月{start.day}日—{end.year}年{end.month}月{end.day}日"
    tail = f"{end.day}日" if start.month == end.month else f"{end.month}月{end.day}日"
    return f"{start.month}月{start.day}日—{tail}"


def _fit(*titles: str) -> str:
    """按顺序取第一个不超过 32 字的标题；都超了用最后一个（最短的），由管理台提示人工删减。"""
    for title in titles:
        if len(title) <= TITLE_LIMIT:
            return title
    return titles[-1]


def _span_short(start: date, end: date) -> str:
    """不带年份的日期范围：跨年的标题放不下时用。"""
    tail = (
        f"{end.day}日"
        if (start.year, start.month) == (end.year, end.month)
        else (f"{end.month}月{end.day}日")
    )
    return f"{start.month}月{start.day}日—{tail}"


def _span_text(start: date, end: date) -> str:
    """正文里的日期范围，写法同正文其他日期（数字两边留空格）。"""
    tail = (
        f"{end.day} 日" if (start.year, start.month) == (end.year, end.month) else (_month_day(end))
    )
    return f"{_month_day(start)}—{tail}"


def _week_text(start: date, end: date) -> str:
    """首行的统计周：同年只写一次年份，跨年两头都写。"""
    if start.year == end.year:
        return f"{start.year} 年 {_span_text(start, end)}"
    return f"{_full_date(start)}—{_full_date(end)}"


def scope(profile: DigestProfile) -> str:
    """标题里的范围：不限省份、不筛关键词的方案写"全国"，其余写方案名称（照旧由管理员起名）。

    限了范围的方案恰好叫"全国"时补"·限定范围"，免得标题和"本周周报"把它当成全国版
    （管理台也不让这样起名，这里兜底已有数据）。"""
    if not profile.provinces and not profile.keywords:
        return "全国"
    name = profile.name.strip()
    return f"{name}·限定范围" if name == "全国" else profile.name


def _province_name(province: str) -> str:
    return "国家部委" if province == NATIONAL else _public_text(province)


def _local_date(stamp: datetime | None) -> date | None:
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(BEIJING).date()


def _deadline_text(deadlines, *, linked=False) -> str:
    """已核对的截止：类型、日期、时刻和核对日期。

    如"企业申报截止 10 月 10 日 17:00（10 月 3 日核对）"。只收人工核对通过的截止（调用方保证）；
    没有就写"截止时间见原文"，不写候选日期。
    """
    if not deadlines:
        return "截止时间见原文"
    parts = []
    for deadline in deadlines:
        day = deadline.approved_date
        at = f" {deadline.approved_time:%H:%M}" if deadline.approved_time else ""
        kind = deadline.kind if deadline.kind != "其他" else "截止"
        reviewed = _local_date(deadline.reviewed_at)
        if reviewed is None:
            checked = "已核对"
        else:
            checked = f"{reviewed.isoformat()} 核对" if linked else f"{_month_day(reviewed)}核对"
        parts.append(f"{kind} {day.isoformat() if linked else _month_day(day)}{at}（{checked}）")
    return "；".join(parts)


class _Page:
    """公众号版（HTML）、纯文本版和带链接版逐行对应地写：HTML 每个段落对应纯文本一行。"""

    def __init__(self):
        self.html = [_SECTION]
        self.lines: list[str] = []
        self.linked: list[str] = []

    def blank(self):
        self.lines.append("")
        self.linked.append("")

    def paragraph(self, value: str, style: str = _PLAIN, linked: str | None = None):
        text = _public_text(value)
        self.html.append(f'<p style="{style}">{escape(text)}</p>')
        self.lines.append(text)
        self.linked.append(_one_line(value if linked is None else linked))

    def heading(self, value: str):
        text = _public_text(value)
        self.html.append(f'<h3 style="{_H3}">{escape(text)}</h3>')
        self.lines.append(text)
        self.linked.append(text)

    def entry(self, title: str, meta: list[str], linked: tuple[str, ...]):
        title_text = _public_text(title)
        meta_text = _public_text(" · ".join(part for part in meta if part))
        self.html.append(f'<p style="{_TITLE}"><strong>{escape(title_text)}</strong></p>')
        self.html.append(f'<p style="{_META}">{escape(meta_text)}</p>')
        self.lines.extend([title_text, meta_text])
        # 带链接版每条一行，字段间用全角竖线；字段里自带的竖线换成半角，列数固定。
        self.linked.append("｜".join(_one_line(value).replace("｜", "|") for value in linked))

    def footer(self, stamp: datetime, lines: list[str], *, deadlines: bool = True):
        local = stamp.astimezone(BEIJING)
        self.blank()
        for line in lines:
            self.paragraph(line, _NOTE)
        if deadlines:
            self.paragraph(LOCAL_EARLIER, _NOTE)
        self.paragraph(f"数据截至 {_full_date(local.date())} {local:%H:%M}（北京时间）。", _NOTE)
        self.paragraph(GUIDE, _NOTE)
        self.paragraph(CORRECTION, _NOTE)
        self.paragraph(FOOT, _NOTE)

    def done(self) -> tuple[str, str, str]:
        self.html.append("</section>")
        return "".join(self.html), "\n".join(self.lines).strip(), "\n".join(self.linked).strip()


def _new_versions(item: Item) -> str:
    return f"另有 {item.versions} 个转发版本" if item.versions else ""


def _published_text(item: Item, *, released=True) -> str:
    if not item.published:
        return "发布日期见原文"
    return f"{_month_day(item.published)}发布" if released else _month_day(item.published)


def _list_states(item: Item) -> str:
    return "/".join(state for state in ("公示", "公布") if state in item.title)


def _weekly_entry(page: _Page, section: str, item: Item, dues: tuple):
    """周报一条：加粗原标题，下一行小字写类别、发文机关、日期和已核对的截止。"""
    if section == "deadlines":
        meta = [_deadline_text(dues), item.issuer, _published_text(item)]
        if item.support:
            meta.append(f"支持方式：{'、'.join(item.support)}")
        meta.append(_new_versions(item))
    elif section == "applications":
        meta = ["新通知", item.issuer, _published_text(item), _deadline_text(item.deadlines)]
        if item.support:
            meta.append(f"支持方式：{'、'.join(item.support)}")
        meta.append(_new_versions(item))
    elif section == "lists":
        meta = ["名单", item.issuer, _published_text(item, released=False), _list_states(item)]
        if item.entries:
            meta.append(f"{item.entries} 条")
        meta.append(_new_versions(item))
    else:
        meta = ["政策文件", item.issuer, _published_text(item), f"文号：{item.number or '见原文'}"]
        meta.append(_new_versions(item))
    issuer = item.issuer + (f" · {_new_versions(item)}" if item.versions else "")
    page.entry(
        item.title,
        meta,
        (
            item.title,
            issuer,
            item.published.isoformat() if item.published else "发布日期见原文",
            _deadline_text(dues if section == "deadlines" else item.deadlines, linked=True),
            item.url,
        ),
    )


def _weekly_entries(profile: DigestProfile, columns: dict) -> tuple[dict, list[tuple]]:
    """一份文件只算一条：30 天内截止优先，其次本周新通知、名单、政策文件。"""
    enabled = {key: columns[key] if profile.columns.get(key, False) else [] for key in COLUMNS}
    entries: dict[int, tuple] = {}
    for item, _due, dues in sorted(
        enabled["deadlines"],
        key=lambda value: (
            value[1].approved_date,
            value[1].approved_time is None,
            value[1].approved_time or time.min,
            value[0].id,
        ),
    ):
        entries.setdefault(item.id, ("deadlines", item, dues))
    for key in ("applications", "lists", "policies"):
        for item in sorted(
            enabled[key], key=lambda item: (-(item.published or date.min).toordinal(), item.id)
        ):
            entries.setdefault(item.id, (key, item, ()))
    return enabled, list(entries.values())


def _blocks(entries: list[tuple]) -> tuple[list[tuple[str, list[tuple]]], list[tuple]]:
    """按省分块：国家部委排最前（全国性的政策文件另列），各省按本期条目多少排，一样多按固定省份顺序。"""
    blocks: dict[str, list[tuple]] = {}
    policies = []
    for entry in entries:
        section, item, _ = entry
        if item.province == NATIONAL and section == "policies":
            policies.append(entry)
        else:
            blocks.setdefault(item.province, []).append(entry)
    for values in blocks.values():
        values.sort(key=lambda entry: SECTION_ORDER.index(entry[0]))
    ordered = sorted(
        blocks.items(),
        key=lambda pair: (
            pair[0] != NATIONAL,
            -len(pair[1]),
            PROVINCE_ORDER.get(pair[0], len(PROVINCES)),
            pair[0],
        ),
    )
    return ordered, policies


def render(profile: DigestProfile, start: date, end: date, stamp: datetime, columns: dict):
    enabled, entries = _weekly_entries(profile, columns)
    counts = {key: len(values) for key, values in enabled.items()}
    if not entries:
        return None
    area = scope(profile)
    summary = [
        f"新通知{counts['applications']}条" if counts["applications"] else "",
        f"30天内截止{counts['deadlines']}项" if counts["deadlines"] else "",
    ]
    if not any(summary):
        summary = [
            f"名单{counts['lists']}份" if counts["lists"] else "",
            f"政策文件{counts['policies']}份" if counts["policies"] else "",
        ]
    # 标题里数字两边不留空格；按顺序能放几项放几项（不超过 32 字），一项也放不下就只写日期和范围
    title = f"申报周报｜{_span(start, end)}（{area}）"
    for n, part in enumerate([value for value in summary if value]):
        longer = f"{title}{'：' if n == 0 else '，'}{part}"
        if len(longer) > TITLE_LIMIT:
            break
        title = longer
    page = _Page()
    page.paragraph(f"{HEAD}统计周：{_week_text(start, end)}（按发布日期）。")
    labels = {
        "applications": f"新通知 {counts['applications']} 条",
        "deadlines": f"30 天内截止 {counts['deadlines']} 项（已核对）",
        "lists": f"名单 {counts['lists']} 份",
        "policies": f"政策文件 {counts['policies']} 份",
    }
    # 只写有条目的栏目：写"30 天内截止 0 项"容易被当成"没有要截止的"，其实只是还没核对。
    numbers = "、".join(labels[key] for key in COLUMNS if counts[key])
    page.paragraph(
        f"本期{'全国' if area == '全国' else ''}{numbers}。"
        "全部条目在小程序「本期清单」里，可以按省份看，点进详情复制原文链接。"
    )
    blocks, policies = _blocks(entries)
    shown = set()
    for province, values in blocks:
        name = _province_name(province)
        page.blank()
        # 块标题不写条数：和标题里的"新通知 X 条"放在一起容易被看成对不上
        # （块里还有截止、名单、政策）
        page.heading(name)
        for section, item, dues in values[:PER_BLOCK]:
            shown.add(item.id)
            _weekly_entry(page, section, item, dues)
        if len(values) > PER_BLOCK:
            page.paragraph(f"{name}还有 {len(values) - PER_BLOCK} 条，在本期清单里看。", _NOTE)
    if policies:
        page.blank()
        page.heading("全国性政策文件")
        for section, item, dues in policies[:POLICY_LIMIT]:
            shown.add(item.id)
            _weekly_entry(page, section, item, dues)
        if len(policies) > POLICY_LIMIT:
            page.paragraph(f"还有 {len(policies) - POLICY_LIMIT} 份，在本期清单里看。", _NOTE)
    notes = ["本期全部条目：小程序「本期清单」。", "看截止、查名单、搜通知：小程序「有据」。"]
    if profile.columns.get("deadlines", False):
        today = stamp.astimezone(BEIJING).date()
        notes.append(f"30 天内截止从 {_month_day(today)}算起，只收人工核对过的日期。")
    page.footer(stamp, notes)
    body_html, body_text, linked = page.done()
    # 本期清单按块的顺序存全部条目（正文每块只列 5 条）；全国性政策文件放最后。
    ordered = [entry for _, values in blocks for entry in values] + policies
    items = [
        {"id": item.id, "section": section, "province": item.province}
        for section, item, _ in ordered
    ]
    return Rendered(title, body_html, body_text, linked, counts, sorted(shown), items)


def _reminder_items(session: Session, profile: DigestProfile, stamp: datetime) -> list[Item]:
    """临期提醒：候选、核对和截止口径与小程序截止雷达相同（youju.api.v1），再套方案筛选。"""
    today = stamp.astimezone(BEIJING).date()
    stmt = v1._base().where(
        Document.id.in_(v1._future_approved_canonical_ids(session, today)),
        v1._kind_is(["申报通知"]),
        # 与周报和 issue_available 的公开范围一致：主文件本身来自一级来源。
        Source.rights_level == 1,
    )
    if profile.provinces:
        stmt = stmt.where(Source.province.in_([NATIONAL, *profile.provinces]))
    rows = [
        (doc, src)
        for doc, src in session.execute(stmt.order_by(Document.id)).all()
        if effective_kind(doc.title, doc.kind_override) == "申报通知"
        and safe_url(doc.url)
        and (not profile.keywords or any(word in doc.title for word in profile.keywords))
    ]
    ids = [doc.id for doc, _ in rows]
    approved = v1._approved(session, ids)
    details = v1._details(session, ids)
    items = []
    for doc, src in rows:
        due = v1.upcoming_deadline(approved.get(doc.id, []), stamp, REMINDER_DAYS)
        if due is None:
            continue
        detail = details.get(doc.id)
        issuer = detail.issuer if detail and detail.issuer else src.dept_full_name
        items.append(
            Item(
                doc.id,
                _text(doc.title),
                _text(issuer),
                src.province,
                doc.publish_date,
                "",
                doc.url,
                (due,),
            )
        )
    # 按截止日期升序；同一天写了时刻的在前，没写时刻的算到当天结束。
    items.sort(
        key=lambda item: (
            item.deadlines[0].approved_date,
            item.deadlines[0].approved_time is None,
            item.deadlines[0].approved_time or time.min,
            item.id,
        )
    )
    return items


def render_reminder(profile: DigestProfile, stamp: datetime, items: list[Item]) -> Rendered:
    """临期提醒正文；没有符合条件的项目时也生成一句话说明，不返回空（没有就不发，由人决定）。"""
    today = stamp.astimezone(BEIJING).date()
    end = today + timedelta(days=REMINDER_DAYS)
    area = scope(profile)
    tag = "已核对" if area == "全国" else f"{area}，已核对"
    # 跨年那一期写年份，放不下 32 字就去掉年份（正文首行写了完整日期）
    spans = (_span(today, end), _span_short(today, end))
    if items:
        title = _fit(*(f"临期提醒｜{span}截止的{len(items)}个申报项目（{tag}）" for span in spans))
    else:
        suffix = "" if area == "全国" else f"（{area}）"
        title = _fit(*(f"临期提醒｜{span}：没有已核对的截止项目{suffix}" for span in spans))
    page = _Page()
    page.paragraph(f"{HEAD}截止从 {_full_date(today)}算起，只列人工核对过的日期。")
    shown = items[:REMINDER_LIMIT]
    if items:
        if len(items) > REMINDER_LIMIT:
            page.paragraph(
                f"共 {len(items)} 项，这里按截止日期先后只列前 {REMINDER_LIMIT} 项，"
                "完整清单在小程序「截止」页。"
            )
        page.blank()
        page.heading(f"{_span_text(today, end)}截止的申报项目（{len(items)} 项）")
        for item in shown:
            due = item.deadlines[0]
            page.entry(
                item.title,
                [_province_name(item.province), item.issuer, _deadline_text((due,))],
                (item.title, item.issuer, _deadline_text((due,), linked=True), item.url),
            )
    else:
        page.paragraph(REMINDER_EMPTY)
    page.footer(stamp, ["完整清单：小程序「截止」页，可以按省份、部门筛。"])
    body_html, body_text, linked = page.done()
    return Rendered(
        title,
        body_html,
        body_text,
        linked,
        {"reminder": len(items)},
        sorted(item.id for item in shown),
        [{"id": item.id, "section": "deadlines", "province": item.province} for item in shown],
    )


def _flash_rows(session: Session, profile: DigestProfile):
    """名单速报能选的名单：一级来源、没有下架、类型是名单的主文件，再套方案的地区筛选。"""
    published = func.coalesce(DocumentDetail.publish_date, Document.publish_date)
    stmt = visible_documents(
        select(
            Document.id,
            Document.title,
            Document.url,
            published.label("published"),
            Source.province,
            _issuer_column(),
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(
            Source.rights_level == 1,
            available_condition(),
            or_(
                Document.kind_override == "名单",
                and_(Document.kind_override.is_(None), Document.rule_kind == "名单"),
            ),
        )
    )
    if profile.provinces:
        stmt = stmt.where(Source.province.in_([NATIONAL, *profile.provinces]))
    return stmt, published


def _flash_items(session: Session, profile: DigestProfile, rows) -> list[Item]:
    rows = [
        row
        for row in rows
        if safe_url(row.url)
        and (not profile.keywords or any(word in row.title for word in profile.keywords))
    ]
    ids = [row.id for row in rows]
    counts = _entry_counts(session, _public_members(session, ids)) if ids else {}
    versions = v1.repost_versions(session, ids) if ids else {}
    return [
        Item(
            row.id,
            _text(row.title),
            _text(row.issuer),
            row.province,
            row.published,
            "",
            row.url,
            entries=counts.get(row.id, 0),
            versions=len(versions[row.id]["others"]) if row.id in versions else 0,
        )
        for row in rows
    ]


# 列个人的或和企业申报无关的名单（招聘、职称、考试、专家、采购等）：人工改成了"名单"
# 也不写速报（D-018）
_FLASH_OFF = (
    *LIST_OFF_TOPIC,
    *PERSONAL_WORDS,
    *PERSONAL_QUALIFICATIONS,
    *PERSONAL_CONTEST,
    # 标题里的常见变体（Codex 第二轮）：评审专家、采购供应商、考试考生、人才和各类人员名单
    "专家",
    "采购",
    "供应商",
    "招标",
    "考试",
    "考生",
    "人才",
    "人员",
    "特派员",
    "教师",
    "医师",
    "学生",
    "毕业生",
    "党员",
    "干部",
)


def flash_target(session: Session, profile: DigestProfile, document_id: int) -> Item:
    """名单速报写的那份名单（副本按归并主文件算）；不公开、不是名单或不在方案范围内的不写。"""
    master = canonical_ids(session, [document_id]).get(document_id, document_id)
    stmt, _ = _flash_rows(session, profile)
    items = _flash_items(session, profile, session.execute(stmt.where(Document.id == master)).all())
    if not items:
        raise ValueError(
            "这份名单不存在、不是名单、已下架、来源不公开，或不在方案的地区和关键词范围内"
        )
    if any(word in items[0].title for word in _FLASH_OFF):
        raise ValueError(
            "这份名单列的是个人，或和企业申报无关（招聘、职称、考试、专家等），不写速报"
        )
    return items[0]


def flash_candidates(
    session: Session, profile: DigestProfile, stamp: datetime | None = None, days: int = 14
) -> list[dict]:
    """管理台选名单用：近 days 天发布的名单，按条数从多到少，带上已生成的速报。"""
    today = (stamp or now()).astimezone(BEIJING).date()
    stmt, published = _flash_rows(session, profile)
    rows = session.execute(
        stmt.where(published.between(today - timedelta(days=days), today)).order_by(
            published.desc(), Document.id.desc()
        )
    ).all()
    rows = [row for row in rows if not any(word in row.title for word in _FLASH_OFF)]
    items = sorted(
        _flash_items(session, profile, rows),
        key=lambda item: (-item.entries, -(item.published or date.min).toordinal(), item.id),
    )[:50]
    issues = {
        issue.subject_id: issue
        for issue in session.scalars(
            select(DigestIssue).where(
                DigestIssue.profile_id == profile.id,
                DigestIssue.kind == "list_flash",
                DigestIssue.subject_id.in_([item.id for item in items]),
            )
        )
    }
    return [
        {
            "id": item.id,
            "title": item.title,
            "issuer": item.issuer,
            "province": _province_name(item.province),
            "published": item.published,
            "entries": item.entries,
            "issue_id": issues[item.id].id if item.id in issues else None,
            "issue_status": issues[item.id].status if item.id in issues else None,
        }
        for item in items
    ]


def clean_period(value: str | None) -> str:
    """公示期由人照原文填写（系统里没有核对过的公示期字段）；去掉网址和多余空白。"""
    return _public_text(value or "")[:60]


def render_flash(profile: DigestProfile, stamp: datetime, item: Item, period: str) -> Rendered:
    """名单速报：名单标题、发文机关、公示期、条数、怎么在小程序里查一家。不列企业名称。"""
    status = list_status(item.title, "名单")
    # 照录原标题；括号里补地区（原标题常不写省份，读者按"省份 名单"搜得到）和条数
    area = _province_name(item.province)
    title = f"名单速报｜{_public_text(item.title)}（{area}" + (
        f"·共{item.entries}家）" if item.entries else "）"
    )
    page = _Page()
    page.paragraph(HEAD)
    page.blank()
    page.entry(
        item.title,
        [item.issuer, _published_text(item, released=False), status or ""],
        (
            item.title,
            item.issuer,
            item.published.isoformat() if item.published else "发布日期见原文",
            item.url,
        ),
    )
    if period:
        page.paragraph(f"公示期：{period}（照原文填写）")
    elif status == "公示":
        page.paragraph("公示期：见原文")
    page.paragraph(
        f"名单条数：{item.entries} 家（按名称去重，以原文为准）"
        if item.entries
        else "名单条数：见原文"
    )
    if item.versions:
        page.paragraph(f"另有 {item.versions} 个转发版本，内容以各自原文为准。")
    page.blank()
    page.heading("怎么查一家企业在不在名单上")
    page.paragraph(
        "打开小程序「查名单」，输入企业全称或统一社会信用代码，能看到它上过哪些公示、公布名单。"
    )
    page.paragraph("看这份名单的原文：在小程序里打开这份名单，点「复制原文链接」，用浏览器打开。")
    page.footer(stamp, [], deadlines=False)
    body_html, body_text, linked = page.done()
    return Rendered(
        title[:200],
        body_html,
        body_text,
        linked,
        {"entries": item.entries},
        [item.id],
        [{"id": item.id, "section": "lists", "province": item.province, "period": period}],
    )


def generate(
    session: Session,
    profile_id: int,
    week: date | None = None,
    stamp: datetime | None = None,
    kind: str = "weekly",
    *,
    only_missing: bool = False,
    subject_id: int = 0,
    period: str | None = None,
) -> Generation:
    """生成或覆盖草稿；only_missing 时本周已有同类稿件就不再生成（定时补跑用）。

    名单速报按 subject_id（名单文件编号）一份名单一篇；period 是人照原文填的公示期，
    不传时沿用上次生成时填的。
    """
    stamp = stamp or now()
    start, end = interval(kind, week, stamp)
    if kind == "list_flash" and subject_id <= 0:
        raise ValueError("请选择要写速报的名单")
    if kind != "list_flash":
        subject_id = 0
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
    if kind == "list_flash":
        # 一份名单一篇（副本按归并主文件算）：发布日期后来改过也找回原来那篇，不另建。
        subject_id = canonical_ids(session, [subject_id]).get(subject_id, subject_id)
        where = (DigestIssue.subject_id == subject_id,)
    else:
        where = (DigestIssue.week_start == start, DigestIssue.subject_id == 0)
    issue = session.scalar(
        select(DigestIssue)
        .where(DigestIssue.profile_id == profile.id, DigestIssue.kind == kind, *where)
        .order_by(DigestIssue.id)
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if issue and only_missing:
        session.commit()
        return Generation(issue, "exists", f"本周{KINDS[kind]}已生成，未重复生成")
    if issue and issue.status == "published":
        session.commit()
        return Generation(issue, "published", "已标为已发布，未覆盖")
    reason = "启用栏目没有符合公开范围、地区、关键词和统计区间的内容"
    if kind == "reminder":
        rendered = render_reminder(profile, stamp, _reminder_items(session, profile, stamp))
    elif kind == "list_flash":
        try:
            target = flash_target(session, profile, subject_id)
        except ValueError as exc:
            if issue is None:
                session.rollback()
                raise
            # 已有草稿的名单后来下架或不再公开：和周报一样删掉草稿，不留可复制的旧题录。
            rendered, reason = None, f"{exc}；原来的草稿已删除"
        else:
            if issue is None:
                start, end = _monday(target.published or stamp.astimezone(BEIJING).date())
            if period is None:
                period = issue.items[0].get("period", "") if issue and issue.items else ""
            rendered = render_flash(profile, stamp, target, clean_period(period))
    else:
        rendered = render(profile, start, end, stamp, _items(session, profile, start, end, stamp))
    if rendered is None:
        # 重新生成也不能留下旧草稿里的下架题录；无内容不创建空期刊。
        if issue:
            session.delete(issue)
        session.commit()
        log.info("周报未生成：方案 #%s，%s 至 %s；%s", profile.id, start, end, reason)
        return Generation(None, "empty", reason)
    if issue is None:
        issue = DigestIssue(
            profile_id=profile.id, kind=kind, subject_id=subject_id, week_start=start, week_end=end
        )
        session.add(issue)
    issue.title, issue.body_html, issue.body_text = (
        rendered.title,
        rendered.body_html,
        rendered.body_text,
    )
    issue.body_text_with_links = rendered.body_text_with_links
    issue.counts, issue.document_ids, issue.items = (
        rendered.counts,
        rendered.document_ids,
        rendered.items,
    )
    issue.generated_at = stamp
    # 记下生成时的范围："我的 → 本周周报"按它找全国版，方案后来改了地区也不影响已发布的期
    issue.scope = scope(profile)[:80]
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


def page_paths(issue: DigestIssue) -> list[dict]:
    """公众号编辑器里插入小程序链接时填的页面路径。本期清单要先标为已发布才对外打开。"""
    if issue.kind == "weekly":
        return [
            {"label": "本期清单", "path": f"{DIGEST_PAGE}?id={issue.id}", "published_only": True},
            {"label": "看截止", "path": "pages/report/index", "published_only": False},
            {"label": "查名单", "path": "pages/lists/index", "published_only": False},
            {"label": "搜通知", "path": "pages/home/index", "published_only": False},
        ]
    if issue.kind == "reminder":
        return [
            {
                "label": "截止页（7 天内，只看已核对）",
                "path": "pages/report/index?days=7&verified=1",
                "published_only": False,
            }
        ]
    return [
        {
            "label": "这份名单",
            "path": f"pages/detail/index?id={issue.subject_id}",
            "published_only": False,
        },
        {"label": "查名单", "path": "pages/lists/index", "published_only": False},
    ]
