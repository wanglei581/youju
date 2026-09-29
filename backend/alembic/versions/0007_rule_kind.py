"""文件按标题规则算出的类型存成一列（rule_kind），筛选时只比较这一列（验收 C1：搜索要快）。

原来按类型筛选时，标题规则翻译成 800 多个模糊匹配条件逐行计算，1.7 万份文件时要 2 秒。
这一列由 youju reclassify 填好（admin 服务启动时在迁移之后运行），新入库的文件入库时就算好。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("rule_kind", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "rule_kind")
