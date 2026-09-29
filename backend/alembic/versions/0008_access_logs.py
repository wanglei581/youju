"""网络日志（《网络安全法》要求留存不少于六个月）和程序自用的密钥。

access_logs 只是暂存：每天归档到云托管对象存储后删掉，归档保留 190 天，不进数据库备份。
app_keys 放程序第一次用到时随机生成的密钥（访问日志里用户编号的去标识化密钥）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "access_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "logged_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("service", sa.String(length=8), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path", sa.String(length=200), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("ms", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("uid", sa.String(length=16), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_access_logs_logged_at", "access_logs", ["logged_at"])
    op.create_table(
        "app_keys",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("app_keys")
    op.drop_index("ix_access_logs_logged_at", table_name="access_logs")
    op.drop_table("access_logs")
