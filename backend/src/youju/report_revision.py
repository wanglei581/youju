"""公开报告的数据库聚合版本，兼容管理端和 API 分属不同进程。"""

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from youju.models import Document, DocumentDeadline, DocumentMerge, ListEntry, ListParse, Source
from youju.takedown import cache_revision


def _checksum(*columns):
    # CONCAT_WS 会跳过 NULL；补空串以保留字段位置，避免日期/时刻被清空后指纹串位。
    return func.coalesce(
        func.sum(func.crc32(func.concat_ws("|", *(func.coalesce(c, "") for c in columns)))),
        0,
    )


def publication_revision(session: Session, kind: str) -> tuple:
    """每类固定三条聚合 SQL（含下架版本），每条只返回一行。"""
    # 撤回会清空 reviewed_at，MAX(时间) 会漏掉非最新记录的变化；校验所有发布状态。
    # 标题、发文机关、文号、正文和普通归并变化较少，交给现有 300 秒 TTL 兜底，
    # 不为缓存命中读取正文、document_details 或 list_entries。
    if kind == "deadlines":
        query = select(
            func.count(),
            _checksum(
                DocumentDeadline.id,
                DocumentDeadline.document_id,
                DocumentDeadline.status,
                DocumentDeadline.approved_date,
                DocumentDeadline.approved_time,
                DocumentDeadline.kind,
                DocumentDeadline.audit_state,
                DocumentDeadline.changed_to_document_id,
                DocumentDeadline.change_reason,
            ),
        ).select_from(DocumentDeadline)
    elif kind == "lists":
        query = select(
            func.count(),
            func.coalesce(func.sum(ListParse.entry_count), 0),
            func.max(ListParse.attempted_at),
            # 同条数重解析较旧的一份也要失效，不能只看全表 MAX(attempted_at)。
            _checksum(
                ListParse.id,
                ListParse.document_id,
                ListParse.status,
                ListParse.entry_count,
                ListParse.attempted_at,
            ),
        ).select_from(ListParse)
    else:
        raise ValueError(f"不支持的报告类型：{kind}")
    published = tuple(session.execute(query).one())
    overrides = tuple(
        session.execute(
            select(func.count(), _checksum(Document.id, Document.kind_override)).where(
                Document.kind_override.is_not(None)
            )
        ).one()
    )
    return published, overrides, cache_revision(session)


def list_count_revision(session: Session) -> tuple:
    """名单条数（每份名单去重后的企业数）另外依赖的数据的版本，一条 SQL、只返回一行。

    名单解析、人工定类型和下架已在 publication_revision(session, "lists") 里；这里补上那边交给
    300 秒兜底的两类——全部归并关系（含停用的）和来源的公开级别——再加名单条目的条数和最大编号：
    程序里写条目的地方（重新解析、清掉无效条目、清掉不再算名单的文件）都同时改解析记录，
    条数和编号是给直接改库加删条目时用的（直接改库改名字靠调用方的 1 小时兜底）。"""
    merges = select(
        func.count().label("count"),
        _checksum(
            DocumentMerge.duplicate_id, DocumentMerge.canonical_id, DocumentMerge.active
        ).label("checksum"),
    ).subquery()
    sources = select(
        func.count().label("count"), _checksum(Source.id, Source.rights_level).label("checksum")
    ).subquery()
    # 条目两项用标量子查询：再多连一张派生表 SQLAlchemy 会当成笛卡尔积告警
    entry_count = select(func.count()).select_from(ListEntry).scalar_subquery()
    latest_entry = select(func.max(ListEntry.id)).scalar_subquery()
    return tuple(
        session.execute(
            select(
                merges.c.count,
                merges.c.checksum,
                sources.c.count,
                sources.c.checksum,
                entry_count,
                latest_entry,
            )
            .select_from(merges)
            .join(sources, true())
        ).one()
    )
