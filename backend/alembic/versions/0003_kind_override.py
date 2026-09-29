"""文件的人工定类型：AI 和标题规则判断不一致时在核对台选定，给用户看这个（D-023）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("kind_override", sa.String(length=8), nullable=True))
    op.add_column("documents", sa.Column("kind_decided_by", sa.String(length=80), nullable=True))
    op.add_column("documents", sa.Column("kind_decided_at", mysql.DATETIME(fsp=6), nullable=True))
    op.create_check_constraint(
        "ck_document_kind_override",
        "documents",
        "kind_override IS NULL OR kind_override IN ('申报通知', '名单', '政策', '其他')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_document_kind_override", "documents", type_="check")
    op.drop_column("documents", "kind_decided_at")
    op.drop_column("documents", "kind_decided_by")
    op.drop_column("documents", "kind_override")
