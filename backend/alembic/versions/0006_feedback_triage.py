"""纠错分诊：纠错记录上存规则和 AI 复查对应文件的结果（D-023）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("triage_note", sa.String(length=300), nullable=True))
    op.add_column("feedback", sa.Column("triaged_at", mysql.DATETIME(fsp=6), nullable=True))


def downgrade() -> None:
    op.drop_column("feedback", "triaged_at")
    op.drop_column("feedback", "triage_note")
