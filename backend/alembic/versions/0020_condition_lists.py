"""申报条件清单（D-052 第④项）：后台模型从申报通知正文抽条件，每条带原文逐字引用；人逐条核对、
整份确认后才对外。

condition_lists：一份通知一版正文一份清单（按正文指纹）；condition_items：清单里的条目。

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "condition_lists",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("dropped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("extracted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("confirmed_by", sa.String(length=80), nullable=True),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "body_hash", name="uq_condition_lists_body"),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'empty', 'superseded')",
            name="ck_condition_lists_status",
        ),
        sa.CheckConstraint(
            "status != 'confirmed' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_condition_lists_confirmed",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_condition_lists_status", "condition_lists", ["status"])
    op.create_table(
        "condition_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("original_text", sa.String(length=300), nullable=False),
        sa.Column("quote", sa.String(length=500), nullable=False),
        sa.Column("quote_offset", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reviewed_by", sa.String(length=80), nullable=True),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["list_id"], ["condition_lists.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'deleted')", name="ck_condition_items_status"
        ),
        sa.CheckConstraint(
            "category IN ('主体资格', '财务指标', '知识产权', '人员', '其他')",
            name="ck_condition_items_category",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_condition_items_list", "condition_items", ["list_id", "position"])


def downgrade() -> None:
    # 直接删表（索引随表删除）：外键还在时 MySQL 不许先单独删它用到的索引
    op.drop_table("condition_items")
    op.drop_table("condition_lists")
