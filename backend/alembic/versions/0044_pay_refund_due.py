"""付费：苹果退款问询"已核实应当退款"的标记（付费服务协议"开卖那一版要一起做的"第 8 项③）。

- pay_orders 加两列可空的 refund_due_at、refund_due_note：负责人用 `youju pay-refund-due` 核实
  一笔应当退款（因我们的原因不能使用、未成年人未经监护人同意购买等）时写上，苹果来问时照它答
  "建议退款"；清除标记时两列都置空。只加列，不改已有数据。
- 不要新授权：api 账号只读这两列（答复苹果问询），写入在 admin 的命令里。

部署顺序：先发 admin（启动时跑这个迁移），再发 api。

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pay_orders", sa.Column("refund_due_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("pay_orders", sa.Column("refund_due_note", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("pay_orders", "refund_due_note")
    op.drop_column("pay_orders", "refund_due_at")
