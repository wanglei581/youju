"""查名单站内闭环（D-088、D-091）：名单附件读取的结果，和每条条目在原文里的那一行。

- list_evidence_sources：名单附件（原站链接）读了没有、读全没有、用的是哪版规则；
- list_entry_locations：每条名单条目命中的原文行（最多 5 行），个人信息列已去掉、号码已遮。
  只存命中行，不存整份名单；条目重新解析（删了重写）时随外键删除，下一轮重读；
- signing_keys：服务端签名用的随机密钥，迁移时生成。查名单结果里的条目编号带签名，只有查到这家
  企业的人拿得到，不能按顺序把整份名单一条条翻出来。

名单附件不进给 AI 用的附件文字表（attachment_text_*），也不存副本。admin 的定时任务
（youju list-evidence）写前两张表；api 只读（api 账号已有 youju.* 的 SELECT，不另加授权）。

编号：10/24 曾为 Codex 的名单查询修订号（数据库触发器）预留 0041；D-091 定过审版不带那套翻页，
触发器在云数据库上要额外权限、没有验证过，所以这次不上，0041 改给这里。

Revision ID: 0041
Revises: 0040
"""

import secrets

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0041"
down_revision = "0040"
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
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("pages_read", sa.Integer(), nullable=True),
        sa.Column("extractor", sa.Integer(), nullable=False),
        sa.Column("locator", sa.Integer(), nullable=False),
        sa.Column("entries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("located", sa.Integer(), server_default="0", nullable=False),
        sa.Column("checked_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "status IN ('ok', 'partial', 'failed')", name="ck_list_evidence_sources_status"
        ),
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
    op.create_table(
        "signing_keys",
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("key_hex", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("name"),
        **OPTIONS,
    )
    op.get_bind().execute(
        sa.text("INSERT INTO signing_keys (name, key_hex) VALUES ('list_entry', :key)"),
        {"key": secrets.token_hex(32)},
    )


def downgrade() -> None:
    op.drop_table("signing_keys")
    op.drop_table("list_entry_locations")
    op.drop_table("list_evidence_sources")
