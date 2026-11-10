"""每份文件能不能做 AI 解读（D-075）：预先算好存进 ai_eligibility，列表行上的"AI 解读"按钮读它。

判断用的就是详情页和提交任务用的 content.check，这里只是把结果存起来，列表请求就不用
逐份读正文。存的结果可能比实际慢一点；提交任务时 api 仍按原文实时判断，以实时为准，不能做时
不扣次数、写明原因。

原网页撤下了（documents.link_gone_at，youju/link_check.py）不存进表里：存的是不看原网页的结果，
列表读表时（flags、eligible_ids）当场换成 link_gone。原网页检查每天一批、标上和恢复都没有
单独的时间戳可找，这样不用等重算，也不用给表的约束加新代码。

什么时候重算（refresh_changed，admin 每 10 分钟跑一次，看最近 30 分钟，跑重了也没关系）：
- 新收录的文件；
- 正文重新抓取，或者抓取状态变了（DocumentDetail.fetched_at 每次抓都会更新）；
- 人工定了类型（kind_decided_at）；
- 下架或恢复（taken_down_at、restored_at）；
- 合并转载新建了关系（DocumentMerge.created_at）。合并组里任何一份变了，主条目一起重算，
  因为主条目没有自己的正文时用组里最长的那份（v1._reading_body）；
- 还没算过的文件。

没有时间戳、靠上面发现不了的变化：
- 撤回人工定的类型（kind_decided_at 被清空）；
- 合并关系改了主条目或被删掉（reconcile_documents 不留时间）；
- 来源的公开级别（应用里没有修改入口，只能直接改数据库，改完最好跑一次
  `youju ai-eligibility-rebuild`）。
这些靠"轮转"补上：每次另外重算计算时间最早的 ROLLING 份，6 万份大约 10 小时轮一遍；
每天夜里再全量重算一次（refresh_all，02:25，和每 10 分钟的增量错开）。本机实测每份约 3 毫秒、
2 次查询，6 万份约 3 分钟。

同一时间只让一个重算在写（writer_lock，MySQL 命名锁）：增量和全量、包括手工跑的全量，同时写时
先算、后写的旧结果会盖掉新结果。增量碰上锁就跳过这次（下次还看最近 30 分钟），全量最多等 10 分钟。
写入是"有就更新、没有就插入"。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from youju.ai import content, interpret
from youju.models import AIEligibility, Document, DocumentDetail, DocumentMerge

# content.check 里排在"原网页撤下"前面的原因：这类文件本来就不解读，撤没撤下都照这几种说
BEFORE_LINK = ("unavailable", "list", "kind", "source")

RECENT = timedelta(minutes=30)
BATCH = 500
# 一次最多补算这么多份还没算过的，免得刚上线第一次跑太久；剩下的下一次接着补，夜里全量也会算到
MISSING_LIMIT = 2000
# 每次另外重算计算时间最早的这么多份（见模块说明）
ROLLING = 1000
# 全量拿锁最多等这么多秒（增量一般几秒就跑完）
FULL_WAIT = 600


def compute(session: Session, doc_id: int) -> str | None:
    """None 表示能解读；否则是原因代码（models.AI_STORED_REASONS）。不看原网页撤下了没有，
    读表时另看（effective_reason）。"""
    try:
        content.check(session, doc_id, link=False)
    except interpret.InterpretRefusal as exc:
        return exc.code or "unavailable"
    return None


def effective_reason(reason: str | None, link_gone_at: datetime | None) -> str | None:
    """存的结果加上原网页撤下了没有，和 content.check 实时判断的一致。"""
    if link_gone_at is not None and reason not in BEFORE_LINK:
        return "link_gone"
    return reason


@contextmanager
def writer_lock(session: Session, wait: int) -> Iterator[bool]:
    """拿"重算 AI 解读可用性"的锁：拿到给 True，wait 秒内没拿到给 False。MySQL 命名锁（按库名区分，
    同一台数据库上的测试库互不影响），用单独的连接拿，进程退出、连接断开时自动放开。"""
    with session.get_bind().connect() as conn:
        name = f"youju_ai_eligibility:{conn.scalar(text('SELECT DATABASE()'))}"[:64]
        got = conn.scalar(text("SELECT GET_LOCK(:name, :wait)"), {"name": name, "wait": wait}) == 1
        conn.commit()  # 锁跟着连接、不跟事务：不留一个长事务挂着
        try:
            yield got
        finally:
            if got:
                conn.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": name})


def _write(session: Session, rows: list[dict]) -> None:
    """有就更新、没有就插入（MySQL INSERT … ON DUPLICATE KEY UPDATE）：每 10 分钟的增量和夜里的
    全量可能同时补算同一份，按行插入会撞主键、整批回滚。"""
    if not rows:
        return
    stmt = mysql_insert(AIEligibility.__table__).values(rows)
    session.execute(
        stmt.on_duplicate_key_update(
            reason=stmt.inserted.reason, computed_at=stmt.inserted.computed_at
        )
    )


def refresh(
    session: Session, doc_ids: Iterable[int], stamp: datetime, *, release: bool = False
) -> int:
    """重算并写入这些文件，返回结果有变化的份数（新算的也算变化）。每 BATCH 份提交一次；
    release=True（全量重算）时每批提交后清空会话，免得几万份文件和正文都留在内存里。"""
    ids = sorted(set(doc_ids))
    changed = 0
    for start in range(0, len(ids), BATCH):
        chunk = ids[start : start + BATCH]
        existing = dict(
            session.execute(
                select(AIEligibility.document_id, AIEligibility.reason).where(
                    AIEligibility.document_id.in_(chunk)
                )
            ).all()
        )
        present = set(session.scalars(select(Document.id).where(Document.id.in_(chunk))))
        rows = []
        for doc_id in chunk:
            if doc_id not in present:
                continue  # 文件已删除：外键级联会删掉旧结果，这里不插新行
            reason = compute(session, doc_id)
            if doc_id not in existing or existing[doc_id] != reason:
                changed += 1
            rows.append({"document_id": doc_id, "reason": reason, "computed_at": stamp})
        try:
            _write(session, rows)
            session.commit()
        except IntegrityError:
            # 算的途中有文件被删了（外键失败）：重新看哪些还在，只写还在的
            session.rollback()
            still = set(session.scalars(select(Document.id).where(Document.id.in_(chunk))))
            _write(session, [row for row in rows if row["document_id"] in still])
            session.commit()
        if release:
            session.expunge_all()
    return changed


def changed_since(session: Session, since: datetime) -> set[int]:
    """最近有变化、要重算的文件（含合并组的主条目），见模块说明。"""
    ids: set[int] = set(
        session.scalars(
            select(Document.id).where(
                or_(
                    Document.first_seen_at > since,
                    Document.kind_decided_at > since,
                    Document.taken_down_at > since,
                    Document.restored_at > since,
                )
            )
        )
    )
    ids |= set(
        session.scalars(select(DocumentDetail.document_id).where(DocumentDetail.fetched_at > since))
    )
    for canonical_id, duplicate_id in session.execute(
        select(DocumentMerge.canonical_id, DocumentMerge.duplicate_id).where(
            DocumentMerge.created_at > since
        )
    ):
        ids |= {canonical_id, duplicate_id}
    # 组里任何一份变了，主条目要重算（它可能在用这一份的正文）
    if ids:
        ids |= set(
            session.scalars(
                select(DocumentMerge.canonical_id).where(
                    DocumentMerge.duplicate_id.in_(ids), DocumentMerge.active.is_(True)
                )
            )
        )
    return ids


def missing(session: Session, limit: int | None = None) -> list[int]:
    """还没算过的文件，按编号从小到大，最多 limit 份（默认 MISSING_LIMIT）。"""
    limit = MISSING_LIMIT if limit is None else limit
    return list(
        session.scalars(
            select(Document.id)
            .where(~Document.id.in_(select(AIEligibility.document_id)))
            .order_by(Document.id)
            .limit(limit)
        )
    )


def oldest(session: Session, limit: int | None = None) -> list[int]:
    """计算时间最早的 limit 份（轮转重算用，默认 ROLLING）。"""
    limit = ROLLING if limit is None else limit
    return list(
        session.scalars(
            select(AIEligibility.document_id)
            .order_by(AIEligibility.computed_at, AIEligibility.document_id)
            .limit(limit)
        )
    )


def refresh_changed(session: Session, stamp: datetime) -> dict[str, int]:
    ids = changed_since(session, stamp - RECENT)
    todo = ids | set(missing(session)) | set(oldest(session))
    return {"checked": len(todo), "changed": refresh(session, todo, stamp)}


def refresh_all(session: Session, stamp: datetime) -> dict[str, int]:
    ids = list(session.scalars(select(Document.id).order_by(Document.id)))
    return {"checked": len(ids), "changed": refresh(session, ids, stamp, release=True)}


def flags(session: Session, doc_ids: Iterable[int]) -> dict[int, dict]:
    """列表行用：{编号: {"interpret", "clause", "reason"}}。还没算过的不给，小程序就不显示按钮。
    原网页撤下了的当场换成 link_gone（effective_reason）。"""
    ids = list(set(doc_ids))
    if not ids:
        return {}
    out = {}
    for doc_id, stored, link_gone_at in session.execute(
        select(AIEligibility.document_id, AIEligibility.reason, Document.link_gone_at)
        .join(Document, Document.id == AIEligibility.document_id)
        .where(AIEligibility.document_id.in_(ids))
    ):
        reason = effective_reason(stored, link_gone_at)
        out[doc_id] = {"interpret": reason is None, "clause": reason is None, "reason": reason}
    return out


def eligible_ids():
    """能解读的文件编号（子查询），给搜索的"只看能 AI 解读的"用：存的结果能解读、原网页没撤下。
    用别名连 documents，免得和外层查询的 documents 关联到一起。"""
    doc = aliased(Document)
    return (
        select(AIEligibility.document_id)
        .join(doc, doc.id == AIEligibility.document_id)
        .where(AIEligibility.reason.is_(None), doc.link_gone_at.is_(None))
    )
