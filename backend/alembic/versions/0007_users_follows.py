"""用户、关注、一次性订阅授权、纠错与埋点。

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("openid", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_visited_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("agreement_version", sa.String(20)),
        sa.Column("agreement_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "follows",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("conditions", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_notified_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_follows_user", "follows", ["user_id"])
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("follow_id", sa.Integer, sa.ForeignKey("follows.id", ondelete="CASCADE")),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("status", sa.String(16), nullable=False, server_default="unused"),
        sa.Column("errcode", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('newDocs', 'deadline')", name="ck_subscription_kind"),
        sa.CheckConstraint(
            "status IN ('unused', 'sent', 'failed', 'refused', 'expired')",
            name="ck_subscription_status",
        ),
        sa.CheckConstraint(
            "(kind = 'newDocs' AND follow_id IS NOT NULL AND document_id IS NULL)"
            " OR (kind = 'deadline' AND document_id IS NOT NULL AND follow_id IS NULL)",
            name="ck_subscription_target",
        ),
    )
    op.create_index("ix_subscriptions_status_kind", "subscriptions", ["status", "kind"])
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("content", sa.String(500), nullable=False),
        sa.Column("check_result", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_feedback_user", "feedback", ["user_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("props", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_events_user_created", "events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("feedback")
    op.drop_table("subscriptions")
    op.drop_table("follows")
    op.drop_table("users")
