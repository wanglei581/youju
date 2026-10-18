"""加量体验（D-070 ③）：每个用户一行体验记录；注销后的去标识凭据记在 reward_claims。

开关 YOUJU_BOOST_TRIAL_ENABLED 默认关；api 账号的权限见 deploy/mysql_accounts.sql 的"加量体验权限"。

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "boost_trials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("ends_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_boost_trials_user"),
        sa.CheckConstraint("ends_at > started_at", name="ck_boost_trials_period"),
        **OPTIONS,
    )
    op.drop_constraint("ck_reward_claim_kind", "reward_claims", type_="check")
    op.create_check_constraint(
        "ck_reward_claim_kind",
        "reward_claims",
        "kind IN ('first_batch', 'referred', 'referral', 'boost_trial')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM reward_claims WHERE kind = 'boost_trial'")
    op.drop_constraint("ck_reward_claim_kind", "reward_claims", type_="check")
    op.create_check_constraint(
        "ck_reward_claim_kind", "reward_claims", "kind IN ('first_batch', 'referred', 'referral')"
    )
    # 体验期提交记在 advisor_usage 的 trial 账本上；回退后没有代码会读它们。
    # 旧代码失败退次数时只认免费账本，体验任务先标成已退，免得退到每月免费次数上
    op.execute("DELETE FROM advisor_usage WHERE kind = 'trial'")
    op.execute("UPDATE advisor_tasks SET usage_released = 1 WHERE usage_source = 'trial'")
    op.drop_table("boost_trials")
