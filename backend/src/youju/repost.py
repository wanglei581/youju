"""多级转载去重（D-055）：同一份部里通知的各地版本归成一个“转发组”。

三层关系见 docs/迭代/转载去重方案_v1.md 第四节：①同一文件（youju.dedup，document_merges）；
②同一上游通知的各地版本（本模块，repost_groups / repost_group_members）；③相关但不同（不处理）。

- 本模块只改 repost_* 两张表。截止日期不随转发组同步：各版本的截止各自核对、各自显示，
  dedup.sync_reviewed_twins、核对台的 _apply_review、_pending_shown_ids 只认 document_merges。
- 对外合并要人确认（D-055）：组员确认过（confirmed_at 有值）才对外按组折叠。
  YOUJU_DEDUPE_AUTO=true 时归组任务自动确认强证据组（确认人记“自动”）；疑似组一直要人确认；
  人工撤回、移出、拆过的组不再自动确认。
- 人工移出的版本（active=False）不会被下次归组自动加回；人工加入、拆出的不会被自动挪走。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, select
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

# 自动确认时记的确认人
AUTO_CONFIRMER = "自动（强证据）"
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
    """按第四节的规则把文件连成组（A–B、B–C 则 A、B、C 同组）。

    只比核心标题相同、或引用了同一个上游文号的文件，不两两全比。组里只要有一条边是疑似、
    去掉疑似边后连不起来，整组就算疑似。"""
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
    parent = {doc_id: doc_id for doc_id in by_id}
    strong_parent = dict(parent)
    for (left, right), strength in edges.items():
        parent[_find(parent, left)] = _find(parent, right)
        if strength == "strong":
            strong_parent[_find(strong_parent, left)] = _find(strong_parent, right)
    components: dict[int, list[int]] = defaultdict(list)
    for doc_id in sorted(by_id):
        components[_find(parent, doc_id)].append(doc_id)
    proposals = []
    for ids in components.values():
        if len(ids) < 2:
            continue
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
    return proposals


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
    auto_confirmed: int = 0


def rebuild(session: Session, *, auto: bool = False, now: datetime | None = None) -> RebuildSummary:
    """按规则增量更新转发组；不提交。

    - 规则提出的组和已有的组没有交集：新建一组（待确认）。
    - 和已有的组有交集：新出现的版本加入交集最多的那一组（待确认）；已在组里的不挪动。
    - 人工移出的（active=False）不加回；人工加入、拆出的不挪动。
    - 规则不再支持、还没确认的自动成员删掉（以后规则又支持了还能加回）；确认过的保留。
    - 文件后来归并成了别的文件的副本：成员身份转给主文件（主文件已有的就删掉副本这条）。
    - auto=True 时自动确认强证据组里还没确认的成员；人工撤回、移出、拆过的组除外。
    """
    stamp = now or datetime.now(UTC)
    summary = RebuildSummary()
    # 先锁组、再锁组员，和管理台的操作（api/repost.py 的 _lock_group）同一顺序，不会互相等成死锁
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
    # 成员文件后来成了副本：转给主文件
    mapping = {
        row.duplicate_id: row.canonical_id
        for row in session.scalars(
            select(DocumentMerge).where(
                DocumentMerge.active.is_(True), DocumentMerge.duplicate_id.in_(list(members))
            )
        )
    }
    for duplicate, canonical in mapping.items():
        row = members.pop(duplicate)
        if canonical in members:
            session.delete(row)
        else:
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
                removed_by=row.removed_by,
                removed_at=row.removed_at,
            )
            session.add(moved)
            members[canonical] = moved
        summary.moved += 1
    session.flush()

    proposals = propose_groups(load_candidates(session))
    summary.proposals = len(proposals)
    supported: dict[int, set[int]] = {}  # 文件 → 规则认为和它同组的文件
    for proposal in proposals:
        ids = set(proposal.ids)
        for doc_id in ids:
            supported[doc_id] = ids
        usable = [d for d in proposal.ids if not (d in members and not members[d].active)]
        existing = Counter(members[d].group_id for d in usable if d in members)
        new_ids = [d for d in usable if d not in members]
        if existing:
            target_id = sorted(existing.items(), key=lambda item: (-item[1], item[0]))[0][0]
            target = groups[target_id]
        elif len(usable) >= 2:
            target = RepostGroup(
                strength=proposal.strength,
                topic=proposal.topic,
                upstream_title=proposal.upstream_title,
                upstream_number=proposal.upstream_number,
                created_by="rule",
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(target)
            session.flush()
            groups[target.id] = target
            summary.created += 1
        else:
            continue
        if target.created_by == "rule":
            changed = (
                target.strength != proposal.strength
                or target.topic != proposal.topic
                or target.upstream_title != proposal.upstream_title
                or target.upstream_number != proposal.upstream_number
            )
            if changed:
                target.strength = proposal.strength
                target.topic = proposal.topic
                target.upstream_title = proposal.upstream_title
                target.upstream_number = proposal.upstream_number
                target.updated_at = stamp
        for doc_id in new_ids:
            row = RepostGroupMember(
                document_id=doc_id,
                group_id=target.id,
                active=True,
                joined_by="rule",
                evidence=proposal.evidence.get(doc_id, ""),
                joined_at=stamp,
            )
            session.add(row)
            members[doc_id] = row
            summary.joined += 1
        for doc_id in usable:
            row = members[doc_id]
            if row.joined_by == "rule" and row.group_id == target.id:
                evidence = proposal.evidence.get(doc_id, "")
                if row.evidence != evidence:
                    row.evidence = evidence
    session.flush()

    # 规则不再支持、还没确认的自动成员：删掉
    by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
    for row in members.values():
        by_group[row.group_id].append(row)
    for rows in by_group.values():
        active_ids = {row.document_id for row in rows if row.active}
        for row in rows:
            if not row.active or row.joined_by != "rule" or row.confirmed_at is not None:
                continue
            peers = supported.get(row.document_id, set()) & (active_ids - {row.document_id})
            if not peers:
                session.delete(row)
                members.pop(row.document_id, None)
                summary.dropped += 1
    session.flush()

    # 没有任何成员记录的组删掉（有人工移出记录的留着，移出的版本可以恢复）
    alive = {row.group_id for row in members.values()}
    for group_id, group in list(groups.items()):
        if group_id not in alive:
            session.delete(group)
            groups.pop(group_id)
    session.flush()

    if auto:
        active_by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
        for row in members.values():
            if row.active:
                active_by_group[row.group_id].append(row)
        for group_id, rows in active_by_group.items():
            group = groups[group_id]
            if group.strength != "strong" or group.no_auto or len(rows) < 2:
                continue
            for row in rows:
                if row.confirmed_at is None:
                    row.confirmed_by = AUTO_CONFIRMER
                    row.confirmed_at = stamp
                    summary.auto_confirmed += 1
        session.flush()
    return summary


# ---- 读取：对外折叠、管理台、核对台共用 --------------------------------------------


def confirmed_members() -> object:
    """对外按组折叠用的成员（子查询：document_id、group_id）：确认过、没有移出的。"""
    return (
        select(RepostGroupMember.document_id, RepostGroupMember.group_id)
        .where(RepostGroupMember.active.is_(True), RepostGroupMember.confirmed_at.is_not(None))
        .subquery()
    )


def active_groups(session: Session, doc_ids: list[int]) -> dict[int, tuple[int, list[int]]]:
    """这些文件所在的转发组（不论是否确认）：{文件: (组编号, 同组全部在组文件（含自己）)}。
    只返回至少 2 个在组成员的组。核对台“同组放在一起”用；对外折叠只认确认过的
    （见 confirmed_members）。"""
    if not doc_ids:
        return {}
    group_of = dict(
        session.execute(
            select(RepostGroupMember.document_id, RepostGroupMember.group_id).where(
                RepostGroupMember.document_id.in_(doc_ids), RepostGroupMember.active.is_(True)
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
