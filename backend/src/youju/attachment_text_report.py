"""附件读入第一步的实测统计（youju attachment-text-report，只读；《附件读入方案》第四节）。

按附件副本定时任务的真实候选范围算，申报通知（近 180 天）、政策（近 90 天）分开，只算一级公开来源、
没下架的：

- 附件：每种格式（按题录里的扩展名）能读的比例，取不出、下载不了的原因分布；
- 通知（合并转载的只算主条目）：有附件的、至少一个附件能读的、没有正文但附件能读的，
  正文加能读的附件超过 2.4 万字的（其中正文本身不超、加上附件才超的）；
- 抽样（默认 30 份申报通知，从至少一个附件能读、按现在的标题规则也是申报通知（AI 会解读）的里
  随机抽，种子固定可复现）：截止、条件、材料、评分四项按关键词粗判是只写在附件里、正文里也有、
  只在正文里还是都没有；--worksheet 写出每份的命中原句，供人逐份核对（粗判只是起点，结论以人
  看过的为准）。
"""

from __future__ import annotations

import random
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from youju import attachment_text
from youju.attachments import (
    APPLICATION_WINDOW_DAYS,
    DOWNLOAD_TYPES,
    _government_url,
    _recent,
    _since,
)
from youju.classify import effective_kind
from youju.dedup import canonical_ids
from youju.models import (
    AttachmentTextUrl,
    AttachmentTextVersion,
    Document,
    DocumentAttachment,
    Source,
)
from youju.takedown import available_condition

KINDS = ("申报通知", "政策")
# 四项的关键词（粗判用，不保证没有漏、没有错）
ASPECTS = {
    "截止": re.compile(
        r"截止|截至|申报时间|受理时间|报送时间|申报期限|提交时间|起止时间"
        r"|\d{1,2}\s*月\s*\d{1,2}\s*日[^。；;\n]{0,12}(?:前|之前|止)"
    ),
    "条件": re.compile(
        r"申报条件|申请条件|申报要求|基本条件|资格条件|准入条件|应当具备|须具备|需具备|应具备"
        r"|符合以下|满足以下|不得申报|不予受理|限项"
    ),
    "材料": re.compile(
        r"申报材料|申请材料|提交材料|报送材料|材料清单|佐证材料|证明材料|附件材料"
        r"|需提交|须提交|应提交|需提供|须提供|应提供"
    ),
    "评分": re.compile(r"评分|分值|打分|计分|评审标准|评价标准|评审指标|评价指标|权重"),
}


@dataclass
class KindReport:
    kind: str
    days: int
    # 附件（按链接去重）：扩展名 → 状态 → 个数
    attachments: dict = field(default_factory=dict)
    # 能读的附件里，实际格式和扩展名不同的（wps 实际是 doc 这类）：(扩展名, 实际格式) → 个数
    actual_types: dict = field(default_factory=dict)
    # 能读、但有扫描页的 PDF 个数，其中一半以上的页是扫描页的个数
    partly_scanned: int = 0
    mostly_scanned: int = 0
    notices: int = 0
    with_attachments: int = 0
    with_readable: int = 0
    with_body: int = 0
    no_body_but_readable: int = 0
    body_too_long: int = 0
    over_limit: int = 0
    over_only_with_attachments: int = 0
    # 库里存的类型（下载范围按它）是这一类、按现在的标题规则（AI 解读按它）却不是的份数
    stale_kind: int = 0
    # 正文加能读的附件合计字数的分布（份数）
    total_chars: dict = field(default_factory=dict)


@dataclass
class SampleItem:
    doc_id: int
    title: str
    url: str
    body_chars: int
    attachments: list[dict]
    # 四项各自：only_attachments / both / body_only / none
    verdicts: dict
    # 四项各自的命中原句（正文、附件），写进 worksheet 供人核对
    hits: dict


@dataclass
class Report:
    now: str
    kinds: list[KindReport]
    sample: list[SampleItem]
    sample_from: int
    seed: int


def _scope(session: Session, now: datetime):
    """附件副本的候选范围（attachments.candidates 同样的条件），按文件。"""
    since, since_at = _since(now)
    apply_since, apply_since_at = _since(now, APPLICATION_WINDOW_DAYS)
    kind = func.coalesce(Document.kind_override, Document.rule_kind)
    return kind, and_(
        Source.rights_level == 1,
        available_condition(),
        or_(
            and_(kind == "政策", _recent(since, since_at)),
            and_(kind == "申报通知", _recent(apply_since, apply_since_at)),
        ),
    )


def _url_states(session: Session) -> dict[str, tuple[str, AttachmentTextVersion | None]]:
    versions = {v.id: v for v in session.scalars(select(AttachmentTextVersion))}
    out = {}
    for row in session.scalars(select(AttachmentTextUrl)):
        if row.status == "pending":
            state = "pending"
        else:
            state = "ok" if row.status == "ok" else row.reason or "unavailable"
        out[row.url_hash] = (state, versions.get(row.version_id) if row.version_id else None)
    return out


def _bucket(chars: int) -> str:
    for limit, label in ((6000, "6000 字以内"), (12000, "6001–12000"), (24000, "12001–24000")):
        if chars <= limit:
            return label
    return "24000 字以上"


def build(session: Session, now: datetime, *, sample: int = 30, seed: int = 20261020) -> Report:
    from youju.ai import interpret
    from youju.api import v1
    from youju.crawler.detail import redact_sensitive

    kind_col, condition = _scope(session, now)
    states = _url_states(session)
    reports = {k: KindReport(k, APPLICATION_WINDOW_DAYS if k == "申报通知" else 90) for k in KINDS}
    # 附件：按链接去重，同一链接只算一次（算在先出现的那类里）
    seen: set[str] = set()
    counts: dict[str, dict[str, Counter]] = {k: defaultdict(Counter) for k in KINDS}
    actual: dict[str, Counter] = {k: Counter() for k in KINDS}
    rows = session.execute(
        select(kind_col, DocumentAttachment.url, DocumentAttachment.extension)
        .join(Document, Document.id == DocumentAttachment.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(condition)
        .order_by(Document.publish_date.desc(), Document.id.desc(), DocumentAttachment.position)
    ).all()
    for kind, url, ext in rows:
        if url in seen:
            continue
        seen.add(url)
        ext = (ext or "").lower() or "无"
        if ext not in DOWNLOAD_TYPES:
            state, version = "format", None
        elif not _government_url(url):
            state, version = "unavailable", None
        else:
            state, version = states.get(attachment_text.url_key(url), ("pending", None))
        counts[kind][ext][state] += 1
        report = reports[kind]
        if state == "ok" and version is not None:
            if version.file_type and version.file_type != ext:
                actual[kind][f"{ext}→{version.file_type}"] += 1
            if version.scanned_pages:
                report.partly_scanned += 1
                if version.pages and version.scanned_pages * 2 > version.pages:
                    report.mostly_scanned += 1
    for kind in KINDS:
        reports[kind].attachments = {ext: dict(c) for ext, c in sorted(counts[kind].items())}
        reports[kind].actual_types = dict(actual[kind])

    # 通知：只算主条目（合并转载的副本不重复算）
    docs = session.execute(
        select(Document.id, kind_col, Document.title, Document.url, Document.kind_override)
        .join(Source, Source.id == Document.source_id)
        .where(condition)
        .order_by(Document.id)
    ).all()
    merged = canonical_ids(session, [d.id for d in docs])
    sampled_from: list[tuple] = []
    totals: dict[str, Counter] = {k: Counter() for k in KINDS}
    for doc_id, kind, title, url, override in docs:
        if doc_id in merged:
            continue
        report = reports[kind]
        report.notices += 1
        current = effective_kind(title, override)
        report.stale_kind += current != kind
        # 管理统计按历史采集范围盘点正文是否存在，包含类型规则已变的旧条目。
        # 公开阅读和 AI 仍使用 _reading_body 默认的类型限制，不能随统计放宽。
        body = redact_sensitive(v1._reading_body(session, doc_id, allowed_kinds=None) or "")
        has_body = bool(body.strip())
        report.with_body += has_body
        if len(body) > interpret.MAX_BODY:
            report.body_too_long += 1
        items = attachment_text.readings(session, doc_id, now)
        readable = {r.version_id: r for r in items if r.state == "ok" and r.version_id}
        report.with_attachments += bool(items)
        report.with_readable += bool(readable)
        if not has_body and readable:
            report.no_body_but_readable += 1
        total = len(body) + sum(r.chars for r in readable.values())
        if items:
            totals[kind][_bucket(total)] += 1
        if total > interpret.MAX_BODY:
            report.over_limit += 1
            if len(body) <= interpret.MAX_BODY:
                report.over_only_with_attachments += 1
        # 抽样只从 AI 会解读的申报通知里抽（按现在的标题规则算的类型）
        if current == "申报通知" and readable:
            sampled_from.append((doc_id, title, url, body, items))
    for kind in KINDS:
        reports[kind].total_chars = dict(totals[kind])

    picked = random.Random(seed).sample(sampled_from, min(sample, len(sampled_from)))
    items = [_sample_item(session, *entry) for entry in sorted(picked, key=lambda e: e[0])]
    return Report(now.isoformat(), list(reports.values()), items, len(sampled_from), seed)


def _hits(pattern: re.Pattern, texts: list[tuple[str, str]], limit: int = 4) -> list[str]:
    out = []
    for where, text in texts:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            out.append(f"{where}：{text[start : match.end() + 60]}".replace("\n", " "))
            break
        if len(out) >= limit:
            break
    return out


def _sample_item(session, doc_id, title, url, body, items) -> SampleItem:
    body_parts = [("正文", line) for line in body.split("\n") if line.strip()]
    attachment_parts = []
    listed = []
    for number, reading in enumerate(items, 1):
        listed.append(
            {"no": number, "name": reading.name[:60], "ext": reading.ext, "state": reading.state}
        )
        if reading.state != "ok" or not reading.version_id:
            continue
        for seg in attachment_text.segments(session, reading.version_id):
            attachment_parts.append((f"附件{number} 第{seg.n}段", seg.text))
    verdicts, hits = {}, {}
    for aspect, pattern in ASPECTS.items():
        in_body = _hits(pattern, body_parts)
        in_attachments = _hits(pattern, attachment_parts)
        if in_attachments and not in_body:
            verdicts[aspect] = "only_attachments"
        elif in_attachments:
            verdicts[aspect] = "both"
        elif in_body:
            verdicts[aspect] = "body_only"
        else:
            verdicts[aspect] = "none"
        hits[aspect] = {"body": in_body, "attachments": in_attachments}
    return SampleItem(doc_id, title, url, len(body), listed, verdicts, hits)


# ---- 输出 ----

_VERDICTS = {
    "only_attachments": "只在附件里",
    "both": "正文、附件都有",
    "body_only": "只在正文里",
    "none": "都没找到",
}


def render(report: Report) -> str:
    lines = [f"附件文字实测（按 {report.now[:10]} 的候选范围；一级公开来源、没下架）"]
    for kr in report.kinds:
        lines.append("")
        lines.append(f"【{kr.kind}（近 {kr.days} 天）】")
        total = Counter()
        for states in kr.attachments.values():
            total.update(states)
        all_count, ok = sum(total.values()), total["ok"]
        lines.append(f"附件 {all_count} 个（按链接去重），能读 {ok} 个（{_pct(ok, all_count)}）")
        for ext, states in kr.attachments.items():
            count = sum(states.values())
            others = "，".join(
                f"{attachment_text.STATES.get(s, s)} {n}"
                for s, n in sorted(states.items(), key=lambda x: -x[1])
                if s != "ok"
            )
            ok = states.get("ok", 0)
            lines.append(
                f"  {ext}：{count} 个，能读 {ok}（{_pct(ok, count)}）"
                + (f"；{others}" if others else "")
            )
        reasons = Counter({s: n for s, n in total.items() if s != "ok"})
        if reasons:
            lines.append(
                "  读不了的原因："
                + "，".join(
                    f"{attachment_text.STATES.get(s, s)} {n}" for s, n in reasons.most_common()
                )
            )
        if kr.actual_types:
            lines.append(
                "  实际格式和扩展名不同（能读的）："
                + "，".join(f"{k} {n}" for k, n in sorted(kr.actual_types.items()))
            )
        if kr.partly_scanned:
            lines.append(
                f"  能读的 PDF 里有扫描页的 {kr.partly_scanned} 个（其中一半以上的页是扫描页的 "
                f"{kr.mostly_scanned} 个），扫描页的字没取到"
            )
        lines.append(
            f"通知 {kr.notices} 份：有附件 {kr.with_attachments}，"
            f"至少一个附件能读 {kr.with_readable}，有正文 {kr.with_body}，"
            f"没有正文但附件能读 {kr.no_body_but_readable}"
        )
        if kr.stale_kind:
            lines.append(
                f"  其中 {kr.stale_kind} 份库里存的类型是{kr.kind}（下载按它），按现在的标题规则"
                "却不是（AI 解读按它）：要重新跑一次类型"
            )
        lines.append(
            f"  正文加能读的附件合计超过 2.4 万字 {kr.over_limit} 份（所有通知里）："
            f"正文本身就超过的 {kr.body_too_long} 份（不论有没有附件，照旧整份不解读），"
            f"正文不超、加上附件才超的 {kr.over_only_with_attachments} 份"
        )
        if kr.total_chars:
            order = ("6000 字以内", "6001–12000", "12001–24000", "24000 字以上")
            lines.append(
                f"  有附件的 {kr.with_attachments} 份，正文加能读的附件合计："
                + "，".join(f"{k} {kr.total_chars.get(k, 0)} 份" for k in order)
            )
    if report.sample:
        lines.append("")
        lines.append(
            f"【抽样】从 {report.sample_from} 份至少一个附件能读的申报通知（按现在的标题规则）里"
            f"随机抽 {len(report.sample)} 份（种子 {report.seed}），按关键词粗判（要逐份核对）："
        )
        for aspect in ASPECTS:
            tally = Counter(item.verdicts[aspect] for item in report.sample)
            lines.append(
                f"  {aspect}：" + "，".join(f"{_VERDICTS[v]} {tally.get(v, 0)}" for v in _VERDICTS)
            )
    return "\n".join(lines)


def _pct(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.0f}%" if whole else "-"


def worksheet(report: Report) -> str:
    """抽样核对表（Markdown）：每份通知的附件和四项的命中原句，人看过后在"核对"一栏写结论。"""
    out = [
        "# 附件读入实测：抽样核对表",
        "",
        f"候选范围按 {report.now[:10]} 算；从 {report.sample_from} 份至少一个附件能读的申报通知里"
        f"随机抽 {len(report.sample)} 份（种子 {report.seed}）。"
        "粗判按关键词，只是起点；核对时看原句，判断这一项是不是只写在附件里。",
    ]
    for item in report.sample:
        out += ["", f"## {item.doc_id} {item.title}", "", f"- 原文：{item.url}"]
        out.append(f"- 正文 {item.body_chars} 字")
        for att in item.attachments:
            state = attachment_text.STATES.get(att["state"], att["state"])
            out.append(f"- 附件{att['no']}《{att['name']}》（{att['ext']}）：{state}")
        for aspect, verdict in item.verdicts.items():
            out += ["", f"**{aspect}**：粗判 {_VERDICTS[verdict]}；核对：", ""]
            for where in ("body", "attachments"):
                for line in item.hits[aspect][where]:
                    out.append(f"> {line[:200]}")
                    out.append("")
    return "\n".join(out) + "\n"


def as_dict(report: Report) -> dict:
    return asdict(report)
