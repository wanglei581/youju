"""附件副本记下检查时用的规则版本（rules_version）：附件规则改了就把版本号加 1，api 只提供
版本号等于当前版本的副本，旧版本的副本由定时任务按新规则重查后恢复或删掉。

已有的副本记为 0（加版本号之前的规则），上线后立即不再提供，等重查。rechecked_at 记上次重查的时间，
重查按它排，读不到的排到后面。

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attachment_copies",
        sa.Column("rules_version", sa.Integer(), server_default="0", nullable=False),
    )
    # 上次按新规则重查的时间：重查按它排，读不到的排到后面
    op.add_column(
        "attachment_copies", sa.Column("rechecked_at", mysql.DATETIME(fsp=6), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("attachment_copies", "rechecked_at")
    op.drop_column("attachment_copies", "rules_version")
