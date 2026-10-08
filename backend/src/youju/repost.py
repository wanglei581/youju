"""多级转载去重（D-055）：同一份部里通知的各地版本归成一个“转发组”。

三层关系见 docs/迭代/转载去重方案_v1.md 第四节：①同一文件（youju.dedup，document_merges）；
②同一上游通知的各地版本（本模块，repost_groups / repost_group_members）；③相关但不同（不处理）。

- 本模块只改 repost_* 两张表。截止日期不随转发组同步：各版本的截止各自核对、各自显示，
  dedup.sync_reviewed_twins、核对台的 _apply_review、_pending_shown_ids 只认 document_merges。
- 对外合并必须人工确认；确认后保存成员快照，内容或整组约束变化则需复核，停止对外折叠。
- 人工移出的版本（active=False）不会被下次归组自动加回；人工加入、拆出的不会被自动挪走；
  人工处理过的组保留；没有人工决定的待确认成员仍从头重算（见 rebuild、has_decision）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from youju.classify import GUIDE, effective_kind
from youju.models import (
    Document,
    DocumentDetail,
    DocumentMerge,
    RepostGroup,
    RepostGroupMember,
    Source,
)

# 发布相隔多少天以内才可能是同一份通知的各地版本
MAX_DAYS_APART = 90
# 核心标题至少几个字才拿来比
MIN_CORE_CHARS = 8
# 只看正文前多少字里引用的上游文号（“现将《……》（文号）转发给你们”一般在开头）
BODY_HEAD_CHARS = 1500
# 核心标题相近：两字词重合的比例（按较短一方算）
SIMILAR_RATIO = 0.6

# ---- 核心标题 ----------------------------------------------------------------

_PUNCT = re.compile(
    r"[\s“”\"'‘’《》〈〉「」『』()（）\[\]【】〔〕、，,。．.：:；;！!？?·•—\-–~～_/／]+"
)
_FORWARD = re.compile(r"转发|转载")
_SUFFIX = re.compile(r"(?:的)?(?:通知|公告|公示|通告|函)$")
# 结尾这些词去掉（可以连着几个，如“推荐工作”“申报指南”“相关事项”）
_TAIL_WORDS = (
    "有关事项",
    "相关事项",
    "有关工作",
    "相关工作",
    "工作",
    "申报指南",
    "指南",
    "申报",
    "推荐",
    "征集",
    "遴选",
    "组织",
    "的",
)
# 开头这些动词去掉（可以连着几个，如“继续做好”“组织开展”）
_HEAD_WORDS = (
    "继续",
    "进一步",
    "认真",
    "做好",
    "组织",
    "开展",
    "启动",
    "发布",
    "印发",
    "公开",
    "征集",
    "申报",
    "推荐",
    "遴选",
    "全区",
    "全省",
    "全市",
)
# 核心标题里写明是本地项目的，各地各一份，不归组
_LOCAL_WORDS = ("省级", "市级", "区级", "自治区级", "县级")


def _strip_words(text: str, words: tuple[str, ...], *, head: bool) -> str:
    changed = True
    while changed:
        changed = False
        for word in words:
            if head and text.startswith(word) and len(text) > len(word):
                text, changed = text[len(word) :], True
            elif not head and text.endswith(word) and len(text) > len(word):
                text, changed = text[: -len(word)], True
    return text


def core_title(title: str) -> str:
    """核心标题：去掉“转发”外壳、“关于”之前的机关名、“的通知”等、开头的“组织开展”等动词、
    结尾的“申报工作”“申报指南”等，再去掉标点；“年度”统一为“年”。"""
    text = re.sub(r"\s+", "", title or "")
    found = _FORWARD.search(text)
    if found:
        text = text[found.end() :]
    if "关于" in text:
        text = text[text.index("关于") + 2 :]
    text = _PUNCT.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = _SUFFIX.sub("", text)
        text = _strip_words(text, _TAIL_WORDS, head=False)
        text = _strip_words(text, _HEAD_WORDS, head=True)
    return text.replace("年度", "年")


def is_local_project(core: str) -> bool:
    return any(word in core for word in _LOCAL_WORDS)


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def similar_titles(a: str, b: str) -> bool:
    """核心标题相近：一方包含另一方，或两字词重合 60% 以上（按较短一方算）。"""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return False
    return len(left & right) / min(len(left), len(right)) >= SIMILAR_RATIO


# ---- 上游证据 ----------------------------------------------------------------

# 正文写明是转发
_BODY_FORWARD = re.compile(r"转发给你们|转发给各|予以转发|转发如下|现将.{0,120}?转发")
# 标题写明部委名称、国家科技计划
_MINISTRIES = re.compile(
    "工业和信息化部|工信部|科学技术部|科技部|国家发展改革委|国家发展和改革委员会|财政部|商务部|"
    "教育部|农业农村部|国家卫生健康委|生态环境部|人力资源社会保障部|人力资源和社会保障部|"
    "国家知识产权局|市场监管总局|国家市场监督管理总局|国家能源局|交通运输部|住房和城乡建设部|"
    "自然资源部|水利部|应急管理部|国务院国资委|国家国防科工局|国家数据局|国家税务总局|中国科协|"
    "国家重点研发计划|国家科技重大专项"
)
# 正文里引用的“《标题》（文号）”。文号里年份的括号各网站写法不一，统一成〔〕
_CITED = re.compile(
    r"《([^《》]{4,160})》\s*[（(]\s*"
    r"([一-龥A-Za-z]{1,16})\s*[〔\[［(（【]\s*(\d{4})\s*[〕\]］)）】]\s*(\d{1,5})\s*号\s*[）)]"
)
# 规则文件（管理办法、细则等）：各地依据同一个办法发的是各自的事，不能说明是同一份通知
_RULE_DOC = re.compile("办法|细则|规定|条例|意见|规划|方案|指引|标准|规范|规程|纲要|计划（")
_RULE_DOC_EXCEPT = re.compile("申报指南|征集|组织开展.*工作的通知")
# 省级机关文号开头的简称：引用省里自己的文件不算上游证据
_PROVINCE_PREFIX = tuple(
    "京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川蜀贵黔云滇藏陕秦甘陇青宁新内"
)


@dataclass(frozen=True)
class Cited:
    number: str
    title: str


def cited_upstream(body_head: str, publish_year: int | None) -> list[Cited]:
    """正文前 1500 字里引用的部里通知：只认“《……》（文号）”的写法；年份不早于发布年份的前一年；
    《》里不是办法、细则这类规则文件（标题写明申报指南、征集、组织开展……工作的通知的除外）；
    文号开头不是省份简称。"""
    found: list[Cited] = []
    for match in _CITED.finditer((body_head or "")[:BODY_HEAD_CHARS]):
        title, prefix, year, serial = match.groups()
        if prefix.startswith(_PROVINCE_PREFIX):
            continue
        if publish_year is not None and int(year) < publish_year - 1:
            continue
        if _RULE_DOC.search(title) and not _RULE_DOC_EXCEPT.search(title):
            continue
        cited = Cited(number=f"{prefix}〔{year}〕{int(serial)}号", title=title.strip())
        if cited.number not in {c.number for c in found}:
            found.append(cited)
    return found


# ---- 归组规则 ----------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """参与归组的一份文件（同一文件层的主文件）。"""

    id: int
    title: str
    kind: str
    province: str
    dept: str
    publish_date: date | None
    body_head: str = ""


@dataclass
class Signals:
    core: str
    local: bool
    cited: list[Cited]
    evidence: list[str]

    @property
    def upstream(self) -> bool:
        return bool(self.evidence)


def signals(doc: Candidate) -> Signals:
    year = doc.publish_date.year if doc.publish_date else None
    cited = cited_upstream(doc.body_head, year)
    evidence = []
    if _FORWARD.search(doc.title):
        evidence.append("标题转发")
    if _BODY_FORWARD.search((doc.body_head or "")[:BODY_HEAD_CHARS]):
        evidence.append("正文转发")
    if cited:
        evidence.append("上游文号")
    if _MINISTRIES.search(doc.title):
        evidence.append("标题含部委或国家计划")
    core = core_title(doc.title)
    return Signals(core=core, local=is_local_project(core), cited=cited, evidence=evidence)


def kind_bucket(kind: str) -> str:
    """比较类型时“资讯与指南”和“其他”算一类（其他类）。"""
    return "其他" if kind == GUIDE else kind


def pair_strength(a: Candidate, sa: Signals, b: Candidate, sb: Signals) -> str | None:
    """两份文件是不是同一份上游通知的各地版本：strong 强证据 / weak 疑似 / None 不归组。"""
    if (a.province, a.dept) == (b.province, b.dept):
        return None  # 同一机关的同名文件是不同阶段或不同批次，不是转发
    if a.publish_date is None or b.publish_date is None:
        return None
    if abs((a.publish_date - b.publish_date).days) > MAX_DAYS_APART:
        return None
    kind = kind_bucket(a.kind)
    if kind != kind_bucket(b.kind):
        return None
    if sa.local or sb.local:
        return None
    same_core = sa.core == sb.core and len(sa.core) >= MIN_CORE_CHARS
    if kind in ("名单", "政策"):
        # 名单、政策各地各一份；只有标题写明转发、原样转发同一份的才归组
        forwarded = "标题转发" in sa.evidence or "标题转发" in sb.evidence
        return "strong" if same_core and forwarded else None
    if kind not in ("申报通知", "其他"):
        return None
    if same_core and (sa.upstream or sb.upstream):
        return "strong"
    shared = {c.number for c in sa.cited} & {c.number for c in sb.cited}
    if shared and similar_titles(sa.core, sb.core):
        return "strong"
    if same_core:
        return "weak"
    return None


@dataclass
class Proposal:
    """归组规则提出的一组（至少 2 份）。"""

    ids: list[int]
    strength: str
    topic: str
    upstream_title: str | None
    upstream_number: str | None
    evidence: dict[int, str] = field(default_factory=dict)


def _find(parent: dict[int, int], x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def propose_groups(docs: list[Candidate]) -> list[Proposal]:
    """按规则提出的组（见 link）。"""
    return link(docs)[0]


def link(docs: list[Candidate]) -> tuple[list[Proposal], dict[int, set[int]]]:
    """按第四节的规则把文件连成组（A–B、B–C 则 A、B、C 同组）。

    只比核心标题相同、或引用了同一个上游文号的文件，不两两全比。组里只要有一条边是疑似、
    去掉疑似边后连不起来，整组就算疑似。连接时对全组检查机关和日期跨度（见下）。"""
    by_id = {doc.id: doc for doc in docs}
    sig = {doc.id: signals(doc) for doc in docs}
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for doc in docs:
        s = sig[doc.id]
        if len(s.core) >= MIN_CORE_CHARS:
            buckets[("core", s.core)].append(doc.id)
        for cited in s.cited:
            buckets[("cited", cited.number)].append(doc.id)
    edges: dict[tuple[int, int], str] = {}
    for ids in buckets.values():
        if len(ids) < 2:
            continue
        for i, left in enumerate(ids):
            for right in ids[i + 1 :]:
                key = (min(left, right), max(left, right))
                if edges.get(key) == "strong":
                    continue
                strength = pair_strength(by_id[left], sig[left], by_id[right], sig[right])
                if strength and (edges.get(key) is None or strength == "strong"):
                    edges[key] = strength

    # 连成组：强证据的边先连，同类边里发布日期近的先连。连之前检查整组的约束（传递连接也不能
    # 绕过两两之间的限制）：同一机关在组里只能有一份（同机关同名的是不同批次或阶段），整组发布
    # 日期跨度不超过 90 天。连上会违反约束的边不连，组就此拆开。
    def apart(edge: tuple[int, int]) -> int:
        left, right = by_id[edge[0]], by_id[edge[1]]
        return abs((left.publish_date - right.publish_date).days)

    parent = {doc_id: doc_id for doc_id in by_id}
    agencies = {doc_id: {(doc.province, doc.dept)} for doc_id, doc in by_id.items()}
    span = {doc_id: (doc.publish_date, doc.publish_date) for doc_id, doc in by_id.items()}
    for edge, _strength in sorted(
        edges.items(), key=lambda item: (item[1] != "strong", apart(item[0]), item[0])
    ):
        left, right = _find(parent, edge[0]), _find(parent, edge[1])
        if left == right or agencies[left] & agencies[right]:
            continue
        first = min(span[left][0], span[right][0])
        last = max(span[left][1], span[right][1])
        if (last - first).days > MAX_DAYS_APART:
            continue
        root, child = min(left, right), max(left, right)
        parent[child] = root
        agencies[root] |= agencies.pop(child)
        span[root] = (first, last)
        span.pop(child)
    components: dict[int, list[int]] = defaultdict(list)
    for doc_id in sorted(by_id):
        components[_find(parent, doc_id)].append(doc_id)
    proposals = []
    for ids in components.values():
        if len(ids) < 2:
            continue
        # 只用组内的强证据边能把全组连起来，才算强证据组
        inside = set(ids)
        strong_parent = {doc_id: doc_id for doc_id in ids}
        for (left, right), strength in edges.items():
            if strength == "strong" and left in inside and right in inside:
                strong_parent[_find(strong_parent, left)] = _find(strong_parent, right)
        strong = len({_find(strong_parent, doc_id) for doc_id in ids}) == 1
        cited = Counter(c.number for doc_id in ids for c in sig[doc_id].cited)
        number = None
        title = None
        if cited:
            number, count = cited.most_common(1)[0]
            # 至少两个版本都引用了这个文号才照录，免得把某一版顺带引用的其他文件当成上游
            if count >= 2:
                title = next(
                    c.title for doc_id in ids for c in sig[doc_id].cited if c.number == number
                )
            else:
                number = None
        topic = Counter(sig[doc_id].core for doc_id in ids).most_common(1)[0][0]
        proposals.append(
            Proposal(
                ids=ids,
                strength="strong" if strong else "weak",
                topic=topic[:200],
                upstream_title=title[:300] if title else None,
                upstream_number=number[:64] if number else None,
                evidence={doc_id: "、".join(sig[doc_id].evidence)[:200] for doc_id in ids},
            )
        )
    proposals.sort(key=lambda p: p.ids[0])
    # 两两之间有规则支持的文件（不管最后连没连进同一组）：判断已有组员是否还有规则支持时用
    neighbors: dict[int, set[int]] = defaultdict(set)
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    return proposals, neighbors


# ---- 数据库：取候选、增量建组 ----------------------------------------------------


def load_candidates(session: Session) -> list[Candidate]:
    """同一文件层的主文件（不含已归并的副本），没有下架、有发布日期的。"""
    from youju.takedown import available_condition

    duplicates = select(DocumentMerge.duplicate_id).where(DocumentMerge.active.is_(True))
    rows = session.execute(
        select(
            Document.id,
            Document.title,
            Document.kind_override,
            Document.publish_date,
            Source.province,
            Source.dept,
            func.left(DocumentDetail.body_text, BODY_HEAD_CHARS),
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(
            Document.publish_date.is_not(None),
            ~Document.id.in_(duplicates),
            available_condition(),
        )
        .order_by(Document.id)
    ).all()
    return [
        Candidate(
            id=doc_id,
            title=title,
            kind=effective_kind(title, override),
            province=province,
            dept=dept,
            publish_date=published,
            body_head=body or "",
        )
        for doc_id, title, override, published, province, dept, body in rows
    ]


@dataclass
class RebuildSummary:
    proposals: int = 0
    created: int = 0
    joined: int = 0
    dropped: int = 0
    moved: int = 0
    conflicts: int = 0
    skipped: int = 0
    needs_review: int = 0


def has_decision(row: RepostGroupMember) -> bool:
    """移出、确认、人工加入和拆组的成员保留；组上有决定不等于每个自动成员都被冻结。"""
    return not row.active or row.confirmed_at is not None or row.joined_by == "human"


def member_facts(session: Session, ids: list[int]) -> tuple[dict[int, tuple], dict[int, str]]:
    """归组约束与确认快照使用同一份当前数据，正文比较全部内容而非归组用的前 1500 字。"""
    info, fingerprints = {}, {}
    for doc, src, detail in session.execute(
        select(Document, Source, DocumentDetail)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(Document.id.in_(ids))
        .execution_options(populate_existing=True)
    ):
        info[doc.id] = ((src.province, src.dept), doc.publish_date)
        values = [
            doc.publish_date.isoformat() if doc.publish_date else None,
            src.province,
            src.dept,
            src.dept_full_name,
            detail.issuer if detail else None,
            detail.publish_date.isoformat() if detail and detail.publish_date else None,
            doc.title,
            hashlib.sha256((detail.body_text or "").encode()).hexdigest() if detail else None,
        ]
        fingerprints[doc.id] = hashlib.sha256(
            json.dumps(values, ensure_ascii=False).encode()
        ).hexdigest()
    return info, fingerprints


def valid_group(info: dict[int, tuple], ids: list[int]) -> bool:
    """整组必须有日期、同机关只有一份，日期跨度不超过 90 天。"""
    if any(i not in info or info[i][1] is None for i in ids):
        return False
    if len({info[i][0] for i in ids}) != len(ids):
        return False
    dates = [info[i][1] for i in ids]
    return not dates or (max(dates) - min(dates)).days <= MAX_DAYS_APART


def bump(group: RepostGroup, stamp: datetime) -> None:
    """组有变化（成员进出、确认状态、事项信息）：修订号加一。管理台确认时带着看到的修订号，
    对不上就不执行，免得确认了没看到的版本。"""
    group.revision = (group.revision or 0) + 1
    group.updated_at = stamp


def _fits(info: dict[int, tuple], ids: list[int], doc_id: int) -> bool:
    """把 doc_id 加进由 ids 组成的组后，仍满足同一机关只有一份、日期跨度不超过 90 天。"""
    return valid_group(info, [*ids, doc_id])


def rebuild(session: Session, *, now: datetime | None = None) -> RebuildSummary:
    """待确认自动成员从头重算；带人工决定的成员保留。确认组内容或约束变化需复核。

    规则提出的整组先检查约束；保留人工成员时，加入新成员也检查整组。更新最终差异，
    相同输入重跑不改变修订号。这里只写 repost_*，绝不改截止审批。
    """
    stamp = now or datetime.now(UTC)
    summary = RebuildSummary()
    # 先锁组、再锁组员，和管理台的操作（api/repost.py 的 _lock_groups）同一顺序，不会互相等成死锁
    groups = {
        row.id: row
        for row in session.scalars(
            select(RepostGroup)
            .order_by(RepostGroup.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    members = {
        row.document_id: row
        for row in session.scalars(
            select(RepostGroupMember)
            .order_by(RepostGroupMember.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    # 成员文件后来成了副本
    mapping = {
        row.duplicate_id: row.canonical_id
        for row in session.scalars(
            select(DocumentMerge).where(
                DocumentMerge.active.is_(True), DocumentMerge.duplicate_id.in_(list(members))
            )
        )
    }
    held: set[int] = set()  # 和主文件的记录冲突、等人处理的副本记录
    for duplicate, canonical in sorted(mapping.items()):
        row = members[duplicate]
        group = groups[row.group_id]
        if canonical in members:
            if has_decision(row):
                held.add(duplicate)
                summary.conflicts += 1
                continue
            members.pop(duplicate)
            session.delete(row)
            bump(group, stamp)
            summary.dropped += 1
            continue
        members.pop(duplicate)
        session.delete(row)
        session.flush()
        moved = RepostGroupMember(
            document_id=canonical,
            group_id=row.group_id,
            active=row.active,
            joined_by=row.joined_by,
            evidence=row.evidence,
            joined_at=row.joined_at,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
            confirmed_fingerprint=row.confirmed_fingerprint,
            removed_by=row.removed_by,
            removed_at=row.removed_at,
        )
        session.add(moved)
        members[canonical] = moved
        if row.confirmed_at is not None and not group.needs_review:
            group.needs_review = True
            summary.needs_review += 1
        bump(group, stamp)
        summary.moved += 1
    session.flush()

    candidates = load_candidates(session)
    info = {c.id: ((c.province, c.dept), c.publish_date) for c in candidates}
    # 只有已有成员需要完整正文快照，其余候选沿用上面的轻量数据。
    member_info, fingerprints = member_facts(session, list(members))
    info.update(member_info)
    by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
    for row in members.values():
        if row.active:
            by_group[row.group_id].append(row)
    for group_id, rows in by_group.items():
        group = groups[group_id]
        confirmed = [r for r in rows if r.confirmed_at is not None]
        changed = any(r.confirmed_fingerprint != fingerprints.get(r.document_id) for r in confirmed)
        if (
            confirmed
            and not group.needs_review
            and (changed or not valid_group(info, [r.document_id for r in rows]))
        ):
            group.needs_review = True
            bump(group, stamp)
            summary.needs_review += 1

    # 先在内存中释放所有未带人工决定的成员；旧归属仅用来尽量复用组编号，绝不约束重算结果。
    free = {d: r for d, r in members.items() if d not in held and not has_decision(r)}
    fixed = {d: r for d, r in members.items() if d not in free}
    active_of: dict[int, list[int]] = defaultdict(list)
    for row in fixed.values():
        if row.active and row.document_id not in held:
            active_of[row.group_id].append(row.document_id)
    proposals = propose_groups(
        [
            c
            for c in candidates
            if c.id not in held and not (c.id in fixed and not fixed[c.id].active)
        ]
    )
    summary.proposals = len(proposals)
    used: set[int] = set()
    desired: dict[int, tuple[int, str]] = {}
    for proposal in proposals:
        existing = Counter(fixed[d].group_id for d in proposal.ids if d in fixed)
        reusable = Counter(
            free[d].group_id
            for d in proposal.ids
            if d in free and free[d].group_id not in used and not active_of[free[d].group_id]
        )
        choices = existing or reusable
        if choices:
            target_id = sorted(choices, key=lambda g: (-choices[g], g))[0]
            target = groups[target_id]
        else:
            target = RepostGroup(
                strength=proposal.strength,
                topic=proposal.topic,
                upstream_title=proposal.upstream_title,
                upstream_number=proposal.upstream_number,
                created_by="rule",
                revision=0,
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(target)
            session.flush()
            groups[target.id] = target
            summary.created += 1
        used.add(target.id)
        if target.created_by == "rule" and not active_of[target.id]:
            values = (
                proposal.strength,
                proposal.topic,
                proposal.upstream_title,
                proposal.upstream_number,
            )
            if (
                target.strength,
                target.topic,
                target.upstream_title,
                target.upstream_number,
            ) != values:
                target.strength, target.topic, target.upstream_title, target.upstream_number = (
                    values
                )
                bump(target, stamp)
        for doc_id in proposal.ids:
            if doc_id in fixed:
                continue
            if not _fits(info, active_of[target.id], doc_id):
                summary.skipped += 1
                continue
            desired[doc_id] = (target.id, proposal.evidence.get(doc_id, ""))
            active_of[target.id].append(doc_id)

    for doc_id, row in free.items():
        if doc_id not in desired:
            session.delete(row)
            members.pop(doc_id)
            bump(groups[row.group_id], stamp)
            summary.dropped += 1
    for doc_id, (group_id, evidence) in desired.items():
        row = free.get(doc_id)
        if row is None:
            row = RepostGroupMember(
                document_id=doc_id,
                group_id=group_id,
                active=True,
                joined_by="rule",
                evidence=evidence,
                joined_at=stamp,
            )
            session.add(row)
            members[doc_id] = row
            bump(groups[group_id], stamp)
            summary.joined += 1
        else:
            old_group = row.group_id
            if old_group != group_id:
                row.group_id = group_id
                bump(groups[old_group], stamp)
                bump(groups[group_id], stamp)
                summary.moved += 1
            if row.evidence != evidence:
                row.evidence = evidence
                if old_group == group_id:
                    bump(groups[group_id], stamp)
    session.flush()
    alive = {row.group_id for row in members.values()}
    for group_id, group in groups.items():
        if group_id not in alive and not group.no_auto:
            session.delete(group)
    session.flush()
    return summary


def duplicate_members(session: Session, doc_ids: list[int]) -> dict[int, int]:
    """这些组员文件里后来成了同一文件副本的：{副本: 主文件}。"""
    if not doc_ids:
        return {}
    return dict(
        session.execute(
            select(DocumentMerge.duplicate_id, DocumentMerge.canonical_id).where(
                DocumentMerge.active.is_(True), DocumentMerge.duplicate_id.in_(doc_ids)
            )
        ).all()
    )


# ---- 读取：对外折叠、管理台、核对台共用 --------------------------------------------


def confirmed_members() -> object:
    """对外按组折叠用的成员（子查询：document_id、group_id）：确认过、没有移出的。"""
    return (
        select(RepostGroupMember.document_id, RepostGroupMember.group_id)
        .join(RepostGroup, RepostGroup.id == RepostGroupMember.group_id)
        .where(
            RepostGroupMember.active.is_(True),
            RepostGroupMember.confirmed_at.is_not(None),
            RepostGroup.needs_review.is_(False),
        )
        .subquery()
    )


def grouped_documents(stmt: Select) -> Select:
    """先筛选、再按转发组折叠（group=1）：命中筛选的版本里，同一确认过的转发组只留一条主条目。

    主条目：部里原件在库就用原件，否则用最早发布的版本（同一天按编号）；选了地区筛选时命中的
    只有该地区的版本，主条目自然是本地版本。主条目的标题、机关、日期、申报状态都是这一版自己的。
    排序、翻页和计数都按主条目（stmt 是 _base() 派生的 select(Document, Source)）。"""
    members = confirmed_members()
    key = func.coalesce(-members.c.group_id, Document.id)
    rank = (
        func.row_number()
        .over(
            partition_by=key,
            order_by=(
                case((Source.province == "国家", 0), else_=1),
                Document.publish_date.is_(None),
                Document.publish_date,
                Document.id,
            ),
        )
        .label("repost_rank")
    )
    ranked = (
        stmt.outerjoin(members, members.c.document_id == Document.id)
        .with_only_columns(Document.id.label("doc_id"), rank)
        .subquery()
    )
    return (
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .join(ranked, ranked.c.doc_id == Document.id)
        .where(ranked.c.repost_rank == 1)
    )


def active_groups(session: Session, doc_ids: list[int]) -> dict[int, tuple[int, list[int]]]:
    """这些文件所在的转发组（不论是否确认）：{文件: (组编号, 同组全部在组文件（含自己）)}。
    只返回至少 2 个在组成员的组。核对台“同组放在一起”用；对外折叠只认确认过的
    （见 confirmed_members）。"""
    if not doc_ids:
        return {}
    # 后来成了同一文件副本的组员不算（它的截止候选随主文件核对）
    not_duplicate = ~RepostGroupMember.document_id.in_(
        select(DocumentMerge.duplicate_id).where(DocumentMerge.active.is_(True))
    )
    group_of = dict(
        session.execute(
            select(RepostGroupMember.document_id, RepostGroupMember.group_id).where(
                RepostGroupMember.document_id.in_(doc_ids),
                RepostGroupMember.active.is_(True),
                not_duplicate,
            )
        ).all()
    )
    if not group_of:
        return {}
    by_group: dict[int, list[int]] = defaultdict(list)
    for doc_id, group_id in session.execute(
        select(RepostGroupMember.document_id, RepostGroupMember.group_id)
        .where(
            RepostGroupMember.group_id.in_(set(group_of.values())),
            RepostGroupMember.active.is_(True),
            not_duplicate,
        )
        .order_by(RepostGroupMember.document_id)
    ):
        by_group[group_id].append(doc_id)
    return {
        doc_id: (group_id, by_group[group_id])
        for doc_id, group_id in group_of.items()
        if len(by_group[group_id]) >= 2
    }


def version_label(province: str, dept: str, same_province: bool) -> str:
    """版本的叫法：国家级来源是“部里”；其余是“江西版”，同组里同省有两个部门时写“四川科技版”。"""
    if province == "国家":
        return "部里"
    return f"{province}{dept}版" if same_province else f"{province}版"


def labels_for(sources: dict[int, Source]) -> dict[int, str]:
    """同一组各版本的叫法（见 version_label）。"""
    provinces = Counter(src.province for src in sources.values())
    return {
        doc_id: version_label(src.province, src.dept, provinces[src.province] > 1)
        for doc_id, src in sources.items()
    }
