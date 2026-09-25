"""初始数据模型：来源、检查记录、文件题录

Revision ID: 0001
Revises:
Create Date: 2026-09-26 00:53:59.043835

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 标题模糊搜索用到 pg_trgm（PostgreSQL 13 起为可信扩展，数据库所有者即可创建）
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("province", sa.String(length=32), nullable=False),
        sa.Column("dept", sa.String(length=32), nullable=False),
        sa.Column("dept_full_name", sa.String(length=128), nullable=False),
        sa.Column("column_name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("fetch_mode", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("parser_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rights_level", sa.Integer(), nullable=False),
        sa.Column("verification_status", sa.String(length=64), nullable=True),
        sa.Column("verified_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("health", sa.String(length=16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publish_date", sa.Date(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_key"),
    )
    op.create_index("ix_documents_first_seen_at", "documents", ["first_seen_at"], unique=False)
    op.create_index("ix_documents_publish_date", "documents", ["publish_date"], unique=False)
    op.create_index("ix_documents_source_id", "documents", ["source_id"], unique=False)
    op.create_index(
        "ix_documents_title_trgm",
        "documents",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_table(
        "source_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("items_found", sa.Integer(), nullable=False),
        sa.Column("items_new", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_checks_source_started",
        "source_checks",
        ["source_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    # 保留 pg_trgm 扩展：可能被其他对象使用，删除扩展不影响回退
    op.drop_index("ix_source_checks_source_started", table_name="source_checks")
    op.drop_table("source_checks")
    op.drop_index(
        "ix_documents_title_trgm",
        table_name="documents",
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.drop_index("ix_documents_source_id", table_name="documents")
    op.drop_index("ix_documents_publish_date", table_name="documents")
    op.drop_index("ix_documents_first_seen_at", table_name="documents")
    op.drop_table("documents")
    op.drop_table("sources")
