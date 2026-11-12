"""首发前搬家（D-127）：从云托管带过来的已下架网址，一张新表，只新增、不动已有表。

本机（公开数据的主库）的数据来自不含下架文件的公开导出，不知道哪些通知下架过。迁移时从云托管
导出当前处于下架状态的归并组里每一份文件的规范网址键、原来的来源编号、下架时间、原因类别
（不带备注、操作人、标题），写进这张表：本机以后再抓到同一个规范网址不再收录，本机已经收进来的
由 youju takedown-carry apply 整组下架。这张表随同步到服务器（本机重建、从服务器导回时不丢）。

写这张表的只有 admin 服务（youju takedown-carry apply）；api 账号已有全库 SELECT，不用新授权。

编号：0051（0049、0050 留给 #461、#463，它们不进首发，谁后合进 main 谁改 down_revision）。

Revision ID: 0051
Revises: 0048
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0051"
down_revision = "0048"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    op.create_table(
        "takedown_blocks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("url_key", sa.String(512), nullable=False),
        sa.Column("origin_source_id", sa.Integer(), nullable=True),
        sa.Column("taken_down_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_key"),
        **OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("takedown_blocks")
