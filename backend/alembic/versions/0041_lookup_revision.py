"""Strict lookup revision: semantic writes and their version commit together.

MySQL FK cascades do not fire child triggers. documents BEFORE DELETE therefore
invalidates explicitly, before cascaded entries/merges disappear. The singleton
row serializes relevant writers; deadlock victims must roll back their entire
transaction (never retry only a failed statement). No runtime API write grant.

Revision ID: 0041
Revises: 0040
"""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

FIELDS = {
    "list_entries": (
        "id",
        "document_id",
        "source_url",
        "name",
        "normalized_name",
        "credit_code",
        "sequence",
        "category",
        "batch",
        "region",
    ),
    "document_merges": ("duplicate_id", "canonical_id", "active"),
    "documents": (
        "id",
        "source_id",
        "title",
        "publish_date",
        "url",
        "kind_override",
        "rule_category",
        "taken_down_at",
        "link_gone_at",
    ),
    "sources": ("id", "rights_level", "level", "province", "city", "district", "dept_full_name"),
}
BUMP = """
UPDATE lookup_revision SET revision = revision + 1 WHERE id = 1;
IF ROW_COUNT() <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'lookup revision missing';
END IF;
"""


def upgrade():
    op.create_table(
        "lookup_revision",
        # MySQL 不允许检查约束引用自增列：单行表的 id 不自增，固定写 1
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("cursor_key", sa.String(64), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_lookup_revision_singleton"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
    )
    op.get_bind().execute(
        sa.text("INSERT INTO lookup_revision (id, revision, cursor_key) VALUES (1, 1, :key)"),
        {"key": secrets.token_hex(32)},
    )
    for table, fields in FIELDS.items():
        # Inserting a document/source cannot create a list result until entries or
        # merges are written. Source deletion is RESTRICT while documents exist.
        events = (
            ("INSERT", "UPDATE", "DELETE")
            if table in ("list_entries", "document_merges")
            else (("UPDATE", "DELETE") if table == "documents" else ("UPDATE",))
        )
        for event in events:
            body = BUMP
            if event == "UPDATE":
                changed = " OR ".join(f"NOT (OLD.`{f}` <=> NEW.`{f}`)" for f in fields)
                body = f"IF {changed} THEN {BUMP} END IF;"
            op.execute(
                sa.text(
                    f"CREATE TRIGGER lookup_{table}_{event.lower()} BEFORE {event} ON `{table}` FOR EACH ROW BEGIN {body} END"
                )
            )


def downgrade():
    for table in FIELDS:
        for event in ("insert", "update", "delete"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS lookup_{table}_{event}"))
    op.drop_table("lookup_revision")
