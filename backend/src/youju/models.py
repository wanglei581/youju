"""数据模型 v1：来源、检查记录、文件题录。设计说明见 docs/adr/0003-数据模型v1.md。"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    """一个被监测的网站栏目（通常是某部门的"通知公告"列表页）。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))  # 国家级 / 省级 / 市级
    province: Mapped[str] = mapped_column(String(32))  # 国家级来源填"国家"
    dept: Mapped[str] = mapped_column(String(32))  # 部门简称：科技、工信、发改委……
    dept_full_name: Mapped[str] = mapped_column(String(128))
    column_name: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text, unique=True)

    # static：直接抓取；browser：需浏览器渲染；manual：需人工确认或暂不可访问
    fetch_mode: Mapped[str] = mapped_column(String(16), default="static")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 按栏目覆盖通用识别规则，如 {"item_selector": "ul.list li", "require_date": false}
    parser_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 1：法定公文、公示名单等，可存全文和导出；2：只存题录；3：按授权（见总方案 §4.5）
    rights_level: Mapped[int] = mapped_column(Integer, default=1)
    verification_status: Mapped[str | None] = mapped_column(String(64))
    verified_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)

    # 健康：unknown / ok / degraded / failing / blocked
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    checks: Mapped[list["SourceCheck"]] = relationship(back_populates="source")


class SourceCheck(Base):
    """对某个来源的一次检查（抓取一次列表页）。失败也要记录，前台据此显示"截至上次成功检查"。"""

    __tablename__ = "source_checks"
    __table_args__ = (Index("ix_source_checks_source_started", "source_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # ok / http_error / network_error / ssl_error / robots_disallowed / robots_error / parse_empty
    status: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    # 该来源第一次成功检查：本次新入库的条目都是基线，不计入"新增"
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[Source] = relationship(back_populates="checks")


class Document(Base):
    """一份被发现的文件（题录层）。同一份文件（规范网址键相同）全库只存一条。"""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_publish_date", "publish_date"),
        Index("ix_documents_first_seen_at", "first_seen_at"),
        Index("ix_documents_source_id", "source_id"),
        # 标题模糊搜索用 pg_trgm 三元组索引（中文可用；3 个字及以上的关键词能走索引）
        Index(
            "ix_documents_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 删除来源不能连带删掉已发现的文件：来源应停用而不是删除
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    # 第一次发现时的原始网址，用于展示和访问
    url: Mapped[str] = mapped_column(Text)
    # 规范网址键（见 crawler/urls.py 的 url_key）：http 与 https、端口、会话参数、
    # 查询参数顺序不同的网址算同一份文件，以它去重
    url_key: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    publish_date: Mapped[date | None] = mapped_column(Date)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 来源第一次成功检查时就已存在的条目算"基线"，不计入"新增"。
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    # discovered：只有题录；fetched：详情抓取成功；后续可加入 extracted 等状态
    status: Mapped[str] = mapped_column(String(16), default="discovered")

    source: Mapped[Source] = relationship()
    detail: Mapped["DocumentDetail | None"] = relationship(back_populates="document", uselist=False)


class DocumentDetail(Base):
    """详情抓取结果；失败也留一行，仅临时错误按退避时间重试。"""

    __tablename__ = "document_details"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    body_text: Mapped[str] = mapped_column(Text, default="")
    issuer: Mapped[str | None] = mapped_column(Text)
    doc_number: Mapped[str | None] = mapped_column(String(128))
    publish_date: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="detail")
    attachments: Mapped[list["DocumentAttachment"]] = relationship(
        back_populates="detail",
        cascade="all, delete-orphan",
        order_by="DocumentAttachment.position",
    )


class DocumentAttachment(Base):
    """只存附件题录和原站链接，不下载附件内容。"""

    __tablename__ = "document_attachments"
    __table_args__ = (Index("ix_document_attachments_document_id", "document_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document_details.document_id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    extension: Mapped[str] = mapped_column(String(16))

    detail: Mapped[DocumentDetail] = relationship(back_populates="attachments")
