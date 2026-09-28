"""后台 AI：AI 检查记录表；截止候选记录来源（规则 / AI）和 AI 是否一致（D-023）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "ai_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("task", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("checked_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "task", name="uq_ai_check_task"),
        **TABLE_OPTIONS,
    )
    op.add_column(
        "document_deadlines",
        sa.Column("origin", sa.String(length=8), server_default="rule", nullable=False),
    )
    op.add_column("document_deadlines", sa.Column("ai_agrees", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_document_deadline_origin", "document_deadlines", "origin IN ('rule', 'ai')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_document_deadline_origin", "document_deadlines", type_="check")
    op.drop_column("document_deadlines", "ai_agrees")
    op.drop_column("document_deadlines", "origin")
    op.drop_table("ai_checks")
