"""申报通知支持方式和申报对象字段候选。

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "document_fields",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=300), nullable=False),
        sa.Column("evidence", sa.String(length=300), nullable=False),
        sa.Column("evidence_offset", sa.Integer(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=8), server_default="rule", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("approved_value", sa.String(length=300), nullable=True),
        sa.Column("reviewed_by", sa.String(length=80), nullable=True),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "field", "value", name="uq_document_fields_value"),
        sa.CheckConstraint("field IN ('support', 'audience')", name="ck_document_fields_field"),
        sa.CheckConstraint("source IN ('rule', 'ai', 'both')", name="ck_document_fields_source"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'stale')",
            name="ck_document_fields_status",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_document_fields_status", "document_fields", ["status"])
    op.create_index(
        "ix_document_fields_document_field", "document_fields", ["document_id", "field"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_fields_document_field", table_name="document_fields")
    op.drop_index("ix_document_fields_status", table_name="document_fields")
    op.drop_table("document_fields")
