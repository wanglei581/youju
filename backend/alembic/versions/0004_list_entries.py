"""名单解析记录与企业名称索引。

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "list_parses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "source_url", name="uq_list_parse_source"),
    )
    op.create_table(
        "list_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("normalized_name", sa.String(256), nullable=False),
        sa.Column("credit_code", sa.String(18)),
        sa.Column("sequence", sa.String(32)),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("batch", sa.String(32)),
        sa.Column("region", sa.String(64)),
        sa.UniqueConstraint(
            "document_id", "source_url", "normalized_name", "sequence", name="uq_list_entry_row"
        ),
    )
    op.create_index("ix_list_entries_normalized_name", "list_entries", ["normalized_name"])
    op.create_index(
        "ix_list_entries_name_trgm",
        "list_entries",
        ["normalized_name"],
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.create_index("ix_list_entries_credit_code", "list_entries", ["credit_code"])


def downgrade() -> None:
    op.drop_index("ix_list_entries_name_trgm", table_name="list_entries")
    op.drop_index("ix_list_entries_credit_code", table_name="list_entries")
    op.drop_index("ix_list_entries_normalized_name", table_name="list_entries")
    op.drop_table("list_entries")
    op.drop_table("list_parses")
