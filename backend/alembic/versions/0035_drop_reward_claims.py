"""删掉不再写入的旧奖励结构（D-071 第②条"注销即删"；同行引荐、首次批量奖励按 D-061、D-070 已删）。

- reward_claims 表：注销后防重复领取的凭据（openid 的 HMAC，能重算，是去标识化不是匿名化）。
- users 的 invite_code（连同唯一约束 uq_users_invite_code）、invited_by_user_id（连同外键
  fk_users_invited_by 和索引 ix_users_invited_by_user_id）、invited_at、batch_completed_at。
  batch_completed_at 线上有值（旧 worker 在每个用户第一次完成批量任务时都写，不看奖励开关），
  删列就是删掉这些时间；#293 起没有代码读写这四列。
- app_keys 里 name='reward_claim_queue' 那一行：旧代码写凭据前用它排队的行锁，不是密钥。

pass_rewards 不动：kind 约束里的 first_batch、referral 和旧流水还要用（旧流水照常显示）。

部署顺序和平时相反：先发 api（新代码不再映射这些列，在旧表上照样能用），旧版 api 归零后再发
admin 跑这个迁移；迁移以后 api、admin 都不能直接回退到这一版之前（旧代码查用户会报列不存在）。
这一版之前的备份要用当时版本的代码恢复（备份按表名和字段逐行核对），见 docs/部署/微信云托管.md。

MySQL 的 DDL 不能和别的语句一起回滚：users 的改动合成一条 ALTER（单条 DDL 在 MySQL 8 里是原子的），
放在最后（旧代码只有它受不了），之后只剩写版本号；每一步先看还在不在，中途失败后重跑能接着做完。
回退只重建空表、空列和约束，数据不恢复。

Revision ID: 0035
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0035"
down_revision = "0039"
branch_labels = None
depends_on = None
COLUMNS = {
    "invite_code": "VARCHAR(16) NULL",
    "invited_by_user_id": "INT NULL",
    "invited_at": "DATETIME(6) NULL",
    "batch_completed_at": "DATETIME(6) NULL",
}
# 等元数据锁的秒数（MySQL 默认等一年）
LOCK_WAIT_SECONDS = 5


def _short_lock_wait() -> None:
    # 有事务读过 users 还没结束（例如批量任务正在处理）时，ALTER 要等它；等的时候后面读 users 的
    # 接口请求都排在 ALTER 后面。宁可迁移失败（admin 起不来，旧版照常服务，过一会儿重新发布，
    # 每一步都能重跑），也不让接口一直卡着
    op.execute(f"SET SESSION lock_wait_timeout = {LOCK_WAIT_SECONDS}")


def _users_keys(inspector) -> set[str]:
    return (
        {i["name"] for i in inspector.get_indexes("users")}
        | {u["name"] for u in inspector.get_unique_constraints("users")}
        | {f["name"] for f in inspector.get_foreign_keys("users")}
    )


def upgrade() -> None:
    _short_lock_wait()
    op.execute("DELETE FROM app_keys WHERE name = 'reward_claim_queue'")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("reward_claims"):
        op.drop_table("reward_claims")
    present = {c["name"] for c in inspector.get_columns("users")}
    keys = _users_keys(inspector)
    clauses = ["DROP FOREIGN KEY fk_users_invited_by"] if "fk_users_invited_by" in keys else []
    clauses += [
        f"DROP INDEX {name}"
        for name in ("ix_users_invited_by_user_id", "uq_users_invite_code")
        if name in keys
    ]
    clauses += [f"DROP COLUMN {name}" for name in COLUMNS if name in present]
    if clauses:
        op.execute("ALTER TABLE users " + ", ".join(clauses))
    op.execute("SET SESSION lock_wait_timeout = DEFAULT")


def downgrade() -> None:
    # 和 0027 建的、0031 改过约束之后一样（测试比对建表语句）；排队行由旧代码第一次用到时自己写
    _short_lock_wait()
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("reward_claims"):
        op.create_table(
            "reward_claims",
            sa.Column("pseudonym", sa.String(64), primary_key=True),
            sa.Column("kind", sa.String(20), primary_key=True),
            sa.Column("claimed_at", mysql.DATETIME(fsp=6), primary_key=True),
            sa.CheckConstraint(
                "kind IN ('first_batch', 'referred', 'referral', 'boost_trial')",
                name="ck_reward_claim_kind",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_0900_bin",
        )
    if "ix_reward_claims_claimed" not in {
        i["name"] for i in sa.inspect(op.get_bind()).get_indexes("reward_claims")
    }:
        op.create_index("ix_reward_claims_claimed", "reward_claims", ["claimed_at"])
    present = {c["name"] for c in inspector.get_columns("users")}
    keys = _users_keys(inspector)
    clauses = [f"ADD COLUMN {name} {ddl}" for name, ddl in COLUMNS.items() if name not in present]
    if "uq_users_invite_code" not in keys:
        clauses.append("ADD CONSTRAINT uq_users_invite_code UNIQUE (invite_code)")
    if "ix_users_invited_by_user_id" not in keys:
        clauses.append("ADD INDEX ix_users_invited_by_user_id (invited_by_user_id)")
    if "fk_users_invited_by" not in keys:
        clauses.append(
            "ADD CONSTRAINT fk_users_invited_by FOREIGN KEY (invited_by_user_id)"
            " REFERENCES users (id) ON DELETE SET NULL"
        )
    if clauses:
        op.execute("ALTER TABLE users " + ", ".join(clauses))
    op.execute("SET SESSION lock_wait_timeout = DEFAULT")
