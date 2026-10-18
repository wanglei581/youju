"""来源统计（推广与获客方案第 2 件，创始人 10/18 同意）：按天记小程序各种打开方式的次数。

一行是一天里同一场景值、来源编号、第一个页面的打开次数；不存任何用户编号、查询参数和时间。
保存 400 天，每天的清理任务删除更早的（youju/entry_stats.py）。只由 admin 服务写入，它的账号本来
就有全部权限；api 账号不需要新权限（deploy/mysql_accounts.sql 不用改）。

编号：0033 已被 PR #290 占用（还没合并）。它先合并的话，把这里的 down_revision 改成 "0033"。

Revision ID: 0034
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0032"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "entry_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("scene", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(24), server_default="", nullable=False),
        sa.Column("page", sa.String(64), server_default="", nullable=False),
        sa.Column("opens", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", "scene", "code", "page", name="uq_entry_stats_key"),
        **OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("entry_stats")
