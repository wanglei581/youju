"""质检：已通过的截止被规则或 AI 标为存疑、等人复核（D-023，AI 质检员）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_deadlines", sa.Column("audit_state", sa.String(length=8)))
    op.add_column("document_deadlines", sa.Column("audit_note", sa.String(length=200)))
    op.add_column("document_deadlines", sa.Column("audit_at", mysql.DATETIME(fsp=6)))
    op.add_column("document_deadlines", sa.Column("audit_by", sa.String(length=80)))
    op.create_check_constraint(
        "ck_document_deadline_audit_state",
        "document_deadlines",
        "audit_state IS NULL OR audit_state IN ('flagged', 'cleared', 'reopened')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_document_deadline_audit_state", "document_deadlines", type_="check")
    op.drop_column("document_deadlines", "audit_by")
    op.drop_column("document_deadlines", "audit_at")
    op.drop_column("document_deadlines", "audit_note")
    op.drop_column("document_deadlines", "audit_state")
