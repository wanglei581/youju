"""AI 板块（D-075）：每份文件能不能做 AI 解读、两个服务的 AI 设置心跳。

- ai_eligibility：列表行上的"AI 解读"按钮读这张表，admin 的定时任务算好写入
  （youju ai-eligibility 每 10 分钟重算有变化的，ai-eligibility-rebuild 每天夜里全部重算）。
- ai_heartbeat：admin 的后台任务每分钟写一行（service=admin），api 服务最多每 5 分钟写一行
  （service=api）；指纹对不上时 api 不收 AI 任务，admin 发提醒。
- document_details.fetched_at 加索引：每 10 分钟按它找最近重新抓过的正文。

api 账号已有整个库的读权限，读这两张表不用另加授权。api 写自己那行心跳要执行
deploy/mysql_accounts.sql 的"AI 设置心跳权限"一行；没执行时 api 照常工作，只是收不到"两个服务
设置不一致"的提醒。

编号：0033–0038 已被占用（0033 #290、0034 #297、0035 #296、0036 #304、0037 和 0038 成熟度会话），
按约定 down_revision 先接 main 上的 0032，后合并的一方改 down_revision。

Revision ID: 0039
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0039"
down_revision = "0032"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}
REASONS = "'unavailable', 'list', 'kind', 'source', 'no_body', 'too_long'"


def upgrade() -> None:
    # 每 10 分钟按抓取时间找最近重新抓过的正文，没有索引就要扫整张正文表
    op.create_index("ix_document_details_fetched_at", "document_details", ["fetched_at"])
    op.create_table(
        "ai_eligibility",
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(16), nullable=True),
        sa.Column("computed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("document_id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            f"reason IS NULL OR reason IN ({REASONS})", name="ck_ai_eligibility_reason"
        ),
        **OPTIONS,
    )
    op.create_table(
        "ai_heartbeat",
        sa.Column("service", sa.String(16), nullable=False),
        sa.Column("beat_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("alerted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("service"),
        sa.CheckConstraint("service IN ('admin', 'api')", name="ck_ai_heartbeat_service"),
        **OPTIONS,
    )


def downgrade() -> None:
    # 两张表都是算出来的，回退后旧代码不读它们；重新升级时由定时任务重算、重写
    op.drop_table("ai_heartbeat")
    op.drop_table("ai_eligibility")
    op.drop_index("ix_document_details_fetched_at", table_name="document_details")
