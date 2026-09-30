"""管理台定时任务台账。

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column(
            "started_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("status", sa.String(16), server_default="running", nullable=False),
        sa.Column("summary", sa.String(500), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('running', 'ok', 'failed')", name="ck_job_runs_status"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_job_runs_started", "job_runs", ["started_at"])
    op.create_index("ix_job_runs_job_started", "job_runs", ["job", "started_at"])


def downgrade() -> None:
    op.drop_table("job_runs")
