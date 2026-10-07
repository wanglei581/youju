"""数据模型 v1：来源、检查记录、文件题录。设计说明见 docs/adr/0003-数据模型v1.md。"""

from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
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

from youju.categories import doc_category
from youju.classify import doc_kind
from youju.dbtypes import NOW, JSONType, LongText, UTCDateTime
from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

# 网址类字段参与唯一索引，MySQL 要求定长：utf8mb4 每字 4 字节，一个索引总长不超过 3072 字节。
# 名单记录的唯一索引含网址、名称（256）和序号（32），所以网址最长 400；开发库里最长的是 307。
URL_KEY_MAX = 512
SOURCE_URL_MAX = 400


def _rule_kind(context) -> str:
    """入库时按标题规则算好类型（ORM 和批量插入都会调用），筛选时只比较这一列。"""
    return doc_kind(context.get_current_parameters().get("title") or "")


def _rule_category(context) -> str:
    """入库时按标题规则算好项目类别（同 _rule_kind），按类别筛选时只比较这一列。"""
    return doc_category(context.get_current_parameters().get("title") or "")


def _rule_supports(context) -> str:
    """入库时按标题规则算好支持方式（逗号分隔，没有为空串），按支持方式筛选时只比较这一列。"""
    return SUPPORT_RULES.stored(context.get_current_parameters().get("title") or "")


def _rule_industries(context) -> str:
    """入库时按标题规则算好产业方向（同 _rule_supports）。"""
    return INDUSTRY_RULES.stored(context.get_current_parameters().get("title") or "")


class Base(DeclarativeBase):
    pass


class JobRun(Base):
    """定时任务运行台账；摘要只记任务结果，不复制命令输出或异常正文。"""

    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_started", "started_at"),
        Index("ix_job_runs_job_started", "job", "started_at"),
        CheckConstraint("status IN ('running', 'ok', 'failed')", name="ck_job_runs_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    summary: Mapped[str] = mapped_column(String(500), default="", server_default="")


class DigestProfile(Base):
    """固定模板周报方案；空省份列表表示全国，国家部委始终包含。"""

    __tablename__ = "digest_profiles"
    __table_args__ = (CheckConstraint("weekday BETWEEN 1 AND 7", name="ck_digest_profile_weekday"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    provinces: Mapped[list] = mapped_column(JSONType, default=list)
    keywords: Mapped[list] = mapped_column(JSONType, default=list)
    columns: Mapped[dict] = mapped_column(
        JSONType,
        default=lambda: dict.fromkeys(("applications", "deadlines", "lists", "policies"), True),
    )
    weekday: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class DigestIssue(Base):
    """人工发布的周报快照；依赖编号用于阻止复制后来已下架的内容。

    kind 区分周报（weekly，按统计周）和临期提醒（reminder，按生成当天所在的周）；
    同一方案每周每种稿件各一份。
    """

    __tablename__ = "digest_issues"
    __table_args__ = (
        UniqueConstraint("profile_id", "kind", "week_start", name="uq_digest_issue_kind_week"),
        CheckConstraint("status IN ('draft', 'published')", name="ck_digest_issue_status"),
        CheckConstraint("week_end >= week_start", name="ck_digest_issue_interval"),
        CheckConstraint("kind IN ('weekly', 'reminder')", name="ck_digest_issue_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("digest_profiles.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16), default="weekly", server_default="weekly")
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(200))
    body_html: Mapped[str] = mapped_column(LongText)
    body_text: Mapped[str] = mapped_column(LongText)
    body_text_with_links: Mapped[str] = mapped_column(LongText)
    counts: Mapped[dict] = mapped_column(JSONType)
    document_ids: Mapped[list] = mapped_column(JSONType)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    note: Mapped[str] = mapped_column(String(500), default="", server_default="")


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
        CheckConstraint(
            "kind_override IS NULL OR kind_override IN "
            "('申报通知', '名单', '政策', '资讯与指南', '其他')",
            name="ck_document_kind_override",
        ),
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
    # 人工定的类型：AI 和标题规则判断不一致时，在核对台选定（D-023）；或在管理台确认"改回申报通知"
    # 的建议（D-051）。有值时给用户看这个
    kind_override: Mapped[str | None] = mapped_column(String(8))
    # 按标题规则算出的类型（申报通知 / 名单 / 政策 / 资讯与指南 / 其他）：入库时算好；规则改了由
    # youju reclassify 重算（admin 服务每次启动时跑）。用户看到的类型 = 人工定的，没定过的按这一列
    rule_kind: Mapped[str | None] = mapped_column(String(8), default=_rule_kind)
    # 按标题规则算出的项目类别（见 youju.categories）：同 rule_kind，入库时算好，规则改了由
    # youju reclassify 重算。只对申报通知和名单显示、筛选
    rule_category: Mapped[str | None] = mapped_column(String(16), default=_rule_category)
    # 按标题规则算出的支持方式、产业方向（见 youju.title_tags，只用于筛选、不展示）：逗号分隔，
    # 没有标签存空串，NULL 是还没算；同 rule_category，入库时算好，规则改了由 youju reclassify 重算
    rule_supports: Mapped[str | None] = mapped_column(String(40), default=_rule_supports)
    rule_industries: Mapped[str | None] = mapped_column(String(80), default=_rule_industries)
    kind_decided_by: Mapped[str | None] = mapped_column(String(80))
    kind_decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # 下架独立于采集状态，恢复后采集、核对和分类结果原样保留。
    taken_down_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    takedown_reason: Mapped[str | None] = mapped_column(String(16))
    takedown_note: Mapped[str | None] = mapped_column(String(300))
    taken_down_by: Mapped[str | None] = mapped_column(String(80))
    restored_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    restore_note: Mapped[str | None] = mapped_column(String(300))
    restored_by: Mapped[str | None] = mapped_column(String(80))

    source: Mapped[Source] = relationship()
    detail: Mapped["DocumentDetail | None"] = relationship(back_populates="document", uselist=False)
    fields: Mapped[list["DocumentField"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    deadlines: Mapped[list["DocumentDeadline"]] = relationship(
        back_populates="document", foreign_keys="DocumentDeadline.document_id"
    )
    list_entries: Mapped[list["ListEntry"]] = relationship(back_populates="document")


class DocumentTakedownEvent(Base):
    """每次下架、恢复为归并组各副本保留一条历史，不覆盖旧操作。"""

    __tablename__ = "document_takedown_events"
    __table_args__ = (
        Index("ix_takedown_events_document_id", "document_id", "id"),
        CheckConstraint("action IN ('takedown', 'restore')", name="ck_takedown_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(300))
    operated_by: Mapped[str] = mapped_column(String(80))
    operated_at: Mapped[datetime] = mapped_column(UTCDateTime())


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


class RepostGroup(Base):
    """转发组（D-055，见 youju/repost.py）：同一份部里通知的各地版本。

    各版本的截止日期仍挂在各自的文件上、各自核对，不随组同步。
    strength：strong 强证据 / weak 疑似（自动确认只对强证据组）。topic 是事项名（核心标题），
    upstream_title、upstream_number 照录正文里引用的部里通知，没有为空。
    no_auto：人工撤回、移出、拆过或整组不合并的组：YOUJU_DEDUPE_AUTO 打开后也不自动确认，
    归组任务也不清理它和它的组员。
    """

    __tablename__ = "repost_groups"
    __table_args__ = (
        CheckConstraint("strength IN ('strong', 'weak')", name="ck_repost_groups_strength"),
        CheckConstraint("created_by IN ('rule', 'human')", name="ck_repost_groups_created_by"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strength: Mapped[str] = mapped_column(String(8))
    topic: Mapped[str] = mapped_column(String(200))
    upstream_title: Mapped[str | None] = mapped_column(String(300))
    upstream_number: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(8), default="rule", server_default="rule")
    no_auto: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 修订号：成员进出、确认状态、事项信息有变化就加一；管理台确认、撤回、拆组、整组不合并时
    # 带上看到的修订号，对不上就不执行（返回 409，提示刷新）
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)


class RepostGroupMember(Base):
    """转发组里的一个版本（同一文件层的主文件）；一份文件最多属于一个转发组。

    confirmed_at 有值才对外按组折叠（D-055：前 4 周每组都要人确认）。active=False 是人工移出，
    归组任务不会再把它加回来。joined_by：rule 归组规则加入 / human 人工加入或拆出。
    """

    __tablename__ = "repost_group_members"
    __table_args__ = (
        Index("ix_repost_group_members_group", "group_id", "active"),
        CheckConstraint("joined_by IN ('rule', 'human')", name="ck_repost_members_joined_by"),
        CheckConstraint(
            "confirmed_at IS NULL OR confirmed_by IS NOT NULL",
            name="ck_repost_members_confirmed",
        ),
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(ForeignKey("repost_groups.id", ondelete="CASCADE"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    joined_by: Mapped[str] = mapped_column(String(8), default="rule", server_default="rule")
    # 归组证据（标题转发、正文转发、上游文号、标题含部委或国家计划），只给核对人看
    evidence: Mapped[str] = mapped_column(String(200), default="", server_default="")
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    confirmed_by: Mapped[str | None] = mapped_column(String(80))
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    removed_by: Mapped[str | None] = mapped_column(String(80))
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


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


class AttachmentCopy(Base):
    """附件副本（见 youju/attachments.py）：admin 定时任务从原网站下载、校验后存到对象存储
    attachments/，小程序经 api 分段读取后在微信里打开。按原站链接一条，多份文件引用同一链接时共用；
    内容相同的链接共用同一个对象（对象名是内容的 SHA-256）。api 只读这张表。"""

    __tablename__ = "attachment_copies"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_attachment_copies_url"),
        Index("ix_attachment_copies_sha256", "sha256"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_attachment_copies_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 原站链接的 SHA-256（链接可能很长，唯一索引建在它上面）
    url_hash: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    # 附件题录里的扩展名；下载后按文件头判断的实际格式要和它是同一类（见 file_type）
    declared_type: Mapped[str] = mapped_column(String(8))
    # pending：还没下载；ready：有副本可以打开；failed：没有副本（原因见 failure）
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    # 有副本时：实际格式（打开时用）、对象存储文件 ID、大小、内容的 SHA-256、下载时间
    file_type: Mapped[str | None] = mapped_column(String(8))
    file_id: Mapped[str | None] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # 换下来、还没从对象存储删掉的旧副本（删除失败时下一轮再删）
    stale_file_ids: Mapped[list[str] | None] = mapped_column(JSONType)
    # 最近一次没成功的原因（代码 + 说明）；有副本时检查失败也记在这里，副本照常可用
    failure: Mapped[str | None] = mapped_column(String(16))
    failure_detail: Mapped[str | None] = mapped_column(String(300))
    # 连续失败次数（成功后清零），决定多久后重试
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 检查这份副本时用的规则版本（youju.attachments.RULES_VERSION）：不是当前版本的不提供，
    # 等定时任务按新规则重查
    rules_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 上次按新规则重查的时间（不论结果）：重查按它排，读不到的排到后面
    rechecked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # 下次检查时间：空值表示尽快
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)


class DocumentField(Base):
    """申报通知的结构化字段候选；公开端只读取人工通过的记录。"""

    __tablename__ = "document_fields"
    __table_args__ = (
        UniqueConstraint("document_id", "field", "value", name="uq_document_fields_value"),
        Index("ix_document_fields_status", "status"),
        Index("ix_document_fields_document_field", "document_id", "field"),
        CheckConstraint("field IN ('support', 'audience')", name="ck_document_fields_field"),
        CheckConstraint("source IN ('rule', 'ai', 'both')", name="ck_document_fields_source"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'stale')",
            name="ck_document_fields_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(300))
    evidence: Mapped[str] = mapped_column(String(300))
    evidence_offset: Mapped[int] = mapped_column(Integer)
    body_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(8), default="rule", server_default="rule")
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    approved_value: Mapped[str | None] = mapped_column(String(300))
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)

    document: Mapped[Document] = relationship(back_populates="fields")


class DocumentDeadline(Base):
    """规则候选和人工核对结果；公开端只读取 approved。"""

    __tablename__ = "document_deadlines"
    __table_args__ = (
        UniqueConstraint("document_id", "fingerprint", name="uq_document_deadline_fingerprint"),
        Index("ix_document_deadlines_status", "status"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'changed')",
            name="ck_document_deadline_status",
        ),
        CheckConstraint(
            "status != 'approved' OR approved_date IS NOT NULL",
            name="ck_document_deadline_approved_date",
        ),
        # 已变更的必须保留原核定日期，写明原因（延期 / 调整 / 撤销），并关联发布变更的通知
        CheckConstraint(
            "status != 'changed' OR (approved_date IS NOT NULL"
            " AND change_reason IS NOT NULL AND change_reason IN ('延期', '调整', '撤销')"
            " AND changed_to_document_id IS NOT NULL)",
            name="ck_document_deadline_changed",
        ),
        CheckConstraint("origin IN ('rule', 'ai')", name="ck_document_deadline_origin"),
        CheckConstraint(
            "audit_state IS NULL OR audit_state IN ('flagged', 'cleared', 'reopened')",
            name="ck_document_deadline_audit_state",
        ),
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
    # 候选从哪来：rule（规则抽取）/ ai（规则没找到、AI 找到的）。AI 找到的也要人工核对
    origin: Mapped[str] = mapped_column(String(8), default="rule", server_default="rule")
    # 规则候选里 AI 是否也找到了同一天：True 一致 / False AI 查过但没找到 / 空 AI 还没查
    ai_agrees: Mapped[bool | None] = mapped_column(Boolean)
    # 质检：flagged 已通过但存疑、等人复核 / cleared 人确认无误 / reopened 质检退回、回到待核对，
    # 必须由人重新核对（不按同组已通过自动沿用，也不按导入结果通过）；空表示没发现问题。
    # audit_note 存疑原因；audit_at 最近一次质检的时间（AI 抽查过、标存疑、复核或退回）
    audit_state: Mapped[str | None] = mapped_column(String(8))
    audit_note: Mapped[str | None] = mapped_column(String(200))
    audit_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    audit_by: Mapped[str | None] = mapped_column(String(80))

    document: Mapped[Document] = relationship(
        back_populates="deadlines", foreign_keys=[document_id]
    )


class AICheck(Base):
    """AI 对一份文件做过的一项检查（kind 类型判断 / deadlines 截止抽取）：结果、用量和出错原因。

    每份文件每项只留最新一次；出错的隔一段时间重试。只发政府公开文件给 AI，不含用户数据。
    """

    __tablename__ = "ai_checks"
    __table_args__ = (UniqueConstraint("document_id", "task", name="uq_ai_check_task"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    task: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict | list | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(String(200))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AIUsage(Base):
    """AI 用量台账：每天（北京时间）、每项任务、每个模型的调用次数、出错次数和 tokens。

    AICheck 每份文件每项只留最新一次，名单认列也不留检查记录，所以另记台账；
    运维日报据此报用量和费用。
    """

    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("day", "task", "model", name="uq_ai_usage_day_task_model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    task: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)


# 申报条件的类别（D-052 第④项）：顺序也是对外展示的顺序
CONDITION_CATEGORIES = ("主体资格", "财务指标", "知识产权", "人员", "其他")


class ConditionList(Base):
    """一份申报通知某一版正文的“申报条件清单”（D-052 第④项）：后台模型抽出条目，人逐条核对后
    整份确认，确认后才对外。

    按正文指纹（body_hash，同 DocumentField）一版一份：正文变了就按新正文另抽一份，旧的保留作留痕，
    对外读取时核对当前正文指纹，旧版即使确认过也不再显示。
    status：pending 待核对 / confirmed 已整份确认（对外只显示其中 approved 的条目）/
    empty 抽过但没有通过原文核对的条目（不进核对台）/ superseded 正文变了、还没确认就被新版替代。
    """

    __tablename__ = "condition_lists"
    __table_args__ = (
        UniqueConstraint("document_id", "body_hash", name="uq_condition_lists_body"),
        Index("ix_condition_lists_status", "status"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'empty', 'superseded')",
            name="ck_condition_lists_status",
        ),
        CheckConstraint(
            "status != 'confirmed' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_condition_lists_confirmed",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    body_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    # 模型给了、但引用在正文里逐字找不到等原因丢掉的条数（核对台提示用）
    dropped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    extracted_at: Mapped[datetime] = mapped_column(UTCDateTime())
    confirmed_by: Mapped[str | None] = mapped_column(String(80))
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)

    items: Mapped[list["ConditionItem"]] = relationship(
        back_populates="condition_list",
        cascade="all, delete-orphan",
        order_by="ConditionItem.position",
    )


class ConditionItem(Base):
    """清单里的一条条件：一句话的条件（text，人可以改）+ 原文逐字引用（quote，人不能改）。

    quote 是脱敏后正文里的一段原文（对外展示的正文也是脱敏后的），quote_offset 是它在脱敏后正文里的
    位置。original_text 留着模型最初给的那句话，改过的能看出改了什么。
    status：pending 待核 / approved 通过（含改后通过）/ deleted 删除（不对外，留作留痕）。
    """

    __tablename__ = "condition_items"
    __table_args__ = (
        Index("ix_condition_items_list", "list_id", "position"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'deleted')", name="ck_condition_items_status"
        ),
        CheckConstraint(
            "category IN ('主体资格', '财务指标', '知识产权', '人员', '其他')",
            name="ck_condition_items_category",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("condition_lists.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(String(300))
    original_text: Mapped[str] = mapped_column(String(300))
    quote: Mapped[str] = mapped_column(String(500))
    quote_offset: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    condition_list: Mapped[ConditionList] = relationship(back_populates="items")


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


class AdvisorTask(Base):
    """第一批顾问技能包的持久任务；客户名单只在 input_rows 中短期保存。"""

    __tablename__ = "advisor_tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_advisor_tasks_user_key"),
        Index("ix_advisor_tasks_user_created", "user_id", "created_at"),
        Index("ix_advisor_tasks_status_created", "status", "created_at"),
        CheckConstraint(
            "kind IN ('compare', 'track', 'interpret', 'clause')", name="ck_advisor_tasks_kind"
        ),
        Index("ix_advisor_tasks_cache", "cache_key", "finished_at"),
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'cancelled')",
            name="ck_advisor_tasks_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    input_rows: Mapped[list | None] = mapped_column(JSONType)
    input_json: Mapped[dict | None] = mapped_column(JSONType)
    result_json: Mapped[dict | None] = mapped_column(JSONType)
    snapshot: Mapped[dict | None] = mapped_column(JSONType)
    cache_key: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    row_count: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    result_file_id: Mapped[str | None] = mapped_column(Text)
    result_rows: Mapped[int | None] = mapped_column(Integer)
    result_document_ids: Mapped[list[int] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AdvisorUsage(Base):
    """顾问技能包按北京时间、用户和类型记账的已用次数。"""

    __tablename__ = "advisor_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "day", name="uq_advisor_usage_user_kind_day"),
        CheckConstraint("used_count >= 0", name="ck_advisor_usage_count"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    day: Mapped[date] = mapped_column(Date)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AdvisorFileTrash(Base):
    """对象存储删除失败的文件，定时任务会重试。"""

    __tablename__ = "advisor_file_trash"
    __table_args__ = (UniqueConstraint("file_id", name="uq_advisor_file_trash_file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str] = mapped_column(String(512))
    user_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)


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
    status: Mapped[str] = mapped_column(String(16), default="unused", server_default="unused")
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
    # 纠错分诊（D-023）：规则和 AI 复查对应文件已发布的截止日期的结果，给处理的人看；
    # 用户写的内容不发给 AI
    triage_note: Mapped[str | None] = mapped_column(String(300))
    triaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(32))
    props: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)


class AccessLog(Base):
    """网络日志（《网络安全法》要求留存不少于六个月）：每次请求一条，健康检查除外。

    这张表只是暂存：每天把前一天的归档到云托管对象存储后删掉，归档保留 190 天
    （youju/access_log.py）；不进数据库备份。只记最少的字段：不记查询参数和请求内容
    （检索词、企业名称都不记），用户只记去标识化的编号。
    """

    __tablename__ = "access_logs"
    __table_args__ = (
        Index("ix_access_logs_logged_at", "logged_at"),
        UniqueConstraint("rid", name="uq_access_logs_rid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 这条记录的随机编号（128 位）：写库失败后补写时，同一条不会写进两次
    rid: Mapped[str] = mapped_column(String(32))
    # 请求处理完、写日志的时间
    at: Mapped[datetime] = mapped_column(UTCDateTime())
    # 写进数据库的时间（数据库的时钟）：每天按它归档，写库失败后补写的记录归进补写那天
    logged_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
    service: Mapped[str] = mapped_column(String(8))
    method: Mapped[str] = mapped_column(String(8))
    # 只有路径，不含问号后面的查询参数
    path: Mapped[str] = mapped_column(String(200))
    status: Mapped[int] = mapped_column(Integer)
    # 开始处理到返回响应头的毫秒数
    ms: Mapped[int] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    # 小程序用户：openid 用程序自己的密钥做 HMAC 后的前 16 位（见 AppKey），不是 openid 本身
    uid: Mapped[str | None] = mapped_column(String(16))
    # 核对台操作：做了什么、对哪条记录（操作人、编号、条数），见 api/app.py 的 _audit
    detail: Mapped[dict | None] = mapped_column(JSONType)


class AppKey(Base):
    """程序自己生成、自己用的密钥（如访问日志里用户编号的去标识化密钥）：第一次用到时随机生成，
    随数据库备份。不用人配置，也不出现在日志里。"""

    __tablename__ = "app_keys"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW)
