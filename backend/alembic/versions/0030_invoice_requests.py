"""开票申请：每笔已完成订单一份（D-058 第一期），支付开关仍默认关闭。

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "invoice_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("title_type", sa.String(16), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("tax_id", sa.String(20), nullable=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("issued_at", mysql.DATETIME(fsp=6), nullable=True),
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
        sa.ForeignKeyConstraint(["order_id"], ["pay_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("order_id", name="uq_invoice_requests_order"),
        sa.CheckConstraint("title_type IN ('personal', 'company')", name="ck_invoice_title_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'issued', 'rejected')", name="ck_invoice_request_status"
        ),
        **OPTIONS,
    )
    op.create_index(
        "ix_invoice_requests_status_created", "invoice_requests", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_requests_status_created", table_name="invoice_requests")
    op.drop_table("invoice_requests")
