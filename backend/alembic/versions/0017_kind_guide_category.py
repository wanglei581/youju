"""文件类型加"资讯与指南"、文件加项目类别列（D-051 第二阶段）。

- 人工定类型（kind_override）允许"资讯与指南"。
- rule_category：按标题规则算出的项目类别，与 rule_kind 一样由 youju reclassify 填好
  （admin 服务启动时在迁移之后运行），新入库的文件入库时就算好；还没算的筛选时当场按规则判断。

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_OLD = "kind_override IS NULL OR kind_override IN ('申报通知', '名单', '政策', '其他')"
_NEW = (
    "kind_override IS NULL OR kind_override IN ('申报通知', '名单', '政策', '资讯与指南', '其他')"
)


def upgrade() -> None:
    op.add_column("documents", sa.Column("rule_category", sa.String(length=16), nullable=True))
    op.drop_constraint("ck_document_kind_override", "documents", type_="check")
    op.create_check_constraint("ck_document_kind_override", "documents", _NEW)


def downgrade() -> None:
    # 旧版本不认识"资讯与指南"：人工定的回到"其他"；规则算的清空，旧版本启动时按旧规则重算
    op.execute("UPDATE documents SET kind_override = '其他' WHERE kind_override = '资讯与指南'")
    op.execute("UPDATE documents SET rule_kind = NULL WHERE rule_kind = '资讯与指南'")
    op.drop_constraint("ck_document_kind_override", "documents", type_="check")
    op.create_check_constraint("ck_document_kind_override", "documents", _OLD)
    op.drop_column("documents", "rule_category")
