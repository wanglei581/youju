"""顾问服务说明单独同意记录。

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("advisor_agreement_version", sa.String(20), nullable=True))
    op.add_column("users", sa.Column("advisor_agreement_at", mysql.DATETIME(fsp=6), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "advisor_agreement_at")
    op.drop_column("users", "advisor_agreement_version")
