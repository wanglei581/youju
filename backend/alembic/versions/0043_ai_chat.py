"""问一问（D-078、D-093；开发设计 #328 第四节）：对话两张表、问答任务类型、问一问使用说明的同意。

- ai_chats：一条对话。每份通知一条（按建的时候那份通知的编号 doc_key 记，0 是不带通知的那条），
  (user_id, doc_key) 唯一；按通知找对话时换成现在的合并组再找（api/chat.py）。删用户时级联删除。
- ai_chat_turns：对话里的一问一答。skill 是 find（找通知）或 ask（问这份通知）；question 是用户打的字
  （找通知最多 100 字、问这份通知最多 200 字）；问这份通知有 task_id（任务删掉时置空）和 doc_id；
  answer_json 是找通知的回答、规则当场答的回答；(chat_id, seq)、(chat_id, idempotency_key) 唯一；
  expires_at 是建的时候加 30 天。不建 FK 到 documents：通知下架、删除时对话照样能打开、能删。
- 内容安全的违规记录表 ai_safety_hits 在 0042（门槛第 4 道）建好了：问一问的提问被拦时，记那句话
  （最多 200 字）和去标识化的用户编号；api 账号对它只有新增权限。
- advisor_tasks 的类型加 ask（只放宽，旧代码不受影响）。
- users 加"问一问使用说明"的同意（版本、时间），同批量工具的使用说明。
- ai_heartbeat 加 ask_ready：admin 写心跳时记"问答能用"（对外 AI 的单独线路配齐了），api 只读。

部署顺序：先发 admin（启动时跑这个迁移）→ 创始人执行 deploy/mysql_accounts.sql 里给 api 账号的三行
授权 → 验证 → 再发 api（第十五节"部署"）。

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "ai_chats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("doc_key", sa.Integer(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "doc_key", name="uq_ai_chats_user_doc"),
        **OPTIONS,
    )
    op.create_index("ix_ai_chats_user_updated", "ai_chats", ["user_id", "updated_at"])
    op.create_table(
        "ai_chat_turns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("skill", sa.String(8), nullable=False),
        sa.Column("question", sa.String(200), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("doc_id", sa.Integer(), nullable=True),
        sa.Column("answer_json", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("skill IN ('find', 'ask')", name="ck_ai_chat_turns_skill"),
        sa.ForeignKeyConstraint(["chat_id"], ["ai_chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["advisor_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "seq", name="uq_ai_chat_turns_chat_seq"),
        sa.UniqueConstraint("chat_id", "idempotency_key", name="uq_ai_chat_turns_chat_key"),
        **OPTIONS,
    )
    op.create_index("ix_ai_chat_turns_expires", "ai_chat_turns", ["expires_at"])
    op.drop_constraint("ck_advisor_tasks_kind", "advisor_tasks", type_="check")
    op.create_check_constraint(
        "ck_advisor_tasks_kind",
        "advisor_tasks",
        "kind IN ('compare', 'track', 'interpret', 'clause', 'ask')",
    )
    op.add_column("users", sa.Column("chat_agreement_version", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("chat_agreement_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column(
        "ai_heartbeat",
        sa.Column("ask_ready", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    # 先删问答任务和对话两张表，再把类型约束收回去（第四节第 2 条）
    op.drop_column("ai_heartbeat", "ask_ready")
    op.drop_column("users", "chat_agreement_at")
    op.drop_column("users", "chat_agreement_version")
    op.drop_index("ix_ai_chat_turns_expires", table_name="ai_chat_turns")
    op.drop_table("ai_chat_turns")
    op.drop_index("ix_ai_chats_user_updated", table_name="ai_chats")
    op.drop_table("ai_chats")
    op.execute("DELETE FROM advisor_tasks WHERE kind = 'ask'")
    op.drop_constraint("ck_advisor_tasks_kind", "advisor_tasks", type_="check")
    op.create_check_constraint(
        "ck_advisor_tasks_kind",
        "advisor_tasks",
        "kind IN ('compare', 'track', 'interpret', 'clause')",
    )
