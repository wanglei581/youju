"""D-130：生成待核对清单，校验证据后写入核对结果。"""

import hashlib
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import select

from youju import deadline_excerpts as excerpts
from youju.models import AICheck, Document, DocumentDeadline, DocumentDetail

AUTO_REVIEWER = "AI 自动（规则与 AI 一致）"
# 自动核对通过的被退回时，在退回人（audit_by）后面加这一句（退回会清掉核对人），
# 统计自动核对错了几条要用
AUTO_REOPEN_TAG = "（原为 AI 自动通过）"


def body_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _without_space(value):
    return re.sub(r"\s+", "", value)


def _body_quote(body, quote):
    positions = [i for i, char in enumerate(body) if not char.isspace()]
    normalized = "".join(body[i] for i in positions)
    needle = _without_space(quote)
    start = normalized.find(needle)
    if start < 0:
        return None
    return body[positions[start] : positions[start + len(needle) - 1] + 1]


# 原句里的时刻：抽取截止时认的写法（extract/deadlines.py 的 _TIME："17:00""17：00""17时"
# "17 时 30 分"）再加上"点"（AI 找到的截止常写"下午5点"）；前面可以带上午、下午、中午、晚上。
# 冒号后面要有两位分钟（带秒的只认整分，"17:00:00"）；"时""点"后面的分钟要带"分"，免得把后面的
# "12月"当成分钟；"5点半"不认。时刻前面可以是"截止时间："这类冒号，不能是数字或"数字加冒号"
# （"17:00:30"里的"00:30"不算一个时刻）
_CLOCK = re.compile(
    r"(?:(?P<period>上午|下午|中午|晚上)\s*)?(?<!\d)(?<!\d[:：])(?P<hour>[01]?\d|2[0-3])\s*"
    r"(?:[:：]\s*(?P<minute>[0-5]\d)(?:[:：](?P<second>[0-5]\d))?\s*分?"
    r"|[时点](?:\s*(?P<cnminute>[0-5]\d)\s*分)?)"
    r"(?![\d半])"
)


class _Clock:
    """把 _CLOCK 的结果交给抽取用的 _clock（它按 hour、minute 两组取值）。"""

    def __init__(self, found):
        self._groups = {
            "hour": found.group("hour"),
            "minute": found.group("minute") or found.group("cnminute"),
        }

    def group(self, name):
        return self._groups[name]


def _has_time(quote, clock):
    """原句里写着这个时刻（换算上午、下午和抽取截止时同一个 _clock）。"""
    from youju.extract.deadlines import _clock

    return any(
        found.group("second") in (None, "00")
        and _clock(_Clock(found), found.group("period")) == clock
        for found in _CLOCK.finditer(quote)
    )


def _reopened_same_deadline(session, row):
    """归并组里同一截止（类型、日期、时间相同；证据句可以不同）被质检退回、还待核对的各条。
    退回时整组同一截止都会退回，页面上也只显示一个，所以退回原因按这一截止整组看。"""
    from youju.dedup import canonical_ids, groups_for

    master = canonical_ids(session, [row.document_id]).get(row.document_id, row.document_id)
    members = groups_for(session, [master])[master]
    return list(
        session.scalars(
            select(DocumentDeadline)
            .where(
                DocumentDeadline.document_id.in_(members),
                DocumentDeadline.kind == row.kind,
                DocumentDeadline.candidate_date == row.candidate_date,
                DocumentDeadline.candidate_time.is_(None)
                if row.candidate_time is None
                else DocumentDeadline.candidate_time == row.candidate_time,
                DocumentDeadline.status == "pending",
                DocumentDeadline.audit_state == "reopened",
            )
            .order_by(DocumentDeadline.id)
            .execution_options(populate_existing=True)
        )
    )


def _reopen_note(row):
    return f"质检退回原因：{row.audit_note}" if row.audit_note else "质检退回（没写原因）"


def queue(session, today, days=7):
    from youju.api import v1
    from youju.classify import effective_kind

    if not 0 <= days <= 7:
        raise ValueError("days 必须在 0 至 7 之间")
    ids = [
        d.id
        for d, _ in session.execute(v1._base())
        if effective_kind(d.title, d.kind_override) == "申报通知"
    ]
    blocked = []
    excerpts.batch(session, ids, v1._approved(session, ids), today, blocked_soon=blocked)
    result = []
    for row in {r.id: r for r in blocked}.values():
        # batch 判"7 天内"时这一类只有一个日期、在今天到 7 天后之间；这里再按 --days 收窄
        if not today <= row.candidate_date <= today + timedelta(days=days):
            continue
        doc = session.get(Document, row.document_id)
        detail = session.get(DocumentDetail, row.document_id)
        if not detail or detail.status != "ok":
            continue
        body = detail.body_text or ""
        digest = body_hash(body)
        if digest == row.verification_unclear_hash:
            continue
        # 同一截止同组里有退回的：每条都带上退回原因，核哪一条都看得到
        reopened = _reopened_same_deadline(session, row)
        sentences = [
            s.strip()[:300]
            for s in re.split(r"(?<=[。！？；;\n])", body)
            if excerpts.date_in_quote(row.candidate_date, s)
        ][:5]
        result.append(
            dict(
                id=row.id,
                document_id=doc.id,
                title=doc.title,
                url=doc.url,
                kind=row.kind,
                date=row.candidate_date.isoformat(),
                time=row.candidate_time.isoformat(timespec="minutes")
                if row.candidate_time
                else None,
                evidence=row.evidence,
                origin=row.origin,
                ai_agrees=row.ai_agrees,
                reopened=bool(reopened),
                # 退回原因：audit_notes 是逐条的（写回时按它比），audit_note 是连成一句给人看的
                audit_notes=[_reopen_note(r) for r in reopened],
                audit_note="；".join(_reopen_note(r) for r in reopened) or None,
                sentences=sentences,
                body_sha256=digest,
            )
        )
    return sorted(result, key=lambda r: (r["date"], r["time"] or "", r["id"]))


def apply_one(session, item, write=False, reviewer="Claude", precheck=None):
    """逐条处理；写入调用方负责提交。"""
    from youju.api.app import _apply_review, _lock_candidates

    if not isinstance(item, dict) or type(item.get("id")) is not int:
        return "格式错误：id 必须是整数"
    if set(item) - {
        "id",
        "verdict",
        "quote",
        "date",
        "time",
        "note",
        "body_sha256",
        "document_id",
        "title",
        "url",
        "kind",
        "evidence",
        "origin",
        "ai_agrees",
        "sentences",
        "reopened",
        "audit_note",
        "audit_notes",
    }:
        return "格式错误：含不支持的字段"
    verdict = item.get("verdict")
    if verdict not in ("confirmed", "corrected", "unclear"):
        return "verdict 无效"
    if not isinstance(item.get("note", ""), str):
        return "note 必须是字符串"
    row = session.get(DocumentDeadline, item["id"])
    if row is None:
        return "截止不存在"
    locked = _lock_candidates(session, row)
    if row.status != "pending":
        return "状态已变，不再待核对"
    if precheck is not None:
        reason = precheck(row)
        if reason:
            return reason
    reopened = _reopened_same_deadline(session, row) if verdict != "unclear" else []
    if reopened:
        # 结果要来自带着当前退回原因的清单：reopened 为 true，现在整组的每条退回原因都在
        # audit_notes 里；出清单以后又有退回的，等下一份清单。还要在 note 里写怎么处理了退回原因，
        # 记进留痕
        if item.get("reopened") is not True:
            return "这一截止同组有质检退回，须按带退回原因的清单核对"
        # 清单逐条给的退回原因（audit_notes）里，现在的每条退回原因都要有，条数也不能少
        # （出清单以后同一截止又有一条退回、原因字样相同，也拦得住）；逐条整句比，不拆、
        # 不按包含比。同一截止另一条已在这份结果里先处理掉的，不再要求
        shown = item.get("audit_notes")
        if not isinstance(shown, list) or not all(isinstance(x, str) for x in shown):
            return "退回原因和清单里的不一样（出清单以后又有退回），等下一份清单"
        have = Counter(shown)
        if any(have[note] < count for note, count in Counter(map(_reopen_note, reopened)).items()):
            return "退回原因和清单里的不一样（出清单以后又有退回），等下一份清单"
        if not item.get("note", "").strip():
            return "同组有质检退回，须在 note 里写怎么处理了退回原因"
    if row.kind not in excerpts.DEADLINE_KINDS and verdict != "unclear":
        return "其他类不能通过"
    stmt = select(DocumentDetail).where(DocumentDetail.document_id == row.document_id)
    # 读最新的正文，别用会话里上一条留下的旧值（--apply 每条提交，会话不过期）
    detail = session.scalar(
        (stmt.with_for_update() if write else stmt).execution_options(populate_existing=True)
    )
    if detail is None or detail.status != "ok":
        return "当前正文不可用"
    body = detail.body_text or ""
    if item.get("body_sha256") != body_hash(body):
        return "正文指纹不一致"
    if verdict == "unclear":
        if not item.get("note", "").strip():
            return "看不准须说明原因"
        if write:
            row.verification_unclear_hash = body_hash(body)
            row.verification_unclear_note = item["note"].strip()
            row.verification_unclear_at = datetime.now(UTC)
        return "记录看不准"
    quote = item.get("quote")
    if not isinstance(quote, str) or not _without_space(quote):
        return "缺少 quote"
    if not 12 <= len(_without_space(quote)) <= 300:
        return "quote 去空白后须为 12 至 300 字"
    quote = _body_quote(body, quote)
    if quote is None:
        return "quote 不在当前正文"
    day, clock = row.candidate_date, row.candidate_time
    if verdict == "corrected":
        try:
            if "time" not in item:
                raise ValueError
            day = date.fromisoformat(item["date"])
            clock = time.fromisoformat(item["time"]) if item["time"] is not None else None
            if clock and (clock.tzinfo or clock.second or clock.microsecond):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            return "corrected 须提供有效 date 和 time（可为 null）"
        if (day, clock) == (row.candidate_date, row.candidate_time):
            return "corrected 的日期时间和候选一样，应写 confirmed"
    if not excerpts.date_in_quote(day, quote):
        return "quote 里的日期不匹配"
    if clock is not None and not _has_time(quote, clock):
        return "原句里没有时间"
    # 同组副本也会公开；只有每份当前正文都包含这句证据才一起通过。
    twin_quotes = {row.id: quote}
    for twin in locked:
        if twin.id == row.id or twin.status != "pending":
            continue
        twin_detail = session.scalar(
            select(DocumentDetail)
            .where(DocumentDetail.document_id == twin.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        twin_quote = (
            _body_quote(twin_detail.body_text or "", quote)
            if twin_detail and twin_detail.status == "ok"
            else None
        )
        if twin_quote is None:
            return "同组副本正文不含 quote，未写入"
        twin_quotes[twin.id] = twin_quote
    # 正文都锁住、证据都核过以后、写入之前再判断一次（等锁期间可能跨过截止时刻，D-135）
    if precheck is not None:
        reason = precheck(row)
        if reason:
            return reason
    if write:
        stamp = datetime.now(UTC)
        # _apply_review 会清空质检信息；同组每条各自保存自己的退回原因。
        # _apply_review 会清掉质检标记；退回过的各自留一笔（没写原因的也留）
        handled = item.get("note", "").strip()
        reopen_notes = {
            d.id: f"{_reopen_note(d)}；Claude 处理：{handled}" if handled else _reopen_note(d)
            for d in locked
            if d.status == "pending" and d.audit_state == "reopened"
        }
        _apply_review(row, locked, "approved", reviewer, stamp, day, clock)
        for item_row in locked:
            if item_row.reviewed_by == reviewer and item_row.reviewed_at == stamp:
                item_row.verification_quote = twin_quotes[item_row.id]
                item_row.verification_corrected = verdict == "corrected"
                if item_row.id in reopen_notes:
                    item_row.verification_unclear_note = reopen_notes[item_row.id]
    return "通过并修改日期" if verdict == "corrected" else "通过"


def _ai_items(session, members):
    """同组（members）当前版本（RESULT_VERSION）、没出错的 AI 截止结果里标为企业申报截止的各项：
    (文件, 原项, 日期, 时刻)；日期、时刻写法不对的跳过。"""
    from youju.ai.deadlines import RESULT_VERSION, stored_items, stored_version

    checks = session.scalars(
        select(AICheck)
        .where(AICheck.document_id.in_(members), AICheck.task == "deadlines")
        .execution_options(populate_existing=True)
    )
    for check in checks:
        if check.error or stored_version(check.result) != RESULT_VERSION:
            continue
        for item in stored_items(check.result):
            if not isinstance(item, dict) or item.get("kind") != "企业申报截止":
                continue
            try:
                day = date.fromisoformat(item["date"])
                clock = time.fromisoformat(item["time"]) if item.get("time") else None
            except (ValueError, TypeError, KeyError):
                continue
            yield check.document_id, item, day, clock


def _ai_supports(session, row, members):
    """同组有 AI 结果标的也是企业申报截止、和这条同一天同一时刻，而且 AI 的原句去掉空白后在它那份
    文件的当前正文里、写着这个日期（写了时刻的也写着这个时刻）。"""
    from youju.extract.deadlines import same_moment

    for document_id, item, day, clock in _ai_items(session, members):
        if not same_moment(row.candidate_date, row.candidate_time, day, clock):
            continue
        detail = session.scalar(
            select(DocumentDetail)
            .where(DocumentDetail.document_id == document_id)
            .execution_options(populate_existing=True)
        )
        quote = item.get("evidence")
        if (
            detail
            and detail.status == "ok"
            and isinstance(quote, str)
            and _without_space(quote)
            and _without_space(quote) in _without_space(detail.body_text or "")
            and excerpts.date_in_quote(day, quote)
            and (clock is None or _has_time(quote, clock))
        ):
            return True
    return False


def _lock_kind(session, row, members):
    """D-135：先按编号顺序锁住同一归并组（members）里同一类型的全部截止行，锁着判断、写入：判断以后
    别人退回、标看不准、通过了这一类、加进别的时刻的候选，都要等这一条做完。和管理台、Claude 写回
    一样先拿审阅队列的锁（开了纠错奖励时才有），再按编号锁行，锁的次序一致，不会互相等成死锁。
    只在写库时锁；演练不锁。"""
    from youju import deadline_clues as clues
    from youju.config import get_settings

    clues.lock_review_queue(session, settings=get_settings())
    list(
        session.scalars(
            select(DocumentDeadline)
            .where(DocumentDeadline.document_id.in_(members), DocumentDeadline.kind == row.kind)
            .order_by(DocumentDeadline.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _auto_reason(session, row, members, now):
    """D-135 自动核对除证据校验（apply_one）以外的条件：都满足返回 None，否则返回没通过的原因。
    调 apply_one 前判断一次；apply_one 锁住同组同一候选、刷新以后再判断一次（precheck），按最新的
    数据和当时的时间判断。now 是北京时间（带时区的会换算）。"同一天"指同一归并组（members）里
    同一类型、同一日期：
    - 同组这一类已经有核对通过或标了变更的，不再自动通过（对外每一类只显示一个核对过的）；
    - 同一天有质检退回的、有人对当前正文标过看不准的，都不自动通过（apply_one 会把同证据副本一起
      通过并清掉看不准；退回的不挡同一天别的时刻，这一类一旦核过，退回那条就从清单里消失了）；
    - 同一天待核对候选和 AI 结果的时刻不止一种（没写时刻也算一种）的不通过，免得丢掉"17:00"。"""
    local_now = now.astimezone(timezone(timedelta(hours=8))) if now.tzinfo else now
    if row.origin != "rule":
        return "不是规则候选"
    if row.ai_agrees is not True:
        return "规则和 AI 未一致"
    if row.kind != "企业申报截止":
        return "不是企业申报截止"
    if row.candidate_date < local_now.date() or (
        row.candidate_date == local_now.date()
        and row.candidate_time
        and row.candidate_time <= local_now.time().replace(tzinfo=None)
    ):
        return "截止已过"
    same_kind = list(
        session.scalars(
            select(DocumentDeadline)
            .where(DocumentDeadline.document_id.in_(members), DocumentDeadline.kind == row.kind)
            .execution_options(populate_existing=True)
        )
    )
    if any(other.status in ("approved", "changed") for other in same_kind):
        return "这一类已有核对结果"
    candidates = [
        other
        for other in same_kind
        if other.status == "pending" and other.candidate_date == row.candidate_date
    ]
    if any(candidate.audit_state == "reopened" for candidate in candidates):
        return "同一截止有质检退回，须人工核对"
    for candidate in candidates:
        if candidate.verification_unclear_hash:
            detail = session.scalar(
                select(DocumentDetail)
                .where(DocumentDetail.document_id == candidate.document_id)
                .execution_options(populate_existing=True)
            )
            if (
                detail
                and detail.status == "ok"
                and candidate.verification_unclear_hash == body_hash(detail.body_text or "")
            ):
                return "同一截止有人标过看不准"
    clocks = {candidate.candidate_time for candidate in candidates}
    clocks.update(
        clock for _, _, day, clock in _ai_items(session, members) if day == row.candidate_date
    )
    if len(clocks) > 1:
        return "同一天有几个不同的时刻"
    if not _ai_supports(session, row, members):
        return "没有符合条件的 AI 证据"
    return None


def auto_twin_quote(session, row):
    """D-135：AI 自动通过的结果沿用给同组副本（dedup.sync_reviewed_twins）前，按 apply_one 同一套
    证据规则核这条副本自己的原句：去掉空白后 12–300 字、在它自己的当前正文里、写着这个日期（有时刻
    的也写着这个时刻），而且它没被标过对当前正文的看不准。行就返回正文里原样的那段（记成它自己的
    verification_quote），不行返回 None（留在待核对）。自动核对不像人核过整份通知，所以不凭"同一份
    通知"直接沿用。"""
    detail = session.scalar(
        select(DocumentDetail)
        .where(DocumentDetail.document_id == row.document_id)
        .execution_options(populate_existing=True)
    )
    if detail is None or detail.status != "ok":
        return None
    body = detail.body_text or ""
    if row.verification_unclear_hash and row.verification_unclear_hash == body_hash(body):
        return None
    if not 12 <= len(_without_space(row.evidence or "")) <= 300:
        return None
    quote = _body_quote(body, row.evidence)
    if quote is None or not excerpts.date_in_quote(row.candidate_date, quote):
        return None
    if row.candidate_time is not None and not _has_time(quote, row.candidate_time):
        return None
    return quote


def auto_switch(value):
    """D-135 自动核对的开关（YOUJU_DEADLINE_AUTO_VERIFY）：只认 "0" 和 "1"，没配（空）当 "0"。
    返回 True 开、False 关；None 是写成了别的值，调用方按关处理并记一行（round.sh 同一个规则）。"""
    if value in (None, "", "0"):
        return False
    if value == "1":
        return True
    return None


def auto_verify(session, today, now, write=False, clock=None):
    """D-135：核对清单（queue，7 天内）里同时满足下面几条的，按 apply_one 同一套校验写成核对通过，
    核对人记 AUTO_REVIEWER；不满足的不动，照旧显示"截止时间见原文"。
    - 规则候选（origin=rule），AI 也找到同一天同一时刻（ai_agrees），类型是企业申报截止；
    - AI 给这一天标的也是企业申报截止，AI 的原句在当前正文里、写着这个日期和时刻（_ai_supports）；
    - 没过期（是今天的，没写时刻或时刻还没到，北京时间）；
    - 同组这一类还没有核对过的结果；同一天没有质检退回的（退回原因要人看）、没有人对当前正文标过
      看不准的，待核对候选和 AI 结果的时刻只有一种（_auto_reason）；
    - 规则的原句（evidence）通过 apply_one 的校验：去掉空白后在当前正文里、写着这个日期和时刻，
      同组副本正文也都有这句。
    同一截止（同一通知、类型、日期、时刻）有几条规则候选时，前一条通过了，后面的不再核（由
    sync_reviewed_twins 按 auto_twin_quote 核过再沿用）；前一条没通过，接着试下一条。
    写库时每一条先锁住同组这一类的全部截止行（_lock_kind）再判断、写入。clock：取当前时间的函数
    （命令传 v1.now），每条判断时重新取，跑的过程中跨过截止时刻的不放；不给就一直用 now。
    逐条返回结果；每条单独提交（write）或回滚（演练），演练不留下修改。"""
    from youju.dedup import canonical_ids, groups_for

    items = sorted(queue(session, today, 7), key=lambda item: item["id"])
    masters = canonical_ids(session, [item["document_id"] for item in items])
    master_ids = {masters.get(item["document_id"], item["document_id"]) for item in items}
    groups = groups_for(session, list(master_ids)) if items else {}
    passed = set()

    def current():
        return clock() if clock is not None else now

    for item in items:
        row = session.get(DocumentDeadline, item["id"])
        master = masters.get(item["document_id"], item["document_id"])
        key = (master, item["kind"], item["date"], item["time"])
        members = groups.get(master, [master])
        if row is not None and write:
            _lock_kind(session, row, members)
        if row is None:
            result = "截止不存在"
        elif key in passed:
            result = "同一截止已由较早的候选通过"
        elif reason := _auto_reason(session, row, members, current()):
            result = reason
        else:
            result = apply_one(
                session,
                {
                    "id": item["id"],
                    "verdict": "confirmed",
                    "quote": item["evidence"],
                    "body_sha256": item["body_sha256"],
                },
                write=write,
                reviewer=AUTO_REVIEWER,
                precheck=lambda r, members=members: _auto_reason(session, r, members, current()),
            )
        if result == "通过":
            passed.add(key)
        if write:
            session.commit()
        else:
            session.rollback()
        yield {k: item[k] for k in ("id", "document_id", "kind", "date", "time")} | {
            "result": result
        }


def stats(session, since=None):
    """统计现存状态；同次通过的同组同候选及后来沿用的副本只计一次。"""
    from youju.dedup import canonical_ids

    stmt = (
        select(DocumentDeadline)
        .where(
            ((DocumentDeadline.status == "approved") & (DocumentDeadline.reviewed_by == "Claude"))
            | (
                (DocumentDeadline.status == "pending")
                & DocumentDeadline.verification_unclear_at.is_not(None)
            )
        )
        .order_by(DocumentDeadline.id)
    )
    rows = list(session.scalars(stmt))
    masters = canonical_ids(session, list({row.document_id for row in rows}))
    groups = {}
    for row in rows:
        unclear = row.status == "pending"
        stamp = row.verification_unclear_at if unclear else row.reviewed_at
        if since and (
            stamp is None
            or stamp
            < datetime.combine(since, time.min, timezone(timedelta(hours=8))).astimezone(UTC)
        ):
            continue
        verdict = (
            "unclear" if unclear else "corrected" if row.verification_corrected else "confirmed"
        )
        # 看不准只写一条；通过的副本沿用同一时间、类型、候选日期和时间。
        key = (
            (row.id,)
            if unclear or stamp is None
            else (
                masters.get(row.document_id, row.document_id),
                stamp,
                row.kind,
                row.candidate_date,
                row.candidate_time,
                verdict,
            )
        )
        groups.setdefault(key, []).append(row)
    result = {
        name: {"confirmed": 0, "corrected": 0, "unclear": 0}
        for name in ("total", "ai_agrees", "other")
    }
    for group in groups.values():
        row = group[0]
        verdict = (
            "unclear"
            if row.status == "pending"
            else "corrected"
            if row.verification_corrected
            else "confirmed"
        )
        bucket = "ai_agrees" if all(r.ai_agrees is True for r in group) else "other"
        result["total"][verdict] += 1
        result[bucket][verdict] += 1
    for counts in result.values():
        n = counts["confirmed"] + counts["corrected"]
        counts["accuracy"] = counts["confirmed"] / n if n else None
    auto_rows = list(
        session.scalars(
            select(DocumentDeadline)
            .where(
                (
                    (DocumentDeadline.status == "approved")
                    & (DocumentDeadline.reviewed_by == AUTO_REVIEWER)
                )
                | (
                    (DocumentDeadline.status == "pending")
                    & (DocumentDeadline.audit_state == "reopened")
                    & DocumentDeadline.audit_by.endswith(AUTO_REOPEN_TAG, autoescape=True)
                )
            )
            .order_by(DocumentDeadline.id)
        )
    )
    auto_masters = canonical_ids(session, [r.document_id for r in auto_rows])
    auto_groups = {}
    for row in auto_rows:
        stamp = row.reviewed_at if row.status == "approved" else row.audit_at
        if since and (
            stamp is None
            or stamp
            < datetime.combine(since, time.min, timezone(timedelta(hours=8))).astimezone(UTC)
        ):
            continue
        key = (
            auto_masters.get(row.document_id, row.document_id),
            stamp,
            row.kind,
            row.candidate_date,
            row.candidate_time,
            row.status,
        )
        auto_groups.setdefault(key, []).append(row)
    result["auto"] = {
        "approved": sum(g[0].status == "approved" for g in auto_groups.values()),
        "spotchecked": sum(
            g[0].status == "approved"
            and any((r.audit_note or "").startswith("Codex 抽查一致") for r in g)
            for g in auto_groups.values()
        ),
        "reopened": sum(g[0].status == "pending" for g in auto_groups.values()),
    }
    return result


_AUDIT_NOTE_MAX = 200  # document_deadlines.audit_note 的长度


def reopen_group(group, note, reviewer, stamp):
    """调用方先按管理台口径锁定同组，并检查目标仍为 approved。
    AI 自动核对通过的（D-135）在 audit_by 末尾加 AUTO_REOPEN_TAG：退回会清掉核对人，不注明就查不出
    自动核对错了几条（Codex 抽查退回、管理台退回都走这里）；退回原因 audit_note 照原样写。"""
    count = 0
    for row in group:
        if row.status != "approved":
            continue
        audit_by = (
            reviewer[: 80 - len(AUTO_REOPEN_TAG)] + AUTO_REOPEN_TAG
            if row.reviewed_by == AUTO_REVIEWER
            else reviewer
        )
        row.status = "pending"
        row.reviewed_by = row.reviewed_at = None
        row.approved_date = row.approved_time = None
        row.audit_state, row.audit_note = "reopened", note
        row.audit_by, row.audit_at = audit_by, stamp
        count += 1
    return count


def _verified_groups(session, since, until):
    from youju.dedup import canonical_ids

    rows = list(
        session.scalars(
            select(DocumentDeadline)
            .where(
                DocumentDeadline.status == "approved",
                DocumentDeadline.reviewed_by.in_(("Claude", AUTO_REVIEWER)),
                DocumentDeadline.reviewed_at >= since,
                DocumentDeadline.reviewed_at <= until,
            )
            .order_by(DocumentDeadline.id)
        )
    )
    masters = canonical_ids(session, [r.document_id for r in rows])
    groups = {}
    for r in rows:
        key = (
            masters.get(r.document_id, r.document_id),
            r.reviewed_at,
            r.reviewed_by,
            r.kind,
            r.candidate_date,
            r.candidate_time,
            bool(r.verification_corrected),
        )
        groups.setdefault(key, []).append(r)
    return list(groups.values())


# 抽查清单里写着截止一类字样的句子排前面；句数、总字数有上限，截掉了标 truncated
_DEADLINE_CUE = re.compile(r"截止|截至|之前|前|止|期限|提交|报送|申报|受理|报名")
_SAMPLE_SENTENCES = 20
_SAMPLE_CHARS = 4000


def _has_date(sentence):
    """句子里写着日期：和核对时一样先规整（全角、空白）再找；月 1–12、日 1–31；
    "1.5万元""第2.1条"这类数字不算。"""
    normal = excerpts._normal(sentence)
    for found in excerpts.DATE_IN_QUOTE.finditer(normal):
        month, dom = found.group(2, 3) if found.group(2) else found.group(5, 6)
        if not (1 <= int(month) <= 12 and 1 <= int(dom) <= 31):
            continue
        after = normal[found.end() : found.end() + 1]
        if not found.group(2) and after and after in "万元%条款项":
            continue
        return True
    return False


_SENTENCE_CHARS = 600


def _date_lines(body):
    """正文里写着日期的句子。日期写在一行、时刻折到下一行（"2026年11月15日\n17:00前提交"）的，
    把下一行并进来，免得清单里只剩日期、看不到时刻。"""
    lines = []
    for part in (s.strip() for s in re.split(r"(?<=[。！？；;\n])", body)):
        if not part:
            continue
        if (
            lines
            and lines[-1][1]
            and lines[-1][0][-1] not in "。！？；;"
            and not _has_date(part)
            and _CLOCK.search(part)
        ):
            lines[-1] = (f"{lines[-1][0]} {part}", True)
            continue
        lines.append((part, _has_date(part)))
    return [text for text, dated in lines if dated]


def _clip(text):
    """长句截到 600 字：从第一个截止一类字样前一点开始截，免得截止日期落在截掉的后半句里。"""
    if len(text) <= _SENTENCE_CHARS:
        return text
    cue = _DEADLINE_CUE.search(text)
    start = max(0, cue.start() - 200) if cue else 0
    return text[start : start + _SENTENCE_CHARS]


def _sample_item(session, row):
    doc = session.get(Document, row.document_id)
    detail = session.get(DocumentDetail, row.document_id)
    if not doc or not detail or detail.status != "ok":
        return None
    body = detail.body_text or ""
    found = [_clip(text) for text in _date_lines(body)]
    ordered = [s for s in found if _DEADLINE_CUE.search(s)]
    ordered += [s for s in found if not _DEADLINE_CUE.search(s)]
    sentences, total = [], 0
    for sentence in ordered:
        if len(sentences) >= _SAMPLE_SENTENCES or total + len(sentence) > _SAMPLE_CHARS:
            break
        sentences.append(sentence)
        total += len(sentence)
    return dict(
        id=row.id,
        reviewed_by=row.reviewed_by,
        title=doc.title,
        url=doc.url,
        kind=row.kind,
        publish_date=doc.publish_date.isoformat() if doc.publish_date else None,
        sentences=sentences,
        truncated=len(sentences) < len(found),
        body_sha256=body_hash(body),
    )


def sample(session, days=7, rate=0.05, minimum=5, seed=None, now=None):
    import math
    import random

    if days < 0 or not 0 <= rate <= 1 or minimum < 0:
        raise ValueError("days、min 须非负，rate 须在 0 至 1 之间")
    now = now or datetime.now(UTC)
    groups = _verified_groups(session, now - timedelta(days=days), now)
    # 先选组再读取正文；不可用的组保留提示，避免悄悄改变抽样概率。
    rng = random.Random(seed)
    chosen = []
    for reviewer in ("Claude", AUTO_REVIEWER):
        stratum = [group for group in groups if group[0].reviewed_by == reviewer]
        chosen.extend(
            rng.sample(stratum, min(len(stratum), max(minimum, math.ceil(len(stratum) * rate))))
        )
    return [
        _sample_item(session, group[0])
        or {"id": group[0].id, "reviewed_by": group[0].reviewed_by, "error": "当前正文不可用"}
        for group in chosen
    ]


def spotcheck(session, item, write=False, now=None):
    from youju.api.app import _lock_group

    result = {"id": item.get("id") if isinstance(item, dict) else None, "follow_up": []}

    def done(message):
        result["result"] = message
        return result

    if not isinstance(item, dict) or type(item.get("id")) is not int:
        return done("格式错误：id 必须是整数")
    if not {"date", "time", "quote", "note", "body_sha256"} <= item.keys() or not isinstance(
        item["note"], str
    ):
        return done("格式错误：缺少字段或 note 不是字符串")
    try:
        day = date.fromisoformat(item["date"]) if item["date"] is not None else None
        clock = time.fromisoformat(item["time"]) if item["time"] is not None else None
        if clock and (clock.tzinfo or clock.second or clock.microsecond):
            raise ValueError
        if day is None and clock is not None:
            raise ValueError
    except (ValueError, TypeError):
        return done("date 或 time 无效")
    row = session.get(DocumentDeadline, item["id"])
    if row is None:
        return done("截止不存在")
    group = _lock_group(session, row)
    if row.status != "approved" or row.reviewed_by not in ("Claude", AUTO_REVIEWER):
        return done("状态或核对人已变")
    stmt = select(DocumentDetail).where(DocumentDetail.document_id == row.document_id)
    detail = session.scalar(
        (stmt.with_for_update() if write else stmt).execution_options(populate_existing=True)
    )
    if (
        not detail
        or detail.status != "ok"
        or item["body_sha256"] != body_hash(detail.body_text or "")
    ):
        return done("正文指纹不一致或正文不可用")
    body = detail.body_text or ""
    quote = item["quote"]
    if day is not None:
        # 和 Claude 写回一样按去掉空白后比（全角空格、换行抄得不一样也认），记正文里的原样那段
        quote = _body_quote(body, quote) if isinstance(quote, str) and quote.strip() else None
        if quote is None:
            return done("quote 须出现在当前正文")
        if not excerpts.date_in_quote(day, quote) or (
            clock is not None and not _has_time(quote, clock)
        ):
            return done("quote 与日期或时间不匹配")
    else:
        # 没找到这一类截止：没有可核的原句，quote 不看（写了说明也不当证据），照样退回；
        # 但要在 note 里写明找过哪里，免得没填完的占位行把对的整组退回
        if not item["note"].strip():
            return done("没找到时要在 note 里写明找过哪里、为什么没找到")
        quote = None
    now = now or datetime.now(UTC)
    if day is not None and (day, clock) == (row.approved_date, row.approved_time):
        if row.audit_state == "flagged":
            return done("已有质检存疑，未覆盖")
        if write:
            # 只留痕，不改 audit_state：每天的质检只查 audit_state 为空的，标成"复核无误"就
            # 永远不再查了（以后发了延期通知也查不出来）。同组同一截止各条都记；质检存疑的
            # 不碰，免得冲掉存疑原因。audit_at 记上，AI 抽查 30 天内不重复抽这条
            note = ("Codex 抽查一致" + (f"：{item['note']}" if item["note"] else ""))[
                :_AUDIT_NOTE_MAX
            ]
            for same in group:
                if same.status == "approved" and same.audit_state is None:
                    same.audit_by, same.audit_at, same.audit_note = "Codex", now, note
        return done("抽查一致")
    doc = session.get(Document, row.document_id)
    if not doc:
        return done("文件不存在")
    for other in _verified_groups(session, now - timedelta(days=30), now):
        for candidate in other:
            other_doc = session.get(Document, candidate.document_id)
            if (
                candidate.id not in {x.id for x in group}
                and candidate.kind == row.kind
                and other_doc
                and other_doc.source_id == doc.source_id
            ):
                entry = _sample_item(session, candidate)
                if entry:
                    result["follow_up"].append(entry)
                    break  # 这一组有一条能给就够了；正文不可用的接着试组里别的副本
    # 原来同组有质检存疑的，退回原因带上存疑原因（和管理台退回一样，不丢）；原句先截到 100 字，
    # 免得 200 字的上限把存疑原因截掉
    flagged = next((x.audit_note for x in group if x.audit_state == "flagged" and x.audit_note), "")
    shown = (quote[:100] + "…") if quote and len(quote) > 100 else quote
    reason = f"Codex 抽查不一致：原文{shown or '未找到这一类截止日期'}"
    if flagged:
        reason += f"；原质检存疑：{flagged}"
    if item["note"].strip():
        reason += f"；{item['note'].strip()}"
    if write:
        # audit_note 最长 200 字：原句长时截断（第二天 Claude 的清单里带的就是这段原因）
        reopen_group(group, reason[:_AUDIT_NOTE_MAX], "Codex", now)
    return done("抽查不一致，退回待核对")
