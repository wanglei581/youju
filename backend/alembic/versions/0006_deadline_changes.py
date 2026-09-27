"""截止日期变更：延期、调整、撤销后，原截止标为 changed 并关联新通知。

Revision ID: 0006
Revises: 0004（#38 的 0005 合并后改为 0005）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_deadlines",
        sa.Column(
            "changed_to_document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column("document_deadlines", sa.Column("change_reason", sa.String(8), nullable=True))
    op.add_column("document_deadlines", sa.Column("changed_by", sa.String(80), nullable=True))
    op.add_column(
        "document_deadlines", sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # 状态多一个 changed
    op.drop_constraint("ck_document_deadline_status", "document_deadlines", type_="check")
    op.create_check_constraint(
        "ck_document_deadline_status",
        "document_deadlines",
        "status IN ('pending', 'approved', 'rejected', 'changed')",
    )
    # 已变更的必须保留原核定日期，写明原因（延期 / 调整 / 撤销），并关联发布变更的通知
    op.create_check_constraint(
        "ck_document_deadline_changed",
        "document_deadlines",
        "status != 'changed' OR (approved_date IS NOT NULL"
        " AND change_reason IS NOT NULL AND change_reason IN ('延期', '调整', '撤销')"
        " AND changed_to_document_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("UPDATE document_deadlines SET status = 'approved' WHERE status = 'changed'")
    op.drop_constraint("ck_document_deadline_changed", "document_deadlines", type_="check")
    op.drop_constraint("ck_document_deadline_status", "document_deadlines", type_="check")
    op.create_check_constraint(
        "ck_document_deadline_status",
        "document_deadlines",
        "status IN ('pending', 'approved', 'rejected')",
    )
    op.drop_column("document_deadlines", "changed_at")
    op.drop_column("document_deadlines", "changed_by")
    op.drop_column("document_deadlines", "change_reason")
    op.drop_column("document_deadlines", "changed_to_document_id")
