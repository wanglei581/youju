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
# V5 初测（50 份样本）申报对象命中率仅约 20–30%，按 D-034 退路先不上线；改为 True 可恢复。
AUDIENCE_ENABLED = False

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
_QUALIFICATION_NEGATIVE = re.compile(
    r"被[^。！？!?；;]{0,24}认定为|认定为(?:虚假|重大失实|不良|不符合|不合格|违法)"
)
_FILING_REFERENCE = re.compile(
    r"纳入[^。！？!?；;]{0,30}(?:培育体系|体系)|"
    r"上一年度已入库[^。！？!?；;]{0,40}(?:下一年度|指南|遴选范围)"
)
_PROJECT_MATERIAL = re.compile(
    r"(?:提交|上传|报送)[^。！？!?；;]{0,20}立项(?:批复|文件)|"
    r"立项(?:批复|文件)[^。！？!?；;]{0,12}(?:复印件|材料)"
)


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
    return "不纳入" in sentence[max(0, match.start() - 4) : match.end()]


def _has_usable_keyword(sentence: str, pattern: str) -> bool:
    return any(not _is_negated(sentence, match) for match in re.finditer(pattern, sentence))


def _usable_support(label: str, sentence: str) -> bool:
    if label == "资金支持":
        return _has_usable_keyword(
            sentence,
            r"资金支持|财政资金|财政补助|补助资金|资金补助|项目资金|资助|奖补|补贴|"
            r"财政科技经费|配套经费|科技创新券|科技计划项目支持",
        )
    if label == "贷款贴息":
        return _has_usable_keyword(sentence, r"贷款贴息|贴息|贷款利息")
    if label == "资格认定":
        if _QUALIFICATION_NEGATIVE.search(sentence):
            return False
        if _has_usable_keyword(
            sentence,
            r"认定为|申报认定(?!条件|标准)|认定工作|组织认定|公告为|正式成为|授牌|"
            r"认定与评价|公告的步骤|(?:创新型中小企业|企业技术中心|孵化器).{0,10}评价",
        ):
            return True
        # 单独出现“认定条件”通常是申报门槛；“对照/依据认定条件”是本通知组织认定。
        return bool(re.search(r"(?:对照|依据|按照)[^。！？!?；;]{0,20}认定条件", sentence))
    if label == "入库备案":
        if _FILING_REFERENCE.search(sentence):
            return False
        return _has_usable_keyword(
            sentence,
            r"入库|备案登记|纳入[^。！？!?；;\n]{0,30}库|进入[^。！？!?；;\n]{0,20}资源池|资源池|"
            r"申报[^。！？!?；;\n]{0,25}储备[^。！？!?；;\n]{0,20}平台",
        )
    if label == "项目立项":
        if _PROJECT_MATERIAL.search(sentence) or re.search(
            r"(?:已经|已)[^。！？!?；;]{0,15}立项支持过|不得作为[^。！？!?；;]{0,30}再次提报",
            sentence,
        ):
            return False
        return _has_usable_keyword(sentence, r"项目立项|立项") or (
            "项目申报" in sentence and _has_usable_keyword(sentence, r"项目资金")
        )
    if re.search(r"字以内|填写说明", sentence):
        return False
    if re.search(
        r"(?:确定|明确|选择|另行|根据[^。！？!?；;]{0,20}确定)[^。！？!?；;]{0,12}支持方式",
        sentence,
    ):
        return False
    return _has_usable_keyword(
        sentence,
        r"其他支持|其他方式|服务支持|支持方式|支持措施|政策支持|税收优惠|增值税税额|"
        r"加计[^。！？!?；;]{0,10}抵减",
    )


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
    result = extract_support(body)
    if AUDIENCE_ENABLED:
        result.extend(extract_audience(body))
    return result


def extract_fields(body: str) -> list[FieldCandidate]:
    """规则抽取的公开入口（别名保留给评测脚本和离线标注工具）。"""
    return extract_rules(body)


def _usable_candidate(candidate: FieldCandidate, body: str) -> bool:
    if candidate.field != "audience":
        return True
    if not AUDIENCE_ENABLED:
        return False
    return (
        len(candidate.value) <= 200
        and candidate.value in body
        and len(candidate.evidence) <= 300
        and candidate.evidence in body
    )


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
        # 关闭 audience 后，历史行只保留作留痕；它们不应阻止 support 重抽，也不因重抽被删除。
        active_existing = [row for row in existing if AUDIENCE_ENABLED or row.field != "audience"]
        if active_existing and all(row.body_hash == body_hash for row in active_existing):
            continue
        if existing:
            changed += 1
            for row in existing:
                if not AUDIENCE_ENABLED and row.field == "audience":
                    if row.status == "approved":
                        row.status = "stale"
                    continue
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
            if _usable_candidate(candidate, detail.body_text)
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
