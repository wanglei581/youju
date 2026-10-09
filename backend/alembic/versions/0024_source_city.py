"""来源城市：市级来源使用，其他层级为空。

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("city", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "city")
