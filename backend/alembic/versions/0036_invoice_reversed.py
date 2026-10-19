"""开票申请加"已冲红"时间：退款不等冲红（D-074）——已开发票的订单先退款，之后负责人按退款金额开红字发票，
在管理台标记已冲红，"要冲红"列表才不再列出它。只加一列可空字段，不改已有数据。

迁移编号 0033–0035 已被并行的 PR 占用（#290、#297、#296），它们和本迁移都接在 0032 后面；
合并时由后合并的一方把 down_revision 接成一条链。

Revision ID: 0036
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0036"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_requests", sa.Column("reversed_at", mysql.DATETIME(fsp=6), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("invoice_requests", "reversed_at")
