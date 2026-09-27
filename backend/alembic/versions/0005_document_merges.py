"""可撤销的文件归并映射。

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_merges",
        sa.Column(
            "duplicate_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "canonical_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("match_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("duplicate_id <> canonical_id", name="ck_document_merge_not_self"),
    )
    op.create_index(
        "ix_document_merges_canonical_active", "document_merges", ["canonical_id", "active"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_merges_canonical_active", table_name="document_merges")
    op.drop_table("document_merges")
