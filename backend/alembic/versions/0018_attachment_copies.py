"""附件副本：近 90 天文件的附件存到对象存储 attachments/，小程序里直接打开。

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachment_copies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("declared_type", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("file_type", sa.String(8), nullable=True),
        sa.Column("file_id", sa.String(512), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("stale_file_ids", sa.JSON(), nullable=True),
        sa.Column("failure", sa.String(16), nullable=True),
        sa.Column("failure_detail", sa.String(300), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("checked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("next_check_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_hash", name="uq_attachment_copies_url"),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_attachment_copies_status"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_attachment_copies_sha256", "attachment_copies", ["sha256"])


def downgrade() -> None:
    # 只删表；对象存储里的副本留着，由管理员在控制台清理 attachments/（不影响 backups/）
    op.drop_table("attachment_copies")
