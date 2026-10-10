"""客户工作台奖励天数与一层同行引荐。

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("invite_code", sa.String(16), nullable=True))
    op.create_unique_constraint("uq_users_invite_code", "users", ["invite_code"])
    op.add_column("users", sa.Column("invited_by_user_id", sa.Integer(), nullable=True))
    op.create_index("ix_users_invited_by_user_id", "users", ["invited_by_user_id"])
    op.create_foreign_key(
        "fk_users_invited_by", "users", "users", ["invited_by_user_id"], ["id"], ondelete="SET NULL"
    )
    op.add_column("users", sa.Column("invited_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("users", sa.Column("batch_completed_at", mysql.DATETIME(fsp=6), nullable=True))
    op.create_table(
        "pass_rewards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("requested_days", sa.Integer(), nullable=False),
        sa.Column("granted_days", sa.Integer(), nullable=False),
        sa.Column("ref_type", sa.String(32), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("granted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.UniqueConstraint("ref_type", "ref_id", name="uq_pass_rewards_ref"),
        sa.CheckConstraint(
            "kind IN ('first_batch', 'correction', 'source', 'referral', 'deadline_clue')",
            name="ck_pass_rewards_kind",
        ),
        sa.CheckConstraint(
            "requested_days > 0 AND granted_days >= 0 AND granted_days <= requested_days",
            name="ck_pass_rewards_days",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index("ix_pass_rewards_user_granted", "pass_rewards", ["user_id", "granted_at"])
    op.alter_column("workbench_passes", "order_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("workbench_passes", sa.Column("reward_id", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_workbench_pass_reward", "workbench_passes", ["reward_id"])
    op.create_foreign_key(
        "fk_workbench_pass_reward", "workbench_passes", "pass_rewards", ["reward_id"], ["id"]
    )
    op.create_check_constraint(
        "ck_workbench_pass_origin", "workbench_passes", "(order_id IS NULL) <> (reward_id IS NULL)"
    )


def downgrade() -> None:
    # 奖励段没有订单，先删除；任务 pass_id 由外键置空。
    op.execute("DELETE FROM workbench_passes WHERE reward_id IS NOT NULL")
    op.drop_constraint("ck_workbench_pass_origin", "workbench_passes", type_="check")
    op.drop_constraint("fk_workbench_pass_reward", "workbench_passes", type_="foreignkey")
    op.drop_constraint("uq_workbench_pass_reward", "workbench_passes", type_="unique")
    op.drop_column("workbench_passes", "reward_id")
    op.alter_column("workbench_passes", "order_id", existing_type=sa.Integer(), nullable=False)
    op.drop_table("pass_rewards")
    op.drop_column("users", "batch_completed_at")
    op.drop_column("users", "invited_at")
    op.drop_constraint("fk_users_invited_by", "users", type_="foreignkey")
    op.drop_index("ix_users_invited_by_user_id", table_name="users")
    op.drop_column("users", "invited_by_user_id")
    op.drop_constraint("uq_users_invite_code", "users", type_="unique")
    op.drop_column("users", "invite_code")
