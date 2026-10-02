"""文件下架、恢复历史及顾问结果文件依赖。

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, type_ in (
        ("taken_down_at", mysql.DATETIME(fsp=6)),
        ("takedown_reason", sa.String(16)),
        ("takedown_note", sa.String(300)),
        ("taken_down_by", sa.String(80)),
        ("restored_at", mysql.DATETIME(fsp=6)),
        ("restore_note", sa.String(300)),
        ("restored_by", sa.String(80)),
    ):
        op.add_column("documents", sa.Column(name, type_, nullable=True))
    op.create_index("ix_documents_taken_down_at", "documents", ["taken_down_at"])
    op.add_column("advisor_tasks", sa.Column("result_document_ids", sa.JSON(), nullable=True))
    op.create_table(
        "document_takedown_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(16), nullable=True),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column("operated_by", sa.String(80), nullable=False),
        sa.Column("operated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("action IN ('takedown', 'restore')", name="ck_takedown_event_action"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.create_index(
        "ix_takedown_events_document_id", "document_takedown_events", ["document_id", "id"]
    )


def downgrade() -> None:
    op.drop_table("document_takedown_events")
    op.drop_column("advisor_tasks", "result_document_ids")
    op.drop_index("ix_documents_taken_down_at", table_name="documents")
    for name in (
        "restored_by",
        "restore_note",
        "restored_at",
        "taken_down_by",
        "takedown_note",
        "takedown_reason",
        "taken_down_at",
    ):
        op.drop_column("documents", name)
