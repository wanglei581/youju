"""申报通知的支持方式和申报对象规则抽取。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from youju.classify import effective_kind
from youju.models import Document, DocumentDetail, DocumentField

SUPPORT_LABELS = ("资金支持", "贷款贴息", "资格认定", "入库备案", "项目立项", "其他")
FIELD_NAMES = ("support", "audience")

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]*(?:[。！？!?；;\n]|$)")
_AUDIENCE_START = re.compile(
    r"^\s*(?:[（(]?[一二三四五六七八九十百\d]+[）)、.．、]\s*)?"
    r"(?:申报对象|申报主体|申报单位|支持对象|申报条件)\s*[:：]?"
)
_NEGATION = re.compile(
    r"不予|不得|不能|不再|尚未|未(?!来)|暂不|不属于|不纳入|取消|不应|禁止|"
    r"不支持|不认定|不立项|不备案|不入库|不享受"
)
_CLAUSE_BREAK = re.compile(r"[，,、；;。！？!?：:\n]")


@dataclass(frozen=True)
class FieldCandidate:
    field: str
    value: str
    evidence: str
    evidence_offset: int

    @property
    def offset(self) -> int:
        return self.evidence_offset


def _sentences(body: str) -> list[tuple[str, int]]:
    """按句子切分并保留正文中的起始位置。"""
    result: list[tuple[str, int]] = []
    for match in _SENTENCE_RE.finditer(body):
        raw = match.group()
        stripped = raw.strip()
        if not stripped:
            continue
        offset = match.start() + len(raw) - len(raw.lstrip())
        result.append((stripped, offset))
    return result


def _is_negated(sentence: str, match: re.Match[str]) -> bool:
    """命中词前同一分句四字内有否定词时，排除该次命中。"""
    prefix = sentence[max(0, match.start() - 4) : match.start()]
    if _CLAUSE_BREAK.search(prefix):
        prefix = _CLAUSE_BREAK.split(prefix)[-1]
    if _NEGATION.search(prefix):
        return True
    # “不纳入××库”中的“入”同时属于否定词和命中词，允许这一字重叠判断。
    if "不纳入" in sentence[max(0, match.start() - 4) : match.end()]:
        return True
    # 这是原有规则明确排除的材料不齐全表述，关键词本身在短语开头。
    return sentence.startswith("备案材料不齐全", match.start())


def _has_usable_keyword(sentence: str, pattern: str) -> bool:
    return any(not _is_negated(sentence, match) for match in re.finditer(pattern, sentence))


def _usable_support(label: str, sentence: str) -> bool:
    if label == "资金支持":
        return _has_usable_keyword(
            sentence, r"资金支持|财政资金|财政补助|补助资金|资金补助|项目资金|资助|奖补|补贴"
        )
    if label == "贷款贴息":
        return _has_usable_keyword(sentence, r"贷款贴息|贴息|贷款利息")
    if label == "资格认定":
        return _has_usable_keyword(sentence, r"认定为|申报认定(?!条件|标准)|认定工作|组织认定")
    if label == "入库备案":
        return _has_usable_keyword(sentence, r"入库|备案登记|纳入[^。！？!?；;\n]{0,30}库")
    if label == "项目立项":
        return _has_usable_keyword(sentence, r"项目立项|立项") or (
            "项目申报" in sentence and _has_usable_keyword(sentence, r"项目资金")
        )
    return _has_usable_keyword(sentence, r"其他支持|其他方式|服务支持|支持方式|支持措施")


def extract_support(body: str) -> list[FieldCandidate]:
    """每个标签只保留一条最有代表性的原文句子。"""
    found: dict[str, FieldCandidate] = {}
    # 先匹配专门术语，再匹配宽泛术语，保证证据句尽量明确。
    priority = {
        "贷款贴息": 0,
        "项目立项": 1,
        "资格认定": 2,
        "入库备案": 3,
        "资金支持": 4,
        "其他": 5,
    }
    for sentence, offset in _sentences(body):
        if len(sentence) > 600:
            continue
        matched_specific = False
        for label in SUPPORT_LABELS:
            if label == "其他":
                continue
            if not _usable_support(label, sentence):
                continue
            matched_specific = True
            evidence = sentence[:300]
            candidate = FieldCandidate("support", label, evidence, offset)
            current = found.get(label)
            if current is None or (priority[label], offset) < (
                priority[label],
                current.evidence_offset,
            ):
                found[label] = candidate
        if not matched_specific and _usable_support("其他", sentence):
            found.setdefault("其他", FieldCandidate("support", "其他", sentence[:300], offset))
    return [found[label] for label in SUPPORT_LABELS if label in found]


def extract_audience(body: str) -> list[FieldCandidate]:
    sentences = _sentences(body)
    for index, (sentence, offset) in enumerate(sentences):
        if not _AUDIENCE_START.search(sentence):
            continue
        evidence = sentence[:300]
        end = offset + len(sentence)
        if index + 1 < len(sentences):
            next_sentence, next_offset = sentences[index + 1]
            end = next_offset + len(next_sentence)
        # 按正文位置切片，保留两句之间的换行和空白，不能拼接出正文里没有的内容。
        value = body[offset : min(end, offset + 200)]
        return [FieldCandidate("audience", value, evidence, offset)]
    return []


def extract_rules(body: str) -> list[FieldCandidate]:
    return extract_support(body) + extract_audience(body)


def extract_fields(body: str) -> list[FieldCandidate]:
    """规则抽取的公开入口（别名保留给评测脚本和离线标注工具）。"""
    return extract_rules(body)


def body_fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExtractReport:
    documents: int
    fields: int
    changed: int

    def __iter__(self):
        """兼容截止抽取服务的二元统计返回习惯。"""
        yield self.documents
        yield self.fields


def _merge_candidates(
    rules: list[FieldCandidate], ai: list[FieldCandidate] | None
) -> list[tuple[FieldCandidate, str]]:
    merged: dict[tuple[str, str], tuple[FieldCandidate, str]] = {
        (item.field, item.value): (item, "rule") for item in rules
    }
    for item in ai or []:
        key = (item.field, item.value)
        if key in merged:
            rule, _ = merged[key]
            # 模型证据必须逐字一致才算 both；规则证据优先保留。
            if rule.evidence == item.evidence:
                merged[key] = (rule, "both")
        else:
            merged[key] = (item, "ai")
    return list(merged.values())


def extract_existing(
    session: Session,
    *,
    since: date | None = None,
    document_ids: list[int] | None = None,
    ai_client=None,
) -> ExtractReport:
    """抽取已成功抓取的申报通知；同一正文重复运行不会改变结果。"""
    stmt = (
        select(Document, DocumentDetail)
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(DocumentDetail.status == "ok", DocumentDetail.body_text != "")
        .order_by(Document.id)
    )
    if since is not None:
        stmt = stmt.where(Document.publish_date >= since)
    if document_ids is not None:
        if not document_ids:
            return ExtractReport(0, 0, 0)
        stmt = stmt.where(Document.id.in_(document_ids))
    documents = fields = changed = 0
    for doc, detail in session.execute(stmt):
        if effective_kind(doc.title, doc.kind_override) != "申报通知":
            continue
        documents += 1
        body_hash = body_fingerprint(detail.body_text)
        existing = list(
            session.scalars(select(DocumentField).where(DocumentField.document_id == doc.id))
        )
        if existing and all(row.body_hash == body_hash for row in existing):
            continue
        if existing:
            changed += 1
            for row in existing:
                if row.status in ("approved", "stale"):
                    row.status = "stale"
                else:
                    session.delete(row)
            session.flush()
        rules = extract_rules(detail.body_text)
        ai_candidates = None
        if ai_client is not None:
            from youju.ai.fields import extract_fields_with_ai

            try:
                ai_candidates = extract_fields_with_ai(ai_client, doc.title, detail.body_text)
            except Exception:  # noqa: BLE001 - 单份模型失败不影响规则抽取
                ai_candidates = []
        merged = _merge_candidates(rules, ai_candidates)
        merged = [
            (candidate, source)
            for candidate, source in merged
            if candidate.field != "audience"
            or (
                len(candidate.value) <= 200
                and candidate.value in detail.body_text
                and len(candidate.evidence) <= 300
                and candidate.evidence in detail.body_text
            )
        ]
        if merged:
            values = [
                {
                    "document_id": doc.id,
                    "field": candidate.field,
                    "value": candidate.value,
                    "evidence": candidate.evidence,
                    "evidence_offset": candidate.evidence_offset,
                    "body_hash": body_hash,
                    "source": source,
                    "status": "pending",
                }
                for candidate, source in merged
            ]
            upsert = mysql_insert(DocumentField).values(values)
            same_body = DocumentField.body_hash == upsert.inserted.body_hash
            reviewed = DocumentField.status.in_(("approved", "stale"))
            # 冲突可能来自另一抽取进程：同正文保留核对结果，变更正文不能沿用 approved。
            # MySQL 按赋值顺序计算，状态和指纹必须最后更新。
            upsert = upsert.on_duplicate_key_update(
                [
                    ("evidence", upsert.inserted.evidence),
                    ("evidence_offset", upsert.inserted.evidence_offset),
                    ("source", upsert.inserted.source),
                    *[
                        (
                            name,
                            case((same_body | reviewed, getattr(DocumentField, name)), else_=None),
                        )
                        for name in ("approved_value", "reviewed_by", "reviewed_at")
                    ],
                    (
                        "status",
                        case(
                            (same_body, DocumentField.status), (reviewed, "stale"), else_="pending"
                        ),
                    ),
                    ("body_hash", upsert.inserted.body_hash),
                ]
            )
            session.execute(upsert)
            for row in existing:
                if row in session:
                    session.expire(row)
            fields += len(merged)
    session.commit()
    return ExtractReport(documents, fields, changed)
