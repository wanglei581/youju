"""内容安全的处置记录（D-093 门槛第 4 条；带 AI 提审补齐清单第 4 道）。

ai_safety_hits：本地高危词库或微信内容安全接口拦下的 AI 结果和用户提问，一次一行。
- AI 解读、条款对照的结果被拦：记通知编号和任务编号，不存结果文字，不记用户编号；
- 问一问的提问被拦（问一问 PR-2 起）：记那句话（最多 200 字）和去标识化的用户编号（同访问日志）。
不建外键：删任务、删对话、注销都不动它；190 天到期由每天的清理删除（advisor_tasks.cleanup_advisor）。

只有 admin 写（处理 AI 任务时）；问一问上线时再给 api 账号加只能新增的授权（deploy/mysql_accounts.sql）。

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "ai_safety_hits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("uid", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(8), nullable=False),
        sa.Column("layer", sa.String(8), nullable=False),
        sa.Column("reason_json", sa.JSON(), nullable=True),
        sa.Column("text", sa.String(200), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "kind IN ('interpret', 'clause', 'find', 'ask')", name="ck_ai_safety_hits_kind"
        ),
        sa.CheckConstraint("stage IN ('question', 'answer')", name="ck_ai_safety_hits_stage"),
        sa.CheckConstraint("layer IN ('local', 'wechat')", name="ck_ai_safety_hits_layer"),
        sa.PrimaryKeyConstraint("id"),
        **OPTIONS,
    )
    op.create_index("ix_ai_safety_hits_expires", "ai_safety_hits", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_safety_hits_expires", table_name="ai_safety_hits")
    op.drop_table("ai_safety_hits")
