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
- changed：原网页变了，不改（这里只补旧规则吞掉的字，不顺带更新内容），列出来另行处理；按新规则
  正文超过长度上限的（换了会截掉文末）、写入时正文已被别的任务换过的也算这里；
- unavailable：没打开、没取到正文（写入时详情状态不再是抓成功的也算），不改，写明原因；
  missing：题录或详情已删除。

默认只预览，不写库；apply=True 才写，只改正文（body_text）：抓取时间、附件、状态都不动（抓取时间
一改，名单补抓附件那一步会以为这份已经重抓过）。每换一份：
- 同一个事务里，找回的字里有日期、时刻（数字或"年月日时周星点午"）的，清掉它的 AI 截止检查结果
  （ai_checks 里 deadlines 那一项），下一轮 ai-check 按新正文重查，免得新找回的截止日期被标成
  "AI 没找到"；只找回单位名、标签的不清（AI 找截止不看这些，省用量）；
- 提交后当场重算它的规则截止候选（extract.service.extract_existing，和每轮 extract-deadlines 一样，
  核对过的不动）：中途中断也不会留下换了正文、没重算的。
一批写完：这一批"不用改"的也重算候选（中断后重跑，已换过、还带标记的是"不用改"）；换了正文的和
"不用改"的重算"能不能 AI 解读"（ai.eligibility，拿不到写锁就跳过，夜里的全量会补上——每 10 分钟
的增量只看抓取时间，这里没改抓取时间）。没打开的（unavailable）分批时会被 after_id 跳过，用 ids
单独重试。写入只写预览过的那一批：预览给出这一批的标记（最后一份的编号和各份编号、正文指纹的摘要），
写入时带上，按它重新选出的这一批和预览时不一样就什么都不做。
正文一换，已核对的字段、确认过的申报条件清单读取时按正文指纹核对，马上不再对外显示（下一轮
extract-fields 把字段标成待重新核对，条件清单等重新抽取和核对），AI 解读的旧结果作废（D-073）：
预览里列出份数。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import delete, func, or_, select, update
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
    DocumentMerge,
    Source,
)

# 正文里有这两种标记的才可能被吞过字（旧规则只在遮邮箱、遮姓名时连带遮掉旁边的字）
MARKERS = ("[邮箱已脱敏]", "[姓名已脱敏]")

# ---- 9/27–10/20 的旧规则（只用来核对原网页变没变，照抄 #35 的写法；空白改用占有量词，结果一样，
# 遇到一长串空格不会卡住） ----
_OLD_HAN = f"{chr(0x4E00)}-{chr(0x9FFF)}"
_OLD_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_OLD_ID_CARD = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]|\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
_OLD_BANK = re.compile(
    r"((?:银行卡|银行账号|银行帐号|账户|账号|帐号|卡号)\s*+[:：]?+\s*+)(?:\d[ -]?){11,18}\d(?!\d)"
)
_OLD_EMAIL = re.compile(r"(?<![\w.])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_OLD_CONTACT = re.compile(rf"(联系人\s*+[:：]\s*+)[{_OLD_HAN}·]{{2,8}}")
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
    # fixed：找回的字里有日期、时刻（数字或"年月日时"），AI 截止检查要按新正文重查
    recheck_ai: bool = False
    # changed：新旧两版正文的相似度（0–1）
    similarity: float | None = None


@dataclass
class RefetchReport:
    results: list[RefetchResult] = field(default_factory=list)
    # fixed 的文件里：已核对的字段、确认过且是当前正文的申报条件清单各有几份；要清掉重查的 AI
    # 截止检查有几份（只算找回的字里有日期、时刻的）
    approved_fields: int = 0
    confirmed_conditions: int = 0
    ai_checks: int = 0
    # 写入以后重算：新增的规则截止候选条数、"能不能 AI 解读"重算了没有
    new_candidates: int = 0
    eligibility_refreshed: bool = False
    # 这一批的标记（batch_token）：预览打出来，写入时带上
    batch: str = ""


class BatchChanged(Exception):
    """写入时按标记重新选出的这一批和预览时不一样（多了、少了，或者有正文变了）：什么都没做。"""


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


def _display_changes(session: Session, batch: int = 500) -> int:
    """库里的正文按新规则再过一遍（详情页、AI 解读、申报条件给出前都这样做）会变的份数：这些文件的
    AI 解读旧结果会作废（D-073）。按编号分批读，不一次读进全部正文。"""
    from youju.crawler.detail import redact_sensitive

    changed, after = 0, 0
    while True:
        rows = session.execute(
            _scope(select(DocumentDetail.document_id, DocumentDetail.body_text))
            .where(DocumentDetail.document_id > after, DocumentDetail.body_text != "")
            .order_by(DocumentDetail.document_id)
            .limit(batch)
        ).all()
        if not rows:
            return changed
        changed += sum(redact_sensitive(body) != body for _, body in rows)
        after = rows[-1][0]


def count_affected(session: Session) -> dict[str, int | None]:
    """只读统计，不打开网页：有正文的份数，含"[邮箱已脱敏]"的、含"[姓名已脱敏][电话已脱敏]"的、
    含"[姓名已脱敏]"的，要重抓的（含前两种标记之一）和它们的编号范围，按新规则再过一遍会变的份数。"""
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
        "display_changes": _display_changes(session),
    }


def _candidates(
    session: Session,
    limit: int | None,
    after_id: int | None,
    ids: list[int] | None = None,
    upto_id: int | None = None,
) -> list[tuple[int, str, str, str]]:
    """(编号, 网址, 标题, 库里的正文)，按编号从小到大，最多 limit 份（None 不限）。ids：只看这几份
    （重试没打开的）；upto_id：只看编号不大于它的（写入时按预览那一批的标记重新选出这一批）。"""
    stmt = (
        _scope(select(Document.id, Document.url, Document.title, DocumentDetail.body_text))
        .where(or_(*(_has(m) for m in MARKERS)))
        .order_by(Document.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if after_id is not None:
        stmt = stmt.where(Document.id > after_id)
    if ids is not None:
        stmt = stmt.where(Document.id.in_(ids))
    if upto_id is not None:
        stmt = stmt.where(Document.id <= upto_id)
    return [(int(i), url, title, body) for i, url, title, body in session.execute(stmt).all()]


_BATCH = re.compile(r"(\d+)-[0-9a-f]{12}")


def batch_token(targets: list[tuple[int, str, str, str]]) -> str:
    """一批的标记："这一批最后一份的编号-各份编号和正文指纹的摘要（12 位）"。预览打在汇总行里，
    写入时照抄：写入按标记里的编号重新选出这一批，和预览时一份不差、正文也没变才做。"""
    digest = sha256()
    for doc_id, _, _, body in targets:
        digest.update(f"{doc_id}:{sha256(body.encode()).hexdigest()}\n".encode())
    return f"{targets[-1][0] if targets else 0}-{digest.hexdigest()[:12]}"


# 日期、时刻用的字：找回的字里有这些（或数字）才清掉 AI 截止检查重查（"本周五前""下午三点前"也算）
_TIME_CHARS = set("年月日时周星点午")


# 两头去掉相同的部分以后，中间超过这么多字就不逐字比：逐字比（difflib）遇到大段重复的字（几千个
# "—"分隔线）是平方级的，6000 个字就要几秒，一份正文最多 400 万字
_DIFF_LIMIT = 4000
# 中间太长时按"句"比（以换行和句号、分号、逗号这些标点断开）：一整行很长、两处改动隔得很远时，按行比
# 只能整段算一处，会把中间原有的"2026年"也算成找回的字、白清一次 AI 截止检查。句子数超过这个就不再
# 比（几十万句的比对本身也慢），整段算一处。按句比用 difflib 默认的忽略高频句（不忽略的话，同一句
# 重复几千遍要比十几秒），代价是这样的正文对不齐、整段算一处
_UNIT_END = re.compile(r"(?<=[\n。；;！!？?，,])")
_MAX_UNITS = 20000


def _units(text: str) -> list[str]:
    return [unit for unit in _UNIT_END.split(text) if unit]


def _trim(old: str, new: str) -> tuple[int, int]:
    """两份正文开头、结尾相同的字数（不重叠）。"""
    head = 0
    limit = min(len(old), len(new))
    while head < limit and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < limit - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    return head, tail


def _added(old: str, new: str, *, by_units: bool = True) -> list[str]:
    """new 比 old 多出来（换掉）的片段。先去掉两头相同的部分；中间不长就逐字比；太长（改动分散在长
    正文里）先按句比、改了的几句再这样比，还太长（或句子太多，或同一句重复很多遍、对不齐）就整段
    算一处。"""
    head, tail = _trim(old, new)
    a, b = old[head : len(old) - tail], new[head : len(new) - tail]
    if not b:
        return []
    if len(a) + len(b) <= _DIFF_LIMIT:
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        return [
            b[j1:j2] for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in ("replace", "insert")
        ]
    a_units, b_units = _units(a), _units(b)
    if (
        not by_units
        or len(a_units) <= 1
        or len(b_units) <= 1
        or len(a_units) + len(b_units) > _MAX_UNITS
    ):
        return [b]
    pieces: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a_units, b_units).get_opcodes():
        if tag in ("replace", "insert"):
            pieces += _added("".join(a_units[i1:i2]), "".join(b_units[j1:j2]), by_units=False)
    return pieces


def _restored(old: str, new: str) -> list[str]:
    """新正文比旧正文多出来的字（按新规则找回的，也包括新遮的"[姓名已脱敏]"），每处去掉多余空白。"""
    return [" ".join(piece.split()) for piece in _added(old, new) if piece.strip()]


def _similarity(a: str, b: str) -> float:
    """两份正文有多像（0–1，只是给人看的参考）：两头相同的部分，加上中间比对上的字数——中间不长就逐字
    比，太长就按句比、对上的句按字数算（原网页常常只改了开头的发布日期和结尾的附件名，只算两头会算成
    0）；句子太多就只算两头。"""
    if not a and not b:
        return 1.0
    head, tail = _trim(a, b)
    same = head + tail
    middle_a, middle_b = a[head : len(a) - tail], b[head : len(b) - tail]
    if len(middle_a) + len(middle_b) <= _DIFF_LIMIT:
        blocks = difflib.SequenceMatcher(None, middle_a, middle_b, autojunk=False)
        same += sum(block.size for block in blocks.get_matching_blocks())
    else:
        units_a, units_b = _units(middle_a), _units(middle_b)
        if len(units_a) + len(units_b) <= _MAX_UNITS:
            for block in difflib.SequenceMatcher(None, units_a, units_b).get_matching_blocks():
                same += sum(len(unit) for unit in units_a[block.a : block.a + block.size])
    return round(2 * same / (len(a) + len(b)), 3)


def _reviewed(session: Session, bodies: dict[int, str]) -> tuple[int, int]:
    """这些文件（正文还是 bodies 里那版）有已核对字段的、有确认过的申报条件清单的各几份：正文一换，
    这些要重新核对。"""
    if not bodies:
        return 0, 0
    ids = list(bodies)
    hashes = {doc_id: sha256(body.encode()).hexdigest() for doc_id, body in bodies.items()}
    fields = len(
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
    conditions = len(
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
    return fields, conditions


def _ai_checks(session: Session, ids: set[int]) -> int:
    """这些文件的 AI 截止检查有几条（找回了日期、时刻的，写入时清掉、下一轮重查）。"""
    if not ids:
        return 0
    return session.scalar(
        select(func.count(AICheck.id)).where(
            AICheck.document_id.in_(ids), AICheck.task == "deadlines"
        )
    )


def _write(
    session: Session, doc_id: int, stored: str, after: str, recheck_ai: bool
) -> tuple[bool, int]:
    """换一份正文：库里还是选中时那版（按指纹比，期间被别的任务重抓过的不改）才换；找回了日期、时刻的
    同一个事务里清掉它的 AI 截止检查结果（下一轮 ai-check 按新正文重查）。不提交：调用的地方接着在
    同一个事务里重算这一份的截止候选、一起提交。返回 (换了没有, 清掉几条)。"""
    changed = session.execute(
        update(DocumentDetail)
        .where(
            DocumentDetail.document_id == doc_id,
            DocumentDetail.status == "ok",
            func.sha2(DocumentDetail.body_text, 256) == sha256(stored.encode()).hexdigest(),
        )
        .values(body_text=after)
        .execution_options(synchronize_session=False)
    ).rowcount
    cleared = 0
    if changed and recheck_ai:
        cleared = session.execute(
            delete(AICheck).where(AICheck.document_id == doc_id, AICheck.task == "deadlines")
        ).rowcount
    return bool(changed), cleared


def _recompute(
    session: Session, written: list[int], same: list[int], report: RefetchReport
) -> None:
    """一批写完以后：这一批"不用改"的重算规则截止候选（中途中断后重跑，已换过、还带标记的几份是
    "不用改"，这样补上；换了正文的已经在写入时逐份算过）；换了正文的和"不用改"的重算"能不能
    AI 解读"（合并组主条目一起）。这一步出错也不要紧：每轮的 extract-deadlines、夜里的
    ai-eligibility-rebuild 都是全量的，会按库里的正文再算。"""
    from youju.ai import eligibility
    from youju.extract.service import extract_existing

    if same:
        _, added = extract_existing(session, document_ids=same)
        report.new_candidates += added
    doc_ids = set(written) | set(same)
    if not doc_ids:
        return
    # 合并转载的主条目没有自己的正文时用组里最长的那份（同 eligibility.changed_since），一起重算
    masters = set(
        session.scalars(
            select(DocumentMerge.canonical_id).where(
                DocumentMerge.duplicate_id.in_(doc_ids), DocumentMerge.active.is_(True)
            )
        )
    )
    # 和 admin 每 10 分钟的增量重算错开：拿不到写锁就跳过，夜里的全量重算会补上
    with eligibility.writer_lock(session, 60) as got:
        if got:
            eligibility.refresh(session, doc_ids | masters, datetime.now(UTC))
            report.eligibility_refreshed = True


def refetch_redacted(
    session: Session,
    *,
    apply: bool = False,
    limit: int | None = 200,
    after_id: int | None = None,
    ids: list[int] | None = None,
    batch: str | None = None,
    fetcher: detail_service.DetailFetcher | None = None,
    max_workers: int | None = None,
) -> RefetchReport:
    """预览或写入（见模块说明）。按编号从小到大最多 limit 份（None 不限），分批时用 after_id 接着
    上一批；ids：只看这几份（重试没打开的）。batch：预览那一批的标记（report.batch），命令行写入时
    必带：按标记里的编号重新选出这一批（不看 limit），和预览时不一样（预览以后有正文被别的任务换过、
    多了少了，或者 after_id、ids 和预览时不同）就抛 BatchChanged，不打开网页、不写——没预览过的
    不会写进去。预览和写入走同一套抓取和解析（detail_service._fetch_outcomes）。"""
    if limit is not None and limit < 1:
        raise ValueError("limit 必须大于 0")
    upto_id = None
    if batch is not None:
        if not (matched := _BATCH.fullmatch(batch)):
            raise ValueError("batch 要照抄预览那一行的 batch=…（编号-12 位摘要）")
        upto_id, limit = int(matched[1]), None
    targets = _candidates(session, limit, after_id, ids, upto_id)
    report = RefetchReport(batch=batch_token(targets))
    if batch is not None and report.batch != batch:
        raise BatchChanged(report.batch)
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
    # 抓取期间详情被删了的，预览、写入都标成已删除
    present = set(
        session.scalars(
            select(DocumentDetail.document_id).where(
                DocumentDetail.document_id.in_([doc_id for doc_id, _ in selected])
            )
        )
    )
    fixed: dict[int, str] = {}
    same: set[int] = set()
    pending: list[tuple[RefetchResult, str, str]] = []
    for doc_id, _, title, stored in targets:
        if doc_id not in present:
            report.results.append(RefetchResult(doc_id, title, "missing", "详情已删除"))
            continue
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
            same.add(doc_id)
            continue
        before = old_redact(parsed.raw_body)[:BODY_TEXT_MAX_CHARS]
        if before != stored:
            similarity = _similarity(stored, before)
            report.results.append(RefetchResult(doc_id, title, "changed", similarity=similarity))
            continue
        if len(parsed.body_text) > BODY_TEXT_MAX_CHARS:
            # 按新规则正文变长、超过长度上限：换了会截掉库里原有的文末，不换
            reason = "按新规则正文超过长度上限，换了会截掉文末，没改"
            report.results.append(RefetchResult(doc_id, title, "changed", reason))
            continue
        pieces = _restored(stored, after)
        result = RefetchResult(
            doc_id,
            title,
            "fixed",
            restored=[piece[:40] for piece in pieces[:3]],
            recheck_ai=any(c.isdigit() or c in _TIME_CHARS for piece in pieces for c in piece),
        )
        report.results.append(result)
        fixed[doc_id] = stored
        pending.append((result, stored, after))
    recheck = {r.document_id for r, _, _ in pending if r.recheck_ai}
    report.approved_fields, report.confirmed_conditions = _reviewed(session, fixed)
    report.ai_checks = _ai_checks(session, recheck)
    if not apply:
        return report
    from youju.extract.service import extract_existing

    written = []
    # 写入时按实际清掉的条数报（期间被别的任务重抓过、没换的那几份不算）
    report.ai_checks = 0
    for result, stored, after in pending:
        changed, cleared = _write(session, result.document_id, stored, after, result.recheck_ai)
        if changed:
            # 同一个事务里重算这一份的规则截止候选，extract_existing 最后一起提交：换的正文、清掉的
            # AI 截止检查、新候选要么都落库、要么都不落（中途出错、进程被杀，这一份还是旧正文、带着
            # 标记，重跑能再选到）
            _, added = extract_existing(session, document_ids=[result.document_id])
            report.new_candidates += added
            report.ai_checks += cleared
            written.append(result.document_id)
            continue
        session.commit()
        detail = session.get(DocumentDetail, result.document_id)
        if detail is None:
            result.result, result.reason = "missing", "详情已删除"
        elif detail.body_text == stored:
            # 正文还是选中时那版，只是详情状态不再是抓成功：这次没换，和没打开的一样记下来重试
            result.result, result.reason = "unavailable", f"写入时详情状态是 {detail.status}，没改"
        else:
            # 期间被别的任务重抓过（名单补抓附件等），按那次的结果为准，这里不再改
            result.result, result.reason = "changed", "期间正文已被别的任务更新，没改"
    # 按实际换了正文的报（期间被别的任务重抓过、没换的那几份不算）
    report.approved_fields, report.confirmed_conditions = _reviewed(
        session, {doc_id: fixed[doc_id] for doc_id in written}
    )
    _recompute(session, written, sorted(same), report)
    return report
