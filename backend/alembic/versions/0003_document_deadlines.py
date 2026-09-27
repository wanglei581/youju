"""截止候选和人工核对记录。

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_deadlines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("candidate_date", sa.Date(), nullable=False),
        sa.Column("candidate_time", sa.Time(), nullable=True),
        sa.Column("evidence", sa.String(120), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewed_by", sa.String(80), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_date", sa.Date(), nullable=True),
        sa.Column("approved_time", sa.Time(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("document_id", "fingerprint", name="uq_document_deadline_fingerprint"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_document_deadline_status"
        ),
        sa.CheckConstraint(
            "status != 'approved' OR approved_date IS NOT NULL",
            name="ck_document_deadline_approved_date",
        ),
    )
    op.create_index("ix_document_deadlines_status", "document_deadlines", ["status"])


def downgrade() -> None:
    op.drop_index("ix_document_deadlines_status", table_name="document_deadlines")
    op.drop_table("document_deadlines")
