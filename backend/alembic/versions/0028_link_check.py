"""原网页还在不在：文件加上次检查时间、第一次 404/410 的时间、确认失效的时间。

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("link_checked_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("documents", sa.Column("link_suspect_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("documents", sa.Column("link_gone_at", mysql.DATETIME(fsp=6), nullable=True))
    op.create_index("ix_documents_link_checked_at", "documents", ["link_checked_at"])


def downgrade() -> None:
    op.drop_index("ix_documents_link_checked_at", table_name="documents")
    op.drop_column("documents", "link_gone_at")
    op.drop_column("documents", "link_suspect_at")
    op.drop_column("documents", "link_checked_at")
