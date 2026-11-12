"""保存 Claude 截止核对证据。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0053"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_deadlines", sa.Column("verification_quote", sa.Text(), nullable=True))
    op.add_column(
        "document_deadlines", sa.Column("verification_unclear_hash", sa.String(64), nullable=True)
    )
    op.add_column(
        "document_deadlines", sa.Column("verification_unclear_note", sa.Text(), nullable=True)
    )
    op.add_column(
        "document_deadlines", sa.Column("verification_corrected", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "document_deadlines",
        sa.Column("verification_unclear_at", mysql.DATETIME(fsp=6), nullable=True),
    )


def downgrade():
    for name in (
        "verification_unclear_at",
        "verification_corrected",
        "verification_unclear_note",
        "verification_unclear_hash",
        "verification_quote",
    ):
        op.drop_column("document_deadlines", name)
