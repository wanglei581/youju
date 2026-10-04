"""按次比对支付后台，默认关闭。

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "pay_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("pseudonym", sa.String(64), nullable=True),
        sa.Column("out_trade_no", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(128), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("row_limit", sa.Integer(), nullable=False),
        sa.Column("env", sa.Integer(), nullable=False),
        sa.Column("input_rows", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("wx_order_id", sa.String(128), nullable=True),
        sa.Column("paid_fee", sa.Integer(), nullable=True),
        sa.Column("paid_time", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("platform", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("task_completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("rerun_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refund_order_id", sa.String(32), nullable=True),
        sa.Column("refund_status", sa.String(16), nullable=True),
        sa.Column("refund_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refund_retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("refund_started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("query_retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("goods_notified", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("provide_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("alerted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("out_trade_no", name="uq_pay_orders_trade"),
        sa.UniqueConstraint("refund_order_id"),
        **OPTIONS,
    )
    op.create_table(
        "pay_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["order_id"], ["pay_orders.id"], ondelete="SET NULL"),
        **OPTIONS,
    )
    op.create_table(
        "pay_reconciliations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("local_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("local_fee", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bill_count", sa.Integer(), nullable=True),
        sa.Column("bill_fee", sa.Integer(), nullable=True),
        sa.Column("differences", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day"),
        **OPTIONS,
    )
    op.create_index("ix_pay_orders_user_created", "pay_orders", ["user_id", "created_at"])
    op.create_index("ix_pay_orders_status_created", "pay_orders", ["status", "created_at"])
    op.create_index("ix_pay_events_created", "pay_events", ["created_at"])
    op.add_column("advisor_tasks", sa.Column("pay_order_id", sa.Integer(), nullable=True))
    op.create_index("ix_advisor_tasks_pay_order_id", "advisor_tasks", ["pay_order_id"])
    op.create_foreign_key(
        "fk_advisor_tasks_pay_order",
        "advisor_tasks",
        "pay_orders",
        ["pay_order_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_advisor_tasks_pay_order", "advisor_tasks", type_="foreignkey")
    op.drop_index("ix_advisor_tasks_pay_order_id", table_name="advisor_tasks")
    op.drop_column("advisor_tasks", "pay_order_id")
    op.drop_table("pay_reconciliations")
    op.drop_table("pay_events")
    op.drop_table("pay_orders")
