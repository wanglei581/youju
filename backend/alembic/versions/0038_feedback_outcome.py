"""纠错处理结果（差距清单 C1）：feedback 加 outcome（处理结果）和 reply（给用户的一句说明）。

依据《网络信息内容生态治理规定》第十六条"及时受理处置公众投诉举报并反馈处理结果"：原来用户在
"我的数据"里只看得到"已处理"，看不到怎么处理的。旧记录两列都是空的，小程序照旧只写"已处理"。
api 账号对 feedback 本来就有 SELECT、INSERT、UPDATE、DELETE，不用改 mysql_accounts.sql。

编号说明：0033（#290）、0034（#297）、0035（#296）、0036（#304）、0037（第 4 批 access_daily）已被别的
PR 占用，都还没合并；这里先接在 main 的 0032 后面，谁后合并谁把 down_revision 接到前一个上。

Revision ID: 0038
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0032"
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
