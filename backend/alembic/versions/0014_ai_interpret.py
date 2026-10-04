"""AI 政策原句摘录任务。

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_advisor_tasks_kind", "advisor_tasks", type_="check")
    op.create_check_constraint(
        "ck_advisor_tasks_kind", "advisor_tasks", "kind IN ('compare', 'track', 'interpret')"
    )
    for name in ("input_json", "result_json", "snapshot"):
        op.add_column("advisor_tasks", sa.Column(name, sa.JSON(), nullable=True))
    for name, size in (("cache_key", 64), ("prompt_version", 32), ("model", 128)):
        op.add_column("advisor_tasks", sa.Column(name, sa.String(size), nullable=True))
    op.create_index("ix_advisor_tasks_cache", "advisor_tasks", ["cache_key", "finished_at"])


def downgrade() -> None:
    # 回退前清掉新类型，保留原有数据工具任务与台账。
    op.execute("DELETE FROM advisor_tasks WHERE kind = 'interpret'")
    op.execute("DELETE FROM advisor_usage WHERE kind = 'interpret'")
    op.drop_index("ix_advisor_tasks_cache", table_name="advisor_tasks")
    for name in ("model", "prompt_version", "cache_key", "snapshot", "result_json", "input_json"):
        op.drop_column("advisor_tasks", name)
    op.drop_constraint("ck_advisor_tasks_kind", "advisor_tasks", type_="check")
    op.create_check_constraint(
        "ck_advisor_tasks_kind", "advisor_tasks", "kind IN ('compare', 'track')"
    )
