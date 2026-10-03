"""固定模板周报方案与人工发布快照。

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("provinces", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("weekday", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.CheckConstraint("weekday BETWEEN 1 AND 7", name="ck_digest_profile_weekday"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.create_table(
        "digest_issues",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body_html", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("body_text", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column(
            "generated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("note", sa.String(500), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["profile_id"], ["digest_profiles.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("profile_id", "week_start", name="uq_digest_issue_week"),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_digest_issue_status"),
        sa.CheckConstraint("week_end >= week_start", name="ck_digest_issue_interval"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    profile = sa.table(
        "digest_profiles",
        sa.column("name", sa.String()),
        sa.column("provinces", sa.JSON()),
        sa.column("keywords", sa.JSON()),
        sa.column("columns", sa.JSON()),
    )
    op.bulk_insert(
        profile,
        [
            {
                "name": "全国版",
                "provinces": [],
                "keywords": [],
                "columns": dict.fromkeys(("applications", "deadlines", "lists", "policies"), True),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("digest_issues")
    op.drop_table("digest_profiles")
