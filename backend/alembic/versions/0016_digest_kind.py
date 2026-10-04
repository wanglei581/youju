"""周报期刊加稿件类型：周报和临期提醒同一方案每周各一份。

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 已有期刊都是周报。
    op.add_column(
        "digest_issues",
        sa.Column("kind", sa.String(16), server_default="weekly", nullable=False),
    )
    # 先建以 profile_id 开头的新 unique 约束，方案外键始终有可用索引，再删旧约束。
    op.create_unique_constraint(
        "uq_digest_issue_kind_week", "digest_issues", ["profile_id", "kind", "week_start"]
    )
    op.drop_constraint("uq_digest_issue_week", "digest_issues", type_="unique")
    op.create_check_constraint(
        "ck_digest_issue_kind", "digest_issues", "kind IN ('weekly', 'reminder')"
    )


def downgrade() -> None:
    # 回退前删掉临期提醒，保留周报期刊。
    op.execute("DELETE FROM digest_issues WHERE kind = 'reminder'")
    op.drop_constraint("ck_digest_issue_kind", "digest_issues", type_="check")
    op.create_unique_constraint(
        "uq_digest_issue_week", "digest_issues", ["profile_id", "week_start"]
    )
    op.drop_constraint("uq_digest_issue_kind_week", "digest_issues", type_="unique")
    op.drop_column("digest_issues", "kind")
