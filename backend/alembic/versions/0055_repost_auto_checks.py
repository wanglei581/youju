"""转发组自动合并的本机抽查记录。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "repost_auto_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("round", sa.String(40), nullable=False),
        sa.Column("purpose", sa.String(8), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("rule_version", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("checked_by", sa.String(40), nullable=False),
        sa.Column("checked_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint("round", "group_id", name="uq_repost_auto_checks_round_group"),
        sa.CheckConstraint("purpose IN ('gate', 'weekly')", name="ck_repost_auto_checks_purpose"),
        sa.CheckConstraint("verdict IN ('correct', 'wrong')", name="ck_repost_auto_checks_verdict"),
    )
    op.create_index(
        "ix_repost_auto_checks_purpose_version_time",
        "repost_auto_checks",
        ["purpose", "rule_version", "checked_at"],
    )


def downgrade():
    op.drop_index("ix_repost_auto_checks_purpose_version_time", table_name="repost_auto_checks")
    op.drop_table("repost_auto_checks")
