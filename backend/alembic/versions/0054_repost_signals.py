"""转发归组改增量（D-129 ②）：repost_signals 存每份文件正文开头算出的归组信号。

有这一行也表示归组程序处理过这份文件：同步据此让新文件等归组跑完再推给服务器（迁移线的约定：
"可以推了"= 这张表里有这份文件的行，只看有没有，不看时间）。所以迁移时给现有的全部文件补一行
（正文信号空着，第一轮归组把窗口里、窗口组成员里有详情的重算），不然老文件会被扣住。
这张表只在本机用，不进公开快照、不同步；服务器跑迁移也会建表、补行，没人读。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "repost_signals",
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("cited", sa.JSON(), nullable=False),
        sa.Column("body_forward", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("has_body", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("body_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "grouped_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "computed_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
    )

    # 现有文件都补一行"处理过"的标记（只写编号和时间，不读正文；一句 INSERT … SELECT）
    op.execute(
        "INSERT INTO repost_signals"
        " (document_id, cited, body_forward, has_body, body_at, grouped_at, computed_at)"
        " SELECT id, JSON_ARRAY(), 0, 0, NULL, CURRENT_TIMESTAMP(6), CURRENT_TIMESTAMP(6)"
        " FROM documents"
    )


def downgrade():
    op.drop_table("repost_signals")
