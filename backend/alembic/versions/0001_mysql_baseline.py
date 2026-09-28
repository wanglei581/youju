"""MySQL 基线：数据库从 PostgreSQL 换成云托管自带的 MySQL 8.0（D-022）。

原来 PostgreSQL 的 0001–0008 合并成这一个：线上还没部署过，没有要迁移的旧库。
时间列用 DATETIME(6)，一律存 UTC（见 youju/dbtypes.py）；正文用 MEDIUMTEXT。

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 不依赖建库时的默认值：每张表都用 utf8mb4 和区分大小写、不忽略尾部空格的排序规则
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("province", sa.String(length=32), nullable=False),
        sa.Column("dept", sa.String(length=32), nullable=False),
        sa.Column("dept_full_name", sa.String(length=128), nullable=False),
        sa.Column("column_name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("fetch_mode", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("parser_config", sa.JSON(), nullable=False),
        sa.Column("rights_level", sa.Integer(), nullable=False),
        sa.Column("verification_status", sa.String(length=64), nullable=True),
        sa.Column("verified_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("health", sa.String(length=16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_check_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("openid", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "last_visited_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("agreement_version", sa.String(length=20), nullable=True),
        sa.Column("agreement_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("openid"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publish_date", sa.Date(), nullable=True),
        sa.Column("first_seen_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_seen_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_key"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_documents_first_seen_at", "documents", ["first_seen_at"], unique=False)
    op.create_index("ix_documents_publish_date", "documents", ["publish_date"], unique=False)
    op.create_index("ix_documents_source_id", "documents", ["source_id"], unique=False)
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("props", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_events_user_created", "events", ["user_id", "created_at"], unique=False)
    op.create_table(
        "follows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("last_seen_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_notified_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_follows_user", "follows", ["user_id"], unique=False)
    op.create_table(
        "source_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("items_found", sa.Integer(), nullable=False),
        sa.Column("items_new", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_source_checks_source_started",
        "source_checks",
        ["source_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "document_deadlines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("candidate_date", sa.Date(), nullable=False),
        sa.Column("candidate_time", sa.Time(), nullable=True),
        sa.Column("evidence", sa.String(length=120), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by", sa.String(length=80), nullable=True),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("approved_date", sa.Date(), nullable=True),
        sa.Column("approved_time", sa.Time(), nullable=True),
        sa.Column("changed_to_document_id", sa.Integer(), nullable=True),
        sa.Column("change_reason", sa.String(length=8), nullable=True),
        sa.Column("changed_by", sa.String(length=80), nullable=True),
        sa.Column("changed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["changed_to_document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "fingerprint", name="uq_document_deadline_fingerprint"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_document_deadlines_status", "document_deadlines", ["status"], unique=False)
    op.create_table(
        "document_details",
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("body_text", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=True),
        sa.Column("doc_number", sa.String(length=128), nullable=True),
        sa.Column("publish_date", sa.Date(), nullable=True),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "document_merges",
        sa.Column("duplicate_id", sa.Integer(), nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("match_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint("duplicate_id <> canonical_id", name="ck_document_merge_not_self"),
        sa.ForeignKeyConstraint(["canonical_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["duplicate_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("duplicate_id"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_document_merges_canonical_active",
        "document_merges",
        ["canonical_id", "active"],
        unique=False,
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("content", sa.String(length=500), nullable=False),
        sa.Column("check_result", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("handled_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_feedback_user", "feedback", ["user_id"], unique=False)
    op.create_table(
        "list_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=400), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("normalized_name", sa.String(length=256), nullable=False),
        sa.Column("credit_code", sa.String(length=18), nullable=True),
        sa.Column("sequence", sa.String(length=32), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("batch", sa.String(length=32), nullable=True),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "source_url", "normalized_name", "sequence", name="uq_list_entry_row"
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_list_entries_credit_code", "list_entries", ["credit_code"], unique=False)
    op.create_index(
        "ix_list_entries_normalized_name", "list_entries", ["normalized_name"], unique=False
    )
    op.create_table(
        "list_parses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=400), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("attempted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "source_url", name="uq_list_parse_source"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("template_id", sa.String(length=128), nullable=False),
        sa.Column("follow_id", sa.Integer(), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("errcode", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("attempted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "(kind = 'newDocs' AND follow_id IS NOT NULL AND document_id IS NULL) OR (kind = 'deadline' AND document_id IS NOT NULL AND follow_id IS NULL)",
            name="ck_subscription_target",
        ),
        sa.CheckConstraint("kind IN ('newDocs', 'deadline')", name="ck_subscription_kind"),
        sa.CheckConstraint(
            "status IN ('unused', 'sent', 'failed', 'refused', 'expired')",
            name="ck_subscription_status",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["follow_id"], ["follows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_subscriptions_status_kind", "subscriptions", ["status", "kind"], unique=False
    )
    op.create_table(
        "document_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document_details.document_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_document_attachments_document_id", "document_attachments", ["document_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_document_attachments_document_id", table_name="document_attachments")
    op.drop_table("document_attachments")
    op.drop_index("ix_subscriptions_status_kind", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("list_parses")
    op.drop_index("ix_list_entries_normalized_name", table_name="list_entries")
    op.drop_index("ix_list_entries_credit_code", table_name="list_entries")
    op.drop_table("list_entries")
    op.drop_index("ix_feedback_user", table_name="feedback")
    op.drop_table("feedback")
    op.drop_index("ix_document_merges_canonical_active", table_name="document_merges")
    op.drop_table("document_merges")
    op.drop_table("document_details")
    op.drop_index("ix_document_deadlines_status", table_name="document_deadlines")
    op.drop_table("document_deadlines")
    op.drop_index("ix_source_checks_source_started", table_name="source_checks")
    op.drop_table("source_checks")
    op.drop_index("ix_follows_user", table_name="follows")
    op.drop_table("follows")
    op.drop_index("ix_events_user_created", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_documents_source_id", table_name="documents")
    op.drop_index("ix_documents_publish_date", table_name="documents")
    op.drop_index("ix_documents_first_seen_at", table_name="documents")
    op.drop_table("documents")
    op.drop_table("users")
    op.drop_table("sources")
