"""附件副本记下检查时用的规则版本（rules_version）：附件规则改了就把版本号加 1，api 只提供
版本号等于当前版本的副本，旧版本的副本由定时任务按新规则重查后恢复或删掉。

已有的副本记为 0（加版本号之前的规则），上线后立即不再提供，等重查。

编号说明：0021 留给同时在做的转发组迁移（feat/repost-dedupe）；两边谁后合并，谁把自己的
down_revision 改成先合并的那个，保持只有一个 head。

Revision ID: 0022
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attachment_copies",
        sa.Column("rules_version", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("attachment_copies", "rules_version")
