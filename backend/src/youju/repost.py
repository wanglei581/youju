"""多级转载去重（D-055）：同一份部里通知的各地版本归成一个“转发组”。

三层关系见 docs/迭代/转载去重方案_v1.md 第四节：①同一文件（youju.dedup，document_merges）；
②同一上游通知的各地版本（本模块，repost_groups / repost_group_members）；③相关但不同（不处理）。

- 正文前 1500 字信号存入 repost_signals，行本身也是文件已处理的标记；只重算 180 天窗口及窗口里的组。
- 本模块只改 repost_* 表。截止日期不随转发组同步：各版本的截止各自核对、各自显示，
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
from datetime import UTC, date, datetime, timedelta, timezone

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from youju.classify import GUIDE, effective_kind
from youju.models import (
    Document,
    DocumentDetail,
    DocumentMerge,
    RepostGroup,
    RepostGroupMember,
    RepostSignal,
    Source,
)

# 发布相隔多少天以内才可能是同一份通知的各地版本
MAX_DAYS_APART = 90
# 只算最近这么多天发布的文件（和窗口里的组的老成员）：两倍于发布相隔上限，窗口里最新的文件能连到
# 的都在里面（第二阶段计划"转发归组改增量"）
WINDOW_DAYS = 2 * MAX_DAYS_APART
# 同一个桶（核心标题相同、引用同一个文号）里，每份只和发布日期排在后面的这么多份比：县里大量转发
# 同一份部里通知时，桶里可能有几千份，不再两两全比；靠相邻的边照样能连成一组
BUCKET_NEIGHBORS = 200
# 窗口外确认过的组：正文、标题被直接改写（抓取时间不变）也要能发现，每轮轮流拿最久没核对的这么多份
# 全文核对，这么多天轮一遍
RECHECK_BATCH = 2000
RECHECK_DAYS = 30
_BEIJING = timezone(timedelta(hours=8))
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


# 文件自己的发文字号整句（详情 doc_number：页面元数据或正文头上的，见 crawler/detail.py）
_OWN_NUMBER = re.compile(r"([一-龥A-Za-z]{1,16})[〔\[［(（【](\d{4})[〕\]］)）】](\d{1,5})号")


def own_number(doc_number: str | None) -> str | None:
    """这份文件自己的发文字号（详情里页面元数据或正文头上的），按引用文号（cited_upstream）同一个
    写法统一：去掉空白，年份的括号一律写成〔〕，序号去掉前面的 0。整句不正好是一个文号的给 None。
    部里的原件不引用自己，归组靠它对上转发件引用的文号（D-129 ②，见 auto_reason）。"""
    match = _OWN_NUMBER.fullmatch("".join((doc_number or "").split()))
    if not match:
        return None
    prefix, year, serial = match.groups()
    return f"{prefix}〔{year}〕{int(serial)}号"


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
    city: str | None = None
    district: str | None = None
    # 存好的正文信号（repost_signals）；None 时照旧从 body_head 算
    cited: tuple[Cited, ...] | None = None
    body_forward: bool | None = None
    has_body: bool | None = None
    # 自己的发文字号（own_number 统一过的，存在 repost_signals 里）
    own_number: str | None = None

    @property
    def agency(self) -> tuple[str, str | None, str | None, str]:
        return (self.province, self.city, self.district, self.dept)


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
    cited = doc.cited if doc.cited is not None else cited_upstream(doc.body_head, year)
    evidence = []
    if _FORWARD.search(doc.title):
        evidence.append("标题转发")
    if (
        doc.body_forward
        if doc.body_forward is not None
        else bool(_BODY_FORWARD.search((doc.body_head or "")[:BODY_HEAD_CHARS]))
    ):
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
    if a.agency == b.agency:
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
        ids.sort(key=lambda doc_id: (by_id[doc_id].publish_date or date.min, doc_id))
        for i, left in enumerate(ids):
            for right in ids[i + 1 : i + 1 + BUCKET_NEIGHBORS]:
                if by_id[left].publish_date is None or by_id[right].publish_date is None:
                    continue
                if (by_id[right].publish_date - by_id[left].publish_date).days > MAX_DAYS_APART:
                    break
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
    agencies = {doc_id: {doc.agency} for doc_id, doc in by_id.items()}
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
    # 组内的强证据边（两端在同一组）先按组分好：每个组只看自己的边，不是每个组都把全部边看一遍
    # （全国铺开后窗口里有几千个组、几万条边）
    strong_edges: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for (left, right), strength in edges.items():
        if strength == "strong" and (root := _find(parent, left)) == _find(parent, right):
            strong_edges[root].append((left, right))
    proposals = []
    for root, ids in components.items():
        if len(ids) < 2:
            continue
        # 只用组内的强证据边能把全组连起来，才算强证据组
        strong_parent = {doc_id: doc_id for doc_id in ids}
        for left, right in strong_edges[root]:
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


# 正文信号一批读写多少份
_SIGNAL_BATCH = 2000


@dataclass
class SignalRefresh:
    """这一轮算正文信号的结果：算了几份；新打标记的文件里，发布日期在窗口里的最早一天（这些文件的
    伙伴可能在窗口外的老组里，候选范围要往前放 MAX_DAYS_APART 天，见 rebuild）。"""

    computed: int = 0
    earliest_new: date | None = None


def _fill_signal(
    row: RepostSignal, published: date | None, body: str | None, fetched, stamp, doc_number=None
) -> None:
    head = body or ""
    row.cited = [
        {"number": c.number, "title": c.title}
        for c in cited_upstream(head, published.year if published else None)
    ]
    row.body_forward = bool(_BODY_FORWARD.search(head))
    row.has_body = bool(head.strip())
    row.own_number = own_number(doc_number)
    # 记下这次用的详情时间（正文是空的也记）：详情没再重抓就不重算
    row.body_at = fetched
    row.computed_at = stamp


def refresh_signals(
    session: Session, *, today: date, stamp: datetime, full: bool = False
) -> SignalRefresh:
    """算正文信号存进 repost_signals（D-129 ②"归组改增量"）。分两步，都只碰该碰的文件：

    1. 还没有行的文件（不管发布日期、有没有正文、是否下架）都算一次、插一行，grouped_at = stamp：
       这一行也是"归组处理过"的标记，同步据此放行新文件。用反连接按编号找，不是按"最大编号"之后
       找：抓取事务提交的先后和编号的先后不一样，编号小、提交晚的文件也要找得到。
    2. 已经有行、会参加归组的（发布日期在归组范围里的，和范围里的组的成员——范围外的老成员也要，
       迁移补的行正文信号是空的），详情比上次算时新（fetched_at 晚于 body_at，或还没算过）的重算；
       full=True 时这些不看详情新不新，全部重算（维护用，比如正文脱敏重写之后）。归组范围和 rebuild
       的候选范围是同一个（candidate_cutoff）：第 1 步新进来的窗口内文件让范围往前放时，这一步也
       跟着放，放进来的老文件（迁移补的空行、离开窗口以后才抓到正文的）这一轮就有正文信号。
    正文只读前 BODY_HEAD_CHARS 字，按编号分批读写。"""
    window = today - timedelta(days=WINDOW_DAYS)
    result = SignalRefresh()
    last_id = 0
    while True:
        rows = session.execute(
            select(
                Document.id,
                Document.publish_date,
                func.left(DocumentDetail.body_text, BODY_HEAD_CHARS),
                DocumentDetail.fetched_at,
                DocumentDetail.doc_number,
            )
            .outerjoin(RepostSignal, RepostSignal.document_id == Document.id)
            .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
            .where(Document.id > last_id, RepostSignal.document_id.is_(None))
            .order_by(Document.id)
            .limit(_SIGNAL_BATCH)
        ).all()
        if not rows:
            break
        for doc_id, published, body, fetched, doc_number in rows:
            row = RepostSignal(document_id=doc_id, grouped_at=stamp)
            _fill_signal(row, published, body, fetched, stamp, doc_number)
            session.add(row)
            if (
                published is not None
                and published >= window
                and (result.earliest_new is None or published < result.earliest_new)
            ):
                result.earliest_new = published
        result.computed += len(rows)
        last_id = rows[-1][0]
        session.flush()

    cutoff = candidate_cutoff(today, result.earliest_new)
    changed = or_(RepostSignal.body_at.is_(None), DocumentDetail.fetched_at > RepostSignal.body_at)
    stale = select(RepostSignal.document_id).join(
        DocumentDetail, DocumentDetail.document_id == RepostSignal.document_id
    )
    if not full:
        stale = stale.where(changed)
    # 窗口里的文件（按发布日期的索引找），加上窗口组里早于窗口的老成员
    in_window = stale.join(Document, Document.id == RepostSignal.document_id).where(
        Document.publish_date >= cutoff
    )
    old_members = stale.where(
        RepostSignal.document_id.in_(
            select(RepostGroupMember.document_id).where(
                RepostGroupMember.group_id.in_(_window_groups(cutoff))
            )
        )
    )
    ids = sorted(set(session.scalars(in_window)) | set(session.scalars(old_members)))
    result.computed += recompute_signals(session, ids, stamp)
    return result


def recompute_signals(session: Session, ids: list[int], stamp: datetime) -> int:
    """按这几份文件现在的详情重算正文信号（有详情、已经有行的才算），按编号分批；返回算了几份。"""
    count = 0
    for i in range(0, len(ids), _SIGNAL_BATCH):
        batch = ids[i : i + _SIGNAL_BATCH]
        for row, published, body, fetched, doc_number in session.execute(
            select(
                RepostSignal,
                Document.publish_date,
                func.left(DocumentDetail.body_text, BODY_HEAD_CHARS),
                DocumentDetail.fetched_at,
                DocumentDetail.doc_number,
            )
            .join(Document, Document.id == RepostSignal.document_id)
            .join(DocumentDetail, DocumentDetail.document_id == RepostSignal.document_id)
            .where(RepostSignal.document_id.in_(batch))
        ):
            _fill_signal(row, published, body, fetched, stamp, doc_number)
            count += 1
        session.flush()
    return count


def candidate_cutoff(today: date, earliest_new: date | None) -> date:
    """归组范围的起点：窗口起点（今天往前 WINDOW_DAYS 天）；这一轮新进来、发布日期在窗口里的文件，
    伙伴可能在它前面 MAX_DAYS_APART 天里、已经在窗口外的老组里（新来源回填几个月前的历史），范围
    往前放到够得着它们。正文信号重算（refresh_signals 第 2 步）和归组候选（rebuild）都用它。"""
    cutoff = today - timedelta(days=WINDOW_DAYS)
    if earliest_new is not None:
        cutoff = min(cutoff, earliest_new - timedelta(days=MAX_DAYS_APART))
    return cutoff


def _window_groups(cutoff: date):
    """窗口里的组：至少有一个在组成员的发布日期不早于 cutoff（子查询）。"""
    return (
        select(RepostGroupMember.group_id)
        .join(Document, Document.id == RepostGroupMember.document_id)
        .where(RepostGroupMember.active.is_(True), Document.publish_date >= cutoff)
        .distinct()
    )


def load_candidates(
    session: Session, today: date | None = None, cutoff: date | None = None
) -> list[Candidate]:
    """参加归组的文件：发布日期不早于 cutoff（默认窗口起点：今天往前 WINDOW_DAYS 天）的主文件，加上
    窗口里的组的全部成员（不论日期）；不含已归并的副本、下架的、没有发布日期的。正文信号读存好的
    （repost_signals），不读正文。"""
    from youju.takedown import available_condition

    today = today or datetime.now(_BEIJING).date()
    cutoff = cutoff or today - timedelta(days=WINDOW_DAYS)
    duplicates = select(DocumentMerge.duplicate_id).where(DocumentMerge.active.is_(True))
    group_members = select(RepostGroupMember.document_id).where(
        RepostGroupMember.group_id.in_(_window_groups(cutoff))
    )
    rows = session.execute(
        select(
            Document.id,
            Document.title,
            Document.kind_override,
            Document.publish_date,
            Source.province,
            Source.city,
            Source.district,
            Source.dept,
            RepostSignal,
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(RepostSignal, RepostSignal.document_id == Document.id)
        .where(
            Document.publish_date.is_not(None),
            ~Document.id.in_(duplicates),
            available_condition(),
            or_(Document.publish_date >= cutoff, Document.id.in_(group_members)),
        )
        .order_by(Document.id)
    ).all()
    return [
        Candidate(
            id=doc_id,
            title=title,
            kind=effective_kind(title, override),
            province=province,
            city=city,
            district=district,
            dept=dept,
            publish_date=published,
            cited=tuple(Cited(**c) for c in signal.cited) if signal else (),
            body_forward=signal.body_forward if signal else False,
            has_body=signal.has_body if signal else False,
            own_number=signal.own_number if signal else None,
        )
        for doc_id, title, override, published, province, city, district, dept, signal in rows
    ]


@dataclass
class RebuildSummary:
    signals: int = 0
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
    if not ids:
        return info, fingerprints
    # 正文的指纹在库里算（SHA2 算的是 utf8mb4 存的字节，和 Python 按 UTF-8 编码算的一样），不把整份
    # 正文读进内存：窗口里的组成员可能很多
    body_hash = func.sha2(func.coalesce(DocumentDetail.body_text, ""), 256)
    for doc, src, detail_id, issuer, detail_date, body_sha in session.execute(
        select(
            Document,
            Source,
            DocumentDetail.document_id,
            DocumentDetail.issuer,
            DocumentDetail.publish_date,
            body_hash,
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(Document.id.in_(ids))
        .execution_options(populate_existing=True)
    ):
        detail = detail_id is not None
        info[doc.id] = ((src.province, src.city, src.district, src.dept), doc.publish_date)
        values = [
            doc.publish_date.isoformat() if doc.publish_date else None,
            src.province,
            src.dept,
            src.dept_full_name,
            issuer if detail else None,
            detail_date.isoformat() if detail and detail_date else None,
            doc.title,
            body_sha if detail else None,
        ]
        # 旧来源的确认快照保持原样，新增城市才加入快照以捕捉城市变更。
        if src.city or src.level == "市级":
            values.extend([src.level, src.city])
        if src.district or src.level == "区县级":
            values.append(src.district)
        fingerprints[doc.id] = hashlib.sha256(
            json.dumps(values, ensure_ascii=False).encode()
        ).hexdigest()
    return info, fingerprints


def member_info(session: Session, ids: list[int]) -> dict[int, tuple]:
    """组员的机关和发布日期（归组约束用）：只读文件和来源，不读正文。"""
    if not ids:
        return {}
    return {
        doc_id: ((province, city, district, dept), published)
        for doc_id, published, province, city, district, dept in session.execute(
            select(
                Document.id,
                Document.publish_date,
                Source.province,
                Source.city,
                Source.district,
                Source.dept,
            )
            .join(Source, Source.id == Document.source_id)
            .where(Document.id.in_(ids))
        )
    }


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


def rebuild(
    session: Session, *, now: datetime | None = None, today: date | None = None, full: bool = False
) -> RebuildSummary:
    """待确认自动成员从头重算；带人工决定的成员保留。确认组内容或约束变化需复核：窗口里的确认组
    每轮核对；窗口外的确认以后详情重抓过的拿全文核对一次，另外每轮轮流核对一批最久没核对的
    （RECHECK_DAYS 天轮一遍），full=True（--full）时全部核对。

    先保存正文信号和已处理标记；只重算窗口里的组（有在组成员的发布日期在窗口里），窗口外的组
    只在副本转主文件、确认成员详情重抓过、成员都没了时处理。窗口默认是最近 WINDOW_DAYS 天；
    这一轮新进来的窗口内文件，范围再往前放 MAX_DAYS_APART 天。
    规则提出的整组先检查约束；保留人工成员时，加入新成员也检查整组。更新最终差异，
    相同输入重跑不改变修订号。这里只写 repost_*，绝不改截止审批。
    """
    stamp = now or datetime.now(UTC)
    # 窗口按北京时间的日子算（政府网站和本程序的发布日期都是北京时间）
    today = today or stamp.astimezone(_BEIJING).date()
    refreshed = refresh_signals(session, today=today, stamp=stamp, full=full)
    summary = RebuildSummary(signals=refreshed.computed)
    # 候选范围：窗口起点，这一轮新进来的窗口内文件往前放（和上面重算正文信号的范围同一个）
    cutoff = candidate_cutoff(today, refreshed.earliest_new)
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

    window_groups = set(session.scalars(_window_groups(cutoff)))
    candidates = load_candidates(session, today, cutoff)
    info = {c.id: (c.agency, c.publish_date) for c in candidates}
    # 只有已有成员需要完整正文快照，其余候选沿用上面的轻量数据。
    # 确认以后详情重抓过、这次重抓还没核对过的确认成员（核对过、内容没变的记下核对的时间，详情
    # 没再重抓就不再拿全文核对，免得重抓过一次就每轮都算）：{文件编号: 详情时间}
    changed_confirmed = dict(
        session.execute(
            select(RepostGroupMember.document_id, DocumentDetail.fetched_at)
            .join(DocumentDetail, DocumentDetail.document_id == RepostGroupMember.document_id)
            .outerjoin(RepostSignal, RepostSignal.document_id == RepostGroupMember.document_id)
            .where(
                RepostGroupMember.active.is_(True),
                RepostGroupMember.confirmed_at.is_not(None),
                DocumentDetail.fetched_at > RepostGroupMember.confirmed_at,
                or_(
                    RepostSignal.checked_at.is_(None),
                    DocumentDetail.fetched_at > RepostSignal.checked_at,
                ),
            )
        ).all()
    )
    # 确认快照的全文指纹只给确认过的成员算：窗口里的确认组每轮都核对（D-055：确认以后任一版本的
    # 日期、机关、标题、正文有变，下一轮就停止折叠；正文被直接改写时抓取时间不变，只能重算才看得
    # 出），量受窗口限制；窗口外的只核对确认以后详情重抓过、还没核对过的；--full 时全部确认过的成员
    # （维护用：改了标题、正文脱敏重写这类不更新抓取时间的改动以后跑）。没确认的成员只要机关和日期
    # （归组约束用），不读正文
    window_ids = {d for d, row in members.items() if row.group_id in window_groups}
    # 窗口外确认过的组：正文、标题被直接改写（抓取时间不变）也要能发现——每轮轮流拿最久没核对的
    # RECHECK_BATCH 份全文核对，RECHECK_DAYS 天轮一遍
    periodic = set(
        session.scalars(
            select(RepostGroupMember.document_id)
            .outerjoin(RepostSignal, RepostSignal.document_id == RepostGroupMember.document_id)
            .where(
                RepostGroupMember.active.is_(True),
                RepostGroupMember.confirmed_at.is_not(None),
                ~RepostGroupMember.group_id.in_(_window_groups(cutoff)),
                func.coalesce(RepostSignal.checked_at, RepostGroupMember.confirmed_at)
                < stamp - timedelta(days=RECHECK_DAYS),
            )
            .order_by(
                func.coalesce(RepostSignal.checked_at, RepostGroupMember.confirmed_at),
                RepostGroupMember.document_id,
            )
            .limit(RECHECK_BATCH)
        )
    )
    fact_ids = (
        set(changed_confirmed)
        | periodic
        | {d for d in window_ids if members[d].active and members[d].confirmed_at is not None}
    )
    if full:
        fact_ids |= {d for d, row in members.items() if row.active and row.confirmed_at is not None}
    info.update(member_info(session, list(window_ids | fact_ids)))
    _, fingerprints = member_facts(session, list(fact_ids))
    by_group: dict[int, list[RepostGroupMember]] = defaultdict(list)
    for row in members.values():
        if row.active:
            by_group[row.group_id].append(row)
    for group_id, rows in by_group.items():
        group = groups[group_id]
        confirmed = [r for r in rows if r.confirmed_at is not None]
        changed = any(
            r.confirmed_fingerprint != fingerprints.get(r.document_id)
            for r in confirmed
            if r.document_id in fact_ids
        )
        if (
            confirmed
            and not group.needs_review
            and (
                changed
                or (
                    group_id in window_groups
                    and not valid_group(info, [r.document_id for r in rows])
                )
            )
        ):
            group.needs_review = True
            bump(group, stamp)
            summary.needs_review += 1
    # 重抓以后、或轮到定期核对的确认成员，核对过、内容没变的：记下核对的时间
    unchanged = [
        d
        for d in set(changed_confirmed) | periodic
        if d in members
        and members[d].confirmed_at is not None
        and members[d].confirmed_fingerprint == fingerprints.get(d)
    ]
    if unchanged:
        for signal in session.scalars(
            select(RepostSignal).where(RepostSignal.document_id.in_(unchanged))
        ):
            signal.checked_at = stamp

    # 先在内存中释放所有未带人工决定的成员；旧归属仅用来尽量复用组编号，绝不约束重算结果。
    # 窗口里的组：没有人工决定的成员（不论发布日期，早于窗口的老成员也一样）都从头重算；它们和同组
    # 成员都在候选里，合规的照样连回原组，不再合规的（比如和别的版本相隔超过 90 天）照规则去掉
    free = {
        d: r
        for d, r in members.items()
        if r.group_id in window_groups and d not in held and not has_decision(r)
    }
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
        # 空组（成员都去掉了、或都转给了主文件）删掉，不论在不在窗口里；人工处理过的照旧保留
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
    """版本的叫法：国家级来源是“部委”（给用户看的写法，口头说的“部里”）；其余是“江西版”，同组里同省有两个部门时写“四川科技版”。"""
    if province == "国家":
        return "部委"
    return f"{province}{dept}版" if same_province else f"{province}版"


def labels_for(sources: dict[int, Source]) -> dict[int, str]:
    """同一组各版本的叫法（见 version_label）。"""

    def place(src: Source) -> str:
        if src.level == "区县级" and src.district:
            return f"{src.city}·{src.district}"
        return src.city if src.level == "市级" and src.city else src.province

    provinces = Counter(place(src) for src in sources.values())
    return {
        doc_id: version_label(place(src), src.dept, provinces[place(src)] > 1)
        for doc_id, src in sources.items()
    }
