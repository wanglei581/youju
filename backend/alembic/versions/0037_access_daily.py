"""每天每个服务的小程序接口请求汇总（差距清单 M3）：访问日志归档前算好，运维日报报接口成功率。

只有计数（请求数、5xx、超过 3 秒），不含 IP、用户编号和路径；随数据库备份。
api 账号对所有表有 SELECT，不需要写这张表，不用改 mysql_accounts.sql。

编号说明：0033（#290）、0034（#297）、0035（#296）、0036（#304）已被别的 PR 占用，都还没合并；
这里先接在 main 的 0032 后面，谁后合并谁把 down_revision 接到前一个上。

Revision ID: 0037
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0032"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "access_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("service", sa.String(16), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("slow", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day", "service"),
        **OPTIONS,
    )


def downgrade() -> None:
    # 只是汇总数，删掉不影响任何功能；访问日志归档本身还在对象存储里
    op.drop_table("access_daily")
