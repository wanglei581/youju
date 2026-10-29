"""卡里的 AI 次数（D-109 甲：AI 次数加进使用期卡；模块 youju/ai_credits.py）：两张新表，只新增。

- ai_credit_grants：每给一次次数记一笔（买卡天数 × 10、续买多送 30、加量体验 30、上线补给）。
  挂在使用期段（pass_id，段删了一起删）或加量体验（trial_id，随用户删）上；已扣 used、占着 held
  记在这一行，锁住这一行就读到最新的数。用户删了置空（注销时另外收回）。
- ai_credit_uses：每占一次记一行（reserved 占着 / charged 扣了 / released 退回），use_key 唯一；
  不建任务外键，任务删了凭据还在；跟着那一笔删。
- 不要新授权才能部署：这次只建表，没有代码写它们。接进解读、对照、问一问以后，api 账号要对这两张表
  有 SELECT、INSERT、UPDATE（加在 deploy/mysql_accounts.sql，和那一版一起执行）。

编号 0046 是协调会话 10/29 给的；main 上现在最新是 0043，0044（#397）、0045（AI 会话）还在 PR 里，
谁后合并谁把 down_revision 接到当时 main 最新的那个（协调会话合并时改）。

Revision ID: 0046
Revises: 0043
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0046"
down_revision = "0043"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "ai_credit_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("pass_id", sa.Integer(), nullable=True),
        sa.Column("trial_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("held", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("granted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint("(pass_id IS NULL) <> (trial_id IS NULL)", name="ck_ai_credit_grants_origin"),
        sa.CheckConstraint(
            "kind IN ('purchase', 'renewal', 'trial', 'backfill')", name="ck_ai_credit_grants_kind"
        ),
        sa.CheckConstraint(
            "amount > 0 AND used >= 0 AND held >= 0 AND used + held <= amount",
            name="ck_ai_credit_grants_counts",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pass_id"], ["workbench_passes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trial_id"], ["boost_trials.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pass_id", "kind", name="uq_ai_credit_grants_pass_kind"),
        sa.UniqueConstraint("trial_id", name="uq_ai_credit_grants_trial"),
        **OPTIONS,
    )
    op.create_index("ix_ai_credit_grants_user", "ai_credit_grants", ["user_id"])
    op.create_table(
        "ai_credit_uses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("use_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), server_default="reserved", nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("release_reason", sa.String(32), nullable=True),
        sa.Column("reserved_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("settled_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("pool_day", sa.Date(), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'charged', 'released')", name="ck_ai_credit_uses_state"
        ),
        sa.CheckConstraint(
            "kind IN ('interpret', 'clause', 'ask', 'photo', 'compile')",
            name="ck_ai_credit_uses_kind",
        ),
        sa.CheckConstraint("reason IN ('free_used', 'site_full')", name="ck_ai_credit_uses_reason"),
        sa.ForeignKeyConstraint(["grant_id"], ["ai_credit_grants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("use_key", name="uq_ai_credit_uses_key"),
        **OPTIONS,
    )
    op.create_index("ix_ai_credit_uses_grant_state", "ai_credit_uses", ["grant_id", "state"])
    op.create_index("ix_ai_credit_uses_user", "ai_credit_uses", ["user_id"])
    op.create_index("ix_ai_credit_uses_pool_day", "ai_credit_uses", ["pool_day"])


def downgrade() -> None:
    op.drop_table("ai_credit_uses")
    op.drop_table("ai_credit_grants")
