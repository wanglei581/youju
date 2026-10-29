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
from datetime import datetime
from functools import cached_property
from typing import Any

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


@dataclass(frozen=True)
class SourceText:
    """一个来源：正文，或者（以后）一个附件。逐字核对只在来源的 text 里找。"""

    key: str  # "body"；附件 "att-<内容 SHA-256 前 12 位>"
    label: str  # "正文"；"附件 2《申报书》"
    version: str  # 正文：原文指纹（同原来的 body_hash）；附件："<内容 SHA-256>:<取文字的规则版本>"
    text: str  # 遮过号码的全文
    attachment_id: int | None = None  # 附件在详情页列表里的编号；正文为 None
    readable: bool = True  # 附件现在能不能在站内打开；正文为 True
    checked_at: datetime | None = None  # 附件最后一次从原网站核对的时间；正文为 None


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
            sources=(SourceText(key=BODY, label="正文", version="", text=body),),
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

    def scope_text(self) -> str:
        """读的范围，给用户看。读附件时（CONFIG_ATTACH）的说法由附件读入第二步定（要加附件取于
        哪天、没读的原因），在那之前不给用户看。附件数按详情页列出的（本份通知自己的附件）。"""
        if self.config == CONFIG_BODY:
            if not self.attachments_total:
                return "读了：正文（这份通知没有附件）"
            return "只读了正文，附件没读"
        read = "、".join(s.label for s in self.sources)
        if not self.unread:
            return f"读了：{read}"
        unread = "、".join(u.label for u in self.unread)
        return f"读了：{read}；没读：{unread}"

    def versions(self) -> dict:
        """追问和读回答时核对内容变没变用。"""
        return {
            "config": self.config,
            "sources": {s.key: s.version for s in self.sources},
            "segments": interpret.digest([(s.sid, s.text) for s in self.segments]),
        }


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
            sources=(SourceText(key=BODY, label="正文", version=interpret.digest(raw), text=body),),
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
