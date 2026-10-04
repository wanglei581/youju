"""公开报告的数据库聚合版本，兼容管理端和 API 分属不同进程。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youju.models import Document, DocumentDeadline, ListParse
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
