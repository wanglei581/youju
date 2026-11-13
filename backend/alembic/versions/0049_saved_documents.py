"""我的保存（D-123：官方失效的通知 7 天后清理，用户可以永久保存或导出；模块 youju/saved.py）：
一张新表，只新增。

- saved_documents：用户保存的通知副本。存保存那一刻阅读页给的正文和附件文字（已脱敏、按页面说法
  换过的那一份，和阅读页一样分段），以及标题、发文机关、来源网站、发布日期、原网页打不开的日期、
  保存时间；不存附件原件（D-123 ③，省服务器硬盘）。
- document_id 不建外键：本机到期清理删掉通知以后（经同步，服务器上的通知行跟着删），用户的副本
  还要在；建了级联外键副本就跟着没了，建 RESTRICT 又会挡住清理。编号留着，下架时按它删副本
  （D-123 ④；documents 的自增编号不回收，不会撞上以后的新通知）。
- user_id 随用户级联删除（注销时一起删，D-123 ③）。
- documents 加一列 link_gone_seen_at（可空）：原网页检查最近一次确认还打不开的时间（判断不了的不算），
  到期清理要求最近确认过，网站临时出故障时不清理（D-123 ①"网站临时出故障不能误删"）。加在表尾的
  可空列，MySQL 8 直接改元数据，不重建表；已有的行是空的，下次检查确认了才填。
- 不要新授权才能部署：这次只建表、加列。打开 YOUJU_GONE_CLEANUP_ENABLED 之前，api 账号要对这张表有
  INSERT、UPDATE、DELETE（见 deploy/mysql_accounts.sql 的"我的保存"一段）。

编号 0049 是协调会话 11/8 给的（0047 留给问一问第二批，0048 是覆盖看板）。接在 main 现在的头 0046
后面；0047、0048 先合进 main 的，合并的人把 down_revision 改成那时的头（只有一个迁移头）。

Revision ID: 0049
Revises: 0046
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0049"
down_revision = "0046"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.add_column("documents", sa.Column("link_gone_seen_at", mysql.DATETIME(fsp=6), nullable=True))
    op.create_table(
        "saved_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("site", sa.String(500), nullable=False),
        sa.Column("region", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("publish_date", sa.Date(), nullable=True),
        sa.Column("gone_on", sa.Date(), nullable=False),
        sa.Column("content", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("saved_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "document_id", name="uq_saved_documents_user_doc"),
        **OPTIONS,
    )
    op.create_index("ix_saved_documents_user_saved", "saved_documents", ["user_id", "saved_at"])
    op.create_index("ix_saved_documents_document", "saved_documents", ["document_id"])


def downgrade() -> None:
    op.drop_table("saved_documents")
    op.drop_column("documents", "link_gone_seen_at")
