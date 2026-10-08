"""成功发送的关注转发组记录，防止跨批次重复提醒及按时间推断造成漏发。

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "follow_repost_notifications",
        sa.Column("follow_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("notified_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["follow_id"], ["follows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["repost_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("follow_id", "group_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )


def downgrade() -> None:
    op.drop_table("follow_repost_notifications")
