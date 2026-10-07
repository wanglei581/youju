"""转发组（D-055，多级转载去重）：同一份部里通知的各地版本归成一组，人确认后才对外按组折叠。

repost_groups：一组一行（强证据 / 疑似、事项名、照录的部里通知标题和文号）；
repost_group_members：组里的版本，一份文件最多属于一个组（document_id 为主键）。
截止日期不在这两张表里，各版本的截止仍各自核对。

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    op.create_table(
        "repost_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strength", sa.String(length=8), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("upstream_title", sa.String(length=300), nullable=True),
        sa.Column("upstream_number", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=8), server_default="rule", nullable=False),
        sa.Column("no_auto", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=NOW, nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("strength IN ('strong', 'weak')", name="ck_repost_groups_strength"),
        sa.CheckConstraint("created_by IN ('rule', 'human')", name="ck_repost_groups_created_by"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "repost_group_members",
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("joined_by", sa.String(length=8), server_default="rule", nullable=False),
        sa.Column("evidence", sa.String(length=200), server_default="", nullable=False),
        sa.Column("joined_at", mysql.DATETIME(fsp=6), server_default=NOW, nullable=False),
        sa.Column("confirmed_by", sa.String(length=80), nullable=True),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("removed_by", sa.String(length=80), nullable=True),
        sa.Column("removed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["repost_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id"),
        sa.CheckConstraint("joined_by IN ('rule', 'human')", name="ck_repost_members_joined_by"),
        sa.CheckConstraint(
            "confirmed_at IS NULL OR confirmed_by IS NOT NULL",
            name="ck_repost_members_confirmed",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_repost_group_members_group", "repost_group_members", ["group_id", "active"])


def downgrade() -> None:
    # 直接删表（索引随表删除）：外键还在时 MySQL 不许先单独删它用到的索引
    op.drop_table("repost_group_members")
    op.drop_table("repost_groups")
