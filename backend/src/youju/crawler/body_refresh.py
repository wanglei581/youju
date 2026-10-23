"""一次性：重新打开已收录通知的原网页，按现在的正文提取规则重取正文（10/22 正文混进网页上的字）。

10/22 发现湖北科技厅等网站收录的正文混进了网页上的导航、发布信息、按钮、二维码、弹窗等字样，
青海工信厅收录的"正文"只有导航（crawler/detail.py 的正文区规则已改）。补详情不会再抓已经抓成功的，
这里按网站（或来源）挑出详情抓成功的，礼貌地重新打开页面，用补详情同一套抓取和解析取正文，和库里的比：
- trimmed：新正文就是库里的去掉开头、结尾若干行，而且去掉的每一行现在都还在原网页上（只是不在正文区
  里了，就是网页上的导航、按钮这些字），写入时换；
- changed：别的变化（原网页改过、删了开头结尾的内容，或以前根本没取到正文），列出前后开头对照，
  加 include_changed 才换；
- unchanged：一样，不写；empty：新正文为空，不换；failed：没抓成功，不换（原来的照旧）。

默认只预览，不写库。写入只换正文和抓取时间（记写入那一刻；发文机关、文号、日期、附件都不动），
再按新正文重算归并（同补详情）。正文换了以后，按现有规则：
- 能不能 AI 解读：admin 每 10 分钟重算最近 30 分钟抓取时间有变的（ai/eligibility.py）；
- 已核对的支持方式、已确认的申报条件：正文指纹对不上就不再对外，下一轮重抽后要重新核对、确认；
- 确认过的转发组：确认时的快照含正文指纹，下一轮归组时转"待复核"；
- 用户已有的 AI 解读、条款对照：显示"通知正文已变化，请重新解读"；
- 已核对的截止照旧；原句原来在旧正文里、新正文里没有了的单独数出来，要人看。
- 后台 AI 复查（类型、截止，ai/pipeline.py）每份只做一次、正文变了不会自己重做：写入时删掉这份的
  复查记录，下一轮按新正文重查（要花 AI 用量）；复查写结果前核对正文没被换掉。
这些在预览里按份数出来（impact，口径和对外显示一致），写不写由人定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import JSON, case, delete, func, or_, select
from sqlalchemy.orm import Session

from youju.ai.pipeline import TASKS as AI_CHECK_TASKS
from youju.config import get_settings
from youju.crawler import detail_service
from youju.crawler.detail import outside_body_text, redact_sensitive
from youju.crawler.fetcher import FetchResult
from youju.dbtypes import BODY_TEXT_MAX_CHARS
from youju.deadline_excerpts import _normal
from youju.dedup import reconcile_documents
from youju.models import (
    AdvisorTask,
    AICheck,
    ConditionList,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    RepostGroup,
    RepostGroupMember,
    Source,
)

# 结果按正文指纹作废的 AI 任务（api/advisor.py 显示时比正文）
AI_TASK_KINDS = ("interpret", "clause")
_BATCH = 500
# 网页上的导航、按钮、二维码、弹窗等常见字样（10/22 抽样所见）：只用来在库里数哪些来源可能有问题
# （survey），正文里偶尔也会有（如"上一条规定"），所以不按它删字，要不要重抓看预览
SURVEY_PHRASES = (
    "当前位置",
    "您现在的位置",
    "欢迎关注",
    "即将离开",
    "扫一扫",
    "【打印】",
    "【关闭】",
    "打印本页",
    "关闭窗口",
    "上一篇",
    "下一篇",
    "上一条",
    "下一条",
    "阅读次数",
    "浏览次数",
    "字号",
    "视力保护色",
    "【市民留言】",
)


@dataclass
class Impact:
    """正文换了以后要重新处理的已有结果（份数）。"""

    approved_fields: int = 0  # 正在对外的已核对支持方式（v1._public_fields）
    confirmed_conditions: int = 0  # 正在对外的已确认申报条件清单（conditions._public_lists）
    confirmed_reposts: int = 0  # 确认过、正在折叠显示的转发组成员
    ai_results: int = 0  # 用户还能看、读的是这份正文的 AI 解读和条款对照结果
    lost_quotes: int = 0  # 已核对截止的原句原来在旧正文里、新正文里没有了
    ai_checks: int = 0  # 按旧正文做的后台 AI 复查（类型、截止；写入时删掉，下一轮重查）

    def add(self, other: Impact) -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))


@dataclass
class BodyRefresh:
    document_id: int
    title: str
    # trimmed / changed / unchanged / empty / failed（见模块说明）
    result: str
    reason: str | None = None
    # 新正文是旧正文去掉开头、结尾若干行时去掉的那几行（changed 里也可能有：有的已不在原网页上）
    head: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)
    old_chars: int = 0
    new_chars: int = 0
    # changed：前后开头对照
    old_start: str = ""
    new_start: str = ""
    impact: Impact = field(default_factory=Impact)
    written: bool = False


@dataclass(frozen=True)
class SurveyRow:
    source: Source
    ok: int  # 详情抓成功的份数
    hits: int  # 其中正文带 SURVEY_PHRASES 里任一字样的份数


def survey(session: Session) -> list[SurveyRow]:
    """只读、不联网：每个一级来源抓成功的正文里，带网页字样的有几份（多的在前），供挑要重抓的网站。"""
    hit = or_(*(DocumentDetail.body_text.contains(phrase) for phrase in SURVEY_PHRASES))
    rows = session.execute(
        select(Document.source_id, func.count(), func.sum(case((hit, 1), else_=0)))
        .join(DocumentDetail, DocumentDetail.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentDetail.status == "ok", Source.rights_level == 1)
        .group_by(Document.source_id)
    ).all()
    sources = {s.id: s for s in session.scalars(select(Source))}
    result = [SurveyRow(sources[sid], int(ok), int(hits or 0)) for sid, ok, hits in rows]
    return sorted(result, key=lambda row: (-row.hits, row.source.id))


def trimmed_lines(old: str, new: str) -> tuple[list[str], list[str]] | None:
    """新正文是不是旧正文去掉开头、结尾若干行（至少去掉一行）；是的话返回去掉的开头行、结尾行。"""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if not new_lines or len(new_lines) >= len(old_lines):
        return None
    for start in range(len(old_lines) - len(new_lines) + 1):
        if old_lines[start : start + len(new_lines)] == new_lines:
            end = start + len(new_lines)
            return old_lines[:start], old_lines[end:]
    return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _still_on_page(lines: list[str], fetch: FetchResult | None) -> bool:
    """去掉的每一行现在都还在原网页上、看得见、不算正文的地方（detail.outside_body_text：导航、按钮、
    页尾这些）。原网页改过、删掉了开头结尾的条款的，这几行找不到（留在脚本或隐藏元素里的也不算）。
    比的时候去掉空白；入库的正文脱敏过，网页文字脱敏前后各比一次。"""
    if fetch is None or not fetch.text:
        return False
    text = outside_body_text(fetch.text, fetch.final_url or fetch.url)
    if text is None:
        return False
    pages = (_squash(text), _squash(redact_sensitive(text)))
    return all(any(_squash(line) in page for page in pages) for line in lines)


def _candidates(
    session: Session,
    hosts: list[str],
    source_ids: list[int],
    limit: int,
    after_id: int | None,
) -> list[tuple[int, str, str, str]]:
    """(编号, 网址, 标题, 库里的正文)：一级来源、详情抓成功的，按编号从小到大。按网站时看跳转后的
    最终网址（解析正文按的也是它），主机名精确匹配。按编号分批读，选够 limit 份就停。"""
    wanted = {host.lower() for host in hosts}
    picked: list[tuple[int, str, str, str]] = []
    after = after_id or 0
    while len(picked) < limit:
        stmt = (
            select(
                Document.id,
                Document.url,
                Document.title,
                DocumentDetail.final_url,
                DocumentDetail.body_text,
            )
            .join(Source, Document.source_id == Source.id)
            .join(DocumentDetail, DocumentDetail.document_id == Document.id)
            .where(Source.rights_level == 1, DocumentDetail.status == "ok", Document.id > after)
            .order_by(Document.id)
            .limit(_BATCH)
        )
        if source_ids:
            stmt = stmt.where(Document.source_id.in_(source_ids))
        if wanted:
            # 粗筛：网址或最终网址里有主机名；精确判断在下面
            stmt = stmt.where(
                or_(
                    *(
                        or_(Document.url.contains(host), DocumentDetail.final_url.contains(host))
                        for host in wanted
                    )
                )
            )
        rows = session.execute(stmt).all()
        if not rows:
            break
        after = rows[-1][0]
        for document_id, url, title, final_url, body in rows:
            if wanted and (urlsplit(final_url or url).hostname or "").lower() not in wanted:
                continue
            picked.append((int(document_id), url, title, body or ""))
            if len(picked) >= limit:
                break
    return picked


def _ai_results(session: Session, now: datetime) -> dict[int, list[str | None]]:
    """用户还没过期的 AI 解读、条款对照结果，按读的是哪份文件的正文分（快照里的 body_id，同一通知
    自己没正文时用归并组里的另一份；旧任务没记的按它自己的文件），每个记下当时的正文指纹
    （body_hash）。那份正文变了结果才作废（interpret.body_changed），统计时只数指纹还对得上
    （还在显示）的。"""
    hashes: dict[int, list[str | None]] = {}
    rows = session.execute(
        select(AdvisorTask.snapshot, AdvisorTask.input_json).where(
            AdvisorTask.kind.in_(AI_TASK_KINDS),
            AdvisorTask.status == "done",
            AdvisorTask.result_json.is_not(None),
            AdvisorTask.result_json != JSON.NULL,
            or_(AdvisorTask.expires_at.is_(None), AdvisorTask.expires_at > now),
        )
    )
    for snapshot, input_json in rows:
        snapshot = snapshot or {}
        document_id = (
            snapshot.get("body_id") or snapshot.get("doc_id") or (input_json or {}).get("doc_id")
        )
        if document_id:
            hashes.setdefault(document_id, []).append(snapshot.get("body_hash"))
    return hashes


def _impact(
    session: Session,
    document_id: int,
    old: str,
    new: str,
    ai_results: dict[int, list[str | None]],
) -> Impact:
    """写入后要重新处理的已有结果。支持方式、申报条件直接用对外读取的条件数（预览时库里还是旧正文，
    对外条件里的"当前正文"就是旧正文；换了以后它们都对不上新正文的指纹）。"""
    # 延迟引入：v1 装载时会引入一大批模块，和这里互相引用
    from youju.ai import interpret
    from youju.api import v1
    from youju.conditions import _public_lists

    approved = session.scalar(
        select(func.count()).select_from(
            v1._public_fields().where(DocumentField.document_id == document_id).subquery()
        )
    )
    conditions = session.scalar(
        select(func.count()).select_from(
            _public_lists()
            .where(ConditionList.document_id == document_id, ConditionList.status == "confirmed")
            .subquery()
        )
    )
    reposts = session.scalar(
        select(func.count())
        .select_from(RepostGroupMember)
        .join(RepostGroup, RepostGroup.id == RepostGroupMember.group_id)
        .where(
            RepostGroupMember.document_id == document_id,
            RepostGroupMember.active.is_(True),
            RepostGroupMember.confirmed_at.is_not(None),
            RepostGroup.needs_review.is_(False),
        )
    )
    # 和截止摘录比原句的写法一样（去掉空白、全角半角统一）：原来在旧正文里、新正文里没有了的才算
    before, after = _normal(old), _normal(new)
    quotes = session.scalars(
        select(DocumentDeadline.evidence).where(
            DocumentDeadline.document_id == document_id, DocumentDeadline.status == "approved"
        )
    )
    lost = sum(
        1 for quote in quotes if (text := _normal(quote or "")) in before and text not in after
    )
    checks = session.scalar(
        select(func.count())
        .select_from(AICheck)
        .where(AICheck.document_id == document_id, AICheck.task.in_(AI_CHECK_TASKS))
    )
    # 还在显示的：快照里的正文指纹（interpret.digest，和 prepare 一样算）还对得上现在这份；
    # 没记指纹的旧任务算上（宁可多数）
    shown = interpret.digest(old)
    ai = sum(1 for digest in ai_results.get(document_id, []) if digest in (None, shown))
    return Impact(approved or 0, conditions or 0, reposts or 0, ai, lost, checks or 0)


def _write(session: Session, document_id: int, old: str, new: str) -> str:
    """换正文和抓取时间，删掉这份按旧正文做的后台 AI 复查记录（下一轮重查），按新正文重算归并。
    先锁住这份详情（后台 AI 复查写结果前也先锁它、核对正文）；库里的正文和预览时不一样（期间被别的
    任务改过）就不写。"""
    detail = session.get(DocumentDetail, document_id, with_for_update=True, populate_existing=True)
    doc = session.get(Document, document_id)
    if detail is None or doc is None or detail.status != "ok" or detail.body_text != old:
        session.rollback()
        return "库里的详情在这期间变了，未写入"
    detail.body_text = new
    # 抓取时间记写入这一刻，不记打开页面那一刻：一批先全部打开再逐份写，同一网站几百份要抓几十分钟，
    # 按打开时间记，"能不能 AI 解读"的增量重算（每 10 分钟看最近 30 分钟）会漏掉前面那些
    detail.fetched_at = datetime.now(UTC)
    session.execute(
        delete(AICheck).where(AICheck.document_id == document_id, AICheck.task.in_(AI_CHECK_TASKS))
    )
    session.flush()
    reconcile_documents(session, {doc.title})
    session.commit()
    return ""


def refresh_bodies(
    session: Session,
    *,
    hosts: list[str] | None = None,
    source_ids: list[int] | None = None,
    apply: bool = False,
    include_changed: bool = False,
    limit: int = 200,
    after_id: int | None = None,
    fetcher: detail_service.DetailFetcher | None = None,
    max_workers: int | None = None,
) -> list[BodyRefresh]:
    """预览或写入（见模块说明）。hosts、source_ids 至少给一个，不整库重抓。
    预览和写入走同一套抓取和解析（detail_service._fetch_outcomes），写入只是把预览的结果换进去。"""
    if not hosts and not source_ids:
        raise ValueError("要指定网站（hosts）或来源（source_ids）")
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    targets = _candidates(session, hosts or [], source_ids or [], limit, after_id)
    if not targets:
        return []
    ai_results = _ai_results(session, datetime.now(UTC))
    selected = [(document_id, url) for document_id, url, _, _ in targets]
    # 同补详情：网络请求期间不占用数据库连接和行锁
    session.commit()
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if fetcher is not None:
        outcomes = detail_service._fetch_outcomes(selected, fetcher, workers)
    else:
        with detail_service._polite_fetcher() as polite:
            outcomes = detail_service._fetch_outcomes(selected, polite, workers)
    results: list[BodyRefresh] = []
    for document_id, _, title, old in targets:
        outcome = outcomes[document_id]
        if outcome.status != "ok" or outcome.parsed is None:
            reason = outcome.failure_reason or outcome.status
            results.append(BodyRefresh(document_id, title, "failed", reason, old_chars=len(old)))
            continue
        # 和补详情存进去的一样：超长的截断（detail_service._save_result）
        new = outcome.parsed.body_text[:BODY_TEXT_MAX_CHARS]
        item = BodyRefresh(document_id, title, "unchanged", old_chars=len(old), new_chars=len(new))
        if new == old:
            results.append(item)
            continue
        if not new:
            item.result, item.reason = "empty", "新正文为空，不换"
            results.append(item)
            continue
        cut = trimmed_lines(old, new) if old else None
        if cut is not None:
            item.head, item.tail = cut
        if cut is not None and _still_on_page(item.head + item.tail, outcome.fetch):
            item.result = "trimmed"
        else:
            item.result, item.old_start, item.new_start = "changed", old[:60], new[:60]
            if cut is not None:
                item.reason = "去掉的开头结尾有的已不在原网页上（原网页可能改过），要人看"
        item.impact = _impact(session, document_id, old, new, ai_results)
        if apply and (item.result == "trimmed" or include_changed):
            item.reason = _write(session, document_id, old, new) or None
            item.written = item.reason is None
        results.append(item)
    session.commit()
    return results
