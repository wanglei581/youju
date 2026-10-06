"""文件加按标题算的支持方式、产业方向两列（D-051 第三阶段）。

rule_supports / rule_industries：按标题规则算出的筛选标签（见 youju.title_tags），逗号分隔，没有标签
存空串；与 rule_category 一样由 youju reclassify 填好（admin 服务启动时在迁移之后运行），新入库的
文件入库时就算好；还没算的（NULL）筛选时当场按规则判断。只用于筛选，不对外展示。

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("rule_supports", sa.String(length=40), nullable=True))
    op.add_column("documents", sa.Column("rule_industries", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "rule_industries")
    op.drop_column("documents", "rule_supports")
