"""交给模型的内容（D-079）：解读、对照、以后的问答都从这里取同一份。

依据：问一问开发设计（#328）第十一节。这一步（PR-1）只取正文：

- check：这份能不能做。列表的可用性预算（eligibility）、详情、提交任务共用，原来的
  interpret.eligible_body 搬到这里，判断和原因代码一字不变；
- load：取内容。正文照原来的规则分段（段号 P001 起），段号、段落、正文指纹和原来一模一样，
  已有的解读、对照结果和缓存不会因为这次改动作废；附件一律记"没读（off）"，读附件的开关和取舍
  由附件读入第二步加；
- locate：逐字核对。原来 interpret.validate 里"找原句、扩成整句、挡脱敏"那一段搬到这里，
  只在这一句所属的来源里找（现在只有正文一个来源，和原来完全一样）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.ai import interpret
from youju.classify import effective_kind
from youju.dedup import group_ids
from youju.models import Document, DocumentAttachment, DocumentDetail, Source
from youju.takedown import available_condition

CONFIG_BODY = "body-v1"  # 不读附件
CONFIG_ATTACH = "attach-v1"  # 读附件（附件读入第二步用；取舍规则改了就升版本）

BODY = "body"  # 正文来源的 key
BEIJING = ZoneInfo("Asia/Shanghai")  # "附件取于"按北京时间的日期写
# 读的时候挑中的是内容一样的另一个附件（替代，问一问开发设计第六节第 3 条"只更新显示"②④）：原句
# 出处、读的范围里"读了""没读完"的附件名称后面都接这一句。不说原来那份为什么不用（打不开、不在
# 列表里、现在读不到都是这一句，Codex 第 63 轮、复核 #406）
SUBSTITUTE = "（回答时读的是另一份内容相同的附件）"


@dataclass(frozen=True)
class AttachmentRef:
    """一个附件现在的样子（Content.attachments、SourceText.attachments 用；问一问开发设计第十一节，
    Codex 第 61 轮）。读的时候拼出处、挑"站内阅读"打开哪个、判断回答还对不对得上都看它（第六节
    第 3 条"只更新显示"⓪到⑤）。"""

    id: int  # 在详情页附件列表里的编号（document_attachments.id）
    n: int  # "附件 N"：现在在详情页附件列表里的位置（从 1 起，每次取内容时现算）
    name: str  # 现在的名称
    # 现在的内容版本（写法同 SourceText.version 的附件那种）；还没取到、取不出文字的为 None：
    # 不知道，不算变了也不算没变
    version: str | None
    readable: bool  # 现在能不能在站内打开（同站内阅读的判断）
    checked_at: datetime | None = None  # 最后一次从原网站核对的时间（同内容的附件各有各的）

    @property
    def label(self) -> str:
        return f"附件 {self.n}《{self.name}》"


@dataclass(frozen=True)
class SourceText:
    """一个来源：正文，或者（以后）一份附件内容。逐字核对只在来源的 text 里找。

    内容一样的几个附件只读一次、只交给模型一次，是同一个来源，都记在 attachments 里（附件读入方案
    的"附件 → 内容版本 → 文字"，Codex 第 39 轮）。出处名称（label）、"站内阅读"入口、"附件取于"
    的日期用同一个附件（shown_id）：现在能站内打开的第一个，一个都打不开就是第一个（Codex 第 40、
    61 轮：名称用一个、入口开另一个，用户点出处会进到另一份）。这几样都由 attachments 现算，对不上
    的写法写不出来。"""

    key: str  # "body"；附件 "att-<内容 SHA-256 前 12 位>"：只随内容变，不随位置、名称、链接变
    version: str  # 正文：原文指纹（同原来的 body_hash）；附件："<内容 SHA-256>:<取文字的规则版本>"
    text: str  # 遮过号码的全文
    # Content.attachments 里现在的版本等于这份内容的那几个，按现在在详情页附件列表里的位置排；
    # 正文为空
    attachments: tuple[AttachmentRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(sorted(self.attachments, key=lambda a: a.n)))

    @property
    def attachment_ids(self) -> tuple[int, ...]:
        return tuple(a.id for a in self.attachments)

    @property
    def shown(self) -> AttachmentRef | None:
        """出处名称和"站内阅读"入口用的那个附件；正文为 None。"""
        return next((a for a in self.attachments if a.readable), None) or next(
            iter(self.attachments), None
        )

    @property
    def shown_id(self) -> int | None:
        shown = self.shown
        return shown.id if shown is not None else None

    @property
    def readable(self) -> bool:
        """shown_id 那一个现在能不能在站内打开；正文为 True（附件来源一个附件都没有的为 False）。"""
        shown = self.shown
        return self.key == BODY if shown is None else shown.readable

    @property
    def label(self) -> str:
        """出处名称：正文写"正文"；附件是 shown_id 那一个的"附件 2《申报书》"。"""
        shown = self.shown
        if shown is not None:
            return shown.label
        return "正文" if self.key == BODY else ""

    @property
    def checked_at(self) -> datetime | None:
        """shown_id 那一个最后一次从原网站核对的时间（"附件取于"用，日期和名称是同一个附件）；
        正文为 None。"""
        shown = self.shown
        return shown.checked_at if shown is not None else None


@dataclass(frozen=True)
class Segment:
    """交给模型的一段。"""

    sid: str  # 正文 "P001"（和原来一样）；附件 "A<那 12 位>-<n>"
    source: str  # SourceText.key
    text: str
    start: int  # 在来源 text 里的位置
    end: int
    place: str = ""  # 给用户看的位置（附件的页、表、行）；正文为空
    kind: str = "p"  # 正文段 "p"；附件 "p"、"head"、"row"
    cells: tuple[tuple[str, str], ...] | None = None  # 表格行：（表头，格里的字）


@dataclass(frozen=True)
class Unread:
    """没读的附件，或只读了一部分的来源。"""

    label: str  # "附件 2《……》"
    reason: str  # off、scanned、format、too_long、old、pending、failed
    part: str | None = None  # 只读了一部分时写没读的那部分；整个没读为 None
    # 附件在详情页附件列表里的编号（DocumentAttachment.id）：按编号对、不按顺序对，
    # 改名、换顺序也对得上；只读了一部分的正文为 None（问一问开发设计第十一节；Codex 第 8 轮）
    attachment_id: int | None = None
    # 只读了一部分时它属于哪一份内容（SourceText.key）：是附件的，"没读完"的名称和入口用读的时候
    # 给这份内容挑出来的那个附件；是正文的照正文写；整个没读的为 None（Codex 复核 #406）
    source: str | None = None


@dataclass(frozen=True)
class Located:
    """逐字核对通过的一句。"""

    source: str  # 来源 key
    sid: str  # 这句开头所在的段
    start: int  # 扩成整句后在来源 text 里的位置
    end: int
    quote: str  # 原句（来源里的原样文字）
    place: str  # 出处（正文为空）


@dataclass(frozen=True)
class AskMatch:
    """问答逐字核对的结果（locate_ask）。"""

    kind: str  # shown 能显示；redacted 原句都含已脱敏的内容；not_found；ambiguous；too_short
    located: Located | None = None  # shown 时那一句
    # redacted 时其中含已脱敏内容的那几句（来源, 起, 止）：调用方按句去重、记个数
    masked: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class Texts:
    """逐字核对用的来源和段落。可以来自刚取的内容，也可以按任务快照里存的段落重建。"""

    sources: tuple[SourceText, ...]
    segments: tuple[Segment, ...]

    @classmethod
    def from_paragraphs(cls, paragraphs: list[dict]) -> Texts:
        """按快照里存的正文段落重建（原来 validate 的做法：段与段之间的空隙补换行）。"""
        body = interpret.paragraph_body(paragraphs)
        return cls(
            sources=(SourceText(key=BODY, version="", text=body),),
            segments=tuple(
                Segment(sid=p["pid"], source=BODY, text=p["text"], start=p["start"], end=p["end"])
                for p in paragraphs
            ),
        )

    @cached_property
    def _index(self) -> dict[str, Any]:
        # 一份内容核对很多句：每个来源的规范化全文、句子区间，每段的规范化文字只算一次
        sources = {
            s.key: (s, *interpret.normalize(s.text), interpret.sentence_spans(s.text))
            for s in self.sources
        }
        segments = {s.sid: (s, *interpret.normalize(s.text)) for s in self.segments}
        by_source: dict[str, list[Segment]] = {}
        for s in self.segments:
            by_source.setdefault(s.source, []).append(s)
        return {"sources": sources, "segments": segments, "by_source": by_source}


@dataclass(frozen=True)
class Content:
    """一份通知交给模型的内容。"""

    doc_id: int
    kind: str
    document_ids: list[int]  # 合并组，同原来的 prepare
    body_id: int | None  # 用的是组里哪一份的正文，同原来的 prepare
    texts: Texts
    unread: list[Unread] = field(default_factory=list)
    partial: bool = False  # 有段落因为超出字数没读（读附件以后才会有）
    attachments_total: int = 0  # 详情页列出的附件一共几个（含没读的）
    config: str = CONFIG_BODY
    # 现在详情页附件列表里的所有附件（读了的、没读的），按位置排：读的时候按编号查"还在不在列表里、
    # 现在是什么版本、能不能站内打开"（第六节第 3 条"只更新显示"⓪到⑤）。只读正文时（附件读入第二步
    # 以前）没有附件来源，用不到，为空
    attachments: tuple[AttachmentRef, ...] = ()

    @property
    def sources(self) -> tuple[SourceText, ...]:
        return self.texts.sources

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self.texts.segments

    def source(self, key: str) -> SourceText | None:
        return next((s for s in self.sources if s.key == key), None)

    def paragraphs(self) -> list[dict]:
        """正文的段落，写进任务快照（和原来 interpret.split_paragraphs 给的一模一样）。"""
        return [
            {"pid": s.sid, "text": s.text, "start": s.start, "end": s.end}
            for s in self.segments
            if s.source == BODY
        ]

    def scope_text(self, picked: dict[str, tuple[AttachmentRef, bool]] | None = None) -> str:
        """读的范围，给用户看。附件数按详情页列出的（本份通知自己的附件）。

        读附件时（CONFIG_ATTACH）每份附件内容写一个附件的名称，后面接"（附件取于 10 月 18 日）"，
        名称和日期是同一个附件。picked：读已有回答时，按那条回答存的 sourceAttachments 给每份附件
        内容挑好的附件（{来源 key: (附件, 是不是替代)}，ask._pick，和原句出处是同一个；同一份现在的
        内容，不同回答当时读的附件可能不一样，各按各的，Codex 复核 #406），替代的名称后面接
        SUBSTITUTE；出回答那一刻不传，用 shown_id 那一个。没读的原因怎么写由附件读入第二步定
        （D-085 的 excluded 还没有说法），在那之前不给用户看。"""
        if self.config == CONFIG_BODY:
            if not self.attachments_total:
                return "读了：正文（这份通知没有附件）"
            return "只读了正文，附件没读"
        read = "、".join(_read_label(s, (picked or {}).get(s.key)) for s in self.sources)
        if not self.unread:
            return f"读了：{read}"
        unread = "、".join(unread_label(u, picked) for u in self.unread)
        return f"读了：{read}；没读：{unread}"

    def versions(self) -> dict:
        """追问和读回答时核对内容变没变用。只放内容，不放附件编号：同内容的附件加一个、减一个
        不算内容变了（Codex 第 63 轮）。"""
        return {
            "config": self.config,
            "sources": {s.key: s.version for s in self.sources},
            "segments": interpret.digest([(s.sid, s.text) for s in self.segments]),
        }

    def source_attachments(self) -> dict[str, list[int]]:
        """{附件来源的 key: 这份内容这时对应的附件编号}：跟回答一起存成 sourceAttachments（读了
        没引用的、没找到的回答里也有），读的时候判断"记下的附件内容换了没有"（第六节第 3 条
        "只更新显示"⓪，Codex 第 62、63 轮）。不进版本摘要。"""
        return {s.key: list(s.attachment_ids) for s in self.sources if s.attachments}


def unread_label(unread: Unread, picked: dict[str, tuple[AttachmentRef, bool]] | None) -> str:
    """没读的写法：整个没读的写它自己的名称；只读了一部分的接上没读的那部分（"第 41 段以后"），
    是附件的用 picked 里给这份内容挑出来的那个附件的名称（替代的接 SUBSTITUTE，和"读了"里的是
    同一个；复核 #406），是正文的、没给 picked 的照它自己的名称。"""
    if not unread.part:
        return unread.label
    chosen = (picked or {}).get(unread.source) if unread.source != BODY else None
    if chosen is None:
        return f"{unread.label}{unread.part}"
    attachment, replaced = chosen
    return f"{attachment.label}{SUBSTITUTE if replaced else ''}{unread.part}"


def _read_label(source: SourceText, picked: tuple[AttachmentRef, bool] | None) -> str:
    """读的范围里一个来源的写法：正文写"正文"；附件写挑中的那个附件的名称（替代的接 SUBSTITUTE），
    有核对时间的再接"（附件取于 M 月 D 日）"（北京时间）。"""
    attachment, substitute = picked if picked is not None else (source.shown, False)
    if attachment is None:
        return source.label
    text = attachment.label + (SUBSTITUTE if substitute else "")
    if attachment.checked_at is None:
        return text
    day = attachment.checked_at
    day = (day if day.tzinfo else day.replace(tzinfo=UTC)).astimezone(BEIJING)
    return f"{text}（附件取于 {day.month} 月 {day.day} 日）"


def _check(session: Session, doc_id: int, *, link: bool = True) -> tuple[Document, str, str, str]:
    """能不能做，能做时给（通知、类型、库里的原文、遮过号码的正文）。

    link=False 不看原网页撤下了没有：打开已有的结果、处理提交时还没撤下的排队任务（prepare），
    以及存进 ai_eligibility 的预先计算（撤下的读表时另看，见 eligibility.effective_reason）。"""
    # 延迟引入 v1，避免 API 路由装载时形成循环依赖。
    from youju.api import v1

    doc = session.scalar(select(Document).where(Document.id == doc_id, available_condition()))
    if doc is None:
        raise interpret.InterpretRefusal("文件不存在或已下架，无法解读", "unavailable")
    kind = effective_kind(doc.title, doc.kind_override)
    if kind not in v1.BODY_KINDS:
        raise interpret.InterpretRefusal(
            "名单类及其他非政策、非申报通知文件不提供解读", "list" if kind == "名单" else "kind"
        )
    if doc.source.rights_level != 1:
        raise interpret.InterpretRefusal("文件不是一级公开来源，无法解读", "source")
    # 原文链接已失效（youju/link_check.py 隔天两次确认；原网页可能撤下了，也可能改了地址，分不清）：
    # 通知可能已撤回或更正，为免误导不再开始新的解读、对照。排在"这类文件不解读"之后、正文的原因
    # 之前（eligibility.effective_reason 同序）
    if link and doc.link_gone_at is not None:
        raise interpret.InterpretRefusal(
            "原文链接已失效，原网页可能已撤下或改了地址，暂不提供 AI 解读和条款对照", "link_gone"
        )
    raw = v1._reading_body(session, doc.id)
    # 不删除网页杂项、不 strip，段落偏移始终对应数据库内的完整脱敏原文。
    body = v1.redact_sensitive(raw or "")
    if not body.strip():
        raise interpret.InterpretRefusal("没有可公开的完整正文，请查看官方原文", "no_body")
    if len(body) > interpret.MAX_BODY:
        raise interpret.InterpretRefusal("正文超过 24000 字，请查看官方原文", "too_long")
    return doc, kind, raw, body


def check(session: Session, doc_id: int, *, link: bool = True) -> None:
    """能不能做：列表（ai_eligibility）、详情、提交解读/对照共用。

    不能做时抛 InterpretRefusal(code)。"""
    _check(session, doc_id, link=link)


def _body_id(session: Session, doc: Document, ids: list[int], raw: str) -> int | None:
    return session.scalar(
        select(DocumentDetail.document_id)
        .join(Document, Document.id == DocumentDetail.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentDetail.document_id.in_(ids),
            DocumentDetail.body_text == raw,
            DocumentDetail.status == "ok",
            Source.rights_level == 1,
            available_condition(),
        )
        .order_by((DocumentDetail.document_id == doc.id).desc(), DocumentDetail.document_id)
        .limit(1)
    )


def load(session: Session, doc_id: int, *, link: bool = True) -> Content:
    """交给模型之前调：先 check，再取正文、编段号。这一步不读附件，详情页列出的附件都记 off。"""
    doc, kind, raw, body = _check(session, doc_id, link=link)
    ids = group_ids(session, doc.id)
    attachments = list(
        session.scalars(
            select(DocumentAttachment)
            .where(DocumentAttachment.document_id == doc.id)
            .order_by(DocumentAttachment.position, DocumentAttachment.id)
        )
    )
    paragraphs = interpret.split_paragraphs(body)
    return Content(
        doc_id=doc.id,
        kind=kind,
        document_ids=ids,
        body_id=_body_id(session, doc, ids, raw),
        texts=Texts(
            sources=(SourceText(key=BODY, version=interpret.digest(raw), text=body),),
            segments=tuple(
                Segment(sid=p["pid"], source=BODY, text=p["text"], start=p["start"], end=p["end"])
                for p in paragraphs
            ),
        ),
        unread=[
            Unread(label=f"附件 {i}《{a.name}》", reason="off", attachment_id=a.id)
            for i, a in enumerate(attachments, 1)
        ],
        attachments_total=len(attachments),
    )


def locate(texts: Texts | Content, sid: Any, quote: str) -> tuple[Located | None, str | None]:
    """逐字核对：引用规范化（空白并成一个空格、全角转半角）以后，先在 sid 那一段里找；找不到，
    在这一段所属的来源里找唯一一处（不跨来源）。sid 对不上任何一段时在正文里找。找到以后扩成
    完整的句子。

    给 (Located, None)；对不上给 (None, "not_found")，有不止一处给 "ambiguous"，原句里有已脱敏的
    内容给 "redacted"。引用太短（少于 6 个字）由调用方先挡。"""
    texts = texts.texts if isinstance(texts, Content) else texts
    index = texts._index
    wanted = interpret.normalize(quote.strip())[0]
    segment = index["segments"].get(sid) if isinstance(sid, str) else None
    at = -1
    if segment is not None:
        segment, normalized, offsets = segment
        at = normalized.find(wanted)
    if at >= 0:
        key = segment.source
        first = segment.start + offsets[at][0]
        last = segment.start + offsets[at + len(wanted) - 1][1]
    else:
        key = segment.source if segment is not None else BODY
        if key not in index["sources"]:
            return None, "not_found"
        _, whole, whole_offsets, _ = index["sources"][key]
        at = whole.find(wanted)
        if at < 0:
            return None, "not_found"
        # 从下一字符继续查，重叠的命中也计为多次。
        if whole.find(wanted, at + 1) >= 0:
            return None, "ambiguous"
        first, last = whole_offsets[at][0], whole_offsets[at + len(wanted) - 1][1]
        # 命中的开头落在段外（以后附件段与段之间若有别的字）时当作对不上，不抛异常；
        # 正文段与段之间只有换行，命中的开头一定在段里，和原来一样
        segment = next(
            (s for s in index["by_source"].get(key, ()) if s.start <= first < s.end), None
        )
        if segment is None:
            return None, "not_found"
    source, _, _, sentences = index["sources"][key]
    # 句界在整个来源上算，超长句即使被硬切分段仍按同一句回填。
    covered = [(a, b) for a, b in sentences if a < last and b > first]
    if not covered:
        return None, "not_found"
    start, end = covered[0][0], covered[-1][1]
    original = source.text[start:end]
    if "已脱敏]" in original:
        return None, "redacted"
    return Located(key, segment.sid, start, end, original, segment.place), None


def _find_all(index: dict[str, Any], segment: Segment, wanted: str) -> list[tuple]:
    """wanted 在这一段里出现的每一处（重叠的也算）：（这一段, 在来源 text 里的起, 止）。"""
    _, normalized, offsets = index["segments"][segment.sid]
    found = []
    at = normalized.find(wanted)
    while at >= 0:
        first = segment.start + offsets[at][0]
        found.append((segment, first, segment.start + offsets[at + len(wanted) - 1][1]))
        at = normalized.find(wanted, at + 1)
    return found


def locate_ask(texts: Texts | Content, sid: Any, quote: Any, *, shortest: int = 6) -> AskMatch:
    """问答的逐字核对（问一问开发设计第六节第 3 条"在哪找""每句怎么判"、第十一节，Codex 第 40 轮）。
    和 locate 共用索引和扩句，不同的三处（解读、对照照旧用 locate）：

    ① sid 必须是交给模型的某一段，对不上直接 not_found，不到正文里找；
    ② 先这一段、再同一来源里交给模型的其他段，找出所有出现的地方（不跨来源，没交给模型的段不找），
       各扩成完整的原句，扩出来一样的算一处；出处按实际找到的那一段；
    ③ 不先挡太短的：每一处扩出来的原句都含已脱敏的内容 → redacted（带上其中含已脱敏内容的那几句）；
       不止一处、又不全含 → ambiguous（不猜是哪句）；只有一处、不含：规范化以后少于 shortest 个字
       → too_short（太短的片段靠不住），否则 shown。

    引用规范化以后是空的、一处都没找到 → not_found。"""
    texts = texts.texts if isinstance(texts, Content) else texts
    index = texts._index
    entry = index["segments"].get(sid) if isinstance(sid, str) else None
    wanted = interpret.normalize(quote.strip())[0] if isinstance(quote, str) else ""
    if entry is None or not wanted:
        return AskMatch("not_found")
    segment = entry[0]
    places = _find_all(index, segment, wanted)
    if not places:
        for other in index["by_source"].get(segment.source, ()):
            if other.sid != segment.sid:
                places.extend(_find_all(index, other, wanted))
    source, _, _, sentences = index["sources"][segment.source]
    spans: dict[tuple[int, int], tuple[Segment, list[tuple[int, int]]]] = {}
    for found, first, last in places:
        covered = [(a, b) for a, b in sentences if a < last and b > first]
        if covered:
            spans.setdefault((covered[0][0], covered[-1][1]), (found, covered))
    if not spans:
        return AskMatch("not_found")
    masked = []
    for (start, end), (_, covered) in spans.items():
        if "已脱敏]" not in source.text[start:end]:
            break
        inner = [(a, b) for a, b in covered if "已脱敏]" in source.text[a:b]]
        # 遮掉的内容被句界切开了（一般不会）：这一处当一句
        masked.extend(inner or [(start, end)])
    else:
        return AskMatch("redacted", masked=tuple((segment.source, a, b) for a, b in masked))
    if len(spans) > 1:
        return AskMatch("ambiguous")
    (start, end), (found, _) = next(iter(spans.items()))
    if len(wanted) < shortest:
        return AskMatch("too_short")
    original = source.text[start:end]
    return AskMatch("shown", Located(segment.source, found.sid, start, end, original, found.place))
