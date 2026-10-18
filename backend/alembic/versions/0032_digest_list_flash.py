"""周报期刊加名单速报和本期清单（公众号方案 PR #287 第 3 件事）。

- subject_id：名单速报一份名单一篇，记那份名单的文件编号；周报和临期提醒为 0，唯一约束照旧
  是同一方案每周每种稿件一份。
- items：周报这一期的全部条目（小程序"本期清单"用；正文每省只列 5 条）。旧期刊为空。

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "digest_issues",
        sa.Column("subject_id", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("digest_issues", sa.Column("items", sa.JSON(), nullable=True))
    # 先建以 profile_id 开头的新 unique 约束，方案外键始终有可用索引，再删旧约束（同 0016）。
    op.create_unique_constraint(
        "uq_digest_issue_subject",
        "digest_issues",
        ["profile_id", "kind", "week_start", "subject_id"],
    )
    op.drop_constraint("uq_digest_issue_kind_week", "digest_issues", type_="unique")
    op.drop_constraint("ck_digest_issue_kind", "digest_issues", type_="check")
    op.create_check_constraint(
        "ck_digest_issue_kind", "digest_issues", "kind IN ('weekly', 'reminder', 'list_flash')"
    )


def downgrade() -> None:
    # 回退前删掉名单速报，保留周报和临期提醒（它们的 subject_id 都是 0，旧约束不会冲突）。
    op.execute("DELETE FROM digest_issues WHERE kind = 'list_flash'")
    op.drop_constraint("ck_digest_issue_kind", "digest_issues", type_="check")
    op.create_check_constraint(
        "ck_digest_issue_kind", "digest_issues", "kind IN ('weekly', 'reminder')"
    )
    op.create_unique_constraint(
        "uq_digest_issue_kind_week", "digest_issues", ["profile_id", "kind", "week_start"]
    )
    op.drop_constraint("uq_digest_issue_subject", "digest_issues", type_="unique")
    op.drop_column("digest_issues", "items")
    op.drop_column("digest_issues", "subject_id")
