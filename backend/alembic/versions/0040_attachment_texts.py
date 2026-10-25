"""附件读入第一步（D-079、D-080，《附件读入方案》第六节第 1 步）：给 AI 用的附件文字，三层记。

- attachment_text_urls：附件（原网站链接），指向现在的内容版本；
- attachment_text_versions：内容版本（文件内容的 SHA-256），能不能取出文字、取了多少；
- attachment_text_segments：文字，按阅读顺序一段一行，入库前遮掉身份证号、手机号、银行账号。

admin 的附件副本定时任务（youju cache-attachments）下载时顺带取文字写这三张表。这一步 AI 还不读，
api 不读也不写它们，不用另加授权。

编号：10/20 开工前查过、在共享笔记登记了 0040。10/23 同步 main：#296 的 0035 已合并、接在 0039
后面，main 上最新的是 0035，所以 down_revision 改接 0035。#290 的 0033 也打算接 0035，谁后合并谁改接。

Revision ID: 0040
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0040"
down_revision = "0035"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}
REASONS = "'scan', 'empty', 'broken', 'format', 'too_large', 'webpage', 'gone', 'unavailable'"


def upgrade() -> None:
    op.create_table(
        "attachment_text_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("file_type", sa.String(8), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(16), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("extractor", sa.Integer(), nullable=False),
        sa.Column("segments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chars", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("scanned_pages", sa.Integer(), nullable=True),
        sa.Column("masked", sa.Integer(), server_default="0", nullable=False),
        sa.Column("extracted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", name="uq_attachment_text_versions_sha256"),
        sa.CheckConstraint("status IN ('ok', 'failed')", name="ck_attachment_text_versions_status"),
        sa.CheckConstraint(
            f"reason IS NULL OR reason IN ({REASONS})", name="ck_attachment_text_versions_reason"
        ),
        **OPTIONS,
    )
    op.create_table(
        "attachment_text_urls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("declared_type", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("reason", sa.String(16), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("extractor", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("checked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("changed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_hash", name="uq_attachment_text_urls_url"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["attachment_text_versions.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ok', 'failed')", name="ck_attachment_text_urls_status"
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR reason IN ({REASONS})", name="ck_attachment_text_urls_reason"
        ),
        **OPTIONS,
    )
    op.create_index("ix_attachment_text_urls_changed_at", "attachment_text_urls", ["changed_at"])
    op.create_table(
        "attachment_text_segments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("text", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("loc", sa.JSON(), nullable=True),
        sa.Column("cells", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "n", name="uq_attachment_text_segments_n"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["attachment_text_versions.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("kind IN ('p', 'head', 'row')", name="ck_attachment_text_segments_kind"),
        **OPTIONS,
    )


def downgrade() -> None:
    # 三张表的内容都能由定时任务重新下载、重新取出；回退后旧代码不读它们
    op.drop_table("attachment_text_segments")
    op.drop_index("ix_attachment_text_urls_changed_at", table_name="attachment_text_urls")
    op.drop_table("attachment_text_urls")
    op.drop_table("attachment_text_versions")
