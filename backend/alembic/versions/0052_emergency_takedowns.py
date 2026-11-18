"""电脑不在线时的紧急下架：一张只在服务器上用的新表，只新增、不动已有表。

服务器上的公开数据是本机（公开数据的主库）同步过来的，直接改文件的下架时间会被下一次同步盖回去。
这张表记服务器上的紧急下架：和正式下架一样归并组里每份各记一行（batch 是同一次的编号）；
没撤销（lifted_at 为空）的行，它指的文件所在的归并组整组不对外（takedown.available_condition）。
电脑恢复后带回本机正式下架，带着这一次编号的下架记录同步过来后自动撤掉。
行不删（撤销只填撤销的人、时间、说明）；本机删了文件时文件编号置空（和纠错一样，不挡同步和
用户数据恢复），下架时的规范网址留着。
本机上也建这张表（两边迁移版本要一样才能同步），但本机不写它。不同步、不进公开快照。

写这张表的只有服务器上的 youju emergency-takedown（worker 容器里运行）。

编号：0052（D-127 时留给紧急下架），接在 0053 后面。

Revision ID: 0052
Revises: 0053
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0052"
down_revision = "0053"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    op.create_table(
        "emergency_takedowns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("url_key", sa.String(512), nullable=False),
        sa.Column("batch", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=NOW, nullable=False),
        sa.Column("lifted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("lifted_by", sa.String(80), nullable=True),
        sa.Column("lift_note", sa.String(300), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        **OPTIONS,
    )
    op.create_index("ix_emergency_takedowns_document_id", "emergency_takedowns", ["document_id"])
    op.create_index(
        "ix_emergency_takedowns_active", "emergency_takedowns", ["lifted_at", "document_id"]
    )


def downgrade() -> None:
    op.drop_table("emergency_takedowns")
