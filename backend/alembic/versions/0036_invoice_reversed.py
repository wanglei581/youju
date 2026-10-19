"""开票申请加"已冲红"时间：退款不等冲红（D-074）——已开发票的订单先退款，之后负责人按退款金额开红字发票，
在管理台标记已冲红，"要冲红"列表才不再列出它。只加一列可空字段，不改已有数据。

编号 0036 是并行开发时预留的；main 上已是 0032 → 0037（运维日报 access_daily）→ 0038（纠错处理结果），
本迁移接在 0038 后面，避免出现两个 head。0033–0035 仍被并行的 PR 占用（#290、#297、#296），
谁后合并谁把 down_revision 接到当时 main 最新的那个。

Revision ID: 0036
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0036"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_requests", sa.Column("reversed_at", mysql.DATETIME(fsp=6), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("invoice_requests", "reversed_at")
