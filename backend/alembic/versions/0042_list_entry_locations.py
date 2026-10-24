"""查名单站内闭环：名单附件取文字的状态，和每条条目在原文里的那一行。

- list_evidence_sources：名单附件（原站链接）取文字的结果和版本；
- list_entry_locations：每条名单条目命中的原文行（最多 5 行，号码已遮），只存命中行，不存整份
  名单。条目重解析（删了重写）时随外键删除，下一轮重取。

名单附件不进给 AI 用的附件文字表（attachment_text_*），也不存副本。

编号：10/23 Claude 接手第四批时占 0042，接在 Codex 的 0041（名单查询修订号）后面。
api 账号已有 youju.* 的 SELECT，不另加授权；只有 admin 的定时任务写。

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "list_evidence_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(16), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("file_type", sa.String(8), nullable=True),
        sa.Column("extractor", sa.Integer(), nullable=False),
        sa.Column("locator", sa.Integer(), nullable=False),
        sa.Column("entries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("located", sa.Integer(), server_default="0", nullable=False),
        sa.Column("checked_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint("status IN ('ok', 'failed')", name="ck_list_evidence_sources_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_hash", name="uq_list_evidence_sources_url"),
        **OPTIONS,
    )
    op.create_table(
        "list_entry_locations",
        sa.Column("entry_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("computed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "state IN ('ok', 'multiple', 'ambiguous', 'notfound', 'no_name_column')",
            name="ck_list_entry_locations_state",
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["list_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["list_evidence_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entry_id"),
        **OPTIONS,
    )
    op.create_index("ix_list_entry_locations_source", "list_entry_locations", ["source_id"])


def downgrade() -> None:
    op.drop_table("list_entry_locations")
    op.drop_table("list_evidence_sources")
