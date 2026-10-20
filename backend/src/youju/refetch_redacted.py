"""一次性：重抓被正文脱敏吞了字的文件详情（2026-10-20）。

9/27（#35）到这次修改前的正文脱敏规则（crawler/detail.py 的 redact_sensitive）会把邮箱、座机前面
连着的中文一起遮掉，常常是截止日期（"PDF电子版于7月2日前发送至……邮箱"整段变成"[邮箱已脱敏]"）
和单位名（"省工信厅中小企业处 张三 0351-…"遮成"省工信厅中小企[姓名已脱敏][电话已脱敏]"）；
"联系人："后面一律遮前 2–8 个汉字，遮掉的常是单位名，姓名反倒留着。正文入库时就遮了，原文不存，
库里这些字已经没了。规则改好以后，这里挑出正文里有"[邮箱已脱敏]""[姓名已脱敏]"的文件，按补详情
同一套抓取（礼貌抓取：robots、同一网站 3 秒间隔）重新打开页面：

- same：按新规则遮出来和库里一样（以前遮得没错，或者已经换过了），不用改；
- fixed：原网页没变（新取到的原文按旧规则遮出来，和库里的正文一字不差），按新规则遮出来不一样：
  换成按新规则遮的正文；
- changed：原网页变了，不改（这里只补旧规则吞掉的字，不顺带更新内容），列出来另行处理；
- unavailable：没打开、没取到正文，不改，写明原因；missing：题录或详情已删除。

默认只预览，不写库；apply=True 才写，只改正文（body_text）：抓取时间、附件、状态都不动（抓取时间
一改，名单补抓附件那一步会以为这份已经重抓过）。写完当场：
- 重算这些文件的规则截止候选（extract.service.extract_existing，和每轮 extract-deadlines 一样，
  核对过的不动）；
- 清掉这些文件的 AI 截止检查结果（ai_checks 里 deadlines 那一项），下一轮 ai-check 按新正文重查，
  免得新找回的截止日期被标成"AI 没找到"；
- 重算"能不能 AI 解读"（ai.eligibility，拿不到写锁就跳过，admin 每 10 分钟的增量和夜里的全量
  会补上——增量只看抓取时间，这里没改抓取时间，所以要等夜里全量或手工跑 ai-eligibility-rebuild）。
正文变了，已核对的字段（document_fields 的 approved）会在下一轮 extract-fields 变成待重新核对，
确认过的申报条件清单不再对外显示、等重新抽取和核对，AI 解读的旧结果作废（D-073）：预览里列出份数。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from youju.config import get_settings
from youju.crawler import detail_service
from youju.dbtypes import BODY_TEXT_MAX_CHARS
from youju.models import (
    AICheck,
    ConditionList,
    Document,
    DocumentDetail,
    DocumentField,
    Source,
)

# 正文里有这两种标记的才可能被吞过字（旧规则只在遮邮箱、遮姓名时连带遮掉旁边的字）
MARKERS = ("[邮箱已脱敏]", "[姓名已脱敏]")

# ---- 9/27–10/20 的旧规则（只用来核对原网页变没变，原样照抄 #35 的写法） ----
_OLD_HAN = f"{chr(0x4E00)}-{chr(0x9FFF)}"
_OLD_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_OLD_ID_CARD = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]|\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
_OLD_BANK = re.compile(
    r"((?:银行卡|银行账号|银行帐号|账户|账号|帐号|卡号)\s*[:：]?\s*)(?:\d[ -]?){11,18}\d(?!\d)"
)
_OLD_EMAIL = re.compile(r"(?<![\w.])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_OLD_CONTACT = re.compile(rf"(联系人\s*[:：]\s*)[{_OLD_HAN}·]{{2,8}}")
_OLD_NAME_PHONE = re.compile(rf"(?:[{_OLD_HAN}·]\s*){{2,4}}(?=0\d{{2,3}}[-－]\d{{7,8}})")
_OLD_NAME_MASKED_PHONE = re.compile(rf"(?:[{_OLD_HAN}·]\s*){{2,4}}(?=\[电话已脱敏\])")
_OLD_PHONE = re.compile(r"(?<!\d)0\d{2,3}[-－]\d{7,8}(?!\d)")


def old_redact(text: str) -> str:
    """9/27–10/20 入库时用的正文脱敏规则。"""
    text = _OLD_ID_CARD.sub("[身份证号已脱敏]", text)
    text = _OLD_MOBILE.sub("[手机号已脱敏]", text)
    text = _OLD_BANK.sub(r"\1[银行账号已脱敏]", text)
    text = _OLD_CONTACT.sub(r"\1[姓名已脱敏]", text)
    text = _OLD_NAME_PHONE.sub("[姓名已脱敏]", text)
    text = _OLD_NAME_MASKED_PHONE.sub("[姓名已脱敏]", text)
    text = _OLD_EMAIL.sub("[邮箱已脱敏]", text)
    return _OLD_PHONE.sub("[电话已脱敏]", text)


@dataclass
class RefetchResult:
    document_id: int
    title: str
    # fixed / same / changed / unavailable / missing（见模块说明）；写入时 fixed 是已经换了的
    result: str
    reason: str | None = None
    # fixed：按新规则找回的字（前几处，每处最多 40 个字），预览时给人看
    restored: list[str] = field(default_factory=list)
    # changed：新旧两版正文的相似度（0–1）
    similarity: float | None = None


@dataclass
class RefetchReport:
    results: list[RefetchResult] = field(default_factory=list)
    # fixed 的文件里：已核对的字段、确认过且是当前正文的申报条件清单、AI 截止检查结果各有几份
    approved_fields: int = 0
    confirmed_conditions: int = 0
    ai_checks: int = 0
    # 写入以后重算：新增的规则截止候选条数、"能不能 AI 解读"重算了没有
    new_candidates: int = 0
    eligibility_refreshed: bool = False


def _has(marker: str):
    return DocumentDetail.body_text.contains(marker, autoescape=True)


def _scope(stmt):
    """只看一级公开来源、详情抓成功的（补详情也只抓一级来源）。"""
    return (
        stmt.select_from(DocumentDetail)
        .join(Document, Document.id == DocumentDetail.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(Source.rights_level == 1, DocumentDetail.status == "ok")
    )


def count_affected(session: Session) -> dict[str, int | None]:
    """只读统计，不打开网页：有正文的份数，含"[邮箱已脱敏]"的、含"[姓名已脱敏][电话已脱敏]"的、
    含"[姓名已脱敏]"的，要重抓的（含前两种标记之一）和它们的编号范围。"""
    count = select(func.count())
    either = or_(*(_has(m) for m in MARKERS))
    first, last = session.execute(
        _scope(
            select(func.min(DocumentDetail.document_id), func.max(DocumentDetail.document_id))
        ).where(either)
    ).one()
    return {
        "bodies": session.scalar(_scope(count).where(DocumentDetail.body_text != "")),
        "email": session.scalar(_scope(count).where(_has("[邮箱已脱敏]"))),
        "name_phone": session.scalar(_scope(count).where(_has("[姓名已脱敏][电话已脱敏]"))),
        "name": session.scalar(_scope(count).where(_has("[姓名已脱敏]"))),
        "candidates": session.scalar(_scope(count).where(either)),
        "first_id": first,
        "last_id": last,
    }


def _candidates(
    session: Session, limit: int, after_id: int | None
) -> list[tuple[int, str, str, str]]:
    """(编号, 网址, 标题, 库里的正文)，按编号从小到大。"""
    stmt = (
        _scope(select(Document.id, Document.url, Document.title, DocumentDetail.body_text))
        .where(or_(*(_has(m) for m in MARKERS)))
        .order_by(Document.id)
        .limit(limit)
    )
    if after_id is not None:
        stmt = stmt.where(Document.id > after_id)
    return [(int(i), url, title, body) for i, url, title, body in session.execute(stmt).all()]


def _restored(old: str, new: str, limit: int = 3) -> list[str]:
    """新正文比旧正文多出来的字（按新规则找回的），前 limit 处，每处最多 40 个字。"""
    pieces = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert") and (piece := new[j1:j2].strip()):
            pieces.append(" ".join(piece.split())[:40])
            if len(pieces) >= limit:
                break
    return pieces


def _impact(session: Session, fixed: dict[int, str], report: RefetchReport) -> None:
    """正文换了以后要重新核对的：已核对的字段、确认过的申报条件清单；会重查的 AI 截止检查。"""
    if not fixed:
        return
    ids = list(fixed)
    hashes = {doc_id: sha256(body.encode()).hexdigest() for doc_id, body in fixed.items()}
    report.approved_fields = len(
        {
            doc_id
            for doc_id, body_hash in session.execute(
                select(DocumentField.document_id, DocumentField.body_hash).where(
                    DocumentField.document_id.in_(ids), DocumentField.status == "approved"
                )
            )
            if hashes.get(doc_id) == body_hash
        }
    )
    report.confirmed_conditions = len(
        {
            doc_id
            for doc_id, body_hash in session.execute(
                select(ConditionList.document_id, ConditionList.body_hash).where(
                    ConditionList.document_id.in_(ids), ConditionList.status == "confirmed"
                )
            )
            if hashes.get(doc_id) == body_hash
        }
    )
    report.ai_checks = session.scalar(
        select(func.count(AICheck.id)).where(
            AICheck.document_id.in_(ids), AICheck.task == "deadlines"
        )
    )


def _recompute(session: Session, written: list[int], report: RefetchReport) -> None:
    """写入以后：重算规则截止候选，清掉 AI 截止检查结果，重算"能不能 AI 解读"。"""
    from youju.ai import eligibility
    from youju.extract.service import extract_existing

    if not written:
        return
    _, report.new_candidates = extract_existing(session, document_ids=written)
    session.execute(
        delete(AICheck).where(AICheck.document_id.in_(written), AICheck.task == "deadlines")
    )
    session.commit()
    # 和 admin 每 10 分钟的增量重算错开：拿不到写锁就跳过，夜里的全量重算会补上
    with eligibility.writer_lock(session, 60) as got:
        if got:
            eligibility.refresh(session, written, datetime.now(UTC))
            report.eligibility_refreshed = True


def refetch_redacted(
    session: Session,
    *,
    apply: bool = False,
    limit: int = 200,
    after_id: int | None = None,
    fetcher: detail_service.DetailFetcher | None = None,
    max_workers: int | None = None,
) -> RefetchReport:
    """预览或写入（见模块说明）。按编号从小到大最多 limit 份，分批时用 after_id 接着上一批。
    预览和写入走同一套抓取和解析（detail_service._fetch_outcomes）。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    targets = _candidates(session, limit, after_id)
    report = RefetchReport()
    if not targets:
        return report
    selected = [(doc_id, url) for doc_id, url, _, _ in targets]
    # 同补详情：网络请求期间不占用数据库连接和行锁
    session.commit()
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if fetcher is not None:
        outcomes = detail_service._fetch_outcomes(selected, fetcher, workers)
    else:
        with detail_service._polite_fetcher() as polite:
            outcomes = detail_service._fetch_outcomes(selected, polite, workers)
    fixed: dict[int, str] = {}
    pending: list[tuple[RefetchResult, str, str]] = []
    for doc_id, _, title, stored in targets:
        outcome = outcomes[doc_id]
        parsed = outcome.parsed
        if outcome.status != "ok" or parsed is None or not parsed.raw_body:
            reason = outcome.failure_reason or outcome.status
            report.results.append(RefetchResult(doc_id, title, "unavailable", reason))
            continue
        after = parsed.body_text[:BODY_TEXT_MAX_CHARS]
        if after == stored:
            # 以前就遮得对，或者已经换过了（重跑这个命令时）
            report.results.append(RefetchResult(doc_id, title, "same"))
            continue
        before = old_redact(parsed.raw_body)[:BODY_TEXT_MAX_CHARS]
        if before != stored:
            similarity = round(difflib.SequenceMatcher(None, stored, before).ratio(), 3)
            report.results.append(RefetchResult(doc_id, title, "changed", similarity=similarity))
            continue
        result = RefetchResult(doc_id, title, "fixed", restored=_restored(stored, after))
        report.results.append(result)
        fixed[doc_id] = stored
        pending.append((result, stored, after))
    _impact(session, fixed, report)
    if not apply:
        return report
    written = []
    for result, stored, after in pending:
        detail = session.get(DocumentDetail, result.document_id)
        if detail is None:
            result.result, result.reason = "missing", "详情已删除"
        elif detail.status != "ok" or detail.body_text != stored:
            # 期间被别的任务重抓过（名单补抓附件等），按那次的结果为准，这里不再改
            result.result, result.reason = "changed", "期间正文已被别的任务更新，没改"
        else:
            detail.body_text = after
            written.append(result.document_id)
        session.commit()
    _recompute(session, written, report)
    return report
