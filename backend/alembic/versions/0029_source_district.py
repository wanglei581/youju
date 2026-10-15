"""来源区县字段；旧来源保持 NULL，不推断历史区县。"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("district", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "district")
