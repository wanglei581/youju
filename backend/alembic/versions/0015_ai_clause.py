"""条款对照复用现有 AI 任务列。

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_advisor_tasks_kind", "advisor_tasks", type_="check")
    op.create_check_constraint(
        "ck_advisor_tasks_kind",
        "advisor_tasks",
        "kind IN ('compare', 'track', 'interpret', 'clause')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM advisor_tasks WHERE kind = 'clause'")
    op.execute("DELETE FROM advisor_usage WHERE kind = 'clause'")
    op.drop_constraint("ck_advisor_tasks_kind", "advisor_tasks", type_="check")
    op.create_check_constraint(
        "ck_advisor_tasks_kind", "advisor_tasks", "kind IN ('compare', 'track', 'interpret')"
    )
