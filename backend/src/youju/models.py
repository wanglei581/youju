"""数据模型 v1：来源、检查记录、文件题录。设计说明见 docs/adr/0003-数据模型v1.md。"""

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from youju.dbtypes import NOW, JSONType, LongText, UTCDateTime

# 网址类字段参与唯一索引，MySQL 要求定长：utf8mb4 每字 4 字节，一个索引总长不超过 3072 字节。
# 名单记录的唯一索引含网址、名称（256）和序号（32），所以网址最长 400；开发库里最长的是 307。
URL_KEY_MAX = 512
SOURCE_URL_MAX = 400


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
    url: Mapped[str] = mapped_column(String(URL_KEY_MAX), unique=True)

    # static：直接抓取；browser：需浏览器渲染；manual：需人工确认或暂不可访问
    fetch_mode: Mapped[str] = mapped_column(String(16), default="static")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 按栏目覆盖通用识别规则，如 {"item_selector": "ul.list li", "require_date": false}
    parser_config: Mapped[dict] = mapped_column(JSONType, default=dict)
    # 1：法定公文、公示名单等，可存全文和导出；2：只存题录；3：按授权（见总方案 §4.5）
    rights_level: Mapped[int] = mapped_column(Integer, default=1)
    verification_status: Mapped[str | None] = mapped_column(String(64))
    verified_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)

    # 健康：unknown / ok / degraded / failing / blocked
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=NOW, onupdate=func.now(6)
    )

    checks: Mapped[list["SourceCheck"]] = relationship(back_populates="source")


class SourceCheck(Base):
    """对某个来源的一次检查（抓取一次列表页）。失败也要记录，前台据此显示"截至上次成功检查"。"""

    __tablename__ = "source_checks"
    __table_args__ = (Index("ix_source_checks_source_started", "source_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime())
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
    """按规范网址键保存的原始题录；内容相同的不同网址由 DocumentMerge 归并展示。"""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_publish_date", "publish_date"),
        Index("ix_documents_first_seen_at", "first_seen_at"),
        Index("ix_documents_source_id", "source_id"),
        # 标题模糊搜索是 LIKE '%词%' 全表扫描：几万份文件以内足够快，不再建索引
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 删除来源不能连带删掉已发现的文件：来源应停用而不是删除
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    # 第一次发现时的原始网址，用于展示和访问
    url: Mapped[str] = mapped_column(Text)
    # 规范网址键（见 crawler/urls.py 的 url_key）：http 与 https、端口、会话参数、
    # 查询参数顺序不同的网址算同一份文件，以它去重
    url_key: Mapped[str] = mapped_column(String(URL_KEY_MAX), unique=True)
    title: Mapped[str] = mapped_column(Text)
    publish_date: Mapped[date | None] = mapped_column(Date)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # 来源第一次成功检查时就已存在的条目算"基线"，不计入"新增"。
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    # discovered：只有题录；fetched：详情抓取成功；后续可加入 extracted 等状态
    status: Mapped[str] = mapped_column(String(16), default="discovered")

    source: Mapped[Source] = relationship()
    detail: Mapped["DocumentDetail | None"] = relationship(back_populates="document", uselist=False)
    deadlines: Mapped[list["DocumentDeadline"]] = relationship(
        back_populates="document", foreign_keys="DocumentDeadline.document_id"
    )
    list_entries: Mapped[list["ListEntry"]] = relationship(back_populates="document")


class DocumentMerge(Base):
    """同一文件的别名关系；停用映射即可撤销，原始文件及衍生记录均保留。"""

    __tablename__ = "document_merges"
    __table_args__ = (
        Index("ix_document_merges_canonical_active", "canonical_id", "active"),
        CheckConstraint("duplicate_id <> canonical_id", name="ck_document_merge_not_self"),
    )

    duplicate_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    canonical_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    match_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)


class DocumentDetail(Base):
    """详情抓取结果；失败也留一行，仅临时错误按退避时间重试。"""

    __tablename__ = "document_details"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    body_text: Mapped[str] = mapped_column(LongText, default="")
    issuer: Mapped[str | None] = mapped_column(Text)
    doc_number: Mapped[str | None] = mapped_column(String(128))
    publish_date: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())
    http_status: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

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


class DocumentDeadline(Base):
    """规则候选和人工核对结果；公开端只读取 approved。"""

    __tablename__ = "document_deadlines"
    __table_args__ = (
        UniqueConstraint("document_id", "fingerprint", name="uq_document_deadline_fingerprint"),
        Index("ix_document_deadlines_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    candidate_date: Mapped[date] = mapped_column(Date)
    candidate_time: Mapped["time | None"] = mapped_column(Time)
    evidence: Mapped[str] = mapped_column(String(120))
    confidence: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    approved_date: Mapped[date | None] = mapped_column(Date)
    approved_time: Mapped["time | None"] = mapped_column(Time)
    # 截止变更（验收清单 A7）：发了延期、调整或撤销的通知后，原来通过的截止标为 changed，
    # 公开端不再当作有效截止，详情里写明"已延期，见新通知"
    changed_to_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT")
    )
    change_reason: Mapped[str | None] = mapped_column(String(8))
    changed_by: Mapped[str | None] = mapped_column(String(80))
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)

    document: Mapped[Document] = relationship(
        back_populates="deadlines", foreign_keys=[document_id]
    )


class ListParse(Base):
    """一份详情页或附件的解析记录，失败原因用于回填报告和安全重试。"""

    __tablename__ = "list_parses"
    __table_args__ = (UniqueConstraint("document_id", "source_url", name="uq_list_parse_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    source_url: Mapped[str] = mapped_column(String(SOURCE_URL_MAX))
    source_kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ListEntry(Base):
    """只存已从政府原文的企业或机构名称列抽取的字段。名单状态按文件标题实时算（D-018），不存。"""

    __tablename__ = "list_entries"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "source_url", "normalized_name", "sequence", name="uq_list_entry_row"
        ),
        Index("ix_list_entries_normalized_name", "normalized_name"),
        Index("ix_list_entries_credit_code", "credit_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    source_url: Mapped[str] = mapped_column(String(SOURCE_URL_MAX))
    name: Mapped[str] = mapped_column(String(256))
    normalized_name: Mapped[str] = mapped_column(String(256))
    credit_code: Mapped[str | None] = mapped_column(String(18))
    sequence: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    batch: Mapped[str | None] = mapped_column(String(32))
    region: Mapped[str | None] = mapped_column(String(64))

    document: Mapped[Document] = relationship(back_populates="list_entries")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    openid: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    last_visited_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    agreement_version: Mapped[str | None] = mapped_column(String(20))
    agreement_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Follow(Base):
    __tablename__ = "follows"
    __table_args__ = (Index("ix_follows_user", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(40))
    conditions: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_status_kind", "status", "kind"),
        CheckConstraint("kind IN ('newDocs', 'deadline')", name="ck_subscription_kind"),
        CheckConstraint(
            "status IN ('unused', 'sent', 'failed', 'refused', 'expired')",
            name="ck_subscription_status",
        ),
        CheckConstraint(
            "(kind = 'newDocs' AND follow_id IS NOT NULL AND document_id IS NULL)"
            " OR (kind = 'deadline' AND document_id IS NOT NULL AND follow_id IS NULL)",
            name="ck_subscription_target",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    template_id: Mapped[str] = mapped_column(String(128))
    follow_id: Mapped[int | None] = mapped_column(ForeignKey("follows.id", ondelete="CASCADE"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="unused")
    errcode: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_user", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    content: Mapped[str] = mapped_column(String(500))
    check_result: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    handled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(32))
    props: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
