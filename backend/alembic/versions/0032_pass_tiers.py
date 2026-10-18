"""批量工具两档开卖（D-070）：订单记下天数和续买多送的天数、退款金额和发起方；赠送流水加两种。

- pay_orders.days：这一档的天数（30 或 90），下单快照；已有订单都是 30 天那一档（D-058 只卖过这一个）。
- pay_orders.bonus_days：下单时"续买多送"开关开着记 3，否则 0。
- pay_orders.invoice_type：下单时购买页写的"能开哪种发票"（YOUJU_PAY_INVOICE_TYPE 当时的值）。
- pay_orders.refund_fee / refund_source / refund_note：退多少（分，空按全额）、谁发起（self 自助、
  staff 负责人按规则发起）、人工退款的原因。
- pass_rewards.kind 加 renewal（续买多送）、trial（加量体验折算）：随订单发的赠送，每次一笔。

支付开关仍默认关闭。api 账号的新权限见 deploy/mysql_accounts.sql 的"批量工具两档开卖权限"。

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None
OLD_KINDS = "'first_batch', 'correction', 'source', 'referral', 'deadline_clue'"


def upgrade() -> None:
    op.add_column("pay_orders", sa.Column("days", sa.Integer(), nullable=True))
    op.execute("UPDATE pay_orders SET days = 30")
    op.alter_column("pay_orders", "days", existing_type=sa.Integer(), nullable=False)
    op.add_column(
        "pay_orders", sa.Column("bonus_days", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("pay_orders", sa.Column("invoice_type", sa.String(100), nullable=True))
    op.add_column("pay_orders", sa.Column("refund_fee", sa.Integer(), nullable=True))
    op.add_column("pay_orders", sa.Column("refund_source", sa.String(16), nullable=True))
    op.add_column("pay_orders", sa.Column("refund_note", sa.String(200), nullable=True))
    op.drop_constraint("ck_pass_rewards_kind", "pass_rewards", type_="check")
    op.create_check_constraint(
        "ck_pass_rewards_kind", "pass_rewards", f"kind IN ({OLD_KINDS}, 'renewal', 'trial')"
    )


def downgrade() -> None:
    # 旧代码不认这两种赠送（CHECK 也不允许），也只会全额退款。有还没到期的赠送天数、或按剩余天数退款
    # 还在办理中的订单时不能自动回退：删掉赠送会让用户少天数、后面的使用期留空档，旧代码会把部分退款
    # 按全额再退一次（Codex 审阅）。先人工处理再回退
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM pass_rewards r JOIN workbench_passes p ON p.reward_id = r.id"
            " WHERE r.kind IN ('renewal', 'trial') AND p.revoked_at IS NULL"
            " AND p.ends_at > UTC_TIMESTAMP(6)"
        )
    ).scalar()
    partial = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM pay_orders WHERE refund_fee IS NOT NULL AND refund_fee <> price"
            " AND status IN ('refunding', 'manual')"
        )
    ).scalar()
    if active or partial:
        raise RuntimeError(
            f"不能自动回退 0032：还有 {active} 段没到期的赠送天数、{partial} 笔按剩余天数退款还没办完"
            "（或转了人工）。先人工处理，再回退"
        )
    # 已收回或已过期的赠送：流水和对应的使用期一起删，使用期上的任务按外键断开。
    # 90 天的使用期、已退完的部分退款金额照旧保留在使用期和订单里，旧代码按已有记录处理
    op.execute(
        "DELETE FROM workbench_passes WHERE reward_id IN "
        "(SELECT id FROM (SELECT id FROM pass_rewards WHERE kind IN ('renewal', 'trial')) AS gifts)"
    )
    op.execute("DELETE FROM pass_rewards WHERE kind IN ('renewal', 'trial')")
    op.drop_constraint("ck_pass_rewards_kind", "pass_rewards", type_="check")
    op.create_check_constraint("ck_pass_rewards_kind", "pass_rewards", f"kind IN ({OLD_KINDS})")
    op.drop_column("pay_orders", "refund_note")
    op.drop_column("pay_orders", "refund_source")
    op.drop_column("pay_orders", "refund_fee")
    op.drop_column("pay_orders", "invoice_type")
    op.drop_column("pay_orders", "bonus_days")
    op.drop_column("pay_orders", "days")
