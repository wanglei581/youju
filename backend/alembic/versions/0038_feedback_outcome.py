"""纠错处理结果（差距清单 C1）：feedback 加 outcome（处理结果）和 reply（给用户的一句说明）。

依据《网络信息内容生态治理规定》第十六条"及时受理处置公众投诉举报并反馈处理结果"：原来用户在
"我的数据"里只看得到"已处理"，看不到怎么处理的。旧记录两列都是空的，小程序照旧只写"已处理"。
api 账号对 feedback 本来就有 SELECT、INSERT、UPDATE、DELETE，不用改 mysql_accounts.sql。

编号说明：0037（#308 运维日报 access_daily）已合并，这里接在它后面。0033（#290）、0034（#297）、
0035（#296）、0036（#304）已被别的 PR 占用、还没合并，谁后合并谁把 down_revision 接到当时最新的迁移上。

Revision ID: 0038
Revises: 0037
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("outcome", sa.String(16), nullable=True))
    op.add_column("feedback", sa.Column("reply", sa.String(200), nullable=True))
    op.create_check_constraint(
        "ck_feedback_outcome",
        "feedback",
        "outcome IS NULL OR outcome IN "
        "('fixed', 'unchanged', 'removed', 'noted', 'added', 'declined', 'resolved')",
    )


def downgrade() -> None:
    # 删掉的只是处理结果和说明；是否已处理（handled_at）还在
    op.drop_constraint("ck_feedback_outcome", "feedback", type_="check")
    op.drop_column("feedback", "reply")
    op.drop_column("feedback", "outcome")
